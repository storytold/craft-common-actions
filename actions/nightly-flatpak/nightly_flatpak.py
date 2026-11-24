#!/usr/bin/env python3
"""Prepare and check a nightly Flatpak build of a Craft app from its stable release files.

  prepare MANIFEST WORKDIR   Write WORKDIR/<id>.nightly.yml from the app's bundle manifest and
                             rewrite WORKDIR/stage (the unpacked stable tarball) for the nightly
                             ID. Prints stable-id=, app-id=, command= and manifest= lines.
  check-oci LAYOUT REF       Fail unless the OCI layout's image config labels org.flatpak.ref=REF.
  oci-manifest LAYOUT OUT    Copy the manifest blob that LAYOUT/index.json points at to OUT, byte
                             for byte (its sha256 is the digest the image is published under).
  oci-archive LAYOUT OUT     Write a deterministic tar of the OCI layout to OUT: sorted entries,
                             mtime 0, uid/gid 0, fixed modes.

The manifest installs the stage's desktop file, metainfo and MIME file under the nightly ID, and
desktop-file-name-suffix marks the menu entry. The stage's icons are renamed, and its desktop
file and metainfo point at the nightly ID, before the build. The nightly claims no file
associations: its desktop file loses MimeType= and its metainfo loses <provides><mediatype>. flatpak-builder's own rename keys
are not used: rename-desktop-file adds X-Flatpak-RenamedFrom=<stable>.desktop (desktop shells
then treat the nightly as the stable app's successor), rename-appdata-file adds
<provides><id>STABLE</id></provides> to the metainfo (software centres read it the same way),
and rename-icon walks the icon directory while renaming in it, so an icon renamed to a name that
starts with the old ID can be renamed again.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import tarfile
import xml.etree.ElementTree as ET

NIGHTLY_SUFFIX = ".nightly"
NAME_SUFFIX = " (Nightly)"
ID_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*(\.[A-Za-z_][A-Za-z0-9_-]*)+")
# Keys this rewrite adds or replaces; a manifest that already sets one is refused rather than merged.
REFUSED_KEYS = (
    "rename-desktop-file",
    "rename-icon",
    "rename-appdata-file",
    "rename-mime-file",
    "desktop-file-name-prefix",
    "desktop-file-name-suffix",
)


class PrepareError(Exception):
    pass


def _scalar(value: str) -> str:
    value = re.sub(r"\s+#.*$", "", value).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
    return value


def _top_level(text: str, key: str) -> list[re.Match[str]]:
    return list(re.finditer(rf"^{re.escape(key)}:[ \t]*(.*)$", text, re.MULTILINE))


def rewrite_manifest(text: str) -> tuple[str, dict[str, str]]:
    """Return the nightly manifest text and its stable-id, app-id and command."""
    id_lines = _top_level(text, "id") + _top_level(text, "app-id")
    if len(id_lines) != 1:
        raise PrepareError(f"expected one top-level id: or app-id: line, found {len(id_lines)}")
    stable_id = _scalar(id_lines[0].group(1))
    if not ID_RE.fullmatch(stable_id):
        raise PrepareError(f"invalid app ID in manifest: {stable_id!r}")
    if stable_id.endswith(NIGHTLY_SUFFIX):
        raise PrepareError(f"manifest already has a nightly ID: {stable_id}")
    for key in REFUSED_KEYS:
        if _top_level(text, key):
            raise PrepareError(f"manifest already sets {key}:")
    commands = _top_level(text, "command")
    if len(commands) != 1:
        raise PrepareError("expected one top-level command: line")
    command = _scalar(commands[0].group(1))
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.+-]*", command):
        raise PrepareError(f"invalid command in manifest: {command!r}")

    app_id = stable_id + NIGHTLY_SUFFIX
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    for path, required in (
        ("share/applications/{}.desktop", True),
        ("share/metainfo/{}.metainfo.xml", True),
        ("share/mime/packages/{}.xml", False),
    ):
        installed = "/app/" + path.format(stable_id)
        if installed in code:
            text = text.replace(installed, "/app/" + path.format(app_id))
        elif required:
            raise PrepareError(f"manifest does not install {installed}")

    line = _top_level(text, id_lines[0].group(0).split(":")[0])[0]
    added = f"id: {app_id}\ndesktop-file-name-suffix: '{NAME_SUFFIX}'"
    new_text = text[: line.start()] + added + text[line.end():]
    return new_text, {"stable-id": stable_id, "app-id": app_id, "command": command}


def rewrite_metainfo(text: str, stable_id: str, app_id: str) -> str:
    """Return the metainfo with the nightly component ID, launchable, stock icon and name."""
    root = ET.fromstring(text, parser=ET.XMLParser(target=ET.TreeBuilder(insert_comments=True)))
    if root.tag not in ("component", "application"):
        raise PrepareError(f"metainfo root is <{root.tag}>, not <component>")
    id_node = root.find("id")
    if id_node is None or (id_node.text or "").strip() not in (stable_id, f"{stable_id}.desktop"):
        found = None if id_node is None else (id_node.text or "").strip()
        raise PrepareError(f"metainfo <id> is {found!r}, expected {stable_id!r}")
    id_node.text = app_id
    for node in root.findall("launchable"):
        if node.get("type") == "desktop-id" and (node.text or "").strip() == f"{stable_id}.desktop":
            node.text = f"{app_id}.desktop"
    for node in root.findall("icon"):
        if node.get("type") == "stock" and (node.text or "").strip() == stable_id:
            node.text = app_id
    for provides in root.findall("provides"):
        for node in provides.findall("mediatype"):
            provides.remove(node)
        if len(provides) == 0:
            root.remove(provides)
    for node in root.findall("name"):
        if node.text and not node.text.endswith(NAME_SUFFIX):
            node.text = node.text.rstrip() + NAME_SUFFIX
    return ET.tostring(root, encoding="unicode", xml_declaration=True) + "\n"


def rewrite_desktop(text: str, stable_id: str, app_id: str) -> str:
    """Point the desktop entry's Icon= keys that name the stable icon at the nightly one and drop
    MimeType=, so the nightly takes no file associations from the stable app."""
    text = re.sub(rf"^(Icon(\[[^\]]*\])?[ \t]*=[ \t]*){re.escape(stable_id)}[ \t]*$",
                  rf"\g<1>{app_id}", text, flags=re.MULTILINE)
    return re.sub(r"^MimeType(\[[^\]]*\])?[ \t]*=.*\n?", "", text, flags=re.MULTILINE)


def rewrite_mime_icons(text: str, stable_id: str, app_id: str) -> str:
    """Rename <icon>/<generic-icon> elements naming the stable ID in a shared-mime-info file.
    flatpak-builder would otherwise prefix the stale name with the app ID on export."""
    return re.sub(rf"(<(?:generic-)?icon\s+name\s*=\s*)([\"']){re.escape(stable_id)}\2",
                  rf"\g<1>\g<2>{app_id}\g<2>", text)


def rename_icons(icons: Path, stable_id: str, app_id: str) -> int:
    """Rename <stable-id>.<ext> and <stable-id>-symbolic.<ext> icons; return how many."""
    renamed = 0
    for path in sorted(icons.rglob("*")) if icons.is_dir() else ():
        name = path.name
        if path.is_symlink() or not path.is_file() or not name.startswith(stable_id):
            continue
        rest = name[len(stable_id):]
        if rest.startswith(".") or rest.startswith("-symbolic."):
            path.rename(path.with_name(app_id + rest))
            renamed += 1
    return renamed


def _stage_file(stage: Path, relative: str) -> Path:
    path = stage / relative
    if path.is_symlink() or not path.is_file():
        raise PrepareError(f"the tarball has no regular file {relative}")
    return path


def prepare(manifest: Path, work: Path) -> dict[str, str]:
    text, info = rewrite_manifest(manifest.read_text(encoding="utf-8"))
    stable_id, app_id = info["stable-id"], info["app-id"]
    stage = work / "stage"
    metainfo = _stage_file(stage, f"share/metainfo/{stable_id}.metainfo.xml")
    metainfo.write_text(rewrite_metainfo(metainfo.read_text(encoding="utf-8"), stable_id, app_id),
                        encoding="utf-8")
    desktop = _stage_file(stage, f"share/applications/{stable_id}.desktop")
    desktop.write_text(rewrite_desktop(desktop.read_text(encoding="utf-8"), stable_id, app_id),
                       encoding="utf-8")
    mime = stage / "share" / "mime" / "packages" / f"{stable_id}.xml"
    if mime.is_symlink():
        raise PrepareError(f"the tarball's share/mime/packages/{stable_id}.xml is a symlink")
    if mime.is_file():
        mime.write_text(rewrite_mime_icons(mime.read_text(encoding="utf-8"), stable_id, app_id),
                        encoding="utf-8")
    if not rename_icons(stage / "share" / "icons", stable_id, app_id):
        raise PrepareError(f"the tarball has no share/icons/**/{stable_id}.* icon")
    out = work / f"{app_id}.yml"
    out.write_text(text, encoding="utf-8")
    info["manifest"] = str(out)
    return info


def oci_manifest_path(layout: Path) -> Path:
    manifests = json.loads((layout / "index.json").read_text(encoding="utf-8")).get("manifests", [])
    if len(manifests) != 1:
        raise PrepareError(f"expected one image in {layout}, found {len(manifests)}")
    algorithm, _, value = manifests[0]["digest"].partition(":")
    if algorithm != "sha256" or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise PrepareError(f"unsupported manifest digest {manifests[0]['digest']!r}")
    return layout / "blobs" / algorithm / value


def write_oci_manifest(layout: Path, out: Path) -> None:
    source = oci_manifest_path(layout)
    shutil.copyfile(source, out)
    if hashlib.sha256(out.read_bytes()).hexdigest() != source.name:
        raise PrepareError(f"the manifest blob {source.name} does not match its digest")


def write_oci_archive(layout: Path, out: Path) -> None:
    """Tar the layout's files and directories, with no owner, time or mode that varies by run."""
    def normalize(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        info.mtime = 0
        info.mode = 0o755 if info.isdir() else 0o644
        return info

    paths = sorted(layout.rglob("*"), key=lambda p: p.relative_to(layout).as_posix())
    with tarfile.open(out, "w", format=tarfile.PAX_FORMAT) as tar:
        for path in paths:
            if path.is_symlink() or not (path.is_dir() or path.is_file()):
                raise PrepareError(f"{path} is not a regular file or directory")
            info = normalize(tar.gettarinfo(str(path), arcname=path.relative_to(layout).as_posix()))
            if info.isdir():
                tar.addfile(info)
            else:
                with path.open("rb") as handle:
                    tar.addfile(info, handle)


def oci_flatpak_ref(layout: Path) -> str | None:
    """Return the org.flatpak.ref label of the single image in an OCI layout."""
    def blob(digest: str) -> dict:
        algorithm, _, value = digest.partition(":")
        return json.loads((layout / "blobs" / algorithm / value).read_text(encoding="utf-8"))

    manifests = json.loads((layout / "index.json").read_text(encoding="utf-8")).get("manifests", [])
    if len(manifests) != 1:
        raise PrepareError(f"expected one image in {layout}, found {len(manifests)}")
    config = blob(blob(manifests[0]["digest"])["config"]["digest"])
    return (config.get("config") or {}).get("Labels", {}).get("org.flatpak.ref")


def main(argv: list[str]) -> int:
    try:
        if len(argv) == 3 and argv[0] == "prepare":
            for key, value in prepare(Path(argv[1]), Path(argv[2])).items():
                print(f"{key}={value}")
            return 0
        if len(argv) == 3 and argv[0] == "check-oci":
            ref = oci_flatpak_ref(Path(argv[1]))
            if ref != argv[2]:
                raise PrepareError(f"OCI image label org.flatpak.ref is {ref!r}, expected {argv[2]!r}")
            return 0
        if len(argv) == 3 and argv[0] == "oci-manifest":
            write_oci_manifest(Path(argv[1]), Path(argv[2]))
            return 0
        if len(argv) == 3 and argv[0] == "oci-archive":
            write_oci_archive(Path(argv[1]), Path(argv[2]))
            return 0
    except (PrepareError, ET.ParseError, OSError, KeyError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

#!/usr/bin/env python3
"""Prepare and check a nightly Flatpak build of a Craft app from its stable release files.

  prepare MANIFEST WORKDIR   Write WORKDIR/<id>.nightly.yml from the app's bundle manifest and
                             rewrite WORKDIR/stage (the unpacked stable tarball) for the nightly
                             ID. Prints stable-id=, app-id=, command= and manifest= lines.
  check-oci LAYOUT REF       Fail unless the OCI layout's image config labels org.flatpak.ref=REF.

The manifest installs the stage's desktop file, metainfo and MIME file under the nightly ID, and
desktop-file-name-suffix marks the menu entry. The stage's icons are renamed, and its desktop
file and metainfo point at the nightly ID, before the build. flatpak-builder's own rename keys
are not used: rename-desktop-file adds X-Flatpak-RenamedFrom=<stable>.desktop (desktop shells
then treat the nightly as the stable app's successor), rename-appdata-file adds
<provides><id>STABLE</id></provides> to the metainfo (software centres read it the same way),
and rename-icon walks the icon directory while renaming in it, so an icon renamed to a name that
starts with the old ID can be renamed again.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys
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
    for node in root.findall("name"):
        if node.text and not node.text.endswith(NAME_SUFFIX):
            node.text = node.text.rstrip() + NAME_SUFFIX
    return ET.tostring(root, encoding="unicode", xml_declaration=True) + "\n"


def rewrite_desktop(text: str, stable_id: str, app_id: str) -> str:
    """Point the desktop entry's Icon= keys that name the stable icon at the nightly one."""
    return re.sub(rf"^(Icon(\[[^\]]*\])?[ \t]*=[ \t]*){re.escape(stable_id)}[ \t]*$",
                  rf"\g<1>{app_id}", text, flags=re.MULTILINE)


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
    if not rename_icons(stage / "share" / "icons", stable_id, app_id):
        raise PrepareError(f"the tarball has no share/icons/**/{stable_id}.* icon")
    out = work / f"{app_id}.yml"
    out.write_text(text, encoding="utf-8")
    info["manifest"] = str(out)
    return info


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
    except (PrepareError, ET.ParseError, OSError, KeyError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

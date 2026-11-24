#!/usr/bin/env python3
"""Launcher payload inventory, driven by the nightly registry (actions/nightly-feed/apps.json).

Reads a launcher archive as data: no extraction unless asked, no execution. Refuses links,
special members, traversal, CLI binaries, install hooks, a wrong-architecture GUI and, on macOS,
document or URL associations. Also holds the admission inputs every signer step reads:
`--admission DIR` with nightly-manifest.json and admission.json.

    inventory --app A --platform P --version ID --archive F [--output DIR] [--smoke-root DIR]
"""
import argparse
import gzip
import json
from pathlib import Path, PurePosixPath
import platform as host
import plistlib
import re
import shutil
import stat
import struct
import sys
import tarfile
import tempfile
import zipfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for _path in (HERE, ROOT / "actions/nightly-feed"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
import nightly_common as c  # noqa: E402
import nightly_feed as feed  # noqa: E402
import nightly_handoff as handoff  # noqa: E402
import nightly_identity as ni  # noqa: E402

MAX_MEMBERS = 10000
MAX_METADATA = 256 * 1024
MAX_CENTRAL_DIRECTORY = 8 * 1024 * 1024
CHUNK = 1024 * 1024
# Files a flat Windows archive may carry beside its GUI (packaging/windows/nightly.ps1 notices).
WINDOWS_NOTICES = {"README.md", "LICENSE", "LICENSE-MIT", "LICENSE-APACHE", "NOTICE", "ATTRIBUTION.md", "COPYRIGHT"}
ELF_MACHINE = {"x86_64": 62, "aarch64": 183}
MACHO_CPU = {"x86_64": 0x01000007, "aarch64": 0x0100000C}
PE_MACHINE = {"x86_64": 0x8664, "aarch64": 0xAA64}
ADMISSION_FIELDS = {"app", "repository", "tag", "tag_family", "identity", "nv", "source_commit", "producer_run_id",
                    "producer_run_attempt", "platforms", "published_at"}


# ---- Registry-derived names -------------------------------------------------------------------

def app_target(app, platform, registry=None):
    """(app record, target row) of a launcher target; refuses apps and platforms outside the registry."""
    registry = registry if registry is not None else feed.load_registry()
    record = feed.app_record(registry, app)
    c.require(record is not None, f"{app} has no launcher adapter in the nightly registry")
    c.require(platform in record["targets"], f"{app} has no {platform} launcher target")
    return record, record["targets"][platform]


def product_stem(record):
    """'PhotoCraft' from the registry's 'PhotoCraft Nightly'."""
    return record["product_name"][: -len(" Nightly")]


def asset_names(row, version):
    """{'asset': final release name, 'unsigned': producer payload name} for one identity."""
    parts = ni.identity_parts(version)
    c.require(parts["identity"] == version, "the version must be a bare nightly identity")
    names = {"asset": row["asset_pattern"].replace("{version}", version),
             "unsigned": row["unsigned_pattern"].replace("{version}", version)}
    c.require(all(c.NAME.fullmatch(name) for name in names.values()), "unsafe launcher asset name")
    return names


def version_line(record, version):
    """What the nightly GUI prints for `--launch-profile <channel> --profile-root D --version`."""
    return f"{product_stem(record)} {version} ({record['build_channel']})\n"


# ---- Admission inputs -------------------------------------------------------------------------

def load_admission(directory, registry=None):
    """(manifest, admission) from --admission DIR, validated and cross-checked field by field."""
    registry = registry if registry is not None else feed.load_registry()
    directory = c.owned_path(directory)
    manifest = handoff.validate(c.bounded_json(directory / handoff.FILE_NAME), registry)
    admission = c.bounded_json(directory / "admission.json")
    c.fields(admission, ADMISSION_FIELDS, "admission")
    pairs = {"app": "app", "repository": "repository", "tag": "tag", "tag_family": "tag_family",
             "identity": "identity", "nv": "nv", "source_commit": "source_commit", "producer_run_id": "run_id",
             "producer_run_attempt": "run_attempt", "platforms": "launcher_platforms", "published_at": "published_at"}
    for key, field in pairs.items():
        c.require(admission[key] == manifest[field], f"admission {key} differs from the handoff manifest")
    return manifest, admission


def manifest_entry(manifest, name):
    entries = [entry for entry in manifest["files"] if entry["name"] == name]
    c.require(len(entries) == 1, f"{name} is not in the handoff manifest")
    return entries[0]


def admitted_file(manifest, directory, name):
    """Path of an admitted producer file, re-hashed against the manifest.

    Accepts the handoff layout (<dir>/<artifact>/<name>) and a flat directory (<dir>/<name>).
    """
    entry = manifest_entry(manifest, name)
    directory = c.owned_path(directory)
    nested = directory / entry["artifact"]
    folder = nested if (nested / name).exists() or (nested / name).is_symlink() else directory
    size, digest = c.digest_file(folder, name)
    c.require((size, digest) == (entry["size"], entry["sha256"]), f"{name} differs from the handoff manifest")
    return folder / name


# ---- Archive reading --------------------------------------------------------------------------

def member_name(value, app):
    c.require(isinstance(value, str) and 0 < len(value.encode("utf-8")) <= 1024 and "\x00" not in value
              and "\\" not in value, "unsafe archive name")
    while value.startswith("./"):
        value = value[2:]
    if value in ("", "."):
        return "."
    path = PurePosixPath(value)
    c.require(len(path.parts) <= 32, "archive path depth exceeds bound")
    c.require(not path.is_absolute() and all(part not in ("", ".", "..") for part in value.rstrip("/").split("/")),
              "archive path escapes root")
    banned = {"portable.txt", f"{app}-cli", f"{app}-cli.exe", "apprun", "install", "uninstall"}
    c.require(not any(part.casefold() in banned or part.casefold().endswith(
        (".desktop", ".sh", ".bat", ".cmd", ".ps1", ".dmg", ".appimage")) for part in path.parts),
        "CLI, portable marker, handler or install hook is forbidden")
    return path.as_posix().rstrip("/")


class BoundedTarReader:
    """Refuses oversized metadata reads before tarfile allocates a PAX or long-name buffer."""

    def __init__(self, stream):
        self.stream, self.metadata, self.metadata_bytes = stream, True, 0

    def read(self, size=-1):
        c.require(0 <= size <= (MAX_METADATA if self.metadata else CHUNK), "oversized archive metadata read")
        if self.metadata:
            self.metadata_bytes += size
            c.require(self.metadata_bytes <= MAX_CENTRAL_DIRECTORY, "cumulative TAR metadata exceeds bound")
        c.require(self.tell() + size <= c.MAX_TOTAL_BYTES + MAX_CENTRAL_DIRECTORY, "expanded archive exceeds bound")
        return self.stream.read(size)

    def seek(self, offset, whence=0):
        c.require(whence == 0 and 0 <= offset <= c.MAX_TOTAL_BYTES + MAX_CENTRAL_DIRECTORY, "archive seek exceeds bound")
        return self.stream.seek(offset)

    def tell(self):
        return self.stream.tell()


def zip_metadata_bound(stream, size):
    """Bound the central directory before ZipFile allocates it. ZIP64 and split archives refuse."""
    stream.seek(max(0, size - 65557))
    tail = stream.read(65557)
    offset = tail.rfind(b"PK\x05\x06")
    c.require(offset >= 0 and len(tail) - offset >= 22, "missing ZIP end record")
    c.require(offset < 20 or tail[offset - 20:offset - 16] != b"PK\x06\x07", "ZIP64 locator is unsupported")
    _, disk, start_disk, count_disk, count, total, central, comment = struct.unpack("<4s4H2IH", tail[offset:offset + 22])
    c.require(disk == start_disk == 0 and count_disk == count and count <= MAX_MEMBERS and total <= MAX_CENTRAL_DIRECTORY
              and central + total <= size and offset + 22 + comment == len(tail),
              "ZIP central directory exceeds bounds or uses an unsupported ZIP64/split layout")
    stream.seek(0)


def check_entrypoint(platform, arch, header):
    if platform == "linux":
        c.require(len(header) >= 64 and header[:7] == b"\x7fELF\x02\x01\x01"
                  and int.from_bytes(header[16:18], "little") in (2, 3)
                  and int.from_bytes(header[18:20], "little") == ELF_MACHINE[arch], f"not a {arch} ELF payload")
    elif platform == "macos":
        c.require(len(header) >= 32 and header[:4] == b"\xcf\xfa\xed\xfe"
                  and int.from_bytes(header[4:8], "little") == MACHO_CPU[arch]
                  and int.from_bytes(header[12:16], "little") == 2, f"not a thin {arch} Mach-O payload")
    else:
        c.require(len(header) >= 64 and header[:2] == b"MZ", "not a PE executable")
        offset = int.from_bytes(header[60:64], "little")
        c.require(64 <= offset and len(header) >= offset + 94, "invalid PE header offset")
        pe = header[offset:offset + 94]
        # Optional header Subsystem 2 = Windows GUI: the nightly never opens a console.
        c.require(pe[:4] == b"PE\0\0" and int.from_bytes(pe[4:6], "little") == PE_MACHINE[arch]
                  and pe[92:94] == b"\x02\x00", f"nightly executable must be a {arch} Windows GUI")


def check_plist(record, row, plist, version):
    bundle_executable = row["entrypoint"].rsplit("/", 1)[1]
    key = product_stem(record).replace(" ", "") + "Version"
    base = ni.identity_parts(version)["base"]
    c.require(isinstance(plist, dict) and plist.get("CFBundleExecutable") == bundle_executable
              and plist.get("CFBundleIdentifier") == row["isolation"]["package_identity"],
              "nightly Mac bundle identity mismatch")
    c.require(plist.get(key) == version, f"missing or mismatched full {key}; a numeric fallback is refused")
    c.require(plist.get("CFBundleVersion") == base and plist.get("CFBundleShortVersionString") == base,
              "Apple numeric versions must be the identity's stable base")
    c.require(not any(plist.get(name) for name in ("CFBundleDocumentTypes", "CFBundleURLTypes",
                                                   "UTExportedTypeDeclarations", "UTImportedTypeDeclarations")),
              "Mac document and URL associations are forbidden")


def verify_archive(app, platform, archive, version, output=None, registry=None):
    """Inventory one launcher archive; with `output` (a new absolute directory), also unpack it.

    The archive must carry the target's final or producer (unsigned) name for this identity.
    """
    record, row = app_target(app, platform, registry)
    names = asset_names(row, version)
    archive = c.owned_path(archive)
    c.require(archive.name in (names["asset"], names["unsigned"]), "archive name differs from the exact identity")
    arch, entrypoint = row["target"]["arch"], row["entrypoint"]
    bundle = entrypoint.split("/", 1)[0] if platform == "macos" else None
    plist_name = f"{bundle}/Contents/Info.plist" if bundle else None
    out = None
    if output is not None:
        c.require(Path(output).is_absolute(), "unpack output must be a new absolute directory")
        out = c.owned_path(output)
        c.require(not out.exists() and not out.is_symlink(), "unpack output must be a new absolute directory")
    raw, info = c.regular_file(archive.parent, archive.name)
    seen, files, required_dirs = set(), set(), set()
    state = {"total": 0, "header": None, "plist": None, "executable": False}
    staging = None
    try:
        if out is not None:
            out.parent.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix=".nightly-unpack-", dir=out.parent))

        def consume(name, size, is_dir, mode, read):
            c.require(not name.endswith("/") or is_dir, "regular file has directory spelling")
            name = member_name(name, app)
            c.require(name != "." or (is_dir and size == 0 and platform == "linux"), "invalid root directory entry")
            key = name.casefold()
            c.require(key not in seen and len(seen) < MAX_MEMBERS, "duplicate or oversized archive inventory")
            parts = key.split("/")
            ancestors = {"/".join(parts[:index]) for index in range(1, len(parts))}
            c.require(not ancestors & files and (is_dir or key not in required_dirs),
                      "archive file and directory hierarchy conflicts")
            required_dirs.update(ancestors)
            seen.add(key)
            if not is_dir:
                files.add(key)
            if platform == "macos":
                c.require(name == bundle or name.startswith(bundle + "/"),
                          f"Mac ZIP must contain exactly one {bundle} bundle")
            if platform == "windows":
                c.require(not is_dir and "/" not in name and (name == entrypoint or name in WINDOWS_NOTICES
                          or re.fullmatch(r"OFL-[A-Za-z0-9._-]+\.txt", name)), f"unexpected Windows ZIP member {name}")
            c.require(type(size) is int and 0 <= size <= c.MAX_ASSET_BYTES, "member exceeds size bound")
            state["total"] += size
            c.require(state["total"] <= c.MAX_TOTAL_BYTES, "expanded payload exceeds bound")
            destination = staging / name if staging is not None else None
            if is_dir:
                c.require(size == 0, "directory contains payload")
                if destination is not None:
                    destination.mkdir(parents=True, exist_ok=True)
                return
            target = None
            if destination is not None:
                destination.parent.mkdir(parents=True, exist_ok=True)
                target = destination.open("xb")
            captured, consumed = bytearray(), 0
            cap = MAX_METADATA if name == plist_name else 4096
            try:
                while block := read(CHUNK):
                    consumed += len(block)
                    c.require(consumed <= size, "member grew beyond its declared size")
                    if target is not None:
                        target.write(block)
                    captured.extend(block[:max(0, cap - len(captured))])
                c.require(consumed == size, "truncated member")
            finally:
                if target is not None:
                    target.close()
            if name == entrypoint:
                state["header"] = bytes(captured)
                state["executable"] = platform == "windows" or bool(mode & 0o111)
                if destination is not None:
                    destination.chmod(0o700)
            if name == plist_name:
                c.require(size <= MAX_METADATA, "plist exceeds metadata bound")
                state["plist"] = plistlib.loads(bytes(captured))

        if row["target"]["format"] == "tar_gz":
            with raw, gzip.GzipFile(fileobj=raw) as expanded:
                reader = BoundedTarReader(expanded)
                with tarfile.open(fileobj=reader, mode="r:") as container:
                    for member in container:
                        c.require(member.isfile() or member.isdir(), "links and special TAR members are forbidden")
                        reader.metadata = False
                        stream = container.extractfile(member) if member.isfile() else None
                        try:
                            consume(member.name, member.size, member.isdir(), member.mode,
                                    stream.read if stream else (lambda _size: b""))
                        finally:
                            reader.metadata = True
                            if stream is not None:
                                stream.close()
                # TAR stops at its end marker; gzip integrity is checked only at EOF.
                limit = c.MAX_TOTAL_BYTES + MAX_CENTRAL_DIRECTORY
                while expanded.read(min(CHUNK, limit - expanded.tell() + 1)):
                    c.require(expanded.tell() <= limit, "expanded archive exceeds bound")
        else:
            with raw:
                zip_metadata_bound(raw, info.st_size)
                with zipfile.ZipFile(raw) as container:
                    for member in container.infolist():
                        mode = member.external_attr >> 16
                        c.require(not member.flag_bits & 1 and stat.S_IFMT(mode) in (0, stat.S_IFREG, stat.S_IFDIR),
                                  "encrypted, link or special ZIP member")
                        with container.open(member) as stream:
                            consume(member.filename, member.file_size, member.is_dir(), mode, stream.read)
        c.require(state["header"] is not None and state["executable"], "missing or non-executable GUI entrypoint")
        check_entrypoint(platform, arch, state["header"])
        if platform == "macos":
            check_plist(record, row, state["plist"], version)
        after = archive.stat(follow_symlinks=False)
        c.require((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                  == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns), "archive changed during inspection")
        if staging is not None:
            c.require(not out.exists() and not out.is_symlink(), "unpack output must remain a new directory")
            staging.rename(out)
            staging = None
        return {"archive": archive.name, "members": len(seen), "expanded_bytes": state["total"],
                "entrypoint": entrypoint, "unpacked": str(out) if out is not None else None}
    finally:
        raw.close()
        if staging is not None:
            shutil.rmtree(staging)


def host_runs(row):
    """Whether this machine can execute the target's GUI (smoke tests run only there)."""
    system = {"linux": "linux", "darwin": "macos", "win32": "windows"}.get(sys.platform)
    machine = {"amd64": "x86_64", "x86_64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}.get(host.machine().lower())
    return (system, machine) == (row["target"]["os"], row["target"]["arch"])


def main(argv):
    parser = argparse.ArgumentParser(prog="inventory", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--app", required=True)
    parser.add_argument("--platform", required=True, choices=feed.LAUNCHER_PLATFORMS)
    parser.add_argument("--version", required=True, help="the nightly identity")
    parser.add_argument("--archive", required=True)
    parser.add_argument("--output", help="new absolute directory to unpack into")
    parser.add_argument("--smoke-root", help="new directory: unpack there and run the smoke test when this host can")
    args = parser.parse_args(argv)
    try:
        c.require(not (args.output and args.smoke_root), "--output and --smoke-root are exclusive")
        if not args.smoke_root:
            result = verify_archive(args.app, args.platform, args.archive, args.version, args.output)
        else:
            root = Path(args.smoke_root).absolute()
            record, row = app_target(args.app, args.platform)
            result = verify_archive(args.app, args.platform, args.archive, args.version, root / "payload")
            if host_runs(row):
                import smoke
                result["smoke"] = smoke.smoke(args.app, root / "payload" / row["entrypoint"], root / "profile",
                                              args.version)
            else:
                result["smoke"] = None
                print(f"Smoke test skipped: this host cannot run a {args.platform}-{row['target']['arch']} payload.",
                      file=sys.stderr)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (c.InvalidNightly, OSError, EOFError, ValueError, KeyError, TypeError, RecursionError, tarfile.TarError,
            zipfile.BadZipFile, plistlib.InvalidFileException) as error:
        print(f"::error::nightly launcher inventory refused: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

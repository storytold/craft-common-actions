"""Shared fail-closed primitives for the nightly tools: bounded reads, owned paths, digests."""
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import stat

MAX_JSON_BYTES = 256 * 1024
MAX_ARTIFACTS = 64
MAX_ASSET_BYTES = 8 * 1024**3
MAX_TOTAL_BYTES = 16 * 1024**3
HEX40 = re.compile(r"[0-9a-f]{40}")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
BASE = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
TARGET_OS = {"windows", "macos", "linux", "freebsd"}
TARGET_ARCH = {"x86", "x86_64", "aarch64"}
# System installers/ordinary DMGs cannot silently reuse stable package identities.
PORTABLE_FORMATS = {"portable_zip": ".zip", "appimage": ".AppImage", "tar_gz": ".tar.gz"}

class InvalidNightly(ValueError):
    pass

class Unavailable(InvalidNightly):
    pass

def require(condition, reason):
    if not condition:
        raise InvalidNightly(reason)

def fields(value, expected, where):
    require(isinstance(value, dict) and set(value) == set(expected), f"invalid {where} fields")

def positive(value, where):
    require(type(value) is int and 0 < value < 2**63, f"invalid {where}")

# On Windows, os.stat() reports 0o777 for every directory without the read-only
# attribute: the mode bits describe that attribute, not the directory's ACL.
POSIX_MODE_PERMISSIONS = os.name != "nt"


def world_writable(info):
    """Report a directory any local account may write, where mode bits say so.

    POSIX: the other-write bit, with or without the sticky bit, since a sticky
    shared scratch directory is still shared. Windows: mode bits prove nothing,
    so the root relies on the drive, traversal, real-directory (resolve also
    follows junctions), link and containment checks.
    """
    return POSIX_MODE_PERMISSIONS and bool(info.st_mode & stat.S_IWOTH)


def owned_path(path):
    """Confine CLI-derived I/O to this job's workspace or private runner temp."""
    raw = os.fspath(path)
    require(isinstance(raw, str) and raw and "\0" not in raw, "invalid owned path")
    candidate = Path(raw)
    require(".." not in candidate.parts and ".." not in PureWindowsPath(raw).parts,
            "owned path traversal forbidden")
    require(os.name == "nt" or not PureWindowsPath(raw).drive, "foreign path drive forbidden")
    absolute = candidate.absolute()
    roots = [Path(os.environ.get("GITHUB_WORKSPACE") or Path.cwd())]
    if temporary := os.environ.get("RUNNER_TEMP"):
        roots.append(Path(temporary))
    for root in roots:
        require(root.is_absolute() and root.parent != root and ".." not in root.parts,
                "job root must be an absolute private directory")
        for component in (root, *root.parents):
            require(not component.is_symlink(), "job root has a symlink ancestor")
        require(root.is_dir() and root.resolve() == root, "job root must be a real directory")
        # Shared scratch directories are writable by every user; a job root must be private.
        require(not world_writable(root.stat()), "job root must not be world-writable")
    require(any(absolute.is_relative_to(root) for root in roots), "path outside owned job roots")
    for component in (absolute, *absolute.parents):
        require(not component.is_symlink(), "owned path has a symlink ancestor or leaf")
    resolved = absolute.resolve()
    require(any(resolved.is_relative_to(root) for root in roots), "resolved path outside owned job roots")
    return resolved


def bounded_bytes(path, limit, kind):
    """Read a stable regular input without following a leaf link or waiting on a FIFO."""
    path = owned_path(path)
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode), f"{kind} must be a regular non-symlink file")
    require(before.st_size <= limit, f"{kind} exceeds bound")
    def stamp(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        require(stamp(before) == stamp(opened), f"{kind} changed before read")
        raw = stream.read(limit + 1)
        require(stamp(opened) == stamp(os.fstat(stream.fileno())) and len(raw) == opened.st_size,
                f"{kind} changed while reading")
    require(len(raw) <= limit, f"{kind} exceeds bound")
    return raw


def bounded_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "duplicate JSON key")
            result[key] = value
        return result
    raw = bounded_bytes(path, MAX_JSON_BYTES, "JSON")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(InvalidNightly("non-finite JSON")))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise InvalidNightly("invalid bounded UTF-8 JSON") from error


def regular_file(directory, name):
    require(isinstance(name, str) and NAME.fullmatch(name), "invalid asset basename")
    path = owned_path(Path(directory) / name)
    require(not path.is_symlink(), "asset must not be a symlink")
    require(path.resolve().parent == Path(directory).resolve(), "asset escapes artifact directory")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    stream = os.fdopen(descriptor, "rb")
    info = os.fstat(stream.fileno())
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_ASSET_BYTES:
        stream.close()
        raise InvalidNightly("asset is not a bounded non-empty regular file")
    return stream, info

def digest_file(directory, name):
    stream, before = regular_file(directory, name)
    digest = hashlib.sha256(); consumed = 0
    with stream:
        while block := stream.read(1024 * 1024):
            consumed += len(block)
            require(consumed <= MAX_ASSET_BYTES, "asset grew beyond limit")
            digest.update(block)
        after = os.fstat(stream.fileno())
    require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) ==
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) and consumed == before.st_size,
            "asset changed while hashing")
    return consumed, digest.hexdigest()


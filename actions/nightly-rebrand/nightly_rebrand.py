#!/usr/bin/env python3
"""Turn one architecture's stable Linux packages of a Craft app into -nightly packages.

The nightly packages install beside the stable ones: the package is named <prefix>-nightly, and
every installed file named after a binary, the prefix or the app ID is renamed, so no file path is
shared with the stable package. Inputs are <prefix>-<version>-linux-<arch>.{tar.gz,deb,rpm} from an
app's packaging/linux/package.sh; outputs are <prefix>-nightly-<nightly-version>-linux-<arch>.<ext>.

usage: nightly_rebrand.py --input-dir DIR --output-dir DIR --version V --nightly-version NV
"""
from __future__ import annotations

import argparse
import bz2
import collections
import gzip
import hashlib
import io
import json
import lzma
import os
import posixpath
import re
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

SUFFIX = '-nightly'
ID_SUFFIX = '.nightly'
LABEL = ' (Nightly)'
EXTENSIONS = ('tar.gz', 'deb', 'rpm')
COMPLETION_DIRS = ('/usr/share/bash-completion/', '/usr/share/zsh/', '/usr/share/fish/')
# deb relation fields that may name the stable package; the nightly drops those clauses.
DEB_RELATIONS = ('Conflicts', 'Breaks', 'Replaces', 'Provides')
RPM_SCRIPTS = {'PREIN': 'preinstall', 'POSTIN': 'postinstall',
               'PREUN': 'preremove', 'POSTUN': 'postremove'}
RPM_TRANS_SCRIPTS = {'PRETRANS': 'pretrans', 'POSTTRANS': 'posttrans'}


class RebrandError(Exception):
    pass


def refuse_nightly_input(what: str) -> None:
    raise RebrandError(f"{what}: the app's packaging already produced nightly packages, and the "
                       "nightly conversion cannot also apply to them")


@dataclass(frozen=True)
class Entry:
    path: str           # absolute install path, e.g. /usr/bin/photocraft
    kind: str           # 'file', 'dir' or 'symlink'
    mode: int = 0o644   # permission bits
    data: bytes = b''
    link: str = ''
    mtime: int = 0
    config: bool = False


@dataclass(frozen=True)
class Names:
    """The names a nightly renames: binaries in /usr/bin and the desktop app IDs."""
    tokens: tuple[str, ...]   # binary names, longest first
    app_ids: tuple[str, ...]  # desktop file basenames without .desktop, longest first

    @classmethod
    def from_entries(cls, prefix: str, entries: list[Entry]) -> 'Names':
        bins = {posixpath.basename(e.path) for e in entries
                if posixpath.dirname(e.path) == '/usr/bin' and e.kind != 'dir'}
        if prefix not in bins:
            raise RebrandError(f'/usr/bin/{prefix} is missing: the main binary must be named after the prefix')
        ids = {posixpath.basename(e.path)[:-len('.desktop')] for e in entries
               if posixpath.dirname(e.path) == '/usr/share/applications'
               and e.path.endswith('.desktop') and e.kind != 'dir'}
        if prefix + SUFFIX in bins:
            refuse_nightly_input(f'/usr/bin/{prefix}{SUFFIX} is already in the package')
        for app_id in ids:
            if app_id.endswith(ID_SUFFIX):
                refuse_nightly_input(f'the desktop ID {app_id} already ends in {ID_SUFFIX}')
        def longest_first(names):
            return tuple(sorted(names, key=lambda n: (-len(n), n)))
        return cls(longest_first(bins), longest_first(ids))

    def rename_id(self, name: str) -> str:
        for app_id in self.app_ids:
            if name == app_id or name.startswith(app_id + '.'):
                return app_id + ID_SUFFIX + name[len(app_id):]
        return name

    def component(self, name: str, parent: str) -> str:
        """Nightly name of one path component; parent is its stable parent directory."""
        if parent == '/usr/bin':
            return name + SUFFIX
        renamed = self.rename_id(name)
        if renamed != name:
            return renamed
        in_man = parent.startswith('/usr/share/man/')
        for token in self.tokens:
            # `_cmd` is a zsh completion; man pages also carry subcommand pages (cmd-sub.1).
            for head in (token, '_' + token):
                if (name == head or name.startswith(head + '.')
                        or (in_man and head == token and name.startswith(head + '-'))):
                    return head + SUFFIX + name[len(head):]
        return name

    def path(self, path: str) -> str:
        parent, out = '/', []
        for part in path.strip('/').split('/'):
            out.append(self.component(part, parent))
            parent = posixpath.join(parent, part)
        return '/' + '/'.join(out)


def exact(word: str) -> str:
    """Pattern for word as a whole name, not part of a longer identifier or path component."""
    return r'(?<![\w.-])' + re.escape(word) + r'(?![\w-])'


def sub(pattern: str, replacement: str, text: str) -> str:
    return re.sub(pattern, lambda _m: replacement, text)


def rewrite_tokens(text: str, names: Names, path_map: dict[str, str]) -> str:
    """Rewrite paths, app IDs and command names in a script.

    path_map keys are absolute install paths, or paths relative to the tarball's top directory
    (bin/<cmd>), which may follow a directory variable such as "$dir/bin/<cmd>"."""
    for old, new in sorted(path_map.items(), key=lambda kv: -len(kv[0])):
        if old != new:
            before = r'(?<![\w./-])' if old.startswith('/') else r'(?<![\w.-])'
            text = sub(before + re.escape(old) + r'(?![\w.-])', new, text)
    for app_id in names.app_ids:
        text = sub(exact(app_id) + r'(?!' + re.escape(ID_SUFFIX) + r'(?![\w-]))', app_id + ID_SUFFIX, text)
    for token in names.tokens:
        text = sub(exact(token), token + SUFFIX, text)
    return text


def rewrite_command(value: str, names: Names) -> str:
    m = re.match(r'(\s*"?)([^\s"]+)(.*)', value, re.S)
    if not m:
        return value
    program = m.group(2)
    base = posixpath.basename(program)
    if base in names.tokens and (program == base or posixpath.dirname(program) == '/usr/bin'):
        program += SUFFIX
    return m.group(1) + program + m.group(3)


def rewrite_desktop(text: str, names: Names) -> str:
    out, group = [], None
    for line in text.splitlines(keepends=True):
        body = line.rstrip('\r\n')
        stripped = body.strip()
        if stripped.startswith('[') and stripped.endswith(']'):
            group = stripped[1:-1]
        elif '=' in body and not stripped.startswith('#'):
            key, value = body.split('=', 1)
            base = key.strip().split('[', 1)[0]
            if base == 'Name' and group == 'Desktop Entry':
                value = value.rstrip() + LABEL
            elif base in ('Exec', 'TryExec'):
                value = rewrite_command(value, names)
            elif base == 'Icon':
                value = names.rename_id(value.strip())
            line = f'{key}={value}{line[len(body):]}'
        out.append(line)
    return ''.join(out)


_XML_TAG = re.compile(
    r'<!--.*?-->|<\?.*?\?>|<!\[CDATA\[.*?\]\]>|<!(?:[^>"\']|"[^"]*"|\'[^\']*\')*>'
    r'|<(/?)([\w:.-]+)(?:[^>"\']|"[^"]*"|\'[^\']*\')*?(/?)>', re.S)


def rewrite_metainfo(text: str, names: Names, path_map: dict[str, str]) -> str:
    """Rename the component's ID, desktop ID, icon and binaries; label its <name>."""
    edits = []    # (start, end, new text) of leaf element contents
    stack = []    # (tag, end of its start tag, index of the tag match)
    for index, m in enumerate(_XML_TAG.finditer(text)):
        closing, tag, empty = m.group(1), m.group(2), m.group(3)
        if tag is None or empty:
            continue
        if not closing:
            stack.append((tag, m.end(), index))
            continue
        open_tag, start, open_index = stack.pop()
        if open_tag != tag or open_index != index - 1:
            continue  # not a leaf (children or comments inside)
        depth = len(stack)  # the component root is depth 0
        content = text[start:m.start()]
        value = content.strip()
        if tag == 'name' and depth == 1:
            new = re.sub(r'(\s*)\Z', lambda w: LABEL + w.group(1), content, count=1)
        elif tag == 'binary' and value in names.tokens:
            new = content.replace(value, value + SUFFIX)
        elif value in path_map:
            new = content.replace(value, path_map[value])
        else:
            new = content.replace(value, names.rename_id(value)) if value else content
        if new != content:
            edits.append((start, m.start(), new))
    for start, end, new in reversed(edits):
        text = text[:start] + new + text[end:]
    return text


def rewrite_mime(text: str, names: Names) -> str:
    # Type names stay; only an icon named after the app ID follows the renamed icon files.
    return re.sub(r'(<(?:generic-)?icon\s+name=")([^"]*)(")',
                  lambda m: m.group(1) + names.rename_id(m.group(2)) + m.group(3), text)


def rewrite_completion(text: str, names: Names, zsh: bool) -> str:
    out = []
    for line in text.splitlines(keepends=True):
        words = line.split(None, 1)
        if words and words[0] in ('complete', 'compdef', '#compdef'):
            for token in names.tokens:
                line = sub(exact(token), token + SUFFIX, line)
        out.append(line)
    text = ''.join(out)
    if zsh:
        # zsh autoloads _cmd-nightly from its file and compares $funcstack[1] with the function name.
        for token in names.tokens:
            text = sub(exact('_' + token), '_' + token + SUFFIX, text)
    return text


def rewrite_content(entry: Entry, names: Names, path_map: dict[str, str]) -> bytes:
    p = entry.path
    if entry.kind != 'file':
        return entry.data
    if p.startswith('/usr/share/applications/') and p.endswith('.desktop'):
        rewrite = lambda t: rewrite_desktop(t, names)
    elif p.startswith('/usr/share/metainfo/') and p.endswith('.xml'):
        rewrite = lambda t: rewrite_metainfo(t, names, path_map)
    elif p.startswith('/usr/share/mime/packages/') and p.endswith('.xml'):
        rewrite = lambda t: rewrite_mime(t, names)
    elif p.startswith(COMPLETION_DIRS):
        rewrite = lambda t: rewrite_completion(t, names, p.startswith('/usr/share/zsh/'))
    else:
        return entry.data
    try:
        text = entry.data.decode('utf-8')
    except UnicodeDecodeError:
        return entry.data
    return rewrite(text).encode('utf-8')


def rewrite_link(entry: Entry, path_map: dict[str, str]) -> str:
    target = entry.link
    if entry.kind != 'symlink':
        return target
    absolute = target if target.startswith('/') else posixpath.normpath(
        posixpath.join(posixpath.dirname(entry.path), target))
    new = path_map.get(absolute, absolute)
    if new == absolute:
        return target
    if target.startswith('/'):
        return new
    return posixpath.relpath(new, posixpath.dirname(path_map[entry.path]))


def rewrite_tarball_extra(entry: Entry, names: Names, path_map: dict[str, str]) -> bytes:
    """Rewrite a text file the tarball carries outside bin/ and share/, such as filmcraft's
    install.sh, which names files relative to the top directory."""
    try:
        text = entry.data.decode('utf-8')
    except UnicodeDecodeError:
        return entry.data
    if '\0' in text:
        return entry.data
    relative = {old[len('/usr/'):]: new[len('/usr/'):] for old, new in path_map.items()
                if old.startswith('/usr/')}
    return rewrite_tokens(text, names, relative).encode('utf-8')


def convert(entries: list[Entry], names: Names, tarball: bool = False) -> tuple[list[Entry], dict[str, str]]:
    path_map = {e.path: names.path(e.path) for e in entries}
    stable = set(path_map)
    # A tarball's files outside bin/ and share/ (install.sh) sit beside the tree, not in it.
    def installed(e: Entry) -> bool:
        return not tarball or e.path.startswith(('/usr/bin/', '/usr/share/'))
    shared = sorted(path_map[e.path] for e in entries
                    if e.kind != 'dir' and installed(e) and path_map[e.path] in stable)
    if shared:
        raise RebrandError('nightly paths would be shared with the stable package: ' + ', '.join(shared))
    clashes = [p for p, n in collections.Counter(path_map.values()).items() if n > 1]
    if clashes:
        raise RebrandError('several stable paths map to the same nightly path: ' + ', '.join(sorted(clashes)))
    def data(e: Entry) -> bytes:
        if e.kind == 'file' and not installed(e):
            return rewrite_tarball_extra(e, names, path_map)
        return rewrite_content(e, names, path_map)
    out = [replace(e, path=path_map[e.path], data=data(e), link=rewrite_link(e, path_map)) for e in entries]
    return out, path_map


# ---- reading and writing file trees ---------------------------------------------------------------

def entries_from_tar(tar: tarfile.TarFile, root: str, mount: str) -> list[Entry]:
    """Entries of tar members under root (a member path), installed below mount."""
    out = []
    for m in tar.getmembers():
        name = m.name[2:] if m.name.startswith('./') else m.name
        name = name.rstrip('/')
        if name == root.rstrip('/'):
            rel = ''
        elif name.startswith(root):
            rel = name[len(root):]
        else:
            raise RebrandError(f'unexpected member {m.name} outside {root}')
        path = posixpath.normpath(posixpath.join(mount, rel)) if rel else mount
        if path == '/':
            continue
        if m.isdir():
            out.append(Entry(path, 'dir', m.mode & 0o7777, mtime=int(m.mtime)))
        elif m.issym():
            out.append(Entry(path, 'symlink', 0o777, link=m.linkname, mtime=int(m.mtime)))
        elif m.isreg():
            out.append(Entry(path, 'file', m.mode & 0o7777, tar.extractfile(m).read(), mtime=int(m.mtime)))
        else:
            raise RebrandError(f'unsupported member type in {m.name}')
    return out


def write_tarball(out: Path, top: str, entries: list[Entry]) -> None:
    with tarfile.open(out, 'w:gz', format=tarfile.PAX_FORMAT) as tar:
        for e in sorted(entries, key=lambda e: e.path):
            info = tarfile.TarInfo(top + e.path[len('/usr'):])
            info.mode, info.mtime = e.mode, e.mtime
            info.uid = info.gid = 0
            info.uname = info.gname = 'root'
            if e.kind == 'dir':
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            elif e.kind == 'symlink':
                info.type, info.linkname = tarfile.SYMTYPE, e.link
                tar.addfile(info)
            else:
                info.size = len(e.data)
                tar.addfile(info, io.BytesIO(e.data))


def write_tree(root: Path, entries: list[Entry]) -> None:
    for e in entries:
        target = root / e.path.lstrip('/')
        target.parent.mkdir(parents=True, exist_ok=True)
        if e.kind == 'dir':
            target.mkdir(exist_ok=True)
        elif e.kind == 'symlink':
            os.symlink(e.link, target)
        else:
            target.write_bytes(e.data)
    # Parents created implicitly get the usual directory mode; listed entries get theirs, deepest
    # first so a read-only directory is set after its contents.
    for directory, _dirs, _files in os.walk(root):
        os.chmod(directory, 0o755)
    for e in sorted(entries, key=lambda e: e.path, reverse=True):
        target = root / e.path.lstrip('/')
        if e.kind != 'symlink':
            os.chmod(target, e.mode)
        os.utime(target, (e.mtime, e.mtime), follow_symlinks=False)


def run(cmd: list, **kwargs) -> subprocess.CompletedProcess:
    try:
        return subprocess.run([str(c) for c in cmd], check=True, capture_output=True, **kwargs)
    except FileNotFoundError as err:
        raise RebrandError(f'{cmd[0]} is not installed') from err
    except subprocess.CalledProcessError as err:
        raise RebrandError(f'{" ".join(map(str, cmd))} failed: {err.stderr.decode(errors="replace").strip()}') from err


# ---- tar.gz ---------------------------------------------------------------------------------------

def convert_tarball(src: Path, out: Path, prefix: str, stable_top: str, nightly_top: str) -> None:
    with tarfile.open(src, 'r:gz') as tar:
        entries = entries_from_tar(tar, stable_top + '/', '/usr')
    entries, _ = convert(entries, Names.from_entries(prefix, entries), tarball=True)
    write_tarball(out, nightly_top, entries)


# ---- deb ------------------------------------------------------------------------------------------

def parse_control(text: str) -> list[list[str]]:
    fields: list[list[str]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if line[0] in ' \t':
            fields[-1][1] += '\n' + line
        else:
            name, value = line.split(':', 1)
            fields.append([name, value.strip()])
    return fields


def drop_package(value: str, package: str) -> str:
    clauses = []
    for clause in ' '.join(value.split()).split(','):
        alternatives = [a.strip() for a in clause.split('|')
                        if a.strip() and re.split(r'[\s(:]', a.strip(), maxsplit=1)[0] != package]
        if alternatives:
            clauses.append(' | '.join(alternatives))
    return ', '.join(clauses)


def rebrand_control(text: str, nightly_name: str, version: str) -> str:
    fields = parse_control(text)
    stable = next(v for k, v in fields if k == 'Package')
    out = []
    for name, value in fields:
        if name == 'Package':
            value = nightly_name
        elif name == 'Version':
            value = version
        elif name == 'Description':
            first, _, rest = value.partition('\n')
            value = first + LABEL + ('\n' + rest if rest else '')
        elif name in DEB_RELATIONS:
            value = drop_package(value, stable)
            if not value:
                continue
        out.append(f'{name}: {value}\n')
    return ''.join(out)


def convert_deb(src: Path, out: Path, prefix: str, nightly_version: str, work: Path) -> None:
    package = run(['dpkg-deb', '--field', src, 'Package']).stdout.decode().strip()
    if package.endswith(SUFFIX):
        refuse_nightly_input(f'{src.name} is package {package}')
    data = run(['dpkg-deb', '--fsys-tarfile', src]).stdout
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        entries = entries_from_tar(tar, '', '/')
    control_dir = work / 'deb-control'
    run(['dpkg-deb', '--control', src, control_dir])
    names = Names.from_entries(prefix, entries)
    entries, path_map = convert(entries, names)

    stage = work / 'deb'
    stage.mkdir()
    write_tree(stage, entries)
    debian = stage / 'DEBIAN'
    debian.mkdir()
    os.chmod(debian, 0o755)
    for item in sorted(control_dir.iterdir()):
        text = item.read_text()
        if item.name == 'control':
            text = rebrand_control(text, prefix + SUFFIX, nightly_version + '-1')
        elif item.name == 'md5sums':
            continue
        elif item.name == 'conffiles':
            text = ''.join(path_map.get(line.strip(), line.strip()) + '\n' for line in text.splitlines() if line.strip())
        else:
            text = rewrite_tokens(text, names, path_map)
        (debian / item.name).write_text(text)
        os.chmod(debian / item.name, stat.S_IMODE(item.stat().st_mode))
    sums = ''.join(f'{hashlib.md5(e.data).hexdigest()}  {e.path.lstrip("/")}\n'
                   for e in sorted(entries, key=lambda e: e.path) if e.kind == 'file')
    if sums:
        (debian / 'md5sums').write_text(sums)
        os.chmod(debian / 'md5sums', 0o644)
    run(['dpkg-deb', '--root-owner-group', '-Zxz', '--build', stage, out])


# ---- rpm ------------------------------------------------------------------------------------------

def parse_cpio(data: bytes):
    """Yield (name, mode, mtime, body) from a newc cpio archive (an rpm payload)."""
    pos = 0
    while True:
        if data[pos:pos + 6] not in (b'070701', b'070702'):
            raise RebrandError('rpm payload is not a newc cpio archive')
        fields = [int(data[pos + 6 + 8 * i:pos + 14 + 8 * i], 16) for i in range(13)]
        mode, mtime, size, namesize = fields[1], fields[5], fields[6], fields[11]
        name = data[pos + 110:pos + 110 + namesize - 1].decode()
        pos = (pos + 110 + namesize + 3) & ~3
        body = data[pos:pos + size]
        pos = (pos + size + 3) & ~3
        if name == 'TRAILER!!!':
            return
        yield name, mode, mtime, body


def rpm_query(src: Path, *args: str) -> str:
    return run(['rpm', '-qp', '--nosignature', *args, src]).stdout.decode()


def rpm_tag(src: Path, tag: str) -> str:
    value = rpm_query(src, '--qf', f'%{{{tag}}}')
    return '' if value == '(none)' else value


def rpm_relations(src: Path, flag: str, stable: str) -> list[str]:
    out = []
    for line in rpm_query(src, flag).splitlines():
        line = line.strip()
        name = re.split(r'[\s(]', line, maxsplit=1)[0]
        if (not line or name == stable or line.startswith(('rpmlib(', f'{stable}(', f'config({stable})'))):
            continue
        out.append(line)
    return out


def rpm_payload(src: Path, compressor: str) -> bytes:
    """The rpm's uncompressed cpio payload.

    Read directly rather than with rpm2cpio: Ubuntu 24.04's rpm2cpio (rpm 4.18) exits 1 on
    large nfpm-built rpms although it writes the whole archive."""
    data = src.read_bytes()
    pos = 96  # the lead
    for padded in (True, False):  # signature header (padded to 8 bytes), then the main header
        if data[pos:pos + 3] != b'\x8e\xad\xe8':
            raise RebrandError(f'{src} has no rpm header at offset {pos}')
        index, size = struct.unpack('>II', data[pos + 8:pos + 16])
        pos += 16 + 16 * index + size
        if padded:
            pos = (pos + 7) & ~7
    payload = data[pos:]
    if compressor == 'gzip':
        return gzip.decompress(payload)
    if compressor in ('xz', 'lzma'):
        return lzma.decompress(payload)
    if compressor == 'bzip2':
        return bz2.decompress(payload)
    if compressor == 'zstd':
        return run(['zstd', '-dc'], input=payload).stdout
    raise RebrandError(f'unsupported rpm payload compressor {compressor!r} in {src}')


def entries_from_cpio(data: bytes, config_paths: set[str]) -> list[Entry]:
    entries = []
    for name, mode, mtime, body in parse_cpio(data):
        path = posixpath.normpath(posixpath.join('/', name[2:] if name.startswith('./') else name))
        if path == '/':
            continue
        if stat.S_ISDIR(mode):
            entries.append(Entry(path, 'dir', stat.S_IMODE(mode), mtime=mtime))
        elif stat.S_ISLNK(mode):
            entries.append(Entry(path, 'symlink', 0o777, link=body.decode(), mtime=mtime))
        elif stat.S_ISREG(mode):
            entries.append(Entry(path, 'file', stat.S_IMODE(mode), body, mtime=mtime,
                                 config=path in config_paths))
        else:
            raise RebrandError(f'unsupported file type in the rpm payload: {name}')
    return entries


def convert_rpm(src: Path, out: Path, prefix: str, nightly_version: str, work: Path) -> None:
    config_paths = set()
    for line in rpm_query(src, '--qf', '[%{FILENAMES}\t%{FILEFLAGS}\n]').splitlines():
        path, _, flag = line.rpartition('\t')
        if int(flag or 0) & 1:  # RPMFILE_CONFIG
            config_paths.add(path)
    stable = rpm_tag(src, 'NAME')
    if stable.endswith(SUFFIX):
        refuse_nightly_input(f'{src.name} is package {stable}')
    entries = entries_from_cpio(rpm_payload(src, rpm_tag(src, 'PAYLOADCOMPRESSOR') or 'gzip'), config_paths)
    names = Names.from_entries(prefix, entries)
    entries, path_map = convert(entries, names)

    summary = rpm_tag(src, 'SUMMARY')
    description = rpm_tag(src, 'DESCRIPTION')
    first, _, rest = description.partition('\n')
    if first == summary:
        description = first + LABEL + ('\n' + rest if rest else '')
    stage = work / 'rpm-root'
    stage.mkdir()
    write_tree(stage, entries)
    contents = []
    for e in sorted(entries, key=lambda e: e.path):
        if e.kind == 'dir':
            contents.append({'dst': e.path, 'type': 'dir', 'file_info': {'mode': e.mode}})
        elif e.kind == 'symlink':
            contents.append({'src': e.link, 'dst': e.path, 'type': 'symlink'})
        else:
            item = {'src': str(stage / e.path.lstrip('/')), 'dst': e.path, 'file_info': {'mode': e.mode}}
            if e.config:
                item['type'] = 'config'
            contents.append(item)
    scripts, trans = {}, {}
    for tags, target in ((RPM_SCRIPTS, scripts), (RPM_TRANS_SCRIPTS, trans)):
        for tag, key in tags.items():
            body = rpm_tag(src, tag)
            if body:
                script = work / f'rpm-{key}.sh'
                script.write_text(rewrite_tokens(body, names, path_map).rstrip('\n') + '\n')
                target[key] = str(script)
    config = {
        'name': prefix + SUFFIX,
        'arch': rpm_tag(src, 'ARCH'),
        'platform': 'linux',
        'version': nightly_version,
        'version_schema': 'none',
        'release': '1',
        'maintainer': rpm_tag(src, 'PACKAGER'),
        'vendor': rpm_tag(src, 'VENDOR'),
        'homepage': rpm_tag(src, 'URL'),
        'license': rpm_tag(src, 'LICENSE'),
        'description': description,
        'depends': rpm_relations(src, '--requires', stable),
        'recommends': rpm_relations(src, '--recommends', stable),
        'suggests': rpm_relations(src, '--suggests', stable),
        'conflicts': rpm_relations(src, '--conflicts', stable),
        'replaces': rpm_relations(src, '--obsoletes', stable),
        'provides': rpm_relations(src, '--provides', stable),
        'contents': contents,
        'scripts': scripts,
        'rpm': {'summary': summary + LABEL, 'group': rpm_tag(src, 'GROUP'), 'scripts': trans},
    }
    if config['rpm']['group'] == 'Unspecified':
        config['rpm']['group'] = ''
    # nfpm reads YAML, and JSON is YAML.
    config_file = work / 'nfpm.yaml'
    config_file.write_text(json.dumps(prune(config), indent=2))
    run(['nfpm', 'package', '--config', config_file, '--packager', 'rpm', '--target', out])


def prune(value):
    if isinstance(value, dict):
        pruned = {k: prune(v) for k, v in value.items()}
        return {k: v for k, v in pruned.items() if v not in ('', [], {}, None)}
    if isinstance(value, list):
        return [prune(v) for v in value]
    return value


# ---- inputs and main ------------------------------------------------------------------------------

def find_inputs(input_dir: Path, version: str) -> tuple[str, str, dict[str, Path]]:
    """Return (prefix, arch, {extension: path}) of the app's stable artifacts.

    Standalone CLI or server tarballs (<prefix>-cli-<version>-...) sit beside the app's; the app's
    prefix is the one every other prefix extends with '-'."""
    pattern = re.compile(r'(?P<prefix>.+)-' + re.escape(version)
                         + r'-linux-(?P<arch>x86_64|aarch64|riscv64)\.(?P<ext>tar\.gz|deb|rpm)')
    found: dict[str, list] = collections.defaultdict(list)
    for item in sorted(input_dir.iterdir()):
        m = pattern.fullmatch(item.name)
        if m and item.is_file():
            found[m.group('prefix')].append((m.group('arch'), m.group('ext'), item))
    if not found:
        raise RebrandError(f'no <prefix>-{version}-linux-<arch>.{{tar.gz,deb,rpm}} in {input_dir}')
    prefixes = sorted(found, key=lambda p: (len(p), p))
    prefix = prefixes[0]
    others = [p for p in prefixes[1:] if not p.startswith(prefix + '-')]
    if others:
        raise RebrandError(f'cannot tell the app prefix from {", ".join(prefixes)}')
    arches = sorted({arch for arch, _ext, _path in found[prefix]})
    if len(arches) != 1:
        raise RebrandError(f'expected one architecture, found {", ".join(arches)}')
    return prefix, arches[0], {ext: path for _arch, ext, path in found[prefix]}


def rebrand(input_dir: Path, output_dir: Path, version: str, nightly_version: str) -> dict:
    if not re.fullmatch(r'[0-9]+(\.[0-9]+)*', nightly_version):
        raise RebrandError(f'nightly version {nightly_version!r} must be digits and dots')
    prefix, arch, inputs = find_inputs(input_dir, version)
    if prefix.endswith(SUFFIX):
        refuse_nightly_input(f'the input files are named {prefix}-{version}-linux-{arch}.*')
    output_dir.mkdir(parents=True, exist_ok=True)
    stable_base = f'{prefix}-{version}-linux-{arch}'
    nightly_base = f'{prefix}{SUFFIX}-{nightly_version}-linux-{arch}'
    written = []
    with tempfile.TemporaryDirectory(prefix='nightly-rebrand-') as tmp:
        for ext in EXTENSIONS:
            if ext not in inputs:
                continue
            out = output_dir / f'{nightly_base}.{ext}'
            work = Path(tmp) / ext.replace('.', '-')
            work.mkdir()
            if ext == 'tar.gz':
                convert_tarball(inputs[ext], out, prefix, stable_base, nightly_base)
            elif ext == 'deb':
                convert_deb(inputs[ext], out, prefix, nightly_version, work)
            else:
                convert_rpm(inputs[ext], out, prefix, nightly_version, work)
            shutil.rmtree(work)
            written.append(out)
    tarball = output_dir / f'{nightly_base}.tar.gz'
    return {'prefix': prefix, 'tarball': str(tarball) if tarball in written else '',
            'files': [str(p) for p in written]}


def write_outputs(result: dict) -> None:
    path = os.environ.get('GITHUB_OUTPUT')
    if not path:
        return
    with open(path, 'a') as out:
        out.write(f'prefix={result["prefix"]}\ntarball={result["tarball"]}\n')
        out.write('files<<NIGHTLY_REBRAND_EOF\n' + ''.join(f + '\n' for f in result['files'])
                  + 'NIGHTLY_REBRAND_EOF\n')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--input-dir', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, default=Path('dist/nightly'))
    parser.add_argument('--version', required=True)
    parser.add_argument('--nightly-version', required=True)
    args = parser.parse_args(argv)
    try:
        result = rebrand(args.input_dir, args.output_dir, args.version, args.nightly_version)
    except RebrandError as err:
        print(f'::error::nightly-rebrand: {err}', file=sys.stderr)
        return 1
    for item in result['files']:
        print(f'wrote {item}')
    write_outputs(result)
    return 0


if __name__ == '__main__':
    sys.exit(main())

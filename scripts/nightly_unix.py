#!/usr/bin/env python3
"""Private Unix producer checks. Pending capability is never feed admission.

No compiler, network, signing, profile access, or publication. Photo owns the fixed
nightly.sh build commands. Mac inventory is not a signature/runtime qualification.
"""
import argparse
import datetime as dt
import gzip
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import shlex
import shutil
import stat
import struct
import tarfile
import tempfile
import tomllib
import zipfile
import nightly_app as apps
import nightly_feed as n
import nightly_policy as p

MAX_MEMBERS = 10000
MAX_METADATA = 256 * 1024
MAX_CENTRAL_DIRECTORY = 8 * 1024 * 1024
CHUNK = 1024 * 1024
DEFAULT_APP = apps.DEFAULT_APP  # only when NIGHTLY_APP is unset; see nightly_app.current_name()
# A Linux tar may start its GUI through a short launcher that adds the nightly profile.
MAX_LAUNCHER = 4096
FORMATS = {"linux-x86_64": ("tar_gz", "x86_64-unknown-linux-gnu"), "macos-aarch64": ("portable_zip", "aarch64-apple-darwin")}

def isolation(app=None):
    app = app or apps.current_name()
    return {"package_identity":apps.App(app).identity, "profile_contract":"nightly", "profile_version":1, "associations":"none"}

def contracts(app=None):
    """The private Unix producer contract for every Unix target of the app's registry row."""
    app = app or apps.current_name()
    names = apps.App(app)
    result = {}
    for platform, (fmt, rust_target) in FORMATS.items():
        if platform not in names.platforms: continue
        os_name, arch = apps.PLATFORMS[platform]
        result[platform] = {"target":{"os":os_name,"arch":arch,"format":fmt}, "rust_target":rust_target,
            "entrypoint":names.entrypoint(platform), "asset_pattern":names.asset_pattern(platform)}
    return result

# PhotoCraft's contract, as nightly-unix-pending.json records it (kept for the main-route workflows).
ISOLATION = isolation("photocraft")
CONTRACTS = contracts("photocraft")

# Admitted rows ship with Windows (one release, every admitted platform); admission is not runtime qualification.
ADMISSION = {
    "linux-x86_64": {"status":"feed-admitted", "feed_admitted":True, "runtime_qualified":False},
    "macos-aarch64": {"status":"feed-admitted", "feed_admitted":True, "runtime_qualified":False},
}

def pending_contract(pending, platform, app=None):
    """The app's Unix producer row, from its registry row; a pending file, if given, must match it."""
    app = app or apps.current_name()
    owned = contracts(app)
    n.require(platform in owned and platform in ADMISSION, "unsupported private Unix target")
    expected = {**owned[platform], "isolation":isolation(app), **ADMISSION[platform]}
    if pending is None:
        return expected
    n.fields(pending, {"schema","app","capabilities"}, "pending producer registry")
    n.require(pending["schema"] == "craft-nightly-pending-producers/v1" and pending["app"] == app, "unreviewed pending registry")
    n.require(isinstance(pending["capabilities"],dict) and set(pending["capabilities"]) == set(owned), "unreviewed pending target set")
    row = pending["capabilities"][platform]
    n.require(row == expected and type(row["isolation"]["profile_version"]) is int and row["feed_admitted"] is ADMISSION[platform]["feed_admitted"] and row["runtime_qualified"] is False, "pending row differs from owner contract or attempts admission")
    return row

def registry_target(row):
    return {"asset_pattern":row["asset_pattern"],"isolation":row["isolation"],"target":row["target"]}

def require_registry_admission(registry, platform, row, app=None):
    """Admitted rows must appear exactly once in the active registry; pending rows never."""
    app = app or apps.current_name()
    targets = n.app_record(registry,app)["targets"]
    matches = [item for item in targets if isinstance(item,dict) and item.get("target") == row["target"]]
    if row["feed_admitted"]:
        n.require(matches == [registry_target(row)], "admitted Unix target missing from or differing in the feed registry")
    else:
        n.require(not matches, "pending Unix target must not appear in the feed registry")

def check_prepared(registry, pending, platform, prepared, cargo, version, environment, target_base, dist, policy, app=None):
    app = app or apps.current_name()
    row = pending_contract(pending,platform,app)
    n.trusted_context(registry,app,environment)
    require_registry_admission(registry,platform,row,app)
    n.fields(prepared,{"schema","channel","app","repository","tag","source_commit","published_at","version","ready"},"prepared identity")
    manifest=n.bounded_bytes(cargo,MAX_METADATA,'manifest')
    tomllib.loads(manifest.decode('utf-8')) # source manifest is bounded/valid; not the identity base
    fresh=p.validate_prepared(registry,prepared,policy,environment)
    n.require(prepared == fresh and version == fresh["version"] and prepared["ready"] is True, "source/run/version identity mismatch")
    target_base, dist = Path(target_base), Path(dist)
    n.require(target_base.is_absolute() and dist.is_absolute(), "target base and dist must be absolute")
    target_base, dist = n.owned_path(target_base), n.owned_path(dist)
    n.require(not target_base.is_symlink() and not dist.is_symlink(), "output paths must not be symlinks")
    target_base, dist = target_base.resolve(), dist.resolve()
    n.require(target_base != dist and target_base not in dist.parents and dist not in target_base.parents, "dist and compiler target base must be disjoint")
    n.require(target_base != Path('/') and dist != Path('/'), "unsafe output root")
    return {"version":version,"asset":row["asset_pattern"].replace("{version}",version),
        "source_commit":prepared["source_commit"],
        "build_date":dt.datetime.fromtimestamp(prepared["published_at"],dt.timezone.utc).strftime("%Y-%m-%d"),
        "rust_target":row["rust_target"],"entrypoint":row["entrypoint"],
        "target_leaf":str(target_base/"nightly-v1"/platform),"dist":str(dist),
        "private_only":not row["feed_admitted"],"feed_admitted":row["feed_admitted"],"runtime_qualified":False}

def require_build_policy(platform):
    # macOS compiles and smokes its ad-hoc (linker) signed GUI in the secret-free
    # producer job only; Developer ID/notarization is the separate release signer's job.
    n.require(platform in CONTRACTS,"unsupported private Unix target")

def canonical_version(version):
    n.require(isinstance(version,str) and len(version)<=128 and re.fullmatch(r'(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)-nightly\.([0-9]{8})\.[1-9][0-9]*\.[1-9][0-9]*\.g[0-9a-f]{12}',version), "noncanonical immutable nightly version")
    dt.datetime.strptime(version.split('-nightly.',1)[1].split('.',1)[0],'%Y%m%d')

def verify_version_output(text,version,app=None):
    app = app or apps.current_name()
    canonical_version(version)
    n.require(isinstance(text,str) and text.replace("\r\n","\n").replace("\r","\n") == apps.App(app).version_line(version), "built flavor or exact version output mismatch")

def member_name(value, app=None):
    names = app if isinstance(app, apps.App) else apps.App(app or apps.current_name())
    n.require(isinstance(value,str) and 0 < len(value.encode('utf-8')) <= 1024 and '\x00' not in value and '\\' not in value, "unsafe archive name")
    while value.startswith('./'): value=value[2:]
    if value in ('','.'): return '.'
    path = PurePosixPath(value)
    n.require(len(path.parts)<=32, 'archive path depth exceeds bound')
    n.require(not path.is_absolute() and all(part not in ('','.','..') for part in value.rstrip('/').split('/')), "archive path escapes root")
    banned = {"portable.txt","apprun","install","uninstall"} | names.banned
    n.require(not any(part.casefold() in banned or part.casefold().endswith(('.desktop','.sh','.bat','.cmd','.ps1','.dmg','.appimage')) for part in path.parts), "CLI, portable marker, handler or install hook is forbidden")
    return path.as_posix().rstrip('/')

class BoundedTarReader:
    # A random-access gzip stream lets us reject oversized metadata reads BEFORE
    # tarfile allocates a PAX/long-name buffer. Payload consumers read CHUNK at a time.
    def __init__(self,stream):
        self.stream = stream; self.metadata = True; self.metadata_bytes = 0
    def read(self,size=-1):
        n.require(0 <= size <= (MAX_METADATA if self.metadata else CHUNK),"oversized archive metadata read")
        if self.metadata:
            self.metadata_bytes+=size
            n.require(self.metadata_bytes<=MAX_CENTRAL_DIRECTORY,"cumulative TAR metadata exceeds bound")
        n.require(self.tell()+size <= n.MAX_TOTAL_BYTES + MAX_CENTRAL_DIRECTORY,"expanded archive exceeds bound")
        return self.stream.read(size)
    def seek(self,offset,whence=0):
        n.require(whence==0 and 0 <= offset <= n.MAX_TOTAL_BYTES + MAX_CENTRAL_DIRECTORY,"archive seek exceeds bound")
        return self.stream.seek(offset)
    def tell(self): return self.stream.tell()

def zip_metadata_bound(stream,size):
    # Bound central-directory allocation before constructing ZipFile. ZIP64 and
    # split archives are intentionally not supported by this initial private slice.
    stream.seek(max(0,size-65557)); tail=stream.read(65557)
    offset=tail.rfind(b'PK\x05\x06')
    n.require(offset>=0 and len(tail)-offset>=22,"missing ZIP end record")
    n.require(offset<20 or tail[offset-20:offset-16] != b'PK\x06\x07', "ZIP64 locator is unsupported")
    _,disk,start_disk,count_disk,count,total,central_offset,comment=struct.unpack('<4s4H2IH',tail[offset:offset+22])
    n.require(disk==start_disk==0 and count_disk==count and count<=MAX_MEMBERS and total<=MAX_CENTRAL_DIRECTORY and central_offset+total<=size and offset+22+comment==len(tail),"ZIP central directory exceeds bounds or unsupported ZIP64/split layout")
    stream.seek(0)

LAUNCHER_SHEBANGS = ("#!/bin/sh", "#!/usr/bin/env sh", "#!/bin/bash", "#!/usr/bin/env bash")
# A launcher is a fixed shape, not arbitrary shell. Each line must be one of:
#   blank or a comment;  set -e / set -eu / set -euo pipefail;
#   NAME="value" with only ${...}/$NAME expansions (no command substitution), or the one
#     directory idiom  here="$(cd "$(dirname "$0")" && pwd)";
#   case "$1" in / case "${1:-}" in;  esac;  a `--opt|-o)` case item;  ;;
#   exec "$<dir var>/<binary>" --launch-profile nightly-v1 --profile-root "<value>" [--version | -- "$@"]
#     (also as a case item, ending in ;;, and in a one-line case ... esac).
LAUNCHER_DIR = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)="\$\(cd "\$\(dirname "\$0"\)" && pwd\)"')
LAUNCHER_VALUE = r'"(?:[^"`$\\]|\$\{[A-Za-z_][A-Za-z0-9_]*(?::-(?:[^}"`$\\]|\$[A-Za-z_][A-Za-z0-9_]*)*)?\}|\$[A-Za-z_][A-Za-z0-9_]*)*"'
LAUNCHER_ASSIGN = re.compile(r'[A-Za-z_][A-Za-z0-9_]*=' + LAUNCHER_VALUE)
LAUNCHER_SET = re.compile(r'set -(?:e|eu|euo pipefail)')
LAUNCHER_CASE = re.compile(r'case "\$(?:1|\{1:-\})" in')
LAUNCHER_ITEM = re.compile(r'(--?[A-Za-z][\w-]*(?:\|--?[A-Za-z][\w-]*)*)\)\s*(.*)')
LAUNCHER_EXEC = re.compile(r'exec "\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?/([^"$`\\]+)" --launch-profile nightly-v1 '
                           r'--profile-root ' + LAUNCHER_VALUE + r'(?: --version| -- "\$@")?')


def launcher_exec(statement, rel, dirs):
    """exec "$<dir>/<binary>" --launch-profile nightly-v1 --profile-root "<value>" [--version | -- "$@"]."""
    match = LAUNCHER_EXEC.fullmatch(statement)
    n.require(match is not None, "nightly launcher exec must start only the GUI binary with the nightly launch profile")
    n.require(match.group(1) in dirs, "nightly launcher may exec the GUI binary only through its own directory")
    n.require(match.group(2) == rel, "nightly launcher may exec only the GUI binary beside it")


def verify_launcher(text, entrypoint, binary):
    """A short text launcher whose only command is exec'ing the GUI binary with the nightly profile."""
    n.require(len(text) <= MAX_LAUNCHER and b"\0" not in text, "nightly launcher must be a short shebang script")
    try:
        script = text.decode("utf-8")
    except UnicodeDecodeError as error:
        raise n.InvalidNightly("nightly launcher is not UTF-8 text") from error
    lines = script.split("\n")
    n.require(lines[0] in LAUNCHER_SHEBANGS, "nightly launcher must be a short shebang script")
    parent = PurePosixPath(entrypoint).parent
    try:
        rel = PurePosixPath(binary).relative_to(parent).as_posix() if parent != PurePosixPath(".") else binary
    except ValueError as error:
        raise n.InvalidNightly("the GUI binary must sit in or below the launcher's directory") from error
    dirs, execs, depth = set(), 0, 0
    for raw in lines[1:]:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if match := LAUNCHER_DIR.fullmatch(line):
            dirs.add(match.group(1)); continue
        if LAUNCHER_SET.fullmatch(line) or LAUNCHER_ASSIGN.fullmatch(line):
            continue
        if LAUNCHER_CASE.fullmatch(line):
            n.require(depth == 0, "nested case in nightly launcher"); depth = 1; continue
        if line == "esac":
            n.require(depth == 1, "unbalanced case in nightly launcher"); depth = 0; continue
        if line == ";;":
            n.require(depth == 1, "case item outside case in nightly launcher"); continue
        one_line = line.startswith("case ") and line.endswith(" esac") and " in " in line
        if one_line:  # case "$1" in --version|-V) exec ... ;; esac
            head, rest = line.split(" in ", 1)
            n.require(depth == 0 and LAUNCHER_CASE.fullmatch(head + " in") is not None, "nightly launcher may only branch on $1")
            line = rest[:-len(" esac")].strip()
        if match := LAUNCHER_ITEM.fullmatch(line):
            n.require(depth == 1 or one_line, "case item outside case in nightly launcher")
            body = match.group(2).strip()
            if body.endswith(";;"):
                body = body[:-2].strip()
            if body:
                launcher_exec(body, rel, dirs); execs += 1
            continue
        n.require(not one_line, "nightly launcher may only branch on $1")
        if line.startswith("exec "):
            launcher_exec(line, rel, dirs); execs += 1; continue
        raise n.InvalidNightly("nightly launcher may only set its directory and profile root and exec the GUI binary")
    n.require(execs > 0 and depth == 0, "nightly launcher must exec the GUI binary")
    return True


def verify_archive(pending,platform,archive,version,output=None,app=None):
    app = app or apps.current_name()
    row = pending_contract(pending,platform,app)
    names_ = apps.App(app)
    bundle = names_.bundle; plist_name = bundle+'/Contents/Info.plist'
    gui = names_.binary(platform)
    canonical_version(version)
    archive = n.owned_path(archive)
    n.require(archive.name == row['asset_pattern'].replace('{version}',version),"archive name differs from exact identity")
    out = Path(output) if output is not None else None
    if out is not None:
        n.require(out.is_absolute(), "unpack output must be a new absolute directory")
        out = n.owned_path(out)
    # Refuse unavailable extraction before opening any archive or creating output.
    if out is not None: require_build_policy(platform)
    raw, info = n.regular_file(archive.parent,archive.name)
    names=set(); files=set(); required_dirs=set(); total=0; entry_header=None; plist=None; executable=False
    binary_header=None; binary_executable=False; launcher=None
    staging=None
    try:
        if out is not None:
            n.require(out.is_absolute() and not out.exists() and not out.is_symlink(),"unpack output must be a new absolute directory")
            out.parent.mkdir(parents=True,exist_ok=True)
            staging=Path(tempfile.mkdtemp(prefix=".nightly-unpack-",dir=out.parent))
        def consume(name,size,is_dir,mode,read):
            nonlocal total,entry_header,plist,executable,binary_header,binary_executable,launcher
            n.require(not name.endswith('/') or is_dir,"regular file has directory spelling")
            name=member_name(name,names_)
            n.require(name!='.' or (is_dir and size==0 and platform=='linux-x86_64'),"invalid root directory entry")
            n.require(name.casefold() not in names and len(names)<MAX_MEMBERS,"duplicate/oversized archive inventory")
            key=name.casefold()
            parts=key.split('/')
            ancestors={'/'.join(parts[:index]) for index in range(1,len(parts))}
            n.require(not ancestors.intersection(files) and (is_dir or key not in required_dirs),"archive file/directory hierarchy conflicts")
            required_dirs.update(ancestors)
            if not is_dir: files.add(key)
            names.add(key)
            if platform=='macos-aarch64':
                n.require(name==bundle or name.startswith(bundle+'/'),"Mac ZIP must contain exactly one nightly app bundle")
            n.require(type(size) is int and 0<=size<=n.MAX_ASSET_BYTES,"member exceeds size bound")
            total+=size
            n.require(total<=n.MAX_TOTAL_BYTES,"expanded payload exceeds bound")
            destination=staging/name if staging is not None else None
            if is_dir:
                n.require(size==0,"directory contains payload")
                if destination is not None: destination.mkdir(parents=True,exist_ok=True)
                return
            if destination is not None:
                destination.parent.mkdir(parents=True,exist_ok=True)
                target=destination.open('xb')
            else: target=None
            captured=bytearray(); consumed=0
            try:
                while block:=read(CHUNK):
                    consumed+=len(block)
                    n.require(consumed<=size,"member grew beyond declared size")
                    if target is not None: target.write(block)
                    cap=MAX_METADATA if name==plist_name else (MAX_LAUNCHER+1 if name==row['entrypoint'] and name!=gui else 64)
                    captured.extend(block[:max(0,cap-len(captured))])
                n.require(consumed==size,"truncated member")
            finally:
                if target is not None: target.close()
            if name==row['entrypoint']:
                entry_header=bytes(captured); executable=bool(mode & 0o111)
                if destination is not None: destination.chmod(0o700)
                if name!=gui: launcher=(bytes(captured),size)
            if name==gui:
                binary_header=bytes(captured[:64]); binary_executable=bool(mode & 0o111)
                if destination is not None: destination.chmod(0o700)
            if name==plist_name:
                n.require(size<=MAX_METADATA,"plist exceeds metadata bound")
                plist=plistlib.loads(captured)
        if platform=='linux-x86_64':
            with raw, gzip.GzipFile(fileobj=raw) as expanded:
                reader=BoundedTarReader(expanded)
                with tarfile.open(fileobj=reader,mode='r:') as container:
                    for member in container:
                        n.require(member.isfile() or member.isdir(),"links and special TAR members are forbidden")
                        reader.metadata=False
                        stream=container.extractfile(member) if member.isfile() else None
                        try: consume(member.name,member.size,member.isdir(),member.mode,stream.read if stream else lambda size:b'')
                        finally:
                            reader.metadata=True
                            if stream is not None: stream.close()
                # TAR stops at its end marker; gzip integrity is checked only at EOF.
                limit=n.MAX_TOTAL_BYTES + MAX_CENTRAL_DIRECTORY
                while True:
                    block=expanded.read(min(CHUNK,limit-expanded.tell()+1))
                    n.require(expanded.tell()<=limit,"expanded archive exceeds bound")
                    if not block: break
        else:
            with raw:
                zip_metadata_bound(raw,info.st_size)
                with zipfile.ZipFile(raw) as container:
                    n.require(len(container.infolist())<=MAX_MEMBERS,"oversized ZIP inventory")
                    for member in container.infolist():
                        mode=member.external_attr>>16
                        n.require(not (member.flag_bits & 1) and stat.S_IFMT(mode) in (0,stat.S_IFREG,stat.S_IFDIR),"encrypted/link/special ZIP member")
                        with container.open(member) as stream: consume(member.filename,member.file_size,member.is_dir(),mode,stream.read)
        n.require(entry_header is not None and executable,"missing/non-executable GUI entrypoint")
        n.require(binary_header is not None and binary_executable,"missing/non-executable GUI binary")
        if platform=='linux-x86_64':
            # The GUI binary named in the registry row is always an x86_64 ELF; the entrypoint is that
            # binary or a short launcher that execs it with the nightly profile.
            n.require(len(binary_header)>=64 and binary_header[:7]==b'\x7fELF\x02\x01\x01' and int.from_bytes(binary_header[16:18],'little') in (2,3) and int.from_bytes(binary_header[18:20],'little')==62,"not an x86_64 ELF payload")
            if launcher is not None:
                n.require(launcher[1]<=MAX_LAUNCHER,"nightly launcher must be a short shebang script")
                verify_launcher(launcher[0],row['entrypoint'],gui)
        else:
            n.require(launcher is None,"the Mac entrypoint must be the bundle executable")
            n.require(len(entry_header)>=32 and entry_header[:4]==b'\xcf\xfa\xed\xfe' and int.from_bytes(entry_header[4:8],'little')==0x0100000c and int.from_bytes(entry_header[12:16],'little')==2,"not a thin aarch64 Mach-O payload")
            n.require(isinstance(plist,dict) and plist.get('CFBundleExecutable')==PurePosixPath(gui).name and gui==bundle+'/Contents/MacOS/'+PurePosixPath(gui).name and plist.get('CFBundleIdentifier')==names_.identity,"nightly Mac bundle identity mismatch")
            n.require(plist.get(names_.plist_version_key)==version,f"missing/mismatched full {names_.plist_version_key}; numeric fallback forbidden")
            n.require(isinstance(plist.get('CFBundleVersion'),str) and re.fullmatch(r'[0-9]{1,4}(?:\.[0-9]{1,2}){0,2}',plist['CFBundleVersion']),"invalid Apple numeric bundle version")
            n.require(isinstance(plist.get('CFBundleShortVersionString'),str) and re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+',plist['CFBundleShortVersionString']),"invalid numeric short version")
            n.require(plist['CFBundleVersion']==version.split('-nightly.',1)[0] and plist['CFBundleShortVersionString']==version.split('-nightly.',1)[0],
                      'Apple numeric versions must use the prepared stable base, not Cargo/fallback')
            n.require(not any(plist.get(key) for key in ('CFBundleDocumentTypes','CFBundleURLTypes','UTExportedTypeDeclarations','UTImportedTypeDeclarations')),"Mac document/URL associations forbidden")
        after=archive.stat(follow_symlinks=False)
        n.require((info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns)==(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns),"archive changed during inspection")
        if staging is not None:
            n.require(not out.exists() and not out.is_symlink(),"unpack output must remain a new directory")
            staging.rename(n.owned_path(out))
            staging=None
        result={'members':len(names),'expanded_bytes':total,'entrypoint':row['entrypoint'],'inventory_only':True,'feed_admitted':False,'runtime_qualified':False,'signature_verified':False}
        if gui!=row['entrypoint']: result['binary']=gui  # PhotoCraft's result keeps its exact old shape.
        return result
    finally:
        raw.close()
        if staging is not None: shutil.rmtree(staging)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=('check','inventory','unpack-private','verify-version'))
    # The producer contract comes from the app's registry row; --pending (PhotoCraft's old file) must match it.
    parser.add_argument('--app',default=apps.current_name()); parser.add_argument('--pending'); parser.add_argument('--platform',required=True,choices=tuple(FORMATS))
    parser.add_argument('--registry'); parser.add_argument('--identity'); parser.add_argument('--policy'); parser.add_argument('--cargo',default='Cargo.toml')
    parser.add_argument('--version',required=True); parser.add_argument('--target-base'); parser.add_argument('--dist')
    parser.add_argument('--archive'); parser.add_argument('--output'); parser.add_argument('--version-output')
    args=parser.parse_args()
    try:
        pending=n.bounded_json(args.pending) if args.pending else None
        if args.mode=='check':
            n.require(args.registry and args.identity and args.policy and args.target_base and args.dist,"check requires exact prepared context and output paths")
            result=check_prepared(n.bounded_json(args.registry),pending,args.platform,n.bounded_json(args.identity),args.cargo,args.version,os.environ,args.target_base,args.dist,n.bounded_json(args.policy),args.app)
            require_build_policy(args.platform)
            if destination:=os.environ.get('GITHUB_OUTPUT'):
                with open(destination,'a',encoding='utf-8') as stream:
                    for key in ('version','asset','source_commit','build_date'):
                        value=result[key]; n.require('\n' not in value and '\r' not in value,'invalid action output')
                        stream.write(f'{key}={value}\n')
        elif args.mode=='verify-version':
            pending_contract(pending,args.platform,args.app)
            n.require(args.version_output,'bounded version output required')
            verify_version_output(n.bounded_bytes(args.version_output,MAX_METADATA,'version output').decode('utf-8'),args.version,args.app)
            result={'exact_version_and_flavor':True,'runtime_qualified':False,'feed_admitted':False}
        else:
            n.require(args.archive and (args.mode!='unpack-private' or args.output),'archive/output required')
            n.require(args.mode!='inventory' or args.output is None,'inventory never extracts')
            result=verify_archive(pending,args.platform,args.archive,args.version,args.output,args.app)
        print(json.dumps(result,sort_keys=True))
    except (n.InvalidNightly,OSError,EOFError,ValueError,KeyError,TypeError,RecursionError,tarfile.TarError,zipfile.BadZipFile,plistlib.InvalidFileException) as error:
        parser.exit(78 if isinstance(error,n.Unavailable) else 1,f'private Unix nightly unavailable: {error}\n')

if __name__=='__main__': main()

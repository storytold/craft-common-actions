#!/usr/bin/env python3
"""Fail when a Rust source file no module reaches, because such a file never compiles.

Roots are every workspace crate's lib, bin, test, bench and example targets (Cargo.toml paths and the
default locations) and build.rs. The walk follows `mod x;` (also inside inline `mod x { }` blocks),
`#[path = "..."]` (also inside `cfg_attr`) and literal `include!("...")`.

Modes: diff (default) reports only files the change adds or modifies against --base; strict reports
every unreached file; off does nothing. --allow is newline-separated glob text (`*` within a segment, `**`
across segments, `#` comments) for files that are unreached on purpose.

Usage: unreached_rs.py [--directory DIR] [--mode diff|strict|off] [--base REV] [--allow GLOBS]
"""
import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib

HINT = ("no module reaches this file, so it never compiles. Add a `mod` line (or `#[path]`) for it, "
        "or delete it. List it in unreached-rs-allow only if it is unreached on purpose.")
MOD_RS = {"mod.rs", "lib.rs", "main.rs"}


def tokens(src):
    """Identifiers, punctuation and string values; comments and char literals are dropped."""
    i, n, out = 0, len(src), []
    while i < n:
        c = src[i]
        if c.isspace():
            i += 1
        elif src.startswith("//", i):
            i = src.find("\n", i)
            i = n if i < 0 else i
        elif src.startswith("/*", i):
            depth, i = 1, i + 2
            while i < n and depth:
                if src.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif src.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
        elif c == '"' or (c in "rb" and re.match(r'(?:b?r#*"|b")', src[i:i + 12])):
            m = re.match(r'b?(r?)(#*)"', src[i:i + 12])
            i += m.end()
            if m.group(1):
                end = src.find('"' + m.group(2), i)
                end = n if end < 0 else end
                out.append(("s", src[i:end]))
                i = end + 1 + len(m.group(2))
            else:
                buf = []
                while i < n and src[i] != '"':
                    if src[i] == "\\" and i + 1 < n:
                        buf.append(src[i + 1])
                        i += 1
                    else:
                        buf.append(src[i])
                    i += 1
                out.append(("s", "".join(buf)))
                i += 1
        elif c == "'":
            m = re.match(r"'(?:\\.[^']*|[^\\'])'", src[i:i + 16])
            if m:
                i += m.end()
            else:
                i += 1
        elif c.isidentifier():
            m = re.compile(r"\w+").match(src, i)
            i = m.end()
            if m.group() == "r" and src.startswith("#", i) and src[i + 1:i + 2].isidentifier():
                i += 1  # raw identifier `r#name` is the identifier `name`
                m = re.compile(r"\w+").match(src, i)
                i = m.end()
            out.append(("i", m.group()))
        elif c.isdigit():
            i = re.compile(r"[\w.]+").match(src, i).end()
        else:
            out.append(("p", c))
            i += 1
    return out


def path_attrs(attr):
    """The `path = "..."` strings in an attribute's tokens, and whether `cfg_attr` makes them conditional."""
    found = [attr[k + 2][1] for k in range(len(attr) - 2)
             if attr[k] == ("i", "path") and attr[k + 1] == ("p", "=") and attr[k + 2][0] == "s"]
    return found, attr[:1] == [("i", "cfg_attr")] or attr[1:2] == [("i", "cfg_attr")]


def inline_dirs(stack, mod_dir, file_dir):
    """Directories `mod x;` can resolve in, given the enclosing inline modules. Each inline module is a list
    of alternatives: its name, or its `#[path]` values, which replace the implicit directory above it
    when it is the outermost one and otherwise extend it."""
    dirs = [mod_dir]
    outer = True
    for entry in stack:
        if entry is None:
            continue
        dirs = [(file_dir if outer else d) / seg if kind == "path" else d / seg
                for d in dirs for kind, seg in entry]
        outer = False
    return dirs


def scan(file, mod_dir, reached, seen):
    """Mark `file` and everything it reaches. mod_dir is where `mod x;` in it resolves. A file can be
    scanned once per module directory: as a module and as a target root it resolves its children differently."""
    file = file.resolve()
    if (file, mod_dir) in seen or not file.is_file():
        return
    seen.add((file, mod_dir))
    reached.add(file)
    toks = tokens(file.read_text(encoding="utf-8", errors="replace"))
    stack = []  # inline module directory components, None for any other block
    sure, maybe = [], []  # `#[path]` values before the next item; maybe = inside cfg_attr
    k = 0
    while k < len(toks):
        t = toks[k]
        if t == ("p", "#"):
            j = k + 1
            if j < len(toks) and toks[j] == ("p", "!"):
                j += 1
            if j < len(toks) and toks[j] == ("p", "["):
                depth, e = 0, j
                while e < len(toks):
                    depth += toks[e] == ("p", "[")
                    depth -= toks[e] == ("p", "]")
                    if depth == 0:
                        break
                    e += 1
                found, conditional = path_attrs(toks[j + 1:e])
                (maybe if conditional else sure).extend(found)
                k = e + 1
                continue
        elif t == ("i", "mod") and k + 2 < len(toks) and toks[k + 1][0] == "i":
            name, after = toks[k + 1][1], toks[k + 2]
            dirs = inline_dirs(stack, mod_dir, file.parent)
            inline = any(e is not None for e in stack)
            if after == ("p", ";"):
                for rel in sure or maybe:
                    for base in dirs if inline else [file.parent]:
                        target = base / rel
                        scan(target, target.resolve().parent, reached, seen)
                if not sure:
                    for base in dirs:
                        for cand, child in ((base / f"{name}.rs", base / name), (base / name / "mod.rs", base / name)):
                            if cand.is_file():
                                scan(cand, child if cand.name != "mod.rs" else cand.parent, reached, seen)
                                break
                sure, maybe = [], []
                k += 3
                continue
            if after == ("p", "{"):
                if sure:
                    stack.append([("path", sure[0])])
                else:
                    stack.append([("name", name)] + [("path", m) for m in maybe])
                sure, maybe = [], []
                k += 3
                continue
        elif t == ("i", "include") and toks[k + 1:k + 2] == [("p", "!")] and \
                len(toks) > k + 3 and toks[k + 2][1] in "([{" and toks[k + 3][0] == "s":
            target = (file.parent / toks[k + 3][1]).resolve()
            scan(target, target.parent, reached, seen)
        elif t == ("p", "{"):
            stack.append(None)
        elif t == ("p", "}"):
            if stack:
                stack.pop()
        if t in (("p", ";"), ("p", "}")):
            sure, maybe = [], []
        k += 1


def load(path):
    return tomllib.loads(path.read_text(encoding="utf-8"))


def used_path_deps(manifest, crate, root, ws_deps):
    """Directories of the local dependencies a manifest uses, including those it inherits with
    `workspace = true` from the workspace root's `[workspace.dependencies]`."""
    tables = [manifest.get(k, {}) for k in ("dependencies", "dev-dependencies", "build-dependencies")]
    for target in manifest.get("target", {}).values():
        tables += [target.get(k, {}) for k in ("dependencies", "dev-dependencies", "build-dependencies")]
    for table in tables:
        for key, dep in table.items():
            if not isinstance(dep, dict):
                continue
            if dep.get("workspace") is True and isinstance(ws_deps.get(key), dict) and "path" in ws_deps[key]:
                yield Path(os.path.normpath(root / ws_deps[key]["path"]))
            elif "path" in dep:
                yield Path(os.path.normpath(crate / dep["path"]))


def workspace_crates(root):
    """The root package, the workspace members, and the local path dependencies inside the workspace
    that a crate uses (Cargo makes those members without being asked), less `workspace.exclude`.
    Paths keep the repository's spelling; symlinks are resolved only to compare."""
    manifest = load(root / "Cargo.toml")
    ws = manifest.get("workspace", {})
    ws_deps = ws.get("dependencies", {})
    excluded = {p.resolve() for pat in ws.get("exclude", []) for p in root.glob(pat)}
    resolved_root = root.resolve()
    crates, queue, seen = [], [], set()
    if "package" in manifest:
        queue.append(root)
    for pat in ws.get("members", []):
        queue += [p for p in sorted(root.glob(pat)) if p.is_dir()]
    while queue:
        crate = queue.pop(0)
        key = crate.resolve()
        if key in seen or key in excluded or not (crate / "Cargo.toml").is_file():
            continue
        seen.add(key)
        crates.append(crate)
        for dep in used_path_deps(load(crate / "Cargo.toml"), crate, root, ws_deps):
            if resolved_root in dep.resolve().parents or dep.resolve() == resolved_root:
                queue.append(dep)
    return crates


def edition_of(package, ws_package):
    edition = package.get("edition", "2015")
    if isinstance(edition, dict):
        edition = ws_package.get("edition", "2015")
    return str(edition)


def crate_roots(crate, ws_package=None):
    """Every target root Cargo builds: explicit targets, and the conventional files unless the
    manifest overrides them, turns auto-discovery off, or names a target of the same name."""
    manifest = load(crate / "Cargo.toml")
    package = manifest.get("package", {})
    name = package.get("name", crate.name)
    edition = edition_of(package, ws_package or {})
    roots = []
    lib = manifest.get("lib")
    if isinstance(lib, dict) and "path" in lib:
        roots.append(crate / lib["path"])
    elif isinstance(lib, dict) or package.get("autolib", True):
        roots.append(crate / "src/lib.rs")
    build = package.get("build", True)
    if build is not False:
        roots.append(crate / (build if isinstance(build, str) else "build.rs"))
    for kind, folder, auto in (("bin", "src/bin", "autobins"), ("test", "tests", "autotests"),
                               ("bench", "benches", "autobenches"), ("example", "examples", "autoexamples")):
        explicit = manifest.get(kind, [])
        names, paths = set(), set()
        for t in explicit:
            n = t.get("name")
            if "path" in t:
                paths.add(os.path.normpath(crate / t["path"]))
                roots.append(crate / t["path"])
            elif n:
                if kind == "bin" and n == name:
                    roots.append(crate / "src/main.rs")
                roots += [crate / folder / f"{n}.rs", crate / folder / n / "main.rs"]
            if n:
                names.add(n)
        # Edition 2015 infers targets of a kind only while the manifest declares none of it.
        if not package.get(auto, edition != "2015" or not explicit):
            continue
        d = crate / folder
        inferred = []
        if d.is_dir():
            inferred += [(r, r.stem) for r in sorted(d.glob("*.rs"))]
            inferred += [(r, r.parent.name) for r in sorted(d.glob("*/main.rs"))]
        if kind == "bin":
            inferred.append((crate / "src/main.rs", name))
        roots += [r for r, n in inferred if n not in names and os.path.normpath(r) not in paths]
    return roots


def crate_files(crate):
    """Every .rs file under the crate (repository paths, symlinks not followed), skipping target/ and
    nested directories with their own manifest."""
    for dirpath, dirs, files in os.walk(crate):
        dirs[:] = [d for d in dirs if d != "target" and not (Path(dirpath, d, "Cargo.toml").is_file())]
        for f in files:
            if f.endswith(".rs"):
                yield Path(dirpath, f)


def glob_regex(pattern):
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def parse_allow(text):
    lines = (line.strip() for line in text.splitlines())
    return [glob_regex(line) for line in lines if line and not line.startswith("#")]


def git(root, *args, token=""):
    cmd = ["git", "-C", str(root)]
    if token:
        import base64
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        cmd += ["-c", f"http.extraheader=AUTHORIZATION: basic {basic}"]
    return subprocess.run(cmd + list(args), capture_output=True)


def text(done):
    return done.stdout.decode("utf-8", "surrogateescape")


def changed_files(root, base, token=""):
    """Repo-relative .rs paths the change adds or modifies against base, or None when there is no base or it cannot be fetched."""
    if not base or set(base) == {"0"}:
        return None
    if git(root, "cat-file", "-e", f"{base}^{{commit}}").returncode:
        fetched = git(root, "fetch", "--no-tags", "--depth=1", "origin", base, token=token)
        if fetched.returncode:
            # A push's previous head can be gone after a force-push; that is no reason to fail the run.
            print(f"::warning::unreached-rs: cannot fetch base {base}: {fetched.stderr.decode(errors='replace').strip()}")
            return None
    diff = git(root, "diff", "-z", "--name-status", "--no-renames", "--diff-filter=AM", base, "HEAD", "--", "*.rs")
    if diff.returncode:
        raise SystemExit(f"git diff failed: {diff.stderr.decode(errors='replace').strip()}")
    fields = text(diff).split("\0")
    return {fields[i + 1] for i in range(0, len(fields) - 1, 2)}


def unreached(root, extra=()):
    """Unreached files as repository paths. `extra` are changed files to check even when no crate holds them."""
    reached, seen, candidates = set(), set(), {}
    ws_package = load(root / "Cargo.toml").get("workspace", {}).get("package", {})
    for crate in workspace_crates(root):
        for r in crate_roots(crate, ws_package):
            scan(r, r.resolve().parent, reached, seen)
        for f in crate_files(crate):
            candidates[f.resolve()] = f
    for f in extra:
        if f.is_file():
            candidates.setdefault(f.resolve(), f)
    return sorted(f for key, f in candidates.items() if key not in reached)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--directory", default=".")
    ap.add_argument("--mode", choices=("diff", "strict", "off"), default="diff")
    ap.add_argument("--base", default="")
    ap.add_argument("--allow", default="", help="newline-separated globs (the text itself, not a file name)")
    args = ap.parse_args(argv)
    if args.mode == "off":
        return 0
    root = Path(os.path.realpath(args.directory))
    top = git(root, "rev-parse", "--show-toplevel")
    top = Path(os.path.realpath(text(top).strip())) if top.returncode == 0 else root
    allow = parse_allow(args.allow)
    changed = None
    if args.mode == "diff":
        changed = changed_files(root, args.base, os.environ.get("UNREACHED_RS_TOKEN", ""))
        if changed is None:
            print("::notice::unreached-rs: no base revision to compare with; skipping diff mode")
            return 0
    extra = [top / rel for rel in changed or () if (top / rel).is_relative_to(root) and "target" not in Path(rel).parts]
    bad = []
    for f in unreached(root, extra):
        rel = f.relative_to(top).as_posix()
        if any(rx.match(rel) for rx in allow):
            continue
        if changed is not None and rel not in changed:
            continue
        bad.append(rel)
    for rel in bad:
        print(f"::error file={rel}::{HINT}")
    if bad:
        print(f"unreached-rs: {len(bad)} unreached .rs file(s)")
        return 1
    print("unreached-rs: every checked .rs file is reached by a module")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Release many Craft apps at once: scripts/cut_release.py for each, in parallel.

    scripts/release_train.py storytold/pdfcraft storytold/wordcraft ...       # next minor each
    scripts/release_train.py --all --skip figcraft,soundcraft --publish
    scripts/release_train.py --all --dry-run

Each app gets its own log (--logs, default ./release-train-logs/<app>.log) and the train ends with
one line per app: published, pushed, or stopped and why. Apps don't wait for each other, and one
app stopping doesn't stop the rest. Options after `--` go to every cut_release.py run.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Every Craft app with a release.yml calling the shared workflow.
APPS = [
    "anicraft", "cadcraft", "deckcraft", "designcraft", "effectcraft", "figcraft", "filmcraft", "gridcraft",
    "lightcraft", "materialcraft", "pdfcraft", "photocraft", "soundcraft", "texturecraft", "vectorcraft",
    "wordcraft",
]


REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
# cut_release.py's options a train may pass on, and the values they may take.
FLAGS = {"--publish", "--dry-run", "--no-sync", "--allow-fewer-assets"}
VALUED = {"--part": re.compile(r"^(minor|patch)$"), "--gate-workflow": re.compile(r"^[A-Za-z0-9 ._()-]+$")}


def check(repos: list[str], extra: list[str]) -> None:
    """Only owner/name repos and known cut_release.py options reach the subprocess."""
    for repo in repos:
        if not REPO.match(repo):
            raise SystemExit(f"{repo!r} is not owner/name")
    i = 0
    while i < len(extra):
        opt = extra[i]
        if opt in FLAGS:
            i += 1
        elif opt in VALUED and i + 1 < len(extra) and VALUED[opt].match(extra[i + 1]):
            i += 2
        else:
            raise SystemExit(f"{opt!r} is not an option a train passes to cut_release.py")


def logs_dir(name: str) -> Path:
    """The log folder: a plain relative path inside the current directory, nothing outside it."""
    base = Path.cwd().resolve()
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", name) or Path(name).is_absolute():
        raise SystemExit(f"{name!r}: --logs is a relative folder name inside the current directory")
    path = (base / name).resolve()
    if path != base and base not in path.parents:
        raise SystemExit(f"{name!r}: --logs must stay inside {base}")
    return path


def run_app(repo: str, extra: list[str], logs: Path) -> tuple[str, int, str]:
    log = logs / f"{repo.split('/')[-1]}.log"
    with log.open("w", encoding="utf-8") as out:
        rc = subprocess.run([sys.executable, str(HERE / "cut_release.py"), repo, *extra],
                            stdout=out, stderr=subprocess.STDOUT).returncode
    lines = [l for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]
    return repo, rc, lines[-1] if lines else "(no output)"


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    extra = argv[argv.index("--") + 1:] if "--" in argv else []
    argv = argv[:argv.index("--")] if "--" in argv else argv
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("repos", nargs="*", help="owner/name of each app")
    p.add_argument("--all", action="store_true", help="every app in the list in this script")
    p.add_argument("--skip", default="", help="comma-separated app names to leave out")
    p.add_argument("--owner", default="storytold")
    p.add_argument("--publish", action="store_true", help="pass --publish: wait and publish each draft")
    p.add_argument("--dry-run", action="store_true", help="pass --dry-run: push nothing")
    p.add_argument("--logs", default="release-train-logs")
    a = p.parse_args(argv)

    skip = {s.strip() for s in a.skip.split(",") if s.strip()}
    repos = a.repos + ([f"{a.owner}/{app}" for app in APPS] if a.all else [])
    repos = [r for r in dict.fromkeys(repos) if r.split("/")[-1] not in skip]
    if not repos:
        p.error("name the apps, or pass --all")
    extra += ["--publish"] * a.publish + ["--dry-run"] * a.dry_run
    check(repos, extra)
    logs = logs_dir(a.logs)
    logs.mkdir(parents=True, exist_ok=True)

    start = time.time()
    print(f"releasing {len(repos)} apps; logs in {logs}/", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(repos)) as pool:
        futures = [pool.submit(run_app, r, extra, logs) for r in repos]
        results = []
        for f in concurrent.futures.as_completed(futures):
            repo, rc, last = f.result()
            results.append((repo, rc, last))
            print(f"{'ok  ' if rc == 0 else 'STOP'} {repo}: {last}", flush=True)
    stopped = [r for r in results if r[1] != 0]
    print(f"\n{len(results) - len(stopped)} of {len(results)} done in {(time.time() - start) / 60:.0f} min"
          + (f"; stopped: {', '.join(r[0].split('/')[-1] for r in stopped)}" if stopped else ""))
    return 1 if stopped else 0


if __name__ == "__main__":
    sys.exit(main())

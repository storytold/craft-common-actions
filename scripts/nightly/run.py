#!/usr/bin/env python3
"""Run one nightly signer module: run.py <module> <args...>.

Workflows call `python3 -I -B run.py ...`. Isolated mode leaves the script's directory off
sys.path, so this runner puts scripts/nightly and actions/nightly-feed there itself; the
modules then import each other and the nightly-feed library by plain name.
"""
import importlib
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
MODULES = ("admit", "platforms", "linux", "inventory", "smoke", "publish", "retain")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in MODULES:
        print(f"usage: run.py {{{','.join(MODULES)}}} [args...]", file=sys.stderr)
        return 2
    for path in (ROOT / "actions/nightly-feed", HERE):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    module = importlib.import_module(argv[0])
    return module.main(argv[1:])


if __name__ == "__main__":
    sys.exit(main())

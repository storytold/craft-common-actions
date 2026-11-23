#!/usr/bin/env python3
"""Run reviewed sibling modules with isolated Python, not the app/PYTHONPATH."""
import runpy
import sys
from pathlib import Path

if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    sys.path.insert(0, str(root))
    runpy.run_path(str(root/"nightly_release.py"), run_name="__main__")

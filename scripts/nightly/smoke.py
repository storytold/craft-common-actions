#!/usr/bin/env python3
"""Secret-free launch smoke test of a packaged nightly GUI: the launcher's version query.

Runs `<executable> --launch-profile <channel> --profile-root <fresh dir> --version` once, the way
craft-launcher probes an installed nightly, and requires exactly `<Product> <identity> (<channel>)`.
This proves the build flavour and version only; it is not a runtime qualification.

    smoke --app A --executable F --profile-root D --version ID
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import inventory  # noqa: E402
from inventory import c, feed, ni  # noqa: E402

LIMIT = 4096
TIMEOUT = 15


def smoke(app, executable, profile_root, version, registry=None):
    registry = registry if registry is not None else feed.load_registry()
    record = feed.app_record(registry, app)
    c.require(record is not None, f"{app} has no launcher adapter in the nightly registry")
    c.require(ni.identity_parts(version)["identity"] == version, "the version must be a bare nightly identity")
    executable, profile_root = Path(executable), Path(profile_root)
    c.require(executable.is_absolute() and executable.is_file() and not executable.is_symlink(),
              "explicit packaged GUI executable required")
    c.require(profile_root.is_absolute() and not profile_root.exists() and not profile_root.is_symlink(),
              "fresh absolute isolated smoke profile required")
    executable, profile_root = executable.resolve(), profile_root.resolve()
    c.require(executable.parent not in profile_root.parents and profile_root not in executable.parents,
              "smoke profile must be separate from the installation")
    profile_root.mkdir(parents=True)
    channel = record["build_channel"]
    argv = [str(executable), "--launch-profile", channel, "--profile-root", str(profile_root), "--version"]
    # The app's own variables could select another flavour, version or root; none reach the probe.
    prefix = app.upper().replace("-", "_") + "_"
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith(prefix)}
    # File capture avoids pipe deadlocks and holds at most LIMIT+1 bytes in memory.
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, env=environment)
        deadline = time.monotonic() + TIMEOUT
        try:
            while process.poll() is None:
                c.require(time.monotonic() < deadline, "packaged version smoke timed out")
                c.require(all(os.fstat(s.fileno()).st_size <= LIMIT for s in (stdout, stderr)), "smoke output exceeded bound")
                time.sleep(0.02)
            outputs = []
            for stream in (stdout, stderr):
                stream.seek(0)
                data = stream.read(LIMIT + 1)
                c.require(len(data) <= LIMIT, "smoke output exceeded bound")
                outputs.append(data)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    c.require(process.returncode == 0, f"packaged GUI --version exited {process.returncode}")
    c.require(not outputs[1], "unexpected smoke stderr")
    text = outputs[0].decode("utf-8").replace("\r\n", "\n")
    expected = inventory.version_line(record, version)
    c.require(text == expected, f"version output {text!r} differs from {expected!r}")
    return {"exact_version_and_flavor": True, "launch_profile": channel, "runtime_qualified": False}


def main(argv):
    parser = argparse.ArgumentParser(prog="smoke", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--app", required=True)
    parser.add_argument("--executable", required=True)
    parser.add_argument("--profile-root", required=True)
    parser.add_argument("--version", required=True, help="the nightly identity")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(smoke(args.app, args.executable, args.profile_root, args.version), sort_keys=True))
        return 0
    except (c.InvalidNightly, OSError, ValueError, UnicodeError, subprocess.SubprocessError) as error:
        print(f"::error::packaged nightly smoke refused: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

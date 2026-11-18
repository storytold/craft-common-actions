"""Keep FreeBSD host cleanup confined to unused hosted-image toolchains."""
from pathlib import Path
import shlex
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
DATA = yaml.load((ROOT / ".github/workflows/freebsd.yml").read_text(), Loader=yaml.BaseLoader)
JOB = DATA["jobs"]["freebsd"]
STEPS = JOB["steps"]
NAMES = [step.get("name", "") for step in STEPS]
CLEANUP = next(step for step in STEPS if step.get("name") == "Reclaim unused hosted toolchains")
PATHS = [
    "/usr/local/lib/android", "/usr/share/dotnet", "/opt/ghc",
    "/usr/local/.ghcup", "/opt/hostedtoolcache/CodeQL",
]
BYTES = 'df -B1 / "$RUNNER_TEMP" "$GITHUB_WORKSPACE"'
INODES = 'df -i / "$RUNNER_TEMP" "$GITHUB_WORKSPACE"'


class FreeBSDHostDiskTests(unittest.TestCase):
    def test_cleanup_runs_on_host_before_vm_and_cache(self):
        self.assertEqual(JOB["runs-on"], "ubuntu-latest")
        self.assertEqual(CLEANUP["shell"], "bash")
        self.assertNotIn("if", CLEANUP)
        self.assertNotIn("continue-on-error", CLEANUP)
        cleanup = NAMES.index("Reclaim unused hosted toolchains")
        self.assertLess(cleanup, NAMES.index("Start the FreeBSD VM"))
        self.assertLess(cleanup, NAMES.index("Restore FreeBSD Cargo cache"))

    def test_cleanup_is_foreground_and_exactly_allowlisted(self):
        # A finite command contract rejects new deletion paths, expansion, or backgrounding.
        lines = [line.strip() for line in CLEANUP["run"].splitlines() if line.strip()]
        self.assertEqual(lines, [
            "set -euo pipefail",
            'echo "::group::Host capacity before cleanup"', BYTES, INODES,
            'echo "::endgroup::"',
            "sudo rm -rf -- " + " ".join(PATHS),
            'echo "::group::Host capacity after cleanup"', BYTES, INODES,
            'echo "::endgroup::"',
        ])
        removal = next(line for line in lines if line.startswith("sudo "))
        self.assertEqual(shlex.split(removal), ["sudo", "rm", "-rf", "--", *PATHS])
        # Workspaces, Cargo caches, VM images and runner logs are never deletion candidates.
        for forbidden in ("$HOME", "$GITHUB_WORKSPACE", "$RUNNER_TEMP", "_diag", "craft-target", "craft-cargo"):
            self.assertNotIn(forbidden, removal)

    def test_capacity_is_measured_after_restore_before_build(self):
        measure = NAMES.index("Report host capacity before build")
        self.assertGreater(measure, NAMES.index("Restore FreeBSD Cargo cache"))
        self.assertLess(measure, NAMES.index("Build and test"))
        step = STEPS[measure]
        self.assertEqual(step["shell"], "bash")
        self.assertNotIn("if", step)
        self.assertEqual(step["run"].splitlines(), [BYTES, INODES])


if __name__ == "__main__":
    unittest.main()

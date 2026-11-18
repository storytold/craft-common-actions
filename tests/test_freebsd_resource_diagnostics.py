"""Ensure failure diagnostics are bounded and never replace workload results."""
from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
DATA = yaml.load((ROOT / ".github/workflows/freebsd.yml").read_text(), Loader=yaml.BaseLoader)
STEPS = DATA["jobs"]["freebsd"]["steps"]


class FreeBSDResourceDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.step = next(s for s in STEPS if s.get("name") == "Report final host and guest resources")

    def test_diagnostics_run_after_all_work_even_on_failure(self):
        self.assertIs(self.step, STEPS[-1])
        self.assertEqual(self.step["if"], "always() && !cancelled()")
        self.assertEqual(self.step["continue-on-error"], "true")
        self.assertEqual(self.step["timeout-minutes"], "2")
        self.assertEqual(self.step["shell"], "bash")
        for step in STEPS:
            if step.get("name") in ("Build and test", "Build and validate the FreeBSD package"):
                self.assertNotIn("continue-on-error", step)

    def test_guest_query_has_bounded_runtime_and_literal_read_only_commands(self):
        run = self.step["run"]
        self.assertIn('timeout --kill-after=5s 30s freebsd "$diagnostics"', run)
        guest = run.split("<<'FREEBSD_RESOURCES'\n", 1)[1].split("\nFREEBSD_RESOURCES", 1)[0]
        self.assertEqual(guest.splitlines(), [
            "df -k / /var/tmp",
            "swapinfo -k",
            "sysctl hw.physmem vm.stats.vm.v_free_count vm.stats.vm.v_inactive_count vm.stats.vm.v_wire_count",
            "sysctl kstat.zfs.misc.arcstats.size",
            "dmesg | grep -Ei 'out of swap|out of memory|killed|swap_pager' | tail -n 30 || true",
        ])
        self.assertNotIn("${{", run)
        self.assertNotIn("rm ", run)
        self.assertNotIn("sysctl -w", run)

    def test_host_reports_bytes_inodes_memory_and_only_filtered_kernel_messages(self):
        run = self.step["run"]
        self.assertIn('df -B1 / "$RUNNER_TEMP" "$GITHUB_WORKSPACE"', run)
        self.assertIn('df -i / "$RUNNER_TEMP" "$GITHUB_WORKSPACE"', run)
        self.assertIn("free -b || true", run)
        self.assertIn("sudo -n dmesg --color=never | grep -Ei 'out of memory|oom-kill|killed process' | tail -n 30 || true", run)


if __name__ == "__main__":
    unittest.main()

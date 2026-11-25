"""Shared helper checkouts must not make the app's own clean-source check fail."""
import os
import pathlib
import re
import subprocess
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/nightly-unsigned.yml"
STEP = "Keep the shared helper checkouts out of the app's clean-source check"
EXPECTED = ["/.nightly-tools/", "/.unsigned-plan", "/.unsigned-plan/", "/.unsigned-handoff/", "/craft-fonts/", "/craft-i18n/"]
BUILD_JOBS = ("build", "build-macos", "build-linux", "build-linux-packages")


def clean_env():
    """Git must act only on the temporary repository and need no signing key or user config."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", HOME=os.environ.get("HOME", "/tmp"))
    return env


def jobs():
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]


def step(job):
    steps = jobs()[job]["steps"]
    found = [s for s in steps if s.get("name") == STEP]
    assert len(found) == 1, job
    return steps, found[0]


class CleanSourceExcludeTests(unittest.TestCase):
    def test_every_app_build_job_excludes_exactly_the_helper_paths_before_building(self):
        for job in BUILD_JOBS:
            with self.subTest(job=job):
                steps, s = step(job)
                self.assertEqual(s.get("shell"), "bash")
                listed = re.search(r"printf '%s\\n' (.*?) >>", s["run"]).group(1).split()
                self.assertEqual(listed, EXPECTED)
                index = steps.index(s)
                for later in steps[:index]:
                    self.assertNotIn("packaging/", str(later.get("run", "")), job)

    def test_the_step_makes_only_helper_paths_invisible_to_git_status(self):
        script = step("build-linux")[1]["run"]
        with tempfile.TemporaryDirectory() as tmp:
            repo = pathlib.Path(tmp)
            env = clean_env()
            run = lambda *a: subprocess.run(a, cwd=repo, env=env, check=True, capture_output=True, text=True).stdout
            run("git", "init", "-q")
            run("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--no-gpg-sign", "--allow-empty", "-m", "x")
            for helper in (".nightly-tools/x", ".unsigned-plan", ".unsigned-handoff/a", "craft-fonts/fonts/f", "craft-i18n/i"):
                p = repo / helper
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("x")
            subprocess.run(["bash", "-c", script], cwd=repo, env=env, check=True)
            self.assertEqual(run("git", "status", "--porcelain"), "")
            # Anything else, including a file that only looks like a helper, is still seen.
            (repo / "src-change.txt").write_text("x")
            (repo / "nested").mkdir()
            (repo / "nested" / "craft-fonts").write_text("x")
            self.assertEqual(sorted(run("git", "status", "--porcelain").split()), sorted(["??", "nested/", "??", "src-change.txt"]))


if __name__ == "__main__":
    unittest.main()

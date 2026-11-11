"""scripts/release_train.py: which apps a train releases, and what it passes on."""
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release_train", ROOT / "scripts/release_train.py")
release_train = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_train)


class TrainTests(unittest.TestCase):
    def run_train(self, argv, rc=0):
        calls = []

        def fake(repo, extra, logs):
            calls.append((repo, extra))
            return repo, rc, "done"

        with mock.patch.object(release_train, "run_app", side_effect=fake):
            code = release_train.main(["--logs", tempfile.mkdtemp()] + argv)
        return code, calls

    def test_all_but_the_skipped_apps_with_the_flags_passed_on(self):
        code, calls = self.run_train(["--all", "--skip", "figcraft,soundcraft", "--publish", "--", "--part", "patch"])
        repos = sorted(r for r, _ in calls)
        self.assertEqual(code, 0)
        self.assertEqual(len(repos), len(release_train.APPS) - 2)
        self.assertNotIn("storytold/figcraft", repos)
        self.assertTrue(all(extra == ["--part", "patch", "--publish"] for _, extra in calls))

    def test_one_stopped_app_fails_the_train(self):
        code, _ = self.run_train(["storytold/pdfcraft"], rc=1)
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()

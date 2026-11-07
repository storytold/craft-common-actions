"""The SonarCloud issue helper: pagination, the reduced fields and the output file."""
from pathlib import Path
import importlib.util
import io
import json
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sonar_issues", ROOT / "scripts/sonar_issues.py")
sonar_issues = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sonar_issues)


class FakeResponse:
    def __init__(self, payload):
        self._body = io.BytesIO(json.dumps(payload).encode())

    def __enter__(self):
        return self._body

    def __exit__(self, *_):
        self._body.close()


class SonarIssuesTests(unittest.TestCase):
    def test_reduce_issue_keeps_the_fields_an_ai_needs(self):
        issue = {
            "component": "storytold_craft-common-actions:actions/packaging-lint/action.yml",
            "line": 79,
            "textRange": {"startLine": 79, "startOffset": 8, "endOffset": 18},
            "rule": "githubactions:S7637",
            "severity": "MAJOR",
            "type": "VULNERABILITY",
            "message": "Do not use a variable in a path.",
        }
        self.assertEqual(
            sonar_issues.reduce_issue(issue),
            {
                "file": "actions/packaging-lint/action.yml",
                "line": 79,
                "startOffset": 8,
                "endOffset": 18,
                "rule": "githubactions:S7637",
                "severity": "MAJOR",
                "type": "VULNERABILITY",
                "message": "Do not use a variable in a path.",
            },
        )

    def test_reduce_issue_keeps_file_level_issues(self):
        self.assertEqual(
            sonar_issues.reduce_issue({"component": "project:README.md", "rule": "x:1", "message": "m"}),
            {
                "file": "README.md",
                "line": None,
                "startOffset": None,
                "endOffset": None,
                "rule": "x:1",
                "severity": None,
                "type": None,
                "message": "m",
            },
        )

    def test_fetch_issues_follows_paging(self):
        pages = [
            {"paging": {"total": 2}, "issues": [{"component": "p:a.py", "message": "one"}]},
            {"paging": {"total": 2}, "issues": [{"component": "p:b.py", "message": "two"}]},
        ]
        requests = []

        def opener(request, timeout=60):
            requests.append(request.full_url)
            return FakeResponse(pages.pop(0))

        total, issues = sonar_issues.fetch_issues("p", "https://sonar.example", opener=opener)
        self.assertEqual(total, 2)
        self.assertEqual([issue["message"] for issue in issues], ["one", "two"])
        self.assertEqual(len(requests), 2)
        self.assertIn("resolved=false", requests[0])
        self.assertIn("p=2", requests[1])

    def test_main_writes_the_filtered_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "issues.json"
            issue = {"component": "p:a.py", "message": "one"}
            with mock.patch.object(sonar_issues, "fetch_issues", return_value=(1, [issue])):
                self.assertEqual(sonar_issues.main(["p", "--out", str(out)]), 0)
            written = json.loads(out.read_text())
        self.assertEqual(written, {"total": 1, "issues": [sonar_issues.reduce_issue(issue)]})


if __name__ == "__main__":
    unittest.main()

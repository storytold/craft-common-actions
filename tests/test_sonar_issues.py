"""The SonarCloud issue helper: pagination, the reduced fields and the output file."""
import importlib.util
import io
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
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

    def test_base_url_must_be_https_and_credential_free(self):
        for url in (
            "http://sonar.example",
            "https://user:pass@sonar.example",
            "https://sonar.example?project=p",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                sonar_issues.fetch_issues("p", url, opener=lambda *_: self.fail("must not fetch"))

    def test_redirects_cannot_leave_the_https_origin(self):
        handler = sonar_issues._SameOriginHTTPSRedirectHandler()
        request = urllib.request.Request("https://sonar.example/api/issues/search")
        for url in ("http://sonar.example/api/issues/search", "https://evil.example/api/issues/search"):
            with self.subTest(url=url), self.assertRaises(urllib.error.HTTPError):
                handler.redirect_request(request, None, 302, "Found", {}, url)

    def test_fetch_issues_refuses_the_paging_cap(self):
        def opener(request, timeout=60):
            return FakeResponse({"paging": {"total": 10_000}, "issues": [{"component": "p:a.py"}]})

        with self.assertRaisesRegex(ValueError, "complete export"):
            sonar_issues.fetch_issues("p", "https://sonar.example", opener=opener)

    def test_main_reports_output_file_errors(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(sonar_issues, "fetch_issues", return_value=(0, [])):
            self.assertEqual(sonar_issues.main(["p", "--out", tmp]), 1)


if __name__ == "__main__":
    unittest.main()

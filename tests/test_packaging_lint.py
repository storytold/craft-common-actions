"""Exercise the packaging tool downloads with real curl and a local HTTP fixture."""

from collections import Counter
from functools import partial
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tarfile
import tempfile
from threading import Thread
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / "actions/packaging-lint/action.yml"


def install_script():
    action = yaml.load(ACTION.read_text(), Loader=yaml.BaseLoader)
    return next(step["run"] for step in action["runs"]["steps"]
                if step.get("name") == "Install tools")


class DownloadHandler(BaseHTTPRequestHandler):
    def __init__(self, *args, files, failures, requests, **kwargs):
        self.files = files
        self.failures = failures
        self.requests = requests
        super().__init__(*args, **kwargs)

    def do_GET(self):
        name = self.path.rsplit("/", 1)[-1]
        self.requests[name] += 1
        failures = self.failures.get(name, [])
        status = failures.pop(0) if failures else 200
        body = self.files[name] if status == 200 else b"download unavailable"
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@unittest.skipUnless(shutil.which("curl"), "curl is required")
class PackagingDownloadTests(unittest.TestCase):
    archive_name = "actionlint_0.0.1_linux_amd64.tar.gz"
    checksum_name = "actionlint_0.0.1_checksums.txt"

    def run_install(self, failures=None, bad_checksum=False):
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as tar:
            data = b"fixture actionlint binary\n"
            entry = tarfile.TarInfo("actionlint")
            entry.size = len(data)
            tar.addfile(entry, io.BytesIO(data))
        payload = archive.getvalue()
        digest = "0" * 64 if bad_checksum else hashlib.sha256(payload).hexdigest()
        files = {self.archive_name: payload,
                 self.checksum_name: f"{digest}  {self.archive_name}\n".encode()}
        requests = Counter()
        handler = partial(DownloadHandler, files=files, failures=failures or {}, requests=requests)
        with HTTPServer(("127.0.0.1", 0), handler) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with tempfile.TemporaryDirectory(prefix="packaging-download-") as directory:
                    work = Path(directory)
                    bin_dir = work / "bin"
                    bin_dir.mkdir()
                    # Do not install system packages in a test. The download, digest check,
                    # and extraction still execute the action's real shell commands.
                    sudo = bin_dir / "sudo"
                    sudo.write_text("#!/bin/sh\nexit 0\n")
                    sudo.chmod(0o755)
                    script = install_script()
                    # The fixture is loopback HTTP, not a release download. Test HTTPS
                    # restrictions separately; preserve curl's actual retry/failure flags.
                    script = script.replace("https://github.com/rhysd/actionlint/releases/download",
                                            f"http://127.0.0.1:{server.server_port}")
                    script = script.replace("'=https'", "'=http'").replace("=https", "=http")
                    result = subprocess.run(
                        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script],
                        env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                             "RUNNER_TEMP": str(work), "ACTIONLINT_VERSION": "0.0.1",
                             "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"},
                        text=True, capture_output=True, timeout=30,
                    )
                    extracted = (work / "actionlint").exists()
            finally:
                server.shutdown()
                thread.join(timeout=5)
        return result, requests, extracted

    def test_transient_archive_failure_is_retried(self):
        result, requests, extracted = self.run_install({self.archive_name: [503]})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(requests[self.archive_name], 2)
        self.assertEqual(requests[self.checksum_name], 1)
        self.assertTrue(extracted)

    def test_transient_checksum_failure_is_retried(self):
        result, requests, extracted = self.run_install({self.checksum_name: [503]})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(requests[self.checksum_name], 2)
        self.assertTrue(extracted)

    def test_persistent_failure_stops_after_three_retries(self):
        result, requests, extracted = self.run_install({self.archive_name: [503] * 4})
        self.assertEqual(result.returncode, 22)
        self.assertEqual(requests[self.archive_name], 4)
        self.assertEqual(requests[self.checksum_name], 0)
        self.assertFalse(extracted)

    def test_missing_release_is_not_retried(self):
        result, requests, extracted = self.run_install({self.archive_name: [404]})
        self.assertEqual(result.returncode, 22)
        self.assertEqual(requests[self.archive_name], 1)
        self.assertFalse(extracted)

    def test_checksum_mismatch_still_blocks_extraction(self):
        result, requests, extracted = self.run_install(bad_checksum=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FAILED", result.stdout)
        self.assertFalse(extracted)

    def test_both_downloads_retain_https_and_finite_limits(self):
        downloads = [shlex.split(line) for line in install_script().splitlines()
                     if line.strip().startswith("curl ")]
        self.assertEqual(len(downloads), 2)
        expected = {"--proto": "=https", "--proto-redir": "=https", "--retry": "3",
                    "--retry-max-time": "120", "--connect-timeout": "15", "--max-time": "60"}
        for args in downloads:
            for option, value in expected.items():
                with self.subTest(option=option, url=args[-1]):
                    self.assertIn(option, args)
                    self.assertEqual(args[args.index(option) + 1], value)
            self.assertIn("-fsSL", args)


if __name__ == "__main__":
    unittest.main()

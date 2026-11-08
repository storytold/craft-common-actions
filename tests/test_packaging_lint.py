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
import socket
import subprocess
import tarfile
import tempfile
from threading import Thread
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / "actions/packaging-lint/action.yml"


def install_script():
    """Read the current install step so tests exercise the shipped shell code."""
    action = yaml.load(ACTION.read_text(), Loader=yaml.BaseLoader)
    return next(step["run"] for step in action["runs"]["steps"]
                if step.get("name") == "Install tools")


class DownloadHandler(BaseHTTPRequestHandler):
    """Serve release fixtures after any configured sequence of HTTP errors."""

    def __init__(self, *args, files, failures, requests, **kwargs):
        """Share fixture payloads and request counters between connections."""
        self.files = files
        self.failures = failures
        self.requests = requests
        super().__init__(*args, **kwargs)

    def do_GET(self):
        """Record an attempt and return the next configured status or payload."""
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
        """Suppress fixture access logs while preserving subprocess diagnostics."""
        pass


@unittest.skipUnless(shutil.which("curl"), "curl is required")
class PackagingDownloadTests(unittest.TestCase):
    """Verify retry bounds and verification failures without external downloads."""

    archive_name = "actionlint_0.0.1_linux_amd64.tar.gz"
    checksum_name = "actionlint_0.0.1_checksums.txt"

    def run_install(self, failures=None, bad_checksum=False, checksum_entry=None):
        """Run the install step against a disposable server and return its outcome."""
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as tar:
            data = b"fixture actionlint binary\n"
            entry = tarfile.TarInfo("actionlint")
            entry.size = len(data)
            tar.addfile(entry, io.BytesIO(data))
        payload = archive.getvalue()
        digest = "0" * 64 if bad_checksum else hashlib.sha256(payload).hexdigest()
        entry_name = self.archive_name if checksum_entry is None else checksum_entry
        files = {self.archive_name: payload,
                 self.checksum_name: f"{digest}  {entry_name}\n".encode()}
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
        """A successful retry replaces the failed archive response before extraction."""
        result, requests, extracted = self.run_install({self.archive_name: [503]})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(requests[self.archive_name], 2)
        self.assertEqual(requests[self.checksum_name], 1)
        self.assertTrue(extracted)

    def test_transient_checksum_failure_is_retried(self):
        """The optional checksum download recovers from a transient HTTP failure."""
        result, requests, extracted = self.run_install({self.checksum_name: [503]})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(requests[self.checksum_name], 2)
        self.assertTrue(extracted)

    def test_persistent_failure_stops_after_three_retries(self):
        """Four failed attempts stop setup before fetching checksums or extracting."""
        result, requests, extracted = self.run_install({self.archive_name: [503] * 4})
        self.assertEqual(result.returncode, 22)
        self.assertEqual(requests[self.archive_name], 4)
        self.assertEqual(requests[self.checksum_name], 0)
        self.assertFalse(extracted)

    def test_missing_release_is_not_retried(self):
        """A permanent HTTP 404 still fails immediately."""
        result, requests, extracted = self.run_install({self.archive_name: [404]})
        self.assertEqual(result.returncode, 22)
        self.assertEqual(requests[self.archive_name], 1)
        self.assertFalse(extracted)

    def test_checksum_mismatch_still_blocks_extraction(self):
        """Retry support must not permit extraction of an unverified archive."""
        result, requests, extracted = self.run_install(bad_checksum=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FAILED", result.stdout)
        self.assertFalse(extracted)

    def test_missing_checksum_entry_is_reported(self):
        """Name the missing archive when the checksum file has no matching entry."""
        result, requests, extracted = self.run_install(checksum_entry="other.tar.gz")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"::error::missing checksum for {self.archive_name}", result.stdout)
        self.assertFalse(extracted)

    def test_checksum_entry_must_match_the_exact_archive_name(self):
        """A similarly named file cannot supply the archive's checksum."""
        result, requests, extracted = self.run_install(checksum_entry=self.archive_name + ".sig")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"::error::missing checksum for {self.archive_name}", result.stdout)
        self.assertFalse(extracted)

    def test_refused_connection_is_retried(self):
        """A bound but non-listening loopback socket produces four refused attempts."""
        command = next(line for line in install_script().splitlines()
                       if line.strip().startswith("curl "))
        command = command.replace("'=https'", "'=http'").replace("=https", "=http")
        with socket.socket() as closed_port, tempfile.TemporaryDirectory() as directory:
            # Reserve the port without listening, so another process cannot claim it.
            closed_port.bind(("127.0.0.1", 0))
            port = closed_port.getsockname()[1]
            result = subprocess.run(
                ["bash", "-e", "-c", command],
                env={**os.environ, "RUNNER_TEMP": directory,
                     "tar_url": f"http://127.0.0.1:{port}/archive.tar.gz",
                     "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"},
                text=True, capture_output=True, timeout=30,
            )
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stderr.count("curl: (7)"), 4, result.stderr)

    def test_both_downloads_retain_https_and_finite_limits(self):
        """Both release requests retain HTTPS restrictions and bounded retries."""
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
            self.assertIn("--retry-connrefused", args)


PACKAGING_ACTION = yaml.safe_load(ACTION.read_text())
POWERSHELL = next(step for step in PACKAGING_ACTION["runs"]["steps"]
                  if step.get("name") == "PowerShell")
SCRIPT = POWERSHELL["run"]


class PackagingLintTests(unittest.TestCase):
    def test_windows_step_checks_the_directory_before_listing_it(self):
        guard = "if (Test-Path ./packaging/windows)"
        listing = "Get-ChildItem packaging/windows/*.ps1"
        self.assertIn(guard, SCRIPT)
        self.assertLess(SCRIPT.index(guard), SCRIPT.index(listing))

    @unittest.skipUnless(shutil.which("pwsh"), "PowerShell 7 is not installed")
    def test_windows_step_runs_without_a_windows_packaging_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                ["pwsh", "-NoProfile", "-NonInteractive", "-Command", SCRIPT],
                cwd=tmp,
                capture_output=True,
                text=True,
                timeout=60,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("pwsh"), "PowerShell 7 is not installed")
    def test_windows_step_still_checks_scripts_when_the_directory_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            windows = Path(tmp) / "packaging" / "windows"
            windows.mkdir(parents=True)
            (windows / "bad.ps1").write_text("if (", encoding="utf-8")
            result = subprocess.run(
                ["pwsh", "-NoProfile", "-NonInteractive", "-Command", SCRIPT],
                cwd=tmp,
                capture_output=True,
                text=True,
                timeout=60,
            )
        self.assertEqual(result.returncode, 1, result.stdout)


if __name__ == "__main__":
    unittest.main()

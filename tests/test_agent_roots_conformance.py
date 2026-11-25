"""Tests for the shared Rule 18 agent-roots conformance check."""

import importlib.util
import io
import os
from pathlib import Path
import stat
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "actions" / "agent-roots-conformance" / "check_agent_roots.py"


def load():
    spec = importlib.util.spec_from_file_location("check_agent_roots", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check_agent_roots = load()


FAKE_CLI_TEMPLATE = textwrap.dedent(
    """\
    #!{python}
    import json
    import os
    import socket
    import sys
    import time

    MODE = {mode!r}
    LOG_FILE = {log_file!r}


    def record(entry):
        if LOG_FILE:
            with open(LOG_FILE, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\\n")


    def serve_mcp():
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                req = json.loads(raw)
            except json.JSONDecodeError:
                continue
            reply = {{
                "jsonrpc": "2.0",
                "id": req.get("id", 1),
                "result": {{
                    "protocolVersion": "2025-06-18",
                    "capabilities": {{}},
                    "serverInfo": {{"name": "fake-craft-cli", "version": "0.1.0"}},
                }},
            }}
            sys.stdout.write(json.dumps(reply) + "\\n")
            sys.stdout.flush()


    def main():
        argv = sys.argv[1:]
        record({{"argv": argv, "env_keys": sorted(os.environ.keys())}})
        if not argv or argv[0] != "mcp":
            sys.stderr.write("expected mcp subcommand\\n")
            return 2

        read_roots = []
        write_roots = []
        connect_target = None
        i = 1
        while i < len(argv):
            arg = argv[i]
            if arg == "--automation-read-root" and i + 1 < len(argv):
                if MODE == "unknown-root-flags":
                    sys.stderr.write("unknown option: --automation-read-root\\n")
                    return 2
                read_roots.append(argv[i + 1])
                i += 2
            elif arg == "--automation-write-root" and i + 1 < len(argv):
                if MODE == "unknown-root-flags":
                    sys.stderr.write("unknown option: --automation-write-root\\n")
                    return 2
                write_roots.append(argv[i + 1])
                i += 2
            elif arg in ("--connect", "--bridge", "--attach") and i + 1 < len(argv):
                connect_target = argv[i + 1]
                i += 2
            elif arg in ("--headless", "--control-token"):
                i += 2 if arg == "--control-token" else 1
            else:
                i += 1

        if connect_target is not None:
            if MODE == "connect-refuse-unconditional":
                sys.stderr.write("connected mode is not supported until desktop enforces roots\\n")
                return 1
            if MODE == "connect-verify-handshake":
                if ":" in connect_target:
                    host, port_s = connect_target.rsplit(":", 1)
                    port = int(port_s)
                else:
                    host, port = "127.0.0.1", int(connect_target)
                with socket.create_connection((host, port), timeout=2.0) as sock:
                    sock.sendall((json.dumps({{"id": 7, "method": "control.capabilities"}}) + "\\n").encode("utf-8"))
                    line = sock.makefile("r", encoding="utf-8").readline()
                resp = json.loads(line)
                result = resp.get("result") or {{}}
                if not (result.get("read_roots") and result.get("write_roots")):
                    sys.stderr.write("target app does not enforce both automation roots\\n")
                    return 1
                serve_mcp()
                return 0
            if MODE == "connect-lazy-allow":
                serve_mcp()
                return 0

        if MODE == "hang-without-roots" and not (read_roots and write_roots):
            time.sleep(30)
            return 0
        if MODE == "spawn-detached-pipe-holder" and not (read_roots and write_roots):
            import subprocess
            subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                start_new_session=True,
                close_fds=False,
            )
            sys.stderr.write("refused with detached helper\\n")
            return 1
        if MODE == "spawn-detached-stderr-spammer" and not (read_roots and write_roots):
            import subprocess
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import sys, time; [sys.stderr.write('warn') or sys.stderr.flush() or time.sleep(0.005) for _ in range(200)]",
                ],
                start_new_session=True,
                close_fds=False,
            )
            sys.stderr.write("refused with stderr-spamming helper\\n")
            return 1
        if MODE == "noisy-stderr" and not (read_roots and write_roots):
            sys.stderr.write("x" * 65536 + "\\n")
            return 1
        if MODE == "large-init-then-exit-nonzero" and not read_roots and not write_roots:
            reply = {{
                "jsonrpc": "2.0",
                "id": 1,
                "result": {{"instructions": "A" * 12000}},
            }}
            sys.stdout.write(json.dumps(reply) + "\\n")
            sys.stdout.flush()
            return 1
        if MODE == "stdout-flood" and not read_roots and not write_roots:
            sys.stdout.write("Z" * 300000 + "\\n")
            sys.stdout.flush()
            return 1
        if MODE == "exit-zero-without-roots" and not (read_roots and write_roots):
            return 0
        if MODE == "allow-no-roots" and not read_roots and not write_roots:
            serve_mcp()
            return 0
        if MODE == "allow-read-only" and read_roots and not write_roots:
            serve_mcp()
            return 0
        if MODE == "allow-write-only" and write_roots and not read_roots:
            serve_mcp()
            return 0
        if MODE == "unknown-root-flags":
            serve_mcp()
            return 0

        if not read_roots or not write_roots:
            sys.stderr.write("both --automation-read-root and --automation-write-root are required\\n")
            return 1

        if MODE != "allow-missing-read":
            for path in read_roots:
                if not os.path.isdir(path):
                    sys.stderr.write(f"read root does not exist: {{path}}\\n")
                    return 1
        if MODE != "allow-missing-write":
            for path in write_roots:
                if not os.path.isdir(path):
                    sys.stderr.write(f"write root does not exist: {{path}}\\n")
                    return 1

        serve_mcp()
        return 0


    raise SystemExit(main())
    """
)


class ConformanceRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="test-agent-roots-")
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)

    def make_fake_cli(self, mode: str, *, log_file: Path | None = None) -> Path:
        script = self.work / f"fake-cli-{mode}.py"
        script.write_text(
            FAKE_CLI_TEMPLATE.format(
                python=sys.executable,
                mode=mode,
                log_file=str(log_file) if log_file is not None else "",
            ),
            encoding="utf-8",
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        return script

    def by_name(self, results):
        return {item.name: item for item in results}

    def test_compliant_standalone_cli_passes(self) -> None:
        cli = self.make_fake_cli("compliant")
        results = check_agent_roots.run_conformance(cli, timeout=3.0)
        self.assertEqual(len(results), 6)
        self.assertTrue(all(r.passed for r in results), results)

    def test_compliant_cli_refusing_connect_unconditionally_passes(self) -> None:
        cli = self.make_fake_cli("connect-refuse-unconditional")
        results = check_agent_roots.run_conformance(
            cli, connect_flag="--connect", timeout=3.0
        )
        self.assertEqual(len(results), 7)
        self.assertTrue(all(r.passed for r in results), results)

    def test_compliant_cli_verifying_control_handshake_passes(self) -> None:
        log_file = self.work / "calls.jsonl"
        cli = self.make_fake_cli("connect-verify-handshake", log_file=log_file)
        results = check_agent_roots.run_conformance(
            cli,
            connect_flag="--bridge",
            connect_address_format="port",
            timeout=3.0,
        )
        self.assertEqual(len(results), 7)
        self.assertTrue(all(r.passed for r in results), results)
        logged = log_file.read_text(encoding="utf-8")
        self.assertIn('"--bridge"', logged)

    def test_unconfined_no_roots_fails(self) -> None:
        cli = self.make_fake_cli("allow-no-roots")
        results = self.by_name(check_agent_roots.run_conformance(cli, timeout=3.0))
        self.assertFalse(results["no-roots"].passed)
        self.assertIn("initialize", results["no-roots"].detail)

    def test_single_root_only_failures_are_caught(self) -> None:
        cli_read = self.make_fake_cli("allow-read-only")
        res_read = self.by_name(check_agent_roots.run_conformance(cli_read, timeout=3.0))
        self.assertFalse(res_read["read-root-only"].passed)
        self.assertTrue(res_read["write-root-only"].passed)

        cli_write = self.make_fake_cli("allow-write-only")
        res_write = self.by_name(check_agent_roots.run_conformance(cli_write, timeout=3.0))
        self.assertTrue(res_write["read-root-only"].passed)
        self.assertFalse(res_write["write-root-only"].passed)

    def test_missing_roots_are_caught(self) -> None:
        cli_mr = self.make_fake_cli("allow-missing-read")
        res_mr = self.by_name(check_agent_roots.run_conformance(cli_mr, timeout=3.0))
        self.assertFalse(res_mr["missing-read-root"].passed)
        self.assertTrue(res_mr["missing-write-root"].passed)

        cli_mw = self.make_fake_cli("allow-missing-write")
        res_mw = self.by_name(check_agent_roots.run_conformance(cli_mw, timeout=3.0))
        self.assertTrue(res_mw["missing-read-root"].passed)
        self.assertFalse(res_mw["missing-write-root"].passed)

    def test_unknown_root_flags_caught_by_positive_initialize_case(self) -> None:
        cli = self.make_fake_cli("unknown-root-flags")
        results = self.by_name(check_agent_roots.run_conformance(cli, timeout=3.0))
        self.assertFalse(results["no-roots"].passed)
        self.assertFalse(results["both-roots-initialize"].passed)
        self.assertIn("exit 2", results["both-roots-initialize"].detail)

    def test_lazy_or_unconfined_connected_mode_fails(self) -> None:
        cli = self.make_fake_cli("connect-lazy-allow")
        results = self.by_name(
            check_agent_roots.run_conformance(
                cli,
                connect_flag="--bridge",
                connect_extra_args=("--control-token", "0000"),
                timeout=3.0,
            )
        )
        self.assertFalse(results["connect-unconfined-refused"].passed)
        self.assertIn("initialize", results["connect-unconfined-refused"].detail)

    def test_exit_zero_or_hang_without_roots_fails(self) -> None:
        cli_zero = self.make_fake_cli("exit-zero-without-roots")
        res_zero = self.by_name(check_agent_roots.run_conformance(cli_zero, timeout=2.0))
        self.assertFalse(res_zero["no-roots"].passed)
        self.assertIn("exited 0", res_zero["no-roots"].detail)

        cli_hang = self.make_fake_cli("hang-without-roots")
        res_hang = self.by_name(check_agent_roots.run_conformance(cli_hang, timeout=0.6))
        self.assertFalse(res_hang["no-roots"].passed)
        self.assertIn("within 0.6s", res_hang["no-roots"].detail)

    def test_sanitized_env_strips_ambient_root_and_control_vars(self) -> None:
        clean = check_agent_roots.sanitized_env(
            {
                "PATH": "/usr/bin",
                "PHOTOCRAFT_CONTROL_PORT": "9999",
                "VECTORCRAFT_AUTOMATION_READ_ROOT": "/tmp/a",
                "VECTORCRAFT_AUTOMATION_WRITE_ROOT": "/tmp/b",
                "AUTOMATION_READ_ROOT": "/tmp/c",
                "APP_CONTROL_TOKEN": "secret",
            }
        )
        self.assertEqual(clean, {"PATH": "/usr/bin"})

    def test_cli_main_returns_zero_on_pass_and_one_on_failure(self) -> None:
        good = self.make_fake_cli("connect-refuse-unconditional")
        bad = self.make_fake_cli("allow-no-roots")
        with mock.patch("sys.stdout", new=io.StringIO()), mock.patch(
            "sys.stderr", new=io.StringIO()
        ):
            self.assertEqual(
                check_agent_roots.main(
                    ["--binary", str(good), "--connect-flag", "--connect", "--timeout", "3"]
                ),
                0,
            )
            self.assertEqual(
                check_agent_roots.main(["--binary", str(bad), "--timeout", "3"]),
                1,
            )

    def test_relative_dot_slash_binary_executes_without_path_lookup(self) -> None:
        cli = self.make_fake_cli("compliant")
        prev_cwd = os.getcwd()
        try:
            os.chdir(cli.parent)
            results = check_agent_roots.run_conformance(
                Path(f"./{cli.name}"), timeout=3.0
            )
        finally:
            os.chdir(prev_cwd)
        self.assertEqual(len(results), 6)
        self.assertTrue(all(r.passed for r in results), results)

    def test_detached_descendant_holding_pipes_does_not_hang_cleanup(self) -> None:
        cli = self.make_fake_cli("spawn-detached-pipe-holder")
        started = time.monotonic()
        results = check_agent_roots.run_conformance(cli, timeout=2.0)
        elapsed = time.monotonic() - started
        self.assertTrue(all(r.passed for r in results), results)
        self.assertLess(elapsed, 5.0)

    def test_detached_helper_streaming_only_stderr_does_not_fail_empty_stdout(self) -> None:
        cli = self.make_fake_cli("spawn-detached-stderr-spammer")
        results = check_agent_roots.run_conformance(cli, timeout=2.0)
        self.assertTrue(all(r.passed for r in results), results)

    def test_noisy_stderr_is_drained_with_bounded_retention(self) -> None:
        cli = self.make_fake_cli("noisy-stderr")
        results = check_agent_roots.run_conformance(cli, timeout=3.0)
        self.assertTrue(all(r.passed for r in results), results)

    def test_large_initialize_reply_before_nonzero_exit_is_caught(self) -> None:
        cli = self.make_fake_cli("large-init-then-exit-nonzero")
        results = self.by_name(check_agent_roots.run_conformance(cli, timeout=3.0))
        self.assertFalse(results["no-roots"].passed)
        self.assertIn("initialize", results["no-roots"].detail)

    def test_stdout_flood_fails_explicitly_within_limit(self) -> None:
        cli = self.make_fake_cli("stdout-flood")
        results = self.by_name(check_agent_roots.run_conformance(cli, timeout=3.0))
        self.assertFalse(results["no-roots"].passed)
        self.assertIn("limit", results["no-roots"].detail)


class AgentRootsWiringTests(unittest.TestCase):
    def test_action_passes_inputs_through_env_without_inline_expressions(self) -> None:
        action = yaml.load(
            (ROOT / "actions/agent-roots-conformance/action.yml").read_text(encoding="utf-8"),
            Loader=yaml.BaseLoader,
        )
        step = action["runs"]["steps"][0]
        self.assertNotIn("${{", step["run"])
        self.assertEqual(step["env"]["TARGET_BINARY"], "${{ inputs.binary }}")
        self.assertEqual(step["env"]["SUBCOMMAND"], "${{ inputs.subcommand }}")
        self.assertEqual(step["env"]["EXTRA_ARGS"], "${{ inputs.extra-args }}")
        self.assertEqual(step["env"]["CONNECT_FLAG"], "${{ inputs.connect-flag }}")
        self.assertEqual(
            step["env"]["CONNECT_ADDRESS_FORMAT"], "${{ inputs.connect-address-format }}"
        )
        self.assertEqual(
            step["env"]["CONNECT_EXTRA_ARGS"], "${{ inputs.connect-extra-args }}"
        )
        self.assertEqual(
            step["env"]["TIMEOUT_SECONDS"], "${{ inputs.timeout-seconds }}"
        )

    def test_reusable_workflow_pins_actions_and_invokes_composite(self) -> None:
        workflow = yaml.load(
            (ROOT / ".github/workflows/agent-roots-conformance.yml").read_text(
                encoding="utf-8"
            ),
            Loader=yaml.BaseLoader,
        )
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        job = workflow["jobs"]["conformance"]
        self.assertIn("timeout-minutes", job)
        harden, download, prepare, check = job["steps"]
        self.assertEqual(
            harden["uses"],
            "step-security/harden-runner@1d88384fd173af6ecb012dfd0264a306b711241a",
        )
        self.assertEqual(
            download["uses"],
            "actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333",
        )
        self.assertNotIn("${{", prepare["run"])
        self.assertEqual(prepare["env"]["BINARY_PATH"], "${{ inputs.binary-path }}")
        self.assertEqual(
            check["uses"],
            "storytold/craft-common-actions/actions/agent-roots-conformance@main",
        )
        self.assertEqual(check["with"]["connect-flag"], "${{ inputs.connect-flag }}")

    def test_pr_test_workflow_pins_actions_and_runs_suite(self) -> None:
        workflow = yaml.load(
            (ROOT / ".github/workflows/test-agent-roots-conformance.yml").read_text(
                encoding="utf-8"
            ),
            Loader=yaml.BaseLoader,
        )
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        job = workflow["jobs"]["tests"]
        self.assertIn("timeout-minutes", job)
        harden, checkout, run_step = job["steps"]
        self.assertEqual(
            harden["uses"],
            "step-security/harden-runner@1d88384fd173af6ecb012dfd0264a306b711241a",
        )
        self.assertEqual(
            checkout["uses"],
            "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
        )
        self.assertEqual(checkout["with"]["persist-credentials"], "false")
        self.assertIn("tests/test_agent_roots_conformance.py", run_step["run"])


if __name__ == "__main__":
    unittest.main()

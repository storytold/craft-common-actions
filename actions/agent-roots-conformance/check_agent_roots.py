#!/usr/bin/env python3
"""Verify Rule 18 startup confinement for a Craft CLI MCP entry point.

Runs a built ``<app>-cli`` binary against temporary directories and, when a
connected-mode flag is supplied, an ephemeral loopback control-channel stub
that advertises no automation roots.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import selectors
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import NamedTuple, Sequence


INIT_REQUEST = (
    json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {
                    "name": "craft-agent-roots-conformance",
                    "version": "1",
                },
            },
        },
        separators=(",", ":"),
    )
    + "\n"
)

MAX_STDERR_BYTES = 8192

STRIPPED_ENV_SUFFIXES = (
    "_AUTOMATION_READ_ROOT",
    "_AUTOMATION_WRITE_ROOT",
    "_CONTROL_PORT",
    "_CONTROL_TOKEN",
)
STRIPPED_ENV_EXACT = {
    "AUTOMATION_READ_ROOT",
    "AUTOMATION_WRITE_ROOT",
    "CRAFT_AUTOMATION_READ_ROOT",
    "CRAFT_AUTOMATION_WRITE_ROOT",
}


class CaseResult(NamedTuple):
    name: str
    passed: bool
    detail: str


class UnconfinedControlStub:
    """Loopback TCP server simulating an unconfined desktop control channel.

    Accepts connections and responds to newline-delimited JSON requests with a
    generic success reply containing no root confinement metadata.
    """

    def __init__(self) -> None:
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._clients: list[socket.socket] = []
        self._lock = threading.Lock()
        self.port: int = 0
        self.connections: int = 0

    def __enter__(self) -> "UnconfinedControlStub":
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(8)
        sock.settimeout(0.1)
        self._sock = sock
        self.port = sock.getsockname()[1]
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        with self._lock:
            clients = list(self._clients)
            self._clients.clear()
        for client in clients:
            try:
                client.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _accept_loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                conn, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._lock:
                self.connections += 1
                self._clients.append(conn)
            threading.Thread(
                target=self._handle_client,
                args=(conn,),
                daemon=True,
            ).start()

    def _handle_client(self, conn: socket.socket) -> None:
        conn.settimeout(0.2)
        buf = b""
        try:
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    raw_line, buf = buf.split(b"\n", 1)
                    req_id: object = 1
                    try:
                        parsed = json.loads(raw_line.decode("utf-8", errors="replace"))
                        if isinstance(parsed, dict) and "id" in parsed:
                            req_id = parsed["id"]
                    except json.JSONDecodeError:
                        pass
                    reply = json.dumps({"id": req_id, "ok": True, "result": {}}) + "\n"
                    try:
                        conn.sendall(reply.encode("utf-8"))
                    except OSError:
                        return
        finally:
            try:
                conn.close()
            except OSError:
                pass


def sanitized_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Return a copy of the environment without ambient automation-root or control vars."""
    source = os.environ if base is None else base
    clean: dict[str, str] = {}
    for key, value in source.items():
        upper = key.upper()
        if upper in STRIPPED_ENV_EXACT or upper.endswith(STRIPPED_ENV_SUFFIXES):
            continue
        clean[key] = value
    return clean


def _parse_json_object(line: str) -> dict[str, object] | None:
    stripped = line.strip()
    if not stripped.startswith("{"):
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _is_mcp_jsonrpc_message(obj: dict[str, object]) -> bool:
    return obj.get("jsonrpc") == "2.0" and (
        "result" in obj or "error" in obj or "method" in obj
    )


def _signal_group(pid: int, sig: int) -> None:
    if os.name != "posix":
        return
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _terminate_process(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        _signal_group(proc.pid, signal.SIGTERM)
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            _signal_group(proc.pid, signal.SIGKILL)
            try:
                proc.kill()
                proc.wait(timeout=0.5)
            except (OSError, subprocess.TimeoutExpired):
                pass
    else:
        # Clean up any descendants still in the child's process group.
        _signal_group(proc.pid, signal.SIGTERM)


def _send_initialize(proc: subprocess.Popen[bytes]) -> None:
    if proc.stdin is None:
        return
    try:
        proc.stdin.write(INIT_REQUEST.encode("utf-8"))
        proc.stdin.flush()
    except OSError:
        # Expected when a compliant binary exits before reading stdin.
        pass


def _close_stdin(proc: subprocess.Popen[bytes]) -> None:
    if proc.stdin is None:
        return
    try:
        proc.stdin.close()
    except OSError:
        pass


def _close_pipes(proc: subprocess.Popen[bytes]) -> None:
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is None:
            continue
        try:
            stream.close()
        except OSError:
            pass


def _stderr_snippet(chunks: Sequence[bytes]) -> str:
    text = b"".join(chunks).decode("utf-8", errors="replace").strip()
    if not text:
        return "(no stderr)"
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    snippet = " | ".join(lines[:3])
    return snippet[:240]


class _PipeCollector:
    """Deadline-bounded, lock-free non-blocking reader for child stdout/stderr."""

    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        assert proc.stdout is not None and proc.stderr is not None
        self._stdout_fd = proc.stdout.fileno()
        self._stderr_fd = proc.stderr.fileno()
        os.set_blocking(self._stdout_fd, False)
        os.set_blocking(self._stderr_fd, False)
        self._sel = selectors.DefaultSelector()
        self._sel.register(self._stdout_fd, selectors.EVENT_READ, "stdout")
        self._sel.register(self._stderr_fd, selectors.EVENT_READ, "stderr")
        self._stdout_buf = b""
        self.stdout_lines: list[str] = []
        self.jsonrpc_msgs: list[dict[str, object]] = []
        self.stderr_chunks: list[bytes] = []
        self._stderr_kept = 0

    def close(self) -> None:
        self._sel.close()

    def _consume_stdout_line(self, raw_line: bytes) -> None:
        text = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        self.stdout_lines.append(text)
        obj = _parse_json_object(text)
        if obj is not None and _is_mcp_jsonrpc_message(obj):
            self.jsonrpc_msgs.append(obj)

    def poll_once(self, wait_seconds: float) -> None:
        if not self._sel.get_map():
            if wait_seconds > 0:
                time.sleep(wait_seconds)
            return
        events = self._sel.select(timeout=max(0.0, wait_seconds))
        for key, _mask in events:
            fd = key.fd
            tag = key.data
            try:
                chunk = os.read(fd, 4096)
            except BlockingIOError:
                continue
            except OSError:
                self._sel.unregister(fd)
                continue
            if not chunk:
                self._sel.unregister(fd)
                if tag == "stdout" and self._stdout_buf:
                    self._consume_stdout_line(self._stdout_buf)
                    self._stdout_buf = b""
                continue
            if tag == "stdout":
                self._stdout_buf += chunk
                while b"\n" in self._stdout_buf:
                    raw_line, self._stdout_buf = self._stdout_buf.split(b"\n", 1)
                    self._consume_stdout_line(raw_line)
            else:
                remaining = MAX_STDERR_BYTES - self._stderr_kept
                if remaining > 0:
                    kept = chunk[:remaining]
                    self.stderr_chunks.append(kept)
                    self._stderr_kept += len(kept)

    def drain_briefly(self, budget_seconds: float = 0.05) -> None:
        """Drain any already-buffered bytes after exit without hanging on inherited pipes."""
        end = time.monotonic() + budget_seconds
        while self._sel.get_map() and time.monotonic() < end:
            before_out = len(self.stdout_lines)
            before_err = self._stderr_kept
            self.poll_once(min(0.01, max(0.0, end - time.monotonic())))
            if len(self.stdout_lines) == before_out and self._stderr_kept == before_err:
                break
        if self._stdout_buf:
            self._consume_stdout_line(self._stdout_buf)
            self._stdout_buf = b""


def run_negative_case(
    name: str,
    cmd: Sequence[str],
    timeout: float,
    env: dict[str, str],
) -> CaseResult:
    """Require the CLI to exit non-zero before answering MCP initialize."""
    proc = subprocess.Popen(
        list(cmd),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        start_new_session=(os.name == "posix"),
    )
    collector = _PipeCollector(proc)
    timed_out = False
    answered = False
    try:
        _send_initialize(proc)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if collector.jsonrpc_msgs:
                answered = True
                break
            if proc.poll() is not None:
                collector.drain_briefly()
                if collector.jsonrpc_msgs:
                    answered = True
                break
            if remaining <= 0:
                timed_out = True
                break
            collector.poll_once(min(0.02, remaining))
    finally:
        collector.close()
        _close_stdin(proc)
        _terminate_process(proc)
        _close_pipes(proc)
        returncode = proc.returncode

    if answered or collector.jsonrpc_msgs:
        first = (
            json.dumps(collector.jsonrpc_msgs[0])
            if collector.jsonrpc_msgs
            else (collector.stdout_lines[0] if collector.stdout_lines else "")
        )
        return CaseResult(
            name=name,
            passed=False,
            detail=f"answered MCP initialize instead of refusing startup (stdout: {first[:200]})",
        )
    if timed_out:
        return CaseResult(
            name=name,
            passed=False,
            detail=f"did not refuse startup within {timeout:.1f}s (stderr: {_stderr_snippet(collector.stderr_chunks)})",
        )
    if returncode == 0:
        return CaseResult(
            name=name,
            passed=False,
            detail=f"exited 0 instead of non-zero refusal (stderr: {_stderr_snippet(collector.stderr_chunks)})",
        )
    return CaseResult(
        name=name,
        passed=True,
        detail=f"refused startup (exit {returncode})",
    )


def run_positive_initialize_case(
    name: str,
    cmd: Sequence[str],
    timeout: float,
    env: dict[str, str],
) -> CaseResult:
    """Require the CLI to accept both valid roots and answer MCP initialize."""
    proc = subprocess.Popen(
        list(cmd),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        start_new_session=(os.name == "posix"),
    )
    collector = _PipeCollector(proc)
    reply: dict[str, object] | None = None
    try:
        _send_initialize(proc)
        deadline = time.monotonic() + timeout
        while True:
            for msg in collector.jsonrpc_msgs:
                if msg.get("id") == 1:
                    reply = msg
                    break
            if reply is not None:
                break
            if proc.poll() is not None:
                collector.drain_briefly()
                for msg in collector.jsonrpc_msgs:
                    if msg.get("id") == 1:
                        reply = msg
                        break
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            collector.poll_once(min(0.02, remaining))
    finally:
        collector.close()
        _close_stdin(proc)
        if proc.poll() is None:
            try:
                proc.wait(timeout=min(timeout, 1.0))
            except subprocess.TimeoutExpired:
                pass
        _terminate_process(proc)
        _close_pipes(proc)
        rc = proc.returncode

    if reply is None:
        return CaseResult(
            name=name,
            passed=False,
            detail=(
                f"did not answer MCP initialize with both valid roots "
                f"(exit {rc}, stderr: {_stderr_snippet(collector.stderr_chunks)})"
            ),
        )
    if "error" in reply or "result" not in reply:
        return CaseResult(
            name=name,
            passed=False,
            detail=f"initialize failed: {json.dumps(reply)[:200]}",
        )
    return CaseResult(
        name=name,
        passed=True,
        detail="answered MCP initialize with both valid roots",
    )


def run_conformance(
    binary: Path,
    *,
    subcommand: Sequence[str] = ("mcp",),
    extra_args: Sequence[str] = (),
    connect_flag: str = "",
    connect_address_format: str = "host:port",
    connect_extra_args: Sequence[str] = (),
    timeout: float = 5.0,
    env: dict[str, str] | None = None,
) -> list[CaseResult]:
    """Run all Rule 18 startup confinement cases against ``binary``."""
    resolved_binary = binary.resolve()
    if not resolved_binary.is_file():
        raise FileNotFoundError(f"binary not found: {binary}")
    if not os.access(resolved_binary, os.X_OK):
        raise PermissionError(f"binary is not executable: {binary}")
    if timeout < 0.5 or timeout > 60.0:
        raise ValueError(f"timeout must be between 0.5 and 60.0 seconds, got {timeout}")
    if connect_address_format not in ("host:port", "port"):
        raise ValueError(
            f"connect_address_format must be 'host:port' or 'port', got {connect_address_format!r}"
        )

    child_env = sanitized_env(env)
    base_cmd = [str(resolved_binary), *subcommand]
    headless_base = [*base_cmd, *extra_args]
    results: list[CaseResult] = []

    with tempfile.TemporaryDirectory(prefix="craft-agent-roots-") as tmp:
        root = Path(tmp)
        read_dir = root / "read"
        write_dir = root / "write"
        missing_read = root / "missing-read"
        missing_write = root / "missing-write"
        read_dir.mkdir()
        write_dir.mkdir()

        results.append(
            run_negative_case("no-roots", headless_base, timeout, child_env)
        )
        results.append(
            run_negative_case(
                "read-root-only",
                [*headless_base, "--automation-read-root", str(read_dir)],
                timeout,
                child_env,
            )
        )
        results.append(
            run_negative_case(
                "write-root-only",
                [*headless_base, "--automation-write-root", str(write_dir)],
                timeout,
                child_env,
            )
        )
        results.append(
            run_negative_case(
                "missing-read-root",
                [
                    *headless_base,
                    "--automation-read-root",
                    str(missing_read),
                    "--automation-write-root",
                    str(write_dir),
                ],
                timeout,
                child_env,
            )
        )
        results.append(
            run_negative_case(
                "missing-write-root",
                [
                    *headless_base,
                    "--automation-read-root",
                    str(read_dir),
                    "--automation-write-root",
                    str(missing_write),
                ],
                timeout,
                child_env,
            )
        )
        results.append(
            run_positive_initialize_case(
                "both-roots-initialize",
                [
                    *headless_base,
                    "--automation-read-root",
                    str(read_dir),
                    "--automation-write-root",
                    str(write_dir),
                ],
                timeout,
                child_env,
            )
        )

        if connect_flag.strip():
            with UnconfinedControlStub() as stub:
                endpoint = (
                    f"127.0.0.1:{stub.port}"
                    if connect_address_format == "host:port"
                    else str(stub.port)
                )
                connect_cmd = [
                    *base_cmd,
                    connect_flag.strip(),
                    endpoint,
                    *connect_extra_args,
                ]
                results.append(
                    run_negative_case(
                        "connect-unconfined-refused",
                        connect_cmd,
                        timeout,
                        child_env,
                    )
                )

    return results


FLAG_VALUE_OPTIONS = frozenset(
    {"--extra-args", "--connect-flag", "--connect-extra-args"}
)


def _normalize_flag_values(argv: Sequence[str]) -> list[str]:
    """Rewrite ``--opt --flag`` to ``--opt=--flag`` so argparse accepts leading dashes."""
    out: list[str] = []
    i = 0
    items = list(argv)
    while i < len(items):
        token = items[i]
        if token in FLAG_VALUE_OPTIONS and i + 1 < len(items):
            out.append(f"{token}={items[i + 1]}")
            i += 2
        else:
            out.append(token)
            i += 1
    return out


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify Rule 18 startup confinement for a Craft CLI MCP entry point."
    )
    parser.add_argument("--binary", required=True, help="Path to the built <app>-cli binary")
    parser.add_argument(
        "--subcommand",
        default="mcp",
        help="Subcommand tokens (default: 'mcp')",
    )
    parser.add_argument(
        "--extra-args",
        default="",
        help="Optional extra flags passed for headless cases (e.g. '--headless')",
    )
    parser.add_argument(
        "--connect-flag",
        default="",
        help="Connected-mode flag to probe against an unconfined loopback stub (e.g. '--connect', '--bridge', '--attach')",
    )
    parser.add_argument(
        "--connect-address-format",
        choices=("host:port", "port"),
        default="host:port",
        help="Format of the stub endpoint argument passed after --connect-flag (default: host:port)",
    )
    parser.add_argument(
        "--connect-extra-args",
        default="",
        help="Optional extra flags passed for the connected-mode refusal case",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="Per-case timeout in seconds (0.5 to 60, default: 5)",
    )
    raw = sys.argv[1:] if argv is None else argv
    return parser.parse_args(_normalize_flag_values(raw))


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        results = run_conformance(
            Path(args.binary),
            subcommand=shlex.split(args.subcommand),
            extra_args=shlex.split(args.extra_args),
            connect_flag=args.connect_flag,
            connect_address_format=args.connect_address_format,
            connect_extra_args=shlex.split(args.connect_extra_args),
            timeout=args.timeout,
        )
    except (OSError, ValueError) as exc:
        print(f"agent-roots-conformance: error: {exc}", file=sys.stderr)
        return 1

    failed = 0
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        stream = sys.stdout if result.passed else sys.stderr
        print(f"[{status}] {result.name}: {result.detail}", file=stream)
        if not result.passed:
            failed += 1

    if failed:
        print(
            f"agent-roots-conformance: {failed}/{len(results)} check(s) failed",
            file=sys.stderr,
        )
        return 1
    print(f"agent-roots-conformance: {len(results)}/{len(results)} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Prototype: bounded process isolation for ONE prebuilt libtest executable.

Not a workspace gate; doctests and complete executable inventory belong to caller.
Process-group cleanup is NOT a sandbox: descendants that create a new session
can escape. No production adoption until application subprocess behavior is assessed.
"""
import concurrent.futures
import hashlib
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import selectors
import time


MAX_OUTPUT = 2_000_000
MAX_TESTS = 20_000


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def invoke(args, seconds):
    if os.name != 'posix':
        raise ValueError('POSIX process groups required')
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               start_new_session=True)
    deadline, output, timed_out = time.monotonic() + seconds, bytearray(), False
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                # Reclaim descendants even if a test parent exits successfully.
                if process.poll() is not None:
                    kill_group(process.pid)
                for key, _ in selector.select(min(remaining, .05)):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        break
                    output.extend(chunk)
                    if len(output) > MAX_OUTPUT:
                        raise ValueError('output exceeds 2 MB')
            if not timed_out:
                try:
                    process.wait(timeout=max(0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    timed_out = True
    finally:
        kill_group(process.pid)
        try:
            process.wait(timeout=2)
        finally:
            process.stdout.close()
    return process.returncode, timed_out, output.decode('utf-8', errors='replace')


def kill_group(pid):
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def inventory(binary, ignored=False):
    code, timed_out, output = invoke(
        [str(binary), '--list', '--format', 'pretty', '--color', 'never'] + (['--ignored'] if ignored else []), 30)
    if code or timed_out:
        raise ValueError('discovery failed')
    names, summary = [], None
    for line in output.splitlines():
        if not line:
            continue
        if line.endswith(': test'):
            names.append(line[:-6])
        elif re.fullmatch(r'\d+ tests?, \d+ benchmarks?', line) and summary is None:
            summary = tuple(map(int, re.findall(r'\d+', line)))
        else:
            raise ValueError('unsupported discovery output')
    if summary != (len(names), 0) or len(set(names)) != len(names) or len(names) > MAX_TESTS:
        raise ValueError('invalid inventory')
    return sorted(names)


def run(binary, workers=1, test_seconds=60, phase_seconds=600):
    # phase_seconds bounds execution only; discovery adds at most 60 seconds,
    # plus regular-file hashing and up to two seconds process-group cleanup.
    if not 1 <= workers <= 4 or not 0 < test_seconds <= 300 or not 0 < phase_seconds <= 1200:
        raise ValueError('invalid bounds')
    binary = Path(binary).resolve(strict=True)
    if not stat.S_ISREG(binary.stat().st_mode) or binary.stat().st_size > 2_000_000_000:
        raise ValueError('expected a regular executable no larger than 2 GB')
    digest = file_sha256(binary)
    all_tests, ignored = inventory(binary), inventory(binary, True)
    if not all_tests or not set(ignored).issubset(all_tests):
        raise ValueError('empty or inconsistent inventory')
    expected = sorted(set(all_tests) - set(ignored))
    if not expected:
        raise ValueError('no runnable tests')
    deadline = time.monotonic() + phase_seconds

    def one(name):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {'name': name, 'status': 'not_run'}
        try:
            code, timeout, output = invoke(
                [str(binary), '--exact', name, '--test-threads=1', '--format', 'pretty', '--color', 'never'], min(test_seconds, remaining))
            passed = (not code and not timeout and
                      re.search(r'test result: ok\. 1 passed; 0 failed; 0 ignored;', output) is not None)
            return {'name': name, 'status': 'passed' if passed else 'failed',
                    'exit_code': code, 'timeout': timeout, 'output': output[-4000:]}
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            return {'name': name, 'status': 'failed', 'error': str(error)}

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(one, expected))
    unchanged = file_sha256(binary) == digest
    return {'binary_sha256': digest, 'binary_unchanged': unchanged,
            'all_tests': all_tests, 'ignored': ignored, 'expected': expected, 'results': results,
            'success': unchanged and [r['name'] for r in results] == expected and
                       all(r['status'] == 'passed' for r in results)}

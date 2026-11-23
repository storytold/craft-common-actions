"""Remove only our direct private directory and positively identified server."""
import os
from pathlib import Path
import re
import shutil
import signal
import time


def cleanup():
    raw = os.environ.get("ARTCRAFT_MYSQL_ROOT", "")
    if not raw:
        return
    root = Path(raw)
    base = Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)
    if (not root.is_absolute() or root.parent != base
            or not re.fullmatch(r"artcraft-mysql\.[A-Za-z0-9]{6}", root.name)
            or root.is_symlink()):
        raise RuntimeError("Invalid fixture directory")
    if not root.exists():
        return
    if root.resolve(strict=True) != root or not root.is_dir() or root.stat().st_uid != os.getuid():
        raise RuntimeError("Invalid fixture ownership or canonical path")
    pidfile = root / "mysql.pid"
    if pidfile.is_symlink():
        raise RuntimeError("Invalid PID file")
    if pidfile.exists():
        value = pidfile.read_text().strip()
        if not value.isdecimal() or int(value) <= 1:
            raise RuntimeError("Invalid MySQL PID")
        pid = int(value)
        proc = Path(f"/proc/{pid}")
        if proc.exists():
            token = root / "process-start"
            args = (proc / "cmdline").read_bytes().split(b"\0")
            current_start = (proc / "stat").read_text().rsplit(")", 1)[1].split()[19]
            if (not token.is_file() or token.is_symlink()
                    or token.read_text().strip() != current_start
                    or proc.stat().st_uid != os.getuid()
                    or (proc / "exe").resolve().name != "mysqld"
                    or f"--datadir={root}/data".encode() not in args
                    or b"--skip-networking" not in args):
                raise RuntimeError("Refusing to signal an unverified MySQL process")
            os.kill(pid, signal.SIGTERM)
            for _ in range(300):
                if not proc.exists():
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("MySQL did not stop; preserving fixture")
    shutil.rmtree(root)


if __name__ == "__main__":
    cleanup()

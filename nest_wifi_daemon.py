"""Start and stop the detached Nest Wifi polling process."""

from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import sys
from pathlib import Path

from nest_wifi_keychain import MonitorError


def _watcher_pid(data_dir: Path) -> int | None:
    with (data_dir / "runtime" / "watch.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return None
        except BlockingIOError:
            lock.seek(0)
            pid = lock.read().strip()
            if not pid.isdigit():
                raise MonitorError("Collector PID unavailable; retry shortly.") from None
            return int(pid)


def start(data_dir: Path, local_only: bool) -> None:
    if _watcher_pid(data_dir) is not None:
        print("Collector is already running.")
        return
    runtime = data_dir / "runtime"
    with (
        (runtime / "collector.stdout.log").open("a") as out,
        (runtime / "collector.stderr.log").open("a") as err,
    ):
        command = [
            sys.executable,
            "-B",
            str(Path(__file__).with_name("nest_wifi_monitor.py").resolve()),
            "--data-dir",
            str(data_dir),
            "watch",
        ]
        if local_only:
            command.append("--local-only")
        child = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            start_new_session=True,
            close_fds=True,
        )
    print(f"Collector started (PID {child.pid}).")


def stop(data_dir: Path) -> None:
    pid = _watcher_pid(data_dir)
    if pid is None:
        print("Collector is not running.")
        return
    os.kill(pid, signal.SIGTERM)
    print("Collector stopped.")

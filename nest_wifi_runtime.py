"""Serialize snapshots and run the background polling loop."""

from __future__ import annotations

import fcntl
import os
import sys
import time
from pathlib import Path

from nest_wifi_collector import collect
from nest_wifi_keychain import MonitorError


def _locked_snapshot(data_dir: Path, local_only: bool) -> str:
    with (data_dir / "runtime" / "collector.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise MonitorError("Another snapshot is already running.") from None
        return collect(data_dir, local_only=local_only)


def watch(data_dir: Path, local_only: bool) -> None:
    with (data_dir / "runtime" / "watch.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise MonitorError("Collector is already running.") from None
        lock.seek(0)
        lock.truncate()
        lock.write(str(os.getpid()))
        lock.flush()
        while True:
            try:
                _locked_snapshot(data_dir, local_only)
            except (MonitorError, OSError) as error:
                message = (
                    str(error) if isinstance(error, MonitorError) else "Local storage unavailable."
                )
                print(message, file=sys.stderr, flush=True)
            sys.stdout.flush()
            time.sleep(300)

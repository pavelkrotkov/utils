"""Exercise the guard with real, bounded worker/descendant processes.

Only reported physical RAM is replaced; RSS reads and signal delivery are real.
The 80 MiB child crosses a 64 MiB budget without stressing the test machine.
Cleanup markers verify interruption reaches finally; a second run checks lock
release, including after cancelling a live job with a descendant.
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def guarded_worker(tmp_path, request):
    script = tmp_path / "worker.py"
    script.write_text(
        """import os, subprocess, sys, time
from pathlib import Path
from types import SimpleNamespace
import psutil
from audio_resources import supervise
psutil.virtual_memory = lambda: SimpleNamespace(total=128*1024**2, available=100*1024**2)
def main():
    assert os.environ["OMP_NUM_THREADS"] != "64"
    size = 80*1024**2 if sys.argv[1] == "memory" else 1
    child = subprocess.Popen([sys.executable, "-c", f"import time; data=bytearray({size}); time.sleep(30)"])
    Path("pid").write_text(str(child.pid))
    try:
        if sys.argv[1] == "success":
            child.terminate()
            child.wait()
            return
        time.sleep(30)
    finally:
        Path("cleaned").touch()
if __name__ == "__main__":
    supervise(main)
"""
    )
    env = dict(os.environ, PYTHONPATH=str(ROOT), TMPDIR=str(tmp_path), OMP_NUM_THREADS="64")
    command = [sys.executable, str(script), request.param]
    worker = subprocess.Popen(
        command, cwd=tmp_path, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        yield worker, command, env
        assert (tmp_path / "cleaned").exists()
        child_pid = int((tmp_path / "pid").read_text())
        assert (
            not psutil.pid_exists(child_pid)
            or psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE
        )
    finally:
        if worker.poll() is None:
            worker.send_signal(signal.SIGTERM)
            worker.communicate(timeout=5)


def wait_for_pid(path):
    deadline = time.monotonic() + 10
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert path.exists()


@pytest.mark.parametrize("guarded_worker", ["memory", "cancel", "success"], indirect=True)
def test_resource_guard_stops_descendants_and_releases_lock(tmp_path, guarded_worker):
    worker, command, env = guarded_worker
    action = command[-1]
    wait_for_pid(tmp_path / "pid")
    if action == "cancel":
        second = subprocess.run(
            command, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=5
        )
        assert second.returncode != 0
        assert "already running" in second.stderr
        worker.send_signal(signal.SIGTERM)
    _, stderr = worker.communicate(timeout=10)
    assert worker.returncode == {"memory": 1, "cancel": 130, "success": 0}[action], stderr
    assert {"memory": "memory budget reached", "cancel": "", "success": ""}[action] in stderr
    rerun = subprocess.run(
        [*command[:2], "success"], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10
    )
    assert rerun.returncode == 0, rerun.stderr

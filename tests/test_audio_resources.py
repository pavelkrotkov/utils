"""Exercise the guard with real, bounded worker/descendant processes.

Only reported physical RAM is replaced; RSS reads and signal delivery are real.
The 80 MiB child crosses a 64 MiB budget without stressing the test machine.
Cleanup markers verify interruption reaches finally; a second run checks lock
release, including after cancelling a live job with a descendant.
"""

import ctypes
import errno
import io
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

import audio_resources as resources

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def guarded_worker(tmp_path, request):
    script = tmp_path / "worker.py"
    script.write_text(
        """import os, subprocess, sys, time
from pathlib import Path
from types import SimpleNamespace
import psutil
import audio_resources
from audio_resources import supervise
audio_resources.macos_memory = lambda: None  # exercise the portable RSS/reserve policy
psutil.virtual_memory = lambda: SimpleNamespace(total=128*1024**2, available=100*1024**2)
def denied_memory(process):
    raise psutil.AccessDenied(process.pid)
if sys.argv[1] == "inspection-error":
    psutil.Process.memory_info = denied_memory
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


@pytest.mark.parametrize(
    "guarded_worker", ["memory", "cancel", "success", "inspection-error"], indirect=True
)
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
    assert (
        worker.returncode
        == {"memory": 1, "cancel": 130, "success": 0, "inspection-error": 1}[action]
    ), stderr
    assert {
        "memory": "memory budget reached",
        "cancel": "",
        "success": "",
        "inspection-error": "Cannot enforce transcription resource guard",
    }[action] in stderr
    rerun = subprocess.run(
        [*command[:2], "success"], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10
    )
    assert rerun.returncode == 0, rerun.stderr


def test_pressure_window_allows_recovery_but_bounds_repeated_warnings():
    from audio_resources import PressureWindow

    window = PressureWindow()
    assert not window.exceeded(0, 1)
    assert not window.exceeded(1, 2)
    assert not window.exceeded(3, 1)  # two seconds of warning, then recovery
    assert not window.exceeded(4, 2)
    assert window.exceeded(7, 2)  # five seconds across both warning episodes
    assert not window.exceeded(7.1, 1)
    assert not window.exceeded(20, 1)  # old exposure has expired


def test_pressure_warning_recovery_at_grace_boundary_does_not_stop():
    window = resources.PressureWindow()
    assert not window.exceeded(0, 2)
    assert not window.exceeded(5, 1)


def test_pressure_warning_window_cannot_override_critical_pressure():
    window = resources.PressureWindow()
    assert not window.exceeded(0, 2)
    assert not window.exceeded(6, 4)


@pytest.fixture
def fake_mac(monkeypatch):
    mac = resources.MacMemory.__new__(resources.MacMemory)
    monkeypatch.setattr(mac, "pressure", lambda: 1)
    monkeypatch.setattr(mac, "footprint", lambda pid: 32)
    return mac


def test_macos_low_available_memory_does_not_stop_healthy_worker(monkeypatch, fake_mac):
    import audio_resources as resources

    class Worker(subprocess.Popen):
        pid = 123
        calls = 0

        def wait(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("worker", timeout or 0.0)
            return 0

    process = SimpleNamespace(pid=123, children=lambda **kw: [])
    monkeypatch.setattr(psutil, "Process", lambda pid: process)
    monkeypatch.setattr(psutil, "virtual_memory", lambda: SimpleNamespace(available=1))
    assert resources.wait_for_worker(Worker.__new__(Worker), 64, 16, mac=fake_mac) == 0


@pytest.mark.parametrize("pressure,footprint", [(4, 32), (1, 65), (4, 65)])
def test_macos_emergency_condition_fails_monitor(
    monkeypatch, fake_mac, capsys, pressure, footprint
):
    import audio_resources as resources

    class Worker(subprocess.Popen):
        pid = 123

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("worker", timeout or 0.0)

    process = SimpleNamespace(pid=123, children=lambda **kw: [])
    monkeypatch.setattr(psutil, "Process", lambda pid: process)
    monkeypatch.setattr(psutil, "virtual_memory", lambda: SimpleNamespace(available=1))
    monkeypatch.setattr(fake_mac, "pressure", lambda: pressure)
    monkeypatch.setattr(fake_mac, "footprint", lambda pid: footprint)
    assert resources.wait_for_worker(Worker.__new__(Worker), 64, 16, mac=fake_mac) == 1
    expected = "critical macOS memory pressure" if pressure == 4 else "memory budget reached"
    assert expected in capsys.readouterr().err


@pytest.mark.parametrize(
    "outcome,expected_grace", [(1, 0.1), (OSError, 0.1), (KeyboardInterrupt, 2.0)]
)
def test_macos_cleanup_runs_once_with_correct_grace(
    tmp_path, monkeypatch, fake_mac, outcome, expected_grace
):
    worker = SimpleNamespace(pid=123)
    stopped = []

    def wait(*args, **kwargs):
        if isinstance(outcome, type):
            raise outcome()
        return outcome

    monkeypatch.setattr(resources, "macos_memory", lambda: fake_mac)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: worker)
    monkeypatch.setattr(psutil, "Process", lambda pid: SimpleNamespace(nice=lambda n: None))
    monkeypatch.setattr(resources, "wait_for_worker", wait)
    monkeypatch.setattr(signal, "pthread_sigmask", lambda *args: set())
    monkeypatch.setattr(resources, "stop_worker", lambda worker, grace: stopped.append(grace))
    with (tmp_path / "lock").open("w") as lock:
        assert resources.run_worker(lock) == (130 if outcome is KeyboardInterrupt else 1)
    assert stopped == [expected_grace]


@pytest.mark.parametrize("pressure", [2, 4])
def test_macos_start_defers_under_elevated_pressure(monkeypatch, fake_mac, pressure):
    monkeypatch.setattr(resources, "macos_memory", lambda: fake_mac)
    monkeypatch.setattr(fake_mac, "pressure", lambda: pressure)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("must not start worker"))
    assert resources.run_worker(io.StringIO()) == 1


def test_macos_sensor_error_is_not_normal_pressure(fake_mac):
    def denied(*args):
        ctypes.set_errno(errno.EPERM)
        return -1

    fake_mac.libc = SimpleNamespace(sysctlbyname=denied)
    with pytest.raises(OSError, match="Cannot read macOS memory pressure"):
        resources.MacMemory.pressure(fake_mac)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS memory ABI")
def test_native_macos_footprint_and_pressure():
    mac = resources.MacMemory()
    assert mac.pressure() in (1, 2, 4)
    before = mac.footprint(os.getpid())
    data = bytearray(16 * 1024**2)
    assert mac.footprint(os.getpid()) >= before + 12 * 1024**2
    assert mac.footprint(2**30) == 0
    assert data[0] == 0

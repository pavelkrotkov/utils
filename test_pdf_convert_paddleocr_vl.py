import subprocess
import sys
import time
from unittest import mock

import pytest

import pdf_convert_paddleocr_vl as paddle


def test_interrupt_cleans_up_worker(tmp_path, monkeypatch):
    worker = mock.Mock()
    worker.wait.side_effect = KeyboardInterrupt
    worker.returncode = None
    cleanup = mock.Mock()
    monkeypatch.setattr(paddle, "LOCK_PATH", tmp_path / "lock")
    monkeypatch.setattr(paddle.subprocess, "Popen", mock.Mock(return_value=worker))
    monkeypatch.setattr(paddle, "_terminate_process_group", cleanup)

    assert paddle._supervise(["input.pdf"]) == 130
    cleanup.assert_called_once_with(worker)


def test_second_run_stops_before_spawning_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(paddle, "LOCK_PATH", tmp_path / "lock")
    popen = mock.Mock()
    monkeypatch.setattr(paddle.subprocess, "Popen", popen)
    with paddle._conversion_lock():
        assert paddle._supervise(["input.pdf"]) == 1
    popen.assert_not_called()


def test_help_bypasses_active_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(paddle, "LOCK_PATH", tmp_path / "lock")
    execute = mock.Mock(return_value=0)
    popen = mock.Mock()
    monkeypatch.setattr(paddle, "execute", execute)
    monkeypatch.setattr(paddle.subprocess, "Popen", popen)
    with paddle._conversion_lock():
        assert paddle._supervise(["--help"]) == 0
    execute.assert_called_once()
    popen.assert_not_called()


def test_worker_inherits_lock_fd(tmp_path, monkeypatch):
    worker = mock.Mock(returncode=0)
    worker.wait.return_value = 0
    popen = mock.Mock(return_value=worker)
    monkeypatch.setattr(paddle, "LOCK_PATH", tmp_path / "lock")
    monkeypatch.setattr(paddle.subprocess, "Popen", popen)

    assert paddle._supervise(["input.pdf"]) == 0
    assert len(popen.call_args.kwargs["pass_fds"]) == 1


def test_worker_restores_termination_signals(monkeypatch):
    pthread_sigmask = mock.Mock()
    set_signal = mock.Mock()
    monkeypatch.setattr(paddle.signal, "pthread_sigmask", pthread_sigmask)
    monkeypatch.setattr(paddle.signal, "signal", set_signal)

    paddle._prepare_worker_signals()

    pthread_sigmask.assert_called_once_with(paddle.signal.SIG_UNBLOCK, paddle.TERMINATION_SIGNALS)
    assert set_signal.call_args_list == [
        mock.call(signum, paddle.signal.default_int_handler)
        for signum in paddle.TERMINATION_SIGNALS
    ]


def test_lock_recovers_after_holder_is_killed(tmp_path, monkeypatch):
    lock_path = tmp_path / "lock"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl,sys,time; f=open(sys.argv[1], 'a'); "
            "fcntl.flock(f, fcntl.LOCK_EX); print('locked', flush=True); time.sleep(60)",
            str(lock_path),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"
        monkeypatch.setattr(paddle, "LOCK_PATH", lock_path)
        with pytest.raises(paddle.ConversionError, match="already running"):
            paddle._conversion_lock()
        holder.kill()
        holder.wait()
        with paddle._conversion_lock():
            pass
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait()


def test_process_group_cleanup_reaches_descendants(tmp_path):
    marker = tmp_path / "survived"
    child_code = (
        "import signal,sys,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(0.5); open(sys.argv[1], 'w').write('alive')"
    )
    worker_code = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}, {str(marker)!r}]); "
        "print('ready', flush=True); time.sleep(60)"
    )
    worker = subprocess.Popen(
        [sys.executable, "-c", worker_code],
        stdout=subprocess.PIPE,
        start_new_session=True,
    )
    assert worker.stdout is not None
    assert worker.stdout.readline().strip() == b"ready"

    paddle._terminate_process_group(worker)
    time.sleep(0.7)

    assert worker.poll() is not None
    assert not marker.exists()

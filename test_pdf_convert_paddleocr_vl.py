import argparse
import subprocess
import sys
import time
import weakref
from pathlib import Path
from unittest import mock

import pytest

import pdf_convert_paddleocr_vl as paddle
import pdf_paddleocr_supervisor as supervisor


def _supervise(argv):
    return supervisor.supervise(argv, paddle.PaddleOcrVlBackend(), Path(paddle.__file__).resolve())


def test_safe_resource_defaults_reach_paddle():
    parser = argparse.ArgumentParser()
    backend = paddle.PaddleOcrVlBackend()
    backend.add_arguments(parser)
    args = parser.parse_args([])
    config = {
        "batch_size": 64,
        "SubModules": {
            "LayoutDetection": {"batch_size": 8},
            "VLRecognition": {"batch_size": -1},
        },
    }
    paddlex = mock.Mock()
    paddlex.load_pipeline_config.return_value = config

    assert paddle._pipeline_kwargs(args) == {
        "engine": None,
        "device": None,
        "pipeline_version": "v1.6",
        "cpu_threads": 4,
        "use_queues": False,
    }
    paddle._resource_config(paddlex, args)
    assert (
        config["batch_size"],
        config["SubModules"]["LayoutDetection"]["batch_size"],
        config["SubModules"]["VLRecognition"]["batch_size"],
    ) == (1, 1, 1)


def test_mlx_backend_and_runtime_are_visible(capsys):
    parser = argparse.ArgumentParser()
    backend = paddle.PaddleOcrVlBackend()
    backend.add_arguments(parser)
    args = parser.parse_args(["--mlx-vlm-url", "http://localhost:8111/"])
    paddle_runtime = mock.Mock()
    paddle_runtime.device.is_compiled_with_cuda.return_value = False

    kwargs = paddle._pipeline_kwargs(args)
    paddle._log_runtime(paddle_runtime, args, "cpu")

    assert kwargs["vl_rec_backend"] == "mlx-vlm-server"
    assert kwargs["vl_rec_server_url"] == "http://localhost:8111/"
    assert kwargs["vl_rec_api_model_name"] == "PaddlePaddle/PaddleOCR-VL-1.6"
    log = capsys.readouterr().out
    assert "device=cpu" in log
    assert "engine=paddle" in log
    assert "vlm_backend=mlx-vlm-server" in log
    assert "paddle=cpu-only" in log
    assert "model=PaddleOCR-VL-1.6" in log
    assert "threads=4 batches=1/1/1 queues=off" in log


def test_mlx_model_override_is_forwarded():
    parser = argparse.ArgumentParser()
    backend = paddle.PaddleOcrVlBackend()
    backend.add_arguments(parser)
    args = parser.parse_args(
        [
            "--mlx-vlm-url",
            "http://localhost:8111/",
            "--mlx-vlm-model",
            "/models/PaddleOCR-VL-1.6",
        ]
    )

    assert paddle._pipeline_kwargs(args)["vl_rec_api_model_name"] == "/models/PaddleOCR-VL-1.6"


def test_mlx_model_default_supports_legacy_namespace():
    args = argparse.Namespace(
        engine=None,
        device=None,
        pipeline_version="v1.6",
        threads=4,
        queues=False,
        mlx_vlm_url="http://localhost:8111/",
    )

    assert paddle._pipeline_kwargs(args)["vl_rec_api_model_name"] == "PaddlePaddle/PaddleOCR-VL-1.6"


def test_memory_monitor_samples_only_while_running(monkeypatch, capsys):
    process = mock.Mock()
    process.memory_info.return_value = argparse.Namespace(rss=2**30)
    psutil = mock.Mock()
    psutil.Process.return_value = process
    psutil.virtual_memory.return_value = argparse.Namespace(percent=91.0, available=2**30)
    psutil.NoSuchProcess = RuntimeError
    monkeypatch.setattr(supervisor, "require_module", lambda *_args: psutil)
    args = argparse.Namespace(memory_interval=1, memory_abort_percent=90)

    completed = mock.Mock(pid=41)
    completed.wait.return_value = 0
    assert supervisor.wait_for_worker(completed, args) == 0
    psutil.virtual_memory.assert_not_called()

    worker = mock.Mock(pid=42)
    worker.wait.side_effect = subprocess.TimeoutExpired("worker", 1)
    with pytest.raises(paddle.ConversionError, match="Memory abort threshold reached"):
        supervisor.wait_for_worker(worker, args)

    captured = capsys.readouterr()
    assert "worker_rss=1.00 GiB" in captured.out
    assert "system=91.0% used" in captured.out
    assert "WARNING: System memory reached 91.0%" in captured.err


def test_streams_pages_without_retaining_results(tmp_path, monkeypatch):
    refs = []

    class Image:
        def __init__(self, data):
            self.data = data

        def save(self, path):
            path.write_bytes(self.data)

    class Result:
        def __init__(self, index):
            images = {"figures/first.png": Image(str(index).encode())} if index < 2 else {}
            image_ref = " ![figure](figures/first.png)" if images else ""
            self.markdown = {
                "markdown_texts": f"page {index} $x^2${image_ref}",
                "markdown_images": images,
            }

    def results():
        for index in range(4):
            result = Result(index)
            refs.append(weakref.ref(result))
            yield result
            if index:
                assert refs[index - 1]() is None

    pipeline = mock.Mock()
    pipeline.predict_iter.return_value = results()
    paddleocr = mock.Mock(PaddleOCRVL=mock.Mock(return_value=pipeline))
    monkeypatch.setattr(
        paddle,
        "require_module",
        lambda name, _package: paddleocr if name == "paddleocr" else mock.Mock(),
    )
    monkeypatch.setattr(paddle, "_resource_config", lambda *_args: {})
    args = argparse.Namespace(
        engine=None, device=None, pipeline_version="v1.6", threads=4, queues=False
    )
    request = paddle.ConversionRequest(
        tmp_path / "input.pdf", tmp_path / "out.md", tmp_path / "work", args
    )

    outcome = paddle.PaddleOcrVlBackend().convert(request)

    assert isinstance(outcome, paddle.MarkdownDirectory)
    assert pipeline.predict_iter.call_args == mock.call(input=str(request.pdf_path))
    assert (outcome.directory / "input.md").read_text(encoding="utf-8").split("\n\n") == [
        "page 0 $x^2$ ![figure](page_1/figures/first.png)",
        "page 1 $x^2$ ![figure](page_2/figures/first.png)",
        "page 2 $x^2$",
        "page 3 $x^2$",
        "",
    ]
    assert (outcome.directory / "page_1/figures/first.png").read_bytes() == b"0"
    assert (outcome.directory / "page_2/figures/first.png").read_bytes() == b"1"


def test_transformers_rejects_unsupported_device():
    parser = argparse.ArgumentParser()
    backend = paddle.PaddleOcrVlBackend()
    backend.add_arguments(parser)
    args = parser.parse_args(["--engine", "transformers", "--device", "npu"])

    with pytest.raises(paddle.ConversionError, match="only cpu or gpu"):
        backend.validate(args)


def test_rejects_nonfinite_memory_interval():
    parser = argparse.ArgumentParser()
    backend = paddle.PaddleOcrVlBackend()
    backend.add_arguments(parser)
    for value in ("nan", "inf"):
        args = parser.parse_args(["--memory-interval", value])
        with pytest.raises(paddle.ConversionError, match="finite and non-negative"):
            backend.validate(args)


def test_interrupt_cleans_up_worker(tmp_path, monkeypatch):
    worker = mock.Mock()
    worker.wait.side_effect = KeyboardInterrupt
    worker.returncode = None
    cleanup = mock.Mock()
    monkeypatch.setattr(supervisor, "LOCK_PATH", tmp_path / "lock")
    monkeypatch.setattr(supervisor.subprocess, "Popen", mock.Mock(return_value=worker))
    monkeypatch.setattr(supervisor, "terminate_process_group", cleanup)

    assert _supervise(["input.pdf"]) == 130
    cleanup.assert_called_once_with(worker)


def test_second_run_stops_before_spawning_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(supervisor, "LOCK_PATH", tmp_path / "lock")
    popen = mock.Mock()
    monkeypatch.setattr(supervisor.subprocess, "Popen", popen)
    with supervisor.conversion_lock():
        assert _supervise(["input.pdf"]) == 1
    popen.assert_not_called()


def test_help_bypasses_active_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(supervisor, "LOCK_PATH", tmp_path / "lock")
    execute = mock.Mock(return_value=0)
    popen = mock.Mock()
    monkeypatch.setattr(supervisor, "execute", execute)
    monkeypatch.setattr(supervisor.subprocess, "Popen", popen)
    with supervisor.conversion_lock():
        assert _supervise(["--help"]) == 0
        assert _supervise(["--", "--help"]) == 1
    execute.assert_called_once()
    popen.assert_not_called()


def test_worker_inherits_lock_fd(tmp_path, monkeypatch):
    worker = mock.Mock(returncode=0)
    worker.wait.return_value = 0
    popen = mock.Mock(return_value=worker)
    monkeypatch.setattr(supervisor, "LOCK_PATH", tmp_path / "lock")
    monkeypatch.setattr(supervisor.subprocess, "Popen", popen)

    assert _supervise(["input.pdf"]) == 0
    assert len(popen.call_args.kwargs["pass_fds"]) == 1


def test_worker_start_failure_is_clean_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(supervisor, "LOCK_PATH", tmp_path / "lock")
    monkeypatch.setattr(
        supervisor.subprocess, "Popen", mock.Mock(side_effect=OSError("fork failed"))
    )

    assert _supervise(["input.pdf"]) == 1
    assert "ERROR: Failed to start PaddleOCR-VL worker: fork failed" in capsys.readouterr().err


def test_worker_restores_termination_signals(monkeypatch):
    pthread_sigmask = mock.Mock()
    set_signal = mock.Mock()
    monkeypatch.setattr(supervisor.signal, "pthread_sigmask", pthread_sigmask)
    monkeypatch.setattr(supervisor.signal, "signal", set_signal)

    supervisor.prepare_worker_signals()

    pthread_sigmask.assert_called_once_with(
        supervisor.signal.SIG_UNBLOCK, supervisor.TERMINATION_SIGNALS
    )
    assert set_signal.call_args_list == [
        mock.call(signum, supervisor.signal.default_int_handler)
        for signum in supervisor.TERMINATION_SIGNALS
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
        monkeypatch.setattr(supervisor, "LOCK_PATH", lock_path)
        with (
            pytest.raises(paddle.ConversionError, match="already running"),
            supervisor.conversion_lock(),
        ):
            pass
        holder.kill()
        holder.wait()
        with supervisor.conversion_lock():
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

    supervisor.terminate_process_group(worker)
    time.sleep(0.7)

    assert worker.poll() is not None
    assert not marker.exists()

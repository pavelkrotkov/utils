"""
Cross-backend transcription smoke tests.

Runs each transcription backend against tests/fixtures/test_speech.m4a and
asserts the output contains the expected phrase "quick brown fox".  Backends
whose dependencies are unavailable are skipped with an explanatory reason.
"""

from __future__ import annotations

import os
import platform
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "test_speech.m4a"
REPO_ROOT = Path(__file__).parent.parent
OPENAI_SCRIPT = REPO_ROOT / "audio_transcribe_openai.sh"
WHISPER_SCRIPT = REPO_ROOT / "audio_transcribe_whisper.py"
VIBEVOICE_SCRIPT = REPO_ROOT / "audio_transcribe_vibevoice.py"

EXPECTED_KEYWORDS = ["quick", "brown", "fox"]


# ---------------------------------------------------------------------------
# Skip conditions
# ---------------------------------------------------------------------------

_has_openai_key = bool(os.environ.get("OPENAI_API_KEY"))
_whisper_bin = shutil.which("whisper-cpp") or shutil.which("whisper-cli")
_default_model = Path(
    os.environ.get("WHISPER_MODEL_PATH", Path.home() / "models" / "ggml-large-v3-turbo-q8_0.bin")
)
_has_whisper = bool(_whisper_bin) and _default_model.exists()
_has_hf_token = bool(os.environ.get("HF_TOKEN"))
_is_apple_silicon = platform.system() == "Darwin" and platform.machine() == "arm64"
_has_mlx_audio = False
if _is_apple_silicon:
    try:
        import importlib.util

        _has_mlx_audio = importlib.util.find_spec("mlx_audio") is not None
    except (ImportError, AttributeError):
        pass

skip_no_openai = pytest.mark.skipif(not _has_openai_key, reason="OPENAI_API_KEY not set")
skip_no_whisper = pytest.mark.skipif(
    not _has_whisper,
    reason=f"whisper-cpp/whisper-cli not in PATH or model not found at {_default_model}",
)
skip_no_hf_token = pytest.mark.skipif(not _has_hf_token, reason="HF_TOKEN not set")
skip_no_vibevoice = pytest.mark.skipif(
    not (_is_apple_silicon and _has_mlx_audio),
    reason="VibeVoice requires Apple Silicon and mlx-audio",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_transcript(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace").lower()


def _assert_keywords(text: str) -> None:
    for kw in EXPECTED_KEYWORDS:
        assert kw in text, f"Expected keyword {kw!r} not found in transcript:\n{text[:500]}"


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 60) -> None:
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd or REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise AssertionError(
            f"Command timed out after {timeout}s:\n"
            f"  cmd: {shlex.join(cmd)}\n"
            f"  stdout: {e.stdout[-500:] if e.stdout else 'N/A'}\n"
            f"  stderr: {e.stderr[-500:] if e.stderr else 'N/A'}"
        ) from e
    if result.returncode != 0:
        raise AssertionError(
            f"Command failed (exit {result.returncode}):\n"
            f"  cmd: {shlex.join(cmd)}\n"
            f"  stdout: {result.stdout[-500:]}\n"
            f"  stderr: {result.stderr[-500:]}"
        )


# ---------------------------------------------------------------------------
# OpenAI backends
# ---------------------------------------------------------------------------


@skip_no_openai
@pytest.mark.parametrize(
    "model",
    ["whisper-1", "gpt-4o-mini-transcribe", "gpt-4o-transcribe"],
)
def test_openai_model(model: str, tmp_path: Path) -> None:
    out = tmp_path / "transcript.txt"
    _run([str(OPENAI_SCRIPT), "--model", model, str(FIXTURE), str(out)])
    assert out.exists() and out.stat().st_size > 0, "Output file is missing or empty"
    _assert_keywords(_read_transcript(out))


# ---------------------------------------------------------------------------
# Local Whisper (no diarization)
# ---------------------------------------------------------------------------


@skip_no_whisper
def test_local_whisper(tmp_path: Path) -> None:
    out = tmp_path / "transcript.txt"
    _run(
        [
            "uv",
            "run",
            str(WHISPER_SCRIPT),
            str(FIXTURE),
            "--format",
            "txt",
            "-o",
            str(out),
            "--large-model",
            str(_default_model),
        ],
        timeout=60,
    )
    assert out.exists() and out.stat().st_size > 0, "Output file is missing or empty"
    _assert_keywords(_read_transcript(out))


# ---------------------------------------------------------------------------
# Local Whisper + diarization
# ---------------------------------------------------------------------------


@skip_no_whisper
@skip_no_hf_token
def test_local_whisper_diarization(tmp_path: Path) -> None:
    out = tmp_path / "transcript.txt"
    _run(
        [
            "uv",
            "run",
            str(WHISPER_SCRIPT),
            str(FIXTURE),
            "--diarization",
            "-o",
            str(out),
            "--large-model",
            str(_default_model),
        ],
        timeout=60,
    )
    assert out.exists() and out.stat().st_size > 0, "Output file is missing or empty"
    _assert_keywords(_read_transcript(out))


# ---------------------------------------------------------------------------
# VibeVoice (Apple Silicon only)
# ---------------------------------------------------------------------------


@skip_no_vibevoice
def test_vibevoice(tmp_path: Path) -> None:
    out = tmp_path / "transcript.txt"
    _run(
        ["uv", "run", str(VIBEVOICE_SCRIPT), str(FIXTURE), "--format", "txt", "-o", str(out)],
        timeout=60,
    )
    assert out.exists() and out.stat().st_size > 0, "Output file is missing or empty"
    _assert_keywords(_read_transcript(out))


# ---------------------------------------------------------------------------
# Spaces-in-filename edge case (uses the first available backend)
# ---------------------------------------------------------------------------


def test_spaces_in_filename(tmp_path: Path) -> None:
    """Verify that backends handle paths with spaces without shell-quoting errors."""
    spaced_dir = tmp_path / "test dir"
    spaced_dir.mkdir()
    spaced_input = spaced_dir / "my recording.m4a"
    shutil.copy(FIXTURE, spaced_input)

    if _has_openai_key:
        out = tmp_path / "out.txt"
        _run([str(OPENAI_SCRIPT), "--model", "whisper-1", str(spaced_input), str(out)])
        assert out.exists() and out.stat().st_size > 0
        _assert_keywords(_read_transcript(out))
    elif _has_whisper:
        out = tmp_path / "out.txt"
        _run(
            [
                "uv",
                "run",
                str(WHISPER_SCRIPT),
                str(spaced_input),
                "--format",
                "txt",
                "-o",
                str(out),
                "--large-model",
                str(_default_model),
            ],
            timeout=60,
        )
        assert out.exists() and out.stat().st_size > 0
        _assert_keywords(_read_transcript(out))
    elif _is_apple_silicon and _has_mlx_audio:
        out = tmp_path / "out.txt"
        _run(
            [
                "uv",
                "run",
                str(VIBEVOICE_SCRIPT),
                str(spaced_input),
                "--format",
                "txt",
                "-o",
                str(out),
            ],
            timeout=60,
        )
        assert out.exists() and out.stat().st_size > 0
        _assert_keywords(_read_transcript(out))
    else:
        pytest.skip("No transcription backend available to test spaces-in-filename")

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "audio_transcribe_openai.sh"


@pytest.fixture
def mocked_api(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("bash", "grep", "stat", "mktemp", "rm", "mv", "jq", "python3"):
        executable = shutil.which(name)
        if executable:
            (bin_dir / name).symlink_to(executable)
    curl = bin_dir / "curl"
    curl.write_text(
        """#!/usr/bin/env python3
import os, sys
from pathlib import Path
args = sys.argv
Path(args[args.index('-o') + 1]).write_bytes(os.environ.get('MOCK_BODY', '').encode())
if os.environ.get('MOCK_MARKER'):
    Path(os.environ['MOCK_MARKER']).touch()
if os.environ.get('MOCK_ARGS'):
    Path(os.environ['MOCK_ARGS']).write_text('\\n'.join(args))
print(os.environ.get('MOCK_STATUS', '200'), end='')
sys.exit(int(os.environ.get('MOCK_EXIT', '0')))
"""
    )
    curl.chmod(0o755)
    audio = tmp_path / "sample audio.m4a"
    audio.write_bytes(b"test audio")
    env = dict(os.environ, PATH=str(bin_dir), OPENAI_API_KEY="sk-unit-test-secret")
    return audio, tmp_path / "transcript.txt", env, bin_dir


def run_script(mocked_api, *args):
    audio, output, env, _ = mocked_api
    return subprocess.run(
        [str(SCRIPT), *args, str(audio), str(output)], env=env, capture_output=True, text=True
    )


@pytest.mark.skipif(not shutil.which("jq"), reason="jq is needed to parse valid responses")
def test_success_writes_utf8_text_atomically(mocked_api):
    _, output, env, _ = mocked_api
    output.write_text("previous transcript")
    transcript = "café 🌍\nsecond line"
    env["MOCK_BODY"] = json.dumps({"text": transcript})
    result = run_script(mocked_api)
    assert result.returncode == 0, result.stderr
    assert output.read_bytes() == (transcript + "\n").encode("utf-8")
    assert "Saved transcript" in result.stdout
    assert not list(output.parent.glob("transcript.txt.*"))


@pytest.mark.skipif(not shutil.which("jq"), reason="jq is needed to parse valid responses")
@pytest.mark.parametrize(
    "body, error",
    [
        ("{not-json", "invalid JSON"),
        ("", "invalid JSON"),
        ("{}", "non-empty '.text'"),
        ('{"text":123}', "non-empty '.text'"),
        ('{"text":"  \\n "}', "non-empty '.text'"),
        ("null", "non-empty '.text'"),
    ],
)
def test_bad_responses_never_replace_or_create_transcript(mocked_api, body, error):
    _, output, env, _ = mocked_api
    env["MOCK_BODY"] = body
    output.write_text("previous transcript")
    failed = run_script(mocked_api)
    assert failed.returncode != 0
    assert error in failed.stderr
    assert "Saved transcript" not in failed.stdout
    assert output.read_text() == "previous transcript"
    output.unlink()
    assert run_script(mocked_api).returncode != 0
    assert not output.exists()
    assert not list(output.parent.glob("transcript.txt.*"))


@pytest.mark.skipif(not shutil.which("jq"), reason="jq is needed for API error extraction")
def test_http_errors_are_actionable_without_exposing_credentials(mocked_api):
    _, output, env, _ = mocked_api
    output.write_text("previous transcript")
    env["MOCK_STATUS"] = "429"
    env["MOCK_BODY"] = json.dumps(
        {"error": {"type": "insufficient_quota", "message": "Check token " + env["OPENAI_API_KEY"]}}
    )
    failed = run_script(mocked_api)
    assert failed.returncode != 0
    assert "HTTP 429" in failed.stderr
    assert "API error (insufficient_quota): Check token [REDACTED]" in failed.stderr
    assert env["OPENAI_API_KEY"] not in failed.stderr
    assert output.read_text() == "previous transcript"


def test_network_error_keeps_previous_transcript(mocked_api):
    _, output, env, _ = mocked_api
    output.write_text("previous transcript")
    env.update(MOCK_EXIT="7", MOCK_BODY='{"text":"not a transcript"}')
    failed = run_script(mocked_api)
    assert failed.returncode != 0
    assert "network error" in failed.stderr
    assert output.read_text() == "previous transcript"


def test_missing_jq_fails_before_upload(mocked_api):
    _, output, env, bin_dir = mocked_api
    (bin_dir / "jq").unlink(missing_ok=True)
    marker = output.parent / "curl-was-called"
    env["MOCK_MARKER"] = str(marker)
    failed = run_script(mocked_api)
    assert failed.returncode != 0
    assert "jq is required" in failed.stderr
    assert not output.exists()
    assert not marker.exists()


@pytest.mark.skipif(not shutil.which("jq"), reason="jq required")
def test_default_and_explicit_model_request_json(mocked_api):
    _, output, env, _ = mocked_api
    env["MOCK_BODY"] = '{"text":"spoken words"}'
    args_file = output.parent / "args"
    env["MOCK_ARGS"] = str(args_file)
    for options in ((), ("--model", "gpt-transcribe")):
        result = run_script(mocked_api, *options)
        assert result.returncode == 0, result.stderr
        arguments = args_file.read_text().splitlines()
        assert "model=gpt-transcribe" in arguments
        assert "response_format=json" in arguments
        assert "spoken words" in output.read_text()


@pytest.mark.skipif(not shutil.which("jq"), reason="jq required")
def test_legacy_model_warns(mocked_api):
    _, output, env, _ = mocked_api
    env["MOCK_BODY"] = '{"text":"spoken words"}'
    result = run_script(mocked_api, "--model", "whisper-1")
    assert result.returncode == 0, result.stderr
    assert "deprecated" in result.stderr
    assert "2027-02-26" in result.stderr
    assert output.read_text().strip() == "spoken words"

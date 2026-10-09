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
    for name in (
        "bash", "grep", "stat", "mktemp", "rm", "mv", "jq", "python3",
        "cat", "cut", "shasum", "mkdir", "dirname",
    ):
        executable = shutil.which(name)
        if executable:
            (bin_dir / name).symlink_to(executable)
    curl = bin_dir / "curl"
    curl.write_text(
        """#!/usr/bin/env python3
import os, sys, json
from pathlib import Path
args = sys.argv
upload = next((Path(s.split('=@', 1)[1]) for s in args if s.startswith('file=@')), None)
index = 0
if os.environ.get('MOCK_UPLOAD_LOG'):
    log = Path(os.environ['MOCK_UPLOAD_LOG'])
    index = len(log.read_text().splitlines()) + 1 if log.exists() else 1
    with log.open('a') as out:
        out.write(f'{upload.name} {upload.stat().st_size}\\n')
body = os.environ.get('MOCK_BODY', '')
status = os.environ.get('MOCK_STATUS', '200')
if os.environ.get('MOCK_DYNAMIC'):
    body = json.dumps({'text': upload.stem})
if os.environ.get('MOCK_FAIL_AT') and int(os.environ['MOCK_FAIL_AT']) == index:
    status, body = '429', '{"error":{"message":"try again"}}'
if os.environ.get('MOCK_INVALID_AT') and int(os.environ['MOCK_INVALID_AT']) == index:
    body = '{}'
Path(args[args.index('-o') + 1]).write_bytes(body.encode())
if os.environ.get('MOCK_MARKER'):
    Path(os.environ['MOCK_MARKER']).touch()
if os.environ.get('MOCK_ARGS'):
    Path(os.environ['MOCK_ARGS']).write_text('\\n'.join(args))
print(status, end='')
sys.exit(int(os.environ.get('MOCK_EXIT', '0')))
"""
    )
    curl.chmod(0o755)
    audio = tmp_path / "sample audio.m4a"
    audio.write_bytes(b"test audio")
    env = dict(os.environ, PATH=str(bin_dir), OPENAI_API_KEY="sk-unit-test-secret")
    return audio, tmp_path / "transcript.txt", env, bin_dir


def run_script(mocked_api, *args, input_text=None):
    audio, output, env, _ = mocked_api
    return subprocess.run(
        [str(SCRIPT), *args, str(audio), str(output)],
        env=env,
        input=input_text,
        capture_output=True,
        text=True,
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


@pytest.fixture
def oversized_audio(mocked_api):
    audio, output, env, bin_dir = mocked_api
    with audio.open("wb") as file:
        file.truncate(30_000_000)
    ffprobe = bin_dir / "ffprobe"
    ffprobe.write_text("#!/usr/bin/env python3\nprint('4000')\n")
    ffprobe.chmod(0o755)
    ffmpeg = bin_dir / "ffmpeg"
    ffmpeg.write_text(
        """#!/usr/bin/env python3
import os, sys
from pathlib import Path
args = sys.argv
if '-af' in args:
    if os.environ.get('MOCK_SILENCES', '1') == '1':
        sys.stderr.write('silence_start: 1699\\nsilence_end: 1701\\n')
        sys.stderr.write('silence_start: 3499\\nsilence_end: 3501\\n')
else:
    output = Path(args[-1]); output.parent.mkdir(parents=True, exist_ok=True)
    index = int(output.stem.rsplit('-', 1)[-1]) if '-ss' in args else 0
    with output.open('wb') as file:
        file.write(str(index).encode())
        file.truncate(26_000_000 if not index or index == int(os.environ.get('MOCK_OVERSIZE', '0')) else 2000)
"""
    )
    ffmpeg.chmod(0o755)
    env.update(MOCK_DYNAMIC="1", MOCK_UPLOAD_LOG=str(output.parent / "uploads"))
    return mocked_api


@pytest.mark.skipif(not shutil.which("jq"), reason="jq required")
def test_oversized_requires_opt_in_and_consent(oversized_audio):
    _, output, env, _ = oversized_audio
    log = Path(env["MOCK_UPLOAD_LOG"])
    output.write_text("existing transcript")
    refused = run_script(oversized_audio)
    assert "Rerun with --chunk" in refused.stderr
    declined = run_script(oversized_audio, "--chunk", input_text="n\n")
    assert "Chunk 1" in declined.stdout and "no audio uploaded" in declined.stderr
    assert not log.exists()
    assert output.read_text() == "existing transcript"


@pytest.mark.skipif(not shutil.which("jq"), reason="jq required")
def test_chunks_upload_in_order_resume_without_overwriting_failed_result(oversized_audio):
    _, output, env, _ = oversized_audio
    log = Path(env["MOCK_UPLOAD_LOG"])
    output.write_text("previous transcript")
    env["MOCK_FAIL_AT"] = "2"
    failed = run_script(oversized_audio, "--chunk", "--yes")
    assert failed.returncode != 0 and "HTTP 429" in failed.stderr
    assert output.read_text() == "previous transcript"
    assert len(list((output.parent / "transcript.txt.parts").glob("*.txt"))) == 1
    assert [s.split()[0] for s in log.read_text().splitlines()] == [
        "chunk-0001.m4a", "chunk-0002.m4a"
    ]
    env.pop("MOCK_FAIL_AT")
    completed = run_script(oversized_audio, "--chunk", "--yes")
    assert completed.returncode == 0, completed.stderr
    assert "Reusing completed chunk 1/3" in completed.stdout
    assert [s.split()[0] for s in log.read_text().splitlines()] == [
        "chunk-0001.m4a", "chunk-0002.m4a", "chunk-0002.m4a", "chunk-0003.m4a"
    ]
    assert all(int(s.split()[1]) < 24_000_000 for s in log.read_text().splitlines())
    result = output.read_text()
    assert result.index("chunk-0001") < result.index("chunk-0002") < result.index("chunk-0003")
    assert "1699" in result or "1700.000" in result
    assert result.count("[Chunk ") == 3


@pytest.mark.skipif(not shutil.which("jq"), reason="jq required")
def test_oversized_extracted_chunk_aborts_before_paid_upload(oversized_audio):
    _, output, env, _ = oversized_audio
    env["MOCK_OVERSIZE"] = "2"
    output.write_text("existing transcript")
    result = run_script(oversized_audio, "--chunk", "--yes")
    assert result.returncode != 0 and "chunk 2" in result.stderr
    assert not Path(env["MOCK_UPLOAD_LOG"]).exists()
    assert output.read_text() == "existing transcript"


@pytest.mark.skipif(not shutil.which("jq"), reason="jq required")
def test_invalid_chunk_is_retryable_without_partial_output(oversized_audio):
    _, output, env, _ = oversized_audio
    env["MOCK_INVALID_AT"] = "2"
    failed = run_script(oversized_audio, "--chunk", "--yes")
    assert failed.returncode != 0
    assert not output.exists()
    assert len(list((output.parent / "transcript.txt.parts").glob("*.txt"))) == 1
    env.pop("MOCK_INVALID_AT")
    completed = run_script(oversized_audio, "--chunk", "--yes")
    assert completed.returncode == 0
    assert output.read_text().count("[Chunk ") == 3


@pytest.mark.skipif(not shutil.which("jq"), reason="jq required")
def test_hard_cut_overlap_is_visible_in_transcript(oversized_audio):
    _, output, env, _ = oversized_audio
    env["MOCK_SILENCES"] = "0"
    result = run_script(oversized_audio, "--chunk", input_text="yes\n")
    assert result.returncode == 0, result.stderr
    assert "overlapping audio, review repeated words" in output.read_text()
    assert "[Chunk 2/3: 1797.500-" in output.read_text()


@pytest.mark.skipif(not shutil.which("jq"), reason="jq required")
def test_cache_is_model_specific(oversized_audio):
    _, output, env, _ = oversized_audio
    log = Path(env["MOCK_UPLOAD_LOG"])
    assert run_script(oversized_audio, "--chunk", "--yes").returncode == 0
    assert run_script(oversized_audio, "--chunk", "--yes", "--model", "whisper-1").returncode == 0
    assert len(log.read_text().splitlines()) == 6

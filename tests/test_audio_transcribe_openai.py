import json
import os
import shutil
import subprocess
from itertools import pairwise
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "audio_transcribe_openai.sh"


@pytest.fixture
def mocked_api(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in (
        "bash",
        "grep",
        "stat",
        "mktemp",
        "rm",
        "mv",
        "jq",
        "python3",
        "wc",
        "mkdir",
        "cat",
        "dirname",
    ):
        executable = shutil.which(name)
        if executable:
            (bin_dir / name).symlink_to(executable)
    curl = bin_dir / "curl"
    curl.write_text(
        """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv
calls = Path(os.environ['MOCK_CALLS']) if os.environ.get('MOCK_CALLS') else None
count = 1
if calls:
    previous = calls.read_text().splitlines() if calls.exists() else []
    count += len(previous)
    calls.write_text('\\n'.join(previous + [str(count)]))
source = next(x[6:] for x in args if x.startswith('file=@'))
if Path(source).stat().st_size > 25000000:
    print('Mock refused oversized upload', file=sys.stderr)
    sys.exit(31)
failure = os.environ.get('MOCK_FAIL_ON') == str(count)
body = '{"error":{"message":"try later"}}' if failure else os.environ.get('MOCK_BODY', json.dumps({'text': f'chunk {count} words'}))
Path(args[args.index('-o') + 1]).write_bytes(body.encode())
if os.environ.get('MOCK_MARKER'):
    Path(os.environ['MOCK_MARKER']).touch()
if os.environ.get('MOCK_ARGS'):
    Path(os.environ['MOCK_ARGS']).write_text('\\n'.join(args))
print('429' if failure else os.environ.get('MOCK_STATUS', '200'), end='')
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
def test_markdown_wraps_exact_cloud_transcript(mocked_api):
    _, output, env, _ = mocked_api
    env["MOCK_BODY"] = json.dumps({"text": "Words unchanged"})
    result = run_script(mocked_api, "--format", "md")
    assert result.returncode == 0, result.stderr
    assert output.read_text() == (
        "# Transcript\n\nAudio: [Open source](<sample audio.m4a>)\n\nWords unchanged\n"
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


MAX_BYTES = 25_000_000


@pytest.fixture
def oversized_api(mocked_api):
    audio, output, env, bin_dir = mocked_api
    with audio.open("wb") as stream:
        stream.truncate(MAX_BYTES + 100)
    ffmpeg = bin_dir / "ffmpeg"
    ffmpeg.write_text(
        """#!/usr/bin/env python3
import os, sys
from pathlib import Path
args = sys.argv[1:]
if '-af' in args:
    sys.stderr.write(os.environ.get('MOCK_SILENCE', 'silence_start: 3599\\nsilence_end: 3601\\n'))
elif '-ss' in args:
    if os.environ.get('MOCK_EXTRACT_FAIL') == '1':
        sys.exit(7)
    chunk = Path(args[-1])
    with chunk.open('wb') as stream:
        if os.environ.get('MOCK_OVERSIZED_CHUNK') == '1':
            stream.truncate(25000001)
        else:
            stream.write(chunk.name.encode())
else:
    with Path(args[-1]).open('wb') as stream:
        stream.truncate(25000100)
"""
    )
    ffmpeg.chmod(0o755)
    ffprobe = bin_dir / "ffprobe"
    ffprobe.write_text('#!/usr/bin/env python3\nprint("7600")\n')
    ffprobe.chmod(0o755)
    env["MOCK_CALLS"] = str(output.parent / "api-calls")
    return mocked_api


def test_oversize_plans_without_upload_or_key(oversized_api):
    audio, output, env, _ = oversized_api
    original_size = audio.stat().st_size
    output.write_text("old transcript")
    env.pop("OPENAI_API_KEY")
    result = run_script(oversized_api)
    assert result.returncode != 0
    assert "Chunk 0001: 0.000-3600.000" in result.stdout
    assert "Chunk 0002: 3600.000-" in result.stdout
    assert "Rerun with --chunk" in result.stderr
    assert not Path(env["MOCK_CALLS"]).exists()
    assert output.read_text() == "old transcript"
    assert audio.stat().st_size == original_size


@pytest.mark.skipif(not shutil.which("jq"), reason="jq required for mocked API")
def test_chunked_failure_resume_and_boundary_headers(oversized_api):
    audio, output, env, _ = oversized_api
    output.write_text("original output")
    env["MOCK_FAIL_ON"] = "2"
    failed = run_script(oversized_api, "--chunk")
    assert failed.returncode != 0
    assert "HTTP 429" in failed.stderr
    assert output.read_text() == "original output"
    assert len(list(output.parent.glob("transcript.txt.openai-chunks/*/chunk-0001.txt"))) == 1
    assert not list(output.parent.glob("transcript.txt.openai-chunks/*/chunk-0002.txt"))
    env.pop("MOCK_FAIL_ON")
    done = run_script(oversized_api, "--chunk")
    assert done.returncode == 0, done.stderr
    assert "Reusing completed chunk 0001" in done.stdout
    assert len(Path(env["MOCK_CALLS"]).read_text().splitlines()) == 4
    text = output.read_text()
    assert text.index("chunk 1 words") < text.index("chunk 3 words") < text.index("chunk 4 words")
    assert text.count("[Chunk ") == 3
    assert text.index("[Chunk 0001:") < text.index("[Chunk 0002:") < text.index("[Chunk 0003:")
    assert audio.stat().st_size == MAX_BYTES + 100


def test_preflight_rejects_oversized_chunk_before_any_upload(oversized_api):
    _, output, env, _ = oversized_api
    output.write_text("old transcript")
    env["MOCK_OVERSIZED_CHUNK"] = "1"
    failed = run_script(oversized_api, "--chunk")
    assert failed.returncode != 0
    assert "nothing uploaded" in failed.stderr
    assert not Path(env["MOCK_CALLS"]).exists()
    assert output.read_text() == "old transcript"


def test_hard_cut_fallback_covers_entire_recording(oversized_api):
    _, _, env, _ = oversized_api
    env["MOCK_SILENCE"] = ""
    plan = run_script(oversized_api)
    assert plan.returncode != 0
    ranges = [
        line.strip().split(": ")[1].split("-")
        for line in plan.stdout.splitlines()
        if line.startswith("  Chunk ")
    ]
    boundaries = [(float(start), float(end)) for start, end in ranges]
    assert boundaries[0][0] == 0
    assert boundaries[-1][1] == 7600
    assert all(0 < end - start <= 3600 for start, end in boundaries)
    assert all(left[1] == right[0] for left, right in pairwise(boundaries))


def test_output_cannot_overwrite_audio(mocked_api):
    audio, _, env, _ = mocked_api
    failed = subprocess.run(
        [str(SCRIPT), str(audio), str(audio)], env=env, capture_output=True, text=True
    )
    assert failed.returncode != 0
    assert "must not replace" in failed.stderr
    assert audio.read_bytes() == b"test audio"


def test_failed_chunk_extraction_never_uploads_or_replaces_output(oversized_api):
    audio, output, env, _ = oversized_api
    output.write_text("previous transcript")
    env["MOCK_EXTRACT_FAIL"] = "1"
    failed = run_script(oversized_api, "--chunk")
    assert failed.returncode != 0
    assert "ffmpeg chunk 1 failed" in failed.stderr
    assert not Path(env["MOCK_CALLS"]).exists()
    assert output.read_text() == "previous transcript"
    assert audio.stat().st_size == MAX_BYTES + 100

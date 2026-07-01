# Utils

Standalone utility scripts for PDF conversion, Markdown splitting, audio transcription,
and TIDAL import.

## Overview

- PDF to Markdown conversion (Mathpix SDK, Docling, LlamaParse, PyMuPDF4LLM, or local marker).
- Markdown splitting into per-section or per-subsection files.
- Audiobook (M4B) conversion from folders of audio tracks, with chapters and cover art.
- Audio transcription (OpenAI API, local whisper-cpp with optional pyannote diarization, or MLX VibeVoice-ASR on Apple Silicon).
- TIDAL playlist import from Gramophone-style MHTML/Markdown pages.

For deeper context, refer to the script headers and inline help.

## Setup

Python dependencies are declared inline in each script (PEP 723) and resolved automatically by `uv run`.

System tools (macOS via Homebrew):

```bash
brew install ffmpeg whisper-cpp jq
```

Environment variables:

- `MATHPIX_APP_ID`, `MATHPIX_APP_KEY` for Mathpix. `MATHPIX_API_KEY` can be used as an app-key fallback.
- `LLAMA_CLOUD_API_KEY` for LlamaParse (LlamaCloud).
- `OPENAI_API_KEY` for OpenAI transcription.
- `TIDAL_CLIENT_ID` for TIDAL import (Required).
- `TIDAL_CLIENT_SECRET` for TIDAL import (Optional for some app types).
- `TIDAL_REDIRECT_URI` (Optional, defaults to `http://127.0.0.1:8765/callback`).
- `TIDAL_SCOPES` (Optional, defaults to playlist and search read/write).
- `TIDAL_COUNTRY_CODE` (Optional, defaults to `US`).
- `HF_TOKEN` for optional pyannote diarization.
- `GGML_METAL_PATH_RESOURCES` optional for whisper-cpp Metal support.

## PDF Conversion

Mathpix (best for math-heavy PDFs):

```bash
uv run ./pdf_convert_mathpix_sdk.py input.pdf -o output.md
uv run ./pdf_convert_mathpix_sdk.py input.pdf --no-rm-spaces --enable-tables-fallback
uv run ./pdf_convert_mathpix_sdk.py input.pdf --app-id YOUR_ID --app-key YOUR_KEY --timeout 300
```

Docling (local, structured parsing):

```bash
uv run ./pdf_convert_docling.py input.pdf -o output.md
uv run ./pdf_convert_docling.py input.pdf --page-range 1-5
```

All local/hosted PDF converters that support `--page-range` use the same
user-facing syntax: 1-based page numbers, comma-separated lists, ranges, and
`N` for the last page, such as `1-5`, `1,3,5-10`, or `5-N`.

LlamaParse (LlamaCloud, hosted):

```bash
uv run ./pdf_convert_llamaparse.py input.pdf -o output.md
uv run ./pdf_convert_llamaparse.py input.pdf --page-range 1-5
uv run ./pdf_convert_llamaparse.py --fetch-job job_id -o output-3.md
```

Create an API key at https://cloud.llamaindex.ai via API Key -> Generate New Key, then set `LLAMA_CLOUD_API_KEY`.

The script always chunks PDFs, saves `output-<i>.md` partials, and skips existing chunks on rerun to resume.

PyMuPDF4LLM (local, fast layout-aware parsing):

```bash
uv run ./pdf_convert_pymupdf4llm.py input.pdf -o output.md
uv run ./pdf_convert_pymupdf4llm.py input.pdf --page-range 1-5
```

Layout mode requires `pymupdf4llm[layout]` (or `pymupdf4llm[ocr,layout]` for OCR support).

Marker (best for simpler PDFs, local):

```bash
uv run ./pdf_convert_marker.py input.pdf -o output.md
uv run ./pdf_convert_marker.py input.pdf --page-range 1,3,5-N
```

## Markdown Tools

Split a Markdown file into smaller Markdown files in the same folder at each `##` or
`###` heading.

```bash
./markdown_split.sh notes.md '##'
./markdown_split.sh notes.md '###'
```

Behavior:

- Uses the full heading text as the output filename.
- Appends ` 2`, ` 3`, etc. only when duplicate heading names collide.
- Writes content before the first matching heading to `<input> Preamble.md`.
- Ignores matching headings inside fenced code blocks.

## Audio & Music Tools

### Audiobook (M4B) Conversion

Convert folders of audio tracks into chaptered M4B audiobooks (one chapter per
track, natural-sorted, with embedded cover art). By default every immediate
subfolder of the input is its own book; use `--single` for one book. Tracks that
ffprobe reports as unreadable or zero-length (e.g. partial re-downloads) are
skipped automatically.

```bash
# A whole collection -> one .m4b per subfolder (output to a sibling dir):
uv run ./audio_folder_to_m4b.py "/path/to/Audiobook Collection"

# A single book folder, with author metadata:
uv run ./audio_folder_to_m4b.py "/path/to/Some Book" --single --artist "Author Name"

# Filter to specific books, or preview without encoding:
uv run ./audio_folder_to_m4b.py "/path/to/Collection" --book Pandas
uv run ./audio_folder_to_m4b.py "/path/to/Collection" --dry-run
```

### TIDAL Import

Match classical album reviews from Gramophone-style Markdown files to TIDAL
catalog entries. The pipeline parses source files into canonical JSON, searches
and scores candidates via the TIDAL API, and supports interactive labelling,
offline evaluation, and weight training.

Parse a source file into canonical album JSON, then match interactively:

```bash
uv run ./tidal_parse_to_json.py input.md --output albums.json
uv run ./tidal_match_from_json.py albums.json --resume
```

Train scoring weights from labelled truth records:

```bash
uv run ./tidal_match_from_json.py albums.json --train-coverage --training-out tidal_match_training.json
```

#### Evaluation

Measure matching quality offline against a truth file (no API calls):

```bash
uv run ./tidal_eval.py album-debug/best2025.truth.json
uv run ./tidal_eval.py album-debug/best2025.truth.json --verbose
uv run ./tidal_eval.py album-debug/best2025.truth.json --json
```

Use quality gates to catch regressions:

```bash
uv run ./tidal_eval.py album-debug/best2025.truth.json --min-precision 0.90
```

Pass `--weights custom.json` to evaluate alternative scoring weights.

#### Improvement workflow

The truth file (`best2025.truth.json`) is a frozen gold set with cached
candidates and ground-truth labels. What you can improve determines which
tool to use:

| What you're tuning | Tool | API calls? |
|---|---|---|
| Scoring weights / features | `tidal_eval.py` | No |
| Query generation / candidate recall | `tidal_match_from_json.py --batch-review` | Yes |
| Truth labels (new albums) | `tidal_match_from_json.py` | Yes |

Re-scoring is deterministic — `tidal_eval.py` replays the cached candidates
through the same search/scoring/ranking driver without touching the API. If you
improve query generation (surfacing better candidates), re-run the matcher to
refresh the candidate pool, re-label any changed results, then re-eval.

Treat the truth file as read-only unless you deliberately want to extend or
refresh the ground truth.

#### Formal Matching Pipeline

See [TIDAL_ARCHITECTURE.md](TIDAL_ARCHITECTURE.md) for the current formal
pipeline, module map, and implementation invariants.

## Audio Transcription

OpenAI API (simple transcription):

```bash
./audio_transcribe_openai.sh recording.m4a output.txt
```

Local whisper-cpp (plain transcript by default):

```bash
uv run ./audio_transcribe_whisper.py interview.m4a
uv run ./audio_transcribe_whisper.py interview.m4a --format srt
uv run ./audio_transcribe_whisper.py interview.m4a --max-context -1
```

The local whisper-cpp wrapper defaults to `--max-context 0`, which disables
rolling text context between decode windows and reduces hallucination loops on
dictation or meeting audio with long pauses. For clean, continuous speech where
context carryover may improve accuracy, punctuation, or casing, use
`--max-context -1` to restore whisper-cpp's default behavior.

Apple Silicon VibeVoice-ASR via MLX (structured JSON by default):

```bash
uv run ./audio_transcribe_vibevoice.py interview.m4a
uv run ./audio_transcribe_vibevoice.py interview.m4a --context "speaker names, acronyms"
uv run ./audio_transcribe_vibevoice.py interview.m4a --format vtt -o interview.vtt
uv run ./audio_transcribe_vibevoice.py interview.m4a --format txt -o interview.txt

# Re-format an existing JSON transcript without re-transcribing:
uv run ./audio_transcribe_vibevoice.py --from-json interview.vibevoice.json --format vtt
uv run ./audio_transcribe_vibevoice.py --from-json interview.vibevoice.json --format srt -o interview.srt
uv run ./audio_transcribe_vibevoice.py --from-json interview.vibevoice.json --format diarized-txt
uv run ./audio_transcribe_vibevoice.py --from-json interview.vibevoice.json --format diarized-breaks
```

Enable pyannote speaker diarization when speaker labels are needed:

```bash
uv run ./audio_transcribe_whisper.py interview.m4a --diarization --num-speakers 2
```

By default, the local script prints progress/ETA reports and writes `<input>.txt`.
With `--diarization`, it writes `<input>.spk.txt`. Use `--no-progress` to silence
progress reports or `--progress-interval SECONDS` to adjust their frequency.

Whisper and VibeVoice share the same local `txt`, `srt`, and `vtt` transcript
emitters. Whisper defaults to `diarized-txt` output when `--diarization` is
enabled. The VibeVoice script defaults to `mlx-community/VibeVoice-ASR-4bit`,
writes `<input>.vibevoice.json`, supports `json`, `txt`, `srt`, `vtt`,
`diarized-txt` (speaker-labeled paragraphs), and `diarized-breaks`
(paragraphs separated by `--- speaker change ---` markers), and downloads the
model through Hugging Face on first use. Use
`--from-json <input>.vibevoice.json --format <fmt>` to convert an existing JSON
transcript to any of those formats without re-running ASR.

## Notes

- Scripts are standalone and run directly; no central build or test harness.
- Prefer small sample files for quick validation.
- Outputs default next to the input file unless overridden.

## Linting

```bash
ruff check .
ruff format --check .
```

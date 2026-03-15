# Utils

Standalone utility scripts for PDF conversion, Markdown splitting, audio transcription,
and TIDAL import.

## Overview

- PDF to Markdown conversion (Mathpix API, Docling, LlamaParse, PyMuPDF4LLM, or local marker).
- Markdown splitting into per-section or per-subsection files.
- Audio transcription (OpenAI API) and local diarization (whisper-cpp + pyannote).
- TIDAL playlist import from Gramophone-style MHTML/Markdown pages.

For deeper context, refer to the script headers and inline help.

## Setup

Python dependencies:

```bash
pip install mpxpy marker-pdf docling requests torch pyannote.audio beautifulsoup4 lxml numpy pandas
pip install mpxpy marker-pdf pymupdf4llm[layout] llama-cloud pypdf requests torch pyannote.audio beautifulsoup4 lxml numpy pandas
```

System tools (macOS via Homebrew):

```bash
brew install ffmpeg whisper-cpp jq
```

Environment variables:

- `MATHPIX_APP_ID`, `MATHPIX_APP_KEY` for Mathpix tools.
- `LLAMA_CLOUD_API_KEY` for LlamaParse (LlamaCloud).
- `OPENAI_API_KEY` for OpenAI transcription.
- `TIDAL_CLIENT_ID` for TIDAL import (Required).
- `TIDAL_CLIENT_SECRET` for TIDAL import (Optional for some app types).
- `TIDAL_REDIRECT_URI` (Optional, defaults to `http://127.0.0.1:8765/callback`).
- `TIDAL_SCOPES` (Optional, defaults to playlist and search read/write).
- `TIDAL_COUNTRY_CODE` (Optional, defaults to `US`).
- `HF_TOKEN` for pyannote diarization.
- `GGML_METAL_PATH_RESOURCES` optional for whisper-cpp Metal support.

## PDF Conversion

Mathpix (best for math-heavy PDFs):

```bash
pipx run ./pdf_convert_mathpix_sdk.py input.pdf -o output.md
pipx run ./pdf_convert_mathpix_api.py input.pdf -o output.md
```

Docling (local, structured parsing):

```bash
pipx run ./pdf_convert_docling.py input.pdf -o output.md
pipx run ./pdf_convert_docling.py input.pdf --page-range 1-5
```

Note: If you omit `--page-range`, the script uses Docling defaults. Provide a contiguous range like `1-5` when you want a subset of pages.

LlamaParse (LlamaCloud, hosted):

```bash
pipx run ./pdf_convert_llamaparse.py input.pdf -o output.md
pipx run ./pdf_convert_llamaparse.py input.pdf --page-range 1-5
pipx run ./pdf_convert_llamaparse.py --fetch-job job_id -o output-3.md
```

Create an API key at https://cloud.llamaindex.ai via API Key -> Generate New Key, then set `LLAMA_CLOUD_API_KEY`.

The script always chunks PDFs, saves `output-<i>.md` partials, and skips existing chunks on rerun to resume.

PyMuPDF4LLM (local, fast layout-aware parsing):

```bash
pipx run ./pdf_convert_pymupdf4llm.py input.pdf -o output.md
pipx run ./pdf_convert_pymupdf4llm.py input.pdf --page-range 0-4
```

Layout mode requires `pymupdf4llm[layout]` (or `pymupdf4llm[ocr,layout]` for OCR support).

Marker (best for simpler PDFs, local):

```bash
pipx run ./pdf_convert_marker.py input.pdf -o output.md
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

### TIDAL Import

Match classical album reviews from Gramophone-style Markdown files to TIDAL
catalog entries. The pipeline parses source files into canonical JSON, searches
and scores candidates via the TIDAL API, and supports interactive labelling,
offline evaluation, and weight training.

Parse a source file into canonical album JSON, then match interactively:

```bash
pipx run ./tidal_parse_to_json.py input.md --output albums.json
pipx run ./tidal_match_from_json.py albums.json --resume
```

Train scoring weights from labelled truth records:

```bash
pipx run ./tidal_match_from_json.py albums.json --train-coverage --training-out tidal_match_training.json
```

#### Evaluation

Measure matching quality offline against a truth file (no API calls):

```bash
pipx run ./tidal_eval.py album-debug/best2025.truth.json
pipx run ./tidal_eval.py album-debug/best2025.truth.json --verbose
pipx run ./tidal_eval.py album-debug/best2025.truth.json --json
```

Use quality gates to catch regressions:

```bash
pipx run ./tidal_eval.py album-debug/best2025.truth.json --min-precision 0.90
```

Pass `--weights custom.json` to evaluate alternative scoring weights.

#### Formal Matching Pipeline

See [TIDAL_ARCHITECTURE.md](TIDAL_ARCHITECTURE.md) for the current formal
pipeline, module map, and implementation invariants.

## Audio Transcription

OpenAI API (simple transcription):

```bash
./audio_transcribe_openai.sh recording.m4a output.txt
```

Local whisper-cpp + pyannote (with diarization):

```bash
./audio_transcribe_whisper.py interview.m4a --num-speakers 2
```

## Notes

- Scripts are standalone and run directly; no central build or test harness.
- Prefer small sample files for quick validation.
- Outputs default next to the input file unless overridden.

## Linting

```bash
ruff check .
ruff format --check .
```

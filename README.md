# Utils

Standalone utility scripts for PDF conversion, audio transcription, and TIDAL import.

## Overview

- PDF to Markdown conversion (Mathpix API, LlamaParse, or local marker).
- Audio transcription (OpenAI API) and local diarization (whisper-cpp + pyannote).
- TIDAL playlist import from Gramophone-style MHTML/Markdown pages.

For deeper context, refer to the script headers and inline help.

## Setup

Python dependencies:

```bash
pip install mpxpy marker-pdf llama-cloud pypdf requests torch pyannote.audio beautifulsoup4 lxml numpy pandas
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

LlamaParse (LlamaCloud, hosted):

```bash
pipx run ./pdf_convert_llamaparse.py input.pdf -o output.md
pipx run ./pdf_convert_llamaparse.py input.pdf --page-range 1-5
pipx run ./pdf_convert_llamaparse.py --fetch-job job_id -o output-3.md
```

Create an API key at https://cloud.llamaindex.ai via API Key -> Generate New Key, then set `LLAMA_CLOUD_API_KEY`.

The script always chunks PDFs, saves `output-<i>.md` partials, and skips existing chunks on rerun to resume.

Marker (best for simpler PDFs, local):

```bash
pipx run ./pdf_convert_marker.py input.pdf -o output.md
```

## Audio & Music Tools

### TIDAL Import

Import classical album tracklists from Gramophone-style MHTML or Markdown files into TIDAL playlists. Uses OAuth 2.1 with PKCE for secure authentication.

```bash
# Preview matches (Dry Run)
pipx run ./tidal_import_page_to_playlist.py test.md --dry-run

# Import to TIDAL (Creates playlist)
pipx run ./tidal_import_page_to_playlist.py test.mhtml
```

#### Matching Strategy

The importer uses a multi-stage matching strategy optimized for classical music:

1. **Performer extraction** - Parses complex performer strings (e.g., "Orchestra / Conductor (Label)") to extract conductor and soloist names, stripping instrument abbreviations (pf, vn, vc, hpd, etc.)

2. **Query construction** - Generates multiple search queries combining:
   - Extracted performer surnames with title tokens
   - Cleaned performer names with composer
   - Review slugs and label hints
   - Fallback composer + work type queries

3. **Scoring** - Matches are scored based on:
   - Title token overlap (up to 0.5)
   - Performer match in artists or title (+0.4 if found)
   - Label match in copyright (+0.2)
   - Reduced penalty when album likely not in catalog

4. **Fallback** - Track-based search when album search fails

#### Testing Matches

Use the built-in test report to validate and compare matching results:

```bash
# Quick report on a file
pipx run ./tidal_import_page_to_playlist.py input.md --dry-run --test-report

# Save baseline and compare after changes
pipx run ./tidal_import_page_to_playlist.py input.mhtml --dry-run --test-report-save baseline.json
pipx run ./tidal_import_page_to_playlist.py input.mhtml --dry-run --test-report-compare baseline.json
```

#### JSON-First Matching (Experimental)

Split parsing and matching for controlled tuning. Use `tidal_parse_prompt.md` to produce structured
JSON, then interactively label ground-truth matches with `tidal_match_from_json.py`.

```bash
# Label matches and save a truth file alongside the input JSON
pipx run ./tidal_match_from_json.py albums.json --resume
```

Train a query/matching model from the first 80 truth records:

```bash
pipx run ./tidal_match_from_json.py albums.json --train-coverage --training-out tidal_match_training.json
```

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

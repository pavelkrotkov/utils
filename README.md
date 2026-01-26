# Utils

Standalone utility scripts for PDF conversion and audio transcription.

## Overview

- PDF to Markdown conversion (Mathpix API or local marker).
- Audio transcription (OpenAI API) and local diarization (whisper-cpp + pyannote).

For deeper context, see `GEMINI.md` and `AGENTS.md`.

## Setup

Python dependencies:

```bash
pip install mpxpy marker-pdf requests torch pyannote.audio beautifulsoup4 lxml
```

System tools (macOS via Homebrew):

```bash
brew install ffmpeg whisper-cpp jq
```

Environment variables:

- `MATHPIX_APP_ID`, `MATHPIX_APP_KEY` for Mathpix tools.
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

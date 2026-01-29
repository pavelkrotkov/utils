# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A collection of standalone utility scripts for:
- **PDF to Markdown conversion** (Mathpix API or local marker-pdf)
- **Audio transcription** with speaker diarization (OpenAI API or local whisper-cpp + pyannote)
- **TIDAL playlist import** from Gramophone-style classical album lists

## Commands

### Linting
```bash
ruff check .
ruff format --check .
```

### Running Scripts
```bash
# PDF conversion
pipx run ./pdf_convert_mathpix_sdk.py input.pdf -o output.md
pipx run ./pdf_convert_mathpix_api.py input.pdf -o output.md
pipx run ./pdf_convert_marker.py input.pdf -o output.md

# Audio transcription
./audio_transcribe_openai.sh recording.m4a output.txt
./audio_transcribe_whisper.py interview.m4a --num-speakers 2

# TIDAL import
./tidal_import_page_to_playlist.py test.md --dry-run
./tidal_import_page_to_playlist.py test.mhtml
```

### Syntax Checking
```bash
python3 -m py_compile *.py
```

## Architecture

- **No shared modules**: Each script is standalone with no cross-file imports
- **No test harness**: Use small fixture files (`test.md`, `test.mhtml`) for manual validation
- **Outputs next to inputs**: Default behavior unless `-o` flag specifies otherwise
- **pipx execution**: Scripts support isolated dependency environments

## Key Constraints

- Keep scripts standalone; do not introduce cross-script imports
- Minimize dependencies; avoid adding new ones unless necessary
- Use `pathlib.Path` for filesystem operations
- Use `argparse` for CLI interfaces with a `main()` function
- Follow existing naming: `*_path` for paths, `*_bin` for binaries

## Environment Variables

| Variable | Script(s) |
|----------|-----------|
| `MATHPIX_APP_ID`, `MATHPIX_APP_KEY` | PDF converters |
| `OPENAI_API_KEY` | `audio_transcribe_openai.sh` |
| `TIDAL_CLIENT_ID` | `tidal_import_page_to_playlist.py` |
| `HF_TOKEN` | `audio_transcribe_whisper.py` |

For complete guidelines on code style and conventions, see `AGENTS.md`.

# Utils

Standalone utility scripts for PDF conversion and audio transcription.

## Overview

- PDF to Markdown conversion (Mathpix API or local marker).
- Audio transcription (OpenAI API) and local diarization (whisper-cpp + pyannote).

For deeper context, see `GEMINI.md` and `AGENTS.md`.

## Setup

Python dependencies:

```bash
pip install mpxpy marker-pdf requests torch pyannote.audio
```

System tools (macOS via Homebrew):

```bash
brew install ffmpeg whisper-cpp jq
```

Environment variables:

- `MATHPIX_APP_ID`, `MATHPIX_APP_KEY` for Mathpix tools.
- `OPENAI_API_KEY` for OpenAI transcription.
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

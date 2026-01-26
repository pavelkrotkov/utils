# Project Context: Utils

## Overview
This directory contains a collection of standalone utility scripts primarily focused on three domains:
1.  **PDF to Markdown Conversion**: Tools to convert PDF documents (especially scientific papers) into Markdown with LaTeX math support using the Mathpix API or local models.
2.  **Audio Transcription & Diarization**: Tools to transcribe audio files using either the OpenAI API or a robust local pipeline combining `whisper-cpp` and `pyannote.audio`.
3.  **Music & Playlist Management**: Tools to import album tracklists from web/document sources into streaming services like TIDAL.

## Scripts

### PDF Tools

| Script | Description | Dependencies |
| :--- | :--- | :--- |
| `pdf_convert_mathpix_sdk.py` | Converts local PDFs to Markdown using the `mpxpy` library. | `mpxpy` |
| `pdf_convert_mathpix_api.py` | Similar conversion tool using direct HTTP requests to Mathpix API. | `requests` |
| `pdf_convert_marker.py` | Converts PDFs to Markdown using the `marker-pdf` local model. | `marker-pdf` |

### Audio & Music Tools

| Script | Description | Dependencies |
| :--- | :--- | :--- |
| `audio_transcribe_openai.sh` | Bash script wrapper for OpenAI's `/v1/audio/transcriptions` API. | `ffmpeg`, `curl`, `jq` |
| `audio_transcribe_whisper.py` | Local pipeline (ASR + Diarization) using `whisper-cpp` and `pyannote`. | `torch`, `pyannote.audio`, `ffmpeg`, `whisper-cpp` |
| `tidal_import_page_to_playlist.py` | Imports classical albums from Gramophone MHTML/MD to TIDAL playlists. | `beautifulsoup4`, `requests`, `lxml` |

## Environment Setup

### Environment Variables
Ensure the following variables are set depending on the tools you use:

- **Mathpix Tools** (`pdf_convert_mathpix_sdk.py`, `pdf_convert_mathpix_api.py`):
    - `MATHPIX_APP_ID`: Your Mathpix Application ID.
    - `MATHPIX_APP_KEY`: Your Mathpix Application Key.
- **OpenAI Transcription** (`audio_transcribe_openai.sh`):
    - `OPENAI_API_KEY`: OpenAI API Key.
- **TIDAL Import** (`tidal_import_page_to_playlist.py`):
    - `TIDAL_CLIENT_ID`: Your TIDAL Developer Client ID (Required).
    - `TIDAL_CLIENT_SECRET`: TIDAL Client Secret (Optional).
    - `TIDAL_COUNTRY_CODE`: Two-letter country code (e.g., `US`).
- **Local Diarization** (`audio_transcribe_whisper.py`):
    - `HF_TOKEN`: HuggingFace token (for `pyannote` models).
    - `GGML_METAL_PATH_RESOURCES`: (Optional, macOS) Path to `whisper-cpp` Metal resources.

### Python Dependencies
Install required packages:
```bash
pip install mpxpy requests torch pyannote.audio beautifulsoup4 lxml marker-pdf
```

### System Tools
- **ffmpeg**: Required for audio processing (`brew install ffmpeg`).
- **whisper-cpp**: Required for `audio_transcribe_whisper.py` (`brew install whisper-cpp`).
- **jq**: Recommended for `audio_transcribe_openai.sh` (`brew install jq`).

## Usage Examples

### PDF to Markdown
```bash
# Using mpxpy wrapper (via pipx)
pipx run ./pdf_convert_mathpix_sdk.py input.pdf -o output.md

# Using Marker (local)
pipx run ./pdf_convert_marker.py input.pdf -o output.md
```

### Audio Transcription (OpenAI API)
```bash
# Default model (whisper-1)
./audio_transcribe_openai.sh recording.m4a
```

### Audio Transcription & Diarization (Local)
```bash
# Basic usage
./audio_transcribe_whisper.py interview.m4a --num-speakers 2
```

### TIDAL Playlist Import
```bash
# Dry run to check matches
./tidal_import_page_to_playlist.py gramophone_review.mhtml --dry-run

# Full import
./tidal_import_page_to_playlist.py gramophone_review.md
```

# Project Context: Utils

## Overview
This directory contains a collection of standalone utility scripts primarily focused on two domains:
1.  **PDF to Markdown Conversion**: Tools to convert PDF documents (especially scientific papers) into Markdown with LaTeX math support using the Mathpix API.
2.  **Audio Transcription & Diarization**: Tools to transcribe audio files using either the OpenAI API or a robust local pipeline combining `whisper-cpp` and `pyannote.audio`.

## Scripts

### PDF Tools

| Script | Description | Dependencies |
| :--- | :--- | :--- |
| `pdf_convert_mathpix_sdk.py` | Converts local PDFs to Markdown using the `mpxpy` library. | `mpxpy` |
| `pdf_convert_mathpix_api.py` | Similar conversion tool using direct HTTP requests to Mathpix API. | `requests` |

### Audio Tools

| Script | Description | Dependencies |
| :--- | :--- | :--- |
| `audio_transcribe_openai.sh` | Bash script wrapper for OpenAI's `/v1/audio/transcriptions` API. Handles large files by downsampling/checking size. | `ffmpeg`, `curl`, `jq` |
| `audio_transcribe_whisper.py` | Advanced local pipeline. Converts audio to mono 16kHz, runs `whisper-cpp` for ASR, `pyannote` for speaker diarization, and merges results. | `torch`, `pyannote.audio`, `ffmpeg`, `whisper-cpp` |

## Environment Setup

### Environment Variables
Ensure the following variables are set depending on the tools you use:

- **Mathpix Tools** (`pdf_convert_mathpix_sdk.py`, `pdf_convert_mathpix_api.py`):
    - `MATHPIX_APP_ID`: Your Mathpix Application ID.
    - `MATHPIX_APP_KEY`: Your Mathpix Application Key.
- **OpenAI Transcription** (`audio_transcribe_openai.sh`):
    - `OPENAI_API_KEY`: OpenAI API Key.
- **Local Diarization** (`audio_transcribe_whisper.py`):
    - `HF_TOKEN`: HuggingFace token (for `pyannote` models).
    - `GGML_METAL_PATH_RESOURCES`: (Optional, macOS) Path to `whisper-cpp` Metal resources.

### Python Dependencies
Install required packages:
```bash
pip install mpxpy requests torch pyannote.audio
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

# Using direct requests wrapper
./pdf_convert_mathpix_api.py input.pdf
```

### Audio Transcription (OpenAI API)
```bash
# Default model (whisper-1)
./audio_transcribe_openai.sh recording.m4a

# Specific model
./audio_transcribe_openai.sh --model gpt-4o-transcribe recording.m4a output.txt
```

### Audio Transcription & Diarization (Local)
```bash
# Basic usage
./audio_transcribe_whisper.py interview.m4a

# With speaker names and specific number of speakers
./audio_transcribe_whisper.py interview.m4a --speakers "Interviewer,Guest" --num-speakers 2
```
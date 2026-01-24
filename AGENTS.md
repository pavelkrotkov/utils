# Agents Guide for utils

This repository is a small collection of standalone utility scripts. There is no
single build system, no automated test suite, and no lint config. Use the notes
below to run scripts safely and follow the established style.

Project focus areas:
- PDF to Markdown conversion for scientific papers (Mathpix API).
- Audio transcription and diarization (OpenAI API or local whisper-cpp + pyannote).

If you need broader project context, read `GEMINI.md`.

-------------------------------------------------------------------------------
Build / Lint / Test Commands
-------------------------------------------------------------------------------

There is no formal build step. Scripts are executed directly.

Python environment setup (manual):
- `pip install mpxpy marker-pdf requests torch pyannote.audio`

System dependencies (manual):
- `brew install ffmpeg whisper-cpp jq`

Run scripts (examples):
- `pipx run ./pdf_convert_mathpix_sdk.py input.pdf -o output.md`
- `pipx run ./pdf_convert_mathpix_api.py input.pdf -o output.md`
- `pipx run ./pdf_convert_marker.py input.pdf -o output.md`
- `./audio_transcribe_openai.sh recording.m4a output.txt`
- `./audio_transcribe_whisper.py interview.m4a --num-speakers 2`

Single-test guidance:
- There is no test harness.
- Use a small fixture file to validate behavior, e.g.
  `./audio_transcribe_openai.sh sample.m4a sample.txt`
- For quick syntax checks on Python scripts: `python3 -m py_compile *.py`

-------------------------------------------------------------------------------
Operational Notes (from existing docs)
-------------------------------------------------------------------------------

Environment variables:
- `MATHPIX_APP_ID`, `MATHPIX_APP_KEY` for Mathpix tools.
- `OPENAI_API_KEY` for OpenAI transcription.
- `HF_TOKEN` for pyannote diarization.
- `GGML_METAL_PATH_RESOURCES` is optional for whisper-cpp Metal support.

Repository scripts:
- `pdf_convert_mathpix_sdk.py` uses the `mpxpy` SDK.
- `pdf_convert_mathpix_api.py` uses direct HTTP requests with `requests`.
- `pdf_convert_marker.py` uses `marker-pdf` for simpler PDFs (local conversion).
- `audio_transcribe_openai.sh` uses OpenAI's `/v1/audio/transcriptions` API and can downsample large files.
- `audio_transcribe_whisper.py` runs a local whisper-cpp + pyannote pipeline (mono 16kHz conversion + diarization merge).

Repository layout:
- Each script is standalone and intended to be run directly.
- Outputs are typically written next to the input file unless overridden.
- No shared modules or packages; avoid introducing cross-script imports.

Execution tips:
- Ensure scripts are executable (`chmod +x <script>` if needed).
- Use absolute paths when invoking tools from other directories.
- Prefer small sample inputs to validate changes before large runs.

-------------------------------------------------------------------------------
Code Style Guidelines
-------------------------------------------------------------------------------

General
- Keep scripts standalone; avoid cross-file imports.
- Keep changes minimal and focused; do not add dependencies unless necessary.
- Favor clarity over cleverness; explicit control flow is preferred.
- Keep output user-friendly and CLI-oriented; print concise status messages.

Python
- Use Python 3 with `#!/usr/bin/env python3` shebang at top of scripts.
- Standard library imports first, then third-party, then local (if any).
- Use 4-space indentation and PEP 8 style for spacing and line breaks.
- Use `Path` from `pathlib` for filesystem paths when practical.
- Prefer `snake_case` for functions and variables.
- Use `UPPER_SNAKE_CASE` for constants.
- Use `PascalCase` for class names.
- Add type hints in new code when it improves clarity.
- Keep module docstrings at the top for multi-step scripts.
- Keep CLI interfaces in a `main()` function with `argparse`.

Shell
- Use `#!/usr/bin/env bash` and `set -euo pipefail` for safety.
- Quote variables and file paths.
- Use arrays for lists of supported options, as in `audio_transcribe_openai.sh`.

Formatting
- Keep lines reasonably short; wrap long argument lists or strings.
- Prefer f-strings over concatenation for readable messages.
- Use `print(..., file=sys.stderr)` for errors and warnings.

Imports
- Keep unused imports out; remove anything not used.
- Avoid wildcard imports.
- When adding new dependencies, update the guidance in this file.

Types and Data
- Use `dict`, `list`, and `Optional` from `typing` when clarifying structure.
- Normalize data structures before processing (see JSON parsing patterns).
- Use `Path` objects for file IO, convert to `str` for subprocess calls.

Naming Conventions
- Use descriptive, action-based function names (e.g., `run_whisper`).
- Keep CLI flags lower-case with hyphens (`--num-speakers`).
- Use `*_path` suffix for path variables.
- Use `*_bin` suffix for binary paths (`ffmpeg_bin`, `whisper_bin`).

Error Handling
- Validate inputs early (files exist, environment variables set).
- For user errors: print a clear message and exit with non-zero status.
- For subprocess errors: catch `CalledProcessError` and surface context.
- Avoid swallowing exceptions silently; log and exit when failing.
- For external APIs: check response status codes and report failures.

CLI Behavior
- Provide `--help` coverage for new flags.
- Keep defaults sensible and derived from common use cases.
- When adding flags, update usage examples in the docstring.

I/O
- When writing output files, be explicit about encoding (UTF-8).
- Prefer deterministic file naming (derive from input names).

Logging
- Use short INFO/WARNING/ERROR prefixes for multi-step scripts.
- Provide a `--verbose` option for noisy output when appropriate.

Process and IO hygiene
- Clean up temporary files in `finally` blocks or via `trap` in shell scripts.
- Avoid writing to the project root unless it is the intended output location.
- Use `tempfile` for intermediate artifacts and delete unless `--keep-temp`.

API and Network Usage
- Time out network requests where practical and report failures clearly.
- Include response context (status code, message) when surfacing errors.
- Avoid retry loops without backoff; keep retries explicit and bounded.

CLI UX
- Keep help text up to date with current flags and defaults.
- Print success messages with output paths to make results discoverable.
- Keep exit codes non-zero for failures (use `sys.exit(1)` or `exit 1`).

-------------------------------------------------------------------------------
Agent Notes
-------------------------------------------------------------------------------

There are no Cursor or Copilot rule files in this repo.
If you add one in the future, mirror it here and keep this file updated.

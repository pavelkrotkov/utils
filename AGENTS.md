# Agents Guide for utils

This repository is a small collection of standalone utility scripts. There is no
single build system, no automated test suite, and no lint config. Use the notes
below to run scripts safely and follow the established style.

Project focus areas:
- PDF to Markdown conversion for scientific papers (Mathpix SDK).
- Audio transcription (OpenAI API, local whisper-cpp with optional pyannote diarization, or MLX VibeVoice-ASR on Apple Silicon).
- TIDAL playlist import from classical music review pages (Gramophone-style MHTML/MD).

If you need broader project context, read `GEMINI.md`.

-------------------------------------------------------------------------------
Build / Lint / Test Commands
-------------------------------------------------------------------------------

There is no formal build step. Scripts are executed directly; run Python scripts via `uv run` (avoid `python3` for normal runs).

Linting (optional):
- `ruff check .`
- `ruff format --check .`

Python environment setup:
- Dependencies are declared inline (PEP 723) and resolved automatically by `uv run`. No manual install needed.

System dependencies (manual):
- `brew install ffmpeg whisper-cpp jq`

Run scripts (examples):
- `uv run ./pdf_convert_mathpix_sdk.py input.pdf -o output.md`
- `uv run ./pdf_convert_marker.py input.pdf -o output.md`
- `uv run ./pdf_convert_docling.py input.pdf -o output.md`
- `uv run ./pdf_convert_llamaparse.py input.pdf -o output.md`
- `uv run ./pdf_convert_pymupdf4llm.py input.pdf -o output.md`
- `uv run ./tidal_import_page_to_playlist.py test.md --dry-run`
- `uv run ./audio_folder_to_m4b.py "/path/to/Audiobook Collection" --dry-run`
- `./audio_transcribe_openai.sh recording.m4a output.txt`
- `uv run ./audio_transcribe_whisper.py interview.m4a`
- `uv run ./audio_transcribe_whisper.py interview.m4a --diarization --num-speakers 2`
- `uv run ./audio_transcribe_vibevoice.py interview.m4a`

Single-test guidance:
- There is no test harness.
- Use a small fixture file to validate behavior, e.g.
  `./audio_transcribe_openai.sh sample.m4a sample.txt`
- For quick syntax checks on Python scripts: run via `uv run` with `--help` or a small fixture input.

-------------------------------------------------------------------------------
Operational Notes (from existing docs)
-------------------------------------------------------------------------------

Environment variables:
- `MATHPIX_APP_ID`, `MATHPIX_APP_KEY` for Mathpix. `MATHPIX_API_KEY` can be used as an app-key fallback.
- `LLAMA_CLOUD_API_KEY` for LlamaParse (LlamaCloud).
- `OPENAI_API_KEY` for OpenAI transcription.
- `TIDAL_CLIENT_ID`, `TIDAL_CLIENT_SECRET` for TIDAL import.
- `HF_TOKEN` for optional pyannote diarization.
- `GGML_METAL_PATH_RESOURCES` is optional for whisper-cpp Metal support.

LlamaCloud API key setup:
- Create a key at https://cloud.llamaindex.ai via API Key -> Generate New Key.

Repository scripts:
- `pdf_convert_mathpix_sdk.py` uses the `mpxpy` SDK.
- `pdf_convert_marker.py` uses `marker-pdf` for simpler PDFs (local conversion).
- `pdf_convert_docling.py` uses Docling for local Markdown conversion.
- `pdf_convert_llamaparse.py` uses LlamaParse (LlamaCloud) for hosted parsing.
- `pdf_convert_pymupdf4llm.py` uses PyMuPDF4LLM for local Markdown conversion.
- `audio_folder_to_m4b.py` converts folders of audio tracks into chaptered M4B audiobooks via ffmpeg (one chapter per track, natural-sorted, with embedded cover art); reuses `probe_media_duration` from `audio_common.py` and skips unreadable/zero-length tracks.
- `audio_transcribe_openai.sh` uses OpenAI's `/v1/audio/transcriptions` API and can downsample large files.
- `audio_transcribe_whisper.py` runs a local whisper-cpp pipeline (mono 16kHz conversion + plain transcript by default). Use `--diarization` to add pyannote speaker diarization and merge speaker labels.
- `audio_transcribe_vibevoice.py` uses `mlx-audio` with VibeVoice-ASR on Apple Silicon, defaults to `mlx-community/VibeVoice-ASR-4bit`, and writes native structured JSON by default.
- `tidal_import_page_to_playlist.py` imports classical albums from MHTML/MD files to TIDAL playlists using API v2.
- `test_matching.py` helper for testing TIDAL matching (see below).

Repository layout:
- Each script is standalone and intended to be run directly.
- Outputs are typically written next to the input file unless overridden.
- Prefer standalone scripts for simple one-off utilities.
- Extract a shared module when duplication is producing bugs or divergent semantics;
  keep shared modules narrow, local to the affected script cluster, and CLI-friendly.

Execution tips:
- Ensure scripts are executable (`chmod +x <script>` if needed).
- Use absolute paths when invoking tools from other directories.
- Prefer small sample inputs to validate changes before large runs.

-------------------------------------------------------------------------------
Code Style Guidelines
-------------------------------------------------------------------------------

General
- Prefer standalone scripts; use focused shared modules when they prevent duplicated
  behavior from drifting across related scripts.
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
TIDAL Matching Test Helper
-------------------------------------------------------------------------------

`test_matching.py` is a wrapper for testing and finetuning the TIDAL matching logic.

What it does:
- Runs `tidal_import_page_to_playlist.py --dry-run` on any input file
- Parses output to extract match/no-match results and scores
- Shows summary: total matched, NO MATCH list, score distribution, low-score warnings
- Can compare results against a saved baseline to detect regressions

Usage:
```bash
# Basic test - shows summary
uv run ./test_matching.py input.mhtml

# Save baseline before making changes
uv run ./test_matching.py input.mhtml --save baseline.log

# After changes, compare to detect regressions
uv run ./test_matching.py input.mhtml --compare baseline.log
```

Reusability notes:
- Useful for finetuning matching logic; baseline comparison workflow makes it easy
  to verify changes don't break existing matches.
- Limitation: parses text output using regex, so if log format changes (e.g.,
  different wording for "MATCH:" or "NO MATCH:"), the parsing would break.
- Alternative: same functionality could be built into the main script as a
  `--test-report` flag for more robustness.

Test fixtures:
- `test_2.md` contains 24 problem albums for quick iteration during finetuning.

-------------------------------------------------------------------------------
Agent Notes
-------------------------------------------------------------------------------

There are no Cursor or Copilot rule files in this repo.
If you add one in the future, mirror it here and keep this file updated.

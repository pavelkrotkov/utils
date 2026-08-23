# Contributing to utils

Thank you for contributing to `utils`! This document outlines how to set up your environment, run checks, and format your code.

## Development Setup

We use `uv` for dependency management. To set up your local development environment:

```bash
# Install dependencies, developer tools, and create a virtual environment
uv sync --dev

# Install git pre-commit hooks
uv run pre-commit install
```

## Quality and Verification Checks

Before pushing your changes, please run the following checks locally:

### 1. Run Tests
Ensure all existing tests pass:
```bash
uv run pytest
```

### 2. Lint and Format Code
We use `ruff` to keep the codebase clean and formatted:
```bash
# Check for lint issues and apply automatic fixes where safe
uv run ruff check .

# Check code formatting
uv run ruff format --check .

# To automatically apply formatting:
uv run ruff format .
```

### 3. Type Checking
We use `ty` for lightweight static type checks:
```bash
uv run ty check \
  --exclude "tidal_pipeline" \
  --exclude "tidal_match_from_json.py" \
  --exclude "test_tidal_search_backend.py" \
  --exclude "factor_fund_performance.py" \
  --exclude "audio_transcribe_vibevoice.py" \
  --exclude "audio_transcribe_whisper.py" \
  --exclude "audio_transcript.py" \
  --exclude "pdf_convert_llamaparse.py" \
  --exclude "test_audio_common.py" \
  .
```

Specialized scripts keep heavyweight or provider-specific dependencies in their inline PEP 723 metadata. Run those scripts directly with `uv run ./script_name.py ...` so `uv` resolves only the dependencies needed for that tool.

### 4. Security Audits
You can run dependency audits locally using:
```bash
uv run pip-audit
```

### 5. Running all pre-commit hooks manually
You can run the checks on all files in the repository:
```bash
uv run pre-commit run --all-files
```

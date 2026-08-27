#!/usr/bin/env python3
"""Shared helpers for PDF conversion scripts."""

from __future__ import annotations

import importlib
import re
import shutil
import sys
import urllib.parse
from pathlib import Path
from types import ModuleType

MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)")
HTML_IMG_SRC_RE = re.compile(r"<img[^>]+src=\"([^\"]+)\"", re.IGNORECASE)


def collect_local_references(markdown_text: str) -> list[str]:
    """Return unique local file references from Markdown links and image tags."""
    references: list[str] = []
    seen: set[str] = set()
    matches = list(MARKDOWN_LINK_RE.finditer(markdown_text))
    matches.extend(HTML_IMG_SRC_RE.finditer(markdown_text))

    for match in matches:
        raw_reference = urllib.parse.unquote(match.group(1))
        reference = raw_reference.split("#", 1)[0].split("?", 1)[0]
        if not reference or reference in seen:
            continue
        if reference.lower().startswith(("http://", "https://", "data:", "file:")):
            continue
        seen.add(reference)
        references.append(reference)
    return references


def copy_referenced_assets(
    markdown_text: str,
    source_dir: Path,
    target_dir: Path,
) -> list[Path]:
    """Copy files referenced by relative links next to the output Markdown.

    Returns the list of copied paths. References outside ``source_dir`` are
    ignored, and existing files at the destination are overwritten so a
    reconversion never pairs new Markdown with stale assets.
    """
    copied: list[Path] = []
    resolved_source_dir = source_dir.resolve()

    for reference in collect_local_references(markdown_text):
        candidate = (source_dir / reference).resolve()
        try:
            relative_path = candidate.relative_to(resolved_source_dir)
        except ValueError:
            continue
        if not candidate.is_file():
            continue

        target_path = target_dir / relative_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(candidate, target_path)
        copied.append(target_path)
    return copied


def find_generated_markdown(directory: Path) -> Path | None:
    """Return the single Markdown file generated under a directory tree."""
    candidates = sorted(path for path in directory.rglob("*.md") if path.is_file())
    if len(candidates) == 1:
        return candidates[0]
    return None


def resolve_output_path(
    input_path: Path,
    output_path: Path | None,
    output_dir: Path | None,
) -> Path:
    """Resolve a Markdown output path and ensure its parent directory exists."""
    if output_path is not None:
        if output_path.resolve() == input_path.resolve():
            print(
                f"ERROR: Output path must differ from the input PDF: {output_path}",
                file=sys.stderr,
            )
            sys.exit(1)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        return output_path

    resolved_dir = output_dir or input_path.parent
    resolved_dir.mkdir(parents=True, exist_ok=True)
    return resolved_dir / f"{input_path.stem}.md"


def require_pdf_path(pdf_path: str | Path) -> Path:
    """Return a valid PDF path or exit with a CLI-oriented error."""
    resolved_path = Path(pdf_path)
    if not resolved_path.exists() or not resolved_path.is_file():
        print(f"ERROR: PDF file not found: {resolved_path}", file=sys.stderr)
        sys.exit(1)
    if resolved_path.suffix.lower() != ".pdf":
        print(f"ERROR: Input file must be a PDF: {resolved_path}", file=sys.stderr)
        sys.exit(1)
    return resolved_path


def import_or_die(module_name: str, install_package: str) -> ModuleType:
    """Import an optional dependency or exit with a consistent install hint."""
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:
        print(f"ERROR: Missing required Python package: {exc}", file=sys.stderr)
        print(f"Install with: pip install {install_package}", file=sys.stderr)
        sys.exit(1)

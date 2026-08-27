#!/usr/bin/env python3
"""One conversion run behind one interface, with the backend as the adapter.

Every ``pdf_convert_*.py`` script used to re-orchestrate the same steps: build
an argument parser, validate the input path, resolve the output path, parse a
page range, invoke a library, find whatever Markdown it produced, copy the
assets that Markdown references, write the result, and report errors. Only the
library invocation actually differed.

This module owns the run. A :class:`Backend` supplies the parts that vary:

    ``add_arguments``   backend-specific CLI flags
    ``validate``        pre-flight checks, before any heavy import
    ``convert``         hand the PDF to the library, return what it produced

``convert`` returns one of three outcomes, and the module takes it from there:

    :class:`MarkdownText`       text in hand; the module writes it out
    :class:`MarkdownDirectory`  the library wrote somewhere; the module finds
                                the Markdown, copies its assets, writes it out
    :class:`AlreadyWritten`     the library wrote the output path itself

Backends raise :class:`ConversionError` instead of exiting, so the whole run is
exercisable through :func:`run` with a fake backend and no PDF library
installed. :func:`execute` is the CLI wrapper that turns those errors into
``ERROR:`` lines and an exit code.
"""

from __future__ import annotations

import argparse
import importlib
import re
import shutil
import sys
import tempfile
import urllib.parse
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from pdf_page_selection import (
    PAGE_RANGE_HELP,
    PageSelection,
    PageSelectionError,
    load_page_count,
)

MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)")
HTML_IMG_SRC_RE = re.compile(r"<img[^>]+src=\"([^\"]+)\"", re.IGNORECASE)


class ConversionError(Exception):
    """A conversion run cannot continue. Reported as ``ERROR: <message>``."""


# --------------------------------------------------------------------------
# Outcomes
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MarkdownText:
    """The backend produced Markdown text directly."""

    text: str


@dataclass(frozen=True)
class MarkdownDirectory:
    """The backend wrote Markdown (and possibly assets) under ``directory``.

    ``expected_stem`` names the file the backend is expected to have written;
    when it is missing, a single unambiguous ``*.md`` anywhere below the
    directory is accepted instead.
    """

    directory: Path
    expected_stem: str | None = None


@dataclass(frozen=True)
class AlreadyWritten:
    """The backend wrote the output itself.

    ``path`` names the file it actually wrote, for backends that choose their
    own extension: marker always writes ``<stem>.md`` even when ``-o`` asked
    for another suffix. Defaults to the requested output path.
    """

    path: Path | None = None


Outcome = MarkdownText | MarkdownDirectory | AlreadyWritten


# --------------------------------------------------------------------------
# Request and backend
# --------------------------------------------------------------------------


@dataclass
class ConversionRequest:
    """Everything a backend needs for one run."""

    pdf_path: Path
    output_path: Path
    workspace: Path
    args: argparse.Namespace
    selection: PageSelection | None = None
    _page_count: int | None = None

    def page_count(self) -> int:
        """Return the document's page count, reading the PDF at most once.

        Raises:
            ConversionError: the PDF cannot be read.
        """
        if self._page_count is None:
            if self.selection is not None:
                self._page_count = self.selection.page_count
            else:
                try:
                    self._page_count = load_page_count(self.pdf_path)
                except PageSelectionError as exc:
                    raise ConversionError(str(exc)) from exc
        return self._page_count


class Backend:
    """The adapter at the seam: one PDF library, one set of flags.

    Subclasses override :meth:`add_arguments`, :meth:`validate` and
    :meth:`convert`. Everything else about a run is the module's business.
    """

    name = "backend"
    description = "Convert a PDF to Markdown."
    epilog: str | None = None
    #: LlamaParse accepts ``--fetch-job`` with no PDF, so it opts out.
    pdf_path_required = True
    #: Mathpix exposes no page argument, so it never offers ``--page-range``.
    supports_page_selection = True
    #: Appended to the shared --page-range help, e.g. MinerU's contiguous-only rule.
    page_range_caveat: str | None = None

    def page_range_help(self) -> str:
        """Return the shared ``--page-range`` help, plus any backend caveat."""
        if self.page_range_caveat:
            return f"{PAGE_RANGE_HELP} {self.page_range_caveat}"
        return PAGE_RANGE_HELP

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        """Declare backend-specific flags."""

    def validate(self, args: argparse.Namespace) -> None:
        """Check flags and the environment before any heavy import.

        Raises:
            ConversionError: the run cannot proceed.
        """

    def convert(self, request: ConversionRequest) -> Outcome:
        """Hand the PDF to the library and report what it produced.

        Raises:
            ConversionError: the conversion failed.
        """
        raise NotImplementedError


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------


def build_parser(backend: Backend) -> argparse.ArgumentParser:
    """Build the shared CLI skeleton plus the backend's own flags."""
    parser = argparse.ArgumentParser(
        description=backend.description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=backend.epilog,
    )
    if backend.pdf_path_required:
        parser.add_argument("pdf_path", type=Path, help="Path to the input PDF file")
    else:
        parser.add_argument(
            "pdf_path",
            type=Path,
            nargs="?",
            help="Path to the input PDF file",
        )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output markdown file path (default: same name as PDF with .md)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Directory to write output files (defaults to input file directory)",
    )
    if backend.supports_page_selection:
        parser.add_argument("--page-range", help=backend.page_range_help())
    backend.add_arguments(parser)
    return parser


def run(backend: Backend, args: argparse.Namespace) -> Path:
    """Execute one conversion run and return the written output path.

    Raises:
        ConversionError: validation, conversion, or writing failed.
    """
    backend.validate(args)

    pdf_path = require_pdf_path(args.pdf_path)
    output_path = resolve_output_path(pdf_path, args.output, getattr(args, "output_dir", None))

    selection = None
    page_range = getattr(args, "page_range", None)
    if page_range:
        try:
            selection = PageSelection.parse(page_range, load_page_count(pdf_path))
        except PageSelectionError as exc:
            raise ConversionError(str(exc)) from exc

    with tempfile.TemporaryDirectory(prefix=f"{backend.name}_") as workspace_raw:
        request = ConversionRequest(
            pdf_path=pdf_path,
            output_path=output_path,
            workspace=Path(workspace_raw),
            args=args,
            selection=selection,
        )
        outcome = backend.convert(request)
        copied_assets = _materialize(backend, request, outcome)

    if isinstance(outcome, AlreadyWritten) and outcome.path is not None:
        output_path = outcome.path

    # A backend that writes its own output can silently write elsewhere; never
    # announce a file that is not there.
    if not output_path.is_file():
        raise ConversionError(
            f"{backend.name} reported an output that does not exist: {output_path}"
        )

    for asset_path in copied_assets:
        print(f"INFO: Copied referenced asset: {asset_path}")
    print(f"Wrote Markdown to: {output_path}")
    return output_path


def execute(backend: Backend, argv: Sequence[str] | None = None) -> int:
    """Parse arguments, run, and turn failures into ``ERROR:`` lines.

    Returns the process exit code.
    """
    parser = build_parser(backend)
    args = parser.parse_args(argv)
    try:
        run(backend, args)
    except ConversionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


def _materialize(backend: Backend, request: ConversionRequest, outcome: Outcome) -> list[Path]:
    """Turn a backend outcome into the written output file. Returns copied assets."""
    if isinstance(outcome, AlreadyWritten):
        return []

    if isinstance(outcome, MarkdownText):
        _write_output(request.output_path, outcome.text)
        return []

    if isinstance(outcome, MarkdownDirectory):
        generated_md = locate_generated_markdown(outcome.directory, outcome.expected_stem)
        if generated_md is None:
            raise ConversionError(
                f"{backend.name} did not produce a Markdown file in: {outcome.directory}"
            )
        try:
            markdown_text = generated_md.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConversionError(f"Unable to read generated Markdown: {exc}") from exc
        copied = copy_referenced_assets(
            markdown_text,
            generated_md.parent,
            request.output_path.parent,
        )
        _write_output(request.output_path, markdown_text)
        return copied

    raise ConversionError(f"{backend.name} returned an unsupported outcome: {outcome!r}")


def _write_output(output_path: Path, markdown_text: str) -> None:
    try:
        output_path.write_text(markdown_text, encoding="utf-8")
    except OSError as exc:
        raise ConversionError(f"Unable to write Markdown output: {exc}") from exc


# --------------------------------------------------------------------------
# Shared implementation
# --------------------------------------------------------------------------


def require_pdf_path(pdf_path: str | Path | None) -> Path:
    """Return a readable PDF path.

    Raises:
        ConversionError: the path is missing, absent, or not a PDF.
    """
    if pdf_path is None:
        raise ConversionError("PDF file not provided.")
    resolved_path = Path(pdf_path)
    if not resolved_path.exists() or not resolved_path.is_file():
        raise ConversionError(f"PDF file not found: {resolved_path}")
    if resolved_path.suffix.lower() != ".pdf":
        raise ConversionError(f"Input file must be a PDF: {resolved_path}")
    return resolved_path


def resolve_output_path(
    input_path: Path,
    output_path: Path | None,
    output_dir: Path | None,
) -> Path:
    """Resolve a Markdown output path and ensure its parent directory exists.

    Raises:
        ConversionError: the output would overwrite the input PDF, or its
            directory cannot be created.
    """
    try:
        if output_path is not None:
            if output_path.resolve() == input_path.resolve():
                raise ConversionError(f"Output path must differ from the input PDF: {output_path}")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            return output_path

        resolved_dir = output_dir or input_path.parent
        resolved_dir.mkdir(parents=True, exist_ok=True)
        return resolved_dir / f"{input_path.stem}.md"
    except OSError as exc:
        raise ConversionError(f"Unable to prepare the output directory: {exc}") from exc


def locate_generated_markdown(directory: Path, expected_stem: str | None = None) -> Path | None:
    """Find the Markdown a backend generated under ``directory``.

    Prefers ``<expected_stem>.md`` at the top level; otherwise accepts a single
    unambiguous ``*.md`` anywhere below. Paths that escape ``directory`` (via a
    symlink, say) are refused.
    """
    resolved_dir = directory.resolve()

    if expected_stem is not None:
        expected_path = directory / f"{expected_stem}.md"
        if expected_path.is_file() and _is_within(expected_path, resolved_dir):
            return expected_path

    candidates = sorted(path for path in directory.rglob("*.md") if path.is_file())
    if len(candidates) != 1:
        return None
    return candidates[0] if _is_within(candidates[0], resolved_dir) else None


def _is_within(path: Path, resolved_dir: Path) -> bool:
    try:
        path.resolve().relative_to(resolved_dir)
    except ValueError:
        return False
    return True


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


def require_module(module_name: str, install_package: str) -> ModuleType:
    """Import an optional dependency.

    Raises:
        ConversionError: the dependency is not installed.
    """
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:
        raise ConversionError(
            f"Missing required Python package: {exc}. Install with: pip install {install_package}"
        ) from exc

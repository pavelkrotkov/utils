#!/usr/bin/env python3
"""One page-selection module for the PDF converters.

A ``--page-range`` spec is parsed once into a :class:`PageSelection`. Each
converter then asks for the projection its backend can honour instead of
re-implementing an indexing convention:

    ``as_one_based``      Docling, OpenDataLoader, LlamaParse
    ``as_zero_based``     marker, PyMuPDF4LLM
    ``as_contiguous``     MinerU (rejects gapped selections)
    ``as_ranges``         API page specs, e.g. ``"1-5,9"``
    ``as_extracted_pdf``  PaddleOCR-VL (backend has no page argument)

``parse`` takes an explicit page count rather than reading the PDF itself, so
converters that already have a document open (PyMuPDF4LLM) do not pull in a
second PDF library. :func:`load_page_count` is the pypdf-backed convenience for
the rest.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

PAGE_RANGE_HELP = (
    "Comma-separated 1-based page numbers or ranges. Examples: 1-5, 1,3,5-10, 5-N (N = last page)."
)


class PageSelectionError(ValueError):
    """A page-range spec is malformed, or a projection cannot honour it."""


def _load_pypdf() -> ModuleType:
    """Import pypdf lazily; it is a per-script PEP 723 dependency, not a project one."""
    try:
        return importlib.import_module("pypdf")
    except ImportError as exc:  # pragma: no cover - install-time failure
        raise PageSelectionError(
            f"Missing required Python package: {exc}. Install with: pip install pypdf"
        ) from exc


def load_page_count(pdf_path: Path) -> int:
    """Return the page count of a PDF, using pypdf.

    Raises:
        PageSelectionError: pypdf is unavailable, or the PDF cannot be read.
    """
    pypdf = _load_pypdf()

    try:
        reader = pypdf.PdfReader(str(pdf_path))
        return len(reader.pages)
    except Exception as exc:
        raise PageSelectionError(f"Unable to read PDF pages: {exc}") from exc


@dataclass(frozen=True)
class PageSelection:
    """A validated set of 1-based page numbers within a document.

    Invariants held by the module, not by callers: pages are sorted, unique,
    at least one page is selected, and every page lies within ``page_count``.
    """

    pages: tuple[int, ...]
    page_count: int

    @classmethod
    def parse(cls, spec: str, page_count: int) -> PageSelection:
        """Parse a ``--page-range`` spec against a document of ``page_count`` pages."""
        if page_count < 1:
            raise PageSelectionError("Invalid --page-range value: PDF has no pages.")

        pages: list[int] = []
        for raw_part in spec.split(","):
            part = raw_part.strip()
            if not part:
                raise PageSelectionError("Invalid --page-range value: empty page range item.")
            if part.count("-") > 1:
                raise PageSelectionError("Invalid --page-range value: malformed page range.")
            if "-" in part:
                start_raw, end_raw = part.split("-", 1)
                if not start_raw.strip() or not end_raw.strip():
                    raise PageSelectionError("Invalid --page-range value: malformed page range.")
                start = _parse_page_token(start_raw.strip(), page_count)
                end = _parse_page_token(end_raw.strip(), page_count)
                if end < start:
                    raise PageSelectionError(
                        "Invalid --page-range value: range end is before start."
                    )
                pages.extend(range(start, end + 1))
            else:
                pages.append(_parse_page_token(part, page_count))

        if not pages:
            raise PageSelectionError("Invalid --page-range value: no pages selected.")
        return cls.from_pages(pages, page_count)

    @classmethod
    def from_pages(cls, pages: list[int] | tuple[int, ...], page_count: int) -> PageSelection:
        """Build a selection from already-known 1-based page numbers."""
        if page_count < 1:
            raise PageSelectionError("Invalid --page-range value: PDF has no pages.")

        ordered = tuple(sorted(set(pages)))
        if not ordered:
            raise PageSelectionError("Invalid --page-range value: no pages selected.")
        if ordered[0] < 1:
            raise PageSelectionError("Invalid --page-range value: pages are 1-based.")
        if ordered[-1] > page_count:
            raise PageSelectionError("Invalid --page-range value: page is outside the document.")
        return cls(pages=ordered, page_count=page_count)

    def __len__(self) -> int:
        return len(self.pages)

    def as_one_based(self) -> list[int]:
        """Return the selected pages numbered from 1."""
        return list(self.pages)

    def as_zero_based(self) -> list[int]:
        """Return the selected pages indexed from 0."""
        return [page - 1 for page in self.pages]

    def as_range_pairs(self, *, zero_based: bool = False) -> list[tuple[int, int]]:
        """Return the selection as inclusive ``(start, end)`` runs."""
        offset = 1 if zero_based else 0
        pairs: list[tuple[int, int]] = []
        start = previous = self.pages[0]
        for page in self.pages[1:]:
            if page == previous + 1:
                previous = page
                continue
            pairs.append((start - offset, previous - offset))
            start = previous = page
        pairs.append((start - offset, previous - offset))
        return pairs

    def as_ranges(self, *, zero_based: bool = False) -> str:
        """Return a comma-separated page spec, e.g. ``"1-5,9"``."""
        return ",".join(
            f"{start}-{end}" if start != end else str(start)
            for start, end in self.as_range_pairs(zero_based=zero_based)
        )

    def is_contiguous(self) -> bool:
        """Report whether the selection is a single unbroken run of pages."""
        return len(self.pages) == self.pages[-1] - self.pages[0] + 1

    def as_contiguous(self) -> tuple[int, int]:
        """Return the inclusive 1-based ``(first, last)`` page of an unbroken run.

        Raises:
            PageSelectionError: the selection has gaps.
        """
        if not self.is_contiguous():
            raise PageSelectionError(
                f"got {len(self.pages)} pages selected across gaps (pages {self.as_ranges()})."
            )
        return self.pages[0], self.pages[-1]

    def as_extracted_pdf(self, source_pdf: Path, target_pdf: Path) -> Path:
        """Write the selected pages of ``source_pdf`` to ``target_pdf``.

        For backends that accept no page argument. Returns ``target_pdf``.

        Raises:
            PageSelectionError: the pages cannot be read or written.
        """
        pypdf = _load_pypdf()

        try:
            reader = pypdf.PdfReader(str(source_pdf))
            writer = pypdf.PdfWriter()
            for page_index in self.as_zero_based():
                writer.add_page(reader.pages[page_index])
            target_pdf.parent.mkdir(parents=True, exist_ok=True)
            with target_pdf.open("wb") as output_file:
                writer.write(output_file)
        except Exception as exc:
            raise PageSelectionError(f"Unable to extract pages: {exc}") from exc
        return target_pdf


def _parse_page_token(token: str, page_count: int) -> int:
    """Resolve a single 1-based page token, where ``N`` means the last page."""
    if token.lower() == "n":
        page = page_count
    else:
        try:
            page = int(token)
        except ValueError as exc:
            raise PageSelectionError(
                f"Invalid --page-range value: {token!r} is not a page number."
            ) from exc

    if page < 1:
        raise PageSelectionError("Invalid --page-range value: pages are 1-based.")
    if page > page_count:
        raise PageSelectionError("Invalid --page-range value: page is outside the document.")
    return page

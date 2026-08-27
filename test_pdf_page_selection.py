#!/usr/bin/env python3
"""Unit tests for the PDF page-selection module."""

from __future__ import annotations

import importlib
import tempfile
import unittest
from pathlib import Path

from pdf_page_selection import (
    PAGE_RANGE_HELP,
    PageSelection,
    PageSelectionError,
    load_page_count,
)


def _write_pdf(path: Path, page_count: int) -> Path:
    """Write a minimal ``page_count``-page PDF, or skip if pypdf is absent."""
    pypdf = _require_pypdf()
    writer = pypdf.PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=72, height=72)
    with path.open("wb") as handle:
        writer.write(handle)
    return path


def _require_pypdf():
    try:
        return importlib.import_module("pypdf")
    except ImportError:  # pragma: no cover - environment without pypdf
        raise unittest.SkipTest("pypdf is not installed") from None


class ParseTest(unittest.TestCase):
    def test_accepts_lists_ranges_and_the_n_sentinel(self) -> None:
        self.assertEqual(PageSelection.parse("1,3,5-N", 7).as_one_based(), [1, 3, 5, 6, 7])
        self.assertEqual(PageSelection.parse("5-N", 7).as_one_based(), [5, 6, 7])
        self.assertEqual(PageSelection.parse("N", 7).as_one_based(), [7])
        self.assertEqual(PageSelection.parse("4", 7).as_one_based(), [4])

    def test_sorts_and_deduplicates(self) -> None:
        self.assertEqual(PageSelection.parse("5,1,2,2,1", 7).as_one_based(), [1, 2, 5])

    def test_tolerates_surrounding_whitespace(self) -> None:
        self.assertEqual(PageSelection.parse(" 1 , 3 - 4 ", 7).as_one_based(), [1, 3, 4])

    def test_rejects_invalid_specs(self) -> None:
        for spec in ["", "0", "4-2", "1,,3", "2-", "1-2-3", "N-1", "11", "abc", "-", "1-x"]:
            with (
                self.subTest(spec=spec),
                self.assertRaisesRegex(PageSelectionError, r"^Invalid --page-range value:"),
            ):
                PageSelection.parse(spec, 10)

    def test_rejects_a_document_with_no_pages(self) -> None:
        with self.assertRaisesRegex(PageSelectionError, "PDF has no pages"):
            PageSelection.parse("1", 0)

    def test_page_selection_error_is_a_value_error(self) -> None:
        self.assertTrue(issubclass(PageSelectionError, ValueError))


class FromPagesTest(unittest.TestCase):
    def test_normalizes_known_pages(self) -> None:
        self.assertEqual(PageSelection.from_pages([3, 1, 1], 5).as_one_based(), [1, 3])

    def test_rejects_out_of_range_and_empty_input(self) -> None:
        with self.assertRaisesRegex(PageSelectionError, "outside the document"):
            PageSelection.from_pages([11], 10)
        with self.assertRaisesRegex(PageSelectionError, "pages are 1-based"):
            PageSelection.from_pages([0], 10)
        with self.assertRaisesRegex(PageSelectionError, "no pages selected"):
            PageSelection.from_pages([], 10)


class ProjectionTest(unittest.TestCase):
    def test_one_based_and_zero_based(self) -> None:
        selection = PageSelection.parse("1-3,N", 5)
        self.assertEqual(selection.as_one_based(), [1, 2, 3, 5])
        self.assertEqual(selection.as_zero_based(), [0, 1, 2, 4])

    def test_ranges_use_singletons_and_runs(self) -> None:
        selection = PageSelection.parse("1-3,5,7-9", 9)
        self.assertEqual(selection.as_ranges(), "1-3,5,7-9")
        self.assertEqual(selection.as_range_pairs(), [(1, 3), (5, 5), (7, 9)])

    def test_zero_based_ranges_shift_by_one(self) -> None:
        selection = PageSelection.parse("1-3,5", 9)
        self.assertEqual(selection.as_ranges(zero_based=True), "0-2,4")
        self.assertEqual(selection.as_range_pairs(zero_based=True), [(0, 2), (4, 4)])

    def test_len_reports_selected_page_count(self) -> None:
        self.assertEqual(len(PageSelection.parse("1,3,5-7", 9)), 5)

    def test_contiguous_run_resolves_to_first_and_last(self) -> None:
        selection = PageSelection.parse("2-6", 9)
        self.assertTrue(selection.is_contiguous())
        self.assertEqual(selection.as_contiguous(), (2, 6))

    def test_single_page_is_contiguous(self) -> None:
        self.assertEqual(PageSelection.parse("4", 9).as_contiguous(), (4, 4))

    def test_gapped_selection_is_rejected_as_contiguous(self) -> None:
        selection = PageSelection.parse("1,3,5", 9)
        self.assertFalse(selection.is_contiguous())
        with self.assertRaisesRegex(PageSelectionError, "across gaps"):
            selection.as_contiguous()


class PdfBackedTest(unittest.TestCase):
    def test_load_page_count_reads_the_document(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = _write_pdf(Path(temp_dir) / "doc.pdf", 4)
            self.assertEqual(load_page_count(pdf_path), 4)

    def test_load_page_count_reports_unreadable_files(self) -> None:
        _require_pypdf()
        with tempfile.TemporaryDirectory() as temp_dir:
            broken = Path(temp_dir) / "broken.pdf"
            broken.write_text("not a pdf", encoding="utf-8")
            with self.assertRaisesRegex(PageSelectionError, "Unable to read PDF pages"):
                load_page_count(broken)

    def test_extracted_pdf_contains_only_the_selected_pages(self) -> None:
        pypdf = _require_pypdf()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = _write_pdf(root / "doc.pdf", 6)
            selection = PageSelection.parse("2,4-5", load_page_count(source))

            target = selection.as_extracted_pdf(source, root / "nested" / "subset.pdf")

            self.assertTrue(target.is_file())
            self.assertEqual(len(pypdf.PdfReader(str(target)).pages), 3)

    def test_extracted_pdf_reports_an_unreadable_source(self) -> None:
        _require_pypdf()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            broken = root / "broken.pdf"
            broken.write_text("not a pdf", encoding="utf-8")
            selection = PageSelection.from_pages([1], 1)
            with self.assertRaisesRegex(PageSelectionError, "Unable to extract pages"):
                selection.as_extracted_pdf(broken, root / "subset.pdf")


class SharedCliSyntaxTest(unittest.TestCase):
    """Every converter must document and accept the same --page-range syntax.

    README.md promises one syntax across all converters that support the flag;
    this is the test that keeps the promise true.
    """

    CONVERTERS = (
        "pdf_convert_docling",
        "pdf_convert_llamaparse",
        "pdf_convert_marker",
        "pdf_convert_mineru",
        "pdf_convert_opendataloader",
        "pdf_convert_paddleocr_vl",
        "pdf_convert_pymupdf4llm",
    )

    def _page_range_action(self, module_name: str):
        from pdf_convert_run import Backend, build_parser

        module = importlib.import_module(module_name)
        backends = [
            value
            for value in vars(module).values()
            if isinstance(value, type) and issubclass(value, Backend) and value is not Backend
        ]
        self.assertEqual(len(backends), 1, f"{module_name} should declare exactly one Backend")
        parser = build_parser(backends[0]())
        for action in parser._actions:
            if "--page-range" in action.option_strings:
                return action
        self.fail(f"{module_name} does not declare --page-range")

    def test_every_converter_documents_the_canonical_syntax(self) -> None:
        for module_name in self.CONVERTERS:
            with self.subTest(module=module_name):
                help_text = self._page_range_action(module_name).help or ""
                # MinerU appends a caveat; the shared syntax stays the prefix.
                self.assertTrue(
                    help_text.startswith(PAGE_RANGE_HELP),
                    f"{module_name} documents a different --page-range syntax: {help_text!r}",
                )

    def test_no_converter_reimplements_page_range_parsing(self) -> None:
        for module_name in self.CONVERTERS:
            with self.subTest(module=module_name):
                module = importlib.import_module(module_name)
                for leaked in ("parse_page_range", "collapse_consecutive", "format_page_ranges"):
                    self.assertFalse(
                        hasattr(module, leaked),
                        f"{module_name} still exposes {leaked}",
                    )


if __name__ == "__main__":
    unittest.main()

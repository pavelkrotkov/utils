#!/usr/bin/env python3
"""Unit tests for shared PDF converter helpers."""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from pdf_convert_common import (
    collapse_consecutive,
    format_page_ranges,
    import_or_die,
    parse_page_range,
    require_pdf_path,
    resolve_output_path,
)


class PdfConvertCommonTest(unittest.TestCase):
    def test_resolve_output_path_uses_input_stem_and_creates_output_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_path = root / "paper.pdf"
            output_dir = root / "nested" / "out"

            output_path = resolve_output_path(input_path, None, output_dir)

            self.assertEqual(output_path, output_dir / "paper.md")
            self.assertTrue(output_dir.is_dir())

    def test_resolve_output_path_respects_explicit_output_and_creates_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            explicit_output = root / "custom" / "notes.md"

            output_path = resolve_output_path(root / "paper.pdf", explicit_output, None)

            self.assertEqual(output_path, explicit_output)
            self.assertTrue(explicit_output.parent.is_dir())

    def test_require_pdf_path_returns_existing_pdf_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "paper.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n")

            self.assertEqual(require_pdf_path(pdf_path), pdf_path)

    def test_require_pdf_path_rejects_missing_or_non_pdf_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            text_path = root / "paper.txt"
            text_path.write_text("not a pdf", encoding="utf-8")

            for candidate in [root / "missing.pdf", text_path]:
                with (
                    self.subTest(candidate=candidate),
                    contextlib.redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit),
                ):
                    require_pdf_path(candidate)

    def test_import_or_die_returns_module(self) -> None:
        module = import_or_die("pathlib", "pathlib")

        self.assertIs(module.Path, Path)

    def test_import_or_die_exits_with_install_hint_for_missing_module(self) -> None:
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
            import_or_die("_definitely_missing_pdf_backend_", "missing-package")

        self.assertIn("pip install missing-package", stderr.getvalue())

    def test_collapse_consecutive_returns_sorted_inclusive_ranges(self) -> None:
        self.assertEqual(
            collapse_consecutive([5, 1, 2, 4, 4, 7]),
            [(1, 2), (4, 5), (7, 7)],
        )

    def test_format_page_ranges_uses_singletons_and_ranges(self) -> None:
        self.assertEqual(
            format_page_ranges([(1, 3), (5, 5), (7, 9)]),
            "1-3,5,7-9",
        )

    def test_parse_page_range_accepts_ranges_n_sentinel_and_zero_based_output(self) -> None:
        self.assertEqual(parse_page_range("1,3,5-N", 7, one_based=True), [1, 3, 5, 6, 7])
        self.assertEqual(parse_page_range("1-3,N", 5, one_based=False), [0, 1, 2, 4])

    def test_parse_page_range_rejects_invalid_specs(self) -> None:
        for spec in ["", "0", "4-2", "1,,3", "2-", "1-2-3", "N-1", "11"]:
            with (
                self.subTest(spec=spec),
                self.assertRaisesRegex(ValueError, r"^Invalid --page-range value:"),
            ):
                parse_page_range(spec, 10, one_based=True)


if __name__ == "__main__":
    unittest.main()

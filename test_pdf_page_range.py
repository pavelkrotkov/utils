#!/usr/bin/env python3
"""Regression tests for PDF converter page-range parser imports."""

from __future__ import annotations

import importlib
import unittest


PARSER_MODULES = [
    "pdf_convert_docling",
    "pdf_convert_llamaparse",
    "pdf_convert_marker",
    "pdf_convert_pymupdf4llm",
]


class PageRangeParserTest(unittest.TestCase):
    def test_comma_separated_pages_and_ranges(self) -> None:
        for module_name in PARSER_MODULES:
            with self.subTest(module=module_name):
                module = importlib.import_module(module_name)
                self.assertEqual(
                    module.parse_page_range("1,3,5-10", 12, one_based=True),
                    [1, 3, 5, 6, 7, 8, 9, 10],
                )

    def test_n_sentinel_resolves_to_last_page(self) -> None:
        for module_name in PARSER_MODULES:
            with self.subTest(module=module_name):
                module = importlib.import_module(module_name)
                self.assertEqual(
                    module.parse_page_range("5-N", 7, one_based=True),
                    [5, 6, 7],
                )

    def test_zero_based_output_keeps_one_based_input_semantics(self) -> None:
        for module_name in PARSER_MODULES:
            with self.subTest(module=module_name):
                module = importlib.import_module(module_name)
                self.assertEqual(
                    module.parse_page_range("1-3,N", 5, one_based=False),
                    [0, 1, 2, 4],
                )

    def test_invalid_input_has_consistent_error_shape(self) -> None:
        invalid_specs = ["", "0", "4-2", "1,,3", "2-", "1-2-3", "N-1", "11"]
        for module_name in PARSER_MODULES:
            module = importlib.import_module(module_name)
            for spec in invalid_specs:
                with self.subTest(module=module_name, spec=spec):
                    with self.assertRaisesRegex(ValueError, r"^Invalid --page-range value:"):
                        module.parse_page_range(spec, 10, one_based=True)


if __name__ == "__main__":
    unittest.main()

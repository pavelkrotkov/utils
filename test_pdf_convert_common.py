#!/usr/bin/env python3
"""Unit tests for shared PDF converter helpers."""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from pdf_convert_common import (
    copy_referenced_assets,
    find_generated_markdown,
    import_or_die,
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

    def test_find_generated_markdown_returns_only_unambiguous_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            nested = root / "doc" / "vlm"
            nested.mkdir(parents=True)
            generated = nested / "doc.md"
            generated.write_text("# hi", encoding="utf-8")

            self.assertEqual(find_generated_markdown(root), generated)

            (root / "other.md").write_text("# other", encoding="utf-8")
            self.assertIsNone(find_generated_markdown(root))
            self.assertIsNone(find_generated_markdown(root / "missing"))

    def test_copy_referenced_assets_copies_local_links_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_dir = root / "generated"
            images = source_dir / "images"
            images.mkdir(parents=True)
            asset = images / "fig.jpg"
            asset.write_bytes(b"jpeg")
            secret = root / "secret.txt"
            secret.write_text("nope", encoding="utf-8")

            markdown_text = (
                "![ok](images/fig.jpg)\n"
                "[evil](../secret.txt)\n"
                "![abs](/etc/hosts)\n"
                "![remote](https://example.com/x.png)\n"
                '<img src="images/fig.jpg">'
            )

            copied = copy_referenced_assets(markdown_text, source_dir, root / "out")

            self.assertEqual([path.name for path in copied], ["fig.jpg"])
            self.assertEqual((root / "out" / "images" / "fig.jpg").read_bytes(), b"jpeg")
            self.assertFalse((root / "out" / "secret.txt").exists())

            stale = root / "out" / "images" / "fig.jpg"
            stale.write_bytes(b"stale")

            copied_again = copy_referenced_assets(markdown_text, source_dir, root / "out")

            self.assertEqual([path.name for path in copied_again], ["fig.jpg"])
            self.assertEqual(stale.read_bytes(), b"jpeg")


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Unit tests for the PDF conversion run module.

The whole run is exercised through a fake backend, so none of this needs a PDF
library installed.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import io
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest import mock

from pdf_convert_run import (
    AlreadyWritten,
    Backend,
    ConversionError,
    ConversionRequest,
    MarkdownDirectory,
    MarkdownText,
    Outcome,
    build_parser,
    copy_referenced_assets,
    execute,
    locate_generated_markdown,
    require_module,
    require_pdf_path,
    resolve_output_path,
    run,
)

CONVERTERS = (
    "pdf_convert_docling",
    "pdf_convert_llamaparse",
    "pdf_convert_marker",
    "pdf_convert_mathpix_sdk",
    "pdf_convert_mineru",
    "pdf_convert_opendataloader",
    "pdf_convert_paddleocr_vl",
    "pdf_convert_pymupdf4llm",
)


class FakeBackend(Backend):
    """A backend that records its request and returns a canned outcome."""

    name = "fake"
    description = "Fake backend."

    def __init__(
        self,
        outcome: Outcome | None = None,
        error: str | None = None,
        produce: Callable[[ConversionRequest], Outcome] | None = None,
    ) -> None:
        self.outcome: Outcome = outcome if outcome is not None else MarkdownText("# fake\n")
        self.produce = produce
        self.error = error
        self.request: ConversionRequest | None = None
        self.validated = False

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--fake-flag", action="store_true")

    def validate(self, args: argparse.Namespace) -> None:
        self.validated = True
        if self.error == "validate":
            raise ConversionError("fake validation failed.")

    def convert(self, request: ConversionRequest) -> Outcome:
        self.request = request
        if self.error == "convert":
            raise ConversionError("fake conversion failed.")
        if self.produce is not None:
            return self.produce(request)
        return self.outcome


def _make_pdf(directory: Path, name: str = "paper.pdf") -> Path:
    pdf_path = directory / name
    pdf_path.write_bytes(b"%PDF-1.4\n")
    return pdf_path


def _args(pdf_path: Path, **overrides) -> argparse.Namespace:
    values = {"pdf_path": pdf_path, "output": None, "output_dir": None, "page_range": None}
    values.update(overrides)
    return argparse.Namespace(**values)


class RunTest(unittest.TestCase):
    def test_markdown_text_outcome_is_written_to_the_resolved_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pdf_path = _make_pdf(root)
            backend = FakeBackend(MarkdownText("# hello\n"))

            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                output_path = run(backend, _args(pdf_path))

            self.assertEqual(output_path, root / "paper.md")
            self.assertEqual(output_path.read_text(encoding="utf-8"), "# hello\n")
            self.assertIn(f"Wrote Markdown to: {output_path}", stdout.getvalue())

    def test_validate_runs_before_the_input_path_is_checked(self) -> None:
        backend = FakeBackend(error="validate")

        with self.assertRaisesRegex(ConversionError, "fake validation failed"):
            run(backend, _args(Path("/definitely/missing.pdf")))

        self.assertTrue(backend.validated)

    def test_missing_input_is_reported_before_the_backend_converts(self) -> None:
        backend = FakeBackend()

        with self.assertRaisesRegex(ConversionError, "PDF file not found"):
            run(backend, _args(Path("/definitely/missing.pdf")))

        self.assertIsNone(backend.request)

    def test_backend_receives_a_workspace_that_is_cleaned_up(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = _make_pdf(Path(temp_dir))
            backend = FakeBackend()

            with contextlib.redirect_stdout(io.StringIO()):
                run(backend, _args(pdf_path))

            assert backend.request is not None
            self.assertFalse(backend.request.workspace.exists())

    def test_already_written_outcome_leaves_the_output_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pdf_path = _make_pdf(root)

            def write_it(request: ConversionRequest) -> Outcome:
                request.output_path.write_text("# by the backend\n", encoding="utf-8")
                return AlreadyWritten()

            backend = FakeBackend(produce=write_it)

            with contextlib.redirect_stdout(io.StringIO()):
                output_path = run(backend, _args(pdf_path))

            self.assertEqual(output_path.read_text(encoding="utf-8"), "# by the backend\n")

    def test_already_written_can_report_a_different_path(self) -> None:
        """marker names its own output; the run must announce the real file."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pdf_path = _make_pdf(root)
            actual = root / "notes.md"

            def write_it(request: ConversionRequest) -> Outcome:
                actual.write_text("# by the backend\n", encoding="utf-8")
                return AlreadyWritten(actual)

            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                output_path = run(
                    FakeBackend(produce=write_it), _args(pdf_path, output=root / "notes.txt")
                )

            self.assertEqual(output_path, actual)
            self.assertIn(f"Wrote Markdown to: {actual}", stdout.getvalue())
            self.assertFalse((root / "notes.txt").exists())

    def test_markdown_directory_outcome_copies_referenced_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pdf_path = _make_pdf(root)
            out_dir = root / "out"

            def produce(request: ConversionRequest) -> Outcome:
                generated = request.workspace / "paper.md"
                generated.write_text("![fig](images/fig.png)\n", encoding="utf-8")
                image = request.workspace / "images" / "fig.png"
                image.parent.mkdir(parents=True)
                image.write_bytes(b"png")
                return MarkdownDirectory(request.workspace, expected_stem="paper")

            backend = FakeBackend(produce=produce)

            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                output_path = run(backend, _args(pdf_path, output_dir=out_dir))

            self.assertEqual(output_path, out_dir / "paper.md")
            self.assertTrue((out_dir / "images" / "fig.png").is_file())
            self.assertIn("Copied referenced asset", stdout.getvalue())

    def test_markdown_directory_outcome_reports_an_ambiguous_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pdf_path = _make_pdf(root)

            def produce(request: ConversionRequest) -> Outcome:
                (request.workspace / "one.md").write_text("a", encoding="utf-8")
                (request.workspace / "two.md").write_text("b", encoding="utf-8")
                return MarkdownDirectory(request.workspace)

            with self.assertRaisesRegex(ConversionError, "did not produce a Markdown file"):
                run(FakeBackend(produce=produce), _args(pdf_path))

    def test_backend_failures_propagate_as_conversion_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = _make_pdf(Path(temp_dir))

            with self.assertRaisesRegex(ConversionError, "fake conversion failed"):
                run(FakeBackend(error="convert"), _args(pdf_path))

    def test_page_range_reaches_the_backend_as_a_selection(self) -> None:
        pypdf = _require_pypdf()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pdf_path = root / "paper.pdf"
            writer = pypdf.PdfWriter()
            for _ in range(5):
                writer.add_blank_page(width=72, height=72)
            with pdf_path.open("wb") as handle:
                writer.write(handle)

            backend = FakeBackend()
            with contextlib.redirect_stdout(io.StringIO()):
                run(backend, _args(pdf_path, page_range="2-4"))

            assert backend.request is not None
            assert backend.request.selection is not None
            self.assertEqual(backend.request.selection.as_one_based(), [2, 3, 4])
            self.assertEqual(backend.request.page_count(), 5)

    def test_invalid_page_range_is_reported_as_a_conversion_error(self) -> None:
        pypdf = _require_pypdf()
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "paper.pdf"
            writer = pypdf.PdfWriter()
            writer.add_blank_page(width=72, height=72)
            with pdf_path.open("wb") as handle:
                writer.write(handle)

            with self.assertRaisesRegex(ConversionError, r"Invalid --page-range value"):
                run(FakeBackend(), _args(pdf_path, page_range="9"))


def _require_pypdf():
    try:
        return importlib.import_module("pypdf")
    except ImportError:  # pragma: no cover - environment without pypdf
        raise unittest.SkipTest("pypdf is not installed") from None


class ExecuteTest(unittest.TestCase):
    def test_successful_run_returns_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = _make_pdf(Path(temp_dir))

            with contextlib.redirect_stdout(io.StringIO()):
                code = execute(FakeBackend(), [str(pdf_path)])

            self.assertEqual(code, 0)

    def test_failure_prints_one_error_line_and_returns_one(self) -> None:
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            code = execute(FakeBackend(), ["/definitely/missing.pdf"])

        self.assertEqual(code, 1)
        self.assertEqual(
            stderr.getvalue().strip(), "ERROR: PDF file not found: /definitely/missing.pdf"
        )


class ParserTest(unittest.TestCase):
    def test_shared_skeleton_is_declared_once(self) -> None:
        parser = build_parser(FakeBackend())
        options = {option for action in parser._actions for option in action.option_strings}

        self.assertLessEqual({"-o", "--output", "--output-dir", "--page-range"}, options)
        self.assertIn("--fake-flag", options)

    def test_backends_can_opt_out_of_page_selection(self) -> None:
        class NoPages(FakeBackend):
            supports_page_selection = False

        parser = build_parser(NoPages())
        options = {option for action in parser._actions for option in action.option_strings}

        self.assertNotIn("--page-range", options)

    def test_page_range_caveat_is_appended_to_the_shared_help(self) -> None:
        class Caveated(FakeBackend):
            page_range_caveat = "Only contiguous ranges."

        help_text = Caveated().page_range_help()

        self.assertTrue(help_text.endswith("Only contiguous ranges."))


class SharedImplementationTest(unittest.TestCase):
    def test_resolve_output_path_uses_input_stem_and_creates_output_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_dir = root / "nested" / "out"

            output_path = resolve_output_path(root / "paper.pdf", None, output_dir)

            self.assertEqual(output_path, output_dir / "paper.md")
            self.assertTrue(output_dir.is_dir())

    def test_resolve_output_path_respects_explicit_output_and_creates_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            explicit_output = root / "custom" / "notes.md"

            output_path = resolve_output_path(root / "paper.pdf", explicit_output, None)

            self.assertEqual(output_path, explicit_output)
            self.assertTrue(explicit_output.parent.is_dir())

    def test_resolve_output_path_refuses_to_overwrite_the_input(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = _make_pdf(Path(temp_dir))

            with self.assertRaisesRegex(ConversionError, "must differ from the input PDF"):
                resolve_output_path(pdf_path, pdf_path, None)

    def test_require_pdf_path_returns_existing_pdf_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = _make_pdf(Path(temp_dir))

            self.assertEqual(require_pdf_path(pdf_path), pdf_path)

    def test_require_pdf_path_rejects_missing_absent_and_non_pdf_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            text_path = root / "paper.txt"
            text_path.write_text("not a pdf", encoding="utf-8")

            for candidate in [None, root / "missing.pdf", text_path]:
                with self.subTest(candidate=candidate), self.assertRaises(ConversionError):
                    require_pdf_path(candidate)

    def test_require_module_returns_module(self) -> None:
        self.assertIs(require_module("pathlib", "pathlib").Path, Path)

    def test_require_module_reports_an_install_hint(self) -> None:
        with self.assertRaisesRegex(ConversionError, "pip install missing-package"):
            require_module("_definitely_missing_pdf_backend_", "missing-package")

    def test_locate_generated_markdown_prefers_the_expected_stem(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            expected = root / "doc.md"
            expected.write_text("# expected", encoding="utf-8")
            nested = root / "sub"
            nested.mkdir()
            (nested / "other.md").write_text("# other", encoding="utf-8")

            self.assertEqual(locate_generated_markdown(root, "doc"), expected)

    def test_locate_generated_markdown_falls_back_to_a_unique_nested_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            nested = root / "doc" / "auto"
            nested.mkdir(parents=True)
            generated = nested / "doc.md"
            generated.write_text("# hi", encoding="utf-8")

            self.assertEqual(locate_generated_markdown(root, "doc"), generated)
            self.assertEqual(locate_generated_markdown(root), generated)

    def test_locate_generated_markdown_refuses_ambiguity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "one.md").write_text("a", encoding="utf-8")
            (root / "two.md").write_text("b", encoding="utf-8")

            self.assertIsNone(locate_generated_markdown(root))

    def test_locate_generated_markdown_refuses_paths_outside_the_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outside_dir = root / "outside"
            outside_dir.mkdir()
            outside = outside_dir / "escaped.md"
            outside.write_text("# escaped", encoding="utf-8")

            work_dir = root / "work"
            work_dir.mkdir()
            (work_dir / "escaped.md").symlink_to(outside)

            self.assertIsNone(locate_generated_markdown(work_dir, "escaped"))

    def test_copy_referenced_assets_copies_local_links_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_dir = root / "src"
            (source_dir / "images").mkdir(parents=True)
            (source_dir / "images" / "fig.png").write_bytes(b"png")
            target_dir = root / "out"
            target_dir.mkdir()

            markdown = (
                "![fig](images/fig.png)\n"
                "![remote](https://example.com/a.png)\n"
                '<img src="images/fig.png">\n'
                "![missing](images/absent.png)\n"
            )

            copied = copy_referenced_assets(markdown, source_dir, target_dir)

            self.assertEqual(copied, [target_dir / "images" / "fig.png"])
            self.assertTrue((target_dir / "images" / "fig.png").is_file())


class ConverterWiringTest(unittest.TestCase):
    """Every converter must go through the run module rather than around it."""

    def test_every_converter_builds_its_parser_from_the_run_module(self) -> None:
        for module_name in CONVERTERS:
            with self.subTest(module=module_name):
                module = importlib.import_module(module_name)
                backend_classes = [
                    value
                    for value in vars(module).values()
                    if isinstance(value, type)
                    and issubclass(value, Backend)
                    and value is not Backend
                ]
                self.assertEqual(
                    len(backend_classes),
                    1,
                    f"{module_name} should declare exactly one Backend",
                )
                parser = build_parser(backend_classes[0]())
                options = {option for action in parser._actions for option in action.option_strings}
                self.assertLessEqual({"-o", "--output", "--output-dir"}, options)

    def test_no_converter_reimplements_the_run(self) -> None:
        for module_name in CONVERTERS:
            with self.subTest(module=module_name):
                module = importlib.import_module(module_name)
                for leaked in (
                    "locate_generated_markdown",
                    "find_generated_markdown",
                    "load_pdf_page_count",
                    "extract_page_subset",
                    "import_or_die",
                ):
                    self.assertFalse(
                        hasattr(module, leaked),
                        f"{module_name} still exposes {leaked}",
                    )


class MarkerOutputPathTest(unittest.TestCase):
    """marker always writes <stem>.md, whatever suffix -o asked for."""

    def test_backend_reports_the_md_file_it_actually_wrote(self) -> None:
        marker = importlib.import_module("pdf_convert_marker")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pdf_path = _make_pdf(root)
            backend = marker.MarkerBackend()
            args = build_parser(backend).parse_args([str(pdf_path)])
            request = ConversionRequest(
                pdf_path=pdf_path,
                output_path=root / "notes.txt",
                workspace=root,
                args=args,
            )

            saved: dict[str, str] = {}

            class FakeConfigParser:
                def __init__(self, options):
                    saved["output_dir"] = options["output_dir"]

                def get_converter_cls(self):
                    return lambda **kwargs: lambda path: "rendered"

                def generate_config_dict(self):
                    return {}

                def get_processors(self):
                    return []

                def get_renderer(self):
                    return None

                def get_llm_service(self):
                    return None

            def fake_require_module(module_name: str, install_package: str):
                if module_name == "marker.config.parser":
                    return argparse.Namespace(ConfigParser=FakeConfigParser)
                if module_name == "marker.models":
                    return argparse.Namespace(create_model_dict=dict)
                return argparse.Namespace(
                    save_output=lambda rendered, out_dir, base: saved.update(base=base)
                )

            with mock.patch.object(marker, "require_module", fake_require_module):
                outcome = backend.convert(request)

            self.assertEqual(outcome, AlreadyWritten(root / "notes.md"))
            self.assertEqual(saved["base"], "notes")


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Exercise real PDF rendering and output writing with a fake MLX runtime."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pymupdf
import pytest
from pypdf import PdfWriter

from pdf_convert_run import execute
from pdf_convert_unlimited_ocr import DEFAULT_MODEL, UnlimitedOCRBackend


@pytest.fixture
def document(tmp_path):
    pdf = tmp_path / "input.pdf"
    writer = PdfWriter()
    for width in (100, 200, 300):
        writer.add_blank_page(width=width, height=200)
    writer.write(pdf)
    output = tmp_path / "result.md"
    output.write_text("Keep existing output", encoding="utf-8")
    return pdf, output


@pytest.fixture
def mlx_runtime(monkeypatch):
    model = SimpleNamespace(config=object())
    processor = object()
    mlx = SimpleNamespace(metal=SimpleNamespace(is_available=lambda: True), gpu=object())
    mlx.set_default_device = Mock()
    runtime = SimpleNamespace(load=Mock(return_value=(model, processor)), generate=Mock())
    prompts = SimpleNamespace(apply_chat_template=Mock(return_value="<image>document parsing."))
    modules = {"mlx.core": mlx, "mlx_vlm": runtime, "mlx_vlm.prompt_utils": prompts}
    monkeypatch.setattr("pdf_unlimited_ocr_mlx.require_module", lambda name, _: modules[name])
    monkeypatch.setattr("pdf_convert_unlimited_ocr.platform.system", lambda: "Darwin")
    monkeypatch.setattr("pdf_convert_unlimited_ocr.platform.machine", lambda: "arm64")
    return SimpleNamespace(
        model=model, processor=processor, mlx=mlx, runtime=runtime, prompts=prompts
    )


def run_converter(document, *options: str) -> int:
    pdf, output = document
    return execute(UnlimitedOCRBackend(), [str(pdf), "-o", str(output), *options])


@pytest.mark.parametrize(
    "selection,expected_widths", [("3,1", [100, 300]), (None, [100, 200, 300])]
)
def test_cli_renders_selected_pages_and_writes_markdown(
    document, mlx_runtime, selection, expected_widths
):
    widths = []

    def generate(*, image, **kwargs):
        [image_path] = image
        assert Path(image_path).is_file()
        widths.append(pymupdf.Pixmap(image_path).width)
        text = f"<PAGE><|ref|>title<|/ref|><|det|>box<|/det|># Page {widths[-1]}\n\n$E=mc^2$"
        return SimpleNamespace(text=text, finish_reason="stop")

    mlx_runtime.runtime.generate.side_effect = generate
    options = ["--dpi", "72", "--max-tokens", "256"]
    if selection:
        options.extend(("--page-range", selection))
    assert run_converter(document, *options) == 0

    mlx_runtime.runtime.load.assert_called_once_with(DEFAULT_MODEL)
    mlx_runtime.mlx.set_default_device.assert_called_once_with(mlx_runtime.mlx.gpu)
    mlx_runtime.prompts.apply_chat_template.assert_called_once_with(
        mlx_runtime.processor, mlx_runtime.model.config, "document parsing.", num_images=1
    )
    assert widths == expected_widths
    recipe = mlx_runtime.runtime.generate.call_args.kwargs.copy()
    recipe.pop("image")
    assert recipe == {
        "model": mlx_runtime.model,
        "processor": mlx_runtime.processor,
        "prompt": "<image>document parsing.",
        "temperature": 0.0,
        "max_tokens": 256,
        "prefill_step_size": 64,
        "skip_special_tokens": False,
        "cropping": False,
        "image_size": 1024,
    }
    expected = "\n\n---\n\n".join(f"# Page {width}\n\n$E=mc^2$" for width in expected_widths) + "\n"
    assert document[1].read_text(encoding="utf-8") == expected


@pytest.mark.parametrize(
    "failed_page",
    [
        RuntimeError("Metal error"),
        SimpleNamespace(text="Partial", finish_reason="length"),
        SimpleNamespace(text="<PAGE><|det|>box<|/det|>", finish_reason="stop"),
        SimpleNamespace(text="<|det|>box<|det|>nested<|/det|># Tail", finish_reason="stop"),
        SimpleNamespace(text="One page<PAGE>Unexpected second part", finish_reason="stop"),
        SimpleNamespace(text="\ud800", finish_reason="stop"),
    ],
)
def test_failure_on_second_page_preserves_existing_output(
    document, mlx_runtime, failed_page, capsys
):
    mlx_runtime.runtime.generate.side_effect = [
        SimpleNamespace(text="# First", finish_reason="stop"),
        failed_page,
    ]
    assert run_converter(document, "--page-range", "1,3") == 1
    assert "Unlimited-OCR failed on page 3" in capsys.readouterr().err
    assert document[1].read_text(encoding="utf-8") == "Keep existing output"


def test_cli_reports_model_load_failure(document, mlx_runtime, capsys):
    mlx_runtime.runtime.load.side_effect = RuntimeError("checkpoint unavailable")
    assert run_converter(document, "--model", "/models/unlimited-ocr") == 1
    assert "Unable to load Unlimited-OCR model /models/unlimited-ocr" in capsys.readouterr().err
    mlx_runtime.runtime.generate.assert_not_called()
    assert document[1].read_text(encoding="utf-8") == "Keep existing output"


@pytest.mark.parametrize("options", [("--dpi", "0"), ("--max-tokens", "0")])
def test_invalid_options_fail_before_model_load(document, mlx_runtime, options):
    assert run_converter(document, *options) == 1
    mlx_runtime.runtime.load.assert_not_called()


def test_unsupported_platform_fails_before_model_load(document, mlx_runtime, monkeypatch, capsys):
    monkeypatch.setattr("pdf_convert_unlimited_ocr.platform.machine", lambda: "x86_64")
    assert run_converter(document) == 1
    assert "Apple Silicon" in capsys.readouterr().err
    mlx_runtime.runtime.load.assert_not_called()

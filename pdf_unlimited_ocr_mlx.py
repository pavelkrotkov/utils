"""Run Unlimited-OCR v1 in process and validate the result of each page."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from pdf_convert_run import ConversionError, require_module

GROUNDING_TOKEN = r"<\|/?(?:ref|det)\|>"
GROUNDING_BLOCK = rf"<\|(ref|det)\|>(?:(?!{GROUNDING_TOKEN}).)*<\|/\1\|>"


def clean_page(text: str, finish_reason: str) -> str:
    if finish_reason != "stop":
        raise ConversionError(
            f"Generation did not finish ({finish_reason}). Try raising --max-tokens."
        )
    # Grounding blocks hold layout labels and coordinates. Recognized text follows them.
    text = re.sub(GROUNDING_BLOCK, "", text, flags=re.DOTALL)
    if re.search(GROUNDING_TOKEN, text):
        raise ConversionError("The model returned malformed grounding markers.")
    text = text.replace("<｜begin▁of▁sentence｜>", "").replace("<｜end▁of▁sentence｜>", "").strip()
    text = text.removeprefix("<PAGE>").strip()
    if "<PAGE>" in text:
        raise ConversionError("The model returned extra or misplaced page markers.")
    if not text:
        raise ConversionError("The model returned empty Markdown.")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ConversionError("The model returned invalid Unicode.") from exc
    return text


def load_ocr(model_name: str, max_tokens: int) -> Callable[[Path], str]:
    mlx = require_module("mlx.core", "mlx-vlm")
    if not mlx.metal.is_available():
        raise ConversionError("Unlimited-OCR requires an available Metal GPU.")
    mlx_vlm = require_module("mlx_vlm", "mlx-vlm")
    prompt_utils = require_module("mlx_vlm.prompt_utils", "mlx-vlm")
    try:
        mlx.set_default_device(mlx.gpu)
        model, processor = mlx_vlm.load(model_name)
        prompt = prompt_utils.apply_chat_template(
            processor, model.config, "document parsing.", num_images=1
        )
    except Exception as exc:
        raise ConversionError(f"Unable to load Unlimited-OCR model {model_name}: {exc}") from exc

    def parse(image_path: Path) -> str:
        # Use one global view and small prompt batches to avoid Metal timeouts on smaller Macs.
        result = mlx_vlm.generate(
            model=model,
            processor=processor,
            prompt=prompt,
            image=[str(image_path)],
            temperature=0.0,
            max_tokens=max_tokens,
            prefill_step_size=64,
            skip_special_tokens=False,
            cropping=False,
            image_size=1024,
        )
        return clean_page(result.text, result.finish_reason)

    return parse

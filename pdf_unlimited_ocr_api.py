"""Unlimited-OCR v1 serving protocol, distinct from an ordinary chat request.

The upstream recipe requires image placeholders, retained special tokens and its
ngram logits processor. Without these controls the server can return empty text
or repeat grounding coordinates. A token-limit finish is not a complete document.

Reference: https://recipes.vllm.ai/baidu/Unlimited-OCR
"""

from __future__ import annotations

import argparse
import json
import os
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pdf_convert_run import ConversionError


def response_markdown(response: Any) -> str:
    try:
        choice = response["choices"][0]
        reason, content = choice["finish_reason"], choice["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ConversionError("Unlimited-OCR returned a malformed completion.") from exc
    if reason != "stop":
        raise ConversionError(
            f"Unlimited-OCR did not finish ({reason}); reduce --page-range or raise --max-tokens."
        )
    if not isinstance(content, str) or not content.strip():
        raise ConversionError("Unlimited-OCR returned empty Markdown.")
    return clean_markdown(content)


def clean_markdown(content: str) -> str:
    content = re.sub(r"<\|det\|>.*?<\|/det\|>", "", content, flags=re.DOTALL)
    for token in ("<|ref|>", "<|/ref|>", "<｜begin▁of▁sentence｜>", "<｜end▁of▁sentence｜>"):
        content = content.replace(token, "")
    content = content.strip().removeprefix("<PAGE>").replace("<PAGE>", "\n\n---\n\n").strip()
    if not content or "<|det|>" in content or "<|/det|>" in content:
        raise ConversionError("Unlimited-OCR returned empty or incomplete Markdown grounding.")
    return content + "\n"


def completion(args: argparse.Namespace, images: list[dict[str, object]]) -> str:
    if not images:
        raise ConversionError("PDF has no pages to parse.")
    task = "document parsing." if len(images) == 1 else "Multi page parsing."
    # The model needs this parsing recipe; ordinary chat defaults produce empty or repetitive output.
    payload = dict(
        model=args.model,
        messages=[
            dict(
                role="user",
                content=[{"type": "text", "text": "<image>" * len(images) + task}, *images],
            )
        ],
        temperature=0.0,
        max_tokens=args.max_tokens,
        skip_special_tokens=False,
        vllm_xargs=dict(ngram_size=35, window_size=128 if len(images) == 1 else 1024),
    )
    headers = {"Content-Type": "application/json"}
    if key := os.environ.get("UNLIMITED_OCR_API_KEY"):
        headers["Authorization"] = f"Bearer {key}"
    request = Request(
        args.base_url.rstrip("/") + "/chat/completions", json.dumps(payload).encode(), headers
    )
    try:
        with urlopen(request, timeout=args.timeout) as response:
            result = json.load(response)
    except HTTPError as exc:
        raise ConversionError(
            f"Unlimited-OCR server returned HTTP {exc.code}: {exc.reason}"
        ) from exc
    except (URLError, OSError, ValueError) as exc:
        raise ConversionError(f"Unlimited-OCR request failed: {exc}") from exc
    return response_markdown(result)

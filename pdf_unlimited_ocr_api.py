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
from http.client import HTTPException
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from pdf_convert_run import ConversionError

GROUNDING_TOKEN = r"<\|/?(?:ref|det)\|>"
GROUNDING_BLOCK = rf"<\|(ref|det)\|>(?:(?!{GROUNDING_TOKEN}).)*<\|/\1\|>"


class RefuseRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl) -> None:
        return None


OCR_OPENER = build_opener(ProxyHandler({}), RefuseRedirects())


def response_markdown(response: Any, page_count: int) -> str:
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
    try:
        content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ConversionError("Unlimited-OCR returned invalid Unicode.") from exc
    return clean_markdown(content, page_count)


def clean_markdown(content: str, page_count: int) -> str:
    # Reference blocks label layout regions. The recognized text follows the coordinates.
    content = re.sub(GROUNDING_BLOCK, "", content, flags=re.DOTALL)
    if re.search(GROUNDING_TOKEN, content):
        raise ConversionError("Unlimited-OCR returned malformed grounding markers.")
    content = (
        content.replace("<｜begin▁of▁sentence｜>", "").replace("<｜end▁of▁sentence｜>", "").strip()
    )
    has_page_markers = page_count > 1 or "<PAGE>" in content
    if has_page_markers and (
        not content.startswith("<PAGE>") or content.count("<PAGE>") != page_count
    ):
        raise ConversionError(f"Unlimited-OCR expected {page_count} page sections in the response.")
    if not content.replace("<PAGE>", "").strip():
        raise ConversionError("Unlimited-OCR returned empty Markdown after grounding cleanup.")
    return content.removeprefix("<PAGE>").replace("<PAGE>", "\n\n---\n\n").strip() + "\n"


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
    request = Request(args.endpoint, json.dumps(payload).encode(), headers)
    try:
        with OCR_OPENER.open(request, timeout=args.timeout) as response:
            result = json.load(response)
    except HTTPError as exc:
        raise ConversionError(
            f"Unlimited-OCR server returned HTTP {exc.code}: {exc.reason}"
        ) from exc
    except (URLError, OSError, ValueError, OverflowError, HTTPException) as exc:
        raise ConversionError(f"Unlimited-OCR request failed: {exc}") from exc
    return response_markdown(result, len(images))

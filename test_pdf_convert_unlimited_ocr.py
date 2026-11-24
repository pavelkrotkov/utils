#!/usr/bin/env python3
"""Exercise the converter with actual PDF rendering and a loopback vLLM fixture.

Distinct page widths identify which PDF pages the client sends and in what order.
The fixture records the real HTTP payload; no GPU, model download or external
service is involved. Failure cases protect an existing output from replacement.
"""

from __future__ import annotations

import base64
import json
import struct
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import pdf_convert_unlimited_ocr as converter
from pdf_convert_run import execute
from pdf_convert_unlimited_ocr import UnlimitedOCRBackend
from unlimited_ocr_test_support import document as document
from unlimited_ocr_test_support import server as server


def convert(document, base_url, *options):
    pdf, output = document
    result = subprocess.run(
        [
            sys.executable,
            str(Path(converter.__file__)),
            str(pdf),
            "-o",
            str(output),
            "--base-url",
            base_url,
            *options,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.returncode


def test_selected_pages_are_rendered_in_order_and_parsed_in_one_request(document, server):
    base_url, state = server
    state["response"]["choices"][0]["message"]["content"] = (
        "<PAGE><|ref|># Parsed<|/ref|><|det|>[[0,0,1,1]]<|/det|>\n$E=mc^2$<PAGE>|A|B|\n|-|-|"
    )
    assert convert(document, base_url, "--page-range", "3,1", "--dpi", "72") == 0
    # Separate per-page requests would discard the model's cross-page context.
    [(path, payload)] = state["requests"]
    content = payload["messages"][0]["content"]
    expected = json.loads("""
        {"model":"baidu/Unlimited-OCR","messages":[{"role":"user","content":[
          {"type":"text","text":"<image><image>Multi page parsing."}]}],
         "temperature":0.0,"max_tokens":8192,"skip_special_tokens":false,
         "vllm_xargs":{"ngram_size":35,"window_size":1024}}
    """)
    expected["messages"][0]["content"].extend(content[1:])
    assert (path, payload) == ("/v1/chat/completions", expected)
    widths = []
    for item in content[1:]:
        raw = base64.b64decode(item["image_url"]["url"].split(",", 1)[1])
        widths.append(struct.unpack(">I", raw[16:20])[0])
    assert widths == [100, 300]
    assert document[1].read_text() == "# Parsed\n$E=mc^2$\n\n---\n\n|A|B|\n|-|-|\n"


@pytest.mark.parametrize(
    "response,status",
    [
        ({"choices": [{"finish_reason": "length", "message": {"content": "Partial"}}]}, 200),
        ({"choices": [{"finish_reason": "stop", "message": {"content": ""}}]}, 200),
        ({"choices": []}, 200),
        (
            {"choices": [{"finish_reason": "stop", "message": {"content": "<|det|>unfinished"}}]},
            200,
        ),
        (
            {"choices": [{"finish_reason": "stop", "message": {"content": "<|det|>box<|/det|>"}}]},
            200,
        ),
        ({"error": "unavailable"}, 503),
    ],
)
def test_server_failures_preserve_existing_output(document, server, response, status):
    base_url, state = server
    state.update(response=response, status=status)
    assert convert(document, base_url, "--page-range", "1") == 1
    assert document[1].read_text() == "Keep existing output"
    payload = state["requests"][0][1]
    assert (payload["messages"][0]["content"][0]["text"], payload["vllm_xargs"]["window_size"]) == (
        "<image>document parsing.",
        128,
    )


def test_transport_timeout_preserves_output(document):
    pdf, output = document
    with (
        patch("pdf_convert_unlimited_ocr.page_images", return_value=[{}]),
        patch("pdf_unlimited_ocr_api.urlopen", side_effect=TimeoutError("server timeout")),
    ):
        assert execute(UnlimitedOCRBackend(), [str(pdf), "-o", str(output)]) == 1
    assert output.read_text() == "Keep existing output"


@pytest.mark.parametrize(
    "options",
    [
        ("--timeout", "nan"),
        ("--timeout", "inf"),
        ("--timeout", "0"),
        ("--dpi", "0"),
        ("--max-tokens", "0"),
        ("--base-url", "file:///invalid"),
    ],
)
def test_invalid_options_fail_before_contacting_server(document, server, options):
    base_url, state = server
    assert convert(document, base_url, *options) == 1
    assert (state["requests"], document[1].read_text()) == ([], "Keep existing output")

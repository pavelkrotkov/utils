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
from http.client import IncompleteRead
from unittest.mock import patch

import pytest

from pdf_convert_run import execute
from pdf_convert_unlimited_ocr import UnlimitedOCRBackend
from unlimited_ocr_test_support import document as document
from unlimited_ocr_test_support import ocr_completion, run_converter
from unlimited_ocr_test_support import server as server


@pytest.mark.parametrize(
    "selection,expected_widths", [("3,1", [100, 300]), (None, [100, 200, 300])]
)
def test_selected_pages_are_rendered_in_order_and_parsed_in_one_request(
    document, server, monkeypatch, selection, expected_widths
):
    base_url, state = server
    pages = ["# Parsed\n$E=mc^2$", "|A|B|\n|-|-|", "Final page."][: len(expected_widths)]
    grounding = "<|ref|>title<|/ref|><|det|>[[0,0,1,1]]<|/det|>"
    state["response"]["choices"][0]["message"]["content"] = (
        "<PAGE>" + grounding + "<PAGE>".join(pages)
    )
    monkeypatch.setenv("UNLIMITED_OCR_API_KEY", "fixture-token")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("no_proxy", "")
    options = ["--dpi", "72"] + (["--page-range", selection] if selection else [])
    result = run_converter(document, base_url, *options)
    # Separate per-page requests would discard the model's cross-page context.
    [(path, payload)] = state["requests"]
    content = payload["messages"][0]["content"]
    expected = json.loads("""
        {"model":"baidu/Unlimited-OCR","messages":[{"role":"user","content":[
          {"type":"text","text":"<image><image>Multi page parsing."}]}],
         "temperature":0.0,"max_tokens":8192,"skip_special_tokens":false,
         "vllm_xargs":{"ngram_size":35,"window_size":1024}}
    """)
    expected["messages"][0]["content"][0]["text"] = (
        "<image>" * len(expected_widths) + "Multi page parsing."
    )
    expected["messages"][0]["content"].extend(content[1:])
    assert (result, path, payload, state["authorization"]) == (
        0,
        "/v1/chat/completions",
        expected,
        "Bearer fixture-token",
    )
    widths = []
    for item in content[1:]:
        raw = base64.b64decode(item["image_url"]["url"].split(",", 1)[1])
        assert (raw[:8], raw[12:16]) == (b"\x89PNG\r\n\x1a\n", b"IHDR")
        widths.append(struct.unpack(">I", raw[16:20])[0])
    assert widths == expected_widths
    assert document[1].read_text() == "\n\n---\n\n".join(pages) + "\n"


def test_single_page_uses_single_image_recipe(document, server):
    base_url, state = server
    assert run_converter(document, base_url, "--page-range", "1") == 0
    [(_, payload)] = state["requests"]
    assert (payload["messages"][0]["content"][0]["text"], payload["vllm_xargs"]["window_size"]) == (
        "<image>document parsing.",
        128,
    )
    assert document[1].read_text() == "# Parsed\n"


@pytest.mark.parametrize(
    "response,status",
    [
        (ocr_completion("Partial", "length"), 200),
        (ocr_completion(""), 200),
        ({"choices": []}, 200),
        (ocr_completion("\ud800"), 200),
        (ocr_completion("# Heading\n<|ref|>Partial"), 200),
        (ocr_completion("<|det|>box<|det|>discarded<|/det|># Tail"), 200),
        (ocr_completion("<PAGE><PAGE>"), 200),
        (ocr_completion("<PAGE>First<PAGE>Unsolicited second page"), 200),
        (ocr_completion("<|det|>unfinished"), 200),
        (ocr_completion("<|det|>box<|/det|>"), 200),
        ({"error": "unavailable"}, 503),
    ],
)
def test_server_failures_preserve_existing_output(document, server, response, status):
    base_url, state = server
    state.update(response=response, status=status)
    assert run_converter(document, base_url, "--page-range", "1") == 1
    assert len(state["requests"]) == 1
    assert document[1].read_text() == "Keep existing output"


def test_completion_missing_a_page_preserves_existing_output(document, server):
    base_url, state = server
    state["response"]["choices"][0]["message"]["content"] = "<PAGE>Only first page."
    assert run_converter(document, base_url, "--page-range", "1,3") == 1
    assert document[1].read_text() == "Keep existing output"


def test_ocr_request_does_not_follow_redirect(document, server, monkeypatch):
    base_url, state = server
    state.update(status=302, location="/redirect")
    monkeypatch.setenv("UNLIMITED_OCR_API_KEY", "fixture-token")
    assert run_converter(document, base_url, "--page-range", "1") == 1
    assert state.get("redirected") is None
    assert document[1].read_text() == "Keep existing output"


@pytest.mark.parametrize(
    "error",
    [TimeoutError("server timeout"), OverflowError("timeout too large"), IncompleteRead(b"", 1)],
)
def test_transport_failure_preserves_output(document, error):
    pdf, output = document
    with (
        patch("pdf_convert_unlimited_ocr.page_images", return_value=[{}]),
        patch("pdf_unlimited_ocr_api.OCR_OPENER.open", side_effect=error),
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
        ("--base-url", "http://127.0.0.1:invalid/v1"),
        ("--base-url", "{base_url}?"),
        ("--base-url", "{base_url}#"),
    ],
)
def test_invalid_options_fail_before_contacting_server(document, server, options):
    base_url, state = server
    options = tuple(option.format(base_url=base_url) for option in options)
    assert run_converter(document, base_url, *options) == 1
    assert (state["requests"], document[1].read_text()) == ([], "Keep existing output")

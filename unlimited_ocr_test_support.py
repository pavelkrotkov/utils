"""Real PDF and loopback HTTP fixtures for Unlimited-OCR protocol integration tests."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest
from pypdf import PdfWriter


@pytest.fixture
def document(tmp_path: Path) -> tuple[Path, Path]:
    pdf = tmp_path / "input.pdf"
    writer = PdfWriter()
    for width in (100, 200, 300):
        writer.add_blank_page(width=width, height=200)
    writer.write(pdf)
    output = tmp_path / "result.md"
    output.write_text("Keep existing output", encoding="utf-8")
    return pdf, output


@pytest.fixture
def server():
    state: dict[str, Any] = {
        "requests": [],
        "status": 200,
        "response": {"choices": [{"finish_reason": "stop", "message": {"content": "# Parsed\n"}}]},
    }

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            state["authorization"] = self.headers.get("Authorization")
            size = int(self.headers["Content-Length"])
            state["requests"].append((self.path, json.loads(self.rfile.read(size))))
            self.send_response(state["status"])
            if location := state.get("location"):
                self.send_header("Location", location)
            self.end_headers()
            self.wfile.write(json.dumps(state["response"]).encode())

        def do_GET(self):
            state["redirected"] = self.path
            self.send_error(404)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    http = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{http.server_port}/v1", state
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=5)

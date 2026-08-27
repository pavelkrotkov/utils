#!/usr/bin/env python3
"""Regression tests for the LlamaParse backend's error reporting."""

from __future__ import annotations

import argparse
import contextlib
import io
import unittest
from pathlib import Path
from unittest import mock

import pdf_convert_llamaparse as llamaparse
from pdf_convert_run import ConversionError


class ParseChunkErrorTest(unittest.TestCase):
    """main() only reports ConversionError; nothing else may escape _parse_chunk."""

    def setUp(self) -> None:
        self.args = argparse.Namespace(tier="cost_effective", version="latest")

    def _client(self, job_id: str = "job-1"):
        class FakeParsing:
            def create(self, **kwargs):
                return argparse.Namespace(id=job_id)

        return argparse.Namespace(parsing=FakeParsing())

    def _parse_chunk(self, failure: BaseException) -> None:
        def boom(client, job_id):
            raise failure

        with (
            mock.patch.object(llamaparse, "wait_for_job", boom),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            llamaparse._parse_chunk(self._client(), self.args, "file-1", "1-5", Path("chunk-1.md"))

    def test_polling_timeout_becomes_a_conversion_error(self) -> None:
        with self.assertRaisesRegex(ConversionError, "Polling timed out"):
            self._parse_chunk(TimeoutError("Polling timed out after 300.0s (job: job-1)."))

    def test_job_failure_becomes_a_conversion_error(self) -> None:
        with self.assertRaisesRegex(ConversionError, "LlamaParse job failed: boom"):
            self._parse_chunk(RuntimeError("boom"))

    def test_conversion_errors_pass_through_unwrapped(self) -> None:
        with self.assertRaisesRegex(ConversionError, "^already reported$"):
            self._parse_chunk(ConversionError("already reported"))


class FetchJobOutputPathTest(unittest.TestCase):
    def test_fetch_job_requires_an_output_location(self) -> None:
        with self.assertRaisesRegex(ConversionError, "requires --output or --output-dir"):
            llamaparse.resolve_output_path_for_job("job-1", None, None)


if __name__ == "__main__":
    unittest.main()

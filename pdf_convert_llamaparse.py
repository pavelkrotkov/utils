#!/usr/bin/env python3
# /// script
# dependencies = ["llama-cloud", "pypdf"]
# ///
"""
Convert a local PDF to Markdown using LlamaParse (LlamaCloud).

Usage:
    # Run with uv (recommended):
    uv run ./pdf_convert_llamaparse.py input.pdf

    # Standard execution:
    ./pdf_convert_llamaparse.py input.pdf -o output.md

    # Fetch results for an existing job:
    uv run ./pdf_convert_llamaparse.py --fetch-job job_id -o output-3.md

Notes:
    - The script always chunks the PDF and writes per-chunk files like output-1.md.
    - Re-runs skip existing chunk files to resume work.
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys
import time
from pathlib import Path

from pdf_convert_run import (
    AlreadyWritten,
    Backend,
    ConversionError,
    ConversionRequest,
    Outcome,
    build_parser,
    require_module,
    run,
)
from pdf_page_selection import PageSelection

DEFAULT_TIER = "cost_effective"
DEFAULT_VERSION = "latest"
DEFAULT_CHUNK_PAGES = 100
DEFAULT_POLL_TIMEOUT_SECONDS = 300.0
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
EXPAND_FIELDS = ["markdown"]


def resolve_output_path_for_job(
    job_id: str,
    output_path: Path | None,
    output_dir: Path | None,
) -> Path:
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        return output_path

    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        return output_dir / f"{job_id}.md"

    raise ConversionError("--fetch-job requires --output or --output-dir.")


def chunk_pages(pages: list[int], chunk_size: int) -> list[list[int]]:
    return [pages[i : i + chunk_size] for i in range(0, len(pages), chunk_size)]


def chunk_output_path(output_path: Path, chunk_index: int) -> Path:
    return output_path.with_name(f"{output_path.stem}-{chunk_index}{output_path.suffix}")


def extract_job_id(result: object) -> str | None:
    job_id = getattr(result, "id", None)
    if job_id:
        return str(job_id)

    job = getattr(result, "job", None)
    if job is not None:
        job_id = getattr(job, "id", None)
        if job_id:
            return str(job_id)

    job_id = getattr(result, "job_id", None)
    if job_id:
        return str(job_id)

    return None


def extract_status(result: object) -> str | None:
    status = getattr(result, "status", None)
    if status:
        return str(status)

    job = getattr(result, "job", None)
    if job is not None:
        status = getattr(job, "status", None)
        if status:
            return str(status)

    return None


def extract_error_message(result: object) -> str | None:
    error_message = getattr(result, "error_message", None)
    if error_message:
        return str(error_message)

    job = getattr(result, "job", None)
    if job is not None:
        error_message = getattr(job, "error_message", None)
        if error_message:
            return str(error_message)

    return None


def wait_for_job(client: object, job_id: str) -> object:
    start_time = time.monotonic()
    while True:
        result = client.parsing.get(job_id=job_id)
        status = extract_status(result)
        if status == "COMPLETED":
            return client.parsing.get(job_id=job_id, expand=EXPAND_FIELDS)
        if status in {"FAILED", "CANCELLED"}:
            error_message = extract_error_message(result)
            message = f"Job {job_id} failed with status {status}."
            if error_message:
                message = f"{message} {error_message}"
            raise RuntimeError(message)
        if status is None:
            raise RuntimeError(f"Job {job_id} returned no status.")

        if time.monotonic() - start_time > DEFAULT_POLL_TIMEOUT_SECONDS:
            raise TimeoutError(
                f"Polling timed out after {DEFAULT_POLL_TIMEOUT_SECONDS:.1f}s (job: {job_id})."
            )
        time.sleep(DEFAULT_POLL_INTERVAL_SECONDS)


def extract_markdown(result: object) -> str:
    markdown_full = getattr(result, "markdown_full", None)
    if isinstance(markdown_full, str) and markdown_full.strip():
        return markdown_full

    markdown = getattr(result, "markdown", None)
    if isinstance(markdown, str) and markdown.strip():
        return markdown

    pages = getattr(markdown, "pages", None) if markdown is not None else None
    if pages:
        collected = [page.markdown for page in pages if getattr(page, "markdown", None)]
        if collected:
            return "\n\n".join(collected)

    items = getattr(result, "items", None)
    pages = getattr(items, "pages", None) if items is not None else None
    if pages:
        collected = [page.markdown for page in pages if getattr(page, "markdown", None)]
        if collected:
            return "\n\n".join(collected)

    return ""


def manual_fetch_command(job_id: str, output_path: Path) -> str:
    output_arg = shlex.quote(str(output_path))
    return f"uv run ./pdf_convert_llamaparse.py --fetch-job {job_id} -o {output_arg}"


def write_joined_markdown(chunk_paths: list[Path], output_path: Path) -> None:
    with output_path.open("w", encoding="utf-8") as output_file:
        for index, chunk_path in enumerate(chunk_paths):
            text = chunk_path.read_text(encoding="utf-8").rstrip()
            if index > 0:
                output_file.write("\n\n")
            output_file.write(text)


class LlamaParseBackend(Backend):
    name = "llamaparse"
    description = "Convert a PDF to Markdown using LlamaParse."
    epilog = (
        "Environment Variables:\n"
        "  LLAMA_CLOUD_API_KEY   LlamaCloud API key\n\n"
        "API Key:\n"
        "  Create one at https://cloud.llamaindex.ai "
        "(API Key -> Generate New Key).\n"
    )
    # --fetch-job retrieves an existing job and needs no PDF, so main() branches
    # before entering the run.
    pdf_path_required = False

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--max-pages",
            type=int,
            help="Limit total pages to process starting from page 1",
        )
        parser.add_argument(
            "--chunk-pages",
            type=int,
            default=DEFAULT_CHUNK_PAGES,
            help=(
                f"Pages per chunk (default: {DEFAULT_CHUNK_PAGES}). "
                "Chunking is always enabled to support resume."
            ),
        )
        parser.add_argument(
            "--tier",
            default=DEFAULT_TIER,
            help=f"LlamaParse tier (default: {DEFAULT_TIER})",
        )
        parser.add_argument(
            "--version",
            default=DEFAULT_VERSION,
            help=f"LlamaParse version (default: {DEFAULT_VERSION})",
        )
        parser.add_argument(
            "--api-key",
            help="LlamaCloud API key (overrides LLAMA_CLOUD_API_KEY)",
        )
        parser.add_argument(
            "--fetch-job",
            help="Fetch markdown for an existing LlamaParse job ID",
        )

    def validate(self, args: argparse.Namespace) -> None:
        if args.chunk_pages < 1:
            raise ConversionError("--chunk-pages must be at least 1.")
        if args.max_pages is not None and args.max_pages < 1:
            raise ConversionError("--max-pages must be at least 1.")

    def convert(self, request: ConversionRequest) -> Outcome:
        args = request.args
        total_pages = request.page_count()

        pages = (
            request.selection.as_one_based()
            if request.selection is not None
            else list(range(1, total_pages + 1))
        )
        if args.max_pages is not None:
            pages = [page for page in pages if page <= args.max_pages]
        if not pages:
            raise ConversionError("No pages selected for parsing.")

        chunks = chunk_pages(pages, args.chunk_pages)
        chunk_paths = [
            chunk_output_path(request.output_path, index) for index in range(1, len(chunks) + 1)
        ]

        print(
            "INFO: Chunking "
            f"{len(pages)} pages into {len(chunks)} chunks of up to "
            f"{args.chunk_pages} pages."
        )

        if not _missing_chunks(chunk_paths):
            print("INFO: All chunk outputs already exist. Joining.")
            write_joined_markdown(chunk_paths, request.output_path)
            return AlreadyWritten()

        client = _build_client(args)
        try:
            file_obj = client.files.create(file=str(request.pdf_path), purpose="parse")
        except Exception as exc:
            raise ConversionError(f"Failed to upload PDF: {exc}") from exc

        file_id = getattr(file_obj, "id", None)
        if not file_id:
            raise ConversionError("Failed to retrieve file ID from upload.")

        for index, chunk_pages_list in enumerate(chunks, start=1):
            chunk_path = chunk_paths[index - 1]
            if chunk_path.exists() and chunk_path.stat().st_size > 0:
                print(f"INFO: Chunk {index}/{len(chunks)} exists, skipping: {chunk_path}")
                continue
            if chunk_path.exists() and chunk_path.stat().st_size == 0:
                print(
                    f"WARNING: Chunk {index}/{len(chunks)} is empty, reprocessing.",
                    file=sys.stderr,
                )

            target_pages = PageSelection.from_pages(chunk_pages_list, total_pages).as_ranges()
            print(f"INFO: Chunk {index}/{len(chunks)} pages {target_pages} -> {chunk_path}")
            markdown_text = _parse_chunk(client, args, file_id, target_pages, chunk_path)
            chunk_path.write_text(markdown_text, encoding="utf-8")
            print(f"INFO: Wrote chunk to: {chunk_path}")

        missing = _missing_chunks(chunk_paths)
        if missing:
            missing_list = ", ".join(str(index) for index in missing)
            raise ConversionError(
                f"Missing chunk outputs; rerun to resume. Missing chunks: {missing_list}"
            )

        write_joined_markdown(chunk_paths, request.output_path)
        return AlreadyWritten()


def _missing_chunks(chunk_paths: list[Path]) -> list[int]:
    return [
        index
        for index, path in enumerate(chunk_paths, start=1)
        if not path.exists() or path.stat().st_size == 0
    ]


def _resolve_api_key(args: argparse.Namespace) -> str:
    api_key = args.api_key or os.getenv("LLAMA_CLOUD_API_KEY")
    if not api_key:
        raise ConversionError(
            "Missing LlamaCloud API key. Set LLAMA_CLOUD_API_KEY or pass --api-key. "
            "Create one at https://cloud.llamaindex.ai (API Key -> Generate New Key)."
        )
    return api_key


def _build_client(args: argparse.Namespace) -> object:
    llama_cloud = require_module("llama_cloud", "llama-cloud")
    return llama_cloud.LlamaCloud(api_key=_resolve_api_key(args))


def _parse_chunk(
    client: object,
    args: argparse.Namespace,
    file_id: str,
    target_pages: str,
    chunk_path: Path,
) -> str:
    """Submit one chunk, wait for it, and return its Markdown."""
    try:
        job = client.parsing.create(
            tier=args.tier,
            version=args.version,
            file_id=file_id,
            page_ranges={"target_pages": target_pages},
        )
    except Exception as exc:
        raise ConversionError(f"LlamaParse request failed: {exc}") from exc

    job_id = extract_job_id(job)
    if not job_id:
        raise ConversionError("LlamaParse did not return a job ID.")

    print(f"INFO: Job ID: {job_id}")
    print(f"INFO: Manual fetch: {manual_fetch_command(job_id, chunk_path)}")

    try:
        result = wait_for_job(client, job_id)
    except (TimeoutError, ConversionError):
        raise
    except Exception as exc:
        raise ConversionError(f"LlamaParse job failed: {exc}") from exc

    markdown_text = extract_markdown(result)
    if not markdown_text.strip():
        raise ConversionError("LlamaParse returned empty markdown output.")
    return markdown_text


def fetch_job(args: argparse.Namespace) -> Path:
    """Fetch a previously submitted job's Markdown and write it out."""
    client = _build_client(args)
    output_path = resolve_output_path_for_job(args.fetch_job, args.output, args.output_dir)

    try:
        result = wait_for_job(client, args.fetch_job)
    except ConversionError:
        raise
    except Exception as exc:
        raise ConversionError(f"Failed to fetch job {args.fetch_job}: {exc}") from exc

    markdown_text = extract_markdown(result)
    if not markdown_text.strip():
        raise ConversionError("LlamaParse returned empty markdown output.")

    output_path.write_text(markdown_text, encoding="utf-8")
    print(f"Wrote Markdown to: {output_path}")
    return output_path


def main() -> None:
    backend = LlamaParseBackend()
    args = build_parser(backend).parse_args()
    try:
        if args.fetch_job:
            fetch_job(args)
        else:
            run(backend, args)
    except ConversionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

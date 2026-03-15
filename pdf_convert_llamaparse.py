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

try:
    from llama_cloud import LlamaCloud
except ImportError as exc:
    print(f"ERROR: Missing required Python package: {exc}", file=sys.stderr)
    print("Install with: pip install llama-cloud", file=sys.stderr)
    sys.exit(1)

try:
    from pypdf import PdfReader
except ImportError as exc:
    print(f"ERROR: Missing required Python package: {exc}", file=sys.stderr)
    print("Install with: pip install pypdf", file=sys.stderr)
    sys.exit(1)


DEFAULT_TIER = "cost_effective"
DEFAULT_VERSION = "latest"
DEFAULT_CHUNK_PAGES = 100
DEFAULT_POLL_TIMEOUT_SECONDS = 300.0
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
EXPAND_FIELDS = ["markdown"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert a PDF to Markdown using LlamaParse.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Environment Variables:\n"
            "  LLAMA_CLOUD_API_KEY   LlamaCloud API key\n\n"
            "API Key:\n"
            "  Create one at https://cloud.llamaindex.ai "
            "(API Key -> Generate New Key).\n"
        ),
    )
    parser.add_argument(
        "pdf_path",
        type=Path,
        nargs="?",
        help="Path to the input PDF file (omit when using --fetch-job)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output markdown file path (default: same name as PDF with .md)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Directory to write output files (defaults to input file directory)",
    )
    parser.add_argument(
        "--page-range",
        help="Comma-separated page numbers or ranges (1-based, e.g., 1,3,5-10)",
    )
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
    return parser


def resolve_output_path(
    input_path: Path,
    output_path: Path | None,
    output_dir: Path | None,
) -> Path:
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        return output_path

    resolved_dir = output_dir or input_path.parent
    resolved_dir.mkdir(parents=True, exist_ok=True)
    return resolved_dir / f"{input_path.stem}.md"


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

    print(
        "ERROR: --fetch-job requires --output or --output-dir.",
        file=sys.stderr,
    )
    sys.exit(1)


def load_pdf_page_count(pdf_path: Path) -> int:
    try:
        reader = PdfReader(str(pdf_path))
        return len(reader.pages)
    except Exception as exc:
        print(f"ERROR: Unable to read PDF pages: {exc}", file=sys.stderr)
        sys.exit(1)


def parse_page_range_spec(page_range: str) -> list[int]:
    pages: list[int] = []
    for raw_part in page_range.split(","):
        part = raw_part.strip()
        if not part:
            continue
        if "-" in part:
            start_raw, end_raw = part.split("-", 1)
            try:
                start = int(start_raw)
                end = int(end_raw)
            except ValueError as exc:
                raise ValueError("Invalid --page-range value.") from exc
            if start < 1 or end < 1 or end < start:
                raise ValueError("Invalid --page-range value.")
            pages.extend(range(start, end + 1))
        else:
            try:
                page = int(part)
            except ValueError as exc:
                raise ValueError("Invalid --page-range value.") from exc
            if page < 1:
                raise ValueError("Invalid --page-range value.")
            pages.append(page)

    if not pages:
        raise ValueError("--page-range produced no pages.")
    return sorted(set(pages))


def format_page_range(pages: list[int]) -> str:
    if not pages:
        return ""

    sorted_pages = sorted(set(pages))
    ranges: list[tuple[int, int]] = []
    start = sorted_pages[0]
    prev = sorted_pages[0]

    for page in sorted_pages[1:]:
        if page == prev + 1:
            prev = page
            continue
        ranges.append((start, prev))
        start = page
        prev = page
    ranges.append((start, prev))

    parts = [f"{begin}-{end}" if begin != end else str(begin) for begin, end in ranges]
    return ",".join(parts)


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


def wait_for_job(client: LlamaCloud, job_id: str) -> object:
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


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.fetch_job:
        api_key = args.api_key or os.getenv("LLAMA_CLOUD_API_KEY")
        if not api_key:
            print(
                "ERROR: Missing LlamaCloud API key. Set LLAMA_CLOUD_API_KEY or pass --api-key.",
                file=sys.stderr,
            )
            print(
                "Create one at https://cloud.llamaindex.ai (API Key -> Generate New Key).",
                file=sys.stderr,
            )
            sys.exit(1)

        output_path = resolve_output_path_for_job(
            args.fetch_job,
            args.output,
            args.output_dir,
        )

        client = LlamaCloud(api_key=api_key)
        try:
            result = wait_for_job(client, args.fetch_job)
        except Exception as exc:
            print(
                f"ERROR: Failed to fetch job {args.fetch_job}: {exc}",
                file=sys.stderr,
            )
            sys.exit(1)

        markdown_text = extract_markdown(result)
        if not markdown_text.strip():
            print("ERROR: LlamaParse returned empty markdown output.", file=sys.stderr)
            sys.exit(1)

        output_path.write_text(markdown_text, encoding="utf-8")
        print(f"Wrote Markdown to: {output_path}")
        return

    if args.pdf_path is None:
        parser.error("pdf_path is required unless --fetch-job is provided.")

    if not args.pdf_path.exists() or not args.pdf_path.is_file():
        print(f"ERROR: PDF file not found: {args.pdf_path}", file=sys.stderr)
        sys.exit(1)

    if args.chunk_pages < 1:
        print("ERROR: --chunk-pages must be at least 1.", file=sys.stderr)
        sys.exit(1)

    if args.max_pages is not None and args.max_pages < 1:
        print("ERROR: --max-pages must be at least 1.", file=sys.stderr)
        sys.exit(1)

    total_pages = load_pdf_page_count(args.pdf_path)
    pages = list(range(1, total_pages + 1))

    if args.page_range:
        try:
            requested_pages = parse_page_range_spec(args.page_range)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
        pages = [page for page in requested_pages if page <= total_pages]
        if len(pages) != len(requested_pages):
            print(
                "WARNING: Some pages were outside the document range and will be ignored.",
                file=sys.stderr,
            )

    if args.max_pages is not None:
        pages = [page for page in pages if page <= args.max_pages]

    if not pages:
        print("ERROR: No pages selected for parsing.", file=sys.stderr)
        sys.exit(1)

    chunks = chunk_pages(pages, args.chunk_pages)
    output_path = resolve_output_path(args.pdf_path, args.output, args.output_dir)
    chunk_paths = [chunk_output_path(output_path, index) for index in range(1, len(chunks) + 1)]

    print(
        "INFO: Chunking "
        f"{len(pages)} pages into {len(chunks)} chunks of up to "
        f"{args.chunk_pages} pages."
    )

    missing_chunks = [
        index
        for index, path in enumerate(chunk_paths, start=1)
        if not path.exists() or path.stat().st_size == 0
    ]
    if not missing_chunks:
        print("INFO: All chunk outputs already exist. Joining.")
        write_joined_markdown(chunk_paths, output_path)
        print(f"Wrote Markdown to: {output_path}")
        return

    api_key = args.api_key or os.getenv("LLAMA_CLOUD_API_KEY")
    if not api_key:
        print(
            "ERROR: Missing LlamaCloud API key. Set LLAMA_CLOUD_API_KEY or pass --api-key.",
            file=sys.stderr,
        )
        print(
            "Create one at https://cloud.llamaindex.ai (API Key -> Generate New Key).",
            file=sys.stderr,
        )
        sys.exit(1)

    client = LlamaCloud(api_key=api_key)
    try:
        file_obj = client.files.create(file=str(args.pdf_path), purpose="parse")
    except Exception as exc:
        print(f"ERROR: Failed to upload PDF: {exc}", file=sys.stderr)
        sys.exit(1)

    file_id = getattr(file_obj, "id", None)
    if not file_id:
        print("ERROR: Failed to retrieve file ID from upload.", file=sys.stderr)
        sys.exit(1)

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

        target_pages = format_page_range(chunk_pages_list)
        print(f"INFO: Chunk {index}/{len(chunks)} pages {target_pages} -> {chunk_path}")

        try:
            job = client.parsing.create(
                tier=args.tier,
                version=args.version,
                file_id=file_id,
                page_ranges={"target_pages": target_pages},
            )
        except Exception as exc:
            print(f"ERROR: LlamaParse request failed: {exc}", file=sys.stderr)
            sys.exit(1)

        job_id = extract_job_id(job)
        if not job_id:
            print("ERROR: LlamaParse did not return a job ID.", file=sys.stderr)
            sys.exit(1)

        print(f"INFO: Job ID: {job_id}")
        print(f"INFO: Manual fetch: {manual_fetch_command(job_id, chunk_path)}")

        try:
            result = wait_for_job(client, job_id)
        except TimeoutError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
        except Exception as exc:
            print(f"ERROR: LlamaParse job failed: {exc}", file=sys.stderr)
            sys.exit(1)

        markdown_text = extract_markdown(result)
        if not markdown_text.strip():
            print("ERROR: LlamaParse returned empty markdown output.", file=sys.stderr)
            sys.exit(1)

        chunk_path.write_text(markdown_text, encoding="utf-8")
        print(f"INFO: Wrote chunk to: {chunk_path}")

    missing_chunks = [
        index
        for index, path in enumerate(chunk_paths, start=1)
        if not path.exists() or path.stat().st_size == 0
    ]
    if missing_chunks:
        missing_list = ", ".join(str(index) for index in missing_chunks)
        print(
            f"ERROR: Missing chunk outputs; rerun to resume. Missing chunks: {missing_list}",
            file=sys.stderr,
        )
        sys.exit(1)

    write_joined_markdown(chunk_paths, output_path)
    print(f"Wrote Markdown to: {output_path}")


if __name__ == "__main__":
    main()

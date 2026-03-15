#!/usr/bin/env python3
# /// script
# dependencies = ["requests"]
# ///
"""
Mathpix PDF to Markdown Converter
Converts PDF files to markdown using the Mathpix API.

Usage:
    # Run with uv (recommended):
    uv run ./pdf_convert_mathpix_api.py input.pdf

    # Standard execution:
    ./pdf_convert_mathpix_api.py input.pdf -o output.md
"""

import os
import sys
import time
import json
import argparse
from pathlib import Path
import requests


class MathpixConverter:
    def __init__(self, app_id=None, app_key=None):
        self.app_id = app_id or os.getenv("MATHPIX_APP_ID")
        self.app_key = app_key or os.getenv("MATHPIX_APP_KEY")
        self.base_url = "https://api.mathpix.com/v3"

        if not self.app_id or not self.app_key:
            raise ValueError(
                "Mathpix credentials not found. Set MATHPIX_APP_ID and MATHPIX_APP_KEY "
                "environment variables or pass them as arguments."
            )

        self.headers = {"app_id": self.app_id, "app_key": self.app_key}

    def upload_pdf(self, pdf_path):
        """Upload PDF to Mathpix and return PDF ID"""
        print(f"Uploading {pdf_path}...")

        with open(pdf_path, "rb") as pdf_file:
            files = {"file": pdf_file}
            data = {
                "options_json": json.dumps(
                    {
                        "conversion_formats": {"md": True},
                        "math_inline_delimiters": ["$", "$"],
                        "math_display_delimiters": ["$$", "$$"],
                        "rm_spaces": True,
                        "enable_tables_fallback": True,
                    }
                )
            }

            response = requests.post(
                f"{self.base_url}/pdf", headers=self.headers, files=files, data=data
            )

            if response.status_code != 200:
                raise Exception(f"Upload failed: {response.status_code} - {response.text}")

            result = response.json()
            if "pdf_id" not in result:
                raise Exception(f"No PDF ID in response: {result}")

            return result["pdf_id"]

    def check_status(self, pdf_id):
        """Check processing status of uploaded PDF"""
        response = requests.get(f"{self.base_url}/pdf/{pdf_id}", headers=self.headers)

        if response.status_code != 200:
            raise Exception(f"Status check failed: {response.status_code} - {response.text}")

        return response.json()

    def wait_for_completion(self, pdf_id, max_wait=300, poll_interval=3):
        """Wait for PDF processing to complete"""
        print("Processing PDF... ", end="", flush=True)

        start_time = time.time()
        while time.time() - start_time < max_wait:
            try:
                status_data = self.check_status(pdf_id)
                status = status_data.get("status", "unknown")

                if status == "completed":
                    print("✅ Complete!")
                    return True
                elif status == "error":
                    error_msg = status_data.get("error", "Unknown error")
                    raise Exception(f"Processing failed: {error_msg}")
                else:
                    print(".", end="", flush=True)
                    time.sleep(poll_interval)

            except requests.RequestException as e:
                print(f"\nError checking status: {e}")
                time.sleep(poll_interval)

        raise Exception("Processing timed out")

    def download_markdown(self, pdf_id):
        """Download the converted markdown content"""
        print("Downloading markdown...")

        response = requests.get(f"{self.base_url}/pdf/{pdf_id}.md", headers=self.headers)

        if response.status_code != 200:
            raise Exception(f"Download failed: {response.status_code} - {response.text}")

        return response.text

    def convert_pdf(self, pdf_path, output_path=None):
        """Complete PDF to markdown conversion workflow"""
        pdf_path = Path(pdf_path)

        if not pdf_path.exists():
            raise FileNotFoundError(f"PDF file not found: {pdf_path}")

        if not pdf_path.suffix.lower() == ".pdf":
            raise ValueError("Input file must be a PDF")

        # Determine output path
        if output_path is None:
            output_path = pdf_path.with_suffix(".md")
        else:
            output_path = Path(output_path)

        try:
            # Upload PDF
            pdf_id = self.upload_pdf(pdf_path)
            print(f"PDF ID: {pdf_id}")

            # Wait for processing
            self.wait_for_completion(pdf_id)

            # Download markdown
            markdown_content = self.download_markdown(pdf_id)

            # Save to file
            output_path.write_text(markdown_content, encoding="utf-8")

            print(f"✅ Successfully converted to: {output_path}")
            print(f"Output size: {len(markdown_content):,} characters")

            return str(output_path)

        except Exception as e:
            print(f"❌ Conversion failed: {e}")
            sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Convert PDF to Markdown using Mathpix API",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s document.pdf
  %(prog)s document.pdf -o notes.md
  %(prog)s document.pdf --app-id YOUR_ID --app-key YOUR_KEY

Environment Variables:
  MATHPIX_APP_ID     Your Mathpix application ID
  MATHPIX_APP_KEY    Your Mathpix application key
        """,
    )

    parser.add_argument("pdf_file", help="PDF file to convert")
    parser.add_argument(
        "-o", "--output", help="Output markdown file (default: same name as PDF with .md extension)"
    )
    parser.add_argument("--app-id", help="Mathpix App ID (overrides environment variable)")
    parser.add_argument("--app-key", help="Mathpix App Key (overrides environment variable)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")

    args = parser.parse_args()

    try:
        converter = MathpixConverter(app_id=args.app_id, app_key=args.app_key)
        converter.convert_pdf(args.pdf_file, args.output)
    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()

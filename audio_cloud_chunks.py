#!/usr/bin/env python3
"""Plan silence-aligned cloud audio uploads, extracting bounded M4A chunks on request."""

import hashlib
import subprocess
import sys
from pathlib import Path

from audio_common import probe_media_duration
from audio_transcribe_vibevoice import detect_silences, plan_chunks

CHUNK_SECONDS = 3600.0


def main() -> None:
    if len(sys.argv) < 4:
        raise SystemExit("Usage: audio_cloud_chunks.py SOURCE TARGET MODEL [--extract]")
    source, target = map(Path, sys.argv[1:3])
    model = sys.argv[3]
    extract = "--extract" in sys.argv[4:]
    duration = probe_media_duration(source, "ffprobe")
    if not duration:
        raise SystemExit("Error: ffprobe could not determine audio duration for chunking.")
    silences = detect_silences(source, noise_db=-30, min_silence=0.5)
    chunks = plan_chunks(duration, CHUNK_SECONDS, silences, overlap=0)
    fingerprint = f"cloud-chunks-v1:{model}\0{[(c.start, c.end) for c in chunks]}"
    digest = hashlib.sha256(fingerprint.encode())
    with source.open("rb") as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(data)
    (target / "cache-key").write_text(digest.hexdigest(), encoding="ascii")
    for index, chunk in enumerate(chunks, 1):
        if extract:
            try:
                subprocess.run(
                    [
                        "ffmpeg",
                        "-v",
                        "error",
                        "-y",
                        "-ss",
                        f"{chunk.start:.3f}",
                        "-i",
                        str(source),
                        "-t",
                        f"{chunk.end - chunk.start:.3f}",
                        "-vn",
                        "-ac",
                        "1",
                        "-ar",
                        "16000",
                        "-b:a",
                        "32k",
                        str(target / f"chunk-{index:04d}.m4a"),
                    ],
                    check=True,
                )
            except subprocess.CalledProcessError as exc:
                raise SystemExit(
                    f"Error: ffmpeg chunk {index} failed (exit {exc.returncode})."
                ) from exc
        print(f"{index:04d}\t{chunk.start:.3f}\t{chunk.end:.3f}")


if __name__ == "__main__":
    main()

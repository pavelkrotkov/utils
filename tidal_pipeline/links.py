"""Shared markdown link application helpers for the TIDAL pipeline."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

from tidal_pipeline.models import TruthRecord
from tidal_pipeline.normalize import is_markdown_separator


@dataclass
class LinkUpdate:
    source_line: int
    title: str
    tidal_id: str

    @property
    def url(self) -> str:
        return f"https://tidal.com/browse/album/{self.tidal_id}"

    @property
    def line(self) -> str:
        return f"[**Listen on TIDAL**]({self.url})"


def load_updates(truth_path: Path) -> List[LinkUpdate]:
    data = json.loads(truth_path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("Truth JSON must be a list of records.")
    records = [TruthRecord.from_dict(item) for item in data if isinstance(item, dict)]

    updates: List[LinkUpdate] = []
    for entry in records:
        status = entry.choice.status
        if status not in {"selected", "auto_selected"}:
            continue
        tidal_id = entry.selected_tidal_id.strip()
        if not tidal_id:
            continue
        source_line = entry.source_line or 0
        if source_line <= 0:
            continue
        title = entry.selected_title.strip()
        updates.append(LinkUpdate(source_line=source_line, title=title, tidal_id=tidal_id))

    updates.sort(key=lambda item: item.source_line, reverse=True)
    return updates


def find_block_end(lines: List[str], start_idx: int) -> int:
    for idx in range(start_idx, len(lines)):
        if is_markdown_separator(lines[idx]):
            return idx
    return len(lines)


def apply_updates(lines: List[str], updates: List[LinkUpdate]) -> Tuple[List[str], int]:
    inserted = 0

    for update in updates:
        start_idx = max(update.source_line - 1, 0)
        end_idx = find_block_end(lines, start_idx)
        block = lines[start_idx:end_idx]

        existing_idx = next(
            (
                start_idx + offset
                for offset, line in enumerate(block)
                if "Listen on TIDAL" in line or "tidal.com/browse/album/" in line
            ),
            -1,
        )
        if existing_idx >= 0:
            if lines[existing_idx] != update.line:
                lines[existing_idx] = update.line
                inserted += 1
            continue

        insert_at = end_idx
        while insert_at > start_idx and not lines[insert_at - 1].strip():
            insert_at -= 1

        snippet: List[str] = []
        if insert_at > start_idx and lines[insert_at - 1].strip():
            snippet.append("")
        snippet.append(update.line)
        snippet.append("")
        lines[insert_at:insert_at] = snippet
        inserted += 1

    return lines, inserted

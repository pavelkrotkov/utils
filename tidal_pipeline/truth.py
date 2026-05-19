"""Persisted truth-record types for TIDAL review/matching output."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from tidal_pipeline.albums import AlbumInput, Candidate, QueryCandidate
from tidal_pipeline.serde import parse_dict, parse_list, parse_string


@dataclass
class Choice:
    status: str = "skip"
    tidal_id: str = ""
    selected_at: str = ""
    manual: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Choice":
        known = {"status", "tidal_id", "selected_at", "manual"}
        return cls(
            status=parse_string(data.get("status")),
            tidal_id=parse_string(data.get("tidal_id")),
            selected_at=parse_string(data.get("selected_at")),
            manual=bool(data.get("manual", False)),
            extra={key: value for key, value in data.items() if key not in known},
        )

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "status": self.status,
            "tidal_id": self.tidal_id,
            "selected_at": self.selected_at,
            "manual": self.manual,
        }
        result.update(self.extra)
        return result


@dataclass
class TruthRecord:
    record_id: str
    source: Dict[str, Any]
    album: AlbumInput
    queries: List[str]
    query_candidates: List[QueryCandidate]
    candidates: List[Candidate]
    top_candidates: List[Candidate]
    choice: Choice
    chosen: Optional[Candidate]
    review: Dict[str, Any]
    meta: Dict[str, Any]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TruthRecord":
        source = parse_dict(data.get("source"))
        album_data = parse_dict(data.get("album")) or data
        choice_data = parse_dict(data.get("choice"))
        chosen_data = data.get("chosen") if isinstance(data.get("chosen"), dict) else None
        return cls(
            record_id=parse_string(data.get("record_id")),
            source=dict(source),
            album=AlbumInput.from_dict(album_data, source=source),
            queries=parse_list(data.get("queries")),
            query_candidates=[
                QueryCandidate.from_dict(item)
                for item in data.get("query_candidates", [])
                if isinstance(item, dict)
            ],
            candidates=[
                Candidate.from_dict(item)
                for item in data.get("candidates", [])
                if isinstance(item, dict)
            ],
            top_candidates=[
                Candidate.from_dict(item)
                for item in data.get("top_candidates", [])
                if isinstance(item, dict)
            ],
            choice=Choice.from_dict(choice_data),
            chosen=Candidate.from_dict(chosen_data) if chosen_data else None,
            review=parse_dict(data.get("review")),
            meta=parse_dict(data.get("meta")),
        )

    @classmethod
    def from_match_result(
        cls,
        album: AlbumInput,
        record_id: str,
        ordered: List[Candidate],
        selected_queries: List[QueryCandidate],
        chosen: Optional[Candidate],
        choice: Choice,
        *,
        top: Optional[int] = None,
        review: Dict[str, Any],
        meta: Dict[str, Any],
    ) -> "TruthRecord":
        return cls(
            record_id=record_id,
            source={
                "file": album.source_file,
                "line": album.source_line,
                "raw": album.source_raw,
                "subsection": album.source_subsection,
                "context": album.source_context,
            },
            album=album,
            queries=[candidate.query for candidate in selected_queries],
            query_candidates=selected_queries,
            candidates=ordered,
            top_candidates=ordered[:top] if top is not None else ordered,
            choice=choice,
            chosen=chosen,
            review=review,
            meta=meta,
        )

    @property
    def source_line(self) -> Optional[int]:
        line = self.source.get("line")
        return int(line) if line else None

    @property
    def selected_tidal_id(self) -> str:
        if self.choice.tidal_id:
            return self.choice.tidal_id
        return self.chosen.id if self.chosen else ""

    @property
    def selected_title(self) -> str:
        if self.chosen and self.chosen.title:
            return self.chosen.title
        return self.album.title

    def to_dict(self) -> Dict[str, Any]:
        return {
            "record_id": self.record_id,
            "source": self.source,
            "album": self.album.to_dict(),
            "queries": self.queries,
            "query_candidates": [candidate.to_dict() for candidate in self.query_candidates],
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "top_candidates": [candidate.to_dict() for candidate in self.top_candidates],
            "choice": self.choice.to_dict(),
            "chosen": self.chosen.to_dict() if self.chosen else None,
            "review": self.review,
            "meta": self.meta,
        }

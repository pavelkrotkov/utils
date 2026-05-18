"""Album and candidate record types for the TIDAL pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class AlbumInput:
    title: str = ""
    composers: List[str] = field(default_factory=list)
    performers: List[str] = field(default_factory=list)
    ensembles: List[str] = field(default_factory=list)
    conductor: str = ""
    label: str = ""
    year: str = ""
    works: List[str] = field(default_factory=list)
    instruments: List[str] = field(default_factory=list)
    source_file: str = ""
    source_line: Optional[int] = None
    source_raw: str = ""
    source_subsection: str = ""
    source_context: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(
        cls, data: Dict[str, Any], source: Optional[Dict[str, Any]] = None
    ) -> "AlbumInput":
        source = source or {}
        context = source.get("context", {})
        return cls(
            title=str(data.get("title", "") or ""),
            composers=_parse_list(data.get("composers")),
            performers=_parse_list(data.get("performers")),
            ensembles=_parse_list(data.get("ensembles")),
            conductor=str(data.get("conductor", "") or ""),
            label=str(data.get("label", "") or ""),
            year=str(data.get("year", "") or ""),
            works=_parse_list(data.get("works")),
            instruments=_parse_list(data.get("instruments")),
            source_file=str(source.get("file", "") or ""),
            source_line=source.get("line"),
            source_raw=str(source.get("raw", "") or ""),
            source_subsection=str(source.get("subsection", "") or ""),
            source_context=context if isinstance(context, dict) else {},
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "composers": self.composers,
            "performers": self.performers,
            "ensembles": self.ensembles,
            "conductor": self.conductor,
            "label": self.label,
            "year": self.year,
            "works": self.works,
            "instruments": self.instruments,
        }


@dataclass
class Candidate:
    id: str
    title: str
    artists: List[str]
    release_date: str
    copyright: str
    score: float
    features: Dict[str, float]
    queries: List[str] = field(default_factory=list)
    track_count: Optional[int] = None
    details_fetched: bool = False
    has_track_count: bool = field(default=True, repr=False)
    has_queries: bool = field(default=True, repr=False)
    has_details_fetched: bool = field(default=True, repr=False)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Candidate":
        return cls(
            id=str(data.get("id", "") or ""),
            title=str(data.get("title", "") or ""),
            artists=_parse_list(data.get("artists")),
            release_date=str(data.get("release_date", "") or ""),
            copyright=str(data.get("copyright", "") or ""),
            track_count=data.get("track_count"),
            score=float(data.get("score", 0.0) or 0.0),
            features=_parse_float_dict(data.get("features")),
            queries=_parse_list(data.get("queries")),
            details_fetched=bool(data.get("details_fetched", False)),
            has_track_count="track_count" in data,
            has_queries="queries" in data,
            has_details_fetched="details_fetched" in data,
        )

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "id": self.id,
            "title": self.title,
            "artists": self.artists,
            "release_date": self.release_date,
            "copyright": self.copyright,
        }
        if self.has_track_count:
            result["track_count"] = self.track_count
        result["score"] = self.score
        result["features"] = self.features
        if self.has_queries:
            result["queries"] = self.queries
        if self.has_details_fetched:
            result["details_fetched"] = self.details_fetched
        return result


@dataclass
class QueryCandidate:
    template: str
    query: str

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "QueryCandidate":
        return cls(
            template=str(data.get("template", "") or ""),
            query=str(data.get("query", "") or ""),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "template": self.template,
            "query": self.query,
        }


def _parse_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    return [str(value)]


def _parse_float_dict(value: Any) -> Dict[str, float]:
    if not isinstance(value, dict):
        return {}
    parsed: Dict[str, float] = {}
    for key, item in value.items():
        parsed[str(key)] = float(item)
    return parsed

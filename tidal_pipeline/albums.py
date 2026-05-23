"""Album and candidate record types for the TIDAL pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tidal_pipeline.serde import parse_float_dict, parse_list, parse_string


@dataclass
class AlbumInput:
    title: str = ""
    composers: list[str] = field(default_factory=list)
    performers: list[str] = field(default_factory=list)
    ensembles: list[str] = field(default_factory=list)
    conductor: str = ""
    label: str = ""
    year: str = ""
    works: list[str] = field(default_factory=list)
    instruments: list[str] = field(default_factory=list)
    source_file: str = ""
    source_line: int | None = None
    source_raw: str = ""
    source_subsection: str = ""
    source_context: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any], source: dict[str, Any] | None = None) -> AlbumInput:
        source = source or {}
        context = source.get("context", {})
        return cls(
            title=parse_string(data.get("title")),
            composers=parse_list(data.get("composers")),
            performers=parse_list(data.get("performers")),
            ensembles=parse_list(data.get("ensembles")),
            conductor=parse_string(data.get("conductor")),
            label=parse_string(data.get("label")),
            year=parse_string(data.get("year")),
            works=parse_list(data.get("works")),
            instruments=parse_list(data.get("instruments")),
            source_file=parse_string(source.get("file")),
            source_line=source.get("line"),
            source_raw=parse_string(source.get("raw")),
            source_subsection=parse_string(source.get("subsection")),
            source_context=context if isinstance(context, dict) else {},
        )

    def to_dict(self) -> dict[str, Any]:
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
    artists: list[str]
    release_date: str
    copyright: str
    score: float
    features: dict[str, float]
    queries: list[str] = field(default_factory=list)
    track_count: int | None = None
    details_fetched: bool = False
    has_track_count: bool = field(default=True, repr=False)
    has_queries: bool = field(default=True, repr=False)
    has_details_fetched: bool = field(default=True, repr=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Candidate:
        return cls(
            id=parse_string(data.get("id")),
            title=parse_string(data.get("title")),
            artists=parse_list(data.get("artists")),
            release_date=parse_string(data.get("release_date")),
            copyright=parse_string(data.get("copyright")),
            track_count=data.get("track_count"),
            score=float(data.get("score", 0.0) or 0.0),
            features=parse_float_dict(data.get("features")),
            queries=parse_list(data.get("queries")),
            details_fetched=bool(data.get("details_fetched", False)),
            has_track_count="track_count" in data,
            has_queries="queries" in data,
            has_details_fetched="details_fetched" in data,
        )

    def to_dict(self) -> dict[str, Any]:
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
    def from_dict(cls, data: dict[str, Any]) -> QueryCandidate:
        return cls(
            template=parse_string(data.get("template")),
            query=parse_string(data.get("query")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "template": self.template,
            "query": self.query,
        }

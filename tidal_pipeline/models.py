"""Shared TIDAL pipeline constants and lightweight models."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


DEFAULT_WEIGHTS = {
    "title": 0.35,
    "composer": 0.1,
    "performer": 0.25,
    "ensemble": 0.15,
    "conductor": 0.1,
    "instrument": 0.05,
    "label": 0.1,
    "year": 0.05,
}

DEFAULT_TEMPLATE_WEIGHTS = {
    "title": 1.0,
    "title_short": 1.0,
    "title_sorted": 0.7,
    "title_reversed": 0.7,
    "title_ngrams": 0.8,
    "title_shuffle": 0.6,
    "work": 0.9,
    "work_ngrams": 0.8,
    "composer": 1.4,
    "composer_title": 1.2,
    "composer_work": 1.2,
    "performer": 1.7,
    "performer_title": 2.3,
    "performer_instrument": 0.9,
    "performer_ensemble": 2.2,
    "performer_composer": 2.1,
    "performer_work": 2.0,
    "ensemble": 1.8,
    "ensemble_title": 1.5,
    "conductor": 1.1,
    "conductor_title": 1.4,
    "conductor_composer": 1.0,
    "instrument_title": 1.1,
    "label_title": 0.8,
}

PERFORMER_WEIGHT_BOOST = 1.5

STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "vol",
    "volume",
    "edition",
    "series",
    "part",
    "no",
    "op",
    "major",
    "minor",
}

ENSEMBLE_HINTS = {
    "orchestra",
    "philharmonic",
    "symphony",
    "ensemble",
    "quartet",
    "quartett",
    "trio",
    "quintet",
    "choir",
    "chorus",
    "consort",
    "players",
    "sinfonia",
    "academy",
    "capella",
    "coro",
    "chamber",
    "opera",
    "filharmonica",
    "radio",
    "baroque",
    "knot",
    "jupiter",
    "inalto",
    "psophos",
}

SKIP_SEGMENTS = {"sols", "sols.", "soloists", "soloist"}
SKIP_ARTIST_SEGMENTS = SKIP_SEGMENTS | {"with"}

GENERIC_ARTIST_TOKENS = {
    "orchestra",
    "philharmonic",
    "symphony",
    "ensemble",
    "quartet",
    "quartett",
    "trio",
    "quintet",
    "choir",
    "chorus",
    "consort",
    "players",
    "sinfonia",
    "academy",
    "capella",
    "coro",
    "chamber",
    "opera",
    "baroque",
}

GENERIC_TITLE_TOKENS = {
    "chamber",
    "works",
    "work",
    "symphonies",
    "symphony",
    "sonatas",
    "sonata",
    "concertos",
    "concerto",
    "quartets",
    "quartet",
    "lieder",
    "orchestral",
    "solo",
    "piano",
    "violin",
    "string",
}

GENERIC_LINE_PREFIXES = {
    "sols",
    "sols incl",
    "incl",
    "label",
    "with",
}

INSTRUMENT_MAP = {
    "pf": "piano",
    "fp": "fortepiano",
    "pno": "piano",
    "piano": "piano",
    "fortepiano": "fortepiano",
    "vn": "violin",
    "violin": "violin",
    "va": "viola",
    "viola": "viola",
    "vc": "cello",
    "cello": "cello",
    "db": "double bass",
    "double": "double",
    "bass": "bass",
    "fl": "flute",
    "flute": "flute",
    "ob": "oboe",
    "oboe": "oboe",
    "cl": "clarinet",
    "clarinet": "clarinet",
    "bn": "bassoon",
    "bassoon": "bassoon",
    "hn": "horn",
    "horn": "horn",
    "tpt": "trumpet",
    "trumpet": "trumpet",
    "trb": "trombone",
    "trombone": "trombone",
    "hp": "harp",
    "harp": "harp",
    "org": "organ",
    "organ": "organ",
    "hpd": "harpsichord",
    "harpsichord": "harpsichord",
    "mandolin": "mandolin",
    "lute": "lute",
    "cornett": "cornett",
    "gtr": "guitar",
    "guitar": "guitar",
    "perc": "percussion",
    "percussion": "percussion",
    "sop": "soprano",
    "soprano": "soprano",
    "mez": "mezzo",
    "mezzo-soprano": "mezzo",
    "mezzo": "mezzo",
    "counterten": "countertenor",
    "countertenor": "countertenor",
    "ten": "tenor",
    "tenor": "tenor",
    "bar": "baritone",
    "baritone": "baritone",
    "cond": "conductor",
    "conductor": "conductor",
}

INSTRUMENT_ABBREVS = set(INSTRUMENT_MAP.keys())
SEPARATOR_RE = re.compile(r"^\*\s+\*\s+\*$")


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
            status=str(data.get("status", "") or ""),
            tidal_id=str(data.get("tidal_id", "") or ""),
            selected_at=str(data.get("selected_at", "") or ""),
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
        source = data.get("source", {}) if isinstance(data.get("source"), dict) else {}
        album_data = data.get("album", {}) if isinstance(data.get("album"), dict) else data
        choice_data = data.get("choice", {}) if isinstance(data.get("choice"), dict) else {}
        chosen_data = data.get("chosen") if isinstance(data.get("chosen"), dict) else None
        return cls(
            record_id=str(data.get("record_id", "") or ""),
            source=dict(source),
            album=AlbumInput.from_dict(album_data, source=source),
            queries=_parse_list(data.get("queries")),
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
            review=data.get("review", {}) if isinstance(data.get("review"), dict) else {},
            meta=data.get("meta", {}) if isinstance(data.get("meta"), dict) else {},
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
            top_candidates=ordered[: int(top or len(ordered))],
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
        try:
            parsed[str(key)] = float(item)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"features[{key!r}] must be a number, got {item!r}") from exc
    return parsed

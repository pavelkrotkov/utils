"""Shared TIDAL pipeline constants and lightweight models."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional


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


@dataclass
class QueryCandidate:
    template: str
    query: str

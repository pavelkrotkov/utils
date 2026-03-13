#!/usr/bin/env python3
# /// script
# dependencies = [
#   "requests",
# ]
# ///
"""Interactive ground-truth labeling for TIDAL album matching."""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from tidal_pipeline.client import (
    AlbumDetail,
    AlbumHit,
    TIDAL_API_BASE,
    TIDAL_COUNTRY_CODE,
    TOKEN_FILE_DIR,
    TOKEN_FILE_NAME,
    TidalClient,
    get_valid_token,
    resolve_country_code,
)
from tidal_pipeline.models import (
    DEFAULT_TEMPLATE_WEIGHTS,
    DEFAULT_WEIGHTS,
    GENERIC_TITLE_TOKENS,
    PERFORMER_WEIGHT_BOOST,
)
from tidal_pipeline.normalize import (
    artist_tokens_from_list,
    extract_numeric_tokens,
    extract_year,
    normalize,
    normalize_instruments,
    normalize_with_symbols,
    overlap_score,
    phrase_overlap_score,
    split_tokens,
    tokenize,
    tokens_from_list,
)


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


def score_hit(
    album: AlbumInput, hit: AlbumHit, weights: Dict[str, float]
) -> Tuple[float, Dict[str, float]]:
    title_values = [album.title] + album.works
    title_tokens = tokens_from_list(title_values)
    generic_title = bool(title_tokens) and title_tokens.issubset(GENERIC_TITLE_TOKENS)
    composer_tokens = tokens_from_list(album.composers)
    performer_tokens = artist_tokens_from_list(album.performers)
    ensemble_tokens = artist_tokens_from_list(album.ensembles)
    conductor_tokens = artist_tokens_from_list([album.conductor])
    instrument_tokens = normalize_instruments(album.instruments)
    requested_numbers = extract_numeric_tokens(title_values)

    hit_title_tokens = tokenize(hit.title)
    hit_artist_tokens = artist_tokens_from_list(hit.artists)
    hit_all_tokens = hit_title_tokens | hit_artist_tokens
    hit_numbers = extract_numeric_tokens([hit.title])
    composer_phrase = phrase_overlap_score(album.composers, [hit.title, *hit.artists])
    performer_phrase = phrase_overlap_score(album.performers, hit.artists)
    ensemble_phrase = phrase_overlap_score(album.ensembles, hit.artists)
    conductor_phrase = phrase_overlap_score([album.conductor], hit.artists)

    features = {
        "title": overlap_score(title_tokens, hit_title_tokens),
        "composer": max(composer_phrase, overlap_score(composer_tokens, hit_all_tokens) * 0.6),
        "performer": max(performer_phrase, overlap_score(performer_tokens, hit_all_tokens) * 0.6),
        "ensemble": max(ensemble_phrase, overlap_score(ensemble_tokens, hit_all_tokens)),
        "conductor": max(conductor_phrase, overlap_score(conductor_tokens, hit_all_tokens) * 0.6),
        "instrument": overlap_score(instrument_tokens, hit_title_tokens),
        "label": 0.0,
        "year": 0.0,
    }

    label_norm = normalize(album.label)
    if label_norm and len(label_norm) > 2:
        copy_norm = normalize(hit.copyright)
        if label_norm in copy_norm:
            features["label"] = 1.0

    album_year = extract_year(album.year)
    hit_year = extract_year(hit.release_date)
    if album_year and hit_year and album_year == hit_year:
        features["year"] = 1.0

    normalized_title = normalize(album.title)
    normalized_hit_title = normalize(hit.title)
    if normalized_title and normalized_hit_title:
        if normalized_title in normalized_hit_title or normalized_hit_title in normalized_title:
            features["title"] = max(features["title"], 0.95)

    score = sum(features[key] * weights.get(key, 0.0) for key in features)
    artist_support = max(features["performer"], features["ensemble"], features["conductor"])
    if performer_tokens or ensemble_tokens or conductor_tokens:
        if artist_support == 0:
            score *= 0.45
        elif artist_support < 0.34:
            score *= 0.7

    if composer_tokens and features["composer"] == 0 and artist_support == 0:
        score *= 0.8
    elif composer_tokens and features["composer"] == 0 and generic_title:
        score *= 0.55

    if label_norm and features["label"] == 0 and artist_support == 0:
        score *= 0.85

    if requested_numbers and hit_numbers:
        if not (requested_numbers & hit_numbers):
            score *= 0.55
        elif len(requested_numbers) > 1 and not requested_numbers.issubset(hit_numbers):
            score *= 0.8

    return score, features


def build_query_candidates(
    album: AlbumInput,
    rng: random.Random,
    shuffle_count: int = 2,
) -> List[QueryCandidate]:
    seen: set[str] = set()
    candidates: List[QueryCandidate] = []

    def add_query(template: str, value: str) -> None:
        cleaned = " ".join(value.split())
        key = cleaned.lower()
        if cleaned and key not in seen:
            seen.add(key)
            candidates.append(QueryCandidate(template=template, query=cleaned))

    def add_title_variants(title: str) -> None:
        if not title:
            return
        tokens = split_tokens(title)
        add_query("title", title)
        if not tokens:
            return
        add_query("title_short", " ".join(tokens[:4]))
        if len(tokens) > 1:
            add_query("title_reversed", " ".join(tokens[::-1]))
            add_query("title_sorted", " ".join(sorted(tokens)))
        for size in range(2, min(4, len(tokens)) + 1):
            for idx in range(0, len(tokens) - size + 1):
                add_query("title_ngrams", " ".join(tokens[idx : idx + size]))
        if len(tokens) > 2 and shuffle_count:
            for _ in range(shuffle_count):
                shuffled = tokens[:]
                rng.shuffle(shuffled)
                add_query("title_shuffle", " ".join(shuffled[: min(4, len(shuffled))]))

    def add_work_variants(work: str) -> None:
        if not work:
            return
        tokens = split_tokens(work)
        add_query("work", work)
        for size in range(2, min(4, len(tokens)) + 1):
            for idx in range(0, len(tokens) - size + 1):
                add_query("work_ngrams", " ".join(tokens[idx : idx + size]))

    add_title_variants(album.title)
    for work in album.works:
        add_work_variants(work)

    for composer in album.composers:
        add_query("composer", composer)
        short_title = " ".join(split_tokens(album.title)[:4]) if album.title else ""
        if short_title:
            add_query("composer_title", f"{composer} {short_title}")
            add_query("composer_title", f"{short_title} {composer}")
        if album.title:
            add_query("composer_title", f"{composer} {album.title}")
            add_query("composer_title", f"{album.title} {composer}")
        for work in album.works:
            work_short = " ".join(split_tokens(work)[:4]) if work else ""
            if work_short:
                add_query("composer_work", f"{composer} {work_short}")
                add_query("composer_work", f"{work_short} {composer}")
            if work:
                add_query("composer_work", f"{composer} {work}")
                add_query("composer_work", f"{work} {composer}")

    for performer in album.performers:
        add_query("performer", performer)
        short_title = " ".join(split_tokens(album.title)[:4]) if album.title else ""
        if short_title:
            add_query("performer_title", f"{performer} {short_title}")
            add_query("performer_title", f"{short_title} {performer}")
        if album.title:
            add_query("performer_title", f"{performer} {album.title}")
            add_query("performer_title", f"{album.title} {performer}")
        for instrument in album.instruments:
            add_query("performer_instrument", f"{performer} {instrument}")
        for ensemble in album.ensembles:
            add_query("performer_ensemble", f"{performer} {ensemble}")
        for composer in album.composers:
            add_query("performer_composer", f"{performer} {composer}")
        for work in album.works:
            add_query("performer_work", f"{performer} {work}")

    for ensemble in album.ensembles:
        add_query("ensemble", ensemble)
        short_title = " ".join(split_tokens(album.title)[:4]) if album.title else ""
        if short_title:
            add_query("ensemble_title", f"{ensemble} {short_title}")
            add_query("ensemble_title", f"{short_title} {ensemble}")
        if album.title:
            add_query("ensemble_title", f"{ensemble} {album.title}")
        for composer in album.composers:
            add_query("ensemble_title", f"{ensemble} {composer}")
        for work in album.works:
            add_query("ensemble_title", f"{ensemble} {work}")

    if album.conductor:
        add_query("conductor", album.conductor)
        short_title = " ".join(split_tokens(album.title)[:4]) if album.title else ""
        if short_title:
            add_query("conductor_title", f"{album.conductor} {short_title}")
            add_query("conductor_title", f"{short_title} {album.conductor}")
        if album.title:
            add_query("conductor_title", f"{album.conductor} {album.title}")
        for composer in album.composers:
            add_query("conductor_composer", f"{composer} {album.conductor}")
        for ensemble in album.ensembles:
            add_query("conductor_composer", f"{album.conductor} {ensemble}")
        for work in album.works:
            add_query("conductor_title", f"{album.conductor} {work}")

    for instrument in album.instruments:
        short_title = " ".join(split_tokens(album.title)[:4]) if album.title else ""
        if short_title:
            add_query("instrument_title", f"{short_title} {instrument}")

    if album.label and album.title:
        add_query("label_title", f"{album.title} {album.label}")
        for performer in album.performers:
            add_query("label_title", f"{performer} {album.label}")
        for ensemble in album.ensembles:
            add_query("label_title", f"{ensemble} {album.label}")

    return candidates


def weighted_sample(
    candidates: List[QueryCandidate],
    weights: List[float],
    limit: int,
    rng: random.Random,
) -> List[QueryCandidate]:
    selected: List[QueryCandidate] = []
    pool = list(zip(candidates, weights))
    for _ in range(min(limit, len(pool))):
        total = sum(weight for _, weight in pool)
        if total <= 0:
            break
        pick = rng.random() * total
        cumulative = 0.0
        for idx, (cand, weight) in enumerate(pool):
            cumulative += weight
            if pick <= cumulative:
                selected.append(cand)
                pool.pop(idx)
                break
    return selected


def query_family(template: str) -> str:
    if template.startswith("performer"):
        return "performer"
    if template.startswith("ensemble"):
        return "ensemble"
    if template.startswith("composer"):
        return "composer"
    if template.startswith("conductor"):
        return "conductor"
    if template.startswith("instrument"):
        return "instrument"
    if template.startswith("label"):
        return "label"
    if template.startswith("work"):
        return "work"
    return "title"


def select_query_candidates(
    candidates: List[QueryCandidate],
    template_weights: Dict[str, float],
    max_queries: Optional[int],
    rng: random.Random,
) -> List[QueryCandidate]:
    if not max_queries or len(candidates) <= max_queries:
        return candidates

    ranked: List[Tuple[float, int, int, QueryCandidate]] = []
    for candidate in candidates:
        base = template_weights.get(candidate.template, 0.5)
        if candidate.template.startswith("performer"):
            base *= PERFORMER_WEIGHT_BOOST
        token_count = len(split_tokens(candidate.query))
        compactness = -abs(token_count - 5)
        brevity = -len(candidate.query)
        ranked.append((base, compactness, brevity, candidate))

    ranked.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    selected: List[QueryCandidate] = []
    seen_queries: set[str] = set()
    family_limits = {
        "title": 2,
        "work": 2,
        "performer": 4,
        "ensemble": 2,
        "composer": 3,
        "conductor": 2,
        "label": 1,
        "instrument": 1,
    }
    family_template_limits = {
        "performer": {
            "performer": 1,
            "performer_composer": 1,
            "performer_ensemble": 1,
            "performer_title": 2,
            "performer_work": 1,
            "performer_instrument": 1,
        },
        "composer": {
            "composer": 1,
            "composer_title": 1,
            "composer_work": 2,
        },
        "ensemble": {
            "ensemble": 1,
            "ensemble_title": 2,
        },
        "conductor": {
            "conductor": 1,
            "conductor_title": 1,
            "conductor_composer": 1,
        },
    }

    for family, family_limit in family_limits.items():
        for _, _, _, candidate in ranked:
            if len(selected) >= max_queries:
                break
            if query_family(candidate.template) != family:
                continue
            if candidate.query in seen_queries:
                continue
            template_limit = family_template_limits.get(family, {}).get(candidate.template)
            if template_limit is not None:
                already = sum(1 for item in selected if item.template == candidate.template)
                if already >= template_limit:
                    continue
            selected.append(candidate)
            seen_queries.add(candidate.query)
            if sum(1 for item in selected if query_family(item.template) == family) >= family_limit:
                break

    for _, _, _, candidate in ranked:
        if len(selected) >= max_queries:
            break
        if candidate.query in seen_queries:
            continue
        selected.append(candidate)
        seen_queries.add(candidate.query)

    return selected


def parse_list(value) -> List[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    return []


def load_album_inputs(path: Path) -> List[AlbumInput]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "albums" in raw:
        entries = raw["albums"]
    elif isinstance(raw, list):
        entries = raw
    else:
        raise ValueError("Input JSON must be a list or have an 'albums' key.")

    albums: List[AlbumInput] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        source = entry.get("source", {}) if isinstance(entry.get("source"), dict) else {}
        album = entry.get("album", {}) if isinstance(entry.get("album"), dict) else entry

        albums.append(
            AlbumInput(
                title=str(album.get("title", "") or ""),
                composers=parse_list(album.get("composers")),
                performers=parse_list(album.get("performers")),
                ensembles=parse_list(album.get("ensembles")),
                conductor=str(album.get("conductor", "") or ""),
                label=str(album.get("label", "") or ""),
                year=str(album.get("year", "") or ""),
                works=parse_list(album.get("works")),
                instruments=parse_list(album.get("instruments")),
                source_file=str(source.get("file", "") or ""),
                source_line=source.get("line"),
                source_raw=str(source.get("raw", "") or ""),
                source_subsection=str(source.get("subsection", "") or ""),
                source_context=source.get("context", {}) if isinstance(source.get("context"), dict) else {},
            )
        )
    return albums


def load_weights(path: Optional[Path]) -> Dict[str, float]:
    if not path or not path.exists():
        return dict(DEFAULT_WEIGHTS)
    raw = json.loads(path.read_text(encoding="utf-8"))
    weights = dict(DEFAULT_WEIGHTS)
    for key, value in raw.items():
        if key in weights:
            weights[key] = float(value)
    return weights


def load_training_model(path: Optional[Path]) -> Tuple[Dict[str, float], Dict[str, float]]:
    if not path or not path.exists():
        return dict(DEFAULT_WEIGHTS), dict(DEFAULT_TEMPLATE_WEIGHTS)
    raw = json.loads(path.read_text(encoding="utf-8"))
    weights = dict(DEFAULT_WEIGHTS)
    template_weights = dict(DEFAULT_TEMPLATE_WEIGHTS)
    for key, value in raw.get("weights", {}).items():
        if key in weights:
            weights[key] = float(value)
    for key, value in raw.get("template_weights", {}).items():
        if key in template_weights:
            template_weights[key] = float(value)
    return weights, template_weights


def save_training_model(path: Path, model: Dict) -> None:
    path.write_text(json.dumps(model, indent=2), encoding="utf-8")


def load_truth_records(path: Path) -> List[Dict]:
    if not path.exists():
        raise RuntimeError(f"Truth file not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise RuntimeError("Truth file must be a JSON array.")
    return [entry for entry in data if isinstance(entry, dict)]


def update_scoring_weights(
    weights: Dict[str, float], feature_sums: Dict[str, float], count: int
) -> Dict[str, float]:
    if count == 0:
        return weights
    updated = dict(weights)
    for key in updated:
        avg = feature_sums.get(key, 0.0) / count
        updated[key] = round((updated[key] * 0.7) + (avg * 0.3), 4)
    return updated


def update_template_weights(
    template_weights: Dict[str, float], stats: Dict[str, Dict[str, int]]
) -> Dict[str, float]:
    updated = dict(template_weights)
    for template, counts in stats.items():
        attempts = counts.get("attempts", 0)
        hits = counts.get("hits", 0)
        if attempts == 0:
            continue
        success = hits / attempts
        base = updated.get(template, 0.5)
        updated[template] = round(base * (0.5 + success), 4)
    return updated


def train_coverage(
    client: TidalClient,
    truth_records: List[Dict],
    weights: Dict[str, float],
    template_weights: Dict[str, float],
    args: argparse.Namespace,
    rng: random.Random,
) -> Tuple[Dict[str, float], Dict[str, float], Dict]:
    start_time = time.time()
    stats: Dict[str, Dict[str, int]] = {}
    feature_sums: Dict[str, float] = {key: 0.0 for key in weights}
    feature_count = 0

    train_records = truth_records[: args.train_limit]
    targets: List[Tuple[str, AlbumInput, str]] = []
    for record in train_records:
        choice = record.get("choice") or {}
        tidal_id = choice.get("tidal_id") or ""
        if not tidal_id:
            continue
        album = album_from_record(record)
        record_id = record.get("record_id") or build_record_id(album)
        targets.append((record_id, album, tidal_id))

    covered: set[str] = set()
    api_calls = 0
    iteration = 0

    while iteration < args.train_iterations:
        if (time.time() - start_time) / 60 >= args.train_minutes:
            break
        if api_calls >= args.train_max_calls:
            break
        uncovered = [(rid, alb, tid) for rid, alb, tid in targets if rid not in covered]
        if not uncovered:
            break

        remaining_calls = args.train_max_calls - api_calls
        per_record = max(1, remaining_calls // max(1, len(uncovered)))
        max_queries = args.max_queries or per_record
        max_queries = min(max_queries, per_record)

        for record_id, album, tidal_id in uncovered:
            if api_calls >= args.train_max_calls:
                break
            if (time.time() - start_time) / 60 >= args.train_minutes:
                break

            candidates = build_query_candidates(album, rng, shuffle_count=args.shuffle_count)
            selected = select_query_candidates(candidates, template_weights, max_queries, rng)

            for candidate in selected:
                if api_calls >= args.train_max_calls:
                    break
                stats.setdefault(candidate.template, {"attempts": 0, "hits": 0})
                stats[candidate.template]["attempts"] += 1
                hits = client.search_albums(candidate.query, limit=args.limit)
                api_calls += 1

                if any(hit.id == tidal_id for hit in hits):
                    stats[candidate.template]["hits"] += 1
                    covered.add(record_id)
                    for hit in hits:
                        if hit.id == tidal_id:
                            _, features = score_hit(album, hit, weights)
                            for key, value in features.items():
                                feature_sums[key] += value
                            feature_count += 1
                            break
                    break

            if args.sleep and api_calls < args.train_max_calls:
                time.sleep(args.sleep)

        template_weights = update_template_weights(template_weights, stats)
        iteration += 1

    weights = update_scoring_weights(weights, feature_sums, feature_count)

    model = {
        "meta": {
            "coverage": len(covered) / max(1, len(targets)),
            "covered": len(covered),
            "total": len(targets),
            "iterations": iteration,
            "api_calls": api_calls,
            "minutes": round((time.time() - start_time) / 60, 2),
            "seed": args.seed,
            "train_limit": args.train_limit,
            "max_calls": args.train_max_calls,
        },
        "weights": weights,
        "template_weights": template_weights,
        "template_stats": stats,
        "uncovered": [rid for rid, _, _ in targets if rid not in covered],
    }

    return weights, template_weights, model


def album_from_record(record: Dict) -> AlbumInput:
    source = record.get("source", {}) if isinstance(record.get("source"), dict) else {}
    album = record.get("album", {}) if isinstance(record.get("album"), dict) else record
    return AlbumInput(
        title=str(album.get("title", "") or ""),
        composers=parse_list(album.get("composers")),
        performers=parse_list(album.get("performers")),
        ensembles=parse_list(album.get("ensembles")),
        conductor=str(album.get("conductor", "") or ""),
        label=str(album.get("label", "") or ""),
        year=str(album.get("year", "") or ""),
        works=parse_list(album.get("works")),
        instruments=parse_list(album.get("instruments")),
        source_file=str(source.get("file", "") or ""),
        source_line=source.get("line"),
        source_raw=str(source.get("raw", "") or ""),
        source_subsection=str(source.get("subsection", "") or ""),
        source_context=source.get("context", {}) if isinstance(source.get("context"), dict) else {},
    )


def album_to_dict(album: AlbumInput) -> Dict:
    return {
        "title": album.title,
        "composers": album.composers,
        "performers": album.performers,
        "ensembles": album.ensembles,
        "conductor": album.conductor,
        "label": album.label,
        "year": album.year,
        "works": album.works,
        "instruments": album.instruments,
    }


def candidate_to_dict(candidate: Candidate) -> Dict:
    return {
        "id": candidate.id,
        "title": candidate.title,
        "artists": candidate.artists,
        "release_date": candidate.release_date,
        "copyright": candidate.copyright,
        "track_count": candidate.track_count,
        "score": candidate.score,
        "features": candidate.features,
        "queries": candidate.queries,
        "details_fetched": candidate.details_fetched,
    }


def query_candidate_to_dict(candidate: QueryCandidate) -> Dict:
    return {
        "template": candidate.template,
        "query": candidate.query,
    }


def build_record_id(album: AlbumInput) -> str:
    return "|".join(
        [
            album.source_file or "",
            str(album.source_line or 0),
            album.title or "",
        ]
    )


def build_source_payload(album: AlbumInput, input_path: Path) -> Dict:
    return {
        "file": album.source_file,
        "line": album.source_line,
        "raw": album.source_raw,
        "subsection": album.source_subsection,
        "context": album.source_context,
    }


def search_candidates_for_album(
    client: TidalClient,
    album: AlbumInput,
    weights: Dict[str, float],
    selected_queries: List[QueryCandidate],
    limit: int,
    sleep_seconds: float,
) -> List[Candidate]:
    candidates_map: Dict[str, Candidate] = {}

    for query_candidate in selected_queries:
        hits = client.search_albums(query_candidate.query, limit=limit)
        for hit in hits:
            if hit.id in candidates_map:
                if query_candidate.query not in candidates_map[hit.id].queries:
                    candidates_map[hit.id].queries.append(query_candidate.query)
                continue

            score, features = score_hit(album, hit, weights)
            candidates_map[hit.id] = Candidate(
                id=hit.id,
                title=hit.title,
                artists=hit.artists,
                release_date=hit.release_date,
                copyright=hit.copyright,
                score=score,
                features=features,
                queries=[query_candidate.query],
            )

        if sleep_seconds:
            time.sleep(sleep_seconds)

    return sorted(
        candidates_map.values(),
        key=lambda c: (c.score, len(c.queries), c.title.lower()),
        reverse=True,
    )


def choose_auto_candidate(
    ordered: List[Candidate],
    score_threshold: float,
    recent_year: int,
    recent_threshold: float,
) -> Tuple[Optional[Candidate], str]:
    if not ordered:
        return None, ""

    top = ordered[0]
    title_signal = top.features.get("title", 0.0)
    artist_signal = max(
        top.features.get("performer", 0.0),
        top.features.get("ensemble", 0.0),
        top.features.get("conductor", 0.0),
    )
    label_signal = top.features.get("label", 0.0)
    if top.score >= score_threshold:
        return top, f"score>={score_threshold:.2f}"

    release_year = extract_year(top.release_date) or ""
    if release_year == str(recent_year) and top.score > recent_threshold:
        if title_signal >= 0.25:
            return top, f"release_year=={recent_year} and score>{recent_threshold:.2f}"
        if artist_signal >= 0.75 and label_signal >= 1.0:
            return top, (
                f"release_year=={recent_year} and score>{recent_threshold:.2f} "
                "with strong artist+label match"
            )

    return None, ""


def build_record(
    album: AlbumInput,
    input_path: Path,
    record_id: str,
    ordered: List[Candidate],
    selected_queries: List[QueryCandidate],
    chosen: Optional[Candidate],
    choice: Dict[str, object],
    weights: Dict[str, float],
    args: argparse.Namespace,
    auto_reason: str,
    mode: str,
) -> Dict:
    top_candidate = ordered[0] if ordered else None
    return {
        "record_id": record_id,
        "source": build_source_payload(album, input_path),
        "album": album_to_dict(album),
        "queries": [candidate.query for candidate in selected_queries],
        "query_candidates": [query_candidate_to_dict(candidate) for candidate in selected_queries],
        "candidates": [candidate_to_dict(candidate) for candidate in ordered],
        "top_candidates": [candidate_to_dict(candidate) for candidate in ordered[: args.top]],
        "choice": choice,
        "chosen": candidate_to_dict(chosen) if chosen else None,
        "review": {
            "mode": mode,
            "top_score": round(top_candidate.score, 3) if top_candidate else 0.0,
            "top_release_year": extract_year(top_candidate.release_date) if top_candidate else "",
            "candidate_count": len(ordered),
            "auto_reason": auto_reason,
            "auto_threshold": args.auto_threshold,
            "recent_year": args.auto_recent_year,
            "recent_threshold": args.auto_recent_threshold,
        },
        "meta": {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "weights": weights,
            "limit": args.limit,
            "max_queries": args.max_queries,
        },
    }


def summarize_review_records(records: List[Dict]) -> Dict[str, int]:
    summary = {"auto_selected": 0, "selected": 0, "needs_review": 0, "none": 0, "skip": 0}
    for entry in records:
        choice = entry.get("choice") or {}
        status = str(choice.get("status") or "")
        if status in summary:
            summary[status] += 1
    return summary


def load_existing_output(path: Path) -> Tuple[List[Dict], Dict[str, Dict]]:
    if not path.exists():
        return [], {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("Output file must be a JSON array.")
    by_id = {entry.get("record_id", ""): entry for entry in data if isinstance(entry, dict)}
    return data, by_id


def save_output(path: Path, records: List[Dict]) -> None:
    path.write_text(json.dumps(records, indent=2), encoding="utf-8")


def prompt_yes_no(prompt: str, default: bool = False) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    raw = input(f"{prompt} {suffix} ").strip().lower()
    if not raw:
        return default
    return raw in {"y", "yes"}


def collect_selected_album_ids(records: List[Dict]) -> Tuple[List[Dict[str, str]], int]:
    selected: List[Dict[str, str]] = []
    seen: set[str] = set()
    unmatched = 0

    for entry in records:
        choice = entry.get("choice") or {}
        status = choice.get("status") or ""
        chosen = entry.get("chosen") or {}
        tidal_id = choice.get("tidal_id") or chosen.get("id") or ""
        album_title = (chosen.get("title") or (entry.get("album") or {}).get("title") or "").strip()

        if not tidal_id or status in {"none", "skip", ""}:
            unmatched += 1
            continue

        if tidal_id not in seen:
            seen.add(tidal_id)
            selected.append({"id": tidal_id, "title": album_title})

    return selected, unmatched


def default_playlist_name(truth_path: Path) -> str:
    return f"{truth_path.stem} (Import)"


def summarize_playlist(client: TidalClient, playlist_id: str) -> Dict:
    track_ids, album_ids = client.get_playlist_track_album_ids(playlist_id)
    album_counts = Counter(album_ids)
    top_albums = album_counts.most_common(5)
    top_details = []
    for album_id, count in top_albums:
        detail = client.get_album_details(album_id)
        title = detail.title if detail else album_id
        artists = ", ".join(detail.artists) if detail else ""
        top_details.append(
            {
                "album_id": album_id,
                "title": title,
                "artists": artists,
                "track_count": count,
            }
        )

    return {
        "track_count": len(track_ids),
        "album_count": len(album_counts),
        "top_albums": top_details,
    }


def select_existing_playlist(client: TidalClient, name: str) -> Tuple[Optional[str], bool]:
    playlists = client.list_playlists()
    matches = []
    for item in playlists:
        attr = item.get("attributes", {}) if isinstance(item, dict) else {}
        if attr.get("name") == name:
            matches.append(item)

    if not matches:
        return None, False

    print(f"\nExisting playlists named '{name}':")
    for idx, item in enumerate(matches, start=1):
        pid = item.get("id")
        stats = summarize_playlist(client, pid)
        print(f"  {idx}. {pid} | tracks {stats['track_count']} | albums {stats['album_count']}")
        for album in stats["top_albums"]:
            title = album["title"]
            artists = album["artists"]
            count = album["track_count"]
            detail = f"{title} ({count})"
            if artists:
                detail = f"{detail} | {artists}"
            print(f"     - {detail}")

    while True:
        raw = input("Use existing playlist number (0 to create new): ").strip()
        if not raw:
            continue
        if raw.isdigit():
            choice = int(raw)
            if choice == 0:
                return None, True
            if 1 <= choice <= len(matches):
                return matches[choice - 1].get("id"), True
        print("Invalid selection.")


def format_features(features: Dict[str, float]) -> str:
    parts = [f"{key}={features[key]:.2f}" for key in sorted(features.keys()) if features[key] > 0]
    return " ".join(parts) if parts else "(no signals)"


def format_candidate_line(candidate: Candidate, index: int) -> str:
    artists = ", ".join(candidate.artists) if candidate.artists else ""
    track_count = "-" if candidate.track_count is None else str(candidate.track_count)
    release = candidate.release_date or ""
    query_count = len(candidate.queries)
    return (
        f"{index:>2}. {candidate.score:.3f} | q={query_count:02d} | {candidate.title}"
        f" | {artists} | {release} | tracks {track_count}"
    )


def apply_details(candidate: Candidate, detail: AlbumDetail) -> None:
    candidate.title = detail.title or candidate.title
    candidate.artists = detail.artists or candidate.artists
    candidate.release_date = detail.release_date or candidate.release_date
    candidate.copyright = detail.copyright or candidate.copyright
    candidate.track_count = detail.track_count
    candidate.details_fetched = True


def ensure_details(client: TidalClient, ordered: List[Candidate], limit: int, sleep: float) -> None:
    for candidate in ordered[:limit]:
        if candidate.details_fetched:
            continue
        detail = client.get_album_details(candidate.id)
        if detail:
            apply_details(candidate, detail)
        if sleep:
            time.sleep(sleep)


def print_album_header(album: AlbumInput, index: int, total: int) -> None:
    print("\n" + "=" * 80)
    print(f"Album {index}/{total}")
    print(f"Title: {album.title}")
    if album.composers:
        print(f"Composers: {', '.join(album.composers)}")
    if album.performers:
        print(f"Performers: {', '.join(album.performers)}")
    if album.ensembles:
        print(f"Ensembles: {', '.join(album.ensembles)}")
    if album.conductor:
        print(f"Conductor: {album.conductor}")
    if album.instruments:
        print(f"Instruments: {', '.join(album.instruments)}")
    if album.label:
        print(f"Label: {album.label}")


def show_help() -> None:
    print("Commands:")
    print("  <number>        select candidate by index")
    print("  none            mark as no match")
    print("  skip            skip for later")
    print("  id <tidal_id>    select by TIDAL album id")
    print("  show <n>         show more candidates")
    print("  info <n>         show full details for candidate")
    print("  queries          show queries used")
    print("  help             show this help")
    print("  quit             save and exit")


def prompt_for_choice(
    album: AlbumInput,
    ordered: List[Candidate],
    queries: List[str],
    client: TidalClient,
    display_count: int,
    detail_sleep: float,
) -> Tuple[str, Optional[Candidate]]:
    show_n = min(display_count, len(ordered)) if ordered else 0

    while True:
        if ordered:
            ensure_details(client, ordered, show_n, detail_sleep)
            print("\nCandidates:")
            for idx, candidate in enumerate(ordered[:show_n], start=1):
                print(format_candidate_line(candidate, idx))
        else:
            print("\nCandidates: none found")

        raw = input("select> ").strip()
        if not raw:
            continue
        if raw in {"help", "h"}:
            show_help()
            continue
        if raw in {"quit", "q"}:
            return "quit", None
        if raw in {"skip", "s"}:
            return "skip", None
        if raw in {"none", "no", "n"}:
            return "none", None
        if raw == "queries":
            print("\nQueries:")
            for query in queries:
                print(f"  - {query}")
            continue
        if raw.startswith("show "):
            try:
                show_n = max(1, int(raw.split()[1]))
            except (ValueError, IndexError):
                print("Invalid show count.")
            continue
        if raw.startswith("info "):
            try:
                idx = int(raw.split()[1])
            except (ValueError, IndexError):
                print("Invalid info index.")
                continue
            if idx < 1 or idx > len(ordered):
                print("Index out of range.")
                continue
            candidate = ordered[idx - 1]
            if not candidate.details_fetched:
                detail = client.get_album_details(candidate.id)
                if detail:
                    apply_details(candidate, detail)
            print("\nCandidate details:")
            print(f"ID: {candidate.id}")
            print(f"Title: {candidate.title}")
            if candidate.artists:
                print(f"Artists: {', '.join(candidate.artists)}")
            if candidate.release_date:
                print(f"Release date: {candidate.release_date}")
            if candidate.track_count is not None:
                print(f"Tracks: {candidate.track_count}")
            if candidate.copyright:
                print(f"Copyright: {candidate.copyright}")
            print(f"Score: {candidate.score:.3f}")
            print(f"Features: {format_features(candidate.features)}")
            if candidate.queries:
                print("Queries:")
                for query in candidate.queries:
                    print(f"  - {query}")
            continue
        if raw.startswith("id "):
            parts = raw.split()
            if len(parts) < 2:
                print("Usage: id <tidal_id>")
                continue
            return "id", Candidate(
                id=parts[1],
                title="",
                artists=[],
                release_date="",
                copyright="",
                score=0.0,
                features={},
            )
        if raw.isdigit():
            idx = int(raw)
            if idx < 1 or idx > len(ordered):
                print("Index out of range.")
                continue
            return "select", ordered[idx - 1]
        print("Unrecognized command. Type 'help' for options.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Interactively label ground-truth TIDAL album matches.",
    )
    parser.add_argument("input_path", type=Path, help="Structured JSON input.")
    parser.add_argument("--token-file", type=Path, default=TOKEN_FILE_DIR / TOKEN_FILE_NAME)
    parser.add_argument(
        "--country-code",
        default=TIDAL_COUNTRY_CODE,
        help="TIDAL country code (use 'auto' to read from token)",
    )
    parser.add_argument("--limit", type=int, default=5, help="Results per query")
    parser.add_argument("--max-queries", type=int, default=0, help="0 means no limit")
    parser.add_argument("--sleep", type=float, default=0.2, help="Seconds between queries")
    parser.add_argument(
        "--detail-sleep", type=float, default=0.1, help="Seconds between detail fetches"
    )
    parser.add_argument("--top", type=int, default=8, help="Candidates to show")
    parser.add_argument("--weights", type=Path, help="Weights JSON for scoring")
    parser.add_argument("--output", type=Path, help="Output truth JSON")
    parser.add_argument("--resume", action="store_true", help="Skip already labeled entries")
    parser.add_argument("--start", type=int, default=1, help="1-based start index")
    parser.add_argument("--stop", type=int, default=0, help="1-based stop index")
    parser.add_argument("--print-queries", action="store_true", help="Print queries per album")
    parser.add_argument("--playlist-name", help="Override playlist name")
    parser.add_argument("--playlist-description", help="Override playlist description")
    parser.add_argument("--unlisted", action="store_true", help="Create playlist as private")
    parser.add_argument(
        "--output-mode",
        choices=["playlist", "favorite"],
        default="favorite",
        help="Output target: playlist or favorite (default: favorite)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply changes (default is dry-run)",
    )
    parser.add_argument(
        "--auto-threshold",
        type=float,
        default=0.7,
        help="Auto-select when top score >= threshold",
    )
    parser.add_argument(
        "--auto-recent-year",
        type=int,
        default=2025,
        help="Also auto-select when the top release year matches this year and score clears the recent threshold",
    )
    parser.add_argument(
        "--auto-recent-threshold",
        type=float,
        default=0.5,
        help="Secondary auto-select threshold when the top release year matches --auto-recent-year",
    )
    parser.add_argument(
        "--batch-review",
        action="store_true",
        help="Collect candidates for every album first and mark unresolved entries as needs_review without prompting",
    )
    parser.add_argument("--training-in", type=Path, help="Load training model JSON")
    parser.add_argument("--training-out", type=Path, help="Write training model JSON")
    parser.add_argument("--train-coverage", action="store_true", help="Run coverage training")
    parser.add_argument("--train-limit", type=int, default=80, help="Training set size")
    parser.add_argument("--train-max-calls", type=int, default=200, help="Max TIDAL calls")
    parser.add_argument("--train-iterations", type=int, default=10, help="Max iterations")
    parser.add_argument("--train-minutes", type=int, default=10, help="Max minutes")
    parser.add_argument("--seed", type=int, default=0, help="Random seed (0 = time-based)")
    parser.add_argument("--shuffle-count", type=int, default=2, help="Shuffle query variants")
    parser.add_argument("--truth", type=Path, help="Truth JSON for training")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.train_coverage and not args.batch_review and not sys.stdin.isatty():
        raise SystemExit("Interactive mode requires a TTY.")
    if not args.input_path.exists():
        raise SystemExit(f"Input JSON not found: {args.input_path}")

    seed = args.seed if args.seed else int(time.time())
    rng = random.Random(seed)

    template_weights = dict(DEFAULT_TEMPLATE_WEIGHTS)
    if args.training_in:
        weights, template_weights = load_training_model(args.training_in)
    else:
        weights = load_weights(args.weights)
    if args.weights and args.training_in:
        weights = load_weights(args.weights)

    if args.train_coverage:
        truth_path = args.truth or args.input_path.with_suffix(".truth.json")
        truth_records = load_truth_records(truth_path)
        token = get_valid_token(args.token_file)
        resolved_country = resolve_country_code(token, args.country_code)
        if (args.country_code or "").strip().lower() == "auto":
            print(f"Using token country code: {resolved_country}")
        client = TidalClient(token, resolved_country)
        weights, template_weights, model = train_coverage(
            client,
            truth_records,
            weights,
            template_weights,
            args,
            rng,
        )
        output_path = args.training_out or args.input_path.with_suffix(".training.json")
        model["meta"]["seed"] = seed
        save_training_model(output_path, model)
        print(f"Saved training model to {output_path}")
        return 0

    output_path = args.output or args.input_path.with_suffix(".truth.json")
    records, by_id = load_existing_output(output_path)

    albums = load_album_inputs(args.input_path)
    if not albums:
        raise SystemExit("No album entries found in input JSON.")

    max_queries = args.max_queries or None

    token = get_valid_token(args.token_file)
    resolved_country = resolve_country_code(token, args.country_code)
    if (args.country_code or "").strip().lower() == "auto":
        print(f"Using token country code: {resolved_country}")
    client = TidalClient(token, resolved_country)

    total = len(albums)
    start_idx = max(args.start, 1)
    stop_idx = args.stop if args.stop else total
    stop_idx = min(stop_idx, total)

    for idx, raw_album in enumerate(albums, start=1):
        if idx < start_idx or idx > stop_idx:
            continue

        record_id = build_record_id(raw_album)
        existing = by_id.get(record_id)
        if args.resume and existing:
            status = (existing.get("choice") or {}).get("status", "")
            if status in {"selected", "none", "auto_selected"}:
                continue

        album = raw_album

        if args.batch_review:
            print(f"\n[{idx}/{total}] {album.title}")
        else:
            print_album_header(album, idx, total)

        query_candidates = build_query_candidates(album, rng, shuffle_count=args.shuffle_count)
        selected_queries = select_query_candidates(
            query_candidates,
            template_weights,
            max_queries,
            rng,
        )
        if args.print_queries:
            for candidate in selected_queries:
                print(f"  [{candidate.template}] {candidate.query}")

        ordered = search_candidates_for_album(
            client=client,
            album=album,
            weights=weights,
            selected_queries=selected_queries,
            limit=args.limit,
            sleep_seconds=args.sleep,
        )
        candidates_map = {candidate.id: candidate for candidate in ordered}

        auto_selected, auto_reason = choose_auto_candidate(
            ordered=ordered,
            score_threshold=args.auto_threshold,
            recent_year=args.auto_recent_year,
            recent_threshold=args.auto_recent_threshold,
        )

        if auto_selected:
            ensure_details(client, ordered, 1, args.detail_sleep)
            action = "auto"
            selected = auto_selected
            print("\nAuto-selected:")
            print(f"  {format_candidate_line(selected, 1)}")
            print(f"  reason: {auto_reason}")
        elif args.batch_review:
            action = "needs_review"
            selected = None
            print(
                "Needs review:"
                f" top score={ordered[0].score:.3f}" if ordered else "Needs review: no candidates"
            )
        else:
            action, selected = prompt_for_choice(
                album,
                ordered,
                [candidate.query for candidate in selected_queries],
                client,
                args.top,
                args.detail_sleep,
            )

        if action == "quit":
            save_output(output_path, records)
            print(f"Saved progress to {output_path}")
            return 0

        choice: Dict[str, object] = {
            "status": "skip",
            "tidal_id": "",
            "selected_at": datetime.now().isoformat(timespec="seconds"),
            "manual": False,
        }

        chosen: Optional[Candidate] = None
        if action == "auto" and selected:
            choice["status"] = "auto_selected"
            choice["tidal_id"] = selected.id
            chosen = selected
        elif action == "needs_review":
            choice["status"] = "needs_review"
        elif action == "none":
            choice["status"] = "none"
        elif action == "select" and selected:
            choice["status"] = "selected"
            choice["tidal_id"] = selected.id
            chosen = selected
        elif action == "id" and selected:
            choice["status"] = "selected"
            choice["tidal_id"] = selected.id
            choice["manual"] = True
            detail = client.get_album_details(selected.id)
            if detail:
                hit = AlbumHit(
                    id=detail.id,
                    title=detail.title,
                    artists=detail.artists,
                    release_date=detail.release_date,
                    copyright=detail.copyright,
                )
                score, features = score_hit(album, hit, weights)
                selected = Candidate(
                    id=detail.id,
                    title=detail.title,
                    artists=detail.artists,
                    release_date=detail.release_date,
                    copyright=detail.copyright,
                    score=score,
                    features=features,
                    track_count=detail.track_count,
                    details_fetched=True,
                )
            else:
                selected = Candidate(
                    id=selected.id,
                    title="",
                    artists=[],
                    release_date="",
                    copyright="",
                    score=0.0,
                    features={},
                    details_fetched=False,
                )
            candidates_map[selected.id] = selected
            ordered = sorted(
                candidates_map.values(),
                key=lambda c: (c.score, len(c.queries), c.title.lower()),
                reverse=True,
            )
            chosen = selected

        record = build_record(
            album=album,
            input_path=args.input_path,
            record_id=record_id,
            ordered=ordered,
            selected_queries=selected_queries,
            chosen=chosen,
            choice=choice,
            weights=weights,
            args=args,
            auto_reason=auto_reason,
            mode="batch_review" if args.batch_review else "interactive",
        )

        if existing:
            for i, entry in enumerate(records):
                if entry.get("record_id") == record_id:
                    records[i] = record
                    break
        else:
            records.append(record)
        by_id[record_id] = record

        save_output(output_path, records)
        print(f"Saved {record_id} -> {output_path}")

    if args.batch_review:
        summary = summarize_review_records(records)
        print("\nBatch review summary:")
        for key in ["auto_selected", "needs_review", "selected", "none", "skip"]:
            print(f"  {key}: {summary.get(key, 0)}")
        print(f"Done. Output written to {output_path}")
        return 0

    selected_entries, unmatched = collect_selected_album_ids(records)
    if not selected_entries:
        print("No matched albums to act on.")
        print(f"Done. Output written to {output_path}")
        return 0

    output_mode = args.output_mode
    apply_changes = args.apply

    if output_mode == "favorite":
        if unmatched > 0:
            print(f"Warning: {unmatched} entries have no match.")

        album_ids = [entry["id"] for entry in selected_entries]
        unique_album_ids = list(dict.fromkeys(album_ids))

        if not apply_changes:
            print(f"Dry run: would favorite {len(unique_album_ids)} albums.")
            print("Use --apply to perform changes.")
            print(f"Done. Output written to {output_path}")
            return 0

        collection_id = client.get_user_collection_id()
        print(f"Favoriting {len(unique_album_ids)} albums in collection {collection_id}...")
        client.add_albums_to_collection(collection_id, unique_album_ids)
        print(f"Albums favorited: {len(unique_album_ids)}")
        print(f"Done. Output written to {output_path}")
        return 0

    if unmatched > 0:
        print(f"Warning: {unmatched} entries have no match.")
        if apply_changes and not prompt_yes_no("Proceed with playlist creation?"):
            print(f"Done. Output written to {output_path}")
            return 0
    else:
        if apply_changes and not prompt_yes_no("All entries matched. Create playlist now?"):
            print(f"Done. Output written to {output_path}")
            return 0

    base_name = args.playlist_name or default_playlist_name(output_path)
    playlist_name = base_name
    playlist_id: Optional[str] = None

    if apply_changes:
        playlist_id, had_existing = select_existing_playlist(client, base_name)
        if not playlist_id:
            if had_existing:
                default_new = f"{base_name} ({datetime.now().date()})"
                raw_name = input(f"New playlist name (blank for '{default_new}'): ").strip()
                playlist_name = raw_name or default_new
            if not prompt_yes_no(f"Create playlist '{playlist_name}'?", default=True):
                print("Playlist creation cancelled.")
                print(f"Done. Output written to {output_path}")
                return 0

            description = args.playlist_description or (
                f"Imported from {output_path.name} on {datetime.now().date()}."
            )
            playlist_id = client.create_playlist(
                playlist_name, description, is_public=not args.unlisted
            )
            print(f"Playlist created: {playlist_id}")
        else:
            print(f"Using existing playlist: {playlist_id}")

    all_tracks: List[str] = []
    seen_tracks: set[str] = set()
    shortfalls: List[str] = []

    for entry in selected_entries:
        album_id = entry["id"]
        detail = client.get_album_details(album_id)
        title = detail.title if detail and detail.title else entry.get("title", "")
        expected = detail.track_count if detail else None
        tracks = client.get_album_tracks(album_id, expected=expected)
        if expected and len(tracks) < expected:
            shortfalls.append(f"{title or album_id}: {len(tracks)}/{expected} tracks returned")
        for track_id in tracks:
            if track_id in seen_tracks:
                continue
            seen_tracks.add(track_id)
            all_tracks.append(track_id)

    if shortfalls:
        print("Warning: some albums returned fewer tracks than expected:")
        for entry in shortfalls:
            print(f"  - {entry}")
        if apply_changes and not prompt_yes_no("Proceed with playlist creation anyway?"):
            print("Playlist creation cancelled.")
            print(f"Done. Output written to {output_path}")
            return 0

    if not all_tracks:
        print("No tracks found to add.")
        print(f"Done. Output written to {output_path}")
        return 0

    if not apply_changes:
        print(f"Dry run: would create playlist '{playlist_name}' and add {len(all_tracks)} tracks.")
        print("Use --apply to perform changes.")
        print(f"Done. Output written to {output_path}")
        return 0

    if not playlist_id:
        raise RuntimeError("Playlist id is missing for apply mode.")

    print(f"Adding {len(all_tracks)} tracks from {len(selected_entries)} albums...")
    client.add_tracks_to_playlist(playlist_id, all_tracks)
    print(f"Tracks added: {len(all_tracks)}")

    print(f"Done. Output written to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Shared query generation, retrieval, and scoring helpers."""

from __future__ import annotations

import argparse
import json
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from tidal_pipeline.client import AlbumDetail, AlbumHit, SearchBackend
from tidal_pipeline.models import (
    AlbumInput,
    Candidate,
    DEFAULT_TEMPLATE_WEIGHTS,
    DEFAULT_WEIGHTS,
    GENERIC_TITLE_TOKENS,
    PERFORMER_WEIGHT_BOOST,
    QueryCandidate,
)
from tidal_pipeline.normalize import (
    artist_tokens_from_list,
    extract_numeric_tokens,
    extract_year,
    normalize,
    normalize_instruments,
    overlap_score,
    phrase_overlap_score,
    split_tokens,
    tokenize,
    tokens_from_list,
)


def score_candidate(
    album: AlbumInput,
    hit: AlbumHit,
    weights: Optional[Dict[str, float]] = None,
) -> Tuple[float, Dict[str, float]]:
    active_weights = weights or DEFAULT_WEIGHTS
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

    score = sum(features[key] * active_weights.get(key, 0.0) for key in features)
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
    template_weights: Optional[Dict[str, float]],
    max_queries: Optional[int],
    rng: random.Random,
) -> List[QueryCandidate]:
    active_template_weights = template_weights or DEFAULT_TEMPLATE_WEIGHTS
    if not max_queries or len(candidates) <= max_queries:
        return candidates

    ranked: List[Tuple[float, int, int, QueryCandidate]] = []
    for candidate in candidates:
        base = active_template_weights.get(candidate.template, 0.5)
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


def search_candidates_for_album(
    client: SearchBackend,
    album: AlbumInput,
    weights: Optional[Dict[str, float]],
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

            score, features = score_candidate(album, hit, weights)
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

    return sort_candidates(list(candidates_map.values()))


def sort_candidates(candidates: List[Candidate]) -> List[Candidate]:
    return sorted(
        candidates,
        key=lambda candidate: (candidate.score, len(candidate.queries), candidate.title.lower()),
        reverse=True,
    )


def apply_details(candidate: Candidate, detail: AlbumDetail) -> None:
    candidate.title = detail.title or candidate.title
    candidate.artists = detail.artists or candidate.artists
    candidate.release_date = detail.release_date or candidate.release_date
    candidate.copyright = detail.copyright or candidate.copyright
    candidate.track_count = detail.track_count
    candidate.details_fetched = True


def ensure_details(
    backend: SearchBackend,
    ordered: List[Candidate],
    limit: int,
    sleep: float,
) -> None:
    for candidate in ordered[:limit]:
        if candidate.details_fetched:
            continue
        detail = backend.get_album_details(candidate.id)
        if detail:
            apply_details(candidate, detail)
        if sleep:
            time.sleep(sleep)


def score_manual_candidate(
    backend: SearchBackend,
    album: AlbumInput,
    tidal_id: str,
    weights: Optional[Dict[str, float]],
) -> Candidate:
    detail = backend.get_album_details(tidal_id)
    if not detail:
        return Candidate(
            id=tidal_id,
            title="",
            artists=[],
            release_date="",
            copyright="",
            score=0.0,
            features={},
            details_fetched=False,
        )

    hit = AlbumHit(
        id=detail.id,
        title=detail.title,
        artists=detail.artists,
        release_date=detail.release_date,
        copyright=detail.copyright,
    )
    score, features = score_candidate(album, hit, weights)
    return Candidate(
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


def parse_list(value) -> List[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
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
                source_context=source.get("context", {})
                if isinstance(source.get("context"), dict)
                else {},
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


def save_truth_records(path: Path, records: List[Dict]) -> None:
    path.write_text(json.dumps(records, indent=2), encoding="utf-8")


def load_existing_output(path: Path) -> Tuple[List[Dict], Dict[str, Dict]]:
    if not path.exists():
        return [], {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("Output file must be a JSON array.")
    by_id = {entry.get("record_id", ""): entry for entry in data if isinstance(entry, dict)}
    return data, by_id


def update_scoring_weights(
    weights: Dict[str, float],
    feature_sums: Dict[str, float],
    count: int,
) -> Dict[str, float]:
    if count == 0:
        return weights
    updated = dict(weights)
    for key in updated:
        avg = feature_sums.get(key, 0.0) / count
        updated[key] = round((updated[key] * 0.7) + (avg * 0.3), 4)
    return updated


def update_template_weights(
    template_weights: Dict[str, float],
    stats: Dict[str, Dict[str, int]],
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


def build_record_id(album: AlbumInput) -> str:
    return "|".join([album.source_file or "", str(album.source_line or 0), album.title or ""])


def train_coverage(
    client: SearchBackend,
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
        uncovered = [
            (record_id, album, tidal_id)
            for record_id, album, tidal_id in targets
            if record_id not in covered
        ]
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
                            _, features = score_candidate(album, hit, weights)
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
        "uncovered": [record_id for record_id, _, _ in targets if record_id not in covered],
    }

    return weights, template_weights, model


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


def build_source_payload(album: AlbumInput) -> Dict:
    return {
        "file": album.source_file,
        "line": album.source_line,
        "raw": album.source_raw,
        "subsection": album.source_subsection,
        "context": album.source_context,
    }


def build_record(
    album: AlbumInput,
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
        "source": build_source_payload(album),
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

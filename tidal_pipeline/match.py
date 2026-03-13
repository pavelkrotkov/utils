"""Shared query generation, retrieval, and scoring helpers."""

from __future__ import annotations

import random
from typing import Dict, List, Optional, Tuple

from tidal_pipeline.client import AlbumHit, TidalClient
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
    client: TidalClient,
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
            import time

            time.sleep(sleep_seconds)

    return sorted(
        candidates_map.values(),
        key=lambda candidate: (candidate.score, len(candidate.queries), candidate.title.lower()),
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

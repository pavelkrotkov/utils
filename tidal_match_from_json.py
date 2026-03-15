#!/usr/bin/env python3
# /// script
# dependencies = [
#   "requests",
# ]
# ///
"""Interactive ground-truth labeling for TIDAL album matching."""

from __future__ import annotations

import argparse
import random
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from tidal_pipeline.client import (
    AlbumDetail,
    AlbumHit,
    TIDAL_COUNTRY_CODE,
    TOKEN_FILE_DIR,
    TOKEN_FILE_NAME,
    TidalClient,
    get_valid_token,
    resolve_country_code,
)
from tidal_pipeline.match import (
    build_record,
    build_record_id,
    build_query_candidates,
    choose_auto_candidate,
    collect_selected_album_ids,
    load_album_inputs,
    load_existing_output,
    load_training_model,
    load_truth_records,
    load_weights,
    save_training_model,
    save_truth_records,
    score_candidate,
    search_candidates_for_album,
    select_query_candidates,
    summarize_review_records,
    train_coverage,
)
from tidal_pipeline.models import (
    AlbumInput,
    Candidate,
    DEFAULT_TEMPLATE_WEIGHTS,
)


def prompt_yes_no(prompt: str, default: bool = False) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    raw = input(f"{prompt} {suffix} ").strip().lower()
    if not raw:
        return default
    return raw in {"y", "yes"}


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
                f"Needs review: top score={ordered[0].score:.3f}"
                if ordered
                else "Needs review: no candidates"
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
            save_truth_records(output_path, records)
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
                score, features = score_candidate(album, hit, weights)
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

        save_truth_records(output_path, records)
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

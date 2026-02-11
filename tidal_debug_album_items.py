#!/usr/bin/env python3
# /// script
# dependencies = [
#   "requests",
# ]
# ///
"""Debug TIDAL album item responses and pagination."""

from __future__ import annotations

import argparse
import base64
import json
import os
import stat
import time
import urllib.parse
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests


TIDAL_TOKEN_URL = "https://auth.tidal.com/v1/oauth2/token"
TIDAL_API_BASE = "https://openapi.tidal.com/v2"
TIDAL_API_V1_BASE = "https://api.tidal.com/v1"

TIDAL_CLIENT_ID = os.environ.get("TIDAL_CLIENT_ID")
TIDAL_CLIENT_SECRET = os.environ.get("TIDAL_CLIENT_SECRET")
TIDAL_COUNTRY_CODE = os.environ.get("TIDAL_COUNTRY_CODE", "auto")

TOKEN_FILE_DIR = Path.home() / ".config" / "tidal-utils"
TOKEN_FILE_NAME = "tokens.json"


def save_tokens(tokens: Dict, file_path: Path) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    if "expires_in" in tokens:
        tokens["expires_at"] = int(time.time()) + tokens["expires_in"]
    file_path.write_text(json.dumps(tokens), encoding="utf-8")
    os.chmod(file_path, stat.S_IRUSR | stat.S_IWUSR)


def load_tokens(file_path: Path) -> Optional[Dict]:
    if not file_path.exists():
        return None
    try:
        return json.loads(file_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def refresh_tokens(refresh_token: str) -> Dict:
    if not TIDAL_CLIENT_ID:
        raise RuntimeError("TIDAL_CLIENT_ID is required to refresh tokens.")
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": TIDAL_CLIENT_ID,
    }
    if TIDAL_CLIENT_SECRET:
        data["client_secret"] = TIDAL_CLIENT_SECRET
    resp = requests.post(TIDAL_TOKEN_URL, data=data, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"Token refresh failed: {resp.status_code} {resp.text}")
    return resp.json()


def get_access_token(token_file: Path) -> str:
    tokens = load_tokens(token_file)
    if not tokens:
        raise RuntimeError(
            f"No tokens found at {token_file}. Run tidal_match_from_json.py to login first."
        )
    expires_at = tokens.get("expires_at", 0)
    if time.time() < expires_at - 60:
        return tokens["access_token"]
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise RuntimeError("Access token expired and no refresh token available.")
    new_tokens = refresh_tokens(refresh_token)
    tokens.update(new_tokens)
    save_tokens(tokens, token_file)
    return tokens["access_token"]


def token_country_code(token: str) -> Optional[str]:
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + padding))
    except Exception:
        return None
    cc = data.get("cc")
    if isinstance(cc, str) and cc:
        return cc.upper()
    return None


def resolve_country_code(token: str, requested: Optional[str]) -> str:
    if requested:
        normalized = requested.strip().upper()
        if normalized and normalized != "AUTO":
            return normalized
    token_cc = token_country_code(token)
    if token_cc:
        return token_cc
    return "US"


def build_session(token: str) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.api+json",
            "Content-Type": "application/vnd.api+json",
        }
    )
    return session


def parse_link(link: str) -> Tuple[str, Dict[str, str]]:
    parsed = urllib.parse.urlparse(link)
    if parsed.scheme and parsed.netloc:
        path = parsed.path
        query = parsed.query
    else:
        if "?" in link:
            path, query = link.split("?", 1)
        else:
            path, query = link, ""

    params = {}
    if query:
        for key, values in urllib.parse.parse_qs(query).items():
            if values:
                params[key] = values[-1]
    if not path.startswith("/"):
        path = f"/{path}"
    return path, params


def request_json(
    session: requests.Session,
    path: str,
    params: Dict[str, str],
    retries: int = 2,
) -> Tuple[int, Dict]:
    url = f"{TIDAL_API_BASE}{path}"
    last_resp = None
    for attempt in range(retries + 1):
        resp = session.get(url, params=params, timeout=30)
        last_resp = resp
        if resp.status_code == 429:
            time.sleep(int(resp.headers.get("Retry-After", 1)) * (attempt + 1))
            continue
        if 500 <= resp.status_code < 600:
            time.sleep(1 * (attempt + 1))
            continue
        if resp.status_code >= 400:
            return resp.status_code, {"_error": resp.text}
        return resp.status_code, resp.json()
    if last_resp is None:
        return 0, {"_error": "no response"}
    return last_resp.status_code, {"_error": last_resp.text}


def request_json_v1(
    session: requests.Session,
    path: str,
    params: Dict[str, str],
    retries: int = 2,
) -> Tuple[int, Dict]:
    url = f"{TIDAL_API_V1_BASE}{path}"
    last_resp = None
    for attempt in range(retries + 1):
        resp = session.get(url, params=params, headers={"Accept": "application/json"}, timeout=30)
        last_resp = resp
        if resp.status_code == 429:
            time.sleep(int(resp.headers.get("Retry-After", 1)) * (attempt + 1))
            continue
        if 500 <= resp.status_code < 600:
            time.sleep(1 * (attempt + 1))
            continue
        if resp.status_code >= 400:
            return resp.status_code, {"_error": resp.text}
        try:
            return resp.status_code, resp.json()
        except Exception:
            return resp.status_code, {"_error": resp.text}
    if last_resp is None:
        return 0, {"_error": "no response"}
    return last_resp.status_code, {"_error": last_resp.text}


def extract_track_ids(doc: Dict) -> Tuple[List[str], Counter, Counter]:
    track_ids: List[str] = []
    data_types = Counter()
    included_types = Counter()

    data_items = doc.get("data") or []
    if isinstance(data_items, dict):
        data_items = [data_items]
    if not isinstance(data_items, list):
        data_items = []

    for item in data_items:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type:
            data_types[item_type] += 1
        if item_type == "tracks" and item.get("id"):
            track_ids.append(str(item["id"]))

    included_items = doc.get("included") or []
    if isinstance(included_items, dict):
        included_items = [included_items]
    if not isinstance(included_items, list):
        included_items = []

    for item in included_items:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type:
            included_types[item_type] += 1
        if item_type == "tracks" and item.get("id"):
            track_ids.append(str(item["id"]))

    return track_ids, data_types, included_types


def fetch_album_resource(
    session: requests.Session, album_id: str, country_code: str, save_dir: Optional[Path]
) -> Tuple[int, Dict, List[str], Counter]:
    status, doc = request_json(
        session,
        f"/albums/{album_id}",
        {"countryCode": country_code, "include": "items"},
    )
    if save_dir and isinstance(doc, dict) and status == 200:
        save_dir.mkdir(parents=True, exist_ok=True)
        out_path = save_dir / f"album-{album_id}-resource.json"
        out_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    track_ids, _, included_types = extract_track_ids(doc if isinstance(doc, dict) else {})
    return status, doc, track_ids, included_types


def fetch_user_country(session: requests.Session) -> Tuple[int, Optional[str]]:
    status, doc = request_json(session, "/users/me", {})
    if status != 200 or not isinstance(doc, dict):
        return status, None
    data = doc.get("data") or {}
    attrs = data.get("attributes") or {}
    return status, attrs.get("country")


def fetch_album_items(
    session: requests.Session,
    album_id: str,
    country_code: str,
    max_pages: int,
    sleep: float,
    save_dir: Optional[Path],
) -> Tuple[int, List[str], List[Dict]]:
    seen: set[str] = set()
    all_tracks: List[str] = []
    pages: List[Dict] = []

    base_path = f"/albums/{album_id}/relationships/items"
    next_link: Optional[str] = base_path
    page_idx = 0

    while next_link and page_idx < max_pages:
        page_idx += 1
        path, extra_params = parse_link(next_link)
        params = {"countryCode": country_code, "include": "items"}
        params.update({k: v for k, v in extra_params.items() if v is not None})

        status, doc = request_json(session, path, params)
        if status != 200:
            pages.append({"page": page_idx, "status": status, "error": doc.get("_error")})
            break

        track_ids, data_types, included_types = extract_track_ids(doc)
        added = 0
        for track_id in track_ids:
            if track_id not in seen:
                seen.add(track_id)
                all_tracks.append(track_id)
                added += 1

        pages.append(
            {
                "page": page_idx,
                "status": status,
                "data_count": len(doc.get("data") or []),
                "included_count": len(doc.get("included") or []),
                "added_tracks": added,
                "data_types": dict(data_types),
                "included_types": dict(included_types),
                "links": doc.get("links") or {},
            }
        )

        if save_dir:
            save_dir.mkdir(parents=True, exist_ok=True)
            out_path = save_dir / f"album-{album_id}-items-page{page_idx}.json"
            out_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")

        links = doc.get("links") or {}
        next_link = links.get("next")
        if not next_link:
            cursor = (links.get("meta") or {}).get("nextCursor")
            if cursor:
                cursor_q = urllib.parse.quote(str(cursor))
                next_link = f"{base_path}?page[cursor]={cursor_q}"
            else:
                next_link = None

        if sleep:
            time.sleep(sleep)

    return (200 if pages else 0), all_tracks, pages


def fetch_album_tracks_v1(
    session: requests.Session,
    album_id: str,
    country_code: str,
    max_pages: int,
    sleep: float,
) -> Tuple[int, List[str], Optional[int]]:
    tracks: List[str] = []
    seen: set[str] = set()
    offset = 0
    total = None
    limit = 100
    page = 0

    while page < max_pages:
        page += 1
        params = {
            "countryCode": country_code,
            "limit": str(limit),
            "offset": str(offset),
        }
        status, doc = request_json_v1(session, f"/albums/{album_id}/tracks", params)
        if status != 200:
            return status, tracks, total
        items = doc.get("items") if isinstance(doc, dict) else None
        if not isinstance(items, list):
            break
        for item in items:
            if not isinstance(item, dict):
                continue
            track_id = item.get("id")
            if track_id is None:
                continue
            track_id = str(track_id)
            if track_id not in seen:
                seen.add(track_id)
                tracks.append(track_id)
        if total is None and isinstance(doc, dict):
            total = doc.get("totalNumberOfItems")
        if not items:
            break
        offset += len(items)
        if total is not None and offset >= total:
            break
        if sleep:
            time.sleep(sleep)

    return 200, tracks, total


def summarize_album(
    album_id: str,
    title: str,
    expected: Optional[int],
    access_type: Optional[str],
    availability: Optional[List[str]],
    rel_tracks: List[str],
    rel_pages: List[Dict],
    res_tracks: List[str],
    res_types: Counter,
    v1_tracks: List[str],
    v1_total: Optional[int],
) -> Dict:
    return {
        "album_id": album_id,
        "title": title,
        "expected_tracks": expected,
        "access_type": access_type,
        "availability": availability,
        "relationship_tracks": len(rel_tracks),
        "album_include_tracks": len(res_tracks),
        "relationship_pages": len(rel_pages),
        "album_include_types": dict(res_types),
        "v1_tracks": len(v1_tracks),
        "v1_total": v1_total,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Debug TIDAL album items endpoints.")
    parser.add_argument("album_ids", nargs="*", help="Album IDs to debug")
    parser.add_argument("--album-ids-file", type=Path, help="File with album IDs, one per line")
    parser.add_argument(
        "--country-code",
        default=TIDAL_COUNTRY_CODE,
        help="TIDAL country code (use 'auto' to read from token)",
    )
    parser.add_argument("--max-pages", type=int, default=20, help="Max pages to fetch")
    parser.add_argument("--sleep", type=float, default=0.2, help="Sleep between pages")
    parser.add_argument("--save-dir", type=Path, help="Save raw JSON responses")
    parser.add_argument(
        "--token-file",
        type=Path,
        default=TOKEN_FILE_DIR / TOKEN_FILE_NAME,
        help="Token file path",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    album_ids = list(args.album_ids)
    if args.album_ids_file:
        album_ids.extend(
            [line.strip() for line in args.album_ids_file.read_text().splitlines() if line.strip()]
        )

    if not album_ids:
        print("No album IDs provided.")
        return 2

    token = get_access_token(args.token_file)
    resolved_country = resolve_country_code(token, args.country_code)
    if (args.country_code or "").strip().lower() == "auto":
        print(f"Using token country code: {resolved_country}")
    session = build_session(token)

    user_status, user_country = fetch_user_country(session)
    if user_status == 200:
        print(f"User country: {user_country}")
    else:
        print(f"User country: unavailable (status {user_status})")

    summaries = []
    for album_id in album_ids:
        status, album_doc, album_tracks, album_types = fetch_album_resource(
            session, album_id, resolved_country, args.save_dir
        )
        title = album_id
        expected = None
        access_type = None
        availability = None
        if status == 200 and isinstance(album_doc, dict):
            data = album_doc.get("data") or {}
            title = (data.get("attributes") or {}).get("title") or album_id
            attrs = data.get("attributes") or {}
            expected = attrs.get("numberOfItems")
            access_type = attrs.get("accessType")
            availability = attrs.get("availability")

        rel_status, rel_tracks, rel_pages = fetch_album_items(
            session,
            album_id,
            resolved_country,
            args.max_pages,
            args.sleep,
            args.save_dir,
        )
        v1_status, v1_tracks, v1_total = fetch_album_tracks_v1(
            session,
            album_id,
            resolved_country,
            args.max_pages,
            args.sleep,
        )

        summary = summarize_album(
            album_id,
            title,
            expected,
            access_type,
            availability,
            rel_tracks,
            rel_pages,
            album_tracks,
            album_types,
            v1_tracks,
            v1_total,
        )
        summaries.append(summary)

        print("\n" + "=" * 80)
        print(f"Album: {title} ({album_id})")
        print(f"Expected tracks: {expected}")
        print(f"Access type: {access_type} | Availability: {availability}")
        print(
            f"Album include items: {len(album_tracks)} tracks | included types: {dict(album_types)}"
        )
        print(
            f"Relationship items: {len(rel_tracks)} tracks | pages: {len(rel_pages)} | status: {rel_status}"
        )
        print(f"V1 album tracks: {len(v1_tracks)} tracks | total: {v1_total} | status: {v1_status}")
        for page in rel_pages:
            if "error" in page:
                print(f"  page {page['page']}: status {page['status']} error {page['error']}")
            else:
                print(
                    "  page {page}: data={data_count} included={included_count} "
                    "added={added_tracks} data_types={data_types} included_types={included_types}".format(
                        **page
                    )
                )

    if args.save_dir:
        summary_path = args.save_dir / "summary.json"
        summary_path.write_text(json.dumps(summaries, indent=2), encoding="utf-8")
        print(f"\nSaved summary to {summary_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

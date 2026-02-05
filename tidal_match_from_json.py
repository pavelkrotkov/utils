#!/usr/bin/env python3
# /// script
# dependencies = [
#   "requests",
# ]
# ///
"""Interactive ground-truth labeling for TIDAL album matching."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import random
import re
import secrets
import stat
import sys
import time
import unicodedata
import urllib.parse
import webbrowser
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import requests


TIDAL_AUTH_URL = "https://login.tidal.com/authorize"
TIDAL_TOKEN_URL = "https://auth.tidal.com/v1/oauth2/token"
TIDAL_API_BASE = "https://openapi.tidal.com/v2"

TIDAL_CLIENT_ID = os.environ.get("TIDAL_CLIENT_ID")
TIDAL_CLIENT_SECRET = os.environ.get("TIDAL_CLIENT_SECRET")
TIDAL_REDIRECT_URI = os.environ.get("TIDAL_REDIRECT_URI", "http://127.0.0.1:8765/callback")
TIDAL_SCOPES = os.environ.get(
    "TIDAL_SCOPES", "playlists.read playlists.write collection.read collection.write search.read"
)
TIDAL_COUNTRY_CODE = os.environ.get("TIDAL_COUNTRY_CODE", "GB")

TOKEN_FILE_DIR = Path.home() / ".config" / "tidal-utils"
TOKEN_FILE_NAME = "tokens.json"

DEFAULT_WEIGHTS = {
    "title": 0.45,
    "composer": 0.2,
    "performer": 0.2,
    "ensemble": 0.1,
    "conductor": 0.05,
    "instrument": 0.05,
    "label": 0.05,
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
    "composer_title": 1.2,
    "composer_work": 1.2,
    "performer_title": 2.0,
    "performer_instrument": 2.2,
    "performer_ensemble": 2.0,
    "performer_composer": 1.8,
    "performer_work": 1.6,
    "ensemble_title": 1.2,
    "conductor_title": 1.1,
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

INSTRUMENT_MAP = {
    "pf": "piano",
    "pno": "piano",
    "piano": "piano",
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
    "gtr": "guitar",
    "guitar": "guitar",
    "perc": "percussion",
    "percussion": "percussion",
    "sop": "soprano",
    "soprano": "soprano",
    "mez": "mezzo",
    "mezzo": "mezzo",
    "ten": "tenor",
    "tenor": "tenor",
    "bar": "baritone",
    "baritone": "baritone",
    "cond": "conductor",
    "conductor": "conductor",
}


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


@dataclass
class AlbumHit:
    id: str
    title: str
    artists: List[str]
    release_date: str
    copyright: str


@dataclass
class AlbumDetail:
    id: str
    title: str
    artists: List[str]
    release_date: str
    copyright: str
    track_count: Optional[int] = None


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


class OAuthHandler:
    def __init__(self) -> None:
        self.code_verifier = self._generate_code_verifier()
        self.code_challenge = self._generate_code_challenge(self.code_verifier)
        self.auth_code: Optional[str] = None
        self.state = secrets.token_urlsafe(16)

    def _generate_code_verifier(self) -> str:
        token = secrets.token_urlsafe(32)
        return token.rstrip("=")

    def _generate_code_challenge(self, verifier: str) -> str:
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    def get_auth_url(self) -> str:
        params = {
            "response_type": "code",
            "client_id": TIDAL_CLIENT_ID,
            "redirect_uri": TIDAL_REDIRECT_URI,
            "scope": TIDAL_SCOPES,
            "state": self.state,
            "code_challenge": self.code_challenge,
            "code_challenge_method": "S256",
            "lang": "en",
        }
        return f"{TIDAL_AUTH_URL}?{urllib.parse.urlencode(params)}"

    def wait_for_callback(self) -> str:
        parsed = urllib.parse.urlparse(TIDAL_REDIRECT_URI)
        port = parsed.port or 80

        handler_ref = self

        class CallbackHandler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                query = urllib.parse.urlparse(self.path).query
                params = urllib.parse.parse_qs(query)

                if "code" in params:
                    if params.get("state", [""])[0] == handler_ref.state:
                        handler_ref.auth_code = params["code"][0]
                        self.send_response(200)
                        self.send_header("Content-type", "text/html")
                        self.end_headers()
                        self.wfile.write(
                            b"<html><body><h1>Authentication Successful</h1><p>You can close this tab.</p></body></html>"
                        )
                    else:
                        self.send_response(400)
                        self.wfile.write(b"Invalid state.")
                else:
                    self.send_response(400)
                    self.wfile.write(b"No code returned.")

        server = HTTPServer(("127.0.0.1", port), CallbackHandler)
        server.handle_request()
        server.server_close()

        if not self.auth_code:
            raise RuntimeError("Failed to capture authorization code.")
        return self.auth_code

    def exchange_code(self, code: str) -> Dict:
        data = {
            "grant_type": "authorization_code",
            "client_id": TIDAL_CLIENT_ID,
            "code": code,
            "redirect_uri": TIDAL_REDIRECT_URI,
            "code_verifier": self.code_verifier,
        }
        if TIDAL_CLIENT_SECRET:
            data["client_secret"] = TIDAL_CLIENT_SECRET

        resp = requests.post(TIDAL_TOKEN_URL, data=data)
        if resp.status_code != 200:
            raise RuntimeError(f"Token exchange failed: {resp.text}")
        return resp.json()

    def refresh_tokens(self, refresh_token: str) -> Dict:
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": TIDAL_CLIENT_ID,
        }
        if TIDAL_CLIENT_SECRET:
            data["client_secret"] = TIDAL_CLIENT_SECRET

        resp = requests.post(TIDAL_TOKEN_URL, data=data)
        if resp.status_code != 200:
            raise RuntimeError(f"Token refresh failed: {resp.text}")
        return resp.json()


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


def get_valid_token(token_file: Path) -> str:
    tokens = load_tokens(token_file)
    handler = OAuthHandler()

    if tokens:
        expires_at = tokens.get("expires_at", 0)
        if time.time() < expires_at - 60:
            return tokens["access_token"]

        try:
            new_tokens = handler.refresh_tokens(tokens["refresh_token"])
            tokens.update(new_tokens)
            save_tokens(tokens, token_file)
            return tokens["access_token"]
        except Exception:
            tokens = None

    if not TIDAL_CLIENT_ID:
        raise RuntimeError("TIDAL_CLIENT_ID environment variable is not set.")

    print("Please log in to Tidal.")
    auth_url = handler.get_auth_url()
    print(f"Opening browser: {auth_url}")
    webbrowser.open(auth_url)

    print("Waiting for callback on localhost...")
    code = handler.wait_for_callback()

    tokens = handler.exchange_code(code)
    save_tokens(tokens, token_file)
    print("Authentication successful.")
    return tokens["access_token"]


class TidalClient:
    def __init__(self, token: str, country_code: str) -> None:
        self.token = token
        self.country_code = country_code
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.api+json",
                "Content-Type": "application/vnd.api+json",
            }
        )

    def _req(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, str]] = None,
        json_data: Optional[Dict] = None,
        retry: int = 2,
    ) -> requests.Response:
        url = f"{TIDAL_API_BASE}{path}"
        p = params or {}
        p["countryCode"] = self.country_code

        resp: Optional[requests.Response] = None
        for attempt in range(retry + 1):
            resp = self.session.request(method, url, params=p, json=json_data)

            if resp.status_code == 429:
                sleep_time = int(resp.headers.get("Retry-After", 1)) * (attempt + 1)
                time.sleep(sleep_time)
                continue

            if 500 <= resp.status_code < 600:
                time.sleep(1 * (attempt + 1))
                continue

            if resp.status_code >= 400:
                if resp.status_code == 404:
                    return resp
                resp.raise_for_status()

            return resp

        if resp is None:
            raise RuntimeError("Failed to reach TIDAL API.")
        return resp

    def search_albums(self, query: str, limit: int = 5) -> List[AlbumHit]:
        encoded_query = urllib.parse.quote(query)
        params = {"page[limit]": limit, "include": "albums.artists"}
        try:
            resp = self._req("GET", f"/searchResults/{encoded_query}", params=params)
            if resp.status_code != 200:
                return []
            data = resp.json()
        except Exception:
            return []

        search_node = data.get("data", {})
        if not search_node or search_node.get("type") != "searchResults":
            return []

        album_rels = search_node.get("relationships", {}).get("albums", {}).get("data", [])
        included = data.get("included", [])
        albums_lookup = {(x["type"], x["id"]): x for x in included if x.get("type") == "albums"}
        artists_lookup = {(x["type"], x["id"]): x for x in included if x.get("type") == "artists"}

        hits: List[AlbumHit] = []
        for rel in album_rels:
            if rel.get("type") != "albums":
                continue

            album_obj = albums_lookup.get(("albums", rel["id"]))
            if not album_obj:
                continue

            attr = album_obj.get("attributes", {})
            cright = attr.get("copyright", "")
            if isinstance(cright, dict):
                cright = str(cright)

            hit = AlbumHit(
                id=album_obj["id"],
                title=attr.get("title", ""),
                release_date=attr.get("releaseDate", ""),
                artists=[],
                copyright=str(cright),
            )

            alb_art_rels = album_obj.get("relationships", {}).get("artists", {}).get("data", [])
            for art_rel in alb_art_rels:
                art_obj = artists_lookup.get(("artists", art_rel["id"]))
                if art_obj:
                    hit.artists.append(art_obj.get("attributes", {}).get("name", ""))

            hits.append(hit)

        return hits

    def get_album_details(self, album_id: str) -> Optional[AlbumDetail]:
        try:
            params = {"include": "artists"}
            resp = self._req("GET", f"/albums/{album_id}", params=params)
            if resp.status_code != 200:
                return None
            data = resp.json()
        except Exception:
            return None

        item = data.get("data")
        if not item or item.get("type") != "albums":
            return None

        attr = item.get("attributes", {})
        cright = attr.get("copyright", "")
        if isinstance(cright, dict):
            cright = str(cright)

        artists: List[str] = []
        included = data.get("included", [])
        artists_lookup = {(x["type"], x["id"]): x for x in included if x.get("type") == "artists"}
        rel_artists = item.get("relationships", {}).get("artists", {}).get("data", [])
        for art_rel in rel_artists:
            art_obj = artists_lookup.get(("artists", art_rel["id"]))
            if art_obj:
                artists.append(art_obj.get("attributes", {}).get("name", ""))

        return AlbumDetail(
            id=item.get("id", album_id),
            title=attr.get("title", ""),
            release_date=attr.get("releaseDate", ""),
            artists=artists,
            copyright=str(cright),
            track_count=attr.get("numberOfItems"),
        )

    def get_album_tracks(self, album_id: str) -> List[str]:
        tracks: List[str] = []
        seen: set[str] = set()
        base_path = f"/albums/{album_id}/relationships/items"
        next_link = base_path

        while next_link:
            path = next_link.replace(TIDAL_API_BASE, "")
            if not path.startswith("/"):
                path = f"/{path}"
            resp = self._req("GET", path, params={"include": "items"})
            if resp.status_code != 200:
                break
            data = resp.json()

            items = data.get("data") or []
            for item in items:
                if item.get("type") == "tracks" and item.get("id"):
                    track_id = str(item["id"])
                    if track_id not in seen:
                        tracks.append(track_id)
                        seen.add(track_id)

            included = data.get("included") or []
            for item in included:
                if item.get("type") == "tracks" and item.get("id"):
                    track_id = str(item["id"])
                    if track_id not in seen:
                        tracks.append(track_id)
                        seen.add(track_id)

            links = data.get("links", {})
            next_link = links.get("next")
            if not next_link:
                cursor = links.get("meta", {}).get("nextCursor")
                if cursor:
                    next_link = f"{base_path}?page[cursor]={urllib.parse.quote(str(cursor))}"

        return tracks

    def add_tracks_to_playlist(self, playlist_id: str, track_ids: List[str]) -> None:
        chunk_size = 20
        for i in range(0, len(track_ids), chunk_size):
            chunk = track_ids[i : i + chunk_size]
            body = {"data": [{"type": "tracks", "id": tid} for tid in chunk]}
            self._req("POST", f"/playlists/{playlist_id}/relationships/items", json_data=body)

    def create_playlist(self, name: str, description: str, is_public: bool) -> str:
        body = {
            "data": {
                "type": "playlists",
                "attributes": {"name": name, "description": description, "public": is_public},
            }
        }

        for path in ["/my/playlists", "/playlists"]:
            try:
                resp = self._req("POST", path, json_data=body)
                if resp.status_code == 201:
                    loc = resp.headers.get("Location", "")
                    if loc:
                        return loc.split("/")[-1]
                    if resp.content:
                        return resp.json()["data"]["id"]
            except Exception:
                continue

        raise RuntimeError("Failed to create playlist.")

    def list_playlists(self, limit: int = 100) -> List[Dict]:
        playlists: List[Dict] = []
        next_link = f"/my/playlists?page[limit]={limit}"

        while next_link:
            path = next_link.replace(TIDAL_API_BASE, "")
            resp = self._req("GET", path)
            if resp.status_code != 200:
                break
            data = resp.json()
            items = data.get("data", [])
            if isinstance(items, list):
                playlists.extend(items)
            links = data.get("links", {})
            next_link = links.get("next")

        return playlists

    def _fetch_track_album_map(self, track_ids: List[str]) -> Dict[str, List[str]]:
        album_map: Dict[str, List[str]] = {}
        chunk_size = 50
        unique_ids = list(dict.fromkeys(track_ids))

        for i in range(0, len(unique_ids), chunk_size):
            chunk = unique_ids[i : i + chunk_size]
            params = {"filter[id]": chunk, "include": "albums"}
            resp = self._req("GET", "/tracks", params=params)
            if resp.status_code != 200:
                continue
            data = resp.json()
            for item in data.get("data", []):
                track_id = item.get("id")
                rel = item.get("relationships", {}).get("albums", {}).get("data", [])
                if isinstance(rel, dict):
                    rel = [rel]
                album_ids: List[str] = []
                for entry in rel:
                    if not isinstance(entry, dict):
                        continue
                    album_id = entry.get("id")
                    if album_id:
                        album_ids.append(str(album_id))
                if track_id:
                    album_map[str(track_id)] = album_ids

        return album_map

    def get_playlist_track_album_ids(self, playlist_id: str) -> Tuple[List[str], List[str]]:
        track_ids: List[str] = []
        album_ids: List[str] = []
        next_link = f"/playlists/{playlist_id}/relationships/items"

        while next_link:
            path = next_link.replace(TIDAL_API_BASE, "")
            resp = self._req("GET", path)
            if resp.status_code != 200:
                break
            data = resp.json()

            items = data.get("data", [])
            for item in items:
                if item.get("type") != "tracks":
                    continue
                track_id = item.get("id")
                if track_id:
                    track_ids.append(track_id)

            links = data.get("links", {})
            next_link = links.get("next")
        if track_ids:
            album_map = self._fetch_track_album_map(track_ids)
            for track_id in track_ids:
                rel_album_ids = album_map.get(track_id, [])
                if rel_album_ids:
                    album_ids.append(rel_album_ids[0])

        return track_ids, album_ids


def normalize(text: str) -> str:
    if not text:
        return ""
    text = text.replace(".", " ")
    text = unicodedata.normalize("NFKD", text).encode("ASCII", "ignore").decode("ASCII")
    text = text.lower()
    text = re.sub(r"[/\-&—–]", " ", text)
    text = re.sub(r"[^a-z0-9\s]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def tokenize(text: str) -> set[str]:
    norm = normalize(text)
    return {t for t in norm.split() if len(t) > 2 and t not in STOPWORDS}


def split_tokens(text: str) -> List[str]:
    norm = normalize(text)
    tokens = [t for t in norm.split() if t and t not in STOPWORDS]
    return tokens


def tokens_from_list(values: Iterable[str]) -> set[str]:
    tokens: set[str] = set()
    for value in values:
        tokens.update(tokenize(value))
    return tokens


def normalize_instruments(values: Iterable[str]) -> set[str]:
    tokens: set[str] = set()
    for value in values:
        for raw in normalize(value).split():
            tokens.add(INSTRUMENT_MAP.get(raw, raw))
    return {t for t in tokens if t}


def overlap_score(left: set[str], right: set[str]) -> float:
    if not left:
        return 0.0
    return len(left & right) / len(left)


def extract_year(text: str) -> Optional[str]:
    match = re.search(r"\b(19\d{2}|20\d{2})\b", text or "")
    return match.group(1) if match else None


def score_hit(
    album: AlbumInput, hit: AlbumHit, weights: Dict[str, float]
) -> Tuple[float, Dict[str, float]]:
    title_tokens = tokens_from_list([album.title] + album.works)
    composer_tokens = tokens_from_list(album.composers)
    performer_tokens = tokens_from_list(album.performers)
    ensemble_tokens = tokens_from_list(album.ensembles)
    conductor_tokens = tokenize(album.conductor)
    instrument_tokens = normalize_instruments(album.instruments)

    hit_title_tokens = tokenize(hit.title)
    hit_artist_tokens = tokens_from_list(hit.artists)
    hit_all_tokens = hit_title_tokens | hit_artist_tokens

    features = {
        "title": overlap_score(title_tokens, hit_title_tokens),
        "composer": overlap_score(composer_tokens, hit_all_tokens),
        "performer": overlap_score(performer_tokens, hit_all_tokens),
        "ensemble": overlap_score(ensemble_tokens, hit_all_tokens),
        "conductor": overlap_score(conductor_tokens, hit_all_tokens),
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

    score = sum(features[key] * weights.get(key, 0.0) for key in features)
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
        short_title = " ".join(split_tokens(album.title)[:4]) if album.title else ""
        if short_title:
            add_query("composer_title", f"{composer} {short_title}")
            add_query("composer_title", f"{short_title} {composer}")
        for work in album.works:
            work_short = " ".join(split_tokens(work)[:4]) if work else ""
            if work_short:
                add_query("composer_work", f"{composer} {work_short}")
                add_query("composer_work", f"{work_short} {composer}")

    for performer in album.performers:
        short_title = " ".join(split_tokens(album.title)[:4]) if album.title else ""
        if short_title:
            add_query("performer_title", f"{short_title} {performer}")
        for instrument in album.instruments:
            add_query("performer_instrument", f"{performer} {instrument}")
        for ensemble in album.ensembles:
            add_query("performer_ensemble", f"{performer} {ensemble}")
        for composer in album.composers:
            add_query("performer_composer", f"{performer} {composer}")
        for work in album.works:
            add_query("performer_work", f"{performer} {work}")

    for ensemble in album.ensembles:
        short_title = " ".join(split_tokens(album.title)[:4]) if album.title else ""
        if short_title:
            add_query("ensemble_title", f"{short_title} {ensemble}")

    if album.conductor:
        short_title = " ".join(split_tokens(album.title)[:4]) if album.title else ""
        if short_title:
            add_query("conductor_title", f"{short_title} {album.conductor}")
        for composer in album.composers:
            add_query("conductor_composer", f"{composer} {album.conductor}")

    for instrument in album.instruments:
        short_title = " ".join(split_tokens(album.title)[:4]) if album.title else ""
        if short_title:
            add_query("instrument_title", f"{short_title} {instrument}")

    if album.label and album.title:
        add_query("label_title", f"{album.title} {album.label}")

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


def select_query_candidates(
    candidates: List[QueryCandidate],
    template_weights: Dict[str, float],
    max_queries: Optional[int],
    rng: random.Random,
) -> List[QueryCandidate]:
    if not max_queries or len(candidates) <= max_queries:
        return candidates

    weights: List[float] = []
    for candidate in candidates:
        base = template_weights.get(candidate.template, 0.5)
        if candidate.template.startswith("performer"):
            base *= PERFORMER_WEIGHT_BOOST
        weights.append(base)

    return weighted_sample(candidates, weights, max_queries, rng)


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


def build_record_id(album: AlbumInput) -> str:
    return "|".join(
        [
            album.source_file or "",
            str(album.source_line or 0),
            album.title or "",
        ]
    )


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
    parser.add_argument("--country-code", default=TIDAL_COUNTRY_CODE, help="TIDAL country code")
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
        "--auto-threshold",
        type=float,
        default=0.7,
        help="Auto-select when top score >= threshold",
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
    if not args.train_coverage and not sys.stdin.isatty():
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
        client = TidalClient(token, args.country_code)
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
    client = TidalClient(token, args.country_code)

    total = len(albums)
    start_idx = max(args.start, 1)
    stop_idx = args.stop if args.stop else total
    stop_idx = min(stop_idx, total)

    for idx, album in enumerate(albums, start=1):
        if idx < start_idx or idx > stop_idx:
            continue

        record_id = build_record_id(album)
        existing = by_id.get(record_id)
        if args.resume and existing:
            status = (existing.get("choice") or {}).get("status", "")
            if status in {"selected", "none", "auto_selected"}:
                continue

        print_album_header(album, idx, total)

        query_candidates = build_query_candidates(album, rng, shuffle_count=args.shuffle_count)
        selected_queries = select_query_candidates(
            query_candidates,
            template_weights,
            max_queries,
            rng,
        )
        queries = [candidate.query for candidate in selected_queries]
        if args.print_queries:
            for candidate in selected_queries:
                print(f"  [{candidate.template}] {candidate.query}")

        candidates_map: Dict[str, Candidate] = {}
        for query in queries:
            hits = client.search_albums(query, limit=args.limit)
            for hit in hits:
                if hit.id in candidates_map:
                    if query not in candidates_map[hit.id].queries:
                        candidates_map[hit.id].queries.append(query)
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
                    queries=[query],
                )

            if args.sleep:
                time.sleep(args.sleep)

        ordered = sorted(
            candidates_map.values(),
            key=lambda c: (c.score, len(c.queries), c.title.lower()),
            reverse=True,
        )

        if ordered and ordered[0].score >= args.auto_threshold:
            ensure_details(client, ordered, 1, args.detail_sleep)
            action = "auto"
            selected = ordered[0]
            print("\nAuto-selected:")
            print(f"  {format_candidate_line(selected, 1)}")
        else:
            action, selected = prompt_for_choice(
                album,
                ordered,
                queries,
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

        record = {
            "record_id": record_id,
            "source": {
                "file": album.source_file,
                "line": album.source_line,
                "raw": album.source_raw,
            },
            "album": album_to_dict(album),
            "queries": queries,
            "candidates": [candidate_to_dict(candidate) for candidate in ordered],
            "choice": choice,
            "chosen": candidate_to_dict(chosen) if chosen else None,
            "meta": {
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "weights": weights,
                "limit": args.limit,
                "max_queries": args.max_queries,
            },
        }

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

    selected_entries, unmatched = collect_selected_album_ids(records)
    if not selected_entries:
        print("No matched albums to create a playlist.")
        print(f"Done. Output written to {output_path}")
        return 0

    if unmatched > 0:
        print(f"Warning: {unmatched} entries have no match.")
        if not prompt_yes_no("Proceed with playlist creation?"):
            print(f"Done. Output written to {output_path}")
            return 0
    else:
        if not prompt_yes_no("All entries matched. Create playlist now?"):
            print(f"Done. Output written to {output_path}")
            return 0

    base_name = args.playlist_name or default_playlist_name(output_path)
    playlist_id, had_existing = select_existing_playlist(client, base_name)
    playlist_name = base_name
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
        tracks = client.get_album_tracks(album_id)
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
        if not prompt_yes_no("Proceed with playlist creation anyway?"):
            print("Playlist creation cancelled.")
            print(f"Done. Output written to {output_path}")
            return 0

    if not all_tracks:
        print("No tracks found to add.")
        print(f"Done. Output written to {output_path}")
        return 0

    print(f"Adding {len(all_tracks)} tracks from {len(selected_entries)} albums...")
    client.add_tracks_to_playlist(playlist_id, all_tracks)
    print(f"Tracks added: {len(all_tracks)}")

    print(f"Done. Output written to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

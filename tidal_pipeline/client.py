"""TIDAL auth, token management, and API access."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import stat
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Protocol, Tuple, runtime_checkable

import requests


TIDAL_AUTH_URL = "https://login.tidal.com/authorize"
TIDAL_TOKEN_URL = "https://auth.tidal.com/v1/oauth2/token"
TIDAL_API_BASE = "https://openapi.tidal.com/v2"
TIDAL_API_V1_BASE = "https://api.tidal.com/v1"
TIDAL_WEB_V1_BASE = "https://tidal.com/v1"

TIDAL_CLIENT_ID = os.environ.get("TIDAL_CLIENT_ID")
TIDAL_CLIENT_SECRET = os.environ.get("TIDAL_CLIENT_SECRET")
TIDAL_REDIRECT_URI = os.environ.get("TIDAL_REDIRECT_URI", "http://127.0.0.1:8765/callback")
TIDAL_SCOPES = os.environ.get(
    "TIDAL_SCOPES",
    "playlists.read playlists.write collection.read collection.write search.read user.read",
)
TIDAL_COUNTRY_CODE = os.environ.get("TIDAL_COUNTRY_CODE", "auto")
TIDAL_WEB_TOKEN = os.environ.get("TIDAL_WEB_TOKEN")
TIDAL_WEB_COOKIES = os.environ.get("TIDAL_WEB_COOKIES")
TIDAL_WEB_LOCALE = os.environ.get("TIDAL_WEB_LOCALE", "en_US")
TIDAL_WEB_DEVICE_TYPE = os.environ.get("TIDAL_WEB_DEVICE_TYPE", "BROWSER")
TIDAL_WEB_USER_AGENT = os.environ.get("TIDAL_WEB_USER_AGENT")
REQUEST_TIMEOUT_SECONDS = float(os.environ.get("TIDAL_REQUEST_TIMEOUT_SECONDS", "20"))

TOKEN_FILE_DIR = Path.home() / ".config" / "tidal-utils"
TOKEN_FILE_NAME = "tokens.json"


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


@runtime_checkable
class SearchBackend(Protocol):
    def search_albums(self, query: str, limit: int = 5) -> List[AlbumHit]: ...

    def get_album_details(self, album_id: str) -> Optional[AlbumDetail]: ...


class CachedSearchBackend:
    """Offline SearchBackend backed by cached truth-record candidates."""

    def __init__(self, truth_records: Iterable[Dict]) -> None:
        self._hits_by_query: Dict[str, List[AlbumHit]] = {}
        self._details_by_id: Dict[str, AlbumDetail] = {}
        self._seen_by_query: Dict[str, set[str]] = {}

        for record in truth_records:
            if not isinstance(record, dict):
                continue
            for raw_candidate in record.get("candidates") or []:
                if not isinstance(raw_candidate, dict):
                    continue
                hit = self._candidate_to_album_hit(raw_candidate)
                self._details_by_id.setdefault(
                    hit.id,
                    AlbumDetail(
                        id=hit.id,
                        title=hit.title,
                        artists=hit.artists,
                        release_date=hit.release_date,
                        copyright=hit.copyright,
                        track_count=raw_candidate.get("track_count"),
                    ),
                )
                for query in raw_candidate.get("queries") or []:
                    self._add_query_hit(str(query), hit)

    def _add_query_hit(self, query: str, hit: AlbumHit) -> None:
        if not query:
            return
        seen = self._seen_by_query.setdefault(query, set())
        if hit.id in seen:
            return
        seen.add(hit.id)
        self._hits_by_query.setdefault(query, []).append(hit)

    def _candidate_to_album_hit(self, candidate: Dict) -> AlbumHit:
        artists = candidate.get("artists") or []
        if isinstance(artists, str):
            artists = [artists]
        return AlbumHit(
            id=str(candidate.get("id", "")),
            title=str(candidate.get("title", "")),
            artists=[str(artist) for artist in artists],
            release_date=str(candidate.get("release_date", "")),
            copyright=str(candidate.get("copyright", "")),
        )

    def search_albums(self, query: str, limit: int = 5) -> List[AlbumHit]:
        return list(self._hits_by_query.get(query, []))[:limit]

    def get_album_details(self, album_id: str) -> Optional[AlbumDetail]:
        return self._details_by_id.get(str(album_id))


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

        resp = requests.post(TIDAL_TOKEN_URL, data=data, timeout=REQUEST_TIMEOUT_SECONDS)
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

        resp = requests.post(TIDAL_TOKEN_URL, data=data, timeout=REQUEST_TIMEOUT_SECONDS)
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


class TidalClient(SearchBackend):
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

    def _req_v1(
        self,
        path: str,
        params: Optional[Dict[str, str]] = None,
        retry: int = 2,
    ) -> requests.Response:
        url = f"{TIDAL_API_V1_BASE}{path}"
        p = params or {}
        p["countryCode"] = self.country_code

        resp: Optional[requests.Response] = None
        for attempt in range(retry + 1):
            resp = self.session.get(
                url,
                params=p,
                headers={"Accept": "application/json"},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )

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

    def _req_web_v1(
        self,
        path: str,
        params: Optional[Dict[str, str]] = None,
        retry: int = 2,
    ) -> requests.Response:
        url = f"{TIDAL_WEB_V1_BASE}{path}"
        p = params or {}
        if "countryCode" not in p:
            p["countryCode"] = self.country_code
        if "locale" not in p:
            p["locale"] = TIDAL_WEB_LOCALE
        if "deviceType" not in p:
            p["deviceType"] = TIDAL_WEB_DEVICE_TYPE

        token = TIDAL_WEB_TOKEN or self.token
        headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
        if TIDAL_WEB_COOKIES:
            headers["Cookie"] = TIDAL_WEB_COOKIES
        if TIDAL_WEB_USER_AGENT:
            headers["User-Agent"] = TIDAL_WEB_USER_AGENT

        resp: Optional[requests.Response] = None
        for attempt in range(retry + 1):
            resp = self.session.get(url, params=p, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)

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
                return resp

            return resp

        if resp is None:
            raise RuntimeError("Failed to reach TIDAL API.")
        return resp

    def _parse_pages_path(self, path: str) -> Tuple[str, Dict[str, str]]:
        parsed = urllib.parse.urlparse(path)
        if parsed.scheme and parsed.netloc:
            raw_path = parsed.path
            raw_query = parsed.query
        else:
            if "?" in path:
                raw_path, raw_query = path.split("?", 1)
            else:
                raw_path, raw_query = path, ""

        params: Dict[str, str] = {}
        if raw_query:
            for key, values in urllib.parse.parse_qs(raw_query).items():
                if values:
                    params[key] = values[-1]
        if not raw_path.startswith("/"):
            raw_path = f"/{raw_path}"
        return raw_path, params

    def _extract_tracks_from_pages(self, doc: Dict) -> Tuple[List[str], List[str]]:
        tracks: List[str] = []
        seen: set[str] = set()
        data_paths: List[str] = []

        def add_track(track_id: Optional[str]) -> None:
            if not track_id:
                return
            tid = str(track_id)
            if tid not in seen:
                seen.add(tid)
                tracks.append(tid)

        def handle_item(entry: Dict) -> None:
            if not isinstance(entry, dict):
                return
            raw_item = entry.get("item")
            item = raw_item if isinstance(raw_item, dict) else entry
            if not isinstance(item, dict):
                return
            item_type = entry.get("type") or item.get("type")
            if item_type and item_type != "track":
                return
            track_id = item.get("id")
            add_track(track_id)

        def handle_paged_list(paged: Dict) -> None:
            data_path = paged.get("dataApiPath")
            if data_path:
                data_paths.append(data_path)
            items = paged.get("items")
            if isinstance(items, list):
                for entry in items:
                    if isinstance(entry, dict):
                        handle_item(entry)

        def handle_module(module: Dict) -> None:
            if module.get("type") != "ALBUM_ITEMS":
                return
            paged = module.get("pagedList")
            if isinstance(paged, dict):
                handle_paged_list(paged)
            items = module.get("items")
            if isinstance(items, list):
                for entry in items:
                    if isinstance(entry, dict):
                        handle_item(entry)

        if isinstance(doc, dict):
            top_items = doc.get("items")
            if isinstance(top_items, list):
                for entry in top_items:
                    if isinstance(entry, dict):
                        handle_item(entry)

            modules = doc.get("modules")
            if isinstance(modules, list):
                for module in modules:
                    if isinstance(module, dict):
                        handle_module(module)

            rows = doc.get("rows")
            if isinstance(rows, list):
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    mods = row.get("modules")
                    if isinstance(mods, list):
                        for module in mods:
                            if isinstance(module, dict):
                                handle_module(module)

        return tracks, data_paths

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
            resp = self.session.request(
                method,
                url,
                params=p,
                json=json_data,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )

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

    def _get_album_tracks_v1(self, album_id: str) -> List[str]:
        tracks: List[str] = []
        seen: set[str] = set()
        offset = 0
        limit = 100
        total: Optional[int] = None

        while True:
            params = {"limit": str(limit), "offset": str(offset)}
            resp = self._req_v1(f"/albums/{album_id}/tracks", params=params)
            if resp.status_code != 200:
                break
            data = resp.json()
            items = data.get("items") if isinstance(data, dict) else None
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
            if total is None and isinstance(data, dict):
                total = data.get("totalNumberOfItems")
            if not items:
                break
            offset += len(items)
            if total is not None and offset >= total:
                break

        return tracks

    def _get_album_tracks_web(self, album_id: str) -> List[str]:
        tracks: List[str] = []
        seen: set[str] = set()

        resp = self._req_web_v1(
            "/pages/album",
            params={
                "albumId": str(album_id),
                "locale": TIDAL_WEB_LOCALE,
                "deviceType": TIDAL_WEB_DEVICE_TYPE,
            },
        )
        if resp.status_code != 200:
            return tracks

        data = resp.json()
        page_tracks, data_paths = self._extract_tracks_from_pages(data)
        for tid in page_tracks:
            if tid not in seen:
                seen.add(tid)
                tracks.append(tid)

        for data_path in dict.fromkeys(data_paths):
            path, params = self._parse_pages_path(data_path)
            resp = self._req_web_v1(path, params=params)
            if resp.status_code != 200:
                continue
            doc = resp.json()
            more_tracks, _ = self._extract_tracks_from_pages(doc)
            for tid in more_tracks:
                if tid not in seen:
                    seen.add(tid)
                    tracks.append(tid)

        return tracks

    def get_album_tracks(self, album_id: str, expected: Optional[int] = None) -> List[str]:
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

        if tracks and (expected is None or len(tracks) >= expected):
            return tracks

        v1_tracks = self._get_album_tracks_v1(album_id)
        if len(v1_tracks) > len(tracks):
            tracks = v1_tracks

        if tracks and (expected is None or len(tracks) >= expected):
            return tracks

        web_tracks = self._get_album_tracks_web(album_id)
        if len(web_tracks) > len(tracks):
            return web_tracks

        return tracks

    def get_current_user_id(self) -> Optional[str]:
        resp = self._req("GET", "/users/me")
        if resp.status_code != 200:
            return None
        data = resp.json()
        item = data.get("data") if isinstance(data, dict) else None
        if isinstance(item, dict):
            user_id = item.get("id")
            if user_id:
                return str(user_id)
        return None

    def get_user_collection_id(self) -> str:
        user_id = self.get_current_user_id()
        if not user_id:
            raise RuntimeError("Failed to resolve current user id.")
        resp = self._req("GET", f"/userCollections/{user_id}")
        if resp.status_code != 200:
            raise RuntimeError(
                f"Failed to resolve user collection id: {resp.status_code} {resp.text}"
            )
        return user_id

    def add_albums_to_collection(self, collection_id: str, album_ids: List[str]) -> None:
        chunk_size = 50
        now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        unique_ids = list(dict.fromkeys(album_ids))

        for i in range(0, len(unique_ids), chunk_size):
            chunk = unique_ids[i : i + chunk_size]
            body = {
                "data": [
                    {"type": "albums", "id": album_id, "meta": {"addedAt": now}}
                    for album_id in chunk
                ]
            }
            resp = self._req(
                "POST", f"/userCollections/{collection_id}/relationships/albums", json_data=body
            )
            if resp.status_code not in {200, 201, 204}:
                raise RuntimeError(
                    f"Failed to add albums to collection: {resp.status_code} {resp.text}"
                )

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

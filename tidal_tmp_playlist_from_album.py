#!/usr/bin/env python3
# /// script
# dependencies = [
#   "requests",
# ]
# ///
"""Create a small playlist from a single album using OpenAPI v2 only."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import stat
import sys
import time
import urllib.parse
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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
TIDAL_COUNTRY_CODE = os.environ.get("TIDAL_COUNTRY_CODE", "auto")

TOKEN_FILE_DIR = Path.home() / ".config" / "tidal-utils"
TOKEN_FILE_NAME = "tokens.json"


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
                            b"<html><body><h1>Authentication Successful</h1>"
                            b"<p>You can close this tab.</p></body></html>"
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

        resp = requests.post(TIDAL_TOKEN_URL, data=data, timeout=30)
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

        resp = requests.post(TIDAL_TOKEN_URL, data=data, timeout=30)
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

        refresh_token = tokens.get("refresh_token")
        if refresh_token:
            try:
                new_tokens = handler.refresh_tokens(refresh_token)
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
            resp = self.session.request(method, url, params=p, json=json_data, timeout=30)

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

    def get_album_details(self, album_id: str) -> Tuple[str, Optional[int]]:
        resp = self._req("GET", f"/albums/{album_id}", params={"include": "artists"})
        if resp.status_code != 200:
            return album_id, None
        data = resp.json()
        item = data.get("data") or {}
        attr = item.get("attributes") or {}
        title = attr.get("title") or album_id
        return title, attr.get("numberOfItems")

    def get_album_tracks_v2(self, album_id: str) -> List[str]:
        tracks: List[str] = []
        seen: set[str] = set()
        base_path = f"/albums/{album_id}/relationships/items"
        next_link: Optional[str] = base_path

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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a small playlist from a single album using v2 track relationships only.",
    )
    parser.add_argument("--album-id", default="466011512", help="Album ID to test")
    parser.add_argument("--playlist-name", default="tmp", help="Playlist name")
    parser.add_argument(
        "--country-code",
        default=TIDAL_COUNTRY_CODE,
        help="TIDAL country code (use 'auto' to read from token)",
    )
    parser.add_argument(
        "--country-codes",
        help="Comma-separated list of country codes to probe (skips playlist creation)",
    )
    parser.add_argument(
        "--token-file",
        type=Path,
        default=TOKEN_FILE_DIR / TOKEN_FILE_NAME,
        help="Token file path",
    )
    parser.add_argument("--unlisted", action="store_true", help="Create playlist as private")
    parser.add_argument(
        "--probe",
        action="store_true",
        help="Probe track counts only; do not create a playlist",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    album_id = args.album_id.strip()
    if not album_id:
        print("Album ID is required.", file=sys.stderr)
        return 2

    token = get_valid_token(args.token_file)
    codes: List[str] = []
    if args.country_codes:
        for entry in args.country_codes.split(","):
            value = entry.strip().upper()
            if value:
                codes.append(value)
    else:
        codes.append(resolve_country_code(token, args.country_code))
        if (args.country_code or "").strip().lower() == "auto":
            print(f"Using token country code: {codes[0]}")

    probe_only = args.probe or len(codes) > 1

    for code in codes:
        client = TidalClient(token, code)
        title, expected = client.get_album_details(album_id)
        tracks = client.get_album_tracks_v2(album_id)

        print(f"Country: {code}")
        print(f"Album: {title} ({album_id})")
        print(f"Expected tracks: {expected}")
        print(f"V2 tracks returned: {len(tracks)}")
        print("-")

        if probe_only:
            continue

        desc = f"tmp from album {album_id} on {datetime.now().date()}"
        playlist_id = client.create_playlist(args.playlist_name, desc, is_public=not args.unlisted)
        print(f"Playlist created: {args.playlist_name} ({playlist_id})")

        if tracks:
            print(f"Adding {len(tracks)} tracks...")
            client.add_tracks_to_playlist(playlist_id, tracks)
            print("Tracks added.")
        else:
            print("No tracks returned from v2; playlist left empty.")

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

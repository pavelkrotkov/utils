#!/usr/bin/env python3
# /// script
# dependencies = [
#   "requests",
#   "beautifulsoup4",
#   "lxml",
# ]
# ///

import argparse
import base64
import email
import hashlib
import json
import logging
import os
import re
import secrets
import stat
import sys
import time
import unicodedata
import urllib.parse
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import List, Optional, Tuple, Dict

import requests
from bs4 import BeautifulSoup

# --- Configuration & Constants ---

TIDAL_AUTH_URL = "https://login.tidal.com/authorize"
TIDAL_TOKEN_URL = "https://auth.tidal.com/v1/oauth2/token"
TIDAL_API_BASE = "https://openapi.tidal.com/v2"

# Default Client ID for a generic 'tidal-utils' app (or user provided)
TIDAL_CLIENT_ID = os.environ.get("TIDAL_CLIENT_ID")
TIDAL_CLIENT_SECRET = os.environ.get("TIDAL_CLIENT_SECRET")
TIDAL_REDIRECT_URI = os.environ.get("TIDAL_REDIRECT_URI", "http://127.0.0.1:8765/callback")
TIDAL_SCOPES = os.environ.get(
    "TIDAL_SCOPES", "playlists.read playlists.write collection.read collection.write search.read"
)
TIDAL_COUNTRY_CODE = os.environ.get("TIDAL_COUNTRY_CODE", "US")

TOKEN_FILE_DIR = Path.home() / ".config" / "tidal-utils"
TOKEN_FILE_NAME = "tokens.json"

LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt="%H:%M:%S")
logger = logging.getLogger("tidal_importer")

# --- Data Structures ---


@dataclass
class Candidate:
    work_title_hint: str
    performer_hint: str = ""
    label_hint: str = ""
    review_slug_hint: str = ""
    raw_text: str = ""
    line_number: Optional[int] = None
    source_file: str = ""

    def fingerprint(self):
        """Normalized tuple for deduplication."""
        return (
            normalize(self.work_title_hint),
            normalize(self.performer_hint),
            normalize(self.label_hint),
        )


@dataclass
class AlbumHit:
    id: str
    title: str
    artists: List[str]
    release_date: str
    label: str = ""
    copyright: str = ""
    track_count: int = 0
    score: float = 0.0
    match_reasons: List[str] = field(default_factory=list)


# --- Normalization ---


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

# Classical music instrument abbreviations to strip from performer strings
INSTRUMENT_ABBREVS = {
    "pf",
    "vn",
    "va",
    "vc",
    "db",
    "fl",
    "ob",
    "cl",
    "bn",
    "hn",
    "tpt",
    "trb",
    "hp",
    "org",
    "hpd",
    "pno",
    "gtr",
    "perc",
    "sop",
    "mez",
    "ten",
    "bar",
    "bass",
    "cond",
    "dir",
    "ens",
    "orch",
    "choir",
    "sols",
}


def clean_performer_string(performer: str) -> str:
    """Remove instrument abbreviations and clean performer string."""
    if not performer:
        return ""
    # Remove parenthetical content (labels)
    cleaned = re.sub(r"\([^)]*\)$", "", performer).strip()
    # Split and filter out instrument abbreviations
    words = cleaned.split()
    filtered = [w for w in words if w.lower() not in INSTRUMENT_ABBREVS]
    return " ".join(filtered)


def extract_conductor_or_soloist(performer: str) -> List[str]:
    """Extract conductor and soloist names from performer string.

    Handles patterns like:
    - "Orchestra / Conductor"
    - "Soloist; Orchestra / Conductor"
    - "Name1 vn Name2 pf"
    """
    if not performer:
        return []

    names = []
    # Remove label in parentheses at end
    performer = re.sub(r"\([^)]*\)$", "", performer).strip()

    # Split by common separators
    parts = re.split(r"[;/]", performer)
    for part in parts:
        part = part.strip()
        if not part:
            continue

        # Remove orchestra/ensemble keywords to find conductor
        if any(kw in part.lower() for kw in ["orchestra", "philharmonic", "symphony", "ensemble"]):
            # Look for name after these keywords (usually conductor)
            continue

        # Remove instrument abbreviations and extract names
        words = part.split()
        name_words = []
        for w in words:
            if w.lower() in INSTRUMENT_ABBREVS:
                # End of a name, save it
                if name_words:
                    names.append(" ".join(name_words))
                    name_words = []
            else:
                name_words.append(w)
        if name_words:
            names.append(" ".join(name_words))

    return names


def normalize(text: str) -> str:
    if not text:
        return ""
    # Replace dots with spaces (Adès. Elgar -> Adès Elgar)
    text = text.replace(".", " ")
    # NFKD decomposition to separate diacritics
    text = unicodedata.normalize("NFKD", text).encode("ASCII", "ignore").decode("ASCII")
    text = text.lower()
    # Replace separators with spaces
    text = re.sub(r"[/\-&—–]", " ", text)
    # Remove punctuation except alphanumeric and spaces
    text = re.sub(r"[^a-z0-9\s]", "", text)
    # Common classical abbreviations
    text = re.sub(r"\b(opus|op)\b\.?", "op", text)
    text = re.sub(r"\b(number|no)\b\.?", "no", text)
    # Remove noise words (edition info)
    noise = r"\b(deluxe|remastered|expanded|anniversary|live|edition|disc|vol|volume)\b.*"
    text = re.sub(noise, "", text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def should_skip_candidate(work_title: str) -> bool:
    work = work_title.strip()
    if not work or len(work) < 5:
        return True
    if re.match(r"^\d{4}$", work):
        return True
    if "Related Articles" in work:
        return True
    return False


def add_candidate(cand: Candidate, candidates: List[Candidate], seen_fingerprints: set) -> None:
    if should_skip_candidate(cand.work_title_hint):
        return

    fp = cand.fingerprint()
    if fp not in seen_fingerprints:
        seen_fingerprints.add(fp)
        candidates.append(cand)


# --- Parsing Logic ---


def parse_mhtml(file_path: Path) -> Tuple[str, List[Candidate], str]:
    """Parses MHTML file to extract title and candidates."""
    with open(file_path, "rb") as f:
        msg = email.message_from_binary_file(f)

    html_content = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                try:
                    html_content = payload.decode(charset, errors="replace")
                except Exception:
                    html_content = payload.decode("utf-8", errors="replace")
                break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            try:
                html_content = payload.decode(charset, errors="replace")
            except Exception:
                html_content = payload.decode("utf-8", errors="replace")
        else:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                html_content = f.read()

    soup = BeautifulSoup(html_content, "lxml")

    # Extract Page Title
    page_title = file_path.stem
    if soup.title:
        page_title = soup.title.string.strip()
    elif soup.find("h1"):
        page_title = soup.find("h1").get_text(strip=True)

    candidates = []
    seen_fingerprints = set()

    # 1. Look for H2/H3 headers which often denote list items
    for header in soup.find_all(["h2", "h3"]):
        text = header.get_text(" ", strip=True)

        # Stop at related articles
        if "Related Articles" in text:
            break

        work_title = text
        next_elem = header.find_next_sibling()
        performer_hint = ""
        label_hint = ""
        review_slug = ""

        dist = 0
        while next_elem and dist < 5:
            if next_elem.name in ["h2", "h3", "hr"]:
                break

            p_text = next_elem.get_text(" ", strip=True)
            if not p_text:
                next_elem = next_elem.find_next_sibling()
                continue

            # Label check: (LabelName)
            label_match = re.search(r"\(([^)]+)\)$", p_text)
            if label_match:
                label_hint = label_match.group(1)
                performer_hint = p_text[: label_match.start()].strip()

            # Review Link check
            link = next_elem.find("a", href=True)
            if link:
                href = link["href"]
                if "review" in href or "product" in href:
                    parts = href.rstrip("/").split("/")
                    if parts:
                        review_slug = parts[-1].replace("-", " ")

            if label_hint and review_slug:
                break

            next_elem = next_elem.find_next_sibling()
            dist += 1

        add_candidate(
            Candidate(
                work_title_hint=work_title,
                performer_hint=performer_hint,
                label_hint=label_hint,
                review_slug_hint=review_slug,
                raw_text=text[:100],
                source_file=file_path.name,
            ),
            candidates,
            seen_fingerprints,
        )

    return page_title, candidates, ""


def parse_markdown(file_path: Path) -> Tuple[str, List[Candidate], str]:
    """Parses Markdown file to extract title and candidates."""
    with open(file_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    page_title = file_path.stem
    candidates = []
    seen_fingerprints = set()

    for line in lines:
        if line.strip().startswith("# "):
            page_title = line.strip()[2:].strip()
            break

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not line:
            continue

        # Skip images and common noise
        if "![Image" in line or line.startswith("**Source:**") or line.startswith("**!["):
            continue

        # Gramophone MD style: **Composer** Title followed by separator
        # or sometimes just the title.
        if line.startswith("**"):
            raw_text = line

            # Look ahead for separator
            has_separator = False
            if i < len(lines) and re.match(r"^[-=]{3,}$", lines[i].strip()):
                has_separator = True

            # Clean title
            work_title = line.replace("**", " ").strip()
            work_title = re.sub(r"\s+", " ", work_title)

            if len(work_title) < 5 or re.match(r"^\d{4}$", work_title):
                continue

            # If not a title by separator, check if it's just a performer line
            if not has_separator:
                # If current line has (Label) at the end, it's likely a performer line
                if re.search(r"\([^)]+\)$", line):
                    continue
                # Check if next non-empty line has (Label)
                is_likely_performer = False
                for j in range(i, min(i + 3, len(lines))):
                    nl = lines[j].strip()
                    if not nl:
                        continue
                    if re.search(r"\(([^)]+)\)$", nl):
                        is_likely_performer = True
                        break
                if is_likely_performer:
                    continue

            if has_separator:
                i += 1  # skip separator

            performer_hint = ""
            label_hint = ""
            review_slug = ""

            # Context search for performers and labels
            # We consumed lines up to 'i'. We need to look ahead.
            # 'lines' index 'i' is the next line to read.

            look_ahead_limit = 6

            for offset in range(look_ahead_limit):
                idx = i + offset
                if idx >= len(lines):
                    break

                next_l = lines[idx].strip()
                if not next_l:
                    continue

                # Stop if we hit a new section or year header
                if next_l.startswith("---") or next_l.startswith("**20"):
                    break

                # Check if this line is a new candidate title (Header style)
                # i.e. it has a separator after it.
                is_next_header = False
                if idx + 1 < len(lines) and re.match(r"^[-=]{3,}$", lines[idx + 1].strip()):
                    is_next_header = True

                if is_next_header:
                    break

                # If it's just a bold line, it's likely the performer
                if next_l.startswith("**"):
                    # Treat as performer line
                    clean_perf = next_l.replace("**", "").replace("__", "")
                    # Check for label inside parens
                    label_match = re.search(r"\(([^)]+)\)$", clean_perf)
                    if label_match:
                        if not label_hint:
                            label_hint = label_match.group(1)
                        clean_perf = clean_perf[: label_match.start()]

                    if not performer_hint:
                        performer_hint = clean_perf.strip()
                    else:
                        performer_hint += " " + clean_perf.strip()
                else:
                    # Normal text line, check for label or links
                    label_match = re.search(r"\(([^)]+)\)$", next_l)
                    if label_match:
                        if not label_hint:
                            label_hint = label_match.group(1)
                        # Text before might be performer part
                        pre_label = next_l[: label_match.start()].strip()
                        if pre_label and not performer_hint:
                            performer_hint = pre_label

                    link_match = re.search(r"\[.*\]\((.*)\)", next_l)
                    if link_match:
                        href = link_match.group(1)
                        if "review" in href or "product" in href:
                            review_slug = href.rstrip("/").split("/")[-1].replace("-", " ")

            # Cleanup
            performer_hint = re.sub(r"[*_]", "", performer_hint).strip()

            cand = Candidate(
                work_title_hint=work_title,
                performer_hint=performer_hint,
                label_hint=label_hint,
                review_slug_hint=review_slug,
                raw_text=raw_text[:100],
                line_number=i - 1,
                source_file=file_path.name,
            )

            add_candidate(cand, candidates, seen_fingerprints)

    return page_title, candidates, ""


def extract_candidates(file_path: Path) -> Tuple[str, List[Candidate], str]:
    ext = file_path.suffix.lower()
    if ext in [".mhtml", ".mht"]:
        return parse_mhtml(file_path)
    elif ext in [".md", ".markdown"]:
        return parse_markdown(file_path)
    else:
        raise ValueError(f"Unsupported file extension: {ext}")


# --- Auth Logic ---


class OAuthHandler:
    def __init__(self):
        self.code_verifier = self._generate_code_verifier()
        self.code_challenge = self._generate_code_challenge(self.code_verifier)
        self.auth_code = None
        self.state = secrets.token_urlsafe(16)

    def _generate_code_verifier(self) -> str:
        token = secrets.token_urlsafe(32)
        return token.rstrip("=")

    def _generate_code_challenge(self, verifier: str) -> str:
        m = hashlib.sha256()
        m.update(verifier.encode("ascii"))
        digest = m.digest()
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

    def wait_for_callback(self):
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
            raise Exception(f"Token exchange failed: {resp.text}")
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
            raise Exception(f"Token refresh failed: {resp.text}")
        return resp.json()


def save_tokens(tokens: Dict, file_path: Path):
    file_path.parent.mkdir(parents=True, exist_ok=True)
    if "expires_in" in tokens:
        tokens["expires_at"] = int(time.time()) + tokens["expires_in"]

    with open(file_path, "w") as f:
        json.dump(tokens, f)
    os.chmod(file_path, stat.S_IRUSR | stat.S_IWUSR)


def load_tokens(file_path: Path) -> Optional[Dict]:
    if not file_path.exists():
        return None
    try:
        with open(file_path, "r") as f:
            return json.load(f)
    except Exception:
        return None


def get_valid_token(token_file: Path) -> str:
    tokens = load_tokens(token_file)
    handler = OAuthHandler()

    if tokens:
        expires_at = tokens.get("expires_at", 0)
        if time.time() < expires_at - 60:
            return tokens["access_token"]

        logger.info("Access token expired, refreshing...")
        try:
            new_tokens = handler.refresh_tokens(tokens["refresh_token"])
            tokens.update(new_tokens)
            save_tokens(tokens, token_file)
            return tokens["access_token"]
        except Exception as e:
            logger.warning(f"Refresh failed: {e}. Re-authenticating.")

    if not TIDAL_CLIENT_ID:
        print("Error: TIDAL_CLIENT_ID environment variable is not set.")
        sys.exit(3)

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


# --- API Interaction ---


class TidalClient:
    def __init__(self, token: str, country_code: str):
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
        self, method: str, path: str, params: Dict = None, json_data: Dict = None, retry=2
    ) -> requests.Response:
        url = f"{TIDAL_API_BASE}{path}"
        p = params or {}
        p["countryCode"] = self.country_code

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
                # Log but allow caller to handle if needed
                if resp.status_code == 404:
                    return resp
                resp.raise_for_status()

            return resp
        return resp

    def search_albums(self, query: str, limit: int = 10) -> List[AlbumHit]:
        encoded_query = urllib.parse.quote(query)
        params = {
            "page[limit]": limit,
            # Ensure we get the actual resources
            "include": "albums.artists",
        }
        try:
            resp = self._req("GET", f"/searchResults/{encoded_query}", params=params)
            if resp.status_code != 200:
                return []
            data = resp.json()
        except Exception as e:
            logger.error(f"Search failed for '{query}': {e}")
            return []

        hits = []
        search_node = data.get("data", {})
        if not search_node or search_node.get("type") != "searchResults":
            # Fallback if it WAS a list (just in case)
            if isinstance(search_node, list):
                # Previous logic (unlikely based on debug)
                return []
            return []

        # Get Album IDs from relationships
        album_rels = search_node.get("relationships", {}).get("albums", {}).get("data", [])

        # Build lookup from included
        included = data.get("included", [])
        albums_lookup = {(x["type"], x["id"]): x for x in included if x.get("type") == "albums"}
        artists_lookup = {(x["type"], x["id"]): x for x in included if x.get("type") == "artists"}

        for rel in album_rels:
            if rel.get("type") != "albums":
                continue

            # Find full object
            album_obj = albums_lookup.get(("albums", rel["id"]))
            if not album_obj:
                continue

            attr = album_obj.get("attributes", {})

            cright = attr.get("copyright", "")
            if isinstance(cright, dict):
                cright = str(cright)  # Fallback

            hit = AlbumHit(
                id=album_obj["id"],
                title=attr.get("title", ""),
                release_date=attr.get("releaseDate", ""),
                artists=[],
                track_count=attr.get("numberOfItems", 0),
                copyright=str(cright),
            )

            # Resolve artists
            # Album object -> relationships -> artists
            alb_art_rels = album_obj.get("relationships", {}).get("artists", {}).get("data", [])
            for art_rel in alb_art_rels:
                art_obj = artists_lookup.get(("artists", art_rel["id"]))
                if art_obj:
                    hit.artists.append(art_obj.get("attributes", {}).get("name", ""))

            hits.append(hit)

        return hits

    def get_album_tracks(self, album_id: str) -> List[str]:
        tracks = []
        next_link = f"/albums/{album_id}/relationships/items?page[limit]=100"

        while next_link:
            path = next_link.replace(TIDAL_API_BASE, "")
            resp = self._req("GET", path)
            if resp.status_code != 200:
                break
            data = resp.json()

            items = data.get("data", [])
            for item in items:
                if item["type"] == "tracks":
                    tracks.append(item["id"])

            links = data.get("links", {})
            next_link = links.get("next")

        return tracks

    def get_album_details(self, album_id: str) -> Optional[AlbumHit]:
        try:
            params = {"include": "artists"}
            resp = self._req("GET", f"/albums/{album_id}", params=params)
            if resp.status_code != 200:
                return None
            data = resp.json()
            item = data.get("data")
            if not item or item.get("type") != "albums":
                return None

            attr = item.get("attributes", {})
            cright = str(attr.get("copyright", ""))

            hit = AlbumHit(
                id=item["id"],
                title=attr.get("title", ""),
                release_date=attr.get("releaseDate", ""),
                artists=[],
                track_count=attr.get("numberOfItems", 0),
                copyright=cright,
            )

            # Resolve artists
            included = data.get("included", [])
            artists_lookup = {
                (x["type"], x["id"]): x for x in included if x.get("type") == "artists"
            }

            rel_artists = item.get("relationships", {}).get("artists", {}).get("data", [])
            for art_rel in rel_artists:
                art_obj = artists_lookup.get(("artists", art_rel["id"]))
                if art_obj:
                    hit.artists.append(art_obj.get("attributes", {}).get("name", ""))

            return hit
        except Exception as e:
            logger.warning(f"Failed to fetch album details for {album_id}: {e}")
            return None

    def search_track_fallback(self, query: str) -> Optional[AlbumHit]:
        encoded_query = urllib.parse.quote(query)
        params = {"page[limit]": 5, "include": "tracks.album"}
        try:
            resp = self._req("GET", f"/searchResults/{encoded_query}", params=params)
            if resp.status_code != 200:
                return None
            data = resp.json()
            search_node = data.get("data", {})

            track_rels = search_node.get("relationships", {}).get("tracks", {}).get("data", [])
            included = data.get("included", [])
            tracks_lookup = {(x["type"], x["id"]): x for x in included if x.get("type") == "tracks"}

            for rel in track_rels:
                track_obj = tracks_lookup.get(("tracks", rel["id"]))
                if not track_obj:
                    continue

                # Check album relationship
                alb_rel = track_obj.get("relationships", {}).get("album", {}).get("data", {})
                if alb_rel and alb_rel.get("id"):
                    return self.get_album_details(alb_rel.get("id"))
        except Exception:
            pass
        return None

    def create_playlist(self, name: str, description: str, is_public: bool) -> str:
        body = {
            "data": {"type": "playlists", "attributes": {"name": name, "description": description}}
        }

        # Try /my/playlists first
        for path in ["/my/playlists", "/playlists"]:
            try:
                resp = self._req("POST", path, json_data=body)
                if resp.status_code == 201:
                    # Extracts ID from Location header: /playlists/<id>
                    loc = resp.headers.get("Location", "")
                    if loc:
                        return loc.split("/")[-1]
                    # Fallback to body if present
                    if resp.content:
                        return resp.json()["data"]["id"]
            except Exception:
                continue

        raise RuntimeError("Failed to create playlist.")

    def add_tracks_to_playlist(self, playlist_id: str, track_ids: List[str]):
        chunk_size = 20
        for i in range(0, len(track_ids), chunk_size):
            chunk = track_ids[i : i + chunk_size]
            body = {"data": [{"type": "tracks", "id": tid} for tid in chunk]}
            self._req("POST", f"/playlists/{playlist_id}/relationships/items", json_data=body)


# --- Matching Logic ---


def get_significant_tokens(text: str) -> List[str]:
    """Extracts significant tokens (longer than 2 chars, ignoring common stops)."""
    norm = normalize(text)
    return [t for t in norm.split() if len(t) > 2 and t not in STOPWORDS]


def score_album(candidate: Candidate, hit: AlbumHit) -> float:
    score = 0.0
    cand_norm = normalize(candidate.work_title_hint)
    hit_norm = normalize(hit.title)

    cand_tokens = set(cand_norm.split())
    hit_tokens = set(hit_norm.split())

    if not cand_tokens:
        return 0.0

    overlap = cand_tokens.intersection(hit_tokens)
    overlap_ratio = len(overlap) / len(cand_tokens)
    title_score = overlap_ratio * 0.5
    score += title_score

    if candidate.performer_hint:
        # Clean performer string and extract meaningful names
        cleaned_perf = clean_performer_string(candidate.performer_hint)
        extracted_names = extract_conductor_or_soloist(candidate.performer_hint)

        # Combine cleaned performer and extracted names for matching
        all_perf_tokens = set()
        for name in [cleaned_perf] + extracted_names:
            perf_norm = normalize(name)
            # Extract tokens longer than 3 chars (likely surnames)
            all_perf_tokens.update(t for t in perf_norm.split() if len(t) > 3)

        hit_artists_norm = [normalize(a) for a in hit.artists]
        full_hit_artists_str = " ".join(hit_artists_norm)

        # Also check hit title for performer names (some albums include artist in title)
        combined_hit_str = full_hit_artists_str + " " + hit_norm

        # Check if ANY performer token exists in hit artists or title
        found_any = False
        for tok in all_perf_tokens:
            if tok in combined_hit_str:
                found_any = True
                break

        if found_any:
            score += 0.4
        else:
            # Reduced penalty: If title overlap is low, don't penalize as much
            # (likely the album just isn't in the catalog)
            if title_score < 0.25:
                score -= 0.3  # Reduced penalty for poor title match
            else:
                score -= 0.5  # Still penalize when title matches but performer doesn't

    if candidate.label_hint:
        label_norm = normalize(candidate.label_hint)
        hit_copy_norm = normalize(hit.copyright)
        label_map = {
            "dg": "deutsche grammophon",
            "hm": "harmonia mundi",
            "sony": "sony classical",
            "decca": "decca",
        }
        label_norm = label_map.get(label_norm, label_norm)
        if label_norm in hit_copy_norm:
            score += 0.2

    if candidate.review_slug_hint:
        slug_norm = normalize(candidate.review_slug_hint)
        slug_tokens = set(slug_norm.split())
        slug_overlap = slug_tokens.intersection(hit_tokens)
        if len(slug_overlap) >= 2:
            score += 0.1

    return min(score, 1.0)


def find_best_match(
    client: TidalClient, candidate: Candidate, force: bool = False
) -> Tuple[Optional[AlbumHit], float, List[str]]:
    wt = candidate.work_title_hint
    ph = candidate.performer_hint
    lh = candidate.label_hint

    def push_query(values: List[str], query: str) -> None:
        cleaned = " ".join(query.split())
        if cleaned and len(cleaned) > 2 and cleaned not in values:
            values.append(cleaned)

    queries: List[str] = []

    # Clean performer string (remove instrument abbreviations)
    cleaned_perf = clean_performer_string(ph) if ph else ""
    extracted_names = extract_conductor_or_soloist(ph) if ph else []
    title_tokens = get_significant_tokens(wt)

    # 1. Try extracted performer names with title
    for name in extracted_names:
        name_tokens = get_significant_tokens(name)
        if name_tokens:
            # Try last name (surname) + title tokens
            surname = name_tokens[-1] if name_tokens else ""
            if surname and len(surname) > 3:
                short_title = " ".join(title_tokens[:4])
                push_query(queries, f"{surname} {short_title}")
                # Try surname + composer (usually first word)
                if title_tokens:
                    push_query(queries, f"{surname} {title_tokens[0]}")

    # 2. Try full cleaned performer name + title
    if cleaned_perf:
        # Performer with short title
        push_query(queries, f"{cleaned_perf} {' '.join(title_tokens[:3])}")

    # 3. Performer + Significant Title Tokens (original logic with cleaned perf)
    if cleaned_perf:
        perf_tokens = get_significant_tokens(cleaned_perf)
        if perf_tokens:
            surname = perf_tokens[-1]
            short_title = " ".join(title_tokens[:4])
            push_query(queries, f"{surname} {short_title}")
            if title_tokens:
                second = title_tokens[1] if len(title_tokens) > 1 else ""
                push_query(queries, f"{surname} {title_tokens[0]} {second}")

    # 4. Review Slug (cleaned)
    if candidate.review_slug_hint:
        push_query(queries, candidate.review_slug_hint.replace("-", " "))

    # 5. Standard queries with cleaned performer
    push_query(queries, f"{wt} {cleaned_perf} {lh}")
    push_query(queries, f"{wt} {cleaned_perf}")

    # 6. Title only (last resort)
    push_query(queries, wt)

    # 7. Try just composer + work type (for generic titles)
    if title_tokens:
        # e.g., "Scarlatti Sonatas", "Bach Mass", "Schumann Sonatas"
        composer = title_tokens[0] if title_tokens else ""
        if len(title_tokens) > 1:
            work_type = title_tokens[-1]  # Often "sonatas", "concerto", etc.
            push_query(queries, f"{composer} {work_type}")

    seen_ids = set()
    best_hit = None
    best_score = -1.0
    debug_log = []

    for q in queries:
        if len(q) < 3:
            continue

        # Limit to 5 results per query to avoid noise
        hits = client.search_albums(q, limit=5)
        for hit in hits:
            if hit.id in seen_ids:
                continue
            seen_ids.add(hit.id)

            s = score_album(candidate, hit)
            hit.score = s
            debug_log.append(
                f"Hit: {hit.title} ({hit.id}) Artists: {hit.artists} Score: {s:.2f} [Q: {q}]"
            )

            if s > best_score:
                best_score = s
                best_hit = hit

        if best_score > 0.85:
            break

    if best_hit:
        if best_score < 0.5 and not force:
            fallback = client.search_track_fallback(f"{wt} {ph}")
            if fallback:
                return fallback, 0.5, debug_log + ["Fallback Track Match"]
            return None, 0.0, debug_log

        return best_hit, best_score, debug_log

    if not best_hit:
        fallback = client.search_track_fallback(f"{wt} {ph}")
        if fallback:
            return fallback, 0.5, debug_log + ["Fallback Track Match"]

    return None, 0.0, debug_log


# --- Main ---


def main():
    parser = argparse.ArgumentParser(
        description="Import albums from webpage/markdown to Tidal Playlist."
    )
    parser.add_argument("input_path", type=Path, help="Input .mhtml or .md file")
    parser.add_argument("--name", help="Override playlist name")
    parser.add_argument("--unlisted", action="store_true", help="Make playlist private")
    parser.add_argument(
        "--token-file",
        type=Path,
        default=TOKEN_FILE_DIR / TOKEN_FILE_NAME,
        help="Path to token file",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Do not create playlist, just show matches"
    )
    parser.add_argument("--force", action="store_true", help="Accept low confidence matches")
    parser.add_argument("--country-code", default=TIDAL_COUNTRY_CODE, help="Tidal Country Code")
    parser.add_argument("--debug", action="store_true", help="Verbose logging")

    args = parser.parse_args()

    if args.debug:
        logger.setLevel(logging.DEBUG)

    if not args.input_path.exists():
        logger.error(f"Input file not found: {args.input_path}")
        sys.exit(2)

    logger.info(f"Parsing {args.input_path}...")
    try:
        title, candidates, _ = extract_candidates(args.input_path)
    except Exception as e:
        logger.error(f"Parsing failed: {e}")
        sys.exit(2)

    if not candidates:
        logger.warning("No candidates found.")
        sys.exit(0)

    logger.info(f"Found {len(candidates)} candidates from '{title}'")
    for i, c in enumerate(candidates):
        logger.info(f"  {i + 1}. {c.work_title_hint} | {c.performer_hint} | {c.label_hint}")

    token = "DRY_RUN_TOKEN"
    # Authenticate if possible, even in dry run, to allow matching tests
    # If TIDAL_CLIENT_ID is missing, we can still try if we have a valid cached token
    has_tokens = args.token_file.exists() and load_tokens(args.token_file)

    if TIDAL_CLIENT_ID or has_tokens:
        try:
            token = get_valid_token(args.token_file)
        except Exception as e:
            if not args.dry_run:
                logger.error(f"Authentication failed: {e}")
                sys.exit(3)
            else:
                logger.warning(f"Authentication failed in dry-run ({e}). Matching will be skipped.")
    elif not args.dry_run:
        logger.error("TIDAL_CLIENT_ID required for real run.")
        sys.exit(3)

    client = TidalClient(token, args.country_code)

    matched_albums = []
    skipped = 0

    if token != "DRY_RUN_TOKEN":
        logger.info("Matching albums...")
        for cand in candidates:
            hit, score, logs = find_best_match(client, cand, force=args.force)

            if hit:
                logger.info(f"MATCH: '{cand.work_title_hint}'")
                logger.info(f"       -> Title: {hit.title}")
                logger.info(f"       -> Artists: {', '.join(hit.artists)}")
                logger.info(f"       -> Label: {hit.copyright}")
                logger.info(f"       -> Released: {hit.release_date} | Tracks: {hit.track_count}")
                logger.info(f"       -> ID: {hit.id} (Score: {score:.2f})")
                matched_albums.append(hit.id)
            else:
                logger.warning(f"NO MATCH: '{cand.work_title_hint}'")
                if args.debug:
                    for log_line in logs:
                        logger.debug(f"  {log_line}")
                skipped += 1

            time.sleep(0.2)
    else:
        logger.info("Dry run without credentials/token. Skipping matching step.")

    if args.dry_run:
        logger.info("Dry run complete (Playlist creation skipped).")
        return

    if not matched_albums:
        logger.warning("No albums matched. Exiting.")
        sys.exit(0)

    pl_name = args.name
    if not pl_name:
        clean_title = title.split("|")[0].split("-")[0].strip()
        pl_name = f"{clean_title[:70]} (Imported)"

    desc = f"Imported from {args.input_path.name} on {datetime.now().date()}."

    logger.info(f"Creating playlist: {pl_name}")
    try:
        pl_id = client.create_playlist(pl_name, desc, not args.unlisted)
        logger.info(f"Playlist created: {pl_id}")
    except Exception as e:
        logger.error(f"Failed to create playlist: {e}")
        sys.exit(4)

    all_tracks = []
    logger.info("Fetching tracks...")
    for aid in matched_albums:
        try:
            tracks = client.get_album_tracks(aid)
            all_tracks.extend(tracks)
        except Exception as e:
            logger.error(f"Error fetching tracks for album {aid}: {e}")

    if all_tracks:
        logger.info(f"Adding {len(all_tracks)} tracks...")
        try:
            client.add_tracks_to_playlist(pl_id, all_tracks)
            logger.info("Done.")
        except Exception as e:
            logger.error(f"Error adding tracks: {e}")
            sys.exit(4)
    else:
        logger.warning("No tracks found to add.")


if __name__ == "__main__":
    main()

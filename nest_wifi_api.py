"""Authenticated, read-only requests to the Google Wifi cloud API."""

from __future__ import annotations

import requests

from nest_wifi_auth import google_auth
from nest_wifi_keychain import MonitorError

CLOUD = "https://googlehomefoyer-pa.googleapis.com/v2/"


class GoogleWifi:
    def __init__(self, credentials: dict[str, str]):
        self.credentials = credentials
        self.token = google_auth(credentials)
        self.session = requests.Session()
        self.session.trust_env = False

    def get(self, path: str, *, retry: bool = True) -> dict:
        try:
            response = self.session.get(
                CLOUD + path,
                timeout=(10, 20),
                allow_redirects=False,
                headers={"Authorization": "Bearer " + self.token},
            )
            if response.status_code == 401 and retry:
                self.token = google_auth(self.credentials)
                return self.get(path, retry=False)
            if response.status_code != 200:
                raise MonitorError(f"Google API HTTP {response.status_code}")
            payload = response.json()
            if not isinstance(payload, dict):
                raise MonitorError("Unexpected Google API schema.")
            return payload
        except (requests.RequestException, ValueError):
            raise MonitorError("Google API transport or JSON error.") from None

    def snapshot(self) -> list[dict]:
        from nest_wifi_snapshot import snapshot_networks

        return snapshot_networks(self)

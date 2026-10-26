"""Select cloud telemetry and redact any credentials before saving it."""

from __future__ import annotations

import datetime as dt
from urllib.parse import quote

from nest_wifi_keychain import MonitorError
from nest_wifi_redact import redact


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


ENDPOINTS = (
    # Keep this list read-only: these calls observe state but do not test links
    # or change router settings. Historical test results retain their own time.
    "status",
    "stations",
    "realtimeMetrics",
    "speedTestResults",
    "wifiblasterSpeedTestResults",
    "meshSpeedTestResults",
    "backhaulOfChildNodes",
)


def _read_endpoint(api, base: str, endpoint: str) -> dict:
    """Record each endpoint separately so one optional 404 stays visible."""
    path = f"{base}{endpoint}?prettyPrint=false"
    if endpoint == "speedTestResults":
        path += "&maxResultCount=1"
    try:
        return {"retrieved_at": now(), "payload": redact(api.get(path))}
    except MonitorError as error:
        # Auth failures and rate limits invalidate the whole polling attempt.
        if "HTTP 429" in str(error) or "HTTP 401" in str(error):
            raise
        return {"retrieved_at": now(), "error": str(error)}


def _read_group(api, group: dict) -> dict:
    """Allowlist telemetry fields; group settings can contain credentials."""
    if not isinstance(group, dict) or not isinstance(group.get("id"), str):
        raise MonitorError("Unexpected Google network schema.")
    # Do not persist groupSettings or any other unreviewed account payload.
    row = {
        key: group[key]
        for key in ("id", "name", "friendlyName", "networkName", "accessPoints")
        if key in group
    }
    base = "groups/" + quote(group["id"], safe="") + "/"
    row["observations"] = {endpoint: _read_endpoint(api, base, endpoint) for endpoint in ENDPOINTS}
    return redact(row)


def snapshot_networks(api) -> list[dict]:
    """Read known networks and close the HTTP session on every exit path."""
    try:
        groups = api.get("groups?prettyPrint=false").get("groups")
        if not isinstance(groups, list) or not groups:
            raise MonitorError("No Google Wifi networks returned for this account.")
        return [_read_group(api, group) for group in groups]
    finally:
        api.session.close()

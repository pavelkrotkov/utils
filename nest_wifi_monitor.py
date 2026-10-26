#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["gpsoauth==2.0.0", "requests>=2.32,<3"]
# ///
"""Observe a Google/Nest Wifi network from macOS without changing its settings.

Usage: uv run ./nest_wifi_monitor.py --data-dir ~/private/nest-wifi auth
       uv run ./nest_wifi_monitor.py --data-dir ~/private/nest-wifi snapshot
       uv run ./nest_wifi_monitor.py --data-dir ~/private/nest-wifi start
       uv run ./nest_wifi_monitor.py --data-dir ~/private/nest-wifi status
       uv run ./nest_wifi_monitor.py --data-dir ~/private/nest-wifi stop
"""

from __future__ import annotations

import argparse
import getpass
import os
import secrets
import sys
import webbrowser
from pathlib import Path

from nest_wifi_api import GoogleWifi
from nest_wifi_auth import google_auth, normalize_oauth_cookie
from nest_wifi_collector import collect, prepare_data_dir, status
from nest_wifi_daemon import start, stop
from nest_wifi_keychain import MonitorError, keychain
from nest_wifi_runtime import _locked_snapshot, watch


def prompt_oauth_cookie() -> str:
    while True:
        try:
            return normalize_oauth_cookie(getpass.getpass("oauth_token cookie (hidden): "))
        except MonitorError as error:
            print(error, file=sys.stderr)


def authenticate(data_dir: Path) -> None:
    if not sys.stdin.isatty():
        raise MonitorError("Sign-in requires an interactive Terminal.")
    print(
        "Sign in at https://accounts.google.com/EmbeddedSetup with the network owner.\n"
        "In browser developer tools, copy the oauth_token cookie Value.\n"
        "Paste it only into the hidden prompt; never into chat or a file.\n"
        "A reusable Android token will be stored in macOS Keychain."
    )
    webbrowser.open("https://accounts.google.com/EmbeddedSetup")
    credentials = {
        "email": input("Google account email: ").strip(),
        "android_id": secrets.token_hex(8),
    }
    if "@" not in credentials["email"]:
        raise MonitorError("Expected a Google account email address.")
    credentials["master_token"] = google_auth(credentials, cookie=prompt_oauth_cookie())
    api = GoogleWifi(credentials)
    try:
        groups = api.get("groups?prettyPrint=false").get("groups")
        if not isinstance(groups, list) or not groups:
            raise MonitorError("Sign-in succeeded but returned no Google Wifi networks.")
    finally:
        api.session.close()
    keychain(credentials, interactive=True)
    print(f"Sign-in verified for {len(groups)} network(s); credential saved in Keychain.")
    collect(data_dir)


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir", required=True, type=Path, help="Private log/runtime directory"
    )
    parser.add_argument("command", choices=("auth", "snapshot", "watch", "start", "stop", "status"))
    parser.add_argument("--local-only", action="store_true", help="Skip Google cloud requests")
    args = parser.parse_args(argv)
    data_dir = args.data_dir.expanduser().resolve()
    prepare_data_dir(data_dir)
    if args.command == "auth":
        authenticate(data_dir)
    elif args.command == "status":
        status(data_dir)
    elif args.command == "start":
        start(data_dir, args.local_only)
    elif args.command == "stop":
        stop(data_dir)
    elif args.command == "watch":
        watch(data_dir, args.local_only)
    else:
        state = _locked_snapshot(data_dir, args.local_only)
        return 2 if state in ("unavailable", "partial") else 0
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (MonitorError, OSError) as error:
        message = (
            str(error) if isinstance(error, MonitorError) else "Local file/OS operation failed."
        )
        print(message, file=sys.stderr)
        sys.exit(1)
    except (KeyboardInterrupt, EOFError):
        print("Cancelled.", file=sys.stderr)
        sys.exit(130)

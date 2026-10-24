#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["wyze-sdk==2.3.9"]
# ///
"""Export Wyze names, MACs, and locally observed IPs without saving credentials.

Example:
    uv run ./wyze_ip_map.py --nest-log /path/to/nest/monitor/logs \
        --probe-nest --output /path/to/private/wyze_ip_map.csv

The optional Nest log is a local JSONL file or directory of dated JSONL files.
Use --existing-csv to refresh a prior export without signing in again.
A probe sends one ping only to Nest clients named Wyze; it makes no settings changes.
"""

import argparse
import csv
import getpass
import ipaddress
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from wyze_sdk import Client

ARP_ENTRY = re.compile(
    r"\((?P<ip>\d+\.\d+\.\d+\.\d+)\) at "
    r"(?P<mac>(?:[0-9a-fA-F]{1,2}:){5}[0-9a-fA-F]{1,2})\b"
)
FIELDS = (
    "wyze_name",
    "wyze_type",
    "wyze_model",
    "wyze_mac",
    "wyze_online",
    "local_ip",
    "ip_source",
    "wyze_reported_ip",
    "nest_client_name",
    "nest_connected",
    "review",
)


def normalize_mac(value: str) -> str:
    parts = (value or "").split(":")
    if len(parts) == 6 and all(1 <= len(part) <= 2 for part in parts):
        return "".join(part.zfill(2) for part in parts).lower()
    return re.sub(r"[^0-9a-fA-F]", "", value or "").lower()


def physical_mac(value: str) -> str:
    """Return a device's network MAC from Wyze switch or plug-channel IDs."""
    value = (value or "").rsplit("_", 1)[-1].split("-", 1)[0]
    normalized = normalize_mac(value)
    return normalized if len(normalized) == 12 else ""


def read_existing(path: Path) -> list[SimpleNamespace]:
    with path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    return [
        SimpleNamespace(
            mac=row["wyze_mac"],
            nickname=row["wyze_name"],
            type=row["wyze_type"],
            product=SimpleNamespace(model=row["wyze_model"]),
            ip=row["wyze_reported_ip"],
            is_online=row.get("wyze_online", ""),
        )
        for row in rows
    ]


def read_nest(path: Path | None) -> tuple[str, list[dict]]:
    if path is None:
        return "", []
    paths = sorted(path.glob("*.jsonl"), reverse=True) if path.is_dir() else [path]
    for log_path in paths:
        for line in reversed(log_path.read_text(encoding="utf-8").splitlines()):
            record = json.loads(line)
            for network in record.get("google_cloud", {}).get("networks", []):
                stations = (
                    network.get("observations", {})
                    .get("stations", {})
                    .get("payload", {})
                    .get("stations")
                )
                if isinstance(stations, list):
                    return record.get("collected_at", ""), stations
    raise ValueError(f"No Nest station inventory in {path}")


def arp_by_mac() -> dict[str, str]:
    result = subprocess.run(["arp", "-an"], check=True, capture_output=True, text=True)
    matches = ARP_ENTRY.finditer(result.stdout)
    return {normalize_mac(match["mac"]): match["ip"] for match in matches}


def probe_nest_wyze(stations: list[dict]) -> None:
    ips = {
        station.get("ipAddress")
        for station in stations
        if station.get("connected") is True
        and "wyze"
        in (
            str(station.get("friendlyName") or "") + str(station.get("automaticFriendlyName") or "")
        ).lower()
    }
    for ip in sorted(ips - {None, ""}):
        try:
            if ipaddress.ip_address(ip).is_private:
                subprocess.run(
                    ["ping", "-c", "1", "-W", "1000", ip],
                    capture_output=True,
                    timeout=3,
                    check=False,
                )
        except (ValueError, subprocess.TimeoutExpired):
            continue


def make_rows(devices, stations: list[dict], arp: dict[str, str]) -> list[dict]:
    nest_by_ip = {s["ipAddress"]: s for s in stations if s.get("ipAddress")}
    mac_by_ip = {ip: mac for mac, ip in arp.items()}
    rows = []
    for device in devices:
        mac = device.mac or ""
        network_mac = physical_mac(mac)
        reported = getattr(device, "ip", None) or ""
        observed = arp.get(network_mac, "")
        station = nest_by_ip.get(observed, {})
        shared_channel = "-" in mac
        embedded_mac = "_" in mac
        if observed and reported and observed != reported:
            review = "Wyze IP differs from ARP"
        elif observed and station.get("connected") is True:
            review = ""
        elif observed:
            review = "Verify ARP address is current"
        elif reported and mac_by_ip.get(reported) not in (None, network_mac):
            review = "Wyze IP belongs to another MAC"
        elif reported:
            review = "Wyze IP unverified"
        else:
            review = "No local IP found"
        rows.append(
            {
                "wyze_name": device.nickname or "",
                "wyze_type": device.type or "",
                "wyze_model": getattr(device.product, "model", "") or "",
                "wyze_mac": mac,
                "wyze_online": getattr(device, "is_online", ""),
                "local_ip": observed,
                "ip_source": (
                    "Parent ARP cache"
                    if shared_channel
                    else "ID MAC ARP cache"
                    if embedded_mac
                    else "ARP cache"
                )
                if observed
                else "",
                "wyze_reported_ip": reported,
                "nest_client_name": (
                    station.get("friendlyName") or station.get("automaticFriendlyName") or ""
                ),
                "nest_connected": station.get("connected") if station else "",
                "review": review,
            }
        )
    return sorted(rows, key=lambda row: (row["wyze_name"].casefold(), row["wyze_mac"]))


def sign_in_and_list():
    email = input("Wyze account email: ").strip()
    key_id = getpass.getpass("Wyze Key ID: ").strip()
    api_key = getpass.getpass("Wyze API key: ").strip()
    password = getpass.getpass("Wyze password: ")
    if not all((email, key_id, api_key, password)):
        raise ValueError("all four Wyze credentials are required")
    client = Client()
    token = client.login(email=email, password=password, key_id=key_id, api_key=api_key)[
        "access_token"
    ]
    return Client(token=token).devices_list()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nest-log", type=Path, help="Nest JSONL log or logs directory")
    parser.add_argument(
        "--existing-csv",
        type=Path,
        help="Reconcile a prior export without Wyze sign-in",
    )
    parser.add_argument(
        "--probe-nest",
        action="store_true",
        help="Ping connected Nest clients named Wyze once to refresh ARP",
    )
    parser.add_argument("--output", type=Path, help="Private CSV output path")
    parser.add_argument("--self-check", action="store_true", help="Run an offline matching check")
    args = parser.parse_args()
    if args.self_check:
        device = SimpleNamespace(
            mac="AA:BB:CC:DD:EE:FF",
            nickname="Porch",
            type="Camera",
            product=SimpleNamespace(model="Cam"),
            ip="192.168.1.9",
        )
        station = {"ipAddress": "192.168.1.7", "connected": True}
        row = make_rows([device], [station], {"aabbccddeeff": "192.168.1.7"})[0]
        assert normalize_mac("a:0:b:1:c:2") == "0a000b010c02"
        assert physical_mac("LD_SS1_AABBCCDDEEFF") == "aabbccddeeff"
        assert physical_mac("AABBCCDDEEFF-0001") == "aabbccddeeff"
        assert row["local_ip"] == "192.168.1.7"
        assert row["review"] == "Wyze IP differs from ARP"
        print("Offline matching check passed.")
        return 0
    if args.output is None:
        parser.error("--output is required unless --self-check is used")
    if args.probe_nest and args.nest_log is None:
        parser.error("--probe-nest requires --nest-log")
    if not args.output.parent.is_dir():
        parser.error("output parent directory does not exist")

    collected, stations = read_nest(args.nest_log)
    devices = read_existing(args.existing_csv) if args.existing_csv else sign_in_and_list()
    if args.probe_nest:
        probe_nest_wyze(stations)
    rows = make_rows(devices, stations, arp_by_mac())
    os.umask(0o077)
    with args.output.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Saved {len(rows)} devices to {args.output}")
    if collected:
        print(f"Nest snapshot: {collected}")
    print(f"Rows needing review: {sum(bool(row['review']) for row in rows)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        print(f"Wyze IP map: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    except Exception as error:
        # Do not print SDK exception bodies: some may include request details.
        print(
            f"Wyze sign-in or inventory failed ({type(error).__name__}).",
            file=sys.stderr,
        )
        raise SystemExit(1) from None

#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["gpsoauth==2.0.0", "requests"]
# ///
"""Match Wyze CSV labels to live Google Wifi MACs and rename clients.

Use a private Wyze CSV exported by wyze_ip_map.py and an existing local
monitor.py for its macOS Keychain authentication. Preview is the default.
No credentials or private inventory belong in this public repository.

    uv run wyze_nest_names.py --csv /private/wyze_ip_map.csv \
        --monitor /private/monitor.py
    uv run wyze_nest_names.py --csv /private/wyze_ip_map.csv \
        --monitor /private/monitor.py --apply --audit /private/nest_renames.csv
"""

import argparse
import csv
import importlib.util
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote


def physical_mac(value):
    value = (value or "").rsplit("_", 1)[-1].split("-", 1)[0]
    mac = re.sub(r"[^0-9A-Fa-f]", "", value).upper()
    return mac if len(mac) == 12 else ""


def load_monitor(path):
    spec = importlib.util.spec_from_file_location("local_nest_monitor", path)
    if not spec or not spec.loader:
        raise ValueError("Cannot load monitor.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def api_write(wifi, method, path, body):
    # A write is sent once. An uncertain result is never automatically replayed.
    try:
        response = wifi.session.request(
            method, wifi_module.CLOUD + path, json=body,
            timeout=(10, 20), allow_redirects=False,
            headers={"Authorization": "Bearer " + wifi.token},
        )
    except Exception as exc:
        raise RuntimeError("Google API transport error; request may have arrived") from exc
    if response.status_code != 200:
        raise RuntimeError(f"Google API HTTP {response.status_code}; request not retried")
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError("Google API returned invalid JSON; request not retried") from exc


def station_macs(wifi, group, ids):
    if not ids:
        return {}
    created = api_write(
        wifi, "POST",
        f"groups/{quote(group, safe='')}/stations/operations/sensitiveInfo?prettyPrint=false",
        {"stationIds": ids},
    )
    operation = (created.get("operation") or {}).get("operationId")
    if not operation:
        raise RuntimeError("Google did not return a MAC lookup operation")
    path = "operations/" + quote(operation, safe="")
    for _ in range(10):
        progress = wifi.get(path + "?prettyPrint=false")
        if progress.get("operationState") == "DONE":
            break
        time.sleep(1)
    else:
        raise RuntimeError("Google MAC lookup did not complete")
    records = wifi.get(path + "/sensitiveInfo?prettyPrint=false").get("stationSensitiveInfos")
    if not isinstance(records, list):
        raise RuntimeError("Google MAC lookup returned no records")
    wanted = set(ids)
    result = {}
    for row in records:
        if isinstance(row, dict) and row.get("stationId") in wanted:
            mac = physical_mac(row.get("macAddress", ""))
            if mac:
                result[row["stationId"]] = mac
    return result


def read_wyze(path):
    by_mac = {}
    with path.open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            mac = physical_mac(row["wyze_mac"])
            if mac:
                by_mac.setdefault(mac, []).append(row)
    return by_mac


def target_name(rows):
    # Channel IDs share a physical plug. Its two labels cannot each name the
    # same Nest client. A generic parent label is not a manual room label.
    parent = [r for r in rows if "-" not in r["wyze_mac"]]
    if len(parent) != 1:
        return None
    label = parent[0]["wyze_name"].strip()
    if not label or label == "Wyze Plug Outdoor":
        return None
    return "Wyze " + label


def generic_name(name):
    return name == "Wyze" or bool(re.match(r"^(WYZE_|HL_BC-)", name or ""))


def current_stations(wifi, group):
    return wifi.get(
        f"groups/{quote(group, safe='')}/stations?prettyPrint=false"
    ).get("stations", [])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--monitor", required=True, type=Path)
    parser.add_argument("--apply", action="store_true", help="Apply verified names")
    parser.add_argument("--audit", type=Path, help="Private CSV audit for --apply")
    args = parser.parse_args()
    if args.apply and args.audit is None:
        parser.error("--apply requires --audit")
    if args.audit and args.audit.exists():
        parser.error("Audit path already exists; choose a new path")
    if args.audit and not args.audit.parent.is_dir():
        parser.error("Audit parent does not exist")

    global wifi_module
    wifi_module = load_monitor(args.monitor)
    names = read_wyze(args.csv)
    wifi = wifi_module.GoogleWifi(wifi_module.keychain())
    try:
        groups = wifi.get("groups?prettyPrint=false").get("groups", [])
        if len(groups) != 1:
            raise RuntimeError("Expected exactly one Google Wifi network")
        group = groups[0]["id"]
        stations = current_stations(wifi, group)
        ids = [s["id"] for s in stations if s.get("connected") is True]
        macs = station_macs(wifi, group, ids)
        plan = []
        for station in stations:
            if station["id"] not in macs or station.get("connected") is not True:
                continue
            rows = names.get(macs[station["id"]], [])
            new_name = target_name(rows)
            if not new_name:
                continue
            old_name = station.get("friendlyName") or ""
            if old_name == new_name:
                continue
            if not generic_name(old_name):
                print(f"SKIP custom Nest name: {old_name}")
                continue
            plan.append((station["id"], macs[station["id"]], old_name, new_name,
                         station.get("ipAddress") or ""))
        for _, _, old, new, ip in plan:
            print(f"PLAN {ip}: {old} -> {new}")
        print(f"Eligible clients: {len(plan)}")
        if not args.apply:
            return

        os.umask(0o077)
        with args.audit.open("x", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["time_utc", "ip_before", "old_name", "requested_name",
                             "status", "ip_after"])
            for station_id, mac, old, new, before_ip in plan:
                # Refresh both station data and sensitive MAC immediately before
                # each name mutation. Stop if identity/name/IP changed.
                latest = [s for s in current_stations(wifi, group)
                          if s.get("id") == station_id]
                if len(latest) != 1 or latest[0].get("connected") is not True:
                    print(f"SKIP disconnected: {new}")
                    continue
                current = latest[0]
                fresh_mac = station_macs(wifi, group, [station_id]).get(station_id)
                if (fresh_mac != mac or current.get("friendlyName") != old
                        or current.get("ipAddress") != before_ip):
                    print(f"SKIP identity changed: {new}")
                    continue
                path = (f"groups/{quote(group, safe='')}/stations/"
                        f"{quote(station_id, safe='')}/stationName?prettyPrint=false")
                when = datetime.now(timezone.utc).isoformat()
                try:
                    api_write(wifi, "PUT", path, {"name": new})
                    observed = [s for s in current_stations(wifi, group)
                                if s.get("id") == station_id]
                    status = ("verified" if len(observed) == 1
                              and observed[0].get("friendlyName") == new
                              else "sent; readback unconfirmed")
                    after_ip = observed[0].get("ipAddress", "") if observed else ""
                except Exception as exc:
                    status, after_ip = str(exc), ""
                writer.writerow([when, before_ip, old, new, status, after_ip])
                stream.flush()
                print(f"{status}: {new}")
                if status != "verified":
                    raise RuntimeError("Stopped after unverified rename")
    finally:
        wifi.session.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Nest rename stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from None

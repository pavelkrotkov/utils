#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["wyze-sdk==2.3.9", "gpsoauth==2.0.0", "requests"]
# ///
"""Restart supported Wyze cameras one at a time and audit IP observations.

Uses an existing local Google Wifi monitor.py for Keychain authentication.
Wyze credentials are prompted in Terminal and never saved. The private audit
must be placed outside this public repository.

    uv run wyze_camera_restart.py --csv /private/wyze_ip_map.csv \
        --monitor /private/monitor.py --audit /private/camera_restarts.csv
"""

import argparse
import csv
import getpass
import importlib.util
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from wyze_sdk import Client


# The generic SDK action is known, and Wyze documents app-based camera
# restart. Outdoor cams and doorbells have different restart behavior.
MODELS = {"WYZE_CAKP2JFUS", "HL_BC"}
FIELDS = ("time_utc", "wyze_name", "model", "mac", "wyze_ip_before",
          "nest_ip_before", "restart_result", "wyze_ip_after",
          "nest_ip_after", "wyze_online_after", "nest_connected_after", "offline_observed", "elapsed_seconds")


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


def station_macs(wifi, module, group, ids):
    if not ids:
        return {}
    path = (f"groups/{quote(group, safe='')}/stations/operations/"
            "sensitiveInfo?prettyPrint=false")
    # This POST only requests a MAC read. Do not retry uncertain delivery.
    response = wifi.session.post(
        module.CLOUD + path, json={"stationIds": ids},
        timeout=(10, 20), allow_redirects=False,
        headers={"Authorization": "Bearer " + wifi.token},
    )
    if response.status_code != 200:
        raise RuntimeError(f"Nest MAC lookup HTTP {response.status_code}")
    op = (response.json().get("operation") or {}).get("operationId")
    if not op:
        raise RuntimeError("Nest MAC lookup did not return an operation")
    path = "operations/" + quote(op, safe="")
    for _ in range(10):
        if wifi.get(path + "?prettyPrint=false").get("operationState") == "DONE":
            break
        time.sleep(1)
    else:
        raise RuntimeError("Nest MAC lookup pending")
    rows = wifi.get(path + "/sensitiveInfo?prettyPrint=false").get("stationSensitiveInfos")
    if not isinstance(rows, list):
        raise RuntimeError("Nest MAC lookup returned no rows")
    wanted = set(ids)
    return {r["stationId"]: physical_mac(r.get("macAddress", ""))
            for r in rows if isinstance(r, dict) and r.get("stationId") in wanted}


def nest_stations(wifi, group):
    return wifi.get(f"groups/{quote(group, safe='')}/stations?prettyPrint=false").get("stations", [])


def login():
    email = input("Wyze account email: ").strip()
    key_id = getpass.getpass("Wyze Key ID: ").strip()
    api_key = getpass.getpass("Wyze API key: ").strip()
    password = getpass.getpass("Wyze password: ")
    if not all((email, key_id, api_key, password)):
        raise ValueError("All four Wyze credentials are required")
    auth = Client()
    token = auth.login(email=email, password=password,
                       key_id=key_id, api_key=api_key)["access_token"]
    return Client(token=token)


def by_mac(devices):
    return {physical_mac(d.mac): d for d in devices if physical_mac(d.mac)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--monitor", required=True, type=Path)
    parser.add_argument("--audit", required=True, type=Path)
    args = parser.parse_args()
    if args.audit.exists():
        parser.error("Audit already exists; do not automatically replay restarts")
    if not args.audit.parent.is_dir():
        parser.error("Audit parent does not exist")

    with args.csv.open(encoding="utf-8", newline="") as stream:
        inventory = list(csv.DictReader(stream))
    targets = [r for r in inventory
               if r["wyze_type"] == "Camera" and r["wyze_model"] in MODELS]
    print(f"Eligible cameras: {len(targets)}")
    print("Excluded: outdoor cameras, doorbell, plugs, switches, base station.")
    module = load_monitor(args.monitor)
    wifi = module.GoogleWifi(module.keychain(interactive=True))
    try:
        groups = wifi.get("groups?prettyPrint=false").get("groups", [])
        if len(groups) != 1:
            raise RuntimeError("Expected exactly one Nest network")
        group = groups[0]["id"]
        stations = nest_stations(wifi, group)
        macs = station_macs(wifi, module, group, [s["id"] for s in stations])
        station_by_mac = {mac: s for s in stations
                          if (mac := macs.get(s["id"]))}
        client = login()
        live = by_mac(client.devices_list())
        os.umask(0o077)
        with args.audit.open("x", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS)
            writer.writeheader()
            stream.flush()
            for row in targets:
                mac = physical_mac(row["wyze_mac"])
                device = live.get(mac)
                if not device or device.type != "Camera" or device.product.model != row["wyze_model"]:
                    print(f"SKIP inventory mismatch: {row['wyze_name']}")
                    continue
                if device.is_online is not True:
                    print(f"SKIP Wyze offline: {row['wyze_name']}")
                    continue
                before = station_by_mac.get(mac, {})
                started = time.monotonic()
                record = dict(
                    time_utc=datetime.now(timezone.utc).isoformat(),
                    wyze_name=device.nickname, model=device.product.model, mac=mac,
                    wyze_ip_before=getattr(device, "ip", "") or "",
                    nest_ip_before=before.get("ipAddress") or "",
                    restart_result="", wyze_ip_after="", nest_ip_after="",
                    wyze_online_after="", nest_connected_after="", offline_observed=False, elapsed_seconds="",
                )
                print(f"RESTART {device.nickname} | Nest {record['nest_ip_before']} | "
                      f"Wyze {record['wyze_ip_before']}", flush=True)
                try:
                    client.cameras.restart(device_mac=device.mac,
                                           device_model=device.product.model)
                    record["restart_result"] = "accepted"
                except Exception as exc:
                    record["restart_result"] = f"error: {type(exc).__name__}"
                    writer.writerow(record)
                    stream.flush()
                    raise RuntimeError("Restart failed or delivery uncertain; stopped") from None

                # Give the camera time to reboot. Keep reading without issuing
                # another restart. The API may miss a brief offline interval.
                time.sleep(20)
                deadline = time.monotonic() + 160
                while True:
                    try:
                        live = by_mac(client.devices_list())
                        current = live.get(mac)
                        now_stations = nest_stations(wifi, group)
                        observed = next((s for s in now_stations
                                         if s.get("id") == before.get("id")), None)
                    except Exception:
                        current, observed = None, None
                    if (current and current.is_online is False) or (
                        observed and observed.get("connected") is False
                    ):
                        record["offline_observed"] = True
                    record["wyze_ip_after"] = getattr(current, "ip", "") or "" if current else ""
                    record["nest_ip_after"] = (observed or {}).get("ipAddress") or ""
                    record["wyze_online_after"] = getattr(current, "is_online", "") if current else ""
                    record["nest_connected_after"] = (observed or {}).get("connected", "")
                    if current and current.is_online is True and (
                        not before or (observed and observed.get("connected") is True)
                    ):
                        record["restart_result"] = (
                            "accepted; reconnect observed" if record["offline_observed"]
                            else "accepted; online after wait"
                        )
                        break
                    if time.monotonic() >= deadline:
                        record["restart_result"] = "accepted; reconnect unverified"
                        break
                    time.sleep(10)
                record["elapsed_seconds"] = round(time.monotonic() - started)
                writer.writerow(record)
                stream.flush()
                print(f"RESULT {device.nickname} | {record['restart_result']} | "
                      f"Nest {record['nest_ip_before']} -> {record['nest_ip_after']} | "
                      f"Wyze {record['wyze_ip_before']} -> {record['wyze_ip_after']}",
                      flush=True)
                if record["restart_result"] == "accepted; reconnect unverified":
                    raise RuntimeError("Stopped because reconnect was not verified")
                time.sleep(5)
    finally:
        wifi.session.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Camera restart stopped: {type(exc).__name__}: "
              "check the audit and device state before another attempt",
              file=sys.stderr)
        raise SystemExit(1) from None

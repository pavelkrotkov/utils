"""Small local probes from the Mac running the monitor."""

from __future__ import annotations

import ipaddress
import re
import subprocess

import requests

from nest_wifi_redact import redact
from nest_wifi_snapshot import now


def run(command: list[str], timeout: int = 15) -> tuple[int, str]:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"},
        )
        return result.returncode, result.stdout
    except (OSError, subprocess.TimeoutExpired):
        return -1, ""


def parse_ping(output: str) -> dict:
    loss = re.search(r"([\d.]+)% packet loss", output)
    rtt = re.search(
        r"(?:round-trip|rtt).*?= ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+) ms",
        output,
    )
    result = {"loss_percent": float(loss[1]) if loss else None}
    if rtt:
        result.update(
            zip(
                ("min_ms", "avg_ms", "max_ms", "stddev_ms"),
                map(float, rtt.groups()),
                strict=True,
            )
        )
    return result


def _local_router_status(gateway: str) -> dict:
    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(
                f"http://{gateway}/api/v1/status",
                timeout=(3, 5),
                allow_redirects=False,
            )
            result = {"http_status": response.status_code}
            if response.status_code == 200:
                result["payload"] = redact(response.json())
            return result
    except (requests.RequestException, ValueError):
        return {"error": "Local status endpoint unavailable."}


def _dns_probe() -> dict:
    code, output = run(["/usr/bin/dig", "+time=3", "+tries=1", "www.google.com", "A"], 8)
    query = re.search(r"Query time: (\d+) msec", output)
    status = re.search(r"status: ([A-Z]+)", output)
    return {
        "hostname": "www.google.com",
        "exit_code": code,
        "status": status[1] if status else None,
        "query_ms": int(query[1]) if query else None,
        "note": "Configured resolver; response may be cached.",
    }


def snapshot() -> dict:
    """Measure only this Mac's route, latency, resolver and local router view."""
    _, route = run(["/sbin/route", "-n", "get", "default"])
    gateway = re.search(r"gateway:\s+([\d.]+)", route)
    interface = re.search(r"interface:\s+(\S+)", route)
    address = gateway[1] if gateway else None
    result = {
        "observed_at": now(),
        "perspective": "this_Mac_only",
        "gateway": address,
        "interface": interface[1] if interface else None,
        "pings": {},
    }
    targets = {"internet_cloudflare": "1.1.1.1"}
    # A successful local probe says nothing about coverage in another room.
    if address:
        targets["gateway"] = address
    for name, target in targets.items():
        code, output = run(["/sbin/ping", "-n", "-c", "5", "-i", "0.2", "-W", "1000", target])
        result["pings"][name] = {"target": target, "exit_code": code, **parse_ping(output)}
    result["dns"] = _dns_probe()
    if address and ipaddress.ip_address(address).is_private:
        result["google_local_status"] = _local_router_status(address)
    return result

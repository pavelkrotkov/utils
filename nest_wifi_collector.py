"""Write private local/cloud observations to a caller-selected directory."""

from __future__ import annotations

import json
from pathlib import Path

from nest_wifi_api import GoogleWifi
from nest_wifi_keychain import MonitorError, keychain
from nest_wifi_local import snapshot as local_snapshot
from nest_wifi_redact import redact
from nest_wifi_snapshot import now


def prepare_data_dir(data_dir: Path) -> None:
    """Keep runtime state and private observations out of the source checkout."""
    for directory in (data_dir, data_dir / "logs", data_dir / "runtime"):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)


def collect(data_dir: Path, *, local_only: bool = False) -> str:
    """Collect one sample and append it, even if Google access is unavailable."""
    record = {"schema_version": 1, "collected_at": now(), "local": local_snapshot()}
    if local_only:
        cloud = {"state": "not_requested"}
    else:
        try:
            networks = GoogleWifi(keychain()).snapshot()
            partial = any(
                "error" in item for network in networks for item in network["observations"].values()
            )
            cloud = {"state": "partial" if partial else "ok", "networks": networks}
        except MonitorError as error:
            cloud = {"state": "unavailable", "error": str(error)}
    record["google_cloud"] = cloud
    record["finished_at"] = now()
    # Retain source payload timestamps; collection time is not a speed-test time.
    path = data_dir / "logs" / f"{record['collected_at'][:10]}.jsonl"
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(redact(record), separators=(",", ":")) + "\n")
    print(
        json.dumps(
            {
                "collected_at": record["collected_at"],
                "google_cloud": cloud["state"],
                "local": record["local"],
                "log": str(path),
            },
            indent=2,
        )
    )
    return cloud["state"]


def status(data_dir: Path) -> None:
    """Show the last persisted result without contacting the network."""
    files = sorted((data_dir / "logs").glob("*.jsonl"))
    if not files:
        print("No observations collected yet.")
        return
    latest = json.loads(files[-1].read_text(encoding="utf-8").splitlines()[-1])
    cloud = latest["google_cloud"]
    print(
        json.dumps(
            {
                "collected_at": latest["collected_at"],
                "google_cloud": cloud["state"],
                "error": cloud.get("error"),
                "networks": len(cloud.get("networks", [])),
                "local": latest["local"],
            },
            indent=2,
        )
    )

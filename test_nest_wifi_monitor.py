"""Offline checks for the Nest Wifi monitor and its explicit quality limits."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from radon.complexity import cc_visit
from radon.metrics import mi_visit

import nest_wifi_collector as collector
import nest_wifi_daemon as daemon
from nest_wifi_auth import normalize_oauth_cookie
from nest_wifi_keychain import MonitorError
from nest_wifi_local import parse_ping
from nest_wifi_redact import redact
from nest_wifi_snapshot import snapshot_networks


def test_cookie_validation_and_redaction():
    assert normalize_oauth_cookie('oauth_token="oauth2_4/example"; Path=/') == "oauth2_4/example"
    with pytest.raises(MonitorError, match="Expected") as error:
        normalize_oauth_cookie("private-value")
    assert "private-value" not in str(error.value)
    payload = {
        "wifiPassword": "private-value",
        "nested": [{"oauth_token": "private-value"}],
        "note": "Bearer private-value",
        "timestamp": "2026-10-21T01:00:00Z",
    }
    result = redact(payload)
    assert "private-value" not in json.dumps(result)
    assert result["timestamp"] == payload["timestamp"]


def test_cloud_snapshot_keeps_telemetry_without_group_settings():
    paths = []
    session = SimpleNamespace(close=Mock())

    def get(path):
        paths.append(path)
        if path.startswith("groups?"):
            return {
                "groups": [
                    {
                        "id": "network/id",
                        "name": "Example",
                        "groupSettings": {"password": "private"},
                    }
                ]
            }
        if "/realtimeMetrics?" in path:
            raise MonitorError("Google API HTTP 404")
        return {"timestamp": "2026-10-21T01:00:00Z", "password": "private"}

    result = snapshot_networks(SimpleNamespace(get=get, session=session))
    assert len(paths) == 8
    assert all(path.startswith(("groups?", "groups/network%2Fid/")) for path in paths)
    assert session.close.called
    assert "private" not in json.dumps(result)
    assert "groupSettings" not in result[0]
    assert result[0]["observations"]["realtimeMetrics"]["error"] == "Google API HTTP 404"
    assert result[0]["observations"]["status"]["payload"]["timestamp"] == "2026-10-21T01:00:00Z"


def test_local_ping_parser_and_private_log(tmp_path, monkeypatch, capsys):
    ping = parse_ping(
        "5 packets transmitted, 4 received, 20.0% packet loss\n"
        "round-trip min/avg/max/stddev = 1.0/2.0/3.0/0.5 ms\n"
    )
    assert ping == {
        "loss_percent": 20.0,
        "min_ms": 1.0,
        "avg_ms": 2.0,
        "max_ms": 3.0,
        "stddev_ms": 0.5,
    }
    collector.prepare_data_dir(tmp_path)
    monkeypatch.setattr(collector, "local_snapshot", lambda: {"gateway": "192.0.2.1"})
    monkeypatch.setattr(collector, "keychain", lambda: {})
    monkeypatch.setattr(
        collector,
        "GoogleWifi",
        lambda _: SimpleNamespace(snapshot=lambda: [{"observations": {}}]),
    )
    assert collector.collect(tmp_path) == "ok"
    record = json.loads(next((tmp_path / "logs").glob("*.jsonl")).read_text().strip())
    assert record["google_cloud"]["state"] == "ok"
    assert record["local"]["gateway"] == "192.0.2.1"
    assert "google_cloud" in capsys.readouterr().out


def test_detached_process_reopens_cli(tmp_path, monkeypatch):
    (tmp_path / "runtime").mkdir()
    monkeypatch.setattr(daemon, "_watcher_pid", lambda _: None)
    popen = Mock(return_value=SimpleNamespace(pid=123))
    monkeypatch.setattr(daemon.subprocess, "Popen", popen)
    daemon.start(tmp_path, False)
    command = popen.call_args.args[0]
    assert Path(command[2]).name == "nest_wifi_monitor.py"
    assert command[-3:] == ["--data-dir", str(tmp_path), "watch"]


def test_maintainability_and_complexity():
    for path in Path(__file__).parent.glob("nest_wifi_*.py"):
        source = path.read_text(encoding="utf-8")
        assert mi_visit(source, multi=True) > 50, path.name
        for block in cc_visit(source):
            assert block.complexity <= 8, f"{path.name}:{block.name}"
            for method in getattr(block, "methods", ()):
                assert method.complexity <= 8, f"{path.name}:{method.name}"

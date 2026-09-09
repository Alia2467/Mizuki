"""Isolated HTTP fixtures; importing server never reads real configuration."""
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_desktop = Path(__file__).resolve().parent.parent / "Mizuki" / "desktop"
if str(_desktop) not in sys.path:
    sys.path.insert(0, str(_desktop))


@pytest.fixture()
def _isolate_config(tmp_path, monkeypatch):
    import server
    from collector import ComputerCollector
    from database import DataCollector
    from collections import OrderedDict

    monkeypatch.delenv("MIZUKI_TOKEN", raising=False)
    monkeypatch.setattr(server, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(server, "app_dir", lambda: tmp_path)
    monkeypatch.setattr(server, "config", dict(server.DEFAULT_CONFIG))
    monkeypatch.setattr(server, "collector", ComputerCollector(interval=0.3))
    monkeypatch.setattr(server, "_latest_phone", {})
    monkeypatch.setattr(server, "_phone_received_at", None)
    monkeypatch.setattr(server, "_phone_received_monotonic", None)
    monkeypatch.setattr(server, "_phone_orders", OrderedDict())
    monkeypatch.setattr(server, "_plugin_heartbeats", {})
    monkeypatch.setattr(server, "_rate_windows", OrderedDict())
    monkeypatch.setattr(server, "_last_computer_key", None)
    server.CONFIG_PATH.write_text(json.dumps(server.DEFAULT_CONFIG), encoding="utf-8")
    database = DataCollector(db_file=tmp_path / "data" / "collected.db")
    monkeypatch.setattr(server, "storage", database)
    assert database.start()
    yield
    server.collector.stop()
    assert database.stop()


@pytest.fixture()
def client(_isolate_config):
    import server
    with TestClient(server.app) as test_client:
        yield test_client

"""Regression tests for cross-end ordering, authentication and configuration."""
from copy import deepcopy
from datetime import datetime
import json
import time
from types import SimpleNamespace

import pytest

from test.test_server import PHONE_PAYLOAD


def sample(heart_rate=72, timestamp=None):
    payload = deepcopy(PHONE_PAYLOAD)
    payload["timestamp"] = timestamp or datetime.now().isoformat(timespec="seconds")
    payload["health"]["heart_rate"] = heart_rate
    return payload


def test_old_replay_is_stored_without_replacing_live_state(client):
    import server
    fresh = sample(72)
    old = sample(140, "2026-09-09T01:00:00")
    old["usage"]["is_navigating"] = True
    assert client.post("/phone-data", json=fresh).status_code == 200
    response = client.post("/phone-data", json=old, headers={"X-Sensor-Replay": "true"})
    assert response.status_code == 200
    assert response.json()["current"] is False
    current = client.get("/merged-data").json()
    assert current["phone"]["health"]["heart_rate"] == 72
    assert current["phone"]["usage"]["is_navigating"] is False
    assert server.storage.flush()
    assert len(client.get("/api/logs").json()) == 2


def test_replay_alone_does_not_make_phone_online(client):
    client.post("/phone-data", json=sample(), headers={"X-Sensor-Replay": "true"})
    data = client.get("/merged-data").json()
    assert data["phone_connected"] is False
    assert data["phone"] == {}


def test_sequence_handles_same_second_out_of_order_and_clock_rollback(client):
    def post(sequence, rate, timestamp):
        return client.post("/phone-data", json=sample(rate, timestamp),
                           headers={"X-Sensor-Session": "session-a", "X-Sensor-Sequence": str(sequence)})
    post(2, 72, "2026-09-09T14:00:00")
    assert post(1, 140, "2026-09-09T14:00:00").json()["current"] is False
    assert client.get("/merged-data").json()["phone"]["health"]["heart_rate"] == 72
    assert post(3, 80, "2026-09-09T13:00:00").json()["current"] is True
    assert client.get("/merged-data").json()["phone"]["health"]["heart_rate"] == 80
    assert post(3, 160, "2026-09-09T15:00:00").json()["current"] is False


def test_retired_session_and_legacy_posts_cannot_replace_new_session(client):
    def post(session, sequence, rate):
        return client.post("/phone-data", json=sample(rate), headers={"X-Sensor-Session": session, "X-Sensor-Sequence": str(sequence)})
    post("old", 100, 120)
    assert post("new", 1, 72).json()["current"] is True
    assert post("old", 101, 180).json()["current"] is False
    assert client.post("/phone-data", json=sample(150)).json()["current"] is False
    assert client.get("/merged-data").json()["phone"]["health"]["heart_rate"] == 72


def test_legacy_older_sample_does_not_replace_newer_sample(client):
    client.post("/phone-data", json=sample(72, "2026-09-09T14:10:00"))
    assert client.post("/phone-data", json=sample(150, "2026-09-09T13:00:00")).json()["current"] is False
    assert client.get("/merged-data").json()["phone"]["health"]["heart_rate"] == 72


@pytest.mark.parametrize("headers", [
    {"X-Sensor-Sequence": "1"}, {"X-Sensor-Session": "a"},
    {"X-Sensor-Session": "a", "X-Sensor-Sequence": "0"},
    {"X-Sensor-Session": "a", "X-Sensor-Sequence": "no"},
    {"X-Sensor-Session": "a", "X-Sensor-Sequence": "1", "X-Sensor-Replay": "true"},
    {"X-Sensor-Replay": "maybe"},
])
def test_invalid_order_headers_are_rejected(client, headers):
    assert client.post("/phone-data", json=sample(), headers=headers).status_code == 422


def test_order_headers_do_not_change_phone_json_contract(client):
    payload = sample()
    payload["diagnostics"]["future_value"] = {"x": 7}
    client.post("/phone-data", json=payload, headers={"X-Sensor-Session": "a", "X-Sensor-Sequence": "1"})
    received = client.get("/merged-data").json()["phone"]
    assert received.pop("received_at")
    assert received == payload


def test_offline_detection_uses_elapsed_time_not_wall_clock(client, monkeypatch):
    import server
    client.post("/phone-data", json=sample())
    now = time.monotonic()
    monkeypatch.setattr(server, "time", SimpleNamespace(monotonic=lambda: now + 11))
    assert client.get("/health").json()["phone_connected"] is False


def test_environment_token_cannot_be_read_or_disabled_by_config(client, monkeypatch):
    monkeypatch.setenv("MIZUKI_TOKEN", "env-review")
    assert client.patch("/api/config", json={"shared_token": ""}).status_code == 401
    response = client.get("/api/config", headers={"X-Sensor-Token": "env-review"})
    assert response.status_code == 200
    assert response.json()["token_from_env"] is True
    assert "env-review" not in response.text
    assert client.post("/api/plugin-heartbeat", json={"plugin_id": "review"}, headers={"X-Sensor-Token": "env-review"}).status_code == 200


def test_config_token_change_requires_current_token_and_then_new_token(client):
    client.patch("/api/config", json={"shared_token": "first"})
    assert client.patch("/api/config", json={"shared_token": "second"}).status_code == 401
    assert client.patch("/api/config", json={"shared_token": "second"}, headers={"X-Sensor-Token": "first"}).status_code == 200
    assert client.get("/api/state", headers={"X-Sensor-Token": "first"}).status_code == 401
    assert client.get("/api/state", headers={"X-Sensor-Token": "second"}).status_code == 200
    assert client.patch("/api/config", json={"shared_token": ""}, headers={"X-Sensor-Token": "second"}).status_code == 200
    assert client.get("/api/state").status_code == 200


@pytest.mark.parametrize("body", [
    {"computer_collect_interval": True}, {"computer_collect_interval": "bad"},
    {"computer_collect_interval": 600001}, {"computer_collect_enabled": "false"},
    {"shared_token": None}, {"port": 1234},
])
def test_invalid_config_does_not_mutate_runtime(client, body):
    before = client.get("/api/config").json()
    assert client.patch("/api/config", json=body).status_code == 422
    assert client.get("/api/config").json() == before


def test_failed_config_write_keeps_runtime_and_disk_config(client, monkeypatch):
    import server
    before = server.CONFIG_PATH.read_text(encoding="utf-8")
    def fail(candidate):
        raise OSError("simulated")
    monkeypatch.setattr(server, "_write_config", fail)
    assert client.patch("/api/config", json={"poll_interval": 9000}).status_code == 503
    assert client.get("/api/config").json()["poll_interval"] == 5000
    assert server.CONFIG_PATH.read_text(encoding="utf-8") == before


def test_invalid_config_file_is_preserved(_isolate_config):
    import server
    server.CONFIG_PATH.write_text('{"computer_collect_interval": -1, broken', encoding="utf-8")
    assert server.load_config() == server.DEFAULT_CONFIG
    assert "broken" in server.CONFIG_PATH.read_text(encoding="utf-8")


def test_subsecond_collector_interval_and_disable_cached_foreground():
    from collector import ComputerCollector
    collector = ComputerCollector(interval=0.3)
    assert collector.interval == 0.3
    collector._data = {"foreground_window": "private", "foreground_process": "x", "is_gaming": True, "cpu_percent": 5}
    collector.is_collecting_foreground = False
    assert collector.get() == {"cpu_percent": 5}


def test_services_can_stop_and_restart_without_duplicate_threads(_isolate_config, monkeypatch):
    import server
    monkeypatch.setattr(server.collector, "snapshot", lambda: {"timestamp": datetime.now().isoformat(timespec="seconds"), "cpu_percent": 1})
    for _ in range(2):
        server.start_services()
        first = list(server._service_threads)
        server.start_services()
        assert server._service_threads == first
        server.stop_services()
        assert all(not thread.is_alive() for thread in first)
        assert not server.collector.is_alive()
        assert not server.storage.status()["alive"]


def test_cross_origin_configuration_write_is_rejected(client):
    assert client.patch("/api/config", json={"shared_token": "attacker"}, headers={"Origin": "https://elsewhere.invalid"}).status_code == 403


def test_authenticated_export_handles_mixed_types_and_cleans_output(client, tmp_path):
    import server
    client.post("/phone-data", json=sample())
    server.storage.record({"type": "computer", "timestamp": datetime.now().isoformat(timespec="seconds"), "foreground_window": "test-window"})
    assert server.storage.flush()
    client.patch("/api/config", json={"shared_token": "export-key"})
    response = client.get("/api/export/csv", headers={"X-Sensor-Token": "export-key"})
    assert response.status_code == 200
    assert "health.heart_rate" in response.text
    assert "foreground_window" in response.text
    assert not list((tmp_path / "data" / "exports").glob("*"))


def test_minimum_sampling_rate_is_within_rate_limit(client):
    import server
    assert all(not server._is_rate_limited("minimum-rate", "/phone-data") for _ in range(660))
    assert all(not server._is_rate_limited("minimum-rate", "/merged-data") for _ in range(600))


def test_failed_persistence_returns_retryable_status_but_keeps_live_state(client, monkeypatch):
    import server
    monkeypatch.setattr(server.storage, "record", lambda record: False)
    response = client.post("/phone-data", json=sample(75))
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert client.get("/merged-data").json()["phone"]["health"]["heart_rate"] == 75


@pytest.mark.parametrize("fragment", ['"location":{"latitude":NaN}', '"extra":Infinity'])
def test_non_finite_json_is_rejected_without_serialization_failure(client, fragment):
    body = '{"device_id":"finite-test","timestamp":"2026-09-09T12:00:00",' + fragment + '}'
    assert client.post("/phone-data", content=body, headers={"Content-Type": "application/json"}).status_code == 422


def test_phone_metadata_cannot_override_persistence_type(client):
    import server
    payload = sample()
    payload["type"] = "computer"
    assert client.post("/phone-data", json=payload).status_code == 200
    assert server.storage.flush()
    rows = client.get("/api/logs").json()
    assert rows[0]["type"] == "phone"


def test_heartbeat_online_then_offline_uses_monotonic_timeout(client, monkeypatch):
    import server
    assert client.post("/api/plugin-heartbeat", json={"plugin_id": "runtime-test"}).status_code == 200
    assert client.get("/api/plugin-status").json()["plugins"][0]["online"] is True
    future = time.monotonic() + 11
    monkeypatch.setattr(server, "time", SimpleNamespace(monotonic=lambda: future))
    assert client.get("/api/plugin-status").json()["plugins"][0]["online"] is False


def test_auth_validation_errors_do_not_echo_secret_inputs(client):
    secret = "x" * 4097
    response = client.patch("/api/config", json={"shared_token": secret})
    assert response.status_code == 422
    assert secret not in response.text

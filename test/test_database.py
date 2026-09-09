"""SQLite persistence behavior: requests enqueue, reads and exports see committed data."""
import csv
import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from database import DataCollector


def test_record_does_not_wait_for_sqlite_write_lock(tmp_path):
    database = DataCollector(db_file=tmp_path / "records.db")
    database.start()
    blocker = sqlite3.connect(str(database.db_file), check_same_thread=False)
    blocker.execute("BEGIN IMMEDIATE")
    release = threading.Timer(0.4, blocker.rollback)
    release.start()
    try:
        started = time.monotonic()
        database.record({"type": "phone", "timestamp": "2026-09-09T12:00:00"})
        elapsed = time.monotonic() - started
        assert elapsed < 0.15, "record() must enqueue rather than wait for the database lock"
    finally:
        release.join()
        blocker.close()
        database.stop()


@pytest.fixture()
def database(tmp_path):
    database = DataCollector(db_file=tmp_path / "records.db")
    assert database.start()
    yield database
    assert database.stop()


def record(value=1, kind="phone"):
    from datetime import datetime
    return {"type": kind, "timestamp": datetime.now().isoformat(timespec="seconds"), "value": value}


def test_flush_persists_owned_copy(database):
    payload = record()
    payload["health"] = {"steps": 12}
    assert database.record(payload)
    payload["health"]["steps"] = 99
    assert database.flush()
    saved = json.loads(database.query()[0]["data"])
    assert saved["health"]["steps"] == 12
    assert database.status()["last_write"]


def test_stop_drains_all_accepted_records_and_allows_restart(database):
    for value in range(100):
        assert database.record(record(value))
    assert database.stop()
    assert database.record(record()) is False
    assert database.get_stats()["total"] == 100
    assert database.start()
    assert database.record(record(101))
    assert database.flush()
    assert database.get_stats()["total"] == 101


def test_mixed_csv_has_union_of_columns_and_safe_text(database, tmp_path):
    phone = record()
    phone["health"] = {"steps": 12}
    computer = record(kind="computer")
    computer["foreground_window"] = "=review()"
    assert database.record(phone)
    assert database.record(computer)
    assert database.flush()
    path = tmp_path / "mixed.csv"
    assert database.export_csv(path) == 2
    with path.open(encoding="utf-8-sig", newline="") as output:
        rows = list(csv.DictReader(output))
    assert set(rows[0]) >= {"type", "timestamp", "health.steps", "foreground_window"}
    assert rows[0]["foreground_window"] == "'=review()"
    assert rows[1]["health.steps"] == "12"


def test_empty_exports_create_valid_files(database, tmp_path):
    json_path, csv_path = tmp_path / "empty.json", tmp_path / "empty.csv"
    assert database.export_json(json_path) == 0
    assert json.loads(json_path.read_text(encoding="utf-8")) == []
    assert database.export_csv(csv_path) == 0
    assert csv_path.read_text(encoding="utf-8-sig").strip() == "type,timestamp"


def test_exports_are_not_truncated_at_ten_thousand(tmp_path):
    database = DataCollector(db_file=tmp_path / "large.db", queue_size=11000, batch_size=256)
    assert database.start()
    try:
        for value in range(10005):
            assert database.record(record(value))
        assert database.flush(timeout=10)
        path = tmp_path / "all.json"
        assert database.export_json(path) == 10005
        assert len(json.loads(path.read_text(encoding="utf-8"))) == 10005
        assert database.export_csv(tmp_path / "all.csv") == 10005
    finally:
        assert database.stop()


def test_queue_capacity_and_shutdown_deadline_are_bounded(tmp_path):
    database = DataCollector(db_file=tmp_path / "bounded.db", queue_size=1, batch_size=1)
    assert database.start()
    blocker = sqlite3.connect(str(database.db_file))
    blocker.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        accepted = sum(database.record(record(value)) for value in range(30))
        assert 1 <= accepted <= 2
        assert time.monotonic() - started < 0.2
        assert database.status()["dropped"] >= 28
        assert database.stop(timeout=0.3)
        assert not database.status()["alive"]
    finally:
        blocker.rollback()
        blocker.close()
        database.stop()


def test_reads_remain_available_during_write_contention(database):
    assert database.record(record())
    assert database.flush()
    blocker = sqlite3.connect(str(database.db_file))
    blocker.execute("BEGIN IMMEDIATE")
    try:
        assert database.record(record(2))
        started = time.monotonic()
        assert len(database.query()) == 1
        assert time.monotonic() - started < 0.2
    finally:
        blocker.rollback()
        blocker.close()
    assert database.flush()
    assert database.get_stats()["total"] == 2


def test_writer_recovers_after_transient_start_failure(tmp_path, monkeypatch):
    database = DataCollector(db_file=tmp_path / "recovery.db")
    original = database._connect_writer
    attempts = []
    def connect():
        attempts.append(1)
        if len(attempts) == 1:
            raise sqlite3.OperationalError("temporary")
        return original()
    monkeypatch.setattr(database, "_connect_writer", connect)
    try:
        assert database.start()
        assert database.record(record())
        assert database.flush()
        assert database.get_stats()["total"] == 1
        assert database.status()["last_error"] == ""
    finally:
        assert database.stop()


def test_retention_cleans_old_data_on_start(tmp_path):
    from datetime import datetime, timedelta
    path = tmp_path / "retention.db"
    with sqlite3.connect(str(path)) as conn:
        conn.execute("CREATE TABLE records(id INTEGER PRIMARY KEY, type TEXT, timestamp TEXT, data TEXT)")
        old = (datetime.now() - timedelta(days=40)).isoformat(timespec="seconds")
        conn.execute("INSERT INTO records(type,timestamp,data) VALUES (?,?,?)", ("phone", old, "{}"))
    database = DataCollector(db_file=path)
    assert database.start()
    assert database.record(record())
    assert database.stop()
    assert database.get_stats()["total"] == 1

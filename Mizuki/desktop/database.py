"""SQLite persistence: bounded non-blocking intake, one writer, independent readers."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import csv
from datetime import datetime, timedelta
import json
import logging
from pathlib import Path
import queue
import sqlite3
import threading
import time
from typing import Any, Iterator

_logger = logging.getLogger("mizuki.database")
DEFAULT_DB_FILE = Path(__file__).resolve().parent / "data" / "collected.db"
DEFAULT_RETENTION_DAYS = 30
DEFAULT_QUEUE_SIZE = 2048
DEFAULT_BATCH_SIZE = 64
DEFAULT_FLUSH_INTERVAL = 0.05
DEFAULT_STOP_TIMEOUT = 5.0
_EXPORT_BATCH_SIZE = 500


class DataCollector:
    """record() only enqueues; flush()/stop() are explicit non-request operations."""

    def __init__(self, db_file: Path = DEFAULT_DB_FILE, retention_days: int = DEFAULT_RETENTION_DAYS,
                 queue_size: int = DEFAULT_QUEUE_SIZE, batch_size: int = DEFAULT_BATCH_SIZE,
                 flush_interval: float = DEFAULT_FLUSH_INTERVAL) -> None:
        self.db_file = Path(db_file)
        self.retention_days = max(1, int(retention_days))
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=max(1, int(queue_size)))
        self._batch_size = max(1, int(batch_size))
        self._flush_interval = max(0.01, float(flush_interval))
        self._accept_lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._is_accepting = False
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._conn: sqlite3.Connection | None = None  # owned exclusively by the writer
        self._last_record_time: datetime | None = None
        self._last_error = ""
        self._dropped = 0
        self._stop_deadline = float("inf")

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return self._ready.is_set()
        self._stop.clear()
        self._ready.clear()
        self._stop_deadline = float("inf")
        with self._accept_lock:
            self._is_accepting = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="data-collector")
        self._thread.start()
        return self._ready.wait(timeout=3)

    def record(self, record: dict[str, Any]) -> bool:
        """Take ownership of a snapshot without waiting for a writer or disk lock."""
        if not self._accept_lock.acquire(blocking=False):
            return False
        try:
            if not self._is_accepting:
                return False
            try:
                self._queue.put_nowait(deepcopy(record))
                return True
            except queue.Full:
                with self._status_lock:
                    self._dropped += 1
                return False
        finally:
            self._accept_lock.release()

    def flush(self, timeout: float = DEFAULT_STOP_TIMEOUT) -> bool:
        """Wait for earlier accepted records to commit (tests/shutdown, never HTTP)."""
        marker = threading.Event()
        started = time.monotonic()
        with self._accept_lock:
            if not self._is_accepting:
                return False
        try:
            self._queue.put(marker, timeout=max(0, timeout))
        except queue.Full:
            return False
        return marker.wait(timeout=max(0, timeout - (time.monotonic() - started)))

    def stop(self, timeout: float = DEFAULT_STOP_TIMEOUT) -> bool:
        with self._accept_lock:
            self._is_accepting = False
        self._stop_deadline = time.monotonic() + max(0, timeout)
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(0, timeout) + 0.2)
        return self._thread is None or not self._thread.is_alive()

    @property
    def last_record_time(self) -> datetime | None:
        with self._status_lock:
            return self._last_record_time

    def status(self) -> dict[str, Any]:
        with self._status_lock:
            return {
                "connected": self._ready.is_set(),
                "alive": self._thread is not None and self._thread.is_alive(),
                "queued": self._queue.qsize(),
                "dropped": self._dropped,
                "last_error": self._last_error,
                "last_write": self._last_record_time.isoformat(timespec="seconds") if self._last_record_time else None,
            }

    def _connect_writer(self) -> sqlite3.Connection:
        self.db_file.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_file), timeout=0.1)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("CREATE TABLE IF NOT EXISTS records (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                         "type TEXT NOT NULL, timestamp TEXT NOT NULL, data TEXT NOT NULL)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_records_timestamp ON records(timestamp)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_records_type_timestamp ON records(type, timestamp DESC, id DESC)")
            conn.commit()
            return conn
        except Exception:
            conn.close()
            raise

    def _run(self) -> None:
        pending: list[dict[str, Any]] = []
        marker: threading.Event | None = None
        next_cleanup = 0.0
        last_log = float("-inf")
        try:
            while True:
                if self._stop.is_set() and time.monotonic() >= self._stop_deadline:
                    count = len(pending)
                    for _ in pending:
                        self._queue.task_done()
                    if marker is not None:
                        self._queue.task_done()
                    while True:
                        try:
                            item = self._queue.get_nowait()
                            count += isinstance(item, dict)
                            self._queue.task_done()
                        except queue.Empty:
                            break
                    with self._status_lock:
                        self._dropped += count
                    if count:
                        _logger.error("Shutdown deadline exceeded; %d records could not be persisted", count)
                    return
                try:
                    if self._conn is None:
                        self._conn = self._connect_writer()
                        self._ready.set()
                    if not pending and marker is None:
                        try:
                            item = self._queue.get(timeout=self._flush_interval)
                        except queue.Empty:
                            item = None
                        if isinstance(item, threading.Event):
                            marker = item
                        elif item is not None:
                            pending.append(item)
                        # Accumulate a short batch rather than committing every sample.
                        deadline = time.monotonic() + self._flush_interval
                        while pending and len(pending) < self._batch_size and marker is None:
                            try:
                                item = self._queue.get(timeout=max(0, deadline - time.monotonic()))
                            except queue.Empty:
                                break
                            if isinstance(item, threading.Event):
                                marker = item
                            else:
                                pending.append(item)
                    if pending:
                        values = []
                        for record in pending:
                            try:
                                values.append((record.get("type", "unknown"),
                                               record.get("timestamp") or datetime.now().isoformat(timespec="seconds"),
                                               json.dumps(record, ensure_ascii=False, allow_nan=False)))
                            except (TypeError, ValueError):
                                with self._status_lock:
                                    self._dropped += 1
                                _logger.warning("Discarded a non-JSON persistence record")
                        self._conn.executemany("INSERT INTO records(type, timestamp, data) VALUES (?, ?, ?)", values)
                        self._conn.commit()
                        with self._status_lock:
                            if values:
                                self._last_record_time = datetime.now()
                            self._last_error = ""
                        for _ in pending:
                            self._queue.task_done()
                        pending.clear()
                    if marker is not None:
                        marker.set()
                        self._queue.task_done()
                        marker = None
                    if time.monotonic() >= next_cleanup:
                        cutoff = (datetime.now() - timedelta(days=self.retention_days)).isoformat(timespec="seconds")
                        self._conn.execute("DELETE FROM records WHERE timestamp < ?", (cutoff,))
                        self._conn.commit()
                        next_cleanup = time.monotonic() + 86400
                    if self._stop.is_set() and self._queue.empty():
                        return
                except Exception as exc:
                    with self._status_lock:
                        self._last_error = type(exc).__name__
                    if time.monotonic() - last_log >= 10:
                        _logger.error("Persistence temporarily unavailable: %s", type(exc).__name__)
                        last_log = time.monotonic()
                    is_busy = (isinstance(exc, sqlite3.OperationalError) and
                               getattr(exc, "sqlite_errorcode", 0) & 255 in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED))
                    if self._conn is not None:
                        try:
                            self._conn.rollback()
                        except Exception:
                            is_busy = False
                        if not is_busy:
                            try:
                                self._conn.close()
                            except Exception:
                                pass
                            self._conn = None
                    if not is_busy:
                        self._ready.clear()
                    # Retain a healthy connection on contention; do not recreate its schema.
                    delay = min(0.1, max(0, self._stop_deadline - time.monotonic()))
                    if delay:
                        time.sleep(delay)
        finally:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
            self._ready.clear()

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        # Reads run in HTTP worker threads, never on the event loop or writer connection.
        conn = sqlite3.connect(self.db_file.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def query(self, record_type: str | None = None, start_time: str | None = None,
              end_time: str | None = None, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        conditions, params = [], []
        for expression, value in (("type = ?", record_type), ("timestamp >= ?", start_time), ("timestamp <= ?", end_time)):
            if value is not None:
                conditions.append(expression)
                params.append(value)
        where = " AND ".join(conditions) or "1=1"
        params.extend([max(1, min(int(limit), 10000)), max(0, int(offset))])
        try:
            with self._read_connection() as conn:
                return [dict(row) for row in conn.execute(
                    f"SELECT * FROM records WHERE {where} ORDER BY timestamp DESC, id DESC LIMIT ? OFFSET ?", params)]
        except sqlite3.Error:
            return []

    def get_stats(self) -> dict[str, Any]:
        try:
            with self._read_connection() as conn:
                counts = dict(conn.execute("SELECT type, COUNT(*) FROM records GROUP BY type"))
                oldest, newest = conn.execute("SELECT MIN(timestamp), MAX(timestamp) FROM records").fetchone()
            return {"total": sum(counts.values()), "phone": counts.get("phone", 0), "computer": counts.get("computer", 0),
                    "oldest": oldest, "newest": newest, "retention_days": self.retention_days}
        except sqlite3.Error:
            return {}

    @staticmethod
    def _rows(conn: sqlite3.Connection, record_type: str | None) -> Iterator[dict[str, Any]]:
        sql = "SELECT * FROM records"
        args: tuple[Any, ...] = ()
        if record_type is not None:
            sql += " WHERE type = ?"
            args = (record_type,)
        cursor = conn.execute(sql + " ORDER BY timestamp DESC, id DESC", args)
        while True:
            batch = cursor.fetchmany(_EXPORT_BATCH_SIZE)
            if not batch:
                return
            for row in batch:
                yield dict(row)

    def export_json(self, output_path: Path, record_type: str | None = None) -> int:
        count = 0
        with self._read_connection() as conn, output_path.open("w", encoding="utf-8") as output:
            output.write("[")
            for row in self._rows(conn, record_type):
                if count:
                    output.write(",\n")
                json.dump(row, output, ensure_ascii=False)
                count += 1
            output.write("]\n")
        return count

    @staticmethod
    def _flat_record(row: dict[str, Any]) -> dict[str, Any]:
        flat = {"type": row["type"], "timestamp": row["timestamp"]}
        data = json.loads(row["data"])
        for key, value in data.items():
            if isinstance(value, dict):
                flat.update({f"{key}.{subkey}": subvalue for subkey, subvalue in value.items()})
            else:
                flat[key] = value
        for key, value in flat.items():
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
                value = "'" + value  # Spreadsheet formula injection protection.
            flat[key] = value
        return flat

    def export_csv(self, output_path: Path, record_type: str | None = None) -> int:
        # Two bounded-memory passes over the same read snapshot give a complete column union.
        with self._read_connection() as conn:
            conn.execute("BEGIN")
            fields = {"type", "timestamp"}
            for row in self._rows(conn, record_type):
                fields.update(self._flat_record(row))
            columns = ["type", "timestamp", *sorted(fields - {"type", "timestamp"})]
            count = 0
            with output_path.open("w", encoding="utf-8-sig", newline="") as output:
                writer = csv.DictWriter(output, fieldnames=columns)
                writer.writeheader()
                for row in self._rows(conn, record_type):
                    writer.writerow(self._flat_record(row))
                    count += 1
            return count

"""数据收集装置（database）— SQLite 存储实现。

替代原有的 JSONL 存储，支持：
- 历史数据查询（时间范围、类型筛选）
- 自动清理过期数据
- 数据导出（JSON/CSV）
- 线程安全（SQLite 原生支持）
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

DEFAULT_DB_FILE = Path(__file__).resolve().parent / "data" / "collected.db"
DEFAULT_RETENTION_DAYS = 30  # 数据保留天数


class DataCollector:
    """收集装置：SQLite 异步写入 + 历史查询。"""

    def __init__(
        self,
        db_file: Path = DEFAULT_DB_FILE,
        retention_days: int = DEFAULT_RETENTION_DAYS,
    ) -> None:
        self.db_file = db_file
        self.retention_days = retention_days
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._cleanup_loop, daemon=True, name="data-cleanup")
        self._started = False

    def start(self) -> None:
        """启动数据库连接和清理线程。"""
        if self._started:
            return
        self.db_file.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_file), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._create_tables()
        self._thread.start()
        self._started = True

    def stop(self) -> None:
        """停止清理线程并关闭数据库。"""
        self._stop.set()
        if self._started:
            self._thread.join(timeout=3)
        if self._conn:
            self._conn.close()
            self._conn = None
        self._started = False

    def record(self, record: dict[str, Any]) -> None:
        """写入一条记录（非阻塞，使用 WAL 模式提升并发性能）。"""
        if not self._conn:
            return
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO records (type, timestamp, data) VALUES (?, ?, ?)",
                    (record.get("type", "unknown"), record.get("timestamp", datetime.now().isoformat()), json.dumps(record, ensure_ascii=False)),
                )
                self._conn.commit()
        except Exception as exc:
            print(f"[database] 写入失败: {exc}")

    def query(
        self,
        record_type: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """查询历史记录。"""
        if not self._conn:
            return []
        sql = "SELECT * FROM records WHERE 1=1"
        params: list[Any] = []
        if record_type:
            sql += " AND type = ?"
            params.append(record_type)
        if start_time:
            sql += " AND timestamp >= ?"
            params.append(start_time)
        if end_time:
            sql += " AND timestamp <= ?"
            params.append(end_time)
        sql += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def get_stats(self) -> dict[str, Any]:
        """获取数据统计。"""
        if not self._conn:
            return {}
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            phone_count = self._conn.execute("SELECT COUNT(*) FROM records WHERE type = 'phone'").fetchone()[0]
            computer_count = self._conn.execute("SELECT COUNT(*) FROM records WHERE type = 'computer'").fetchone()[0]
            oldest = self._conn.execute("SELECT MIN(timestamp) FROM records").fetchone()[0]
            newest = self._conn.execute("SELECT MAX(timestamp) FROM records").fetchone()[0]
        return {
            "total": total,
            "phone": phone_count,
            "computer": computer_count,
            "oldest": oldest,
            "newest": newest,
            "retention_days": self.retention_days,
        }

    def export_json(self, output_path: Path, record_type: str | None = None) -> int:
        """导出数据为 JSON 文件。"""
        records = self.query(record_type=record_type, limit=10000)
        output_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        return len(records)

    def export_csv(self, output_path: Path, record_type: str | None = None) -> int:
        """导出数据为 CSV 文件。"""
        import csv
        records = self.query(record_type=record_type, limit=10000)
        if not records:
            return 0
        # 展平嵌套 JSON
        flat_records = []
        for r in records:
            flat = {"type": r["type"], "timestamp": r["timestamp"]}
            data = json.loads(r["data"]) if isinstance(r["data"], str) else r["data"]
            for k, v in data.items():
                if isinstance(v, dict):
                    for kk, vv in v.items():
                        flat[f"{k}.{kk}"] = vv
                else:
                    flat[k] = v
            flat_records.append(flat)
        # 写入 CSV
        fieldnames = list(flat_records[0].keys())
        with output_path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(flat_records)
        return len(flat_records)

    def cleanup(self) -> int:
        """清理过期数据。"""
        if not self._conn:
            return 0
        cutoff = (datetime.now() - timedelta(days=self.retention_days)).isoformat()
        with self._lock:
            cursor = self._conn.execute("DELETE FROM records WHERE timestamp < ?", (cutoff,))
            self._conn.commit()
            return cursor.rowcount

    def _create_tables(self) -> None:
        """创建数据表。"""
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                data TEXT NOT NULL
            )
        """)
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_records_type ON records(type)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_records_timestamp ON records(timestamp)")
        # WAL 模式提升读写并发
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.commit()

    def _cleanup_loop(self) -> None:
        """定期清理过期数据（每天一次）。"""
        while not self._stop.wait(86400):  # 24 小时
            try:
                deleted = self.cleanup()
                if deleted > 0:
                    print(f"[database] 清理过期数据 {deleted} 条")
            except Exception as exc:
                print(f"[database] 清理失败: {exc}")

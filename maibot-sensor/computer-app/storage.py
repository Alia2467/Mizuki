"""数据收集装置（storage）— 与主服务解耦的独立落盘模块。

为什么单独拆出来：
- 主服务（server.py）负责 HTTP 请求/响应，属于"热路径"；如果在这里直接写磁盘，
  磁盘 I/O 会阻塞请求处理，并频繁打断 CPU 缓存（缓存未命中），拖慢整体。
- 本模块用「线程安全队列 + 专用写盘线程」把落盘动作从请求路径剥离：
  server 只负责把记录丢进队列（纳秒级、非阻塞），真正的磁盘写由后台线程串行完成。

数据文件位于 data/ 目录，与程序代码彻底分离：
  computer-app/
    ├── server.py          # 程序（运行）
    ├── collector.py       # 电脑状态采集
    ├── storage.py         # 收集装置（本模块）
    └── data/              # 收集到的信息
        └── collected.jsonl
"""

from __future__ import annotations

import json
import queue
import threading
from pathlib import Path
from typing import Any

DEFAULT_DATA_FILE = Path(__file__).resolve().parent / "data" / "collected.jsonl"


class DataCollector:
    """收集装置：异步、单写盘线程的落盘队列。"""

    def __init__(self, data_file: Path = DEFAULT_DATA_FILE) -> None:
        self.data_file = data_file
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True, name="data-collector")
        self._started = False

    def start(self) -> None:
        """启动写盘线程。"""
        if self._started:
            return
        self.data_file.parent.mkdir(parents=True, exist_ok=True)
        self._thread.start()
        self._started = True

    def record(self, record: dict[str, Any]) -> None:
        """把一条记录放入队列（非阻塞，立即返回）。"""
        self._queue.put(record)

    def stop(self) -> None:
        """停止写盘线程（发送结束信号，并等待队列排干，避免尾部记录丢失）。"""
        self._queue.put(None)
        if self._started:
            self._thread.join(timeout=3)

    def _run(self) -> None:
        with self.data_file.open("a", encoding="utf-8") as f:
            while True:
                item = self._queue.get()
                if item is None:
                    break
                try:
                    f.write(json.dumps(item, ensure_ascii=False) + "\n")
                    f.flush()
                except Exception as exc:
                    print(f"[收集装置] 写入失败: {exc}")

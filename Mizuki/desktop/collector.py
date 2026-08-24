"""海月感知（Mizuki）— 电脑端状态采集器

负责周期性地采集电脑当前状态：
- 前台窗口标题 / 进程名
- 是否在打游戏（进程名关键词）
- 是否在导航（窗口标题关键词）

Windows 下使用 win32gui + psutil；其他平台优雅降级为占位值。
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

try:
    import psutil
except ImportError:  # pragma: no cover - 非 Windows 环境
    psutil = None

try:
    import win32gui
except ImportError:  # pragma: no cover - 非 Windows 环境
    win32gui = None

try:
    import win32process
except ImportError:  # pragma: no cover - 非 Windows 环境
    win32process = None

# 硬件采集占位值（psutil 不可用时回退）
_HARDWARE_PLACEHOLDER: dict[str, Any] = {
    "cpu_percent": 0,
    "memory_percent": 0,
    "memory_used_gb": 0.0,
    "memory_total_gb": 0.0,
    "disk_percent": 0,
    "disk_used_gb": 0.0,
    "disk_total_gb": 0.0,
}
GAME_KEYWORDS: tuple[str, ...] = (
    "steam",
    "league of legends",
    "valorant",
    "genshin",
    "原神",
    "minecraft",
    "英雄联盟",
    "绝地求生",
    "永劫无间",
    "cs2",
    "counter-strike",
)

# 导航窗口标题关键词（小写匹配）
NAVIGATION_KEYWORDS: tuple[str, ...] = (
    "高德",
    "百度地图",
    "腾讯地图",
    "google maps",
    "导航",
    "地图",
)


class ComputerCollector:
    """电脑状态采集器（后台线程）。"""

    def __init__(self, interval: float = 5.0) -> None:
        self.interval = max(1, int(interval))
        self._data: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # 采集逻辑
    # ------------------------------------------------------------------
    def _active_window(self) -> tuple[str, str]:
        """返回 (窗口标题, 进程名)。"""
        if win32gui is None:
            return "未知（非 Windows 环境）", ""
        try:
            hwnd = win32gui.GetForegroundWindow()
            title = win32gui.GetWindowText(hwnd)
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            process_name = ""
            if psutil is not None:
                try:
                    process_name = psutil.Process(pid).name()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    process_name = ""
            return title, process_name
        except Exception:  # 窗口在采集瞬间可能被关闭
            return "", ""

    @staticmethod
    def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
        lowered = text.lower()
        return any(kw in lowered for kw in keywords)

    def snapshot(self) -> dict[str, Any]:
        """采集一次当前电脑状态（始终包含硬件指标，前台窗口/进程/游戏/导航可选）。"""
        data = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "local_ip": self._local_ip(),
        }
        data.update(self._snapshot_hardware())
        return data

    def snapshot_foreground(self) -> dict[str, Any]:
        """采集前台窗口/进程/游戏/导航状态（受 computer_collect_enabled 控制）。"""
        title, process = self._active_window()
        return {
            "foreground_window": title,
            "foreground_process": process,
            "is_gaming": self._contains_any(process, GAME_KEYWORDS),
            "is_navigating": self._contains_any(title, NAVIGATION_KEYWORDS),
        }

    @staticmethod
    def _local_ip() -> str:
        """获取本机局域网 IP（UDP 探测法，取实际出站的接口地址）。"""
        import socket
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                return s.getsockname()[0]
        except Exception:
            return "127.0.0.1"

    @staticmethod
    def _snapshot_hardware() -> dict[str, Any]:
        """采集硬件占用指标（CPU / 内存 / 磁盘），psutil 不可用时返回占位值。"""
        if psutil is None:
            return dict(_HARDWARE_PLACEHOLDER)
        try:
            mem = psutil.virtual_memory()
            disk = psutil.disk_usage("/")
            return {
                "cpu_percent": int(psutil.cpu_percent(interval=None)),
                "memory_percent": int(mem.percent),
                "memory_used_gb": round(mem.used / (1024 ** 3), 1),
                "memory_total_gb": round(mem.total / (1024 ** 3), 1),
                "disk_percent": int(disk.percent),
                "disk_used_gb": round(disk.used / (1024 ** 3), 1),
                "disk_total_gb": round(disk.total / (1024 ** 3), 1),
            }
        except Exception:
            return dict(_HARDWARE_PLACEHOLDER)

    # ------------------------------------------------------------------
    # 后台线程
    # ------------------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                data = self.snapshot()
                with self._lock:
                    self._data = data
            except Exception as exc:  # 采集异常不应终止线程
                print(f"[电脑状态采集器] 采集失败: {exc}")
            self._stop.wait(self.interval)

    def start(self) -> None:
        """启动后台采集线程。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="computer-collector")
        self._thread.start()

    def stop(self) -> None:
        """停止后台采集线程。"""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def get(self) -> dict[str, Any]:
        """返回最近一次采集到的电脑状态。"""
        with self._lock:
            return dict(self._data)

    def get_or_empty(self) -> dict[str, Any]:
        """返回最近一次采集到的电脑状态，缓存为空返回空 dict。"""
        with self._lock:
            return dict(self._data) if self._data else {}

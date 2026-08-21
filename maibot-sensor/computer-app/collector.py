"""maibot感知 — 电脑端状态采集器

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

# 游戏进程关键词（小写匹配）
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

    def __init__(self, interval: int = 5) -> None:
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
            _, pid = win32gui.GetWindowThreadProcessId(hwnd)
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
        """采集一次当前电脑状态。"""
        title, process = self._active_window()
        return {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "foreground_window": title,
            "foreground_process": process,
            "is_gaming": self._contains_any(process, GAME_KEYWORDS),
            "is_navigating": self._contains_any(title, NAVIGATION_KEYWORDS),
        }

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
            return dict(self._data) if self._data else self.snapshot()

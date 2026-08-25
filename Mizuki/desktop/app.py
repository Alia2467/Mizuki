"""Mizuki · 控制台 — 电脑端汇聚服务（内嵌 WebUI + 系统托盘后台运行）

双击运行后：
- 在后台启动汇聚服务（HTTP 服务 + 电脑状态采集 + 异步落盘）
- 弹出内嵌 WebUI 的窗口，无需打开浏览器
- 关闭窗口会最小化到系统托盘，服务继续后台运行
- 托盘菜单可「显示窗口」或「退出」
- 运行日志写入 exe 同目录下的 server.log
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading

import pystray
import webview
from PIL import Image

from server import VERSION, app_dir, config, create_server, resource_dir, start_services, stop_services


def _redirect_logs() -> None:
    """窗口模式下 stdout/stderr 为 None，重定向到日志文件避免日志库崩溃。"""
    try:
        f = open(app_dir() / "server.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = f
        sys.stderr = f
    except Exception:
        pass


def make_icon() -> Image.Image:
    """托盘图标：优先用打包进去的 icon.ico，失败则画月牙。"""
    try:
        img = Image.open(resource_dir() / "icon.ico").convert("RGBA")
        img.thumbnail((64, 64), Image.LANCZOS)
        return img
    except Exception:
        from PIL import ImageDraw
        size = 64
        mask = Image.new("L", (size, size), 0)
        md = ImageDraw.Draw(mask)
        md.ellipse((6, 6, 58, 58), fill=255)
        md.ellipse((22, 0, 58, 36), fill=0)
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        ImageDraw.Draw(img).ellipse((6, 6, 58, 58), fill=(99, 102, 241, 255))
        img.putalpha(mask)
        return img


def _acquire_single_instance_lock() -> bool:
    """尝试获取全局互斥体，确保只有一个实例运行。返回 True 表示获取成功。"""
    mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\MizukiConsole")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        return False
    return True


def main() -> None:
    """桌面入口：内嵌窗口 + 系统托盘，服务在后台持续运行。"""
    if not _acquire_single_instance_lock():
        ctypes.windll.user32.MessageBoxW(0, "海月之音控制台已在运行中。", "海月之音", 0x40)
        sys.exit(0)
    _redirect_logs()
    start_services()
    server = create_server()
    threading.Thread(target=server.run, daemon=True, name="uvicorn").start()

    port = config["port"]
    url = f"http://localhost:{port}/"

    window = None
    try:
        window = webview.create_window(
            "控制台", url,
            width=1100, height=740,
            min_size=(760, 520),
            confirm_close=False,
        )
    except Exception as exc:
        print(f"内嵌窗口初始化失败: {exc}")

    if window is not None:
        def on_closing():
            # 关闭窗口改为隐藏到托盘，服务继续后台运行
            window.hide()
            return False

        window.events.closing += on_closing

    def show_window(icon=None, item=None):
        if window is not None:
            try:
                window.show()
            except Exception:
                pass

    def on_quit(icon, item):
        icon.stop()
        if window is not None:
            try:
                window.destroy()
            except Exception:
                pass
        server.should_exit = True
        stop_services()
        # 强制退出进程，避免残留
        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem("显示窗口", show_window, default=True),
        pystray.MenuItem("退出", on_quit),
    )
    icon = pystray.Icon("mizuki-console", make_icon(), "控制台", menu)

    threading.Thread(target=icon.run, daemon=True, name="tray").start()

    # 内嵌窗口在主线程运行，直到被销毁（退出）
    if window is not None:
        try:
            webview.start()
        except Exception as exc:
            print(f"内嵌窗口启动失败，改为纯后台运行: {exc}")
            threading.Event().wait()
    else:
        threading.Event().wait()

    server.should_exit = True
    stop_services()
    try:
        icon.stop()
    except Exception:
        pass


if __name__ == "__main__":
    main()

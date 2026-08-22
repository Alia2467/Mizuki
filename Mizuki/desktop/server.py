"""海月感知（Mizuki）— 电脑端汇聚服务（控制台）

职责：
- 接收手机端 APP 推送的感知数据（POST /phone-data）
- 采集电脑端自身状态（前台窗口 / 进程 / 游戏 / 导航）
- 合并两类数据（GET /merged-data，供 MaiBot 插件拉取）
- 提供简洁浅色的 WebUI 仪表盘（GET /）

「程序」与「收集到的信息」分离：
- 程序：server.py（本文件，HTTP 服务）+ collector.py（电脑状态采集）
- 收集装置：storage.py（队列 + 专用写盘线程，异步落盘，不阻塞请求路径）
- 收集到的信息：data/collected.jsonl（与程序代码分目录存放）

数据契约（手机 → 电脑，POST /phone-data）：
{
  "device_id": "my-phone",
  "timestamp": "2026-07-11T14:30:00",
  "location": {"city": "郑州", "latitude": 34.75, "longitude": 113.65},
  "weather":  {"condition": "rain", "temperature": 26, "humidity": 78},
  "health":   {"heart_rate": 85, "steps": 4200, "sleep_hours": 7.2},
  "usage":    {"foreground_app": "高德地图", "is_navigating": true, "is_calling": false,
               "is_listening_music": false, "music_app": "", "screen_text": ""},
  "diagnostics": {"app_version": "2.1.1", "send_success": 120, "send_failed": 3, "permissions": {...}, "warnings": [...]}
}
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

# Windows 控制台默认 GBK，打印 emoji 会触发 UnicodeEncodeError；
# 统一重配为 UTF-8 并对无法编码的字符做替换，保证日志输出不崩溃。
if sys.stdout is not None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr is not None:
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

from collector import ComputerCollector
from storage import DataCollector

# ----------------------------------------------------------------------
# 常量与路径（兼容 PyInstaller 打包：程序目录 vs 资源目录分离）
# ----------------------------------------------------------------------
VERSION = "1.0.0"

# 值取 config.json 的 shared_token；手机上报与插件拉取共用同一请求头
TOKEN_HEADER = "X-Sensor-Token"


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def app_dir() -> Path:
    """可写目录：config.json 与 data/ 放在 exe 旁边（源码运行时即源码目录）。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_dir() -> Path:
    """静态资源目录：源码运行时是源码目录，打包后是解包临时目录。"""
    if is_frozen():
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


BASE_DIR = app_dir()
STATIC_DIR = resource_dir() / "static"
CONFIG_PATH = app_dir() / "config.json"
DATA_FILE = app_dir() / "data" / "collected.jsonl"

DEFAULT_CONFIG: dict[str, Any] = {
    "host": "0.0.0.0",
    "port": 821,
    "computer_collect_interval": 5,  # 电脑状态采集间隔（秒）
    "phone_timeout_seconds": 90,  # 超过该时长未上报视为手机离线（秒）
    "poll_interval": 5,  # WebUI 刷新间隔（秒）
    "shared_token": "",  # 共享鉴权 token；空串表示不启用鉴权（兼容模式）
}

app = FastAPI(title="控制台", version=VERSION)


# ----------------------------------------------------------------------
# 数据契约模型（手机 → 电脑，POST /phone-data 的五段结构）
# 字段名与语义见 .docs/architecture.md §2.1；extra="allow" 保证新字段向前兼容
# ----------------------------------------------------------------------
class LocationData(BaseModel):
    model_config = ConfigDict(extra="allow")

    city: str = "未知"
    latitude: float = 0.0
    longitude: float = 0.0


class WeatherData(BaseModel):
    model_config = ConfigDict(extra="allow")

    condition: str = "unknown"
    temperature: int = 0
    humidity: int = 0


class HealthData(BaseModel):
    model_config = ConfigDict(extra="allow")

    heart_rate: int = 0
    steps: int = 0
    sleep_hours: float = 0.0


class UsageData(BaseModel):
    model_config = ConfigDict(extra="allow")

    foreground_app: str = "未知"
    is_navigating: bool = False
    is_calling: bool = False
    is_listening_music: bool = False
    music_app: str = ""
    screen_text: str = ""


class PhoneData(BaseModel):
    model_config = ConfigDict(extra="allow")

    device_id: str
    timestamp: str
    location: LocationData = LocationData()
    weather: WeatherData = WeatherData()
    health: HealthData = HealthData()
    usage: UsageData = UsageData()
    diagnostics: dict[str, Any] = {}

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ----------------------------------------------------------------------
# 运行态数据（进程内缓存 + 线程锁）
# ----------------------------------------------------------------------
_state_lock = threading.Lock()
_latest_phone: dict[str, Any] = {}
_phone_received_at: datetime | None = None
_started_at = datetime.now()


def load_config() -> dict[str, Any]:
    """读取 config.json，缺失时写入默认配置。"""
    if CONFIG_PATH.exists():
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            return {**DEFAULT_CONFIG, **raw}
        except Exception:
            pass
    CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
    return dict(DEFAULT_CONFIG)


def save_config() -> None:
    """将当前配置写入 config.json。"""
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


config = load_config()
collector = ComputerCollector(interval=config["computer_collect_interval"])
storage = DataCollector(data_file=DATA_FILE)  # 收集装置：队列 + 专用写盘线程，与主服务解耦


# ----------------------------------------------------------------------
# 辅助函数
# ----------------------------------------------------------------------
def _iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _auth_enabled() -> bool:
    return bool(config.get("shared_token"))


def _reject_unauthorized() -> JSONResponse:
    return JSONResponse({"status": "error", "message": "token 无效或缺失"}, status_code=401)


def _check_token(request: Request) -> JSONResponse | None:
    """校验共享 token；未启用鉴权或校验通过返回 None，否则返回 401 响应。"""
    if not _auth_enabled():
        return None
    if request.headers.get(TOKEN_HEADER) != config["shared_token"]:
        return _reject_unauthorized()
    return None


# 落盘比对只看四元组（前台窗口/进程/游戏/导航），变化才记录，避免刷屏
_last_computer_key: tuple[Any, ...] | None = None


def _computer_persistence_loop() -> None:
    """后台线程：电脑状态发生变化时，把快照交给收集装置落盘。"""
    global _last_computer_key
    while True:
        try:
            data = collector.get()
            key = (
                data.get("foreground_window"),
                data.get("foreground_process"),
                data.get("is_gaming"),
                data.get("is_navigating"),
            )
            if key != _last_computer_key:
                _last_computer_key = key
                storage.record({"type": "computer", **data})
        except Exception as exc:
            print(f"[电脑状态采集] 异常: {exc}")
        # 间隔唯一数据源是 config；落盘比对与采集器共用同一配置项
        time.sleep(config["computer_collect_interval"])


def _phone_is_online() -> bool:
    # 共享状态的读取也必须持锁（写入方在请求处理器内持 _state_lock）
    with _state_lock:
        received_at = _phone_received_at
    if received_at is None:
        return False
    return (datetime.now() - received_at).total_seconds() <= config["phone_timeout_seconds"]


def _build_state() -> dict[str, Any]:
    """构造仪表盘与插件共用的完整状态。"""
    with _state_lock:
        phone = dict(_latest_phone)
        phone_last_seen = _phone_received_at.isoformat(timespec="seconds") if _phone_received_at else None
    return {
        "timestamp": _iso_now(),
        "phone": phone,
        "phone_connected": _phone_is_online(),
        "phone_last_seen": phone_last_seen,
        "computer": collector.get(),
        "server": {
            "started_at": _started_at.isoformat(timespec="seconds"),
            "uptime_seconds": int((datetime.now() - _started_at).total_seconds()),
            "version": VERSION,
        },
    }


def _merged_data() -> dict[str, Any]:
    """供 MaiBot 插件拉取的合并数据。"""
    state = _build_state()
    return {
        "timestamp": state["timestamp"],
        "phone": state["phone"],
        "phone_connected": state["phone_connected"],
        "computer": state["computer"],
    }


# ----------------------------------------------------------------------
# 路由
# ----------------------------------------------------------------------
@app.get("/")
async def dashboard() -> FileResponse:
    """WebUI 仪表盘首页。"""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health() -> dict[str, Any]:
    """健康检查。"""
    return {"status": "ok", "version": VERSION, "phone_connected": _phone_is_online()}


@app.get("/api/state")
async def api_state() -> JSONResponse:
    """仪表盘轮询用的完整状态。"""
    return JSONResponse(_build_state())


@app.post("/phone-data")
async def receive_phone_data(request: Request, data: PhoneData) -> JSONResponse:
    """接收手机端推送的感知数据（Pydantic 按契约模型校验，非法请求 422 拒收）。"""
    global _latest_phone, _phone_received_at

    denied = _check_token(request)
    if denied is not None:
        return denied

    payload = data.model_dump()
    payload["received_at"] = _iso_now()

    with _state_lock:
        _latest_phone = payload
        _phone_received_at = datetime.now()

    # 落盘走收集装置异步完成，请求路径不做磁盘 I/O
    storage.record({"type": "phone", **payload})

    return JSONResponse({"status": "ok", "message": "数据已收到"})


@app.get("/merged-data")
async def merged_data(request: Request) -> JSONResponse:
    """合并数据接口，供 MaiBot 插件周期拉取（启用鉴权时需携带 token）。"""
    denied = _check_token(request)
    if denied is not None:
        return denied
    return JSONResponse(_merged_data())


@app.get("/api/config")
async def api_config() -> JSONResponse:
    """返回当前配置（供仪表盘展示）。"""
    return JSONResponse({
        "host": config["host"],
        "port": config["port"],
        "computer_collect_interval": config["computer_collect_interval"],
        "phone_timeout_seconds": config["phone_timeout_seconds"],
        "poll_interval": config.get("poll_interval", 5),
        "shared_token": config["shared_token"],
        "auth_enabled": _auth_enabled(),
        "data_file": str(storage.data_file),
    })


@app.patch("/api/config")
async def api_config_update(request: Request) -> JSONResponse:
    """更新配置并持久化到 config.json。"""
    body = await request.json()
    allowed = {"computer_collect_interval", "phone_timeout_seconds", "shared_token", "poll_interval"}
    updated = []
    for key in allowed:
        if key in body:
            val = body[key]
            if key in ("computer_collect_interval", "phone_timeout_seconds", "poll_interval"):
                val = max(1, int(val))
            config[key] = val
            updated.append(key)
    if "computer_collect_interval" in updated:
        collector.interval = config["computer_collect_interval"]
    save_config()
    return JSONResponse({"status": "ok", "updated": updated})


@app.get("/api/logs")
async def api_logs(limit: int = 20) -> JSONResponse:
    """返回最近 N 条已落盘记录（新的在前），供仪表盘展示。"""
    limit = max(1, min(int(limit), 200))
    lines = _read_tail_lines(DATA_FILE, limit)
    records: list[dict[str, Any]] = []
    for line in reversed(lines[-limit:]):
        try:
            records.append(json.loads(line))
        except Exception:
            continue
    return JSONResponse(records)


def _read_tail_lines(path: Path, limit: int) -> list[str]:
    """读取文件末尾最多 limit 行；大文件只读尾部块，避免整读劣化。"""
    try:
        size = path.stat().st_size
    except OSError:
        return []
    if size == 0:
        return []
    # 单条记录上限按 8KB 估算，多留一块避免截断首行
    chunk = min(size, limit * 8192 + 8192)
    try:
        with path.open("rb") as f:
            f.seek(size - chunk)
            data = f.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    # 分块读取时首行可能被截断，仅在非整读时丢弃
    if chunk < size:
        lines = lines[1:]
    return lines[-limit:]


# ----------------------------------------------------------------------
# 启动
# ----------------------------------------------------------------------
def start_services() -> None:
    """启动采集器与落盘线程（供桌面入口 app.py 与命令行入口共用）。"""
    storage.start()
    collector.start()
    threading.Thread(target=_computer_persistence_loop, daemon=True, name="computer-persistence").start()


def stop_services() -> None:
    """停止采集器与落盘线程。"""
    collector.stop()
    storage.stop()


def _is_port_available(host: str, port: int) -> bool:
    """检查端口是否可用。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((host, port))
            return True
    except OSError:
        return False


def _find_available_port(host: str, preferred: int, max_search: int = 100) -> int:
    """从首选端口开始向上搜索可用端口。"""
    if _is_port_available(host, preferred):
        return preferred
    for port in range(preferred + 1, preferred + max_search):
        if _is_port_available(host, port):
            return port
    return preferred  # 搜索失败仍返回首选，让 uvicorn 报错


def create_server() -> uvicorn.Server:
    """构造 uvicorn 服务器实例（可放入后台线程运行）。"""
    host = config["host"]
    port = _find_available_port(host, config["port"])
    if port != config["port"]:
        print(f"⚠ 端口 {config['port']} 被占用，自动切换到 {port}")
        print(f"   请同步更新手机端和插件的端口配置")
        config["port"] = port
    cfg = uvicorn.Config(app, host=host, port=port, log_level="info")
    return uvicorn.Server(cfg)


def main() -> None:
    """命令行入口：启动采集与落盘线程，前台运行 HTTP 服务。"""
    print("📡 海月感知 · 控制台 已启动")
    print(f"   WebUI 仪表盘:  http://localhost:{config['port']}/")
    print(f"   手机数据接口:  http://<你的局域网IP>:{config['port']}/phone-data")
    print(f"   合并数据接口:  http://localhost:{config['port']}/merged-data")
    if not _auth_enabled():
        print("⚠ [安全] shared_token 未配置，接口鉴权已关闭。在 config.json 填入 shared_token，并在手机端/插件配置同一 token 后重启即可启用。")
    start_services()
    print(f"   数据落盘位置:  {storage.data_file}")
    server = create_server()
    try:
        server.run()
    finally:
        stop_services()


if __name__ == "__main__":
    main()

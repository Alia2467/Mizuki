"""Mizuki — 电脑端汇聚服务（控制台）

职责：
- 接收手机端 APP 推送的感知数据（POST /phone-data）
- 采集电脑端自身状态（前台窗口 / 进程 / 游戏 / 导航）
- 合并两类数据（GET /merged-data，供 MaiBot 插件拉取）
- 提供简洁浅色的 WebUI 仪表盘（GET /）

「程序」与「收集到的信息」分离：
- 程序：server.py（本文件，HTTP 服务）+ collector.py（电脑状态采集）
- 收集装置：database.py（SQLite 异步写入 + 历史查询）
- 收集到的信息：data/collected.db（与程序代码分目录）

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
import logging
import socket
import sys
import threading
import time
import traceback
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
from database import DataCollector

# ----------------------------------------------------------------------
# 常量与路径（兼容 PyInstaller 打包：程序目录 vs 资源目录分离）
# ----------------------------------------------------------------------
VERSION = "1.1.0"

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


STATIC_DIR = resource_dir() / "static"
CONFIG_PATH = app_dir() / "config.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "host": "0.0.0.0",
    "port": 821,
    "computer_collect_enabled": True,  # 是否启用电脑状态采集（前台窗口/进程/游戏/导航）
    "computer_collect_interval": 300,  # 电脑状态采集间隔（毫秒）
    "phone_timeout_ms": 10000,  # 超过该时长未上报视为手机离线（毫秒）
    "poll_interval": 5000,  # WebUI 仪表盘轮询间隔（毫秒）
    "shared_token": "",  # 共享鉴权 token；空串表示不启用鉴权（兼容模式）
    "plugin_heartbeat_timeout": 10,  # 插件心跳超时（秒），超过此时间未心跳视为离线
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

# ----------------------------------------------------------------------
# 速率限制（简单滑动窗口，按 IP 限流）
# ----------------------------------------------------------------------
_rate_lock = threading.Lock()
_rate_windows: dict[str, list[float]] = {}
_RATE_LIMITS: dict[str, tuple[int, float]] = {
    "/phone-data": (60, 60.0),    # 60 次/分钟
    "/merged-data": (500, 60.0),  # 500 次/分钟（插件高频轮询）
    "default": (120, 60.0),       # 默认 120 次/分钟
}


def _is_rate_limited(client_ip: str, path: str) -> bool:
    """检查是否超出速率限制。返回 True 表示应拒绝。"""
    now = time.time()
    limit, window = _RATE_LIMITS.get(path, _RATE_LIMITS["default"])
    key = f"{client_ip}:{path}"
    with _rate_lock:
        timestamps = _rate_windows.setdefault(key, [])
        # 清理过期记录
        cutoff = now - window
        timestamps[:] = [t for t in timestamps if t > cutoff]
        if len(timestamps) >= limit:
            return True
        timestamps.append(now)
        return False


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    """全局速率限制中间件。"""
    # 只对数据端点限流
    path = request.url.path
    if path in _RATE_LIMITS:
        client_ip = request.client.host if request.client else "unknown"
        if _is_rate_limited(client_ip, path):
            return JSONResponse({"status": "error", "message": "请求过于频繁，请稍后再试"}, status_code=429)
    return await call_next(request)

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


def _resolve_effective_token() -> str:
    """解析有效 Token：环境变量 MIZUKI_TOKEN 优先于 config.json。"""
    import os
    env_token = os.environ.get("MIZUKI_TOKEN", "").strip()
    return env_token if env_token else config.get("shared_token", "")


def save_config() -> None:
    """将当前配置写入 config.json。"""
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


config = load_config()
collector = ComputerCollector(interval=config["computer_collect_interval"] / 1000)
storage = DataCollector(db_file=app_dir() / "data" / "collected.db")  # 收集装置：队列 + 专用写盘线程，与主服务解耦

# 插件心跳追踪（plugin_id -> 最后心跳时间）
_plugin_heartbeats: dict[str, datetime] = {}


# ----------------------------------------------------------------------
# 辅助函数
# ----------------------------------------------------------------------
def _iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ----------------------------------------------------------------------
# 结构化日志（统一入口，便于后续扩展为文件日志）
# ----------------------------------------------------------------------

_logger = logging.getLogger("mizuki")


_LOG_LEVELS = {"error": logging.ERROR, "warning": logging.WARNING, "debug": logging.DEBUG, "info": logging.INFO}


def _log(level: str, message: str) -> None:
    """统一日志入口，带级别与线程名。"""
    _logger.log(_LOG_LEVELS.get(level, logging.INFO), f"[{threading.current_thread().name}] {message}")


def _auth_enabled() -> bool:
    return bool(_resolve_effective_token())


def _reject_unauthorized() -> JSONResponse:
    return JSONResponse({"status": "error", "message": "token 无效或缺失"}, status_code=401)


def _check_token(request: Request) -> JSONResponse | None:
    """校验共享 token；未启用鉴权或校验通过返回 None，否则返回 401 响应。"""
    if not _auth_enabled():
        return None
    if request.headers.get(TOKEN_HEADER) != _resolve_effective_token():
        return _reject_unauthorized()
    return None


# 落盘比对只看四元组（前台窗口/进程/游戏/导航），变化才记录，避免刷屏
_last_computer_key: tuple[Any, ...] | None = None


_COMPUTER_PERSISTENCE_KEYS = ("foreground_window", "foreground_process", "is_gaming")


def _computer_persistence_key(data: dict[str, Any]) -> tuple[Any, ...]:
    """提取电脑状态落盘比对键（前台窗口/进程/游戏三元组）。"""
    return tuple(data.get(k) for k in _COMPUTER_PERSISTENCE_KEYS)


def _computer_persistence_loop() -> None:
    """后台线程：电脑状态前台三元组发生变化时，把快照交给收集装置落盘。"""
    global _last_computer_key
    while True:
        try:
            if config.get("computer_collect_enabled", True):
                data = collector.get()
                if data:
                    key = _computer_persistence_key(data)
                    if key != _last_computer_key:
                        _last_computer_key = key
                        storage.record({"type": "computer", **data})
        except Exception as exc:
            _log("error", f"[电脑状态采集] 异常: {exc}")
        # 间隔唯一数据源是 config；落盘比对与采集器共用同一配置项
        time.sleep(config["computer_collect_interval"] / 1000)


def _phone_is_online() -> bool:
    # 共享状态的读取也必须持锁（写入方在请求处理器内持 _state_lock）
    with _state_lock:
        received_at = _phone_received_at
    return received_at is not None and (datetime.now() - received_at).total_seconds() * 1000 <= config["phone_timeout_ms"]


def _build_state() -> dict[str, Any]:
    """构造仪表盘与插件共用的完整状态。"""
    try:
        with _state_lock:
            phone = dict(_latest_phone)
            phone_last_seen = _phone_received_at.isoformat(timespec="seconds") if _phone_received_at else None
        # collector.get() 已包含硬件 + 前台（受 is_collecting_foreground 控制）
        computer = collector.get()
        return {
            "timestamp": _iso_now(),
            "phone": phone,
            "phone_connected": _phone_is_online(),
            "phone_last_seen": phone_last_seen,
            "computer": computer,
            "server": {
                "started_at": _started_at.isoformat(timespec="seconds"),
                "uptime_seconds": int((datetime.now() - _started_at).total_seconds()),
                "version": VERSION,
            },
        }
    except Exception as exc:
        _log("error", f"[_build_state] 异常: {exc}")
        traceback.print_exc()
        return {
            "timestamp": _iso_now(),
            "phone": {},
            "phone_connected": False,
            "computer": {},
            "server": {"version": VERSION, "error": str(exc)},
        }


def _merged_data() -> dict[str, Any]:
    """供 MaiBot 插件拉取的合并数据。"""
    state = _build_state()
    keys = ("timestamp", "phone", "phone_connected", "computer")
    return {k: state[k] for k in keys}


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


@app.get("/health/deep")
async def health_deep() -> JSONResponse:
    """深度健康检查（数据库、采集器、插件状态）。"""
    now = datetime.now()

    # 数据库状态
    db_healthy = storage._conn is not None
    db_last_write = storage.last_record_time
    db_lag_seconds: int | None = None
    if db_last_write:
        db_lag_seconds = int((now - db_last_write).total_seconds())

    # 采集器线程状态
    collector_alive = collector._thread is not None and collector._thread.is_alive()

    # 插件心跳状态
    plugins_total = len(_plugin_heartbeats)
    plugins_online = sum(1 for t in _plugin_heartbeats.values() if (now - t).total_seconds() < config.get("plugin_heartbeat_timeout", 10))

    overall_healthy = db_healthy and collector_alive

    return JSONResponse({
        "status": "ok" if overall_healthy else "degraded",
        "version": VERSION,
        "phone_connected": _phone_is_online(),
        "database": {
            "connected": db_healthy,
            "last_write": db_last_write.isoformat(timespec="seconds") if db_last_write else None,
            "lag_seconds": db_lag_seconds,
        },
        "collector": {
            "alive": collector_alive,
            "collect_foreground": collector.is_collecting_foreground,
        },
        "plugins": {
            "online": plugins_online,
            "total": plugins_total,
        },
    })


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
    try:
        return JSONResponse(_merged_data())
    except Exception as exc:
        _log("error", f"[merged-data] 异常: {exc}")
        traceback.print_exc()
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=500)


@app.get("/api/config")
async def api_config() -> JSONResponse:
    """返回当前配置（供仪表盘展示）。"""
    return JSONResponse({
        "host": config["host"],
        "port": config["port"],
        "computer_collect_enabled": config.get("computer_collect_enabled", True),
        "computer_collect_interval": config["computer_collect_interval"],
        "phone_timeout_ms": config["phone_timeout_ms"],
        "poll_interval": config.get("poll_interval", 5),
        "shared_token": config["shared_token"],
        "auth_enabled": _auth_enabled(),
        "token_from_env": bool(_resolve_effective_token()) and not bool(config.get("shared_token")),
        "db_file": str(storage.db_file),
    })


@app.patch("/api/config")
async def api_config_update(request: Request) -> JSONResponse:
    """更新配置并持久化到 config.json。"""
    body = await request.json()
    allowed = {"computer_collect_enabled", "computer_collect_interval", "phone_timeout_ms", "shared_token", "poll_interval"}
    updated = []
    for key in allowed:
        if key in body:
            val = body[key]
            if key in ("computer_collect_interval", "phone_timeout_ms", "poll_interval"):
                val = max(100, int(val))
            config[key] = val
            updated.append(key)
    if "computer_collect_interval" in updated:
        collector.interval = config["computer_collect_interval"] / 1000
    if "computer_collect_enabled" in updated:
        collector.is_collecting_foreground = config["computer_collect_enabled"]
    save_config()
    return JSONResponse({"status": "ok", "updated": updated})


@app.get("/api/logs")
async def api_logs(limit: int = 20, offset: int = 0, record_type: str | None = None) -> JSONResponse:
    """返回历史记录（新的在前），支持分页和类型筛选。"""
    limit = max(1, min(int(limit), 200))
    records = storage.query(record_type=record_type, limit=limit, offset=offset)
    return JSONResponse(records)


@app.get("/api/stats")
async def api_stats() -> JSONResponse:
    """返回数据统计。"""
    return JSONResponse(storage.get_stats())


@app.get("/api/export/{format}")
async def api_export(format: str, record_type: str | None = None) -> FileResponse:
    """导出数据（json 或 csv）。"""
    export_dir = app_dir() / "data" / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if format == "json":
        path = export_dir / f"mizuki_export_{timestamp}.json"
        storage.export_json(path, record_type=record_type)
    elif format == "csv":
        path = export_dir / f"mizuki_export_{timestamp}.csv"
        storage.export_csv(path, record_type=record_type)
    else:
        return JSONResponse({"status": "error", "message": "不支持的格式"}, status_code=400)
    return FileResponse(path, filename=path.name, media_type="application/octet-stream")


@app.post("/api/plugin-heartbeat")
async def plugin_heartbeat(request: Request) -> JSONResponse:
    """插件心跳上报（插件定期调用，用于 WebUI 显示插件连接状态）。"""
    body = await request.json()
    plugin_id = body.get("plugin_id", "unknown")
    _plugin_heartbeats[plugin_id] = datetime.now()
    return JSONResponse({"status": "ok"})


@app.get("/api/plugin-status")
async def plugin_status() -> JSONResponse:
    """返回插件连接状态（供 WebUI 展示）。"""
    now = datetime.now()
    plugins = []
    for plugin_id, last_beat in _plugin_heartbeats.items():
        elapsed = (now - last_beat).total_seconds()
        plugins.append({
            "plugin_id": plugin_id,
            "last_seen": last_beat.isoformat(timespec="seconds"),
            "online": elapsed < config.get("plugin_heartbeat_timeout", 10),
        })
    return JSONResponse({"plugins": plugins, "timeout": config.get("plugin_heartbeat_timeout", 10)})


# ----------------------------------------------------------------------
# 配置热重载
# ----------------------------------------------------------------------
_config_watch_thread: threading.Thread | None = None
_config_mtime: float = 0


def _config_watcher() -> None:
    """监听 config.json 变化，自动重载配置。"""
    global _config_mtime
    while True:
        time.sleep(5)
        try:
            if not CONFIG_PATH.exists():
                continue
            mtime = CONFIG_PATH.stat().st_mtime
            if mtime == _config_mtime:
                continue
            _config_mtime = mtime
            new_config = load_config()
            # 同步前台采集开关到采集器
            if "computer_collect_enabled" in new_config:
                collector.is_collecting_foreground = new_config["computer_collect_enabled"]
            config.update(new_config)
            collector.interval = config["computer_collect_interval"] / 1000
            _log("info", f"[config] 配置已热重载: {list(new_config.keys())}")
        except Exception as exc:
            _log("error", f"[config] 热重载失败: {exc}")


# ----------------------------------------------------------------------
# 启动
# ----------------------------------------------------------------------
def start_services() -> None:
    """启动采集器、落盘线程和配置热重载（硬件检测始终运行，前台数据落盘受开关控制）。"""
    storage.start()
    collector.start()  # 硬件检测始终运行
    collector.is_collecting_foreground = config.get("computer_collect_enabled", True)
    threading.Thread(target=_computer_persistence_loop, daemon=True, name="computer-persistence").start()
    # 启动配置热重载监听
    global _config_watch_thread, _config_mtime
    _config_mtime = CONFIG_PATH.stat().st_mtime if CONFIG_PATH.exists() else 0
    _config_watch_thread = threading.Thread(target=_config_watcher, daemon=True, name="config-watcher")
    _config_watch_thread.start()


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
    return next(
        (port for port in range(preferred, preferred + max_search) if _is_port_available(host, port)),
        preferred,
    )


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
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    print("📡 Mizuki · 控制台 已启动")
    print(f"   WebUI 仪表盘:  http://localhost:{config['port']}/")
    print(f"   手机数据接口:  http://<你的局域网IP>:{config['port']}/phone-data")
    print(f"   合并数据接口:  http://localhost:{config['port']}/merged-data")
    if not _auth_enabled():
        print("⚠ [安全] shared_token 未配置，接口鉴权已关闭。在 config.json 填入 shared_token，或设置环境变量 MIZUKI_TOKEN，并在手机端/插件配置同一 token 后重启即可启用。")
    else:
        token_source = "环境变量" if (_resolve_effective_token() and not config.get("shared_token")) else "config.json"
        print(f"🔒 鉴权已启用（{token_source}）")
    start_services()
    print(f"   数据落盘位置:  {storage.db_file}")
    server = create_server()
    try:
        server.run()
    finally:
        stop_services()


if __name__ == "__main__":
    main()

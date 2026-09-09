"""Mizuki desktop: authenticated cached HTTP data, asynchronous SQLite persistence."""
from __future__ import annotations

from collections import OrderedDict, deque
from copy import deepcopy
from datetime import datetime
import hmac
import json
import logging
import os
from pathlib import Path
import socket
import sys
import threading
import time
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

for output in (sys.stdout, sys.stderr):
    if output is not None and hasattr(output, "reconfigure"):
        output.reconfigure(encoding="utf-8", errors="replace")

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, ValidationError, field_validator
from starlette.background import BackgroundTask

from collector import ComputerCollector
from database import DataCollector

VERSION = "1.1.3"
TOKEN_HEADER = "X-Sensor-Token"
SESSION_HEADER = "X-Sensor-Session"
SEQUENCE_HEADER = "X-Sensor-Sequence"
REPLAY_HEADER = "X-Sensor-Replay"
_MAX_ORDER_DEVICES = 64
_MAX_RETIRED_SESSIONS = 64
_MAX_RATE_KEYS = 2048
_MAX_PLUGIN_IDS = 128


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def app_dir() -> Path:
    return Path(sys.executable).resolve().parent if is_frozen() else Path(__file__).resolve().parent


def resource_dir() -> Path:
    return Path(sys._MEIPASS) if is_frozen() else Path(__file__).resolve().parent


STATIC_DIR = resource_dir() / "static"
CONFIG_PATH = app_dir() / "config.json"
DEFAULT_CONFIG: dict[str, Any] = {
    "host": "0.0.0.0", "port": 821,
    "computer_collect_enabled": True, "computer_collect_interval": 300,
    "phone_timeout_ms": 10000, "poll_interval": 5000,
    "shared_token": "", "plugin_heartbeat_timeout": 10,
}
app = FastAPI(title="控制台", version=VERSION)
_logger = logging.getLogger("mizuki")
_LOG_LEVELS = {"error": logging.ERROR, "warning": logging.WARNING, "debug": logging.DEBUG, "info": logging.INFO}
_state_lock = threading.Lock()
_config_io_lock = threading.Lock()
config = dict(DEFAULT_CONFIG)  # Importing a module must not read/write a user's configuration.
collector = ComputerCollector(interval=config["computer_collect_interval"] / 1000)
storage = DataCollector(db_file=app_dir() / "data" / "collected.db")
_latest_phone: dict[str, Any] = {}
_phone_received_at: datetime | None = None
_phone_received_monotonic: float | None = None
_phone_orders: OrderedDict[str, dict[str, Any]] = OrderedDict()
_plugin_heartbeats: dict[str, tuple[datetime, float]] = {}
_started_at = datetime.now()
_started_monotonic = time.monotonic()
_services_lock = threading.Lock()
_services_started = False
_services_stop = threading.Event()
_service_threads: list[threading.Thread] = []
_last_computer_key: tuple[Any, ...] | None = None
_COMPUTER_PERSISTENCE_KEYS = ("foreground_window", "foreground_process", "is_gaming")


class LocationData(BaseModel):
    model_config = ConfigDict(extra="allow", allow_inf_nan=False)
    city: str = "未知"
    latitude: float = Field(default=0.0, ge=-90, le=90)
    longitude: float = Field(default=0.0, ge=-180, le=180)


class WeatherData(BaseModel):
    model_config = ConfigDict(extra="allow")
    condition: Literal["clear", "cloudy", "fog", "drizzle", "rain", "snow", "shower", "thunderstorm", "unknown"] = "unknown"
    temperature: int = 0
    humidity: int = Field(default=0, ge=0, le=100)


class HealthData(BaseModel):
    model_config = ConfigDict(extra="allow", allow_inf_nan=False)
    heart_rate: int = Field(default=0, ge=0)
    steps: int = Field(default=0, ge=0)
    sleep_hours: float = Field(default=0.0, ge=0)


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
    device_id: str = Field(min_length=1, max_length=128)
    timestamp: str
    location: LocationData = Field(default_factory=LocationData)
    weather: WeatherData = Field(default_factory=WeatherData)
    health: HealthData = Field(default_factory=HealthData)
    usage: UsageData = Field(default_factory=UsageData)
    diagnostics: dict[str, Any] = Field(default_factory=dict)

    @field_validator("timestamp")
    @classmethod
    def valid_timestamp(cls, value: str) -> str:
        if "T" not in value:
            raise ValueError("timestamp must be ISO 8601")
        datetime.fromisoformat(value)
        return value


class ConfigPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    computer_collect_enabled: StrictBool | None = None
    computer_collect_interval: StrictInt | None = None
    phone_timeout_ms: StrictInt | None = None
    poll_interval: StrictInt | None = None
    shared_token: StrictStr | None = Field(default=None, max_length=4096)

    @field_validator("computer_collect_interval", "phone_timeout_ms", "poll_interval")
    @classmethod
    def bounded_interval(cls, value: int | None) -> int | None:
        if value is not None and value > 600000:
            raise ValueError("interval must not exceed 600000 milliseconds")
        return max(100, value) if value is not None else None


class HeartbeatData(BaseModel):
    plugin_id: str = Field(default="unknown", min_length=1, max_length=128)


def _log(level: str, message: str) -> None:
    _logger.log(_LOG_LEVELS.get(level, logging.INFO), "[%s] %s", threading.current_thread().name, message)


def _config_snapshot() -> dict[str, Any]:
    with _state_lock:
        return dict(config)


def _validate_config(raw: dict[str, Any]) -> dict[str, Any]:
    candidate = {**DEFAULT_CONFIG, **raw}
    fields = {key: candidate[key] for key in ConfigPatch.model_fields}
    parsed = ConfigPatch.model_validate(fields).model_dump()
    if any(value is None for value in parsed.values()):
        raise ValueError("configuration values cannot be null")
    candidate.update(parsed)
    if not isinstance(candidate["host"], str) or not candidate["host"]:
        raise ValueError("host must be a nonempty string")
    if type(candidate["port"]) is not int or not 1 <= candidate["port"] <= 65535:
        raise ValueError("port must be between 1 and 65535")
    timeout = candidate["plugin_heartbeat_timeout"]
    if type(timeout) is not int or not 1 <= timeout <= 3600:
        raise ValueError("plugin heartbeat timeout must be between 1 and 3600 seconds")
    return candidate


def _write_config(candidate: dict[str, Any]) -> None:
    temporary = CONFIG_PATH.with_name(f".{CONFIG_PATH.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(CONFIG_PATH)
    finally:
        temporary.unlink(missing_ok=True)


def load_config() -> dict[str, Any]:
    """Load at startup/on the config thread; preserve invalid files for correction."""
    with _config_io_lock:
        if not CONFIG_PATH.exists():
            try:
                _write_config(DEFAULT_CONFIG)
            except OSError:
                _log("error", "无法写入默认配置，继续使用内存默认值")
            return dict(DEFAULT_CONFIG)
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("configuration must be an object")
            return _validate_config(raw)
        except (OSError, ValueError, TypeError):
            _log("warning", "配置无效，保留文件与上次有效配置；请检查配置格式和数值")
            return _config_snapshot()


def _apply_config(candidate: dict[str, Any]) -> None:
    with _state_lock:
        config.clear()
        config.update(candidate)
        collector.interval = candidate["computer_collect_interval"] / 1000
        collector.is_collecting_foreground = candidate["computer_collect_enabled"]


def save_config() -> None:
    with _config_io_lock:
        _write_config(_config_snapshot())


def _resolve_effective_token() -> str:
    env_token = os.environ.get("MIZUKI_TOKEN", "").strip()
    if env_token:
        return env_token
    with _state_lock:
        return config["shared_token"]


def _auth_enabled() -> bool:
    return bool(_resolve_effective_token())


def _check_token(request: Request) -> JSONResponse | None:
    expected = _resolve_effective_token()
    supplied = request.headers.get(TOKEN_HEADER, "")
    if expected and not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
        return JSONResponse({"status": "error", "message": "token 无效或缺失"}, status_code=401)
    return None


_rate_lock = threading.Lock()
_rate_windows: OrderedDict[str, deque[float]] = OrderedDict()
_RATE_LIMITS = {
    "/phone-data": (1200, 60.0),  # Up to 600/min live samples plus bounded historical replay.
    "/merged-data": (1200, 60.0),
    "/api/export/json": (12, 60.0), "/api/export/csv": (12, 60.0),
    "default": (1200, 60.0),
}


def _is_rate_limited(client_ip: str, path: str) -> bool:
    now = time.monotonic()
    limit, window = _RATE_LIMITS.get(path, _RATE_LIMITS["default"])
    key = f"{client_ip}:{path}"
    with _rate_lock:
        while _rate_windows:
            first = next(iter(_rate_windows))
            if _rate_windows[first] and _rate_windows[first][-1] > now - 60:
                break
            _rate_windows.pop(first)
        if key not in _rate_windows and len(_rate_windows) >= _MAX_RATE_KEYS:
            _rate_windows.popitem(last=False)
        timestamps = _rate_windows.setdefault(key, deque())
        _rate_windows.move_to_end(key)
        while timestamps and timestamps[0] <= now - window:
            timestamps.popleft()
        if len(timestamps) >= limit:
            return True
        timestamps.append(now)
    return False


@app.middleware("http")
async def protect_data(request: Request, call_next):
    path = request.url.path.rstrip("/")
    is_private = path.startswith("/api/") or path in ("/phone-data", "/merged-data", "/health/deep")
    if is_private:
        if _is_rate_limited(request.client.host if request.client else "unknown", path):
            return JSONResponse({"status": "error", "message": "请求过于频繁，请稍后再试"}, status_code=429,
                                headers={"Retry-After": "1"})
        denied = _check_token(request)
        if denied is not None:
            return denied
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD", "OPTIONS") and origin and urlsplit(origin).netloc != request.url.netloc:
            return JSONResponse({"status": "error", "message": "不允许跨站修改数据"}, status_code=403)
    response = await call_next(request)
    if is_private:
        response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.exception_handler(RequestValidationError)
async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Validation errors must not reflect secrets or non-finite numbers from raw inputs.
    errors = [{key: error[key] for key in ("type", "loc", "msg")} for error in exc.errors()]
    return JSONResponse({"detail": errors}, status_code=422)


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _phone_is_online() -> bool:
    with _state_lock:
        return (_phone_received_monotonic is not None and
                (time.monotonic() - _phone_received_monotonic) * 1000 <= config["phone_timeout_ms"])


def _build_state() -> dict[str, Any]:
    with _state_lock:
        phone = deepcopy(_latest_phone)
        last_seen = _phone_received_at.isoformat(timespec="seconds") if _phone_received_at else None
        is_online = (_phone_received_monotonic is not None and
                     (time.monotonic() - _phone_received_monotonic) * 1000 <= config["phone_timeout_ms"])
    return {
        "timestamp": _iso_now(), "phone": phone, "phone_connected": is_online, "phone_last_seen": last_seen,
        "computer": collector.get(),
        "server": {"started_at": _started_at.isoformat(timespec="seconds"),
                   "uptime_seconds": int(time.monotonic() - _started_monotonic), "version": VERSION},
    }


def _merged_data() -> dict[str, Any]:
    state = _build_state()
    return {key: state[key] for key in ("timestamp", "phone", "phone_connected", "computer")}


def _is_new_phone(payload: dict[str, Any], session: str | None, sequence: int | None) -> bool:
    """Called with _state_lock. Ordering metadata stays outside the phone contract."""
    device = payload["device_id"]
    if session is not None:
        order = _phone_orders.get(device)
        if order is not None:
            if session in order["retired"]:
                return False
            if session == order["session"]:
                if sequence <= order["sequence"]:
                    return False
            else:
                order["retired"].append(order["session"])
        else:
            if len(_phone_orders) >= _MAX_ORDER_DEVICES:
                _phone_orders.popitem(last=False)
            order = {"retired": deque(maxlen=_MAX_RETIRED_SESSIONS)}
            _phone_orders[device] = order
        order.update(session=session, sequence=sequence)
        _phone_orders.move_to_end(device)
        return True
    # Legacy senders have no sub-second ordering; equal timestamps retain arrival order.
    if device in _phone_orders:
        return False  # A legacy/replayed request cannot downgrade a sequence-aware stream.
    if _latest_phone.get("device_id") == device:
        incoming = datetime.fromisoformat(payload["timestamp"]).timestamp()
        current = datetime.fromisoformat(_latest_phone["timestamp"]).timestamp()
        return incoming >= current
    return True


@app.get("/")
def dashboard() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "version": VERSION, "phone_connected": _phone_is_online()}


@app.get("/health/deep")
def health_deep() -> JSONResponse:
    db = storage.status()
    collector_alive = collector.is_alive()
    plugins = _plugin_status()
    return JSONResponse({
        "status": "ok" if db["connected"] and db["alive"] and collector_alive else "degraded",
        "version": VERSION, "phone_connected": _phone_is_online(), "database": db,
        "collector": {"alive": collector_alive, "collect_foreground": collector.is_collecting_foreground},
        "plugins": {"online": sum(item["online"] for item in plugins), "total": len(plugins)},
    })


@app.get("/api/state")
async def api_state() -> JSONResponse:
    return JSONResponse(_build_state())


@app.post("/phone-data")
async def receive_phone_data(request: Request, data: PhoneData) -> JSONResponse:
    global _latest_phone, _phone_received_at, _phone_received_monotonic
    replay = request.headers.get(REPLAY_HEADER, "false").lower()
    session = request.headers.get(SESSION_HEADER)
    raw_sequence = request.headers.get(SEQUENCE_HEADER)
    if replay not in ("true", "false"):
        return JSONResponse({"message": "invalid replay header"}, status_code=422)
    if bool(session) != (raw_sequence is not None) or (session is not None and not 1 <= len(session) <= 128):
        return JSONResponse({"message": "session and sequence must be supplied together"}, status_code=422)
    try:
        sequence = int(raw_sequence) if raw_sequence is not None else None
        if sequence is not None and not 1 <= sequence <= 2**63 - 1:
            raise ValueError
        if replay == "true" and session is not None:
            raise ValueError
    except ValueError:
        return JSONResponse({"message": "invalid sample ordering headers"}, status_code=422)
    payload = data.model_dump()
    try:
        json.dumps(payload, allow_nan=False)  # Validate extras too, before JSONResponse/persistence.
    except (ValueError, TypeError):
        return JSONResponse({"message": "phone data must contain finite JSON values"}, status_code=422)
    payload["received_at"] = _iso_now()
    is_recorded = storage.record({**payload, "type": "phone"})
    is_current = False
    if replay != "true":
        with _state_lock:
            if _is_new_phone(payload, session, sequence):
                _latest_phone = payload
                _phone_received_at = datetime.now()
                _phone_received_monotonic = time.monotonic()
                is_current = True
    if not is_recorded:
        return JSONResponse({"status": "error", "message": "存储队列繁忙，请稍后重试", "current": is_current},
                            status_code=503, headers={"Retry-After": "1"})
    return JSONResponse({"status": "ok", "message": "数据已收到", "current": is_current})


@app.get("/merged-data")
async def merged_data() -> JSONResponse:
    return JSONResponse(_merged_data())


@app.get("/api/config")
async def api_config() -> JSONResponse:
    snapshot = _config_snapshot()
    public = {key: snapshot[key] for key in ("host", "port", "computer_collect_enabled", "computer_collect_interval",
                                             "phone_timeout_ms", "poll_interval")}
    public.update(auth_enabled=_auth_enabled(), token_from_env=bool(os.environ.get("MIZUKI_TOKEN", "").strip()),
                  token_configured=bool(snapshot["shared_token"]), db_file=str(storage.db_file))
    return JSONResponse(public)


@app.patch("/api/config")
def api_config_update(body: ConfigPatch) -> JSONResponse:
    updates = body.model_dump(exclude_unset=True)
    if any(value is None for value in updates.values()):
        return JSONResponse({"status": "error", "message": "配置值不能为 null"}, status_code=422)
    try:
        with _config_io_lock:
            candidate = _validate_config({**_config_snapshot(), **updates})
            _write_config(candidate)
            _apply_config(candidate)
    except (OSError, ValueError, TypeError):
        return JSONResponse({"status": "error", "message": "保存配置失败，原配置已保留"}, status_code=503)
    return JSONResponse({"status": "ok", "updated": list(updates)})


@app.get("/api/logs")
def api_logs(limit: int = 20, offset: int = 0, record_type: str | None = None) -> JSONResponse:
    return JSONResponse(storage.query(record_type=record_type, limit=max(1, min(limit, 200)), offset=max(0, offset)))


@app.get("/api/stats")
def api_stats() -> JSONResponse:
    return JSONResponse(storage.get_stats())


@app.get("/api/export/{format}")
def api_export(format: str, record_type: str | None = None):
    if format not in ("json", "csv"):
        return JSONResponse({"status": "error", "message": "不支持的格式"}, status_code=400)
    export_dir = app_dir() / "data" / "exports"
    path = export_dir / f"mizuki_export_{uuid4().hex}.{format}"
    try:
        export_dir.mkdir(parents=True, exist_ok=True)
        if format == "json":
            storage.export_json(path, record_type=record_type)
        else:
            storage.export_csv(path, record_type=record_type)
        return FileResponse(path, filename=f"mizuki_export.{format}", media_type="application/octet-stream",
                            background=BackgroundTask(path.unlink, missing_ok=True))
    except Exception:
        path.unlink(missing_ok=True)
        return JSONResponse({"status": "error", "message": "导出暂时不可用，请稍后重试"}, status_code=503)


@app.post("/api/plugin-heartbeat")
async def plugin_heartbeat(body: HeartbeatData) -> JSONResponse:
    with _state_lock:
        if body.plugin_id not in _plugin_heartbeats and len(_plugin_heartbeats) >= _MAX_PLUGIN_IDS:
            oldest = min(_plugin_heartbeats, key=lambda key: _plugin_heartbeats[key][1])
            del _plugin_heartbeats[oldest]
        _plugin_heartbeats[body.plugin_id] = (datetime.now(), time.monotonic())
    return JSONResponse({"status": "ok"})


def _plugin_status() -> list[dict[str, Any]]:
    now = time.monotonic()
    with _state_lock:
        timeout = config["plugin_heartbeat_timeout"]
        return [{"plugin_id": key, "last_seen": seen.isoformat(timespec="seconds"), "online": now - tick < timeout}
                for key, (seen, tick) in _plugin_heartbeats.items()]


@app.get("/api/plugin-status")
async def plugin_status() -> JSONResponse:
    return JSONResponse({"plugins": _plugin_status(), "timeout": _config_snapshot()["plugin_heartbeat_timeout"]})


def _computer_persistence_key(data: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(data.get(key) for key in _COMPUTER_PERSISTENCE_KEYS)


def _computer_persistence_loop() -> None:
    global _last_computer_key
    while not _services_stop.is_set():
        try:
            snapshot = _config_snapshot()
            if snapshot["computer_collect_enabled"]:
                data = collector.get()
                if data:
                    key = _computer_persistence_key(data)
                    if key != _last_computer_key and storage.record({**data, "type": "computer"}):
                        _last_computer_key = key
        except Exception:
            _log("error", "电脑状态落盘轮询失败，下一轮重试")
        _services_stop.wait(_config_snapshot()["computer_collect_interval"] / 1000)


def _config_watcher() -> None:
    last_mtime = None
    while not _services_stop.wait(5):
        try:
            mtime = CONFIG_PATH.stat().st_mtime_ns if CONFIG_PATH.exists() else None
            if mtime != last_mtime:
                candidate = load_config()
                active = _config_snapshot()
                # The bound socket cannot follow a file edit; host/port take effect on restart.
                candidate["host"], candidate["port"] = active["host"], active["port"]
                _apply_config(candidate)
                last_mtime = mtime
        except Exception:
            _log("warning", "配置热重载暂时失败，保留原配置")


def start_services() -> None:
    global _services_started, _last_computer_key
    with _services_lock:
        if _services_started:
            return
        _apply_config(load_config())
        _services_stop.clear()
        _last_computer_key = None
        storage.start()
        collector.start()
        _service_threads.clear()
        for name, target in (("computer-persistence", _computer_persistence_loop), ("config-watcher", _config_watcher)):
            thread = threading.Thread(target=target, daemon=True, name=name)
            thread.start()
            _service_threads.append(thread)
        _services_started = True


def stop_services() -> None:
    global _services_started
    with _services_lock:
        _services_stop.set()
        for thread in _service_threads:
            thread.join(timeout=2)
        collector.stop()
        storage.stop()
        _service_threads.clear()
        _services_started = False


def _is_port_available(host: str, port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host, port))
        return True
    except OSError:
        return False


def _find_available_port(host: str, preferred: int, max_search: int = 100) -> int:
    return next((port for port in range(preferred, min(65536, preferred + max_search))
                 if _is_port_available(host, port)), preferred)


def create_server() -> uvicorn.Server:
    snapshot = _config_snapshot()
    port = _find_available_port(snapshot["host"], snapshot["port"])
    if port != snapshot["port"]:
        _log("warning", f"端口 {snapshot['port']} 被占用，切换到 {port}；请同步更新手机和插件端口")
        with _state_lock:
            config["port"] = port
    return uvicorn.Server(uvicorn.Config(app, host=snapshot["host"], port=port, log_level="info", timeout_graceful_shutdown=5))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    start_services()
    server = create_server()
    print(f"Mizuki {VERSION} · http://localhost:{_config_snapshot()['port']}/")
    if not _auth_enabled():
        print("警告：共享令牌未配置，当前为无鉴权兼容模式。请配置 shared_token 或 MIZUKI_TOKEN。")
    try:
        server.run()
    finally:
        stop_services()


if __name__ == "__main__":
    main()

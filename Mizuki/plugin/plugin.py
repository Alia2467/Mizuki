"""Mizuki 插件 — MaiBot 主动感知（决策执行层）。

手机/电脑采集与汇聚由控制台负责；本插件仅判断情境、注入上下文、请求主动说话。
SDK 返回值的确认边界见 _result_accepted；未知返回不会被当作成功。
"""

import asyncio
import math
import operator
import time
from functools import reduce
from string import Formatter
from typing import Any, Callable, Literal
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx

from maibot_sdk import Field, MaiBotPlugin, PluginConfigBase

TOKEN_HEADER = "X-Sensor-Token"
HTTP_TIMEOUT_SECONDS = 3
SDK_TIMEOUT_SECONDS = 10
HEARTBEAT_INTERVAL_SECONDS = 3
RETRY_INITIAL_SECONDS = 5
RETRY_MAX_SECONDS = 60
FAILURE_LOG_INTERVAL_SECONDS = 30


class PluginSectionConfig(PluginConfigBase):
    """插件基础开关。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.1.3", description="配置版本")


class SourceConfig(PluginConfigBase):
    """电脑端汇聚服务数据源。"""

    __ui_label__ = "数据源"
    __ui_icon__ = "server"
    __ui_order__ = 1

    data_url: str = Field(default="http://localhost:821/merged-data", description="电脑端合并数据接口地址")
    token: str = Field(default="", description="共享鉴权 token（与控制台 config.json 的 shared_token 一致；控制台未启用鉴权时留空）")
    fetch_interval: int = Field(default=300, ge=100, description="数据拉取间隔（毫秒）")


class TargetConfig(PluginConfigBase):
    """主动说话目标聊天流。"""

    __ui_label__ = "主动说话目标"
    __ui_icon__ = "user"
    __ui_order__ = 2

    platform: str = Field(default="qq", description="平台标识，例如 qq")
    chat_type: Literal["private", "group"] = Field(default="private", description="聊天类型：private / group")
    user_id: str = Field(default="", description="私聊目标用户 ID（chat_type=private 时生效）")
    group_id: str = Field(default="", description="群聊目标群 ID（chat_type=group 时生效）")


class ProactiveConfig(PluginConfigBase):
    """主动说话与安静模式。"""

    __ui_label__ = "主动说话"
    __ui_icon__ = "message-circle"
    __ui_order__ = 3

    cooldown_ms: int = Field(default=180000, ge=60000, description="同一触发条件成功受理后的冷却时间（毫秒）")


class RuleSpec(PluginConfigBase):
    """声明式规则；规则键唯一，成功冷却和失败退避均按键独立。"""

    __ui_label__ = "规则条目"
    __ui_icon__ = "bell"
    __ui_order__ = 0

    key: str = Field(default="", description="规则键（必须非空且在规则表内唯一）")
    enabled: bool = Field(default=True, description="是否启用本条规则")
    field: str = Field(default="", description="合并数据字段路径，如 phone.health.heart_rate")
    op: Literal[">=", ">", "<=", "<", "==", "in"] = Field(default=">=", description="比较运算符：>= / > / <= / < / == / in")
    value: Any = Field(default=0, description="阈值；op=in 时为候选值列表")
    situation: str = Field(default="", description="注入情境模板，支持占位符 {value}")
    intent: str = Field(default="", description="主动说话意图模板，支持占位符 {value}")


def _default_rules() -> list[RuleSpec]:
    """内置规则表默认值。"""
    return [
        RuleSpec(
            key="heart_high", field="phone.health.heart_rate", op=">=", value=100,
            situation="当前心率 {value} 次/分，偏高。",
            intent="心率有点偏高，温柔地关心 TA，提醒 TA 别太累、注意休息。",
        ),
        RuleSpec(
            key="steps", field="phone.health.steps", op=">=", value=10000,
            situation="今天已经走了 {value} 步。",
            intent="今天走了很多路，心疼地关心 TA，让 TA 放松一下腿。",
        ),
        RuleSpec(
            key="weather_rain", field="phone.weather.condition", op="in",
            value=["rain", "snow", "shower", "drizzle", "thunderstorm"],
            situation="当前天气为 {value}。",
            intent="外面在下雨（或下雪），提醒 TA 出门带伞、路上注意安全。",
        ),
        RuleSpec(
            key="weather_hot", field="phone.weather.temperature", op=">=", value=35,
            situation="当前温度 {value} ℃，比较热。",
            intent="天气很热，提醒 TA 多喝水、注意防暑。",
        ),
    ]


class RulesConfig(PluginConfigBase):
    """触发规则（声明式规则表）。"""

    __ui_label__ = "触发规则"
    __ui_icon__ = "bell"
    __ui_order__ = 4

    table: list[RuleSpec] = Field(default_factory=_default_rules, description="按顺序求值，合并所有命中规则后一次触发")


class MizukiSensorConfig(PluginConfigBase):
    """Mizuki 插件总配置；跨字段校验在加载和热更新时统一执行。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    source: SourceConfig = Field(default_factory=SourceConfig)
    target: TargetConfig = Field(default_factory=TargetConfig)
    proactive: ProactiveConfig = Field(default_factory=ProactiveConfig)
    rules: RulesConfig = Field(default_factory=RulesConfig)


class MizukiSensorPlugin(MaiBotPlugin):
    """异步采集、规则求值和 SDK 调用；任务由加载/更新/卸载串行管理。"""

    config_model = MizukiSensorConfig

    def __init__(self) -> None:
        super().__init__()
        self._loop_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._http_client: httpx.AsyncClient | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._is_loaded = False
        self._is_config_valid = False
        self._generation = 0
        self._clock = time.monotonic
        self._stream_id = ""
        self._connection_status = "unknown"
        self._is_navigating: bool | None = None
        self._last_spoken: dict[str, float] = {}
        self._failures: dict[str, int] = {}
        self._retry_after: dict[str, float] = {}
        self._last_warning: dict[str, float] = {}
        # 未完成事件按批保存；同一规则最多属于一个事件，内存受规则数限制。
        self._pending: dict[str, dict[str, Any]] = {}

    @property
    def _http(self) -> httpx.AsyncClient:
        if not self._is_loaded:
            raise RuntimeError("插件已卸载")
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS)
        return self._http_client

    def _is_active(self, generation: int) -> bool:
        return (self._is_loaded and self._is_config_valid and self.config.plugin.enabled
                and generation == self._generation)

    def _build_headers(self) -> dict[str, str]:
        return {TOKEN_HEADER: self.config.source.token} if self.config.source.token else {}

    async def on_load(self) -> None:
        async with self._lifecycle_lock:
            if self._is_loaded:
                return
            self._is_loaded = True
            await self._load_runtime()
        self._get_logger().info("Mizuki 插件已加载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        del config_data  # SDK 已更新 self.config；不另存第二份配置。
        async with self._lifecycle_lock:
            if not self._is_loaded:
                return
            await self._load_runtime()
        self._get_logger().info("Mizuki 配置已更新: scope=%s version=%s", scope, version)

    async def _load_runtime(self) -> None:
        """取消旧代任务后再启动新代；重复通知不会产生并行主循环。"""
        self._is_config_valid = False
        self._generation += 1
        await self._cancel_tasks()
        self._stream_id = ""
        self._is_navigating = None
        self._pending.clear()
        self._failures.clear()
        self._retry_after.clear()
        self._last_warning.clear()
        try:
            _validate_config(self.config)
        except (ValueError, TypeError, AttributeError) as exc:
            self._connection_status = "error"
            self._get_logger().error("Mizuki 配置无效，已暂停任务: %s", exc)
            return
        self._is_config_valid = True
        await self._test_connection()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(), name="mizuki-heartbeat")
        if self.config.plugin.enabled:
            self._loop_task = asyncio.create_task(self._main_loop(), name="mizuki-rules")

    async def _cancel_tasks(self) -> None:
        tasks = [task for task in (self._loop_task, self._heartbeat_task) if task is not None]
        self._loop_task = None
        self._heartbeat_task = None
        for task in tasks:
            task.cancel()
        if tasks:
            # 同时取消、收取已结束任务的异常；某个任务失败不阻碍其余资源清理。
            await asyncio.gather(*tasks, return_exceptions=True)

    async def on_unload(self) -> None:
        async with self._lifecycle_lock:
            self._is_loaded = False
            self._is_config_valid = False
            self._generation += 1
            await self._cancel_tasks()
            self._pending.clear()
            client, self._http_client = self._http_client, None
            if client is not None:
                try:
                    await client.aclose()
                except Exception as exc:
                    self._get_logger().warning("关闭 HTTP 客户端失败: %s", type(exc).__name__)
        self._get_logger().info("Mizuki 插件已卸载")

    def _can_retry(self, key: str) -> bool:
        return self._clock() >= self._retry_after.get(key, 0)

    def _record_failure(self, key: str, message: str) -> None:
        # 计数本身也有上限，避免长时间离线形成无限增大的指数。
        attempt = min(self._failures.get(key, 0) + 1, 8)
        self._failures[key] = attempt
        delay = min(RETRY_INITIAL_SECONDS * 2 ** (attempt - 1), RETRY_MAX_SECONDS)
        now = self._clock()
        self._retry_after[key] = now + delay
        last = self._last_warning.get(key)
        if last is None or now - last >= FAILURE_LOG_INTERVAL_SECONDS:
            self._last_warning[key] = now
            self._get_logger().warning("%s；%.0f 秒后可重试", message, delay)

    def _record_success(self, key: str) -> None:
        self._failures.pop(key, None)
        self._retry_after.pop(key, None)

    async def _test_connection(self) -> None:
        try:
            url = _build_endpoint_url(self.config.source.data_url, "health")
            resp = await self._http.get(url, headers=self._build_headers())
            resp.raise_for_status()
            body = resp.json()
            if not isinstance(body, dict) or body.get("status") != "ok":
                raise ValueError("invalid health response")
            self._connection_status = "connected"
            self._record_success("health")
        except Exception as exc:
            self._connection_status = "disconnected"
            # 异常文本可能带 URL 查询或鉴权信息，只记录异常类型。
            self._record_failure("health", f"控制台连接检测失败: {type(exc).__name__}")

    async def _heartbeat_loop(self) -> None:
        generation = self._generation
        while self._is_loaded and generation == self._generation:
            await self._send_heartbeat()
            delay = max(HEARTBEAT_INTERVAL_SECONDS, self._retry_after.get("heartbeat", 0) - self._clock())
            await asyncio.sleep(delay)

    async def _send_heartbeat(self) -> None:
        if not self._is_loaded or not self._can_retry("heartbeat"):
            return
        try:
            url = _build_endpoint_url(self.config.source.data_url, "api/plugin-heartbeat")
            resp = await self._http.post(url, json={"plugin_id": "mizuki-sensor"}, headers=self._build_headers())
            resp.raise_for_status()
            self._record_success("heartbeat")
        except Exception as exc:
            self._record_failure("heartbeat", f"插件心跳失败: {type(exc).__name__}")

    async def _main_loop(self) -> None:
        generation = self._generation
        while self._is_active(generation):
            try:
                if self._can_retry("loop"):
                    await self._tick()
                    self._record_success("loop")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_failure("loop", f"Mizuki 主循环异常: {type(exc).__name__}")
            delay = max(self.config.source.fetch_interval / 1000,
                        self._retry_after.get("fetch", 0) - self._clock(),
                        self._retry_after.get("loop", 0) - self._clock())
            await asyncio.sleep(delay)

    async def _tick(self) -> None:
        generation = self._generation
        if not self._is_active(generation):
            return
        data = await self._fetch_data()
        if not self._is_active(generation) or not data:
            return
        data = _build_current_data(data)
        usage = _extract_field(data, "phone.usage") or {}
        if not isinstance(usage, dict):
            usage = {}
        is_connected = data.get("phone_connected") is True
        is_navigating = usage.get("is_navigating") is True
        is_calling = usage.get("is_calling") is True
        # 翻转检测先于安静门控。离线重置为未知，不伪造一次“导航结束”。
        previous = self._is_navigating
        self._is_navigating = is_navigating if is_connected else None
        if previous is not None and is_connected and previous != is_navigating:
            self._get_logger().debug("导航状态变化: %s -> %s", previous, is_navigating)
        # 不生成额外主动消息；进入安静状态时丢弃待触发事件，恢复后重新求值。
        if is_navigating or is_calling:
            self._pending.clear()
            return
        stream_id = await self._resolve_stream()
        if stream_id and self._is_active(generation):
            await self._evaluate_rules(stream_id, data)

    async def _evaluate_rules(self, stream_id: str, data: dict[str, Any]) -> None:
        generation = self._generation
        if not self._is_active(generation):
            return
        data = _build_current_data(data)
        eligible = {}
        for rule in self.config.rules.table:
            if not rule.enabled or not self._can_speak(rule.key):
                continue
            actual = _extract_field(data, rule.field)
            if actual is not None and _compare(actual, rule.op, rule.value):
                eligible[rule.key] = (rule, actual)
        # 条件消失、手机离线或目标改变时，旧事件不能继续发送。
        for event_id, batch in list(self._pending.items()):
            if batch["stream_id"] != stream_id or not set(batch["keys"]).issubset(eligible):
                del self._pending[event_id]
        reserved = {key for batch in self._pending.values() for key in batch["keys"]}
        keys = [key for key in eligible if key not in reserved and self._can_retry(f"rule:{key}")]
        if keys:
            event_id = f"Mizuki-sensor:{uuid4().hex}"
            self._pending[event_id] = {
                "stream_id": stream_id, "keys": keys, "has_context": False,
                "situation": " ".join(_format_template(eligible[key][0].situation, eligible[key][1]) for key in keys),
                "intent": " ".join(_format_template(eligible[key][0].intent, eligible[key][1]) for key in keys),
            }
        for event_id, batch in list(self._pending.items()):
            if not self._is_active(generation):
                return
            if all(self._can_retry(f"rule:{key}") for key in batch["keys"]):
                await self._trigger_batch(event_id, batch, generation)

    async def _trigger_batch(self, event_id: str, batch: dict[str, Any], generation: int) -> None:
        stage = "append_context"
        failure_reason = "调用异常"
        try:
            if not batch["has_context"]:
                text = f"[海月之音] {batch['situation']}"
                result = await asyncio.wait_for(self.ctx.maisaka.append_context(
                    batch["stream_id"], [{"type": "text", "content": text}],
                    visible_text=text, source_kind="plugin:Mizuki_sensor", message_id=event_id,
                ), timeout=SDK_TIMEOUT_SECONDS)
                if not _result_accepted(result):
                    failure_reason = "返回失败或未知结构"
                    raise ValueError("append_context acceptance unconfirmed")
                batch["has_context"] = True
            if not self._is_active(generation):
                return
            stage = "trigger_proactive"
            result = await asyncio.wait_for(self.ctx.maisaka.trigger_proactive(
                batch["stream_id"], batch["intent"], reason=f"触发规则:{','.join(batch['keys'])}",
                priority="normal", metadata={"triggers": batch["keys"], "event_id": event_id},
            ), timeout=SDK_TIMEOUT_SECONDS)
            if not _result_accepted(result):
                failure_reason = "返回失败或未知结构"
                raise ValueError("trigger_proactive acceptance unconfirmed")
            if not self._is_active(generation):
                return
            now = self._clock()
            for key in batch["keys"]:
                self._last_spoken[key] = now
                self._record_success(f"rule:{key}")
            self._pending.pop(event_id, None)
            self._get_logger().info("海月主动说话请求已受理: %s", batch["keys"])
        except Exception as exc:
            for key in batch["keys"]:
                self._record_failure(f"rule:{key}", f"{stage} {failure_reason}: {type(exc).__name__}")
            # 已确认的上下文保留；下次只重试触发。未确认的注入沿用同一 message_id。
            # trigger 超时后的远端去重依赖 SDK，metadata 中事件 ID 不是 exactly-once 保证。

    def _can_speak(self, trigger_key: str) -> bool:
        last = self._last_spoken.get(trigger_key)
        return last is None or self._clock() - last >= self.config.proactive.cooldown_ms / 1000

    async def _fetch_data(self) -> dict[str, Any]:
        if not self._is_loaded or not self._can_retry("fetch"):
            return {}
        try:
            resp = await self._http.get(self.config.source.data_url, headers=self._build_headers())
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                raise ValueError("merged-data must be an object")
            self._connection_status = "connected"
            self._record_success("fetch")
            return data
        except Exception as exc:
            self._connection_status = "disconnected"
            self._record_failure("fetch", f"拉取电脑端数据失败: {type(exc).__name__}")
            return {}

    async def _resolve_stream(self) -> str:
        generation = self._generation
        if not self._is_active(generation):
            return ""
        if self._stream_id:
            return self._stream_id
        if not self._can_retry("stream"):
            return ""
        target = self.config.target
        stream_id = ""
        try:
            if target.chat_type == "group":
                call = self.ctx.chat.get_stream_by_group_id(target.group_id, platform=target.platform)
            else:
                call = self.ctx.chat.get_stream_by_user_id(target.user_id, platform=target.platform)
            result = await asyncio.wait_for(call, timeout=SDK_TIMEOUT_SECONDS)
            stream_id = _extract_stream_id(result)
        except Exception:
            pass  # 同一轮先尝试回退；两种方式均失败后再记录一次退避。
        if not self._is_active(generation):
            return ""
        if not stream_id:
            try:
                result = await asyncio.wait_for(self.ctx.chat.open_session(
                    platform=target.platform, chat_type=target.chat_type,
                    user_id=target.user_id if target.chat_type == "private" else "",
                    group_id=target.group_id if target.chat_type == "group" else "",
                ), timeout=SDK_TIMEOUT_SECONDS)
                stream_id = _extract_stream_id(result)
            except Exception:
                pass
        if not self._is_active(generation):
            return ""  # 旧代查询不得写入新目标的聊天流缓存。
        if stream_id:
            self._stream_id = stream_id
            self._record_success("stream")
        else:
            self._record_failure("stream", "无法解析目标聊天流")
        return stream_id


_OPS: dict[str, Callable[[float, float], bool]] = {
    ">=": operator.ge, ">": operator.gt, "<=": operator.le, "<": operator.lt, "==": operator.eq,
}


def _build_endpoint_url(data_url: str, endpoint: str) -> str:
    """只替换最终路径段，保留反向代理前缀与查询参数，移除 HTTP 不发送的 fragment。"""
    parts = urlsplit(data_url)
    path = parts.path.rstrip("/")
    if parts.scheme not in {"http", "https"} or not parts.hostname or path.rsplit("/", 1)[-1] != "merged-data":
        raise ValueError("source.data_url 必须是 HTTP(S) /merged-data 接口地址")
    if parts.username is not None or parts.password is not None:
        raise ValueError("source.data_url 不允许内嵌账号密码，请使用 token")
    try:
        parts.port
    except ValueError:
        raise ValueError("source.data_url 端口无效") from None
    path = path.rsplit("/", 1)[0] + "/" + endpoint.lstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def _validate_config(config: MizukiSensorConfig) -> None:
    """跨字段验证不依赖 SDK 的特定 Pydantic 版本；无效配置暂停而不是静默忽略规则。"""
    _build_endpoint_url(config.source.data_url, "health")
    if config.source.fetch_interval < 100 or config.proactive.cooldown_ms < 60000:
        raise ValueError("fetch_interval 至少 100ms，cooldown_ms 至少 60000ms")
    target = config.target
    if target.chat_type not in {"private", "group"}:
        raise ValueError("target.chat_type 必须为 private 或 group")
    target_id = target.user_id if target.chat_type == "private" else target.group_id
    if config.plugin.enabled and (not target.platform.strip() or not target_id.strip()):
        raise ValueError("启用前请填写目标平台及对应的 user_id/group_id")
    keys = set()
    for rule in config.rules.table:
        if not rule.key.strip() or rule.key != rule.key.strip() or rule.key in keys:
            raise ValueError("规则 key 必须非空、无首尾空白且不可重复")
        keys.add(rule.key)
        if not rule.field or any(not part.strip() or part != part.strip() for part in rule.field.split(".")):
            raise ValueError("规则 field 必须为非空的点分字段路径")
        if rule.field.split(".")[0] not in {"phone", "computer", "phone_connected"}:
            raise ValueError("规则 field 必须指向 phone、computer 或 phone_connected")
        if rule.op == "in":
            if not isinstance(rule.value, list) or not rule.value:
                raise ValueError("规则 in 的 value 必须为非空列表")
        elif rule.op not in _OPS:
            raise ValueError("规则 op 不受支持")
        else:
            try:
                is_finite = math.isfinite(float(rule.value))
            except (TypeError, ValueError, OverflowError):
                is_finite = False
            if not is_finite:
                raise ValueError("数值规则的 value 必须为有限数值")
        for template in (rule.situation, rule.intent):
            if rule.enabled and not template.strip():
                raise ValueError("启用规则的 situation 和 intent 不可为空")
            try:
                for _, field, spec, conversion in Formatter().parse(template):
                    if field is not None and (field != "value" or "{" in spec or "}" in spec
                                              or conversion not in {None, "s", "r", "a"}):
                        raise ValueError("unsupported placeholder")
            except ValueError:
                raise ValueError("规则模板仅支持 {value} 占位符及其格式说明") from None


def _build_current_data(data: dict[str, Any]) -> dict[str, Any]:
    """仅构造插件决策视图；不改控制台数据契约或原始缓存。缺失在线标志也按离线处理。"""
    phone = data.get("phone") if data.get("phone_connected") is True else {}
    return {**data, "phone": phone if isinstance(phone, dict) else {}}


def _result_accepted(result: Any) -> bool:
    """保守的 SDK 确认边界，不把“未抛异常”或任意非空对象视为受理。

    仅识别 True、明确的 accepted/success/ok=True 或成功 status；失败标志优先。
    None、空对象和未知结构均不确认，进入有限退避。实际 SDK 若使用其他返回模型，
    须根据其公开契约在此适配并补集成测试，不能通过猜测放宽为成功。
    """
    if result is True:
        return True
    if not isinstance(result, dict):
        return False
    flags = [result.get(key) for key in ("accepted", "success", "ok")]
    status = str(result.get("status", "")).lower()
    if any(flag is False for flag in flags) or result.get("error"):
        return False
    if status in {"error", "failed", "failure", "rejected", "denied", "cancelled"}:
        return False
    # 包装层 ok=True 不能覆盖内层的拒绝或未知结果；不推断 ticket/id 等字段的语义。
    nested = [result[key] for key in ("data", "result") if key in result]
    if nested and not all(_result_accepted(value) for value in nested):
        return False
    return (any(flag is True for flag in flags)
            or status in {"ok", "success", "accepted", "queued"} or bool(nested))


def _extract_field(data: dict[str, Any], path: str) -> Any:
    """按点分路径取字段，任一层缺失返回 None。"""
    return reduce(lambda d, k: d.get(k) if isinstance(d, dict) else None, path.split("."), data)


def _compare(actual: Any, op: str, expected: Any) -> bool:
    if op == "in":
        if not isinstance(expected, list):
            return False
        return str(actual).lower() in {str(v).lower() for v in expected}
    try:
        left, right = float(actual), float(expected)
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(left) and math.isfinite(right) and _OPS.get(op, lambda a, b: False)(left, right)


def _format_template(template: str, value: Any) -> str:
    display = int(value) if isinstance(value, float) and value.is_integer() else value
    try:
        return template.format(value=display)
    except (KeyError, IndexError, ValueError, AttributeError):
        return template


def _extract_stream_id(result: Any) -> str:
    stream = result
    if isinstance(result, dict) and isinstance(result.get("stream"), dict):
        stream = result["stream"]
    if isinstance(stream, dict):
        return str(stream.get("stream_id") or stream.get("session_id") or "").strip()
    return ""


def create_plugin() -> MizukiSensorPlugin:
    return MizukiSensorPlugin()

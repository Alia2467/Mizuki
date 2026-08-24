"""海月感知插件（Mizuki）— MaiBot 主动感知（决策执行层）

职责：
- 周期从电脑端汇聚服务拉取合并数据（手机 + 电脑状态）
- 把当前情境翻译成自然语言，注入 MaiBot 的上下文
- 基于内置触发规则，请求 MaiBot 结合人设主动说话（不是固定模板，是海月自己生成）

数据流：手机 APP → 电脑端 → 本插件 → MaiBot 主动说话
"""

import asyncio
import time
from typing import Any

import httpx

from maibot_sdk import Field, MaiBotPlugin, PluginConfigBase

# 鉴权头名称（与控制台 server.py 的 TOKEN_HEADER 一致）
TOKEN_HEADER = "X-Sensor-Token"

# ----------------------------------------------------------------------
# 配置模型
# ----------------------------------------------------------------------


class PluginSectionConfig(PluginConfigBase):
    """插件基础开关。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.0.0", description="配置版本")


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
    chat_type: str = Field(default="private", description="聊天类型：private / group")
    user_id: str = Field(default="", description="私聊目标用户 ID（chat_type=private 时生效）")
    group_id: str = Field(default="", description="群聊目标群 ID（chat_type=group 时生效）")


class ProactiveConfig(PluginConfigBase):
    """主动说话与安静模式。"""

    __ui_label__ = "主动说话"
    __ui_icon__ = "message-circle"
    __ui_order__ = 3

    cooldown_ms: int = Field(default=180000, ge=60000, description="同一触发条件的冷却时间（毫秒）")


class RuleSpec(PluginConfigBase):
    """声明式规则：对合并数据某字段做条件判断，命中则注入情境并请求主动说话。"""

    __ui_label__ = "规则条目"
    __ui_icon__ = "bell"
    __ui_order__ = 0

    key: str = Field(default="", description="规则键（冷却计时按此键独立）")
    enabled: bool = Field(default=True, description="是否启用本条规则")
    field: str = Field(default="", description="合并数据字段路径，如 phone.health.heart_rate")
    op: str = Field(default=">=", description="比较运算符：>= / > / <= / < / == / in")
    value: Any = Field(default=0, description="阈值；op=in 时为候选值列表")
    situation: str = Field(default="", description="注入情境模板，支持占位符 {value}")
    intent: str = Field(default="", description="主动说话意图模板，支持占位符 {value}")


def _default_rules() -> list[RuleSpec]:
    """内置规则表默认值（与旧版硬编码规则等价的声明式表达）。"""
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
            # 候选值与契约受控词表（架构规格 §2.1）一致，不得新造词
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

    table: list[RuleSpec] = Field(default_factory=_default_rules, description="声明式规则表，按顺序求值，首个命中即触发")


class MizukiSensorConfig(PluginConfigBase):
    """海月感知插件总配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    source: SourceConfig = Field(default_factory=SourceConfig)
    target: TargetConfig = Field(default_factory=TargetConfig)
    proactive: ProactiveConfig = Field(default_factory=ProactiveConfig)
    rules: RulesConfig = Field(default_factory=RulesConfig)


# ----------------------------------------------------------------------
# 插件主体
# ----------------------------------------------------------------------


class MizukiSensorPlugin(MaiBotPlugin):
    """海月感知插件。"""

    config_model = MizukiSensorConfig

    def __init__(self) -> None:
        super().__init__()
        self._loop_task: asyncio.Task | None = None
        self._stream_id: str = ""
        self._last_navigating: bool = False
        self._last_spoken: dict[str, float] = {}
        self._connection_status: str = "unknown"
        self._http_client: httpx.AsyncClient | None = None
        self._heartbeat_task: asyncio.Task | None = None

    @property
    def _http(self) -> httpx.AsyncClient:
        """复用 httpx 客户端（避免每次请求重建连接）。"""
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=3)
        return self._http_client

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def on_load(self) -> None:
        self._get_logger().info("海月感知插件已加载")
        # 启动时检测连接状态
        await self._test_connection()
        # 启动心跳上报
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        if self.config.plugin.enabled:
            self._loop_task = asyncio.create_task(self._main_loop())

    async def on_unload(self) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        if self._heartbeat_task is not None and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None
        self._loop_task = None
        self._heartbeat_task = None
        self._get_logger().info("海月感知插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        del config_data
        # 目标配置可能变更，清空缓存让下一轮重新解析聊天流
        self._stream_id = ""
        # 配置更新后重新检测连接
        await self._test_connection()
        self._get_logger().info("海月感知配置已更新: scope=%s version=%s", scope, version)

    # ------------------------------------------------------------------
    # 连接检测
    # ------------------------------------------------------------------
    async def _test_connection(self) -> None:
        """检测与控制台的连接状态。"""
        url = self.config.source.data_url.replace("/merged-data", "/health")
        headers: dict[str, str] = {}
        if self.config.source.token:
            headers[TOKEN_HEADER] = self.config.source.token
        try:
            resp = await self._http.get(url, headers=headers)
            if resp.status_code == 200:
                body = resp.json()
                if body.get("status") == "ok":
                    self._connection_status = "connected"
                    self._get_logger().info(
                        "控制台连接成功: version=%s phone_connected=%s",
                        body.get("version", "unknown"),
                        body.get("phone_connected", "unknown"),
                    )
                else:
                    self._connection_status = "error"
                    self._get_logger().warning("控制台响应异常: %s", body)
            else:
                self._connection_status = "error"
                self._get_logger().warning("控制台返回错误: HTTP %s", resp.status_code)
        except Exception as exc:
            self._connection_status = "disconnected"
            self._get_logger().error("控制台连接失败: %s", exc)

    # ------------------------------------------------------------------
    # 心跳上报
    # ------------------------------------------------------------------
    async def _heartbeat_loop(self) -> None:
        """定期向控制台上报心跳（供 WebUI 显示插件连接状态）。"""
        heartbeat_url = self.config.source.data_url.replace("/merged-data", "/plugin-heartbeat")
        while True:
            try:
                headers: dict[str, str] = {}
                if self.config.source.token:
                    headers[TOKEN_HEADER] = self.config.source.token
                await self._http.post(heartbeat_url, json={"plugin_id": "mizuki-sensor"}, headers=headers)
            except Exception:
                pass  # 心跳失败不影响主功能
            await asyncio.sleep(30)

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    async def _main_loop(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._get_logger().error("海月感知主循环异常: %s", exc)
            await asyncio.sleep(int(self.config.source.fetch_interval) / 1000)

    async def _tick(self) -> None:
        data = await self._fetch_data()
        if not data:
            return

        stream_id = await self._resolve_stream()
        if not stream_id:
            return

        phone = data.get("phone") or {}
        usage = phone.get("usage") or {}
        computer = data.get("computer") or {}

        is_navigating = bool(usage.get("is_navigating") or computer.get("is_navigating"))
        is_calling = bool(usage.get("is_calling"))

        # 导航结束检测（nav_end）
        if self._last_navigating and not is_navigating:
            await self._proactive(
                stream_id,
                "nav_end",
                "刚刚结束了导航，应该到达目的地了。",
                "刚结束导航（可能到家了），结合你的身份温柔地问候 TA、关心 TA 是否累了。",
            )

        self._last_navigating = is_navigating

        # 安静模式：导航中/通话中不说话
        if is_navigating or is_calling:
            return

        await self._evaluate_rules(stream_id, data)

    async def _evaluate_rules(self, stream_id: str, data: dict[str, Any]) -> None:
        """按声明式规则表顺序求值，首个命中的规则触发后即结束本轮。"""
        for rule in self.config.rules.table:
            if not rule.enabled or not rule.key or not rule.field:
                continue
            actual = _extract_field(data, rule.field)
            if actual is None or not _compare(actual, rule.op, rule.value):
                continue
            await self._proactive(
                stream_id,
                rule.key,
                _format_template(rule.situation, actual),
                _format_template(rule.intent, actual),
            )
            return

    # ------------------------------------------------------------------
    # 主动说话
    # ------------------------------------------------------------------
    async def _proactive(self, stream_id: str, trigger_key: str, situation: str, intent: str) -> None:
        """注入情境上下文，并请求 MaiBot 主动说话。"""
        if not self._can_speak(trigger_key):
            return

        try:
            # 顺序是硬约束：先注入情境让海月“看见”发生了什么，再请求她主动说话
            await self.ctx.maisaka.append_context(
                stream_id,
                [{"type": "text", "content": f"[海月感知] {situation}"}],
                visible_text=f"[海月感知] {situation}",
                source_kind="plugin:Mizuki_sensor",
                message_id=f"Mizuki-sensor:{trigger_key}:{int(time.time())}",
            )
            result = await self.ctx.maisaka.trigger_proactive(
                stream_id,
                intent,
                reason=f"触发规则:{trigger_key}",
                priority="normal",
                metadata={"trigger": trigger_key},
            )
            self._last_spoken[trigger_key] = time.time()
            self._get_logger().info("海月主动说话已触发: %s → %s", trigger_key, result)
        except Exception as exc:
            self._get_logger().error("触发主动说话失败 (%s): %s", trigger_key, exc)

    def _can_speak(self, trigger_key: str) -> bool:
        last = self._last_spoken.get(trigger_key)
        if last is None:
            return True
        return time.time() - last >= self.config.proactive.cooldown_ms / 1000

    # ------------------------------------------------------------------
    # 数据拉取与聊天流解析
    # ------------------------------------------------------------------
    async def _fetch_data(self) -> dict[str, Any]:
        url = self.config.source.data_url
        headers: dict[str, str] = {}
        if self.config.source.token:
            headers[TOKEN_HEADER] = self.config.source.token
        try:
            resp = await self._http.get(url, headers=headers)
            if resp.status_code != 200:
                self._connection_status = "error"
                self._get_logger().warning("拉取数据失败: HTTP %s", resp.status_code)
                return {}
            data = resp.json()
            self._connection_status = "connected"
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            self._connection_status = "disconnected"
            self._get_logger().warning("拉取电脑端数据失败: %s", exc)
            return {}

    async def _resolve_stream(self) -> str:
        """解析主动说话目标对应的真实聊天流 ID（带缓存）。"""
        if self._stream_id:
            return self._stream_id

        target = self.config.target
        try:
            if target.chat_type == "group":
                result = await self.ctx.chat.get_stream_by_group_id(target.group_id, platform=target.platform)
            else:
                result = await self.ctx.chat.get_stream_by_user_id(target.user_id, platform=target.platform)
            stream_id = _extract_stream_id(result)
            if stream_id:
                self._stream_id = stream_id
                self._get_logger().info("已解析主动说话目标聊天流: %s", stream_id)
                return stream_id
        except Exception as exc:
            self._get_logger().warning("按 ID 查找聊天流失败，尝试打开会话: %s", exc)

        # 回退：打开或创建聊天流
        try:
            result = await self.ctx.chat.open_session(
                platform=target.platform,
                chat_type=target.chat_type,
                user_id=target.user_id if target.chat_type == "private" else "",
                group_id=target.group_id if target.chat_type == "group" else "",
            )
            stream_id = _extract_stream_id(result)
            if stream_id:
                self._stream_id = stream_id
                self._get_logger().info("已打开主动说话目标聊天流: %s", stream_id)
                return stream_id
        except Exception as exc:
            self._get_logger().error("打开目标聊天流失败: %s", exc)

        return ""


# ----------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------


def _extract_field(data: dict[str, Any], path: str) -> Any:
    """按点分路径从合并数据中取字段，任一层缺失返回 None。"""
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _compare(actual: Any, op: str, expected: Any) -> bool:
    """声明式规则的条件比较；op=in 为小写成员判定，其余为数值比较。"""
    if op == "in":
        if not isinstance(expected, list):
            return False
        return str(actual).lower() in {str(v).lower() for v in expected}
    try:
        left, right = float(actual), float(expected)
    except (TypeError, ValueError):
        return False
    if op == ">=":
        return left >= right
    if op == ">":
        return left > right
    if op == "<=":
        return left <= right
    if op == "<":
        return left < right
    if op == "==":
        return left == right
    return False


def _format_template(template: str, value: Any) -> str:
    """渲染情境/意图模板，占位符 {value}；渲染失败返回原模板。"""
    display = int(value) if isinstance(value, float) and value.is_integer() else value
    try:
        return template.format(value=display)
    except (KeyError, IndexError, ValueError):
        return template


def _extract_stream_id(result: Any) -> str:
    """从聊天流查询结果中提取 stream_id。"""
    stream = result
    if isinstance(result, dict) and isinstance(result.get("stream"), dict):
        stream = result["stream"]
    if isinstance(stream, dict):
        return str(stream.get("stream_id") or stream.get("session_id") or "").strip()
    return ""


def create_plugin() -> MizukiSensorPlugin:
    """创建海月感知插件实例。"""
    return MizukiSensorPlugin()

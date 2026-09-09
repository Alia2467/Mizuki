"""执行实际插件类的业务回归；仅宿主 SDK、HTTP 和单调时钟使用替身。

独立运行：py -B -m pytest test/test_plugin.py test/test_plugin_runtime.py --noconftest -p no:cacheprovider
Pydantic 模型是真实校验；宿主 SDK 返回模型和远端幂等性仍需另行集成验证。
"""

import asyncio
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from pydantic import ValidationError

# 也支持 pytest 的 importlib 导入模式及仅运行本文件。
_test_dir = str(Path(__file__).resolve().parent)
if _test_dir not in sys.path:
    sys.path.insert(0, _test_dir)
import test_plugin  # 安装与辅助函数测试一致的 SDK 替身。
import plugin as m


def response(status=200, data=None, url="http://console.invalid/merged-data"):
    return httpx.Response(status, json=data, request=httpx.Request("GET", url))


def phone_data(**usage):
    return {
        "phone_connected": True,
        "phone": {
            "health": {"heart_rate": 120, "steps": 0},
            "weather": {"condition": "clear", "temperature": 20},
            "usage": usage,
        },
        "computer": {"is_gaming": True},
    }


@pytest.fixture
def runtime():
    p = m.MizukiSensorPlugin()
    p.config = m.MizukiSensorConfig(target={"user_id": "mock-user"})
    p.config.source.data_url = "http://console.invalid/merged-data"
    m._validate_config(p.config)
    p._is_loaded = True
    p._is_config_valid = True
    p._clock = Mock(return_value=1000.0)
    p.log = Mock()
    p._get_logger = lambda: p.log
    p.ctx = SimpleNamespace(
        maisaka=SimpleNamespace(append_context=AsyncMock(return_value={"success": True}),
                               trigger_proactive=AsyncMock(return_value={"accepted": True})),
        chat=SimpleNamespace(get_stream_by_user_id=AsyncMock(return_value={"stream_id": "mock-stream"}),
                             get_stream_by_group_id=AsyncMock(return_value={"stream_id": "mock-group"}),
                             open_session=AsyncMock(return_value={"stream_id": "mock-fallback"})),
    )
    p._http_client = SimpleNamespace(
        get=AsyncMock(return_value=response(data=phone_data())),
        post=AsyncMock(return_value=response(data={"status": "ok"})),
        aclose=AsyncMock(),
    )
    return p


def tick(p, data=None):
    if data is not None:
        p._http_client.get.return_value = response(data=data)
    asyncio.run(p._tick())


def test_online_rule_uses_real_fetch_resolve_and_trigger(runtime):
    tick(runtime)
    runtime._http_client.get.assert_awaited_once()
    runtime.ctx.chat.get_stream_by_user_id.assert_awaited_once()
    runtime.ctx.maisaka.append_context.assert_awaited_once()
    runtime.ctx.maisaka.trigger_proactive.assert_awaited_once()
    assert runtime._last_spoken == {"heart_high": 1000.0}
    assert not runtime._pending


@pytest.mark.parametrize("connected", [False, None, "true", 0])
def test_offline_or_missing_connected_never_uses_phone_cache(runtime, connected):
    data = phone_data()
    if connected is None:
        data.pop("phone_connected")
    else:
        data["phone_connected"] = connected
    original = deepcopy(data)
    tick(runtime, data)
    runtime.ctx.maisaka.trigger_proactive.assert_not_awaited()
    assert data == original


@pytest.mark.parametrize("flag", ["is_navigating", "is_calling"])
def test_offline_cached_quiet_state_does_not_block_computer(runtime, flag):
    runtime.config.rules.table.append(m.RuleSpec(
        key="gaming", field="computer.is_gaming", op="==", value=True,
        situation="电脑游戏状态 {value}", intent="结合当前电脑状态判断是否关心",
    ))
    data = phone_data(**{flag: True})
    data["phone_connected"] = False
    tick(runtime, data)
    args = runtime.ctx.maisaka.trigger_proactive.await_args
    assert args.kwargs["metadata"]["triggers"] == ["gaming"]
    assert "heart_high" not in runtime._last_spoken
    assert runtime._is_navigating is None


@pytest.mark.parametrize("flag", ["is_navigating", "is_calling"])
def test_online_quiet_gate_precedes_stream_sdk_and_clears_pending(runtime, flag):
    runtime._pending["old-event"] = {"keys": ["heart_high"]}
    tick(runtime, phone_data(**{flag: True}))
    runtime.ctx.chat.get_stream_by_user_id.assert_not_awaited()
    runtime.ctx.maisaka.append_context.assert_not_awaited()
    runtime.ctx.maisaka.trigger_proactive.assert_not_awaited()
    assert not runtime._pending


def test_navigation_transition_is_recorded_before_quiet_return(runtime):
    data = phone_data(is_navigating=False)
    data["phone"]["health"]["heart_rate"] = 60
    tick(runtime, data)
    assert runtime._is_navigating is False
    data["phone"]["usage"]["is_navigating"] = True
    tick(runtime, data)
    assert runtime._is_navigating is True
    runtime.log.debug.assert_called_once_with("导航状态变化: %s -> %s", False, True)
    data["phone"]["usage"]["is_navigating"] = False
    tick(runtime, data)
    assert runtime._is_navigating is False
    assert runtime.log.debug.call_count == 2
    runtime.ctx.maisaka.trigger_proactive.assert_not_awaited()


def test_disconnect_resets_navigation_without_synthetic_transition(runtime):
    tick(runtime, phone_data(is_navigating=True))
    data = phone_data(is_navigating=True)
    data["phone_connected"] = False
    tick(runtime, data)
    assert runtime._is_navigating is None
    runtime.log.debug.assert_not_called()


def test_multirule_order_and_single_trigger(runtime):
    data = phone_data()
    data["phone"]["health"]["steps"] = 12000
    data["phone"]["weather"] = {"condition": "rain", "temperature": 38}
    order = []

    async def append(*args, **kwargs):
        order.append("append")
        return True

    async def trigger(*args, **kwargs):
        order.append("trigger")
        return {"accepted": True}

    runtime.ctx.maisaka.append_context.side_effect = append
    runtime.ctx.maisaka.trigger_proactive.side_effect = trigger
    tick(runtime, data)
    assert order == ["append", "trigger"]
    args = runtime.ctx.maisaka.trigger_proactive.await_args
    assert args.kwargs["metadata"]["triggers"] == ["heart_high", "steps", "weather_rain", "weather_hot"]
    assert len(runtime._last_spoken) == 4


def test_success_cooldown_is_independent_and_uses_monotonic(runtime, monkeypatch):
    tick(runtime)
    data = phone_data()
    data["phone"]["health"]["steps"] = 12000
    monkeypatch.setattr(m.time, "time", lambda: -999999999)
    runtime._clock.return_value = 1001
    tick(runtime, data)
    assert runtime.ctx.maisaka.trigger_proactive.await_args.kwargs["metadata"]["triggers"] == ["steps"]
    assert runtime.ctx.maisaka.trigger_proactive.await_count == 2
    runtime._clock.return_value = 1180
    tick(runtime, data)
    assert runtime.ctx.maisaka.trigger_proactive.await_args.kwargs["metadata"]["triggers"] == ["heart_high"]


@pytest.mark.parametrize("failed_call", ["append_context", "trigger_proactive"])
@pytest.mark.parametrize("failure", [RuntimeError("mock failure"), False, {"accepted": False},
                                     {"success": False}, {"status": "error"}, None, {}])
def test_sdk_failures_do_not_commit_cooldown_and_do_back_off(runtime, failed_call, failure):
    call = getattr(runtime.ctx.maisaka, failed_call)
    if isinstance(failure, Exception):
        call.side_effect = failure
    else:
        call.return_value = failure
    tick(runtime)
    assert not runtime._last_spoken
    assert runtime._can_speak("heart_high")
    assert runtime._retry_after["rule:heart_high"] == 1005
    tick(runtime)
    assert call.await_count == 1
    if failed_call == "append_context":
        runtime.ctx.maisaka.trigger_proactive.assert_not_awaited()


def test_append_failure_reuses_event_id_and_recovers(runtime):
    runtime.ctx.maisaka.append_context.side_effect = [RuntimeError("mock"), {"ok": True}]
    tick(runtime)
    first_id = runtime.ctx.maisaka.append_context.await_args.kwargs["message_id"]
    runtime._clock.return_value = 1005
    tick(runtime)
    assert runtime.ctx.maisaka.append_context.await_args.kwargs["message_id"] == first_id
    assert runtime._last_spoken == {"heart_high": 1005}
    assert "rule:heart_high" not in runtime._failures


@pytest.mark.parametrize("failure", [RuntimeError("mock"), {"accepted": False}, None])
def test_trigger_retry_never_reappends_confirmed_context(runtime, failure):
    runtime.ctx.maisaka.trigger_proactive.side_effect = [failure, {"accepted": True}]
    tick(runtime)
    event_id = runtime.ctx.maisaka.trigger_proactive.await_args.kwargs["metadata"]["event_id"]
    runtime._clock.return_value = 1005
    tick(runtime)
    runtime.ctx.maisaka.append_context.assert_awaited_once()
    assert runtime.ctx.maisaka.trigger_proactive.await_count == 2
    assert runtime.ctx.maisaka.trigger_proactive.await_args.kwargs["metadata"]["event_id"] == event_id
    assert runtime._last_spoken == {"heart_high": 1005}
    assert not runtime._pending


def test_failed_rule_does_not_delay_an_independent_new_rule(runtime):
    runtime.ctx.maisaka.trigger_proactive.side_effect = [{"accepted": False}, {"accepted": True}]
    tick(runtime)
    data = phone_data()
    data["phone"]["health"]["steps"] = 12000
    runtime._clock.return_value = 1001
    tick(runtime, data)
    assert runtime.ctx.maisaka.trigger_proactive.await_args.kwargs["metadata"]["triggers"] == ["steps"]
    assert runtime._last_spoken == {"steps": 1001}
    assert len(runtime._pending) == 1


@pytest.mark.parametrize("change", ["offline", "condition", "quiet"])
def test_obsolete_pending_phone_event_is_discarded(runtime, change):
    runtime.ctx.maisaka.trigger_proactive.return_value = {"accepted": False}
    tick(runtime)
    assert runtime._pending
    data = phone_data()
    if change == "offline":
        data["phone_connected"] = False
    elif change == "condition":
        data["phone"]["health"]["heart_rate"] = 50
    else:
        data["phone"]["usage"]["is_calling"] = True
    runtime._clock.return_value = 1005
    tick(runtime, data)
    assert not runtime._pending
    assert runtime.ctx.maisaka.trigger_proactive.await_count == 1


def test_disabled_and_unloaded_tick_have_no_side_effect(runtime):
    runtime.config.plugin.enabled = False
    tick(runtime)
    runtime._http_client.get.assert_not_awaited()
    runtime.config.plugin.enabled = True
    runtime._is_loaded = False
    tick(runtime)
    runtime._http_client.get.assert_not_awaited()
    runtime.ctx.maisaka.trigger_proactive.assert_not_awaited()


def test_disabled_during_append_does_not_trigger(runtime):
    async def append(*args, **kwargs):
        runtime.config.plugin.enabled = False
        return True

    runtime.ctx.maisaka.append_context.side_effect = append
    tick(runtime)
    runtime.ctx.maisaka.trigger_proactive.assert_not_awaited()
    assert not runtime._last_spoken


def test_old_generation_stream_resolution_does_not_pollute_cache(runtime):
    async def resolve(*args, **kwargs):
        runtime._generation += 1
        return {"stream_id": "old-stream"}

    runtime.ctx.chat.get_stream_by_user_id.side_effect = resolve
    tick(runtime)
    assert runtime._stream_id == ""
    runtime.ctx.chat.open_session.assert_not_awaited()
    runtime.ctx.maisaka.trigger_proactive.assert_not_awaited()


@pytest.mark.parametrize("stage", ["append_context", "trigger_proactive"])
def test_sdk_timeout_recovers_without_success_cooldown(runtime, monkeypatch, stage):
    monkeypatch.setattr(m, "SDK_TIMEOUT_SECONDS", 0.001)

    async def blocked(*args, **kwargs):
        await asyncio.Event().wait()

    getattr(runtime.ctx.maisaka, stage).side_effect = blocked
    tick(runtime)
    assert not runtime._last_spoken
    assert runtime._retry_after["rule:heart_high"] == 1005


@pytest.mark.parametrize("result, accepted", [
    (True, True), ({"accepted": True}, True), ({"success": True}, True), ({"ok": True}, True),
    ({"status": "queued"}, True), (False, False), (None, False), ({}, False),
    ({"opaque": "value"}, False), ({"accepted": True, "success": False}, False),
    ({"accepted": True, "status": "rejected"}, False), ({"ok": True, "error": "denied"}, False),
    ({"ok": True, "data": {"accepted": False}}, False),
    ({"ok": True, "data": {"ticket": "opaque"}}, False),
    ({"ok": True, "data": {"accepted": True}}, True),
    ({"result": {"success": True}}, True),
])
def test_sdk_acceptance_is_explicit_and_failure_dominates(result, accepted):
    assert m._result_accepted(result) is accepted


@pytest.mark.parametrize("url, expected", [
    ("http://example.invalid/merged-data", "http://example.invalid/api/plugin-heartbeat"),
    ("https://example.invalid/base/merged-data/", "https://example.invalid/base/api/plugin-heartbeat"),
    ("https://example.invalid/base/merged-data?q=/merged-data#frag",
     "https://example.invalid/base/api/plugin-heartbeat?q=/merged-data"),
])
def test_endpoint_url_preserves_prefix_and_query(url, expected):
    assert m._build_endpoint_url(url, "api/plugin-heartbeat") == expected


@pytest.mark.parametrize("status", [302, 400, 404, 500])
def test_heartbeat_non_2xx_backs_off_and_limits_logs(runtime, status):
    runtime._http_client.post.return_value = response(status=status)
    asyncio.run(runtime._send_heartbeat())
    assert runtime._retry_after["heartbeat"] == 1005
    assert runtime.log.warning.call_count == 1
    asyncio.run(runtime._send_heartbeat())
    assert runtime._http_client.post.await_count == 1
    runtime._clock.return_value = 1005
    asyncio.run(runtime._send_heartbeat())
    assert runtime._http_client.post.await_count == 2
    assert runtime.log.warning.call_count == 1


def test_heartbeat_uses_updated_url_and_headers_and_recovers(runtime):
    async def probe():
        urls = []

        def handler(request):
            urls.append((str(request.url), request.headers.get(m.TOKEN_HEADER)))
            return httpx.Response(200, json={"status": "ok"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as client:
            runtime._http_client = client
            await runtime._send_heartbeat()
            runtime.config.source.data_url = "https://new.invalid/base/merged-data?q=/merged-data"
            runtime.config.source.token = "mock-only-token"
            await runtime._send_heartbeat()
        assert urls == [("http://console.invalid/api/plugin-heartbeat", None),
                        ("https://new.invalid/base/api/plugin-heartbeat?q=/merged-data", "mock-only-token")]

    asyncio.run(probe())


def test_fetch_recovers_after_http_timeout_and_invalid_payloads(runtime):
    async def probe():
        outcomes = iter([httpx.Response(503), httpx.ReadTimeout("mock"),
                         httpx.Response(200, content=b"{"), httpx.Response(200, json=[]),
                         httpx.Response(200, json=phone_data())])
        count = 0

        def handler(request):
            nonlocal count
            count += 1
            value = next(outcomes)
            if isinstance(value, Exception):
                raise value
            return value

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as client:
            runtime._http_client = client
            for _ in range(4):
                assert await runtime._fetch_data() == {}
                assert runtime._connection_status == "disconnected"
                previous = count
                assert await runtime._fetch_data() == {}
                assert count == previous
                runtime._clock.return_value = runtime._retry_after["fetch"]
            assert await runtime._fetch_data() == phone_data()
            assert runtime._connection_status == "connected"
            assert "fetch" not in runtime._retry_after
            assert "fetch" not in runtime._failures

    asyncio.run(probe())


def test_backoff_and_log_volume_are_bounded(runtime):
    for _ in range(100):
        runtime._record_failure("fetch", "mock failure")
        assert runtime._retry_after["fetch"] - runtime._clock() <= m.RETRY_MAX_SECONDS
    assert runtime.log.warning.call_count == 1
    assert runtime._failures["fetch"] <= 8


def test_empty_dict_and_non_dict_phone_are_safe(runtime):
    tick(runtime, {})
    runtime.ctx.maisaka.trigger_proactive.assert_not_awaited()
    tick(runtime, {"phone_connected": True, "phone": "invalid"})
    runtime.ctx.maisaka.trigger_proactive.assert_not_awaited()


def test_stream_failure_backs_off_and_group_fallback_recovers(runtime):
    runtime.config.target.chat_type = "group"
    runtime.config.target.group_id = "mock-group-id"
    runtime.ctx.chat.get_stream_by_group_id.side_effect = RuntimeError("mock")
    runtime.ctx.chat.open_session.side_effect = [RuntimeError("mock"), {"stream_id": "recovered"}]
    tick(runtime)
    tick(runtime)
    assert runtime.ctx.chat.open_session.await_count == 1
    runtime._clock.return_value = 1005
    tick(runtime)
    assert runtime._stream_id == "recovered"
    assert runtime.ctx.chat.open_session.await_args.kwargs["group_id"] == "mock-group-id"
    runtime.ctx.maisaka.trigger_proactive.assert_awaited_once()


def test_lifecycle_updates_are_idempotent_and_unload_closes_client(runtime):
    async def probe():
        runtime._is_loaded = False
        runtime._test_connection = AsyncMock()
        started = asyncio.Event()

        async def waiting_loop():
            started.set()
            await asyncio.Event().wait()

        runtime._main_loop = AsyncMock(side_effect=waiting_loop)
        runtime._heartbeat_loop = AsyncMock(side_effect=waiting_loop)
        await runtime.on_load()
        await started.wait()
        first_main, first_heartbeat = runtime._loop_task, runtime._heartbeat_task
        await runtime.on_load()
        assert runtime._loop_task is first_main
        assert runtime._heartbeat_task is first_heartbeat
        runtime.config.plugin.enabled = False
        await runtime.on_config_update("global", {}, "2")
        assert first_main.done() and first_heartbeat.done()
        assert runtime._loop_task is None
        disabled_heartbeat = runtime._heartbeat_task
        runtime.config.plugin.enabled = True
        await runtime.on_config_update("global", {}, "3")
        assert disabled_heartbeat.done()
        assert runtime._loop_task is not None
        active_main, active_heartbeat = runtime._loop_task, runtime._heartbeat_task
        await runtime.on_config_update("global", {}, "3")
        assert active_main.done() and active_heartbeat.done()
        final_main, final_heartbeat = runtime._loop_task, runtime._heartbeat_task
        client = runtime._http_client
        await runtime.on_unload()
        assert final_main.done() and final_heartbeat.done()
        assert runtime._loop_task is None and runtime._heartbeat_task is None
        assert runtime._http_client is None
        client.aclose.assert_awaited_once()
        await runtime.on_unload()
        client.aclose.assert_awaited_once()
        await runtime.on_config_update("global", {}, "4")
        assert runtime._loop_task is None

    asyncio.run(probe())


def test_real_main_loop_disable_cancels_inflight_append(runtime):
    async def probe():
        runtime._is_loaded = False
        runtime._test_connection = AsyncMock()
        entered = asyncio.Event()

        async def append(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        runtime.ctx.maisaka.append_context.side_effect = append
        await runtime.on_load()
        await asyncio.wait_for(entered.wait(), timeout=1)
        old_loop = runtime._loop_task
        runtime.config.plugin.enabled = False
        await runtime.on_config_update("global", {}, "2")
        assert old_loop.done()
        assert runtime._loop_task is None
        runtime.ctx.maisaka.trigger_proactive.assert_not_awaited()
        await runtime.on_unload()

    asyncio.run(probe())


def test_invalid_update_stops_existing_tasks_without_sdk_calls(runtime):
    async def probe():
        runtime._is_loaded = False
        runtime._test_connection = AsyncMock()
        runtime._main_loop = AsyncMock(side_effect=asyncio.Event().wait)
        runtime._heartbeat_loop = AsyncMock(side_effect=asyncio.Event().wait)
        await runtime.on_load()
        old_loop = runtime._loop_task
        runtime.config.target.user_id = ""
        await runtime.on_config_update("global", {}, "2")
        assert old_loop.done()
        assert not runtime._is_config_valid
        assert runtime._loop_task is None and runtime._heartbeat_task is None
        await runtime._tick()
        runtime.ctx.maisaka.trigger_proactive.assert_not_awaited()
        await runtime.on_unload()

    asyncio.run(probe())


def test_unload_retrieves_failed_task_and_handles_close_failure(runtime):
    async def probe():
        async def failed():
            raise RuntimeError("mock task failure")

        runtime._loop_task = asyncio.create_task(failed())
        await asyncio.gather(runtime._loop_task, return_exceptions=True)
        runtime._heartbeat_task = asyncio.create_task(asyncio.Event().wait())
        heartbeat = runtime._heartbeat_task
        runtime._http_client.aclose.side_effect = RuntimeError("mock close failure")
        await runtime.on_unload()
        assert heartbeat.done()
        assert runtime._http_client is None
        assert runtime._loop_task is None and runtime._heartbeat_task is None

    asyncio.run(probe())


@pytest.mark.parametrize("change", [
    "duplicate_key", "empty_key", "empty_field", "invalid_root", "invalid_op", "invalid_in",
    "in_empty", "non_numeric", "infinite", "missing_target", "invalid_chat_type", "bad_template",
    "blank_template", "bad_conversion", "bad_source", "embedded_password", "bad_port", "fast_fetch", "short_cooldown",
])
def test_runtime_cross_field_validation_rejects_invalid_configuration(runtime, change):
    config = runtime.config
    rule = config.rules.table[0]
    if change == "duplicate_key":
        config.rules.table[1].key = rule.key
    elif change == "empty_key":
        rule.key = " "
    elif change == "empty_field":
        rule.field = "phone..health"
    elif change == "invalid_root":
        rule.field = "private.secret"
    elif change == "invalid_op":
        rule.op = "!="
    elif change == "invalid_in":
        rule.op, rule.value = "in", "rain"
    elif change == "in_empty":
        rule.op, rule.value = "in", []
    elif change == "non_numeric":
        rule.value = "high"
    elif change == "infinite":
        rule.value = float("inf")
    elif change == "missing_target":
        config.target.user_id = ""
    elif change == "invalid_chat_type":
        config.target.chat_type = "typo"
    elif change == "bad_template":
        rule.intent = "{value.__class__}"
    elif change == "blank_template":
        rule.intent = " "
    elif change == "bad_conversion":
        rule.intent = "{value!z}"
    elif change == "bad_source":
        config.source.data_url = "file:///merged-data"
    elif change == "embedded_password":
        config.source.data_url = "https://user:mock-password@example.invalid/merged-data"
    elif change == "bad_port":
        config.source.data_url = "https://example.invalid:bad/merged-data"
    elif change == "fast_fetch":
        config.source.fetch_interval = 1
    elif change == "short_cooldown":
        config.proactive.cooldown_ms = 1
    with pytest.raises(ValueError):
        m._validate_config(config)


def test_disabled_template_may_omit_target(runtime):
    runtime.config.plugin.enabled = False
    runtime.config.target.user_id = ""
    m._validate_config(runtime.config)


@pytest.mark.parametrize("config", [
    {"target": {"chat_type": "typo"}}, {"source": {"fetch_interval": 1}},
    {"proactive": {"cooldown_ms": 1}}, {"rules": {"table": [{"op": "unknown"}]}},
])
def test_config_model_rejects_invalid_types_and_bounds(config):
    with pytest.raises(ValidationError):
        m.MizukiSensorConfig(**config)


def test_config_model_defaults_construct_real_rules():
    config = m.MizukiSensorConfig(target={"user_id": "mock-user"})
    m._validate_config(config)
    assert config.plugin.config_version == "1.1.3"
    assert [rule.key for rule in config.rules.table] == ["heart_high", "steps", "weather_rain", "weather_hot"]


@pytest.mark.parametrize("actual", [float("inf"), float("nan"), -float("inf")])
def test_nonfinite_sensor_values_do_not_trigger_numeric_rules(actual):
    assert not m._compare(actual, ">=", 100)

"""插件核心逻辑单元测试（plugin.py 的纯函数，无需 maibot_sdk）。"""

import sys
import types
from pathlib import Path

import pytest

# 把 plugin/ 加入 sys.path
_plugin_dir = Path(__file__).resolve().parent.parent / "Mizuki" / "plugin"
if str(_plugin_dir) not in sys.path:
    sys.path.insert(0, str(_plugin_dir))

# Mock maibot_sdk（测试环境不安装该 SDK）
if "maibot_sdk" not in sys.modules:
    mock_sdk = types.ModuleType("maibot_sdk")

    class _Field:
        def __init__(self, default=None, description="", **kwargs):
            self.default = default
            self.description = description

    class _Base:
        pass

    class _Plugin(_Base):
        pass

    mock_sdk.Field = _Field
    mock_sdk.MaiBotPlugin = _Plugin
    mock_sdk.PluginConfigBase = _Base
    sys.modules["maibot_sdk"] = mock_sdk

# 导入纯函数
from plugin import _compare, _extract_field, _extract_stream_id, _format_template


# ── _extract_field ─────────────────────────────────────────────────────

class TestExtractField:
    def test_simple_key(self):
        data = {"name": "test"}
        assert _extract_field(data, "name") == "test"

    def test_nested_path(self):
        data = {"phone": {"health": {"heart_rate": 85}}}
        assert _extract_field(data, "phone.health.heart_rate") == 85

    def test_missing_key_returns_none(self):
        data = {"phone": {}}
        assert _extract_field(data, "phone.health.heart_rate") is None

    def test_deeply_missing_returns_none(self):
        data = {}
        assert _extract_field(data, "a.b.c.d") is None

    def test_non_dict_intermediate_returns_none(self):
        data = {"phone": "not_a_dict"}
        assert _extract_field(data, "phone.health") is None

    def test_zero_value_is_not_none(self):
        data = {"health": {"steps": 0}}
        assert _extract_field(data, "health.steps") == 0

    def test_false_value_is_not_none(self):
        data = {"usage": {"is_navigating": False}}
        assert _extract_field(data, "usage.is_navigating") is False

    def test_empty_string_value(self):
        data = {"location": {"city": ""}}
        assert _extract_field(data, "location.city") == ""


# ── _compare ───────────────────────────────────────────────────────────

class TestCompare:
    def test_gte_true(self):
        assert _compare(100, ">=", 100) is True

    def test_gte_false(self):
        assert _compare(99, ">=", 100) is False

    def test_gt(self):
        assert _compare(101, ">", 100) is True
        assert _compare(100, ">", 100) is False

    def test_lte(self):
        assert _compare(99, "<=", 100) is True
        assert _compare(100, "<=", 100) is True

    def test_lt(self):
        assert _compare(99, "<", 100) is True
        assert _compare(100, "<", 100) is False

    def test_eq(self):
        assert _compare(100, "==", 100) is True
        assert _compare(99, "==", 100) is False

    def test_in_list(self):
        assert _compare("rain", "in", ["rain", "snow"]) is True

    def test_not_in_list(self):
        assert _compare("clear", "in", ["rain", "snow"]) is False

    def test_in_case_insensitive(self):
        assert _compare("Rain", "in", ["rain", "snow"]) is True

    def test_in_with_non_list_expected(self):
        assert _compare("rain", "in", "rain") is False

    def test_non_numeric_returns_false(self):
        assert _compare("abc", ">=", 100) is False

    def test_unknown_op_returns_false(self):
        assert _compare(100, "!=", 50) is False


# ── _format_template ──────────────────────────────────────────────────

class TestFormatTemplate:
    def test_replace_placeholder(self):
        assert _format_template("心率 {value} 次/分", 100) == "心率 100 次/分"

    def test_float_integer_display(self):
        assert _format_template("走了 {value} 步", 10000.0) == "走了 10000 步"

    def test_float_decimal_display(self):
        assert _format_template("睡了 {value} 小时", 7.5) == "睡了 7.5 小时"

    def test_string_value(self):
        assert _format_template("天气: {value}", "rain") == "天气: rain"

    def test_no_placeholder_returns_original(self):
        assert _format_template("无占位符文本", 100) == "无占位符文本"

    def test_malformed_template_returns_original(self):
        # 模板格式错误时返回原模板，不抛异常
        assert _format_template("{invalid", 100) == "{invalid"


# ── _extract_stream_id ────────────────────────────────────────────────

class TestExtractStreamId:
    def test_dict_with_stream_id(self):
        assert _extract_stream_id({"stream": {"stream_id": "abc123"}}) == "abc123"

    def test_dict_with_session_id(self):
        assert _extract_stream_id({"stream": {"session_id": "xyz789"}}) == "xyz789"

    def test_flat_dict_with_stream_id(self):
        assert _extract_stream_id({"stream_id": "flat123"}) == "flat123"

    def test_empty_dict_returns_empty(self):
        assert _extract_stream_id({}) == ""

    def test_non_dict_returns_empty(self):
        assert _extract_stream_id("not_a_dict") == ""

    def test_none_returns_empty(self):
        assert _extract_stream_id(None) == ""

    def test_whitespace_stripped(self):
        assert _extract_stream_id({"stream_id": "  abc  "}) == "abc"

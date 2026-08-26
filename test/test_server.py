"""控制台接口测试（server.py）。"""

import json
import time


# ── /health ────────────────────────────────────────────────────────────

class TestHealth:
    def test_returns_ok(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert "version" in body
        assert "phone_connected" in body

    def test_phone_offline_by_default(self, client):
        resp = client.get("/health")
        assert resp.json()["phone_connected"] is False


# ── /api/config ────────────────────────────────────────────────────────

class TestConfig:
    def test_get_config_returns_defaults(self, client):
        resp = client.get("/api/config")
        assert resp.status_code == 200
        body = resp.json()
        assert body["port"] == 821
        assert body["computer_collect_interval"] == 300
        assert body["phone_timeout_ms"] == 10000
        assert body["poll_interval"] == 5000
        assert body["auth_enabled"] is False

    def test_patch_config_updates_interval(self, client):
        resp = client.patch("/api/config", json={"computer_collect_interval": 500})
        assert resp.status_code == 200
        assert "computer_collect_interval" in resp.json()["updated"]
        # 读回来确认
        body = client.get("/api/config").json()
        assert body["computer_collect_interval"] == 500

    def test_patch_config_updates_timeout(self, client):
        resp = client.patch("/api/config", json={"phone_timeout_ms": 120000})
        assert resp.status_code == 200
        assert client.get("/api/config").json()["phone_timeout_ms"] == 120000

    def test_patch_config_updates_token(self, client):
        resp = client.patch("/api/config", json={"shared_token": "abc123"})
        assert resp.status_code == 200
        body = client.get("/api/config").json()
        assert body["shared_token"] == "abc123"
        assert body["auth_enabled"] is True

    def test_patch_config_updates_poll_interval(self, client):
        resp = client.patch("/api/config", json={"poll_interval": 500})
        assert resp.status_code == 200
        assert client.get("/api/config").json()["poll_interval"] == 500

    def test_patch_config_clamps_minimum(self, client):
        resp = client.patch("/api/config", json={"computer_collect_interval": 0})
        assert resp.status_code == 200
        assert client.get("/api/config").json()["computer_collect_interval"] == 100

    def test_patch_config_persists(self, client, tmp_path):
        client.patch("/api/config", json={"phone_timeout_ms": 200000})
        cfg_file = tmp_path / "config.json"
        assert cfg_file.exists()
        saved = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert saved["phone_timeout_ms"] == 200000


# ── /api/state ─────────────────────────────────────────────────────────

class TestState:
    def test_returns_expected_keys(self, client):
        resp = client.get("/api/state")
        assert resp.status_code == 200
        body = resp.json()
        assert "timestamp" in body
        assert "phone" in body
        assert "phone_connected" in body
        assert "computer" in body
        assert "server" in body

    def test_server_section_has_version(self, client):
        body = client.get("/api/state").json()
        assert "version" in body["server"]
        assert "uptime_seconds" in body["server"]


# ── POST /phone-data ───────────────────────────────────────────────────

PHONE_PAYLOAD = {
    "device_id": "test-device",
    "timestamp": "2026-08-23T12:00:00",
    "location": {"city": "测试城", "latitude": 34.0, "longitude": 113.0},
    "weather": {"condition": "rain", "temperature": 25, "humidity": 80},
    "health": {"heart_rate": 72, "steps": 5000, "sleep_hours": 7.5},
    "usage": {
        "foreground_app": "测试应用",
        "is_navigating": False,
        "is_calling": False,
        "is_listening_music": False,
        "music_app": "",
        "screen_text": "",
    },
    "diagnostics": {"app_version": "1.0.0", "send_success": 10, "send_failed": 0},
}


class TestPhoneData:
    def test_post_phone_data_success(self, client):
        resp = client.post("/phone-data", json=PHONE_PAYLOAD)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_phone_becomes_online_after_post(self, client):
        client.post("/phone-data", json=PHONE_PAYLOAD)
        body = client.get("/health").json()
        assert body["phone_connected"] is True

    def test_phone_data_appears_in_state(self, client):
        client.post("/phone-data", json=PHONE_PAYLOAD)
        body = client.get("/api/state").json()
        assert body["phone"]["device_id"] == "test-device"
        assert body["phone"]["location"]["city"] == "测试城"

    def test_phone_data_appears_in_merged(self, client):
        client.post("/phone-data", json=PHONE_PAYLOAD)
        body = client.get("/merged-data").json()
        assert body["phone"]["device_id"] == "test-device"
        assert body["phone_connected"] is True

    def test_phone_data_recorded_to_storage(self, client, tmp_path):
        import server
        client.post("/phone-data", json=PHONE_PAYLOAD)
        # 等写入完成
        time.sleep(0.5)
        # 验证数据已写入 SQLite
        records = server.storage.query(record_type="phone", limit=1)
        assert len(records) >= 1
        record = json.loads(records[0]["data"])
        assert record["type"] == "phone"
        assert record["device_id"] == "test-device"

    def test_post_missing_device_id_returns_422(self, client):
        payload = {**PHONE_PAYLOAD}
        del payload["device_id"]
        resp = client.post("/phone-data", json=payload)
        assert resp.status_code == 422

    def test_post_with_defaults_fills_missing_sections(self, client):
        """契约保证五段结构必在，缺字段走默认值。"""
        minimal = {"device_id": "x", "timestamp": "2026-01-01T00:00:00"}
        resp = client.post("/phone-data", json=minimal)
        assert resp.status_code == 200
        body = client.get("/api/state").json()
        assert body["phone"]["location"]["city"] == "未知"
        assert body["phone"]["weather"]["condition"] == "unknown"


# ── 鉴权 ───────────────────────────────────────────────────────────────

class TestAuth:
    def _enable_token(self, client):
        client.patch("/api/config", json={"shared_token": "secret"})

    def test_no_token_rejected_when_enabled(self, client):
        self._enable_token(client)
        resp = client.post("/phone-data", json=PHONE_PAYLOAD)
        assert resp.status_code == 401

    def test_wrong_token_rejected(self, client):
        self._enable_token(client)
        resp = client.post(
            "/phone-data",
            json=PHONE_PAYLOAD,
            headers={"X-Sensor-Token": "wrong"},
        )
        assert resp.status_code == 401

    def test_correct_token_accepted(self, client):
        self._enable_token(client)
        resp = client.post(
            "/phone-data",
            json=PHONE_PAYLOAD,
            headers={"X-Sensor-Token": "secret"},
        )
        assert resp.status_code == 200

    def test_merged_data_requires_token(self, client):
        self._enable_token(client)
        resp = client.get("/merged-data")
        assert resp.status_code == 401

    def test_merged_data_accepts_token(self, client):
        self._enable_token(client)
        resp = client.get("/merged-data", headers={"X-Sensor-Token": "secret"})
        assert resp.status_code == 200


# ── /api/logs ──────────────────────────────────────────────────────────

class TestLogs:
    def test_empty_logs(self, client):
        resp = client.get("/api/logs")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_logs_after_phone_data(self, client):
        client.post("/phone-data", json=PHONE_PAYLOAD)
        time.sleep(0.5)
        logs = client.get("/api/logs").json()
        assert len(logs) >= 1
        assert logs[0]["type"] == "phone"


# ── 离线判定 ──────────────────────────────────────────────────────────

class TestOfflineDetection:
    def test_phone_goes_offline_after_timeout(self, client):
        """停止推送超过 phone_timeout_ms 后，phone_connected 变为 false。"""
        # 先上报一次，手机在线
        client.post("/phone-data", json=PHONE_PAYLOAD)
        assert client.get("/health").json()["phone_connected"] is True

        # 设置极短的超时阈值（最低钳制值 100ms，这里用 300ms 便于测试）
        client.patch("/api/config", json={"phone_timeout_ms": 300})
        # 等待超时
        time.sleep(0.5)
        assert client.get("/health").json()["phone_connected"] is False


# ── WebUI 静态文件服务 ────────────────────────────────────────────────

class TestWebUI:
    def test_index_returns_html(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    def test_static_app_js(self, client):
        resp = client.get("/static/app.js")
        assert resp.status_code == 200
        assert "javascript" in resp.headers.get("content-type", "")

    def test_static_style_css(self, client):
        resp = client.get("/static/style.css")
        assert resp.status_code == 200
        assert "css" in resp.headers.get("content-type", "")


# ── 电脑状态采集开关 ──────────────────────────────────────────────────

class TestComputerCollectToggle:
    def test_computer_collect_enabled_in_config(self, client):
        """默认配置包含 computer_collect_enabled 且为 true。"""
        body = client.get("/api/config").json()
        assert "computer_collect_enabled" in body
        assert body["computer_collect_enabled"] is True

    def test_disable_computer_collect(self, client):
        """禁用前台采集后，computer 段不再包含前台窗口/进程/游戏/导航。"""
        resp = client.patch("/api/config", json={"computer_collect_enabled": False})
        assert resp.status_code == 200
        assert "computer_collect_enabled" in resp.json()["updated"]

        # 禁用后 computer 段不应包含前台数据
        state = client.get("/api/state").json()
        assert "foreground_window" not in state["computer"]
        assert "is_gaming" not in state["computer"]

        # 恢复启用
        client.patch("/api/config", json={"computer_collect_enabled": True})
        body = client.get("/api/config").json()
        assert body["computer_collect_enabled"] is True


# ── 连接状态 ──────────────────────────────────────────────────────────

class TestConnectionStatus:
    def test_health_endpoint(self, client):
        """健康检查端点返回正确结构。"""
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert "version" in body
        assert "phone_connected" in body

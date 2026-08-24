"""pytest 配置：为测试提供 FastAPI TestClient 共享 fixtures。"""

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# 把 desktop/ 加入 sys.path，让 import server / collector / storage 能找到
_desktop = Path(__file__).resolve().parent.parent / "Mizuki" / "desktop"
if str(_desktop) not in sys.path:
    sys.path.insert(0, str(_desktop))


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path, monkeypatch):
    """每个测试用独立的 config.json 和 data/，不污染真实文件。"""
    import server

    cfg_path = tmp_path / "config.json"
    data_file = tmp_path / "data" / "collected.jsonl"
    data_file.parent.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(server, "DATA_FILE", data_file)
    # 重新加载默认配置到临时路径
    server.config.update(server.DEFAULT_CONFIG)
    cfg_path.write_text(
        json.dumps(server.DEFAULT_CONFIG, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    # 重建 storage 指向临时文件
    from storage import DataCollector

    server.storage = DataCollector(data_file=data_file)
    server.storage.start()
    yield
    server.storage.stop()


@pytest.fixture()
def client():
    """返回 FastAPI TestClient（同步）。"""
    import server
    return TestClient(server.app)

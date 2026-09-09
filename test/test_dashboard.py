"""将无依赖的 Node.js 前端回归测试纳入 pytest；详细用例见 test_dashboard.cjs。"""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_dashboard_node_regressions():
    node = shutil.which("node")
    if node is None:
        pytest.skip("前端回归测试需要 Node.js 18+；可单独执行 node --test test/test_dashboard.cjs")
    root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [node, "--test", str(root / "test" / "test_dashboard.cjs")],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

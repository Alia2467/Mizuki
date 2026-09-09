"""Check the configured data endpoint without printing credentials or private payloads."""
import argparse
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request

_desktop = Path(__file__).resolve().parent.parent / "Mizuki" / "desktop"
sys.path.insert(0, str(_desktop))
from server import DEFAULT_CONFIG, TOKEN_HEADER


def main() -> int:
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=f"http://localhost:{DEFAULT_CONFIG['port']}/merged-data")
    args = parser.parse_args()
    headers = {}
    token = os.environ.get("MIZUKI_TOKEN", "").strip()
    if token:
        headers[TOKEN_HEADER] = token
    try:
        request = urllib.request.Request(args.url, headers=headers)
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.load(response)
        if not isinstance(data, dict):
            raise ValueError("响应不是 JSON 对象")
        print("连接成功；JSON 结构有效")
        print(f"phone_connected: {data.get('phone_connected', False)}")
        print(f"computer 字段: {list((data.get('computer') or {}).keys())}")
        return 0
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}")
        if exc.code == 401:
            print("请通过 MIZUKI_TOKEN 环境变量提供与控制台一致的令牌。")
        elif exc.code == 429:
            print("已限流，请稍后重试。")
    except (urllib.error.URLError, OSError, ValueError):
        print("连接或响应解析失败，请检查服务、端口与网络设置。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

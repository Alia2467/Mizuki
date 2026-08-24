"""诊断脚本：检查控制台服务是否正常运行"""
import sys
import json
import urllib.request
import urllib.error

URL = "http://localhost:821/merged-data"

def main():
    print(f"正在测试 {URL} ...")
    try:
        req = urllib.request.Request(URL)
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
            body = resp.read().decode("utf-8")
            print(f"状态码: {status}")
            print(f"响应长度: {len(body)} 字节")
            try:
                data = json.loads(body)
                print(f"JSON 解析: 成功")
                print(f"  timestamp: {data.get('timestamp', 'N/A')}")
                print(f"  phone_connected: {data.get('phone_connected', 'N/A')}")
                print(f"  computer keys: {list(data.get('computer', {}).keys())}")
                print("\n结论: 服务正常")
            except json.JSONDecodeError:
                print(f"JSON 解析: 失败")
                print(f"原始响应: {body[:200]}")
                print("\n结论: 服务返回了非 JSON 数据")
    except urllib.error.HTTPError as e:
        print(f"HTTP 错误: {e.code} {e.reason}")
        body = e.read().decode("utf-8", errors="replace")
        print(f"响应体: {body[:200]}")
        if e.code == 502:
            print("\n可能原因:")
            print("  1. 服务器进程异常（重启服务器试试）")
            print("  2. 杀毒软件/防火墙拦截了本地 HTTP 请求")
            print("  3. 系统代理设置拦截了 localhost 请求")
    except urllib.error.URLError as e:
        print(f"连接失败: {e.reason}")
        print("\n可能原因:")
        print("  1. 控制台服务没启动")
        print("  2. 端口号不对（默认 821）")
        print("  3. 防火墙阻止了连接")
    except Exception as e:
        print(f"未知错误: {type(e).__name__}: {e}")

if __name__ == "__main__":
    main()

# Mizuki

AI 伴侣感官系统（手机 App + 电脑控制台 + MaiBot 插件）。手机端采集定位、天气、健康与设备使用状态并实时上报，电脑端汇聚双端数据并落盘，MaiBot 插件把处境翻译成情境上下文，驱动 AI 伴侣在心率过高、下雨等真实情境下结合人设主动关心。

当前版本： **v1.1.0**

---

## 功能概述

| 能力 | 说明 |
|------|------|
| 手机端采集 | GPS/城市、天气（Open-Meteo，15 分钟缓存）、心率/步数/睡眠（Health Connect，计步传感器兜底）、前台应用、导航/通话/听歌状态 |
| 电脑端采集 | 前台窗口标题/进程名、打游戏状态判定（关键词表）、CPU/内存/磁盘硬件指标 |
| 汇聚服务 | 双端数据合并（`/merged-data`）、手机在线判定（超时制）、内嵌 WebUI 仪表盘、SQLite 异步落盘 |
| 触发规则 | 心率过高 / 步数达标 / 雨雪 / 高温，四条规则独立开关与阈值配置，按规则键独立冷却（默认 3 分钟） |
| 主动说话 | 插件注入情境上下文（`append_context`）+ 请求 MaiBot 结合人设生成话术（`trigger_proactive`），文案零硬编码 |
| 安静模式 | 手机导航中/通话中自动静默，不打扰 |
| 降级容错 | 全链路失败不中断：GPS→上次有效位置→IP 定位；Health Connect→计步传感器；上报失败指数退避（间隔加倍，上限 2.4s），永不停止服务 |
| 自诊断 | 手机端发送成功/失败计数、权限缺失警告、最近错误，仪表盘实时可见；深度健康检查 `/health/deep` |
| 传输鉴权 | 共享 Token（`shared_token` / `source.token` / 环境变量 `MIZUKI_TOKEN`）经 `X-Sensor-Token` 请求头校验，空串为不启用鉴权的兼容模式 |
| 安全防护 | API 速率限制（滑动窗口按 IP 限流），`/phone-data` 60 次/分、`/merged-data` 500 次/分，超限返回 429 |
| 异地组网 | 手机与电脑不在同一局域网时，用 Tailscale / ZeroTier 虚拟局域网接入 |
| 插件状态 | WebUI 实时显示插件连接状态（心跳机制） |
| 数据管理 | 历史数据查询、统计、导出（JSON/CSV）、自动清理（保留 30 天），WebUI 一键导出 |

---

## 数据流

```
手机传感器/外部API → 周期采集（300ms，失败占位值 + 指数退避）→ HTTP POST /phone-data
    → 控制台合并 + 在线判定 + SQLite 异步落盘
    → 插件周期拉取 /merged-data（300ms）→ 安静模式过滤 → 多规则并行求值 + 冷却
    → 合并注入情境上下文 → MaiBot 结合人设主动说话 → QQ
```

---

## 安装

**前置要求**：手机 Android 8.0+（minSdk 26）；电脑 Windows 10/11；MaiBot 1.x + maibot_sdk 2.5+；手机与电脑同一局域网（异地则先组 Tailscale）。

1. **手机端**：直接安装 `Release/apk/` 里的成品 APK。
2. **电脑端**：双击 `Release/exe/Mizuki.exe`，记下电脑局域网 IP 和端口（默认 **821**）。
3. **插件**：把 `Mizuki/plugin/` 整个目录拷入 MaiBot 的 `plugins/` 目录，在插件管理里启用「海月感知」，把「主动说话目标」里的 `user_id` 改成你的 QQ 号。
4. **连接**：手机 App「设置 → 连接」填电脑 IP 和端口 821，点「开始连接」，授权定位、通知、使用情况访问等权限；启用鉴权时同时填写与控制台 `shared_token` 一致的 Token。

源码运行电脑端（开发环境）：

```bash
cd Mizuki/desktop
pip install -r requirements.txt
python server.py        # 纯服务；python app.py 为桌面窗口入口
```

源码编译手机端（开发环境）：

1. 安装 Android Studio（勾选 Android SDK）；手机开启开发者模式与 USB 调试（设置 → 关于手机 → 连续点「版本号」7 次）。
2. 用 Android Studio 打开 `Mizuki/android/`，等待 Gradle 同步（首次联网下载依赖）。
3. 顶部选择设备 → 点 ▶ 运行；或直接命令行 `gradle assembleDebug`（JDK 21，`local.properties` 指向本机 Android SDK）。

### 异地组网（Tailscale）

手机和电脑不在同一个局域网时，手机连不上电脑端。推荐用 Tailscale（免费、零配置的异地组网工具）把两台设备拉进同一个虚拟局域网：

1. 去 <https://tailscale.com> 注册账号。
2. 电脑和手机都安装 Tailscale 客户端，登录同一账号（仓库根目录 `start-tailscale.bat` 为电脑端快捷入口）。
3. 两台设备各自获得一个 `100.x.x.x` 虚拟 IP；记下「电脑」的那个地址。
4. 手机 App「设置 → 连接」里 IP 填电脑的 `100.x.x.x`，端口 821。

之后无论手机用 WiFi 还是流量都能直连。推荐理由：免费、开箱即用，无需公网 IP 与端口映射；点对点加密，比端口直接暴露公网安全；电脑端监听 `0.0.0.0` 自动包含 Tailscale 网卡，无需额外配置。

---

## 配置

**控制台** `config.json`（缺失自动生成默认值，修改后自动热重载）：

| 变量 | 说明 |
|------|------|
| `host` | 监听地址，默认 `0.0.0.0` |
| `port` | 服务端口，默认 `821`（三端共识值） |
| `computer_collect_enabled` | 是否启用电脑状态采集（前台窗口/进程/游戏），默认 `true` |
| `computer_collect_interval` | 电脑状态采集间隔（毫秒），默认 `300` |
| `phone_timeout_ms` | 超过该时长未上报视为手机离线（毫秒），默认 `10000` |
| `poll_interval` | WebUI 仪表盘轮询间隔（毫秒），默认 `5000` |
| `shared_token` | 共享鉴权令牌，空串 = 不启用鉴权的兼容模式 |

**插件** `config.toml`（四分节，关键可选项）：

| 变量 | 说明 |
|------|------|
| `source.data_url` | 电脑端合并数据接口，默认 `http://localhost:821/merged-data` |
| `source.token` | 与控制台 `shared_token` 一致的令牌；控制台未启用鉴权时留空 |
| `source.fetch_interval` | 数据拉取间隔（毫秒），默认 `300` |
| `target.platform` / `chat_type` / `user_id` | 主动说话目标聊天流（默认 QQ 私聊） |
| `proactive.cooldown_ms` | 同一触发条件的冷却时间（毫秒），默认 `180000`（3 分钟） |
| `rules.table` | 声明式规则表：心率 ≥100 / 步数 ≥10000 / 降雨（受控词表）/ 高温 ≥35 ℃，逐条 `enabled` 开关与阈值 |

**手机端**：连接 IP / 端口 / 采集间隔 / Token 在 App 内配置；Android 明文 HTTP 依赖 Manifest 的 `usesCleartextTraffic="true"`（勿删）。

---

## 使用

```bash
# 电脑端（源码运行；打包版直接双击 Mizuki.exe）
python server.py

# 链路验证：模拟手机上报
curl -X POST http://localhost:821/phone-data \
  -H "Content-Type: application/json" \
  -d '{"device_id":"test","timestamp":"2026-08-21T14:30:00","location":{"city":"郑州","latitude":34.75,"longitude":113.65},"weather":{"condition":"rain","temperature":26,"humidity":78},"health":{"heart_rate":85,"steps":4200,"sleep_hours":7.2},"usage":{"foreground_app":"高德地图","is_navigating":true,"is_calling":false,"screen_text":""},"diagnostics":{}}'

# 查看合并数据（插件消费同一接口）
curl http://localhost:821/merged-data

# 健康检查 / 仪表盘
curl http://localhost:821/health
# WebUI：浏览器打开 http://localhost:821/（exe 运行时自动弹出内嵌窗口）

# 数据导出
curl http://localhost:821/api/export/json -o export.json
curl http://localhost:821/api/export/csv -o export.csv

# 手机端 APK 源码编译
cd Mizuki/android
gradle assembleDebug          # Gradle 8.7 + JDK 21，local.properties 指向 D:/AndroidSDK

# 电脑端 exe 打包
cd Mizuki/desktop
pyinstaller app.py            # 依赖见 requirements.txt

# 异地组网
start-tailscale.bat
```

---

## 健康数据说明

- **步数**：手机自带计步传感器，无需额外设备（Health Connect 可用时优先读今日累计）。
- **心率/睡眠**：需连接支持 Health Connect 的健康设备（手表/手环），缺失时上报占位值 0。

---

## 数据结构

采集数据落盘 `data/collected.db`（SQLite 数据库，与程序代码分目录）：

```sql
CREATE TABLE records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,          -- "phone" 或 "computer"
    timestamp TEXT NOT NULL,     -- ISO 8601 时间戳
    data TEXT NOT NULL           -- JSON 格式完整数据
);
```

手机记录逐条落盘；电脑状态仅在关键值变化（前台窗口/进程/游戏三元组）时落盘，避免刷屏。数据自动清理（保留 30 天）。数据契约（`/phone-data` 五段结构、`/merged-data` 合并结构、受控天气词表）见 `.docs/architecture.md` §2。

---

## 目录结构

```
Mizuki/
├── README.md                    # 本文档
├── LICENSE                      # GPL-3.0 全文
├── .editorconfig                # 编辑器配置
├── Mizuki/                      # 源码根目录
│   ├── android/                 # Android 手机端
│   ├── desktop/                 # 电脑端源码
│   └── plugin/                  # MaiBot 插件
├── test/                        # 自动化测试
└── start-tailscale.bat          # 异地组网脚本
```

---

## 技术栈

- **手机端**：Kotlin 1.9.24、OkHttp、Gson、Google Fused Location、Health Connect、Leaflet
- **电脑端**：FastAPI、uvicorn、psutil、pywin32、pywebview、pystray、Pillow、PyInstaller
- **插件**：maibot_sdk 2.5+、httpx、asyncio
- **数据**：JSON over HTTP，SQLite
- **构建**：AGP 8.4.2 + Gradle 8.7 + JDK 21；Python 3.13

---

## 验证

```bash
# 运行自动化测试（65 个用例：32 个服务端 API + 33 个插件核心逻辑）
py -m pytest test/ -v
```

---

## License

GPL-3.0

# Mizuki（海月感知）

AI 伴侣感官系统（手机 App + 电脑控制台 + MaiBot 插件）。手机端采集定位、天气、健康与设备使用状态并实时上报，电脑端汇聚双端数据并落盘，MaiBot 插件把处境翻译成情境上下文，驱动 AI 伴侣「海月」在心率过高、下雨、导航结束等真实情境下结合人设主动关心——不是固定模板，是她自己生成的话。

当前版本：控制台 **v2.4.0** / 手机端 **v2.1.0** / 插件 **v1.1.0**

---

## 功能概述

| 能力 | 说明 |
|------|------|
| 手机端采集 | GPS/城市、天气（Open-Meteo，15 分钟缓存）、心率/步数/睡眠（Health Connect，计步传感器兜底）、前台应用、导航/通话/听歌状态 |
| 电脑端采集 | 前台窗口标题/进程名、打游戏/导航状态判定（关键词表） |
| 汇聚服务 | 双端数据合并（`/merged-data`）、手机在线判定（超时制）、内嵌 WebUI 仪表盘、收集装置异步落盘（队列 + 专用写盘线程，不阻塞请求路径） |
| 触发规则 | 心率过高 / 步数达标 / 雨雪 / 高温 / 导航结束，五条规则独立开关与阈值配置，按规则键独立冷却（默认 30 分钟） |
| 主动说话 | 插件注入情境上下文（`append_context`）+ 请求 MaiBot 结合人设生成话术（`trigger_proactive`），文案零硬编码 |
| 安静模式 | 导航中/通话中自动静默，不打扰 |
| 降级容错 | 全链路失败不中断：GPS→上次有效位置→IP 定位；Health Connect→计步传感器；上报失败只计数、按周期自然重试 |
| 自诊断 | 手机端发送成功/失败计数、权限缺失警告、最近错误，仪表盘实时可见 |
| 传输鉴权 | 共享 Token（`shared_token` / `source.token`）经 `X-Sensor-Token` 请求头校验，空串为不启用鉴权的兼容模式 |
| 异地组网 | 手机与电脑不在同一局域网时，用 Tailscale / ZeroTier 虚拟局域网接入 |

---

## 数据流

```
手机传感器/外部API → 周期采集（30s，失败占位值）→ HTTP POST /phone-data
    → 控制台合并 + 在线判定 + 异步落盘 collected.jsonl
    → 插件周期拉取 /merged-data（15s）→ 安静模式过滤 → 规则门控 + 冷却
    → 注入情境上下文 → MaiBot 结合人设主动说话 → QQ
```

---

## 安装

**前置要求**：手机 Android 8.0+（minSdk 26）；电脑 Windows 10/11（内嵌窗口依赖系统自带 WebView2）；MaiBot 1.x + maibot_sdk 2.5+；手机与电脑同一局域网（异地则先组 Tailscale）。

1. **手机端**：直接安装 `Release/apk/` 里的成品 APK。
2. **电脑端**：双击 `Release/exe/Mizuki.exe`，记下电脑局域网 IP 和端口（默认 **821**）。
3. **插件**：把 `maibot-sensor/plugin/` 整个目录拷入 MaiBot 的 `plugins/` 目录，在插件管理里启用「maibot感知」，把「主动说话目标」里的 `user_id` 改成你的 QQ 号。
4. **连接**：手机 App「设置 → 连接」填电脑 IP 和端口 821，点「开始连接」，授权定位、通知、使用情况访问等权限；启用鉴权时同时填写与控制台 `shared_token` 一致的 Token。

源码运行电脑端（开发环境）：

```bash
cd maibot-sensor/computer-app
pip install -r requirements.txt
python server.py        # 纯服务；python app.py 为桌面窗口入口
```

源码编译手机端（开发环境）：

1. 安装 Android Studio（勾选 Android SDK）；手机开启开发者模式与 USB 调试（设置 → 关于手机 → 连续点「版本号」7 次）。
2. 用 Android Studio 打开 `maibot-sensor/phone-app/`，等待 Gradle 同步（首次联网下载依赖）。
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

**控制台** `config.json`（缺失自动生成默认值）：

| 变量 | 说明 |
|------|------|
| `host` | 监听地址，默认 `0.0.0.0` |
| `port` | 服务端口，默认 `821`（三端共识值） |
| `computer_collect_interval` | 电脑状态采集间隔（秒），默认 `5` |
| `phone_timeout_seconds` | 超过该时长未上报视为手机离线，默认 `30` |
| `shared_token` | 共享鉴权令牌，空串 = 不启用鉴权的兼容模式 |

**插件** `config.toml`（六分节，关键可选项）：

| 变量 | 说明 |
|------|------|
| `source.data_url` | 电脑端合并数据接口，默认 `http://localhost:821/merged-data` |
| `source.token` | 与控制台 `shared_token` 一致的令牌；控制台未启用鉴权时留空 |
| `source.fetch_interval` | 数据拉取间隔（秒），默认 `15` |
| `target.platform` / `chat_type` / `user_id` | 主动说话目标聊天流（默认 QQ 私聊） |
| `proactive.cooldown_seconds` | 同一触发条件的冷却时间，默认 `1800` |
| `proactive.quiet_when_navigating` / `quiet_when_calling` | 导航中/通话中安静模式，默认开启 |
| `rules.nav_end_enabled` | 导航结束问候开关（状态翻转检测，不进规则表） |
| `rules.table` | 声明式规则表：心率 ≥100 / 步数 ≥10000 / 降雨（受控词表）/ 高温 ≥35 ℃，逐条 `enabled` 开关与阈值 |
| `persona.appellation` | 对用户的称呼（替换情境文案里的「哥哥」） |

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

# 手机端 APK 源码编译
cd maibot-sensor/phone-app
gradle assembleDebug          # Gradle 8.7 + JDK 21，local.properties 指向 D:/AndroidSDK

# 电脑端 exe 打包
cd maibot-sensor/computer-app
pyinstaller app.py            # 依赖见 requirements.txt

# 异地组网
start-tailscale.bat
```

---

## 手机端可自定义项

| 项目 | 方式 |
|------|------|
| 首页横幅图片 | 点一下横幅 → 从相册选图 |
| 首页那行字 | 点一下文字 → 弹窗修改 |
| 侧边栏顶部 | 一个随主题色变化的相框 |
| 主题色 | 个性设置里 RGB 自定义（默认纯白），只作用于按键 |
| 字体 | 个性设置里选 系统默认 / 思源黑体 / 思源宋体 |

---

## 健康数据说明

- **步数**：手机自带计步传感器，无需额外设备（Health Connect 可用时优先读今日累计）。
- **心率/睡眠**：需连接支持 Health Connect 的健康设备（手表/手环），缺失时上报占位值 0。

---

## 数据结构

采集数据落盘 `data/collected.jsonl`（与程序代码分目录，每行一个 JSON 对象）：

```jsonl
{"type":"phone","device_id":"V2270A","timestamp":"...","location":{...},"weather":{...},"health":{...},"usage":{...},"diagnostics":{...},"received_at":"..."}
{"type":"computer","timestamp":"...","foreground_window":"Visual Studio Code","foreground_process":"Code.exe","is_gaming":false,"is_navigating":false}
```

手机记录逐条落盘；电脑状态仅在关键值变化（前台窗口/进程/游戏/导航四元组）时落盘，避免刷屏。数据契约（`/phone-data` 五段结构、`/merged-data` 合并结构、受控天气词表）见 `.docs/architecture.md` §2。

---

## 目录结构

```
suzukimizuki/
├── README.md                    # 本文档
├── AGENTS.md                    # 硬性编码指令与编码风格规范
├── LICENSE                      # GPL-3.0 全文
├── .docs/
│   ├── architecture.md          # 架构规格（数据契约/模块设计/数据流）
│   └── CHANGELOG.md             # 三端统一变更日志
├── Release/                     # 构建产物（按类型分子文件夹）
│   ├── apk/                     # 手机端 APK（maibot-sensor.apk）
│   └── exe/                     # 控制台发行目录（Mizuki.exe + config.json + data/）
├── maibot-sensor/               # 唯一源码根目录
│   ├── phone-app/               # Android 手机端（Kotlin：采集服务/主界面/地图/天气/笔记）
│   ├── computer-app/            # 电脑端源码（server.py 服务 + collector.py 采集 + storage.py 收集装置 + app.py 桌面入口 + static/ WebUI）
│   └── plugin/                  # MaiBot 插件（plugin.py + _manifest.json + config.toml）
└── start-tailscale.bat          # 异地组网脚本
```

---

## 技术栈

- **手机端**：Kotlin 1.9.24、OkHttp、Gson、Google Fused Location、Health Connect、Leaflet（离线地图）
- **电脑端**：FastAPI、uvicorn、psutil、pywin32、pywebview、pystray、Pillow、PyInstaller
- **插件**：maibot_sdk 2.5+、httpx、asyncio
- **数据**：JSON over HTTP（局域网明文，默认端口 821），落盘 JSONL
- **构建**：AGP 8.4.2 + Gradle 8.7 + JDK 21（手机）；Python 3.13（电脑）

---

## 验证

暂无自动化测试框架，端到端验收步骤（链路/落盘/离线判定/五条规则/自诊断）见 `AGENTS.md`「测试与验证规范」一节。

---

## License

GPL-3.0，全文见 [`LICENSE`](LICENSE)。

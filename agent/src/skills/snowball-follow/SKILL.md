---
name: snowball-follow
category: data-source
description: 雪球投资日报 — 追踪雪球大V投资观点，生成每日AI摘要。大V列表来自 influencer 模块动态维护的池子。支持 CDP 浏览器抓取(稳定)与 API fallback(无需 Chrome)。可选飞书文档推送。
---

# Snowball Follow — 雪球投资日报

追踪雪球大V的投资观点，每日生成 AI 摘要报告。大V列表由 `agent/src/influencer` 模块动态维护（月度评分进出），本技能消费该池子，不独立维护名单。

## 与 influencer 模块的关系

```
influencer 模块 (Python)              snowball-follow 技能 (Node.js)
┌─────────────────────────┐           ┌──────────────────────────────┐
│ seeds.py 种子集          │           │ prepare-digest.js            │
│ scoring.py 动态评分      │  export   │   ├─ CDP 抓取 (Chrome 登录态) │
│ refresh.py 月度刷新池子  │ ────────> │   └─ API fallback (token)    │
│ store.py 持久化          │ sources   │ deliver.js 推送 (stdout/飞书) │
└─────────────────────────┘           └──────────────────────────────┘
```

大V名单的**唯一真相源**是 influencer 模块的池子。本技能通过
`python -m src.influencer.export_sources` 导出池子为 `sources.json`，
digest 脚本读取该文件决定追踪谁。

## 前置条件

| 条件 | 说明 | 必需 |
| --- | --- | --- |
| Node.js 22+ | 脚本依赖 Node 原生 `fetch` 和 `WebSocket`（Node 24 已验证） | ✅ |
| Chrome 远程调试 | CDP 模式需要：Chrome 用 `--remote-debugging-port=9222` 启动并登录雪球 | CDP 模式(推荐) |
| XUEQIU_TOKEN | API fallback 模式需要（浏览器 DevTools 取 `xq_a_token` cookie），写入 `agent/.env`。注意:雪球 WAF 会拦截非浏览器请求,API 模式常不可用 | API 模式(常失效) |
| lark-cli | 仅推送飞书文档时需要 | 可选 |

> **抓取模式自动选择**：脚本启动时探测 `localhost:9222/json/version`，可用则用 CDP（直连 Chrome，最稳定）；
> 不可用则回退到 API（用 XUEQIU_TOKEN，但雪球 WAF 常拦截非浏览器请求）。**生产环境务必用 CDP 模式。**

## CDP 模式配置（推荐）

### 1. 启动带远程调试的 Chrome

**关键**：必须用独立的 `user-data-dir`，否则会和日常 Chrome 冲突。在该实例里登录雪球后，登录态会保留在这个 profile 里。

```bash
# Windows (Git Bash)
mkdir -p ~/.vibe-trading/chrome-cdp-profile
"/c/Program Files/Google/Chrome/Application/chrome.exe" \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/.vibe-trading/chrome-cdp-profile" \
  --no-first-run --no-default-browser-check \
  "https://xueqiu.com"
```

启动后在弹出的 Chrome 窗口里**登录雪球**。登录态会保存在这个独立 profile 里，下次启动同一命令无需重新登录。

### 2. 验证 CDP 可用

```bash
curl -s http://localhost:9222/json/version | head -c 100
# 应返回 {"Browser":"Chrome/...","webSocketDebuggerUrl":"ws://..."}
```

### 3. 运行抓取（脚本自动探测 9222 并使用 CDP）

```bash
node agent/src/skills/snowball-follow/scripts/prepare-digest.js \
  --config ~/.snowball-follow/config.json \
  --state ~/.snowball-follow/state.json \
  --lookback 24 --max 5
# stderr 会打印 "Fetch mode: CDP (local Chrome)"
```

> **自定义端口**：若 9222 被占用，用 `CDP_PORT=9223 node ...` 或 `--cdp-port 9223`。

> **互动数据(点赞/评论数)**：未登录时这些字段为 0（雪球隐藏）。登录雪球后可拿到完整互动数据。

## 快速开始

### 1. 导出大V列表（从 influencer 池子）

```bash
python -m src.influencer.export_sources --output ~/.snowball-follow/sources.json
```

### 2. 配置（首次）

创建 `~/.snowball-follow/config.json`：

```json
{
  "language": "zh",
  "timezone": "Asia/Shanghai",
  "frequency": "daily",
  "deliveryTime": "08:00",
  "delivery": { "method": "stdout", "folderToken": "" },
  "sources": [],
  "lookbackHours": 24,
  "maxPostsPerAuthor": 5,
  "onboardingComplete": true
}
```

### 3. 生成日报

```bash
# 前置：Chrome 已用 --remote-debugging-port=9222 启动并登录雪球
node agent/src/skills/snowball-follow/scripts/prepare-digest.js \
  --config ~/.snowball-follow/config.json \
  --state ~/.snowball-follow/state.json \
  --lookback 24 --max 5 > /tmp/digest-data.json

# 若 status 为 "empty"，今日无新帖，停止。

# Agent 读取 digest-data.json，按 prompts/ 规则生成中文摘要，组装成日报 Markdown。

# 推送（stdout 或飞书）
node agent/src/skills/snowball-follow/scripts/deliver.js \
  --config ~/.snowball-follow/config.json \
  --file /tmp/snowball-digest-$(date +%Y%m%d).md
```

## 抓取模式

### CDP 模式（推荐，直连 Chrome 9222）

通过本地 Chrome 的雪球登录态抓取，绕过 WAF/反爬最可靠。脚本直连 Chrome
原生 CDP 端口（WebSocket），无需 web-access proxy。需 Chrome 用
`--remote-debugging-port=9222` 启动 + 独立 user-data-dir + 登录雪球。

### API fallback 模式（常被 WAF 拦截）

CDP 不可用时自动启用。用 `XUEQIU_TOKEN`（`agent/.env`）调用雪球状态接口。
**注意**：雪球部署了阿里云 WAF，非浏览器请求（curl/requests/Node fetch）
即使带正确 token 也常被 WAF 验证墙拦截。此模式主要作为兜底，不保证可用。

## 配置字段

| 字段 | 说明 |
| --- | --- |
| `delivery.method` | `stdout` 输出控制台；`feishu-doc` 创建飞书文档 |
| `delivery.folderToken` | 飞书目标文件夹 token（仅 feishu-doc） |
| `sources` | 大V列表 `{name, slug, tag}`；推荐由 `export_sources.py` 生成 |
| `sources[].slug` | 雪球主页标识（字母别名或数字 ID） |
| `lookbackHours` | 回溯窗口（小时），默认 24 |
| `maxPostsPerAuthor` | 每位大V最多进摘要的帖子数，默认 5 |

## Prompt 自定义

复制默认 prompt 到 `~/.snowball-follow/prompts/` 即可覆盖（优先级高于内置）：
- `summarize-posts.md` — 单个作者帖子摘要规则
- `digest-intro.md` — 整份日报组装格式

## 飞书推送

```bash
lark-cli auth login --domain docs   # 首次登录
```

然后把 `delivery.method` 改为 `feishu-doc` 并填 `folderToken`（从飞书文件夹 URL
`/drive/folder/<folderToken>` 获取）。

## 文件结构

```
agent/src/skills/snowball-follow/
├── SKILL.md                              # 本文件
├── config/
│   ├── config-schema.json                # 配置 schema
│   └── default-sources.json              # 默认大V（fallback；推荐用 export_sources 覆盖）
├── prompts/
│   ├── summarize-posts.md                # 帖子摘要规则
│   └── digest-intro.md                   # 日报组装规则
├── references/site-patterns/
│   └── xueqiu.com.md                     # DOM 选择器经验
└── scripts/
    ├── prepare-digest.js                 # 抓取（CDP/API 双路径）
    ├── deliver.js                        # 推送（stdout/飞书）
    └── package.json
```

## 常见问题

- **CDP: not connected / 自动回退到 API** → Chrome 未用 `--remote-debugging-port=9222` 启动。按"CDP 模式配置"启动。可用 `curl http://localhost:9222/json/version` 验证。
- **CDP: 9222 被占用** → 用其他端口启动 Chrome，并设 `CDP_PORT=<端口> node ...`。
- **互动数据全为 0** → 未登录雪球。在 CDP Chrome 实例里登录后重新抓取。
- **API: WAF 拦截 / token 无效** → API fallback 模式常被雪球 WAF 挡；改用 CDP 模式（登录态浏览器）。
- **日报为空** → 回溯窗口内无新帖；或全部已在 `state.json` 去重。临时调大 `lookbackHours`。
- **页面结构变化** → DOM 选择器记录在 `references/site-patterns/xueqiu.com.md`，需同步更新。

## 致谢

原技能来自 [yanglaiyang/Snowball-Follow-Skill](https://github.com/yanglaiyang/Snowball-Follow-Skill) (MIT)。
本项目集成时增加了：(1) API fallback 双路径、(2) 与 influencer 模块池子打通、(3) 项目路径适配。

# 🎮 真心话大冒险 Telegram Bot — Render 部署版

部署在 [Render](https://render.com/) 云端的真心话大冒险群组游戏 Bot。玩家投骰子比点数（1–99），**点数最大者为胜利者，点数最小者为失败者**，由胜利者指定失败者执行「真心话」或「大冒险」挑战。

> 与本地版（CF 隧道 + 一键启动）的区别：**移除全部本地逻辑**（cloudflared / start_all.bat / 隧道监控），改为 Render Webhook 模式；**所有敏感配置存于 Upstash**，GitHub 仓库中不含任何密钥。

---

## ✨ 功能特性

- 🎲 **投骰子比点数**（1–99），自动判定胜者 / 败者，支持多人并列
- 💬 / 🔥 **真心话 · 大冒险**：内联按钮一键选择（仅本轮失败者可点击）
- 👑 **主持人机制**：创建、踢人、转让主持人
- 🗳️ **投票机制**：投票结束游戏、投票转让主持人（超过半数通过）
- 🛡️ **管理员权限**：强制结束、强制转让
- ⏰ **15 分钟无操作自动结束**，避免游戏残留
- 🧵 **群组话题（Topics）支持**：不同话题独立开游戏、互不干扰
- 🔐 **密钥外置**：BOT_TOKEN 等敏感配置存放在 Upstash，仓库零密钥
- 💾 **Upstash Redis 持久化**：游戏进度、胜负统计跨重启保留

---

## 🧭 架构

```
Telegram 用户
   │  (setWebhook 注册 https://xxx.onrender.com/webhook)
   ▼
Render Web Service（python bot.py）
   ├─ POST /webhook  ← Telegram 推送更新（aiohttp 自定义服务器）
   ├─ GET  /         ← Render 健康检查（返回 OK）
   └─ Upstash Redis（REST）← 敏感配置 + 游戏数据（tod:game:* / tod:stats:*）
```

启动时 Bot 从 Upstash 自动拉取 `tod:config:bot_token` 等敏感配置，**Render 环境变量只需两个**：`UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN`。

---

## 🚀 Render 部署步骤

### 1. 准备 Upstash Redis（up大上海1）

1. 打开 [upstash.com](https://upstash.com) 进入你的 Redis 数据库（本 Bot 与游戏数据共用的实例）
2. 在数据库详情页复制 **REST URL**（形如 `https://xxx.upstash.io`）与 **REST TOKEN**

### 2. 写入 Bot Token（敏感配置入 Upstash，不入仓库）

在 Upstash 控制台 **Data Browser** 或 REST API 中设置：

| Key | Value |
|---|---|
| `tod:config:bot_token` | 你的 Bot Token（形如 `123456789:ABC...`） |

可选 key：`tod:config:proxy`（本地调试代理）、`tod:config:mode`（强制指定运行模式）。

> ⚠️ **不要**把 Token 写进 GitHub 仓库的任何文件（含 README / .env.example）。Bot 启动时若检测到 Upstash 配置则优先使用，环境变量兜底。

### 3. 在 Render 创建服务

**方式 A — Blueprint（推荐）**

1. Fork / 推送本仓库到你的 GitHub
2. Render Dashboard → **New +** → **Blueprint**
3. 选择本仓库，Render 读取 `render.yaml` 自动创建 Web Service
4. 按提示填写 `sync: false` 的环境变量（见下表）

**方式 B — 手动创建 Web Service**

| 配置项 | 值 |
|---|---|
| Runtime | Python 3 |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `python bot.py` |
| Health Check Path | `/` |
| Instance Type | Free（免费实例） |

### 4. 配置环境变量（Environment）

| 变量 | 必填 | 说明 |
|---|---|---|
| `UPSTASH_REDIS_REST_URL` | ✅ | Upstash REST URL |
| `UPSTASH_REDIS_REST_TOKEN` | ✅ | Upstash REST Token |
| `PYTHON_VERSION` | 否 | 建议 3.11.9（render.yaml 已带默认值） |

> `RENDER_EXTERNAL_URL` 与 `PORT` 由 Render **自动注入**，程序自动以 Webhook 模式运行并注册到 Telegram，无需手动配置。

### 5. 部署并验证

1. 等待 Build & Deploy 完成，日志出现 `✅ 服务器已启动，等待请求...`
2. 将 Bot 加入群组，发送 `/createnewgame` → `/join` → `/roll` 测试

---

## 🎮 指令总览

| 指令 | 说明 | 使用范围 |
|------|------|----------|
| `/start` | 查看游戏说明 | 私聊 / 群组 |
| `/help` | 查看详细帮助 | 私聊 / 群组 |
| `/createnewgame` | 创建新游戏（创建者为主持人） | 群组 |
| `/join` | 加入游戏 | 群组 |
| `/leave` | 离开游戏（主持人离开将结束游戏） | 群组 |
| `/players` | 查看当前玩家列表 | 群组 |
| `/roll` | 开始投骰子并公布结果 | 主持人 |
| `/stop` | 结束游戏 | 主持人 |
| `/votestop` | 发起投票结束游戏（超半数通过） | 群组玩家 |
| `/kick` | 踢出玩家（回复目标玩家消息） | 主持人 |
| `/trans` | 转让主持人（回复目标玩家消息） | 主持人 |
| `/votetrans` | 投票转让主持人 | 群组玩家 |
| `/adminstop` | 强制结束游戏 | 群管理员 |
| `/admintrans` | 强制转让主持人 | 群管理员 |

---

## 🎯 游戏流程

```
1. 将 Bot 添加进群组
2. 任意成员 /createnewgame → 创建者成为主持人 👑
3. 其他玩家 /join 加入（至少需要 2 人）
4. 主持人 /roll → 每位玩家获得 1-99 随机点数
5. 点数最大 = 胜利者 🏆，点数最小 = 失败者 💀（并列则同时列出）
6. 群内出现「真心话 / 大冒险」内联按钮，仅失败者可点击选择
7. 失败者执行对应挑战
8. 主持人发送 /roll 开始新一轮
```

---

## 🔧 本地调试（可选）

```bash
pip install -r requirements.txt
cp .env.example .env   # 填写 UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN 等
python bot.py          # 本地默认 Polling 模式，无需隧道
```

> 本地未配置 `RENDER_EXTERNAL_URL` 时自动使用 Polling 模式，与原来一键启动的 Polling 行为一致；若本地要测 Webhook，需在 `.env` 中配置 `MODE=webhook` 与公网 `WEBHOOK_URL`。

---

## ❓ 常见问题

**Q：免费实例会休眠吗？**
A：Render 免费实例 15 分钟无请求会休眠，Telegram 推送更新时会自动唤醒（有冷启动延迟）。与网易云 Bot 的 Render 版行为一致。需要 7×24 秒回可升级 Starter 实例（render.yaml 的 `plan: free` 改为 `starter`）。

**Q：如何更换 Bot Token？**
A：直接在 Upstash 更新 `tod:config:bot_token`，然后重启 Render 服务（Deploy → Clear build cache & deploy 或 Restart）。无需改代码、无需改仓库。

**Q：游戏数据存在哪？**
A：Upstash Redis，key 前缀 `tod:game:*`（进行中的游戏）与 `tod:stats:*`（玩家胜负统计）。

---

## 📁 项目结构

```
truth-or-dare-render/
├── bot.py               # 主程序（全部逻辑，Render Webhook + 健康检查）
├── requirements.txt     # Python 依赖
├── render.yaml          # Render Blueprint 配置
├── .env.example         # 环境变量示例（本地调试用）
├── .gitignore           # 版本库忽略规则（.env 永不入库）
└── README.md            # 本文档
```

---

## 📄 License

[MIT](LICENSE)

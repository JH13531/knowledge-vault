# 飞书知识库剪藏机器人 — 配置指南

## 概述

通过飞书群消息发送 URL，机器人自动调用 clip-server 完成剪藏，并回复任务状态。

```
飞书群 @机器人 + URL → WebSocket 长连接 → feishu-bot.py
    → clip-server /api/clip-url → 平台适配器抓取 → 保存 raw/inbox/
    → 回复成功/失败卡片到群
```

**优势**：手机端飞书 → 机器人 → 知识库，无需电脑开机。

---

## 第一步：飞书开放平台创建/配置应用

### 1.1 进入开放平台

浏览器打开 https://open.feishu.cn/app → 选择已有应用或新建应用。

> 建议新建一个专用应用（如「知识库剪藏」），不要和 lark-cli 用的应用混用。

### 1.2 启用机器人能力

应用功能 → 机器人 → 开启

### 1.3 配置长连接接收事件

事件订阅 → 接收方式 → 选择 **「使用长连接接收事件」**

> 长连接模式不需要公网回调地址，适合本机部署。

### 1.4 添加事件订阅

事件订阅 → 添加事件 → 搜索并添加：

| 事件名称 | 事件标识 | 说明 |
|---------|---------|------|
| 接收消息 | `im.message.receive_v1` | 接收群内 @机器人的消息 |

### 1.5 添加权限

权限管理 → 搜索并开通：

| 权限名称 | 权限标识 | 说明 |
|---------|---------|------|
| 获取与发送单聊、群组消息 | `im:message` | 接收消息 |
| 接收群聊中 @机器人消息事件 | `im:message.group_at_msg` | 群内 @触发 |
| 以应用身份发送消息 | `im:message:send_as_bot` | 回复卡片通知 |

### 1.6 发布应用

版本管理与发布 → 创建版本 → 申请发布

> 企业管理员审批通过后生效。

### 1.7 添加到目标群

在飞书中打开目标群 → 群设置 → 群机器人 → 添加刚发布的应用机器人。

---

## 第二步：获取凭据并配置

### 2.1 获取 App ID 和 App Secret

飞书开放平台 → 应用凭证页面，复制：
- **App ID**：`cli_xxxxxxxxxxxxxxxx`
- **App Secret**：点击显示后复制

### 2.2 创建配置文件

```bash
cd tools
cp feishu-bot-config.example.json feishu-bot-config.json   # Windows: copy feishu-bot-config.example.json feishu-bot-config.json
```

编辑 `feishu-bot-config.json`：

```json
{
  "app_id": "cli_xxxxxxxxxxxxxxxx",
  "app_secret": "你的实际 app secret",
  "clip_server": "http://127.0.0.1:7878"
}
```

> ⚠️ `feishu-bot-config.json` 已在 `.gitignore` 中，不会被提交。

**或使用环境变量**（systemd 方式推荐）：

```bash
# 在 systemd service 中设置
Environment=LARK_APP_ID=cli_xxx
Environment=LARK_APP_SECRET=xxx
```

---

## 第三步：测试运行

### 3.1 确保 clip-server 已启动

```bash
systemctl --user status clip-server
# 如未启动：
systemctl --user start clip-server
```

### 3.2 手动测试

```bash
cd /path/to/knowledge-vault/tools/
python3 feishu-bot.py
```

应看到：
```
🤖 飞书知识库剪藏机器人
   App ID: cli_xxx...
   clip-server: http://127.0.0.1:7878
   连接模式: WebSocket 长连接

[*] 正在连接飞书长连接...
```

### 3.3 在飞书群测试

在目标群发送：

```
@知识库机器人 https://b23.tv/O2SpzrG
```

机器人应回复成功/等待卡片。

---

## 第四步：后台常驻运行

### Linux：systemd 服务（推荐）

```bash
# 重载 systemd
systemctl --user daemon-reload

# 启动
systemctl --user start feishu-clip-bot

# 查看状态
systemctl --user status feishu-clip-bot

# 开机自启
systemctl --user enable feishu-clip-bot

# 查看日志
journalctl --user -u feishu-clip-bot -f
```

### Windows / macOS

没有 systemd。**推荐直接用仓库自带的自启方案**：双击 `tools\autostart\enable-autostart.bat`，
它会同时把 clip-server 和 feishu-bot 一起托管（注册登录自启 + 立即启动、幂等、挂掉自动拉起）。
要关闭就双击同目录的 `disable-autostart.bat`。

详见 [`tools/autostart/README.md`](../autostart/README.md)。

想手动跑也行：`python tools/feishu-bot.py`（终端常驻，关窗口即停）。
需要真正的 Windows 服务可借助 [NSSM](https://nssm.cc/)。

> macOS 建议用 launchd；也可用上面那个 `vault-autostart.py`（本身跨平台）。

---

## 怎么用

- **入库**：群里 `@机器人 + 链接`（私聊直接发链接即可）。机器人回一张剪藏结果卡片，
  含平台、字数、文件名、链接。
- **跟踪更新**：消息以 `跟踪 ` 或 `追踪 ` 开头（如 `跟踪 https://...`），该 raw 会被标记为
  `live-source`，日后可检查源文档是否更新（见 `AGENTS.md` §4.5）。
- **卡片按钮**：
  - 「🔄 重新解读」→ 调 `wiki_gen` 重新生成 wiki 页（需配好 Tier 1 的 `claude`/`codex` CLI
    或 Tier 2 的 API key，见 [`tools/README.md`](../README.md)「Wiki 自动精加工」）
  - 「🔗 打开原文」「⚠️ 强制覆盖」「🔄 重试」「❌ 取消」按字面含义操作

> **机器人也要过 clip-server 的鉴权。** 它会自动读 `CLIP_SERVER_TOKEN` 环境变量或
> `tools/config.local.json` 里的 token，所以请先把 clip-server 的 token 配好再启动机器人。

---

## 支持的指令

| 指令 | 示例 | 说明 |
|------|------|------|
| 发送 URL | `@机器人 https://b23.tv/O2SpzrG` | 创建剪藏任务 |
| 帮助 | `@机器人 帮助` | 打印指令列表 |
| 统计 | `@机器人 统计` | 队列与入库统计 |
| 查询 | `@机器人 查询 abcd1234`（或 `查询 全部` / `查询 失败`） | 查询任务状态 |
| 重试 | `@机器人 重试 abcd1234` | 重试失败任务 |
| 取消 | `@机器人 取消 abcd1234` | 取消排队任务 |

---

## 消息去重机制

- 同一 URL 在 5 分钟内重复发送会被忽略
- clip-server 层面也有 URL 规范化去重（短链解析 + 平台稳定 ID）
- 机器人不处理自己发送的消息

---

## ⚠️ 与 OpenClaw 的冲突

一个飞书应用**只能建立一条**长连接。如果你之前在 OpenClaw 里也用同一个应用接了飞书，
消息会进 OpenClaw、**不进**本仓库的 `feishu-bot.py`。

测本仓库时二选一：

- 用**另一个独立飞书应用**（互不干扰，推荐）
- 临时停掉 OpenClaw 的连接，用同一个 app 跑本仓库的机器人

---

## 排错

| 现象 | 可能原因 | 解决 |
|------|---------|------|
| 机器人无响应 | 未发布/未加群 | 检查应用发布状态和群机器人列表 |
| 收到消息但不处理 | 消息格式不是文本 | 确保发送纯文本 URL |
| clip-server 连接失败 | clip-server 未启动 | `systemctl --user start clip-server` |
| 长连接断开 | 网络波动 | 已配置 `auto_reconnect=True`，自动重连 |
| 权限错误 | 未开通 im 权限 | 回到第一步 1.5 检查权限 |

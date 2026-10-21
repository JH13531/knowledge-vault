# 开源发布清单（Privacy & Security Checklist）

本文件记录 knowledge-vault 开源前的去隐私要求。导出工具：`tools/export_opensource.py`。

## 1. 密钥与凭据

| 文件 / 位置 | 内容 | 处理方式 |
|---|---|---|
| `tools/feishu-bot-config.json` | 真实飞书 `app_id` / `app_secret` | 不上传（`.gitignore`），仅提交 `feishu-bot-config.example.json`；发布前轮换密钥 |
| `tools/config.local.json` | clip-server 鉴权 token | 不上传（`.gitignore`），仅提交 `config.example.json`；发布前轮换 |
| `tools/sanitize.local.json` | 脱敏规则里的真实公司名 / 本机路径 / 内部域名 | 不上传（`.gitignore`），仅提交 `sanitize.example.json` |
| `.claude/settings.local.json` | 历史命令中的 LLM API Key（DashScope / 火山方舟） | 整个 `.claude/` 不上传；发布前轮换两个 Key |
| `tools/feishu-bot.log` | WebSocket `access_key`/`ticket`、群 ID、业务消息内容 | `*.log` 不上传；日志不长期保留 |
| `*.bak-*`（如 `feishu-bot.py.bak-20260904`） | 历史代码备份，可能含旧逻辑/密钥 | `*.bak*` 不上传 |

统一管理原则：密钥只存在于被忽略的本地配置或环境变量，仓库中永远只有
`*.example.json` 占位模板。支持的环境变量：

- `CLIP_SERVER_TOKEN` — clip-server 鉴权 token（clip-server / feishu-bot / playwright-worker 共用）
- `LARK_APP_ID` / `LARK_APP_SECRET` — 飞书应用凭据

安全默认：clip-server 绑定非回环地址且未配 token 时拒绝启动；CORS 默认仅放行浏览器扩展与本机 origin。

### 脱敏规则为什么外置

`export_opensource.py` 里**不硬编码任何公司名 / 本机路径 / 内部域名**，这些字串一律放在
`sanitize.local.json`（已被 `.gitignore` 忽略）。因为导出脚本本身就是被导出的文件之一——
规则写死在脚本里，导出时会被自己替换掉自己，规则退化成空操作
（`re.compile(r"某公司") → "某公司"`），公开版脚本的脱敏能力就废了。

新机器首次使用：`cp tools/sanitize.example.json tools/sanitize.local.json` 再填真实值。
缺这个文件时脚本会提示一行、跳过自定义脱敏，其余功能照常。

## 2. 私有内容

| 位置 | 内容 | 处理方式 |
|---|---|---|
| `raw/` | 第三方文章全文快照、剪藏图片（版权 + 隐私） | 不发布，导出时仅保留空骨架目录 |
| `wiki/` | 提炼笔记，含「我的思考」个人内容 | 不发布，导出时仅保留空骨架目录 |
| `reviews/ADR-001` 等 | 公司名、本机路径 | 由导出脚本自动脱敏（`某公司` / `/path/to/...`） |
| `tools/GAP-ANALYSIS.md` | 内部飞书 wiki 链接 | 不发布，由 `sanitize.local.json` 的 `exclude_files` 排除 |
| `tools/runtime/*.db*`、`tools/clip-tasks.db` | 任务队列数据库 | `*.db*` 不上传 |
| `__pycache__/` | 字节码缓存 | 不上传 |

## 3. 导出步骤

```bash
# 1) 导出干净的框架版本到 dist/knowledge-vault（只读源目录，不删任何原文件）
python3 tools/export_opensource.py --force

# 2) 人工抽查导出目录（重点：AGENTS.md、reviews/、tools/docs/）
find dist/knowledge-vault -type f | sort

# 3) 对源目录做一次密钥扫描（只读）
python3 tools/export_opensource.py --scan

# 4) 在导出目录初始化公开仓库
cd dist/knowledge-vault
git init && git add . && git status   # 确认无 raw/wiki 内容、无密钥文件
```

## 4. 本地待清理项（需用户确认后手动删除）

以下文件不影响开源（导出脚本已排除），但建议本地清理，删除前请人工确认：

- `tools/feishu-bot.py.bak-20260904` — 旧备份
- `tools/feishu-bot.log` — 含历史消息内容
- `tools/clip-tasks.db`（0 字节）与 `tools/runtime/clip-tasks.db*`（任务数据库）
- 各处 `__pycache__/`

## 5. 发布前必做

- [ ] 在飞书开放平台轮换 `app_secret`
- [ ] 在 DashScope / 火山方舟轮换历史泄露过的 API Key
- [ ] 确认 `git status` 中不含 `raw/`、`wiki/`、配置、日志、数据库
- [ ] 补充 LICENSE 与根 README（见后续步骤）

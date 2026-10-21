# Codex 未完成任务交接

> 整理日期：2026-09-30  
> 来源：Codex 线程 `01a0eb33-0d36-77a0-8298-c820d27e0da8`（标题「确认GitHub上传内容」）  
> 说明：本文件只是任务交接记录，未修改仓库中任何既有文件。

---

## ⚠️ 当前状态（2026-10-07 核验，先读这段）

下面「三、未完成任务」里的两项**都已经处理完**，本文件现在只剩历史价值：

| 任务 | 现状 |
|---|---|
| 任务 1：飞书报「三级精加工通道均不可用」 | ✅ 已修。根因是 `wiki_gen.py` 的 `CliProvider.available()` 把 `shutil.which()` 返回的 `""` 当成非 None，导致**永远报可用**；已改为 `bool(self.executable())`，并把文案改成「PATH 中找不到 claude/codex」以区分「没装」与「不在 PATH」 |
| 任务 2：悬浮按钮抓 B 站视频只有元数据、没字幕 | ✅ 已实现。根因是**字幕不在 DOM 里**，插件读 DOM 天然拿不到；改为页面内直调 B 站字幕 API（自带 MD5 + WBI 签名），`cid` 改从 `view` 接口取（不再依赖隔离世界里读不到的 `window.__INITIAL_STATE__`） |

另外注意：文中提到的「Windows 上后端 yt-dlp 拿不到 B 站字幕」**至今仍然成立** ——
那是 Chrome App-Bound Encryption 的设计限制，见
[`windows-vs-linux-browser-profile.md`](windows-vs-linux-browser-profile.md)。

---

## 一、Codex 为什么停在这里

用户切换模型后，该线程从 2026-09-30 15:34 起每个 turn 都请求失败，无法继续。

**报错原文**

```
The supported API model names are deepseek-flash, deepseek-v4-pro,
but you passed ark-code-latest.
```

**根因**

`~/.codex/config.toml` 不是手写的，由 **cc-switch** 生成并覆盖。当前生效的 Codex provider 是  
「火山（Openai）备用2」（`universal-codex-394dc307-…`，库里 `is_current=1`），其配置为：

| 项          | 值                                                 |
| ---------- | ------------------------------------------------- |
| `model`    | `ark-code-latest`                                 |
| `base_url` | `https://ark.cn-beijing.volces.com/api/coding/v3` |

而该端点只接受 `deepseek-flash` / `deepseek-v4-pro`。

**为什么在界面里切模型没用**

线程记录里 `thread_settings_applied` 显示切过三次模型——  
`deepseek-v4-pro` → `deepseek-flash` → `deepseek-v4-flash-vision-exp`，  
但每次实际发出的请求仍旧携带 `ark-code-latest`。该值来自 config.toml，而 config.toml 由 cc-switch 覆盖。

**最后一个 turn 的报错已变成**

```
exceeded retry limit, last status: 429 Too Many Requests
```

**恢复办法**

1. 在 cc-switch 里把该 provider 的 model 由 `ark-code-latest` 改为 `deepseek-v4-pro`（或 `deepseek-flash`），保存并重新切换一次；
2. **新开一个会话**，不要 resume——该 thread 里切了三次模型都没生效，说明 resume 会沿用 thread 里持久化的旧模型；
3. 不要直接手改 `~/.codex/config.toml`，下次 cc-switch 一动作就会被覆盖。

---

## 二、线程定位

| 项         | 值                                                                                                     |
| --------- | ----------------------------------------------------------------------------------------------------- |
| Thread ID | `01a0eb33-0d36-77a0-8298-c820d27e0da8`                                                                |
| 会话文件      | `~/.codex/sessions/2026/09/29/rollout-2026-09-29T11-26-26-01a0eb33-0d36-77a0-8298-c820d27e0da8.jsonl` |
| 消息数       | 284                                                                                                   |
| 工作目录      | `/path/to/knowledge-vault`                                                                   |
| 线程起点      | 用户提问：「我今天准备上传项目到github，现在只会上传dist的内容而不会上传我的私人wiki对吗」                                                  |

> ⚠️ 另有一条**同名空壳线程** `01a0f13c-b541-73b1-9745-374beeddf511`（9-30 创建），  
> 文件里只有 `thread_settings_applied` 和报错事件、**一条消息都没有**。只读这条会读不到任务内容。

---

## 三、未完成任务

线程中断时，用户最后一条有效消息提出两个问题，Codex 查证到一半即失败。

### 任务 1：飞书报「三级精加工通道均不可用」

**用户原话**

> 飞书提示"三级精加工通道均不可用（未安装 claude/codex，且未配置 API key），raw 保留，可稍后重试。"

**Codex 断点**

> 「两个问题都查。先诊断第一个——服务进程里的 PATH 是否找不到 claude/codex」

诊断未出结论。

**已定位的代码位置**

| 位置                        | 内容                                            |
| ------------------------- | --------------------------------------------- |
| `tools/feishu-bot.py:498` | 用户看到的这段文案                                     |
| `tools/wiki_gen.py:541`   | `{provider.name} 未成功，尝试下一个精加工通道…`             |
| `tools/wiki_gen.py:551`   | `无可用的 wiki 精加工通道（未安装 Agent CLI 且未配置 API key）` |

**待办**

- 确认 feishu-bot 服务进程的实际 `PATH`——由 systemd 启动的进程 PATH 常与登录 shell 不同，很可能只是找不到 `claude` / `codex` 可执行文件，而非真的没装；
- 若确为 PATH 问题：给服务单元补 `Environment=PATH=…`，或改用绝对路径；
- 若不想依赖本地 CLI：在服务配置里补一个 OpenAI 兼容 API key，走 API 通道。

### 任务 2：悬浮按钮抓 B 站视频只入库元数据、没有字幕

**用户原话**

> 那不对啊，点击悬浮按钮的目的是快速完整入库，内容是不完整的那意义何在？

**Codex 断点**

> 「Chromium 没在运行，用它的 profile 在页面上下文里实测字幕 API」

最后一个工具调用是临时脚本 `/tmp/test_bili_api3.py`（Playwright 在页面上下文里测 B 站字幕 API），  
输出中取到了 `SESSDATA`，随后 turn 因 429 失败。

**已经查明的事实**（Codex 上一轮已确认）

| 位置                                                 | 事实                                                                       |
| -------------------------------------------------- | ------------------------------------------------------------------------ |
| `tools/clip-extension/platforms/bilibili.js:65-79` | 读页面现成的 `window.__INITIAL_STATE__`：标题 / UP 主 / 简介 / 标签 / 播放量。零网络请求，所以"秒完" |
| `tools/clip-extension/platforms/bilibili.js:313`   | 文件里自己写着：`> ⚠️ 视频字幕需要后端处理后获取。如需完整字幕，请使用 inbox-add 命令触发后端抓取。`              |
| `tools/platforms/bilibili.py`（后端）                  | 完整字幕只有 yt-dlp 这条慢路径能给                                                    |

结论：**悬浮按钮对 B 站视频入库的是"信息卡"而不是完整内容**，用户看到的"成功"具有误导性。

**待决方案**（Codex 提出，用户尚未拍板）

- 方案 A：悬浮按钮在只拿到元数据时明确提示「仅元数据，点此获取完整字幕」；
- 方案 B：视频类页面直接默认走后端队列，不再走扩展的秒级路径。

用户的原话倾向是「内容不完整就没意义」，即倾向让悬浮按钮也能拿到完整内容，而不仅是加提示。

---

## 四、同一线程中已完成、无需重做的部分

| 项                         | 状态                                                                           |
| ------------------------- | ---------------------------------------------------------------------------- |
| `tools/feishu-bot.py:486` | 通知改为真实完成后发送：`generate_wiki_topic` 返回成功才发「✅ 已重新解读完成」，三级回退全失败则发「❌ 重新解读失败」并说明原因 |
| `requirements.txt`        | 补上 `youtube-transcript-api>=0.6`——`tools/platforms/youtube.py:218` 在用但此前漏写   |
| `dist`                    | 当时已同步（其后在 2026-09-30 被重新导出覆盖，见下）                                             |

---

## 五、2026-09-30 另做的开源导出修复（Claude Code 完成）

与上述两个任务无关，同属该仓库的开源准备，一并记录：

- `dist/knowledge-vault/` 重新导出：此前那份全部文件为 CRLF 行尾，且多带一个本该排除的 `tools/GAP-ANALYSIS.md`；
- 脱敏规则从 `tools/export_opensource.py` 外置到 `tools/sanitize.local.json`（本地、已忽略）+ `tools/sanitize.example.json`（随仓库发布）——原先规则写死在脚本里，导出时会被自己替换掉自己，公开版脚本的脱敏能力实际失效；
- 导出脚本 `write_text` 增加 `newline="\n"`，避免在 Windows 上导出整树 CRLF；
- 密钥扫描规则的 `\s` 收紧为 `[ \t]`，修掉一个跨行误报（会把注释行的 `app_secret` 与下一行单词连成"密钥"并中止导出）；
- 核对结果：61 个文件全 LF、密钥扫描 0 命中、无 `raw/` `wiki/` 私有内容、无公司名与内部域名。

**尚未执行**：`git init` 与推送（用户明确说先不做）。

---

## 六、给接手者的提醒

1. 先看第三节的两个任务，那是 Codex 真正没做完的部分；
2. 该仓库**没有 git**（无 `.git`），改动没有历史可退，动手前自行备份；
3. `tools/sanitize.local.json` 是本机私有文件，含公司名 / 本机路径 / 内部域名，不要提交、不要外发。

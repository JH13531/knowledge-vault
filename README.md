# knowledge-vault

AI 协作的个人知识库治理框架：一套「原始证据 → 结构化知识」的三层规范、一个体检脚本，\
以及一键网页/视频剪藏工具链（本地服务 + 浏览器扩展 + 飞书机器人）。

灵感来自 Karpathy 的 LLM Wiki 模式。本仓库是**可直接使用的框架模板**：\
`raw/`、`wiki/` 默认只有空骨架，你存入的内容属于私有数据，不随仓库分发。

## 特性

- **三层架构与治理契约**：`raw/`（不可变原始快照）→ `wiki/`（AI 提炼的知识）→ `AGENTS.md`/`reviews/`（规则与 ADR），双向溯源
- **lint 体检**：frontmatter 合规、孤儿素材、死链、triage 衰减、标签重叠、矛盾双向标注等检查，默认 dry-run
- **一键剪藏**：浏览器扩展（Chrome/Edge，Manifest V3）+ 本地服务，支持普通网页、YouTube、B 站、微信公众号文章、小红书、飞书文档；需要登录态的页面走浏览器协助队列 / Playwright worker（**哪些情况抓不到、抓不全，见下方 [已知局限](#已知局限)**）
- **飞书机器人**：手机端发链接给机器人即可入库
- **Wiki 自动精加工**：raw 落库后自动尝试提炼成主题页（本地 `claude`/`codex` CLI 或 OpenAI 兼容 API key 二选一），详见 [tools/README](tools/README.md)「Wiki 自动精加工」
- **图片文字 OCR（可选）**：正文写在图片里的笔记（小红书图片笔记等）入库时识别为「图多字少」，交**下游** OCR（多模态 API 或本地命令）提取文字后重跑 wiki；**默认自动提取**，想省下这一次可在剪藏时叫停（插件取消勾选「提取图片文字（OCR）」／飞书消息加 `不OCR` 前缀）。详见 [tools/README](tools/README.md)「图片文字 OCR」
- **默认安全**：回环监听、Bearer Token 鉴权、CORS 白名单、图片下载 SSRF 防护；密钥只走本地配置或环境变量，不入库

## 目录结构

```
├── AGENTS.md          # 治理契约：规则 + frontmatter 格式 + 操作流程（先读这个）
├── lint.py            # 知识库体检脚本（python lint.py）
├── templates/         # raw / topic / query / ADR 模板
├── reviews/           # ADR 架构决策记录（示例）
├── tools/             # 剪藏工具链（详见 tools/README.md）
│   ├── clip-server.py        # 本地接收服务（127.0.0.1:7878）
│   ├── clip-extension/       # 浏览器扩展（Chrome/Edge）
│   ├── task_queue.py         # 浏览器协助任务队列
│   ├── wiki_gen.py           # 入库后自动提炼 wiki 页（可选）
│   ├── ocr_images.py         # 下游 OCR：提取图片里的文字（可选）
│   ├── content_measure.py    # 「真实正文」的统一度量口径（门禁 / 去重 / OCR 候选共用）
│   ├── feishu-bot.py         # 飞书机器人（可选）
│   ├── playwright-worker.py  # 浏览器协助 worker（可选）
│   ├── autostart/            # 开机自启 / 守护启动器（可选）
│   ├── docs/                 # 配置步骤、平台差异等专题文档
│   └── platforms/            # 多平台抓取适配器
├── raw/               # 证据层：文章/笔记/剪藏快照（使用时由你填充，默认空）
└── wiki/              # 知识层：主题页与问答产物（使用时由你填充，默认空）
```

## 快速开始

**前提**：Python 3.10+。Node 仅在用浏览器扩展 / Playwright worker / 飞书文档 CLI（`lark-cli`）时需要，
跑 lint 和剪藏核心不需要。各平台的命令写法差异见下方「[平台差异](#平台差异)」。

### 1. 安装依赖

```bash
pip install -r requirements.txt    # 核心只需 requests / beautifulsoup4 / trafilatura / PyYAML
```

最小安装（只跑 lint + 剪藏服务）：

```bash
pip install requests beautifulsoup4 trafilatura PyYAML
# 要抓 YouTube / B 站字幕，再补：pip install yt-dlp
```

> **飞书文档入库另需飞书官方 CLI（npm 工具，不是 pip）**：若你想测试「飞书文档 / 知识库」类链接
> 入库（**不是**飞书消息），需额外 `npm install -g @larksuite/cli`（命令为 `lark-cli`），
> 再 `lark-cli auth login` 扫码授权。它只被飞书文档适配器（`feishu.py`）和 live-source 更新用到；
> 飞书**消息**机器人（`feishu-bot.py`）只用 `lark-oapi`，**不依赖 lark-cli**。
> 国内网络可用镜像：`npm install -g @larksuite/cli --registry=https://registry.npmmirror.com`

然后读 [`AGENTS.md`](AGENTS.md)，它是 AI 与人类共同遵守的唯一治理契约。

### 2. 体检空仓库

```bash
python lint.py          # 输出报告；默认只读，不修改任何文件
```

### 3. 启动剪藏服务

```bash
# 推荐：启用鉴权（token 自己生成，服务端与插件填同一串）
python -c "import secrets; print(secrets.token_urlsafe(32))"   # 生成 token
cp tools/config.example.json tools/config.local.json            # 填入 token（Windows 用 copy）
python tools/clip-server.py

# 或跳过配置文件，直接用环境变量
CLIP_SERVER_TOKEN=xxx python tools/clip-server.py      # Windows: set CLIP_SERVER_TOKEN=xxx && python ...

# 临时调试也可以不配 token（仅监听 127.0.0.1，不建议长期这样）
python tools/clip-server.py
```

服务默认只监听 `127.0.0.1:7878`，本机安全。

### 4. 安装浏览器扩展

- Chrome / Edge：`chrome://extensions` → 开发者模式 → 加载已解压的扩展程序 → 选择 `tools/clip-extension/`
- 打开扩展设置页，服务地址填 `http://127.0.0.1:7878`；启用鉴权后把**同一串 token**\
  粘进 Token 输入框，点「测试连接」——页面会明确提示 Token 是否验证通过，并内联「如何获取 Token」的步骤

### 5. 开始使用

- 页面右下角悬浮按钮：悬停展开「存入待审区 / 完整入库」
- 或按 `Ctrl/Cmd+Shift+K`、右键「入库此页」
- **选中正文剪藏**：先鼠标选中页面正文，再点悬浮按钮 / 右键菜单 / 快捷键，
  仅把选中文字作为正文入库。适合正文藏在 iframe 或动态渲染、整页剪藏抓不全的场景
- 或直接 API：`curl -X POST http://127.0.0.1:7878/api/clip-url -H 'Content-Type: application/json' -d '{"url":"..."}'`

## 已知局限

> 这一节集中回答「什么情况下抓不到、抓不全」。入库结果不符预期时，先对照这里：
> **抓取能力的边界**（iframe / 懒加载 / 动态渲染 / 后端抓取得少）、
> **内容完整性口径**（图多字少 / 字数统计 / 覆盖需确认）、
> **登录态与平台差异**（Windows 的 cookie 加密）、
> **需要额外组件的平台**（飞书文档要 lark-cli）。

### 抓取能力的边界

- **跨域 iframe 正文可抓取**：若目标站把正文放在另一个域名的 iframe 内
  （典型如某商户经营平台 `merchant.example.com` 的 `iUrl` 套壳页，正文在 `h5.example.com`），
  扩展会**逐 frame 提取并选取正文最丰富的那一个**（`all_frames: true` + 在各 frame
  内跑平台适配器），因此正文可以正常入库。
- **懒加载长文会自动滚动后提取**：长正文常按滚动位置分批渲染。剪藏时扩展会先
  **在每个 frame 内滚动到底再回顶**，等页面把下游内容都渲染出来之后才提取，
  所以即便用户只浏览到一半，入库的也是全篇而非首屏。滚动步骤会带来约 1 秒延迟。
- **动态渲染内容须等渲染完成**：普通 SPA 页面若在点剪藏时尚未完成 JS 渲染，
  会抓到「正在加载」骨架。平台站（YouTube / B 站等）适配器会主动等待内容就绪，
  通用站点在自动滚动后即提取，请确认页面已开始渲染内容后再剪。
- **同源限制下的后端抓取更少**：飞书机器人等「无浏览器在场」的路径走后端抓取，
  对套壳 SPA 页面只能拿到服务端渲染的静态部分（通常比浏览器里少），
  且不会渲染 iframe。这类页面建议在浏览器中剪藏。
- **图片不保证全部本地化**：单篇最多本地化 30 张，且单张超过 10MB、类型不受支持
  （如 SVG）、URL 不安全或下载失败的都会跳过。跳过的图片**在正文里保留远程地址**
  （源站删除即失效），并在笔记里写明「图片未全部本地化」，不会静默略过。

### 内容完整性口径

- **正文写在图片里时，不会自动变成文字**：若一篇笔记「真实正文不足 200 字且含图」
  （典型如小红书长图、微信公众号图片消息），它**不会被拿去生成 wiki** —— 否则只会
  基于那几十个字产出一页貌似有内容、实则无用的 wiki。配好 OCR 通道后，图片里的文字
  会在**下游**被提取、补进笔记（`## 图片文字（OCR）` 一节）再重跑 wiki。OCR 与 wiki 在
  **后台**跑完后会**补发一条结论通知**（飞书卡片 / 任务队列），不必守着界面等；
  **没配 OCR 时如实标记「未生成 wiki（未做 OCR）」**，不会假装完整。配置见
  [tools/README](tools/README.md)「图片文字 OCR」。
- **显示的「字数」只统计真实正文**：raw 头部与界面的字数已剔除图片链接、元数据行、
  话题标签、结构小标题等**脚手架**。因此不会再出现「正文只有 19 字的图文笔记显示
  『约 232 字』」这种虚高 —— 字数是对外承诺「剪到了多少」。
- **重复内容不会被自动覆盖**：抓到同一来源时一律回报「已存在」并给出**两版字数对比**；
  **即便本次抓得更完整，也需要你点「强制覆盖」才会替换** —— 覆盖会连同已入库那份里的
  `## 我的笔记` 一起删除，这件事不能替你做主。live-source 类型的内容更新走「新增版本
  （`-v2` 起）」，旧版完整保留，不经此确认。

### 登录态与平台差异

| 场景                                       | Linux / macOS                               | Windows                                                                    |
| ---------------------------------------- | ------------------------------------------- | -------------------------------------------------------------------------- |
| 浏览器**已打开**，使用扩展剪藏                        | ✅                                           | ✅                                                                          |
| 浏览器**未打开**，剪藏公开网页                        | ✅                                           | ✅                                                                          |
| 浏览器**未打开**，剪藏 bilibili / YouTube（字幕）     | ❌ 两个平台都走浏览器协助队列，字幕只能在**已登录的页面内**获取，关着浏览器抓不到 | ❌ 同左（此类站与能否读 cookie 无关，全平台都依赖打开的浏览器）                                       |
| 浏览器**未打开**，剪藏**后端适配器可用**的登录站点（公众号 / 小红书） | ✅ 后端适配器可直接读取浏览器 cookie（Linux 上为明文 SQLite）   | ❌ Chrome ≥ 127 的 **App-Bound Encryption** 将 cookie 解密密钥绑定在浏览器二进制上，外部进程无法解密 |

即：**在 Windows 上，剪藏「后端适配器可用」的登录站点（公众号 / 小红书等）必须有一个
已打开的浏览器参与**（由扩展在页面内抓取，或经由浏览器协助队列）。而上面 bilibili /
YouTube 一栏的 ❌ 在 **Linux 上同样成立** —— 这类视频站全平台都依赖打开的浏览器在页面内
取字幕，与 Windows 的 cookie 加密无关；Windows 独有的限制是下一项「后端适配器可用的
登录站点」。「用无头浏览器复用已有 profile」同样不可行 ——
Chrome 拒绝在默认 profile 目录上开放调试端口，而改用独立 profile 则没有登录态。
这不是配置问题，而是 Chrome 的安全设计，无法绕过。原因与 12 项实测证据见
[`tools/docs/windows-vs-linux-browser-profile.md`](tools/docs/windows-vs-linux-browser-profile.md)，
其中亦记录了各平台的差异实测。

实际使用建议：

- **首选浏览器扩展** —— 登录态天然继承，两个平台行为一致；
- 手机转发 / 飞书机器人等「无浏览器在场」的场景，在 Windows 上**只能剪藏公开网页**；
  需要登录的链接会进入待审区，待浏览器打开后由扩展补抓。

### 需要额外组件的平台

- **飞书文档必须安装 `lark-cli`**（`npm install -g @larksuite/cli` 并 `lark-cli auth login`）。
  它的正文在浏览器里也不完整：扩展的飞书适配器依赖内层滚动容器、必须已登录，
  而 `playwright-worker.py` 根本没有飞书适配器（只对 `window` 滚动，而飞书滚的是内部容器，
  只能拿到首屏）。**因此飞书文档不降级到浏览器协助队列**——缺 lark-cli 时直接失败并说明原因，
  不会用一份残缺快照冒充完整入库。
- **B 站 / YouTube 的浏览器协助**：这两个站的后端适配器（`platforms/bilibili.py`、
  `platforms/youtube.py`）**当前永不执行**（被 `_prefer_browser()` 旁路，仅作备用实现）。
  它们的字幕完全依赖浏览器协助队列——**需要浏览器开着、且已登录目标站点**。
- 若用 `playwright-worker.py` 代替浏览器扩展消费浏览器协助队列：它没有各平台的专用适配器，
  对 SPA / 虚拟列表类页面（飞书、小红书等）只能拿到首屏，**建议优先用浏览器扩展**。

***

## 可选组件

以下三项都是**跨平台**的，按需启用。

### 飞书机器人（手机发链接入库）

在飞书开放平台建个自建应用、开机器人 + 长连接，加事件 `im.message.receive_v1` 和权限
`im:message` / `im:message.group_at_msg` / `im:message:send_as_bot`，然后：

```bash
pip install lark-oapi
python tools/feishu-bot.py
```

群内 `@机器人 + 链接` 即入库，私聊直接发链接也行。机器人也要读 clip-server 的 token，请先完成第 3 步。
完整配置（三种凭据方式、指令、卡片按钮、与 OpenClaw 冲突说明）见
[`tools/docs/feishu-bot-setup.md`](tools/docs/feishu-bot-setup.md)。

> 区分清楚：这是**飞书消息**机器人（手机发链接），只用 `lark-oapi`，**不需要** `lark-cli`。

### 开机自启（不用每次重启都手动开两个终端）

`clip-server` 和 `feishu-bot` 都是前台脚本，默认重启后要手动再起一次。Windows 上直接双击
`tools\autostart\` 下的两个文件即可（会弹一次管理员确认，点「是」，不用开命令行）：

| 双击 | 效果 |
|---|---|
| `enable-autostart.bat` | 注册登录自启 **并立即启动服务** |
| `disable-autostart.bat` | 停止服务 **并移除登录自启** |

**开发者日常命令**（`vault-autostart.py` 跨平台可用）：

```bash
python tools/autostart/vault-autostart.py --status   # 看状态（守护/两个服务/端口/飞书凭据）
python tools/autostart/vault-autostart.py            # 启动 + 守护（幂等，可重复执行）
python tools/autostart/vault-autostart.py --stop     # 关掉：停守护进程，并停掉它启动的服务
```

> 注意：`--stop` 只停当前服务；若登录计划任务还挂着，下次登录仍会自动启动。
> 想彻底不再自启，用 `disable-autostart.bat`（或 `manage-autostart.ps1 -Action uninstall`），它会先停服务再移除任务。

日志在 `tools/runtime/logs/`（`autostart.log` 最常看）。原理与幂等设计见
[`tools/autostart/README.md`](tools/autostart/README.md)。
Linux / macOS 请改用 systemd `--user` 单元或 launchd（`vault-autostart.py` 本身跨平台可用）。

### Wiki 自动精加工

raw 落库后，后台自动尝试提炼成 wiki 主题页，三级回退：

- **Tier 1**：本机装了 `claude` 或 `codex` CLI，自动读 `AGENTS.md` 自主提炼（质量最高）
- **Tier 2**：配置 OpenAI 兼容 API key（DeepSeek / 豆包等），单次调用直出
- **Tier 3**：都没配 → 跳过，raw 留作孤儿日后手动提炼

Tier 2 开启：

```bash
cp tools/wiki-config.example.json tools/wiki-config.local.json
# 填入 api_key / base_url / model（或用环境变量 WIKI_GEN_API_KEY / WIKI_GEN_BASE_URL / WIKI_GEN_MODEL）
```

默认 Tier 1/2 都没配也不影响剪藏，只是不产生 wiki 页。另可用 `python tools/retry-wiki.py`
给已入库的 raw 补生成。完整说明见 [tools/README](tools/README.md)「Wiki 自动精加工」。

**正文写在图片里的笔记**（小红书长图 / 微信图片消息）：入库时会先剔除图片后统计真实
文字，识别为「图多字少」时不产垃圾 wiki；配好 OCR 通道即自动在**下游**提取图片文字并
重跑 wiki（多模态 API 或本地命令，见 [tools/README](tools/README.md)「图片文字 OCR」）。
未配通道则如实标为 `wiki: skipped`。

***

## 平台差异

| <br />           | Linux / macOS                 | Windows                                       |
| ---------------- | ----------------------------- | --------------------------------------------- |
| Python 命令        | `python3`                     | `python`（安装时须勾选 Add to PATH）                  |
| 复制文件             | `cp a b`                      | `copy a b`                                    |
| pip 安装到系统 Python | 可能需 `--break-system-packages` | 不需要                                           |
| 环境变量             | `VAR=x cmd`                   | `set VAR=x && cmd`（PowerShell：`$env:VAR="x"`） |
| 后台常驻 / 开机自启      | systemd `--user` 单元、launchd   | 计划任务（见「开机自启」）                                 |
| 本机代理             | 注意 `NO_PROXY` 含 `127.0.0.1`   | 同左                                            |

剪藏核心、浏览器扩展、飞书机器人三层都跨平台；只有常驻方式与上面这些命令写法不同。

**各平台实际使用的抓取通道**（别与「适配器注册表」混淆）：

| 平台                        | 实际通道              | 失败 / 例外                                                    |
| ------------------------- | ----------------- | ---------------------------------------------------------- |
| 飞书文档                      | 后端 `lark-cli`     | 缺 lark-cli 时**直接报错**，不降级（见 [已知局限](#已知局限)）                   |
| 微信公众号 / 小红书               | 后端适配器             | 失败才降级浏览器协助队列                                               |
| 普通网页                      | 后端 trafilatura    | 同上；动态渲染页后端只拿得到静态骨架                                         |
| B 站 / YouTube             | 浏览器协助队列           | 后端适配器（`bilibili.py` / `youtube.py`）**当前永不执行**，仅作备用实现       |

`tools/platforms/` 里注册了 6 个适配器并按 `priority` 排序，但**注册表优先级 ≠ 实际生效通道**：
`clip-server.py` 的 `_prefer_browser()` 按主机名把 B 站 / YouTube 直接送进浏览器协助队列，
这两个后端适配器从不被调用。登录态相关的边界（含 Windows 的 cookie 加密）见
[已知局限 → 登录态与平台差异](#登录态与平台差异)。

## 安全模型

| 风险         | 默认行为                                                                             |
| ---------- | -------------------------------------------------------------------------------- |
| 未授权访问      | 默认只监听 `127.0.0.1`；绑定非回环地址且无 token 时拒绝启动                                          |
| 跨站请求（CSRF） | CORS 仅放行浏览器扩展与本机 origin，公网网页被拒                                                   |
| 凭据泄露       | `config.local.json`、`.claude/`、日志、数据库均在 `.gitignore`；支持 `CLIP_SERVER_TOKEN` 环境变量 |
| SSRF       | 图片下载拦截内网/环回/云元数据地址，重定向逐跳校验，单图 10MB 上限，拒绝 SVG                                     |
| 路径穿越       | inbox 处理只接受单层 `.md` 文件名                                                          |

> **Token 要不要设？** 纯本机自用（默认只监听 `127.0.0.1`）可以留空——\
> CORS 白名单已经挡住公网网页向本地服务发起请求；一旦用 `--host` 监听\
> 非回环地址（局域网/服务器）则必须配置，否则服务拒绝启动。

详见 [`tools/README.md`](tools/README.md) 的「安全模型」章节。

## 文档

- [`AGENTS.md`](AGENTS.md) — 治理契约（三层架构、frontmatter、lint 规则、摄入/查询流程）
- [`tools/README.md`](tools/README.md) — 剪藏工具链完整文档
- [`templates/`](templates/) — 各类文件模板
- [`reviews/`](reviews/) — 设计决策记录（ADR，示例）
- [`tools/docs/`](tools/docs/) — 专题文档：飞书机器人配置、平台差异分析
- [`tools/OPENSOURCE.md`](tools/OPENSOURCE.md) — 发布去隐私清单（从私有库开源时参考）

## 许可证

[MIT](LICENSE)

> 注意：`raw/` 中的剪藏内容可能涉及第三方版权，属于你的私有数据，\
> 本仓库不分发、不主张任何相关权利。


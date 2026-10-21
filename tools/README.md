# 浏览器剪藏插件 + 本地服务

一键将网页/视频内容剪藏到 knowledge-vault 的 `raw/inbox/`。

## 组成

```
tools/
├── README.md              # 本文件
├── clip-server.py         # 本地接收服务（需 requests/bs4/trafilatura，见依赖）
├── clip-extension/        # Chrome 浏览器插件（Manifest V3）
├── task_queue.py          # 浏览器协助任务队列
├── wiki_gen.py            # 入库后自动提炼 wiki 页（Tier 1/2/3 回退）
├── ocr_images.py          # 下游 OCR：把「写在图片里」的文字提取出来补进笔记（可选）
├── content_measure.py     # 「真实正文」的统一度量口径（门禁 / 去重比较 / OCR 候选共用）
├── platforms/             # 多平台适配器
│   ├── __init__.py
│   ├── base.py            # 基类和数据结构
│   ├── registry.py        # 自动注册
│   ├── feishu.py          # 飞书文档（lark-cli，缺了就报错不降级）
│   ├── youtube.py         # YouTube（备用实现，当前被 _prefer_browser 旁路）
│   ├── bilibili.py        # B 站（备用实现，当前被 _prefer_browser 旁路）
│   ├── wechat.py          # 微信公众号（服务端直抓，失败才降级浏览器协助）
│   ├── xiaohongshu.py     # 小红书（服务端直抓，失败才降级浏览器协助）
│   └── webpage.py         # 普通网页（trafilatura）
├── feishu-bot.py         # 飞书机器人（WebSocket 长连接，手机发链接入库）
└── playwright-worker.py   # Playwright 浏览器协助 Worker
```

## 快速开始

### 1. 启动本地服务

```bash
cd knowledge-vault
python3 tools/clip-server.py
# 监听 127.0.0.1:7878
```

依赖：
- 核心：`requests` + `beautifulsoup4` + `trafilatura`（clip-server 启动会自动导入 platforms，缺任一会报错）
- 多平台抓取：
  - `yt-dlp`（YouTube / B 站视频字幕下载，pip 可装：`pip install yt-dlp`）
  - `lark-cli`（飞书文档，npm 包 `@larksuite/cli`）
  - `lark-oapi`（飞书机器人 WebSocket 长连接 SDK）

### 2. 安装浏览器插件

**Chrome / Edge：**
1. 打开 `chrome://extensions/`
2. 开启右上角「开发者模式」
3. 点击「加载已解压的扩展程序」
4. 选择 `tools/clip-extension/` 目录

### 3. 使用

**快捷键**：`Ctrl+Shift+S`（Mac: `Cmd+Shift+S`）

**右键菜单**：
- 页面空白处 → 剪藏此页到知识库
- 链接上右键 → 剪藏链接到知识库（后台打开抓取）
- 选中文字 → 剪藏此页到知识库

**插件图标**：点击查看详情后剪藏

**API 方式**：
```bash
# 直接发送已提取好的内容
curl -X POST http://localhost:7878/api/clip \
  -H "Content-Type: application/json" \
  -d '{"title": "...", "url": "...", "content": "..."}'

# 发送 URL，服务端自动判断用什么方式抓
curl -X POST http://localhost:7878/api/clip-url \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/article"}'
```

## 飞书机器人（手机发链接入库）

`feishu-bot.py` 是一个独立进程，通过飞书开放平台的 **WebSocket 长连接**实时接收群消息 / 私聊，
把消息里的 URL 自动交给本地 `clip-server` 剪藏，再用飞书卡片回传结果。手机上给机器人发个链接，
知识库就多一条素材——**不需要开着电脑的浏览器**。

```bash
pip install lark-oapi
python tools/feishu-bot.py        # Windows: python tools\feishu-bot.py
```

群内 `@机器人 + 链接` 即入库（私聊直接发链接也行）。

> **完整配置指南**（开放平台建应用 → 权限 → 凭据三种方式 → 常驻 → 指令表 → 排错）
> 见 [`docs/feishu-bot-setup.md`](docs/feishu-bot-setup.md)。

两个容易混的点：

- **飞书消息 vs 飞书文档**：本节的机器人只用 `lark-oapi`，**不需要** `lark-cli`。
  若还要入库**飞书文档 / 知识库**链接，另需飞书官方 CLI（npm 工具，非 pip）：
  `npm install -g @larksuite/cli` 再 `lark-cli auth login` 扫码授权。
- **机器人也要过 clip-server 的鉴权**：它会自动读 `CLIP_SERVER_TOKEN` 或
  `tools/config.local.json` 的 token，请先把 clip-server 配好。

## 工作原理

### 智能分发

收到 URL 时，服务端自动匹配平台：

| 平台 | 抓取方式 | 支持度 |
|------|----------|--------|
| 飞书文档 | `lark-cli`（**唯一通道**；缺失或失败则直接报错，不降级） | S |
| YouTube | **浏览器扩展页面内抓**（后端适配器永不执行） | S |
| B 站 | **浏览器扩展页面内抓**（后端适配器永不执行） | S |
| 微信公众号 | 后端适配器，失败才降级浏览器协助队列（需登录态） | B |
| 小红书 | 后端适配器，失败才降级浏览器协助队列（需登录态） | B |
| 普通网页 | 后端 trafilatura，失败才降级浏览器协助队列 | A |
| 其他 / 后端抓不到 | 浏览器协助队列 | - |

> ⚠️ **别把这张表和 `platforms/registry.py` 的 `priority` 混为一谈**：注册表列了 6 个
> 适配器并按数字排序，但 `_prefer_browser()` 会把 YouTube / B 站按主机名直接送进浏览器
> 协助队列 —— 它们的后端适配器（`youtube.py` / `bilibili.py`）**当前永不执行**，仅作备用。
> 上表「抓取方式」一列描述的是**实际生效**的通道。

> **YouTube / B 站为何不经由后端抓取**（规则实现于 `clip-server.py` 的 `_prefer_browser()`，实测于 2026-10-07）：
> - YouTube 的字幕接口已加入 **PoToken 风控**：从网页取得的 `captionTracks[].baseUrl`
>   带有 `exp=xpe` 标记，直接请求只会返回 **HTTP 200 + 空 body**。扩展改为调用
>   `/youtubei/v1/player`（ANDROID / IOS 客户端身份），以取得不受风控约束的地址。
> - B 站的字幕同样需要在**带登录态的页面中**调用 WBI 接口才能取得。
> - 反之，后端的 yt-dlp 在这两个站点上**必然失败**，且代价明显：
>   ① Windows 下启动 `yt-dlp.EXE` 会**弹出控制台窗口**（clip-server 运行在 pythonw
>   下，本身没有控制台）；② YouTube 在命令行环境无法出网（浏览器有代理，命令行没有）
>   → 30 秒超时；③ B 站无法读取 Chrome cookie（App-Bound Encryption）→ 1.8 秒退出。
>
> 因此这两个域名的 URL 在服务端**直接进入浏览器协助队列**，不再额外等待并弹出窗口。
> `platforms/youtube.py` / `platforms/bilibili.py` 仍然保留，但已不在主链路上。

### 浏览器协助队列

遇到需要登录态的页面（知乎、小红书、公众号等），后端无法直接抓取时，即放入任务队列。
浏览器开着时，插件自动通过 SSE 连接服务端，有任务就开新标签页抓，抓完就关。

```
手机飞书发链接 → 后端收到
    ↓
后端能直接抓？ → 是 → 直接写入 inbox
    ↓ 否
放入浏览器协助队列
    ↓
浏览器插件 / Playwright worker 连上
    ↓
开新标签页 → 等加载 → 提取正文 → 提交结果 → 关标签页
    ↓
写入 inbox
```

### Wiki 自动精加工（三级回退）

raw 落盘后，后台自动尝试提炼 wiki 主题页：

| 级别 | 条件 | 方式 |
|------|------|------|
| Tier 1 | 安装了 Agent CLI | 依次尝试 `claude` → `codex`；agent 自主读 AGENTS.md、浏览 topics、读写文件 |
| Tier 2 | 配置了 API key | OpenAI 兼容接口直出（DeepSeek、豆包等均可），Python 负责组装上下文和读写文件 |
| Tier 3 | 都没有 | 跳过；raw 正常保存为孤儿（`wiki_refs: []`），日后手动提炼 |

Tier 2 配置：

```bash
cp tools/wiki-config.example.json tools/wiki-config.local.json   # 填入 api_key / base_url / model
# 或环境变量：WIKI_GEN_API_KEY / WIKI_GEN_BASE_URL / WIKI_GEN_MODEL
```

注意：Tier 2 是单次调用、上下文预置，交叉引用质量低于 Tier 1 的自主浏览；
两种方式都会遵守来源回链和 `## 我的思考` 人工保护规则。

### 图片文字 OCR（可选，下游）

有些小红书 / 微信笔记把正文写成图片，文章本身极短。这类内容**文字不在正文里**，
直接精加工只会产出一页垃圾 wiki。处理方式：

- **入库时**：剔除图片 markdown 后统计**真实文字**；若「文字极少 + 含图」，不生成
  wiki —— 有 OCR 通道则排入**下游** OCR 队列，否则如实跳过（计入 `wiki: skipped`）。
- **下游**：OCR 读同名图片目录 → 把识别文字补进笔记 `## 图片文字（OCR）` 一节
  → 重跑 wiki。与 `convert_to_webp.py` 同层，**不在入库链路做**（守「采集只做
  字节保真、不在入库链路解码图片」）。

两个后端（配置 `tools/ocr-config.local.json`，缺省 `engine=auto`）：

| 后端 | 说明 | 代价 |
|------|------|------|
| `multimodal`（auto 时优先） | 复用 wiki 的 Tier 2 配置，换 **vision 模型** 读图 | 图片外发 |
| `local` | 填任意 OCR 命令模板，如 `tesseract {input} stdout -l chi_sim+eng` | 需本地装工具 |

```bash
cp tools/ocr-config.example.json tools/ocr-config.local.json   # 可选；不配 = auto
# 环境变量：OCR_ENGINE / OCR_MODEL / OCR_LOCAL_COMMAND

# 手动补跑（默认 dry-run）：列出「图多字少」候选；--apply 才 OCR 并重跑 wiki
python tools/ocr_images.py
python tools/ocr_images.py --apply --file 2026-10-10-xxx.md
```

注意：`engine=auto`（缺省）下，多模态**必须显式填 vision 模型**（`model`）才启用——
wiki 的 Tier 2 model 常是纯文本模型，拿它读图只会白跑一次失败调用。填了 `model`
（多模态）或可用的 `local_command`（本地），「图多字少」才会真正走 OCR；都没配则
如实标 `wiki: skipped`。

> **判据只有一份实现**：`content_measure.py`。入库门禁（`clip-server.py`）、去重时的
> 新旧版比较、以及本命令的候选筛选（`ocr_images.py`）都从它取口径与阈值。历史上这里
> 各写一份、口径漂移，导致 `ocr_images.py` 列不出 clip-server 判为「图多字少」的笔记
> —— 下游补救这一环静默失效。新增/修改判据请只改 `content_measure.py`。

几点补充：

- **默认自动提取，可主动叫停**：命中「图多字少」即自动排队 OCR —— 用户什么都不做
  就会跑，所以「忘了选」的后果只是多花一次调用，**不会留下没 wiki 的笔记**。
  想省这一次（多模态有费用且图片外发）时，在**剪藏那一刻**表态：
  - 插件面板取消勾选「提取图片文字（OCR）」（默认勾选，选择会记住）；
  - 飞书消息加前缀 **`不OCR`**（或 `noocr`），如 `不OCR https://…`，可与 `跟踪` 组合。
  两处都只把本次记为 `wiki: skipped`（原因写明「本次选择跳过图片文字提取」），
  之后仍可用 `--apply` 补跑 —— 跳过是终态，不会停在「后台生成中」。
  > 「叫停」为什么不做成卡片按钮：OCR 在 raw 落盘后**即刻**排队，用户看到卡片时
  > 它往往已经在跑了，那时再点只能白花成本。
- **回填后会自动刷新字数**（frontmatter 的 `word_count` 与头部的「字数：约 N 字」）——
  否则会出现「正文已补进 1671 字，头部还写着约 232 字」。口径同 `content_measure.count_words`。
- **异步排队路径会补发结论通知**：界面先回「⏳ 正在提取图片文字（OCR）」，OCR 与 wiki
  在后台跑完后，服务端把最终结论**回写任务记录**（`_settle_task_async` 清掉
  `asyncPending`），飞书卡片随即**补发**一条最终结果（已完成 / wiki 未生成 /
  已退回待审区）。任务面板与扩展弹窗读的是同一条记录，所以三处口径一致。
  手动补跑（`--apply`）是同步的，命令行直接给出结论。
- 配置读取是**每次调用时读文件**，所以新增/修改 `ocr-config.local.json` 后
  **不需要重启 clip-server**。

## Playwright Worker

如果你不想一直开着浏览器，或者想让服务器上也能"用浏览器抓"，可以用 Playwright worker。

### 安装

```bash
pip install playwright httpx     # Linux 如受 PEP 668 限制再加 --break-system-packages
playwright install chromium
```

### 使用

```bash
# 用你自己的 Chrome profile（带登录态）
python3 tools/playwright-worker.py --profile ~/.config/google-chrome

# Windows 上 Chrome profile 路径：
python tools/playwright-worker.py --profile "%LOCALAPPDATA%\Google\Chrome\User Data"

# 干净浏览器（测试用）
python3 tools/playwright-worker.py

# 自定义服务地址
python3 tools/playwright-worker.py --server http://localhost:7878
```

Worker 启动后会自动连接服务端，有任务就抓，没任务就等。

**与浏览器扩展的区别：**

- 扩展：在使用电脑的过程中顺带完成抓取，无额外操作
- Playwright worker：可在服务器后台长期运行，不依赖浏览器处于开启状态
- 两者可同时运行，共同领取队列中的任务

> ⚠️ 「不依赖浏览器」有一个前提：**所抓取的内容不需要登录态**。
> Windows 上无法取得您的登录态 —— Chrome ≥ 127 的 App-Bound Encryption 将 cookie
> 解密密钥绑定在浏览器二进制上，外部进程无法解密；`--profile` 指向真实 Chrome
> profile 时还会被运行中的 Chrome 独占（`SingletonLock` / `TargetClosedError`），
> 改用独立 profile 则等同于没有登录态。因此需要登录的站点在 Windows 上**必须使用
> 浏览器扩展**（页面内抓取，登录态天然继承）。
> 细节见 [`docs/windows-vs-linux-browser-profile.md`](docs/windows-vs-linux-browser-profile.md)。

## 安全模型

- **鉴权**：复制 `config.example.json` 为 `config.local.json` 并填入随机 token，所有
  API（除 `/api/health`）都需要 `Authorization: Bearer <token>`；也可用环境变量
  `CLIP_SERVER_TOKEN`（优先级更高，适合不落盘的部署）。
- **网络绑定**：默认只监听 `127.0.0.1`。若用 `--host` 绑定非回环地址（如 `0.0.0.0`）
  而未配置 token，服务会拒绝启动。
- **CORS 白名单**：默认只允许浏览器扩展 origin（`chrome-extension://*` 等）和本机页面
  （`localhost` / `127.0.0.1`）跨域访问，普通公网网页无法向本地服务发起请求；
  可在 `config.local.json` 的 `allowed_origins` 中追加白名单。
- **SSE 鉴权**：浏览器 EventSource 无法自定义请求头，扩展和 worker 通过
  `/api/browser/connect?token=<token>` 传参；playwright-worker 用 `--token` 或
  `CLIP_SERVER_TOKEN`，其结果回传统一走 Bearer 头。
- **SSRF 防护**：图片下载仅允许 http(s)，解析后的目标 IP 不得是环回/内网/链路本地/
  保留地址（拦截 `127.0.0.1`、`169.254.169.254` 云元数据等），重定向逐跳校验，
  单图上限 10MB，拒绝 SVG。
- **路径安全**：inbox 处理接口只接受单层 `.md` 文件名，拒绝 `../`、绝对路径和隐藏文件。

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/health` | 健康检查 + 平台列表 + 待处理任务数 |
| POST | `/api/clip` | 提交已提取的内容（浏览器插件用） |
| POST | `/api/clip-url` | 提交 URL，自动分发 |
| GET | `/api/task/{id}` | 查询任务状态 |
| GET | `/api/browser/connect` | SSE：浏览器/worker 连接拉取任务 |
| POST | `/api/browser/result` | 提交任务抓取结果 |

## 离线测试

解析 / 路由这类纯逻辑有离线测试，不依赖浏览器和网络：

```bash
node tools/tests/test_youtube_adapter.js
node tools/tests/test_routing2.js
```

见 [`tests/README.md`](tests/README.md)（含「为什么必须写这些」——项目踩过
「测试全绿但真机炸」的坑，根因都是测试环境不等于真实运行环境）。

## 后续可扩展

- 图片下载 + WebP 压缩 + OCR
- Whisper 语音转写（视频没有字幕时）
- 更多平台适配器（知乎等）

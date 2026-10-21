# 为什么 Windows 无法直接复用浏览器 Profile 剪藏视频，而 Linux 正常

> 本文汇总 2026-10-04 ~ 10-05 一轮排查的结论。
> 测试机：Windows + Chrome 154 + yt-dlp 2026.08.19；Linux 侧为用户的日常环境。
> 文中区分**实测**（本机跑过、有输出）与**推断**（据机制或文档推导），请按标注取信。

---

## 0. 一句话结论

| | Linux | Windows |
|---|---|---|
| Chrome cookie 在磁盘上的形态 | **明文 SQLite** | **App-Bound Encryption 加密**（Chrome ≥ 127） |
| 外部进程（yt-dlp）能否读出登录态 | ✅ 能 | ❌ 不能 |
| 「复用用户 profile 剪藏」是否可行 | 可行 | **不可行**（是浏览器安全设计，不是配置问题） |

核心就一句：**Windows 上 Chrome 把 cookie 的解密密钥绑定在了浏览器自己身上，外部程序拿不到；Linux 上 cookie 根本就是明文。**

但这句话只解释了一半，剩下的一半是本文第 1、2 节——**「用 profile 剪藏」其实是两条完全不同的路径**，混在一起谈就会得出错误结论。

---

## 1. 前提：项目里有两条剪藏路径，机制完全不同

这是理解全部现象的钥匙。

| | **插件剪藏**（`tools/clip-extension/`） | **服务端抓取**（`platforms/bilibili.py` → yt-dlp） |
|---|---|---|
| 在哪执行 | **用户浏览器进程内部** | 服务端（Python）进程 |
| 怎么拿内容 | content script 在已登录页面里抽取 DOM | 自己发 HTTP 请求重新抓 |
| 登录态来源 | **天然继承**（就长在已登录的标签页里） | 必须自己持有 cookie |
| 浏览器要关吗 | 不需要 | 需要（或改用 CDP） |
| 服务端要不要重抓 | ❌ 不用，收到的是**结果** | ✅ 要，收到的是**任务** |

证据（`tools/clip-extension/popup.js`）：

```js
const response = await chrome.tabs.sendMessage(tab.id, { action: "extract" });  // :256
const result   = await sendToServer(response.data);                            // :285
```

**插件从不把 URL 交给服务端去重抓，它把抽好的内容整包 POST 给 `localhost:7878`。** 服务端压根没发那个 bilibili 请求，自然不需要 cookie，也就碰不到你的 profile——所以 Chrome 开着关着都无所谓。

而服务端抓取路径恰恰相反：它得自己去请求那个 URL，没有你的登录态就拿不到字幕，于是才需要「借」你的 profile——**这才撞上了 Windows 的墙。**

> 所以「为什么 Linux 能用 profile 剪藏」这个问题，第一步要先问：你当时走的是哪条路？

---

## 2. Linux 为什么「运作正常」

两个独立原因，可能同时成立。

### 2.1 Linux 的 Chrome cookie 是明文（机制层）

| 平台 | Chrome cookie 存储 | yt-dlp 可读 |
|---|---|---|
| **Linux** | 明文 SQLite（`Cookies` 表） | ✅ 直接读值 |
| macOS | keychain 包装，yt-dlp 走系统接口 | ✅ |
| **Windows（Chrome ≥ 127）** | App-Bound Encryption | ❌ |

`yt-dlp --cookies-from-browser chrome` 在 Linux 上只是「打开一个 SQLite 文件、把 `SESSDATA` 读出来」，没有任何解密环节 → 天然可用。
Windows 上同一个命令要先解密 → 撞墙。（实测见第 3.1、4 节）

### 2.2 你实际走的很可能是插件路径（行为层）

你描述的场景是「**浏览器开着**的情况下，通过飞书/插件剪藏登录态的 bilibili 视频」。

- 如果是**插件**：这正是它的固有性质，与 profile 无关，见第 1 节。
- 如果是**服务端抓取**：Linux 能成，靠的是第 2.1 节的明文 cookie。

两种情况都不需要「让外部进程去用你的 profile」，所以都没有撞到 Windows 那堵墙。

---

## 3. Windows 为什么不行：三层原因（按决定性排序）

### 3.1 【决定性】App-Bound Encryption —— Chrome ≥ 127 引入

Chrome 把加密 cookie 的 AES 主密钥，用「**绑定到浏览器自身二进制**」的方式保护起来，放在 profile 的 `Local State` 里。

实测 `Local State` → `os_crypt` 顶层键：

```
['app_bound_encrypted_key', 'audit_enabled', 'encrypted_key']

app_bound_encrypted_key = "QVBQQgEAAADQjJ3fARXREYx6AMB..."
                          └──────┬──────┘
                     base64 解出 'APPB' + 版本号 1
                     A = App  B = Bound
```

外部进程调用 Windows 的 `CryptUnprotectData` 去解这把钥匙，只会拿到 `ERROR_INVALID_DATA`（数据无效）。实测试过三种 entropy 参数（`None` / `b'Chrome'` / `b'chrome'`），**全部失败**。

yt-dlp 把它包装成两种看起来很不一样、其实同源的报错：

```
ERROR: Could not copy Chrome cookie database.
ERROR: Failed to decrypt with DPAPI.
```

> 需要强调：**这是 Chrome 的主动安全设计，不是 bug，也不是本项目的缺陷。**
> 它的目的正是「防止其他程序偷走你的登录态」。
> 任何声称能从外部直接解开的做法，本质是在跟浏览器的安全更新赛跑，不适合放进开源项目。

### 3.2 Profile 独占锁（跨平台，但最常被误认为 Windows 特有）

浏览器运行时会**独占**自己的 cookie 库。实测（Chrome 开着，15 个进程）：

```
shutil.copy2(Cookies)            → PermissionError [WinError 32] 另一个程序正在使用此文件
sqlite3.connect(..., immutable=1) → unable to open database file
```

用真实 profile 起 playwright：

```
TargetClosedError
BrowserType.launch_persistent_context: Target page, context or browser has been closed
```

**注意：这不是 Windows 独有的**——Linux 同样锁（SQLite WAL / fcntl），macOS 也锁。
它只是「还不能用」的原因之一；真正让 Windows 无解的是 3.1。

之所以要单独列出来，是因为**两种失败长得太像**，很容易把「锁」误判成「加密」：
- 浏览器**开着** → 报「copy 不了」（锁）
- 浏览器**关掉** → 报「解不开」（加密）

于是「我关了浏览器还是不行」和「我开着也能用（插件）」这两句话会把人绕晕。

### 3.3 Chrome 拒绝在**默认数据目录**上开调试端口

想用 CDP 接管用户已开着的浏览器时，需要 `--remote-debugging-port=9222`。
但实测（Chrome 154）不带 `--user-data-dir` 时，Chrome 直接把 stderr 甩给你：

```
DevTools remote debugging requires a non-default data directory.
Specify this using --user-data-dir.
```

现象很有迷惑性：**浏览器 9 个进程正常起来，但 9222 根本不监听**（`netstat` 无记录）。加显式 `--user-data-dir=<默认目录>` 照样拒绝。

换成非默认目录后端口正常监听，但**登录态搬不过去**——理由见 3.1：cookie 值是绑定的加密数据，光拷文件没用。

---

## 4. 实测证据汇总

| # | 场景 | 结果 | 说明 |
|---|---|---|---|
| 1 | `yt-dlp --cookies-from-browser chrome`（Windows） | ❌ `Could not copy Chrome cookie database` | 3.2 锁 + 3.1 加密 |
| 2 | `yt-dlp --cookies <cookies.txt>` | ✅ 返回完整元数据 JSON | 明文文件绕开整条加密链 |
| 3 | `yt-dlp` 不带任何 cookie（匿名） | ✅ 返回完整元数据 JSON | 元数据**不需要**登录态 |
| 4 | `yt-dlp` 匿名下载 AI 字幕 | ❌ `Subtitles are only available when logged in` | 字幕**需要**登录态 |
| 5 | 进程内 `win32crypt.CryptUnprotectData`（3 种 entropy） | ❌ 全部「数据无效」 | 证明 3.1 是设计层阻断 |
| 6 | `Local State` → `os_crypt` 键 | 含 `app_bound_encrypted_key` | App-Bound 的直接物证 |
| 7 | Chrome 运行时复制 `Cookies` 文件 | ❌ `WinError 32` | 3.2 独占锁 |
| 8 | 真实 profile 起 `launch_persistent_context` | ❌ `TargetClosedError` | 同上 |
| 9 | `chrome.exe --remote-debugging-port=9222`（默认目录） | ❌ 端口不监听 + stderr 硬拒绝 | 3.3 |
| 10 | 只拷 `Default/Network/Cookies` 到新 profile | ❌ `SESSDATA`/`bili_jct`/`DedeUserID` 全丢 | 加密绑定，拷了没用 |
| 11 | 再补拷 `Local State`（密钥所在） | ❌ 依旧全丢 | 同上 |
| 12 | CDP `connect_over_cdp` 后的 cookie jar | ✅ 与浏览器**双向共享** | CDP 是「接上」而非「拷贝」，另当别论 |

---

## 5. 三个被实测否掉的「想当然」

### 5.1 「把 Cookies 文件拷到新 profile 就有登录态了」

| 尝试 | 结果 |
|---|---|
| 只拷 `Default/Network/Cookies` | ❌ 登录 cookie 全丢，只剩本次新生成的（buvid3 之类） |
| 再补拷 `Local State`（密钥） | ❌ 依旧丢 |

**结论：登录态没法"搬家"。** 唯一干净的路径是建一个专用 profile、在里面登录一次——登录态由浏览器原生写入，不存在解密问题。

### 5.2 「cookie 拿不到只是少了个字幕，无伤大雅」

实测确认为：元数据匿名可取 ✅、字幕必须登录 ❌。
**但对 B 站 / YouTube 这类**视频**源，字幕就是正文。**
只剩标题 + UP主 + 简介，那不是剪藏，是个书签。

> 我在本轮对话里曾据此把「cookie 拿不到」设计成"优雅降级"，这是**判断错误**——对视频链路，cookie 是必需项，不是增强项。此设计已按用户要求回退。

### 5.3 「yt-dlp 报的是 DPAPI，那就是 DPAPI 的问题」

不准确。开着浏览器时是「**copy 不了**」（文件锁），关掉浏览器时才是「**解不开**」（加密）。
两种现象被 yt-dlp 包装成相似措辞，但它们归属第 3.2 与 3.1 两个不同成因。

---

## 6. Windows 上的可行方案与代价

| 方案 | 一次性还是每次 | 要关浏览器吗 | 扩展/书签栏 | 适用场景 |
|---|---|---|---|---|
| **浏览器插件剪藏**（推荐） | 零配置 | ❌ 不用 | ✅ 全在 | 日常剪藏（含登录态页面） |
| 装 Firefox，让项目自动用它 | 登录一次 | ❌ 不用 | — | 有 Firefox 习惯的用户 |
| 专用 Chrome profile + CDP | 登录一次 | 需另起一个实例 | 仅该实例内 | 服务端抓取、后台批量 |
| 导出 `cookies.txt` + `YTDLP_COOKIES_FILE` | 导出一次（SESSDATA 有效期数月） | 导出时需退出浏览器 | — | 飞书机器人等**服务端主动抓取**场景 |
| 无头服务器 | 同 cookies.txt | — | — | 部署在服务器上 |

**推荐顺序：插件 → Firefox → 专用 profile/CDP → cookies.txt。**
前两个几乎零成本；后两个只在「扩展帮不上忙」时才需要（服务端主动抓取、无头批量、换机器）。

---

## 7. 本对话产生过、随后被回退的改动

为保持记录完整，如实列出（**均已回退，仓库里现在没有这些文件**）：

- 新增后删除：`tools/platforms/_cookies.py`、`tools/export-browser-cookies.py`、
  `reviews/ADR-012-cookie-resolution-cross-platform.md`、`.github/`
- 改后还原：`tools/playwright-worker.py`（CDP 分支）、`tools/README.md`（CDP 文档）、
  `tools/platforms/bilibili.py`（cookie 解析改造）、`README.md`、`.gitignore`

**唯一保留**：`requirements.txt` 与 `dist/knowledge-vault/requirements.txt` 首行的
`# coding: utf-8`（见第 8 节，与本主题无关的独立 bug）。

备份位置（含全部被回退的文件与一个测试剪藏）：`.workbuddy/backup/revert-20261005-1656/`

---

## 8. 附：本对话另一条独立线索 —— pip 在中文 Windows 下解析 requirements 失败

与 cookie 无关，但同样是 Windows/Linux 差异，一并记录。

**现象**：中文环境 PowerShell 里跑 `pip install -r requirements.txt` 报

```
UnicodeDecodeError: 'gbk' codec can't decode byte 0xa3 in position 31: illegal multibyte sequence
```

**根因**：pip 24.2 的 `auto_decode()`（`pip/_internal/utils/encoding.py`）**不先试 UTF-8**：

```python
for bom, encoding in BOMS: ...                 # 1. 看 BOM
for line in data.split(b"\n")[:2]: ...         # 2. 看前两行有没有 PEP 263 声明
return data.decode(locale.getpreferredencoding(False))   # 3. 直接按系统 locale 解
```

`requirements.txt` 的中文注释是 UTF-8，但既无 BOM、前两行也无 `coding:` 声明 → 落到第 3 步，用系统 locale 解码。中文 Windows 控制台是 **cp936**，UTF-8 中文在第 31 字节炸掉——正好是报错里的 `position 31`。

**为什么有时「看起来能跑」**：Git Bash 会把 locale 设成 UTF-8，第 3 步就蒙对了。这也是它藏得久的原因。

**修法**（已在首行加上，保留至今）：

```
# coding: utf-8
```

命中第 2 步，pip 按 UTF-8 解整个文件，中文注释全部保留。
备选：`chcp 65001`、`set PYTHONUTF8=1`，或把注释改成纯 ASCII。

---

## 附录：可复现的验证命令

```bash
# ① 你的 locale 是不是 cp936（中文 Windows）
python -c "import locale; print(locale.getpreferredencoding(False))"

# ② yt-dlp 直接读浏览器 cookie —— Windows 上会看到 DPAPI/copy 报错
yt-dlp --cookies-from-browser chrome -J --skip-download "<视频URL>"

# ③ 匿名也能拿元数据（证明元数据不需要登录）
yt-dlp -J --skip-download "<视频URL>"

# ④ 字幕需要登录（证明 cookie 对视频正文是必需）
yt-dlp --write-subs --sub-lang ai-zh --skip-download -o "/tmp/sub.%(ext)s" "<视频URL>"

# ⑤ 看 Chrome 是否启用了 App-Bound Encryption（Windows）
python -c "import json,os;d=json.load(open(os.path.expandvars(r'%LOCALAPPDATA%\Google\Chrome\User Data\Local State'),encoding='utf-8'));print(list(d.get('os_crypt',{}).keys()))"
# 出现 app_bound_encrypted_key 即为此机制生效

# ⑥ 尝试给默认 profile 开调试端口 —— 会被 Chrome 硬拒绝
chrome.exe --remote-debugging-port=9222 2>&1 | head -3
```

---

## 一句话回顾

**Linux 能成，是因为那边的 cookie 是明文；Windows 不成，是因为 Chrome ≥127 把 cookie 密钥绑在了自己身上（App-Bound Encryption），外部无法解密——这是浏览器的安全设计，不是任何配置能绕过的。**
**而在「浏览器开着也能剪登录视频」这件事上，真正起作用的往往是浏览器插件路径（登录态天然继承），跟 profile 能不能被外部读取无关。**

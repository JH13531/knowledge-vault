# 工具链离线测试

不依赖浏览器、不依赖网络、不需要凭据，直接 `node` 跑。用于验证**纯逻辑**部分
（解析、路由、协议），这些是最容易在改代码时悄悄改坏、又最难在真机上看出来的地方。

```bash
node tools/tests/test_bilibili_adapter.js    # B 站页面内字幕抓取
node tools/tests/test_youtube_adapter.js     # YouTube 页面内字幕抓取
node tools/tests/test_routing2.js            # 站点分类 + 标签页复用 + 子进程/零宽字符断言
node tools/tests/test_crossframe.js          # 跨 frame 正文提取（需要 jsdom，见下）
```

`test_crossframe.js` 是唯一需要外部依赖的测试（`jsdom`）——它要真实执行
`platforms/*.js` 的提取链，手写 `document` 桩撑不起 `querySelector`/`innerHTML`
这套 DOM API。安装与运行：

```bash
npm i jsdom    # 或用隔离环境：
# NODE_PATH=<node_modules 路径> node tools/tests/test_crossframe.js
```

| 脚本 | 覆盖什么 |
|---|---|
| `test_bilibili_adapter.js` | **协议相对地址补全**（`//aisubtitle.hdslb.com/...` 不补成 https，background 的白名单校验会直接抛异常并把字幕永远挡掉）、失败原因必须写进 `extra.captionNote`（无字幕 / 未登录 / 拿不到 cid / WBI 接口报错，且带上真实错误）、成功路径的正文与 `extra` 形状、字幕 JSON → `[HH:MM:SS]`（跨小时、跳过空行）、AI 中文字幕标识 `aizh` 的优先级 |
| `test_youtube_adapter.js` | 从内联 `<script>` 取 `ytInitialPlayerResponse`（**深嵌套 JSON 用括号配对，不能用非贪婪正则**）、**PO Token 风控识别**（`exp=xpe`/`potc=1`）、**改走 InnerTube `/youtubei/v1/player`（ANDROID/IOS）取无风控的字幕地址**、挑轨优先 zh→en→其它、json3 → `[HH:MM:SS]`（含跨小时、相邻重复行去重）、XML 兜底（srv3 `<p t d>` / srv1 `<text start>` / 双重转义实体）、拿不到字幕 / HTTP 200 空 body / HTTP 403 时的降级与 note |
| `test_routing2.js` | `known-sites.js` 的站点分类、`_tabUrlKey` 的等价性（`watch?v=` 与 `youtu.be` 同一页 / 带时间戳参数同一页 / 不同视频不同页）、background 里保活与标签页清理的静态断言、**每个 py 文件 `subprocess.run(` 的次数必须等于 `creationflags=NO_WINDOW` 的次数**（防「新增子进程忘了不弹窗」复发）、**视频平台必须跳过后端 yt-dlp** 的静态断言、**`cleanZeroWidth` 不许删普通连字符**（曾经把 `state-of-the-art` 清成 `stateoftheart`）、**飞书轮询超时不得静默吞掉**（`MAX_POLL_TIME` 必须长于服务端清扫阈值，且未收到结论时也要发卡片）、**浏览器协助结果必须崩溃安全回传**（持久化发件箱 + 闹钟补发，防 MV3 SW 回收吞掉 fetch）、**待审区失败必须写回原因到卡片**、**前端必须解释「卡在哪一步」**、**浏览器重连要重发无回传的任务**、**进行中的任务必须落盘并能续跑**（根因：MV3 里 `setInterval` 无法保活 SW，抓取中途被回收会把 `submitResult` 一起带走）、**服务端 / 飞书 / 前端三处时间阈值必须彼此相容**（前端轮询 ≥ 服务端卡死 ≥ 重发）、**风控拦截页不得被当作正文入库**（Google 验证码页 `/sorry/` 有几百字，长度检查拦不住）、**待审指针与待审卡片的 URL 必须认「任务原始 URL」**（抓取被重定向到验证码页时，用拦截页地址比对/建卡都会出错）、**飞书「浏览器协助」重复卡片必须有强制覆盖按钮**（服务端要回传完整字段 + 飞书轮询要渲染按钮，缺一条就没有按钮）、**降级回待审区时必须刷新卡片 note**（`_inbox_create_card` 遇已存在卡片只返回 duplicate 且不改 note，卡片会永远留着「等待浏览器抓取」）、**「全部处理」必须等各任务出结论再报真实结局**（只派发不轮询 → 永远显示「结果没有回传」，即便任务早已入库/降级）、**选中文本必须跨 frame 汇总**（顶层 `getSelection()` 看不到跨域 iframe 的选区）、**整页提取也必须跨 frame**（`sendMessage(tab.id)` 只到顶层；商户后台套壳页顶层仅 99 字、iframe 有 3170 字）、**任务面板必须统计全部渠道**（`/api/clip` 直接推送过去不写任务记录）、**弹窗请求必须有超时**（无超时的 fetch 会让 loading 永不过期） |
| `test_crossframe.js` | **跨 frame 正文提取的真实行为**：用 jsdom 加载 `platforms/*.js`，分别对「套壳顶层」与「iframe 正文」建 DOM 跑 `extractContent()`，断言 iframe 提取到完整正文、套壳页只有寥寥数字、选优规则挑中 iframe，且**普通页面（正文就在顶层）行为不变** |

## 为什么要写这些

这个项目里踩过好几次「测试全绿但真机炸」的坑，都是因为**测试环境不等于真实运行环境**：

- 用 `add_script_tag` 把插件脚本注入页面**主世界**测 — 那里 `window.__INITIAL_STATE__` 读得到，
  测试全过；可真机上 content script 跑在**隔离世界**，读到的永远是 `undefined`
- 用 CDP 建隔离世界测跨域 fetch — 那个世界**没有扩展的网络策略**，测了等于没测

所以现在的分工是：**纯逻辑用离线测试锁住**（快、可复现），
**只有环境相关的部分才上真机**（用户实测）。

## 约定

- 不写死绝对路径（用 `path.join(__dirname, "..")`），换台机器也能跑
- 用最小的假环境（`vm.createContext` + 手写 `document`/`location`/`fetch` 桩），
  不引入 jsdom 之类的重依赖
  - **例外**：`test_crossframe.js` 要真实跑 DOM 提取链，手写桩撑不起
    `querySelector`/`innerHTML`/`textContent`，故引入 `jsdom`（仅此一个脚本）
- 断言失败要打印期望值与实际值，别只打一句 fail

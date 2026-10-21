// 验证：① known-sites 的站点分类（YouTube 现在应有页面内适配器）
//       ② background.js 的 _tabUrlKey（决定「要不要另开标签页」）
const fs = require("fs");
const vm = require("vm");

const path = require("path");
const EXT = path.join(__dirname, "..", "clip-extension");
let ok = 0, fail = 0;
function check(name, got, want) {
  const good = JSON.stringify(got) === JSON.stringify(want);
  if (good) ok++; else fail++;
  console.log((good ? "  PASS " : "  FAIL ") + name);
  if (!good) { console.log("         got : " + JSON.stringify(got)); console.log("         want: " + JSON.stringify(want)); }
}

console.log("=== ① known-sites 分类 ===");
const sbox = { window: {}, console };
sbox.globalThis = sbox;
vm.createContext(sbox);
vm.runInContext(fs.readFileSync(EXT + "/platforms/known-sites.js", "utf8"), sbox);
const V = sbox.window.__clipVault;

check("YouTube 属于「有页面内适配器」", V.hasLocalAdapter("https://www.youtube.com/watch?v=abc"), true);
check("youtu.be 同属", V.hasLocalAdapter("https://youtu.be/abc"), true);
check("YouTube 不再被判为「仅服务端」", V.needsServerAdapter("https://www.youtube.com/watch?v=abc"), false);
check("SERVER_ONLY 列表已空", V.SERVER_ONLY_ADAPTER_DOMAINS, []);
check("B 站仍在本地列表", V.hasLocalAdapter("https://www.bilibili.com/video/BV1x"), true);
check("飞书/小红书/公众号仍在", [
  V.hasLocalAdapter("https://foo.feishu.cn/wiki/x"),
  V.hasLocalAdapter("https://www.xiaohongshu.com/explore/x"),
  V.hasLocalAdapter("https://mp.weixin.qq.com/s/x"),
], [true, true, true]);
check("普通网页两者都不命中", [
  V.isKnownPlatform("https://example.com/a"),
  V.hasLocalAdapter("https://example.com/a"),
  V.needsServerAdapter("https://example.com/a"),
], [false, false, false]);
check("知乎/抖音仍是「已知平台但无专属适配器」", [
  V.isKnownPlatform("https://www.zhihu.com/question/1"),
  V.hasLocalAdapter("https://www.zhihu.com/question/1"),
], [true, false]);

console.log();
console.log("=== ② background.js 的 _tabUrlKey ===");
const bg = fs.readFileSync(EXT + "/background.js", "utf8");
const fnText = bg.match(/function _tabUrlKey\(raw\) \{[\s\S]*?\n\}/)[0];
const _tabUrlKey = new Function("URL", "return " + fnText.replace(/^function _tabUrlKey/, "function"))(URL);

const k = (u) => _tabUrlKey(u);
check("watch?v= 与带时间戳参数视为同一页",
  k("https://www.youtube.com/watch?v=abc&t=30s") === k("https://www.youtube.com/watch?v=abc"), true);
check("www 前缀不影响", k("https://www.youtube.com/watch?v=abc") === k("https://youtube.com/watch?v=abc"), true);
check("youtu.be 短链与 watch 页视为同一页",
  k("https://youtu.be/abc") === k("https://www.youtube.com/watch?v=abc"), true);
check("不同视频不同 key",
  k("https://www.youtube.com/watch?v=abc") === k("https://www.youtube.com/watch?v=xyz"), false);
check("普通网页忽略 hash 之外的 query？——保留 query（不同 query 视为不同页）",
  k("https://example.com/p?a=1") === k("https://example.com/p?a=2"), false);
check("普通网页忽略结尾斜杠", k("https://example.com/p/") === k("https://example.com/p"), true);
check("非法 URL 不抛异常", typeof k("not a url") === "string", true);

console.log();
console.log("=== ③ background.js 是否已带 SW 保活与标签页清理 ===");
check("有 startSwKeepalive", /function startSwKeepalive/.test(bg), true);
check("processBrowserTask 里启动了保活", /const keepalive = startSwKeepalive\(\)/.test(bg), true);
check("在 finally 里停止保活", /stopSwKeepalive\(keepalive\)/.test(bg), true);
check("只清理自己开的标签页（createdTab）", /if \(createdTab && tab\)/.test(bg), true);
check("先尝试复用已有标签页", /const existing = await findTabForUrl\(url\)/.test(bg), true);
check("submitError 也会检查响应并按终态处理",
  /上报被服务端拒绝 \[/.test(bg), true);

console.log();
console.log("=== ④ 子进程一律不许弹控制台窗口 ===");
// 这个坑犯过不止一次（守护每 5 秒闪黑框、点「处理」弹出 cmd），复发点是
// **新写一个 subprocess.run 时忘了带 creationflags**。所以直接按出现次数对账：
// 文件里出现几次 subprocess.run( ，就必须出现几次 creationflags=NO_WINDOW。
const TOOLS = path.join(__dirname, "..");
const pyFiles = [
  "wiki_gen.py",
  "platforms/bilibili.py",
  "platforms/youtube.py",
  "platforms/feishu.py",
];
for (const rel of pyFiles) {
  const code = fs.readFileSync(path.join(TOOLS, rel), "utf8");
  const runs = (code.match(/subprocess\.run\(/g) || []).length;
  const flags = (code.match(/creationflags\s*=\s*_?NO_WINDOW/g) || []).length;
  check(`${rel}：subprocess.run ${runs} 处，带 creationflags 的 ${flags} 处`, flags, runs);
}

const nw = fs.readFileSync(path.join(TOOLS, "no_window.py"), "utf8");
check("no_window.py 用 CREATE_NO_WINDOW", /getattr\(subprocess,\s*"CREATE_NO_WINDOW"/.test(nw), true);
check("  → 非 Windows 退化为 0（否则 Unix 上会 TypeError）", /if os\.name == "nt" else 0/.test(nw), true);

const auto = fs.readFileSync(path.join(TOOLS, "autostart", "vault-autostart.py"), "utf8");
check("守护脚本仍带 _NO_WINDOW", /_NO_WINDOW/.test(auto), true);

console.log();
console.log("=== ⑤ 视频平台不得在后端跑 yt-dlp（会弹窗 + 必然超时）===");
const cs = fs.readFileSync(path.join(TOOLS, "clip-server.py"), "utf8");
// 实测（2026-10-07）：后端跑 yt-dlp 抓 YouTube/B站 = 弹一个 cmd 窗口 +
// 30 秒超时 / 1.8 秒 cookie 失败，然后才降级 —— 而浏览器协助本来就是唯一可行通道。
check("_smart_clip 跳过视频平台的后端直抓",
  /if platform is not None and not _prefer_browser\(url\):/.test(cs), true);
check("待审区「▶ 处理」对视频平台改走浏览器协助",
  /if _prefer_browser\(url\):\s*\n\s*active = task_queue\.find_active_task/.test(cs), true);
check("_prefer_browser 按主机名判定（不看子串）",
  /urlparse\(url\)\.netloc/.test(cs.slice(cs.indexOf("def _prefer_browser"), cs.indexOf("def _prefer_browser") + 600)), true);
check("浏览器协助回传成功后也会摘掉待审指针",
  /_settle_inbox_pointer\(result_data, wiki, origin_url=_origin\)/.test(cs), true);

console.log();
console.log("=== ⑥ cleanZeroWidth 不许伤到正常字符 ===");
// 这个函数曾经写成 /[-‏‪-⁯﻿᠎͏؜￹-￻]/g —— 字符类**第一个字符是普通连字符**，
// 于是剪藏时把所有 `-` 都删了（`state-of-the-art` → `stateoftheart`，URL 一起坏掉）。
// 必须用 \\u 转义写清楚，不能靠字面量字符区间。
const bsbox = { window: {}, console };
bsbox.globalThis = bsbox;
vm.createContext(bsbox);
vm.runInContext(fs.readFileSync(EXT + "/platforms/base.js", "utf8"), bsbox);
const clean = bsbox.window.__clipVault.base.cleanZeroWidth;

check("普通连字符保留（曾经的 bug）", clean("state-of-the-art"), "state-of-the-art");
check("URL 里的连字符保留", clean("web_related_0.router-related-2589621"), "web_related_0.router-related-2589621");
check("日期/编号不受影响", clean("2026-10-07-001"), "2026-10-07-001");
check("下划线、点、斜杠不受影响", clean("a_b.c/d"), "a_b.c/d");
check("零宽空格 U+200B 被清掉", clean("知\u200b识"), "知识");
check("零宽连接符 U+200C/D 被清掉", clean("a\u200db\u200cc"), "abc");
check("BOM / 方向标记被清掉", clean("\ufeff知\u200f识\u061c"), "知识");
check("双向控制符被清掉", clean("a\u202ab\u202ec"), "abc");
check("软连字符 U+00AD 被清掉", clean("co\u00adoperate"), "cooperate");
check("变体选择符保留（emoji 呈现要用）", clean("⚠\ufe0f 注意"), "⚠\ufe0f 注意");
check("null / undefined 不抛异常", [clean(null), clean(undefined)], ["", ""]);
check("源码里已无字面量连字符区间",
  /\/\[[^\]]*\\u200B-\\u200F/.test(fs.readFileSync(EXT + "/platforms/base.js", "utf8")), true);

console.log();
console.log("=== ⑦ 飞书轮询不得静默吞掉「未收到结果」 ===");
// 实测（2026-10-07）：飞书发入库只收到「⏳ 等待浏览器抓取」，之后永远没有下文。
// 成因：poll_task_status() 最长轮询 300 秒，而服务端 STALE_PROCESSING_SECONDS 也是
// 300 秒 —— 两边同时到点，轮询拿不到 done/failed 就返回 None，_poll_and_notify 里
// `if task_result:` 为假 → 静默 return，一张卡片都不发。用户只能干等。
// 复发（2026-10-13，任务 2283fe28）：两边双双调到 600 秒后**又撞平了一次** ——
// 清扫每 60 秒才跑一轮，服务端在第 606.8 秒才写下「无浏览器接入」的结论，
// 而轮询在第 600 秒已经放弃，那句精确原因被丢掉，用户收到的是一张什么都没说的卡。
// 故这里要求**明显长于**阈值，而不是 ≥（≥ 允许撞平，正是漏网的地方）。
const fb = fs.readFileSync(path.join(TOOLS, "feishu-bot.py"), "utf8");
const m = fb.match(/MAX_POLL_TIME\s*=\s*(\d+)/);
check("MAX_POLL_TIME 已定义", !!m, true);
check("  → 必须长于服务端清扫阈值 600 秒（否则两者擦肩而过）",
  m ? Number(m[1]) > 600 : false, true);
check("轮询超时（未收到结论）时也要发卡片，不许静默 return",
  /if not task_result:/.test(fb), true);
check("  → 该卡片要说清「未收到结果」并给出可行下一步",
  (() => {
    // 锚定带图标的卡片标题（源码注释里也出现过这几个字，不能用裸词定位）。
    const i = fb.indexOf('"⌛ 尚未收到抓取结果"');
    if (i < 0) return false;
    // 卡片自身要带「重试」按钮 —— 用户不必记命令也能重做一次。
    return /retry_task/.test(fb.slice(i, i + 900));
  })(), true);
// 服务端清扫时写下的两句判词必须各自被翻译，不能落进「兜底」那一条 ——
// 它们的成因与处置都不同：等不到浏览器接入 → 去开浏览器；
// 已发给浏览器但没回传 → 保住那张抓取标签页。混成一句等于什么都没说。
check("服务端「无浏览器接入」判词有专属翻译",
  /"无浏览器接入" in err[\s\S]{0,200}请打开浏览器/.test(fb), true);
check("服务端「结果没有回传」判词有专属翻译",
  /"超时未响应" in err[\s\S]{0,200}抓取标签页/.test(fb), true);
check("轮询拿到的 failed 要带原因（不再是一句「未知错误」）",
  /task_result\.get\("error"\) or "未知错误"/.test(fb), true);

console.log();
console.log("=== ⑧ 报结果/报错都必须扛住 MV3 worker 回收 ===");
// MV3 的 service worker 空闲约 30 秒被回收，正在 await 的 fetch 会一起消失。
// 这正是「扩展显示任务已完成、飞书却收不到结果」的另一半成因。
// （结论：只加 keepalive 顶不住，故改为持久化发件箱 + 闹钟补发，见 ⑨。）
check("submitResult / submitError 仍存在且各自走发件箱",
  /async function submitResult\(/.test(bg) && /async function submitError\(/.test(bg), true);
check("  → 两者都经由 _report 落盘",
  (bg.match(/return await _report\(/g) || []).length >= 2, true);

console.log();
console.log("=== ⑨ 浏览器协助结果必须崩溃安全地回传 ===");
// 实测（2026-10-07）：11 个任务被派发给浏览器、0 个结果回传 —— 扩展侧明明执行了
// 任务，飞书/待审区却收不到结论。成因是 MV3 的 SW 在抓取途中被回收，
// 正在 await 的 fetch 随之消失。只加 keepalive 顶不住，必须持久化 + 闹钟补发。
const bg2 = fs.readFileSync(EXT + "/background.js", "utf8");
check("有持久化发件箱（chrome.storage）", /const OUTBOX_PREFIX = "outbox-"/.test(bg2), true);
check("上报前先落盘", /await _outboxAdd\(taskId, payload\)/.test(bg2), true);
check("有补发函数 flushOutbox", /async function flushOutbox\(\)/.test(bg2), true);
check("闹钟里会补发发件箱",
  /flushOutbox\(\)\.catch\(\(e\) => console\.warn\("\[Vault\] 补发发件箱异常/.test(bg2), true);
check("SW 启动（bootstrap）也会补发", /vaultBootstrap\(\)[\s\S]{0,400}flushOutbox/.test(bg2), true);
check("发件箱有防堆积的过期上限", /OUTBOX_MAX_AGE/.test(bg2), true);

console.log();
console.log("=== ⑩ 待审区失败必须写回原因（不是只出现在一次 HTTP 响应里）===");
check("服务端有写回原因的函数", /def _inbox_set_note\(/.test(cs), true);
check("异常失败时写回", /_inbox_set_note\(filename, f"处理失败：\{e\}"\)/.test(cs), true);
check("wiki 未生成时写回具体原因",
  /_inbox_set_note\(filename, f"处理失败（/.test(cs), true);
check("排入浏览器队列时写回排队说明",
  /_inbox_set_note\(filename, f"已排入浏览器协助队列/.test(cs), true);

console.log();
console.log("=== ⑪ 前端必须解释「卡在哪一步」，不能只说「仍在队列中」===");
const pj = fs.readFileSync(EXT + "/popup.js", "utf8");
check("有 explainTaskStall 辅助函数", /async function explainTaskStall\(taskId\)/.test(pj), true);
check("按 stage 给出人话原因", /const STAGE_REASON = \{/.test(pj), true);
check("区分「没浏览器接单」与「接了却没回传」",
  /尚未有浏览器接单/.test(pj) && /结果一直没有回传/.test(pj), true);
check("轮询超时分支改用 explainTaskStall",
  /const why = await explainTaskStall\(result\.taskId\)/.test(pj), true);

console.log();
console.log("=== ⑫ 浏览器重连要重发「发出去却无回传」的任务 ===");
check("task_queue 有重发方法",
  /def redispatch_stale_dispatched\(/.test(fs.readFileSync(path.join(TOOLS, "task_queue.py"), "utf8")), true);
check("SSE 连接后调用重发",
  /task_queue\.redispatch_stale_dispatched\(\)/.test(cs), true);

console.log();
console.log("=== ⑬ 进行中的任务必须能扛住 SW 回收后续跑 ===");
// 根因：MV3 里 setInterval **不能**让 Service Worker 保持存活（只有扩展 API 调用/
// 事件才行）。此前靠 startSwKeepalive 的定时器「保活」，SW 一旦在抓取中途被回收，
// 进行中的 processBrowserTask 连同 submitResult 一起消失，结果永远回不来。
// 因此必须把「正在抓的任务」落盘，SW 重启后由 alarm/bootstrap 续跑。
const bgjs = fs.readFileSync(EXT + "/background.js", "utf8");
check("任务开始前先落盘（pendingTask）",
  /async function _pendingTaskAdd\(taskId, url\)/.test(bgjs) &&
  /await _pendingTaskAdd\(taskId, url\)/.test(bgjs), true);
check("任务结束（成功或失败）都要摘盘",
  /async function _pendingTaskRemove\(taskId\)/.test(bgjs) &&
  /await _pendingTaskRemove\(taskId\)/.test(bgjs), true);
check("有续跑函数 resumePendingTasks",
  /async function resumePendingTasks\(\)/.test(bgjs), true);
check("闹钟里会续跑中断的任务",
  /resumePendingTasks\(\)/.test(bgjs.replace(/async function resumePendingTasks[\s\S]{0,2000}?\n\}/, "")), true);
check("已有回传记录的任务不再重复续跑",
  /const outbox = await chrome\.storage\.local\.get\(OUTBOX_PREFIX \+ item\.taskId\)/.test(bgjs), true);
check("续跑有过期上限，避免堆积",
  /PENDING_TASK_MAX_AGE/.test(bgjs), true);

console.log();
console.log("=== ⑭ 服务端与前端的时间阈值必须彼此相容 ===");
// 三条阈值的关系（见各自注释）：
//   前端轮询上限 ≥ 服务端卡死阈值 ≥ 重发阈值
// 任何一条反了，都会出现「明明还在处理，却被判成超时/被重复派发」。
const tqsrc = fs.readFileSync(path.join(TOOLS, "task_queue.py"), "utf8");
const fbsrc = fs.readFileSync(path.join(TOOLS, "feishu-bot.py"), "utf8");
check("服务端卡死阈值已放宽（≥ 600 秒）",
  /STALE_PROCESSING_SECONDS\s*=\s*(\d+)/.test(tqsrc) &&
  Number(tqsrc.match(/STALE_PROCESSING_SECONDS\s*=\s*(\d+)/)[1]) >= 600, true);
check("重发阈值明显长于正常耗时（≥ 240 秒）",
  /def redispatch_stale_dispatched\(self, min_age: float = (\d+)/.test(tqsrc) &&
  Number(tqsrc.match(/def redispatch_stale_dispatched\(self, min_age: float = (\d+)/)[1]) >= 240, true);
// 注意要求是**严格留出一个清扫间隔**，不能只判 ≥：清扫每 60 秒一轮
// （task_queue.start_sweeper），服务端写结论的时刻落在 [阈值, 阈值+60] 之间。
// 写成「等于阈值」会让两边同时到点、结论被丢掉（实测任务 2283fe28）。
const _stale = Number((tqsrc.match(/STALE_PROCESSING_SECONDS\s*=\s*(\d+)/) || [])[1] || 0);
const _sweep = Number((tqsrc.match(/def start_sweeper\(self, interval: float = (\d+)/) || [])[1] || 0);
check("前端轮询上限 > 服务端卡死阈值 + 清扫间隔",
  (() => {
    const poll = Number((pj.match(/const deadline = Date\.now\(\) \+ (\d+)/) || [])[1] || 0);
    return _stale > 0 && _sweep > 0 && poll >= (_stale + _sweep) * 1000;
  })(), true);
check("飞书轮询上限 > 服务端卡死阈值 + 清扫间隔",
  (() => {
    const mp = Number((fbsrc.match(/MAX_POLL_TIME\s*=\s*(\d+)/) || [])[1] || 0);
    return _stale > 0 && _sweep > 0 && mp >= _stale + _sweep;
  })(), true);

console.log();
console.log("=== ⑮ 风控拦截页不得被当作正文入库 ===");
// 根因：YouTube 抓取被 Google 验证码页劫持（google.com/sorry/?continue=...），
// 该页有几百字解释文字，长度检查拦不住、旧关键词表也没命中，
// 于是被当正常正文写进 raw、还生成了 wiki；同时 raw 的 source_url 变成验证码
// 地址，导致待审指针永远匹配不上、卡片一直卡在「等待浏览器抓取」。
const css = fs.readFileSync(path.join(TOOLS, "clip-server.py"), "utf8");
check("有拦截页识别函数 _interstitial_reason",
  /def _interstitial_reason\(data\) -> str:/.test(css), true);
check("识别 URL 指纹（google.com/sorry 等）",
  /_INTERSTITIAL_URL_MARKERS = \([\s\S]*?\/sorry\//.test(css), true);
check("识别正文指纹（异常流量 / 人机验证）",
  /_INTERSTITIAL_TEXT_MARKERS = \([\s\S]*?异常流量[\s\S]*?人机识别系统/.test(css), true);
check("拦截页闸门排在「图多字少」与长度检查之前",
  (() => {
    const iInt = css.indexOf("interstitial = _interstitial_reason(data)");
    const iImg = css.indexOf("text_in_images = _text_in_images_reason(data)");
    const iLen = css.indexOf("if real_len < _AUTO_REFINE_MIN_CHARS:");
    return iInt >= 0 && iImg > iInt && iLen > iImg;
  })(), true);
check("拦截页走 demote_to_inbox 退回待审区",
  /_interstitial_reason\(data\)[\s\S]{0,400}_demote_to_inbox\(\s*\n\s*filepath, data,\s*\n\s*\{"status": "failed", "reason": interstitial/.test(css), true);
check("拦截页的 note 前缀是「抓取被拦截」",
  /"interstitial": "抓取被拦截",/.test(css), true);

console.log();
console.log("=== ⑯ 待审指针的摘除必须认「任务原始 URL」===");
// 抓取可能被重定向（验证码页），result_data["url"] 已不是卡片里的那条链接。
// 只用它比对会永远摘不掉卡片 —— 界面一直显示「等待浏览器抓取」。
check("_settle_inbox_pointer 支持 origin_url",
  /def _settle_inbox_pointer\(data: dict, wiki, origin_url: str = ""\)/.test(css), true);
check("两个 URL 都参与比对",
  /for u in \(str\(data\.get\("url"\) or ""\)\.strip\(\), str\(origin_url or ""\)\.strip\(\)\):/.test(css), true);
check("浏览器结果路径传入任务原始 URL",
  /task_queue\.get_task_status\(task_id\)[\s\S]{0,300}_settle_inbox_pointer\(result_data, wiki, origin_url=_origin\)/.test(css), true);
check("重复分支也认任务原始 URL",
  /_inbox_clear_for_url\(_ou or str\(result_data\.get\("url"\) or ""\)\.strip\(\)\)/.test(css), true);
check("浏览器结果路径记录 _origin_url",
  /result_data\["_origin_url"\] = str\(_t0\["url"\]\)/.test(css), true);
check("_demote_to_inbox 建卡优先用 _origin_url",
  /origin = str\(data\.get\("_origin_url"\) or ""\)\.strip\(\)[\s\S]{0,120}url = origin/.test(css), true);

console.log();
console.log("=== ⑰ 飞书「浏览器协助」重复卡片必须有强制覆盖按钮 ===");
// 两条路径的分工：服务端回传完整字段 + 飞书轮询渲染按钮。缺一条就没有按钮。
check("服务端重复分支回传 canOverwrite",
  /"canOverwrite": gate\.get\("canOverwrite", False\)/.test(css), true);
check("服务端重复分支回传字数与 wiki 字段",
  /"existing_word_count": gate\.get\("existing_word_count", 0\)[\s\S]{0,900}"has_wiki": gate\.get\("has_wiki", False\)/.test(css), true);
check("飞书轮询的重复分支会构造按钮",
  /if info\.get\("error"\) == "duplicate":[\s\S]{0,1600}dbuttons\.append\(\{/.test(fbsrc), true);
check("飞书轮询重复分支带 force_overwrite 动作",
  /dbuttons\.append\(\{[\s\S]{0,200}"action": "force_overwrite"/.test(fbsrc), true);
check("飞书轮询重复分支的按钮会传进卡片",
  /send_feishu_card\(client, chat_id, "📌 已存在", dfields,\s*\n\s*color="orange", buttons=dbuttons or None\)/.test(fbsrc), true);

console.log();
console.log("=== ⑱ 降级回待审区时必须刷新卡片 note 并说清结局 ===");
// 根因：_inbox_create_card 在卡片已存在时返回 duplicate 且**不改 note**，
// 于是 demote 后卡片仍留着上一轮的「已排入浏览器协助队列…等待浏览器抓取」，
// 用户以为「处理了毫无反应」，实际任务早已完成并降级。
check("卡片已存在时会改写 note",
  /if not card\.get\("filename"\) and card\.get\("existing_file"\):\s*\n\s*_inbox_set_note\(card\["existing_file"\], f"\{prefix\}：\{reason\}"\)/.test(css), true);

console.log();
console.log("=== ⑲「全部处理」必须等浏览器任务出结论 ===");
// 根因：processAllBtn 只派发、立刻 explainTaskStall，不轮询最终结果，
// 于是永远停在「已发送给浏览器，结果一直没有回传」—— 哪怕任务早已入库/降级。
check("全部处理会等待各任务结论",
  /const settled = await Promise\.all\([\s\S]{0,200}awaitInflight\(it\.taskId/.test(pj), true);
check("按真实结局分别统计（入库/去重/降级）",
  /dupN\+\+[\s\S]{0,400}demotedN\+\+[\s\S]{0,400}doneN\+\+/.test(pj), true);
check("降级条目会说明「已退回待审区」",
  /条未入库（已退回待审区）/.test(pj), true);
check("未出结论的条目才回落到 explainTaskStall",
  /const why = await explainTaskStall\(it\.taskId\);\s*\n\s*detailLines\.push\("· " \+ it\.taskId \+ "：" \+ why\)/.test(pj), true);

console.log();
console.log("=== ⑳ 拦截降级要有专属文案 ===");
// kind="interstitial" 既不是「素材不完整」也不是「wiki 失败」，
// 处置是「在浏览器里过掉验证再重试」，文案必须区分开。
check("有 demoteIsBlocked 判定",
  /function demoteIsBlocked\(w\) \{[\s\S]{0,120}w\.kind === "interstitial"/.test(pj), true);
check("拦截降级有独立标题",
  /byBlocked \? INGEST_TITLE\.demotedBlocked/.test(pj) &&
  /demotedBlocked: "⚠️ 入库未完成：抓取被风控拦截"/.test(pj), true);
check("拦截降级的处置提示是「完成人机验证后重试」",
  /byBlocked[\s\S]{0,200}完成人机验证后，再点待审区的/.test(pj), true);

console.log();
console.log("=== ㉑ 弹窗关闭后结论不能丢 ===");
// Chrome 弹窗**一失焦就被销毁**，而处理待审区要等浏览器抓取 + 生成 wiki
// （几十秒到几分钟）。若结论只留在内存，用户重开弹窗就再也看不到失败原因。
check("结论会持久化到 storage",
  /const LAST_RESULT_KEY = "lastProcessResult"/.test(pj) &&
  /function persistStatus\(type, msg\)/.test(pj), true);
check("showStatus 落盘（过程态 loading 也落，供重开时核实）",
  /function showStatus\(type, msg\) \{[\s\S]{0,500}persistStatus\(type, msg\)/.test(pj) &&
  /if \(type === "loading"\) return;/.test(pj), false);
check("弹窗打开时恢复上次结论",
  /chrome\.storage\.local\.get\(\[LAST_RESULT_KEY\]\)/.test(pj), true);
check("恢复时只在状态框为空时进行（不覆盖本次新状态）",
  /if \(statusEl\.textContent\) return;/.test(pj), true);
check("过旧的结论不再回显",
  /if \(ageMin > 60\) return;/.test(pj), true);

console.log();
console.log("=== ㉒ 浏览器协助任务的结论要主动通知 ===");
// 弹窗可能早被关掉，任务却在后台跑完 —— 必须有一条离开弹窗的通知渠道。
const bgjs2 = fs.readFileSync(EXT + "/background.js", "utf8");
check("任务完成后会发桌面通知",
  /await notify\("✅ 已入库"/.test(bgjs2), true);
check("降级会通知「已退回待审区」",
  /await notify\("⚠️ 未入库，已退回待审区"/.test(bgjs2), true);
check("去重会通知「已存在于知识库」",
  /await notify\("📌 该链接已存在于知识库"/.test(bgjs2), true);
check("失败也会通知（带原因）",
  /await notify\("❌ 浏览器协助抓取失败"/.test(bgjs2), true);
// 通知图标必须**真实存在**：chrome.notifications.create 遇到缺失的 iconUrl
// 会静默失败（不报错、通知不出现）。此前 icon.png 从未随扩展打包，
// 于是所有完成通知都消失了 —— 用户因此永远收不到「抓完了」的提示。
check("通知用的图标文件确实存在",
  fs.existsSync(path.join(EXT, "icon128.png")), true);
check("notify 用的是 icon128.png（不是不存在的 icon.png）",
  /iconUrl = chrome\.runtime\.getURL\("icon128\.png"\)/.test(bgjs2), true);
check("notify 会报告 lastError，不再静默吞掉",
  /chrome\.runtime\.lastError[\s\S]{0,120}通知发送失败/.test(bgjs2), true);
check("manifest 声明了全套图标",
  (() => {
    const mf = JSON.parse(fs.readFileSync(path.join(EXT, "manifest.json"), "utf8"));
    const ic = mf.icons || {};
    return !!(ic["16"] && ic["32"] && ic["48"] && ic["128"]);
  })(), true);
check("manifest 图标文件都在",
  (() => {
    const mf = JSON.parse(fs.readFileSync(path.join(EXT, "manifest.json"), "utf8"));
    return Object.values(mf.icons || {}).every(f => fs.existsSync(path.join(EXT, f)));
  })(), true);

console.log();
console.log("=== ㉓ 浏览器协助不得抢焦点 / 尽量复用已有标签页 ===");
check("新建标签页不激活（active:false）",
  /chrome\.tabs\.create\(\{[\s\S]{0,200}active: false/.test(bgjs2), true);
check("显式指定 lastFocusedWindow",
  /chrome\.windows\.getLastFocused\(\)/.test(bgjs2), true);
check("优先复用「当前活动」的匹配标签页",
  /const active = tabs\.find\(t => t\.active && t\.url && _tabUrlKey\(t\.url\) === want\)/.test(bgjs2), true);
check("只关闭自己开的标签页（复用用户页面时不动它）",
  /if \(createdTab && tab\) \{/.test(bgjs2), true);

console.log();
console.log("=== ㉔ 重开弹窗要能接上「上次没跑完的任务」===");
// 弹窗一关，内存里的 taskId 就没了；只恢复「上次的结论」还不够 ——
// 那次操作可能压根还没结论。必须把在办任务一并落盘，重开时主动去核实。
check("在办任务会落盘（含 taskId 与原始 URL）",
  /const INFLIGHT_KEY = "lastInflightTask"/.test(pj) &&
  /function rememberInflight\(taskId, url\)/.test(pj) &&
  /inflightTask = \{ taskId, url: url \|\| "", at: Date\.now\(\) \}/.test(pj), true);
check("结论落地后会清除在办记录",
  /function forgetInflight\(\)/.test(pj) &&
  /async function awaitInflight\(taskId, url\) \{[\s\S]{0,140}forgetInflight\(\)/.test(pj), true);
check("loading 状态一并落盘（否则重开不知道上次没跑完）",
  /loading: type === "loading"/.test(pj), true);
check("弹窗打开时会去核实在办任务",
  /resumeInFlightTask\(\);/.test(pj) &&
  /async function resumeInFlightTask\(\)/.test(pj), true);
check("已完成的任务会被恢复成「上次已完成入库」",
  /上次的任务已完成入库/.test(pj), true);
check("降级任务会被恢复成「已退回待审区」",
  /上次的任务未入库，已退回待审区/.test(pj), true);
check("失败任务会被恢复成「上次的任务失败」",
  /上次的任务失败\\n原因/.test(pj), true);
check("仍在处理中的任务会显示阶段",
  /仍在处理中：\\n" \+[\s\S]{0,80}task\.stageLabel/.test(pj), true);
check("超过 1 小时的在办记录不再追",
  /if \(Date\.now\(\) - \(saved\.at \|\| 0\) > 3600000\) \{ forgetInflight\(\); return; \}/.test(pj), true);
check("连不上服务端时不丢在办记录（可能只是暂时不可达）",
  /未能连上本地服务确认最新状态/.test(pj) &&
  !/catch \(e\) \{[\s\S]{0,200}forgetInflight\(\);[\s\S]{0,200}未能连上本地服务/.test(pj), true);
check("不误用 /api/task/{id}/retry（它只接受 failed/done）",
  !/\/retry/.test(pj), true);

console.log();
console.log("=== ㉕ 风控拦截必须与「视频确实没字幕」分开表述 ===");
// 二者的处置**相反**：前者重试有意义（先登录/过验证），后者重试无意义。
// 过去共用一句「（该视频可能确实没有字幕/转录）」，把被风控的视频说成
// 「这视频没字幕」→ 用户直接放弃，而实际上重试一次就可能成功。
const serverSrc = fs.readFileSync(path.join(TOOLS, "clip-server.py"), "utf8");
check("定义了风控指纹列表",
  /_RISK_CONTROL_MARKERS = \(/.test(serverSrc), true);
check("指纹含 LOGIN_REQUIRED", /"LOGIN_REQUIRED"/.test(serverSrc), true);
check("指纹含 PO Token", /"PO Token"/.test(serverSrc), true);
check("指纹含「响应体为空」", /"响应体为空"/.test(serverSrc), true);
check("有 _is_risk_control 判定函数",
  /def _is_risk_control\(detail: str\) -> bool:/.test(serverSrc), true);
check("风控文案给出「登录+完成人机验证后重新剪藏」的处置",
  /来源被风控拦截。[\s\S]{0,120}完成人机验证[\s\S]{0,40}重新剪藏/.test(serverSrc), true);
// 「需要登录」是比一般风控更具体的一类：用户往往**已经登录了**却仍被要求登录
// （移动端客户端不认浏览器 cookie）。处置与一般风控不同，必须单独表述。
check("有 _is_login_required 单独判定",
  /def _is_login_required\(extra: dict, detail: str\) -> bool:/.test(serverSrc), true);
check("登录类优先于一般风控判定",
  serverSrc.indexOf("if _is_login_required(extra, detail):") <
  serverSrc.indexOf("if _is_risk_control(detail):"), true);
check("登录文案不谎称「视频没有字幕」",
  (() => {
    const m = serverSrc.match(/if _is_login_required\(extra, detail\):[\s\S]{0,600}?return reason/);
    return !!m && !/可能确实没有字幕/.test(m[0]) &&
      /要求登录后才能读取字幕数据/.test(m[0]);
  })(), true);
check("兼容扩展侧的 loggedOut 标记",
  /for k in \("logged_out", "loggedOut"\)/.test(serverSrc), true);
check("「可能确实没有字幕」只在非风控时才追加",
  (() => {
    // 风控分支必须**提前 return**，不能落到下面那句「可能确实没有字幕」
    const m = serverSrc.match(/if _is_risk_control\(detail\):[\s\S]{0,400}?return reason/);
    return !!m && !/可能确实没有字幕/.test(m[0]);
  })(), true);

// 用真实日志里的那句 captionNote 验证判定确实命中
check("真实 LOGIN_REQUIRED 文案被识别为风控",
  (() => {
    const m = serverSrc.match(/(_RISK_CONTROL_MARKERS = \([\s\S]*?\)\n)/);
    if (!m) return false;
    const markers = [...m[1].matchAll(/"([^"]+)"/g)].map(x => x[1]);
    const realNote = "InnerTube：ANDROID：无字幕轨（LOGIN_REQUIRED：请登录，" +
      "以便我们确认你不是聊天机器人）";
    return markers.some(k => realNote.includes(k));
  })(), true);
check("真实 PO Token 文案被识别为风控",
  (() => {
    const m = serverSrc.match(/(_RISK_CONTROL_MARKERS = \([\s\S]*?\)\n)/);
    if (!m) return false;
    const markers = [...m[1].matchAll(/"([^"]+)"/g)].map(x => x[1]);
    const realNote = "页面/zh：HTTP 200 但响应体为空（YouTube 的 PO Token 风控拦截的典型表现）";
    return markers.some(k => realNote.includes(k));
  })(), true);

console.log();
console.log("=== ㉖ 飞书的完成通知不得把「降级」说成「剪藏完成」 ===");
// 服务端在降级时刻意把 filename 置空串，但 success 仍为 True。
// 飞书过去直接读 filename → 渲染出绿色的「✅ 剪藏完成」而文件栏空白，
// 用户以为成功入了一个空文件。
const fbSrc = fs.readFileSync(path.join(TOOLS, "feishu-bot.py"), "utf8");
check("飞书先判 demoted 再判成功",
  /wk\.get\("status"\) == "demoted"[\s\S]{0,80}not str\(info\.get\("filename"\) or ""\)\.strip\(\)/.test(fbSrc), true);
check("降级卡片是橙色告警而非绿色成功",
  /send_feishu_card\(client, chat_id, head, dfields, color="orange"\)/.test(fbSrc), true);
check("内容不完整→专属标题",
  /kind == "content_incomplete"[\s\S]{0,80}未入库：内容不完整/.test(fbSrc), true);
check("风控拦截→专属标题与处置",
  /kind == "interstitial"[\s\S]{0,120}未入库：抓取被风控拦截/.test(fbSrc) &&
  /完成人机验证后，再重新剪藏/.test(fbSrc), true);

console.log();
console.log("=== ㉗ 选中文本必须跨 frame 汇总（正文在异构 iframe 内的站点） ===");
// 踩过：某商户经营平台 merchant.example.com 的 iUrl 套壳页，正文在 h5.example.com 的
// 跨域 iframe 里。顶层 window.getSelection() 看不到异构 iframe 的选区 →
// 用户手确实选中了，却报「未检测到选中文本」。修复靠 allFrames 汇总。
const bgSrc2 = fs.readFileSync(path.join(EXT, "background.js"), "utf8");
const registrySrc2 = fs.readFileSync(path.join(EXT, "platforms", "registry.js"), "utf8");
const contentSrc2 = fs.readFileSync(path.join(EXT, "content.js"), "utf8");

check("background 有跨 frame 汇总函数 probeSelection",
  /async function probeSelection\(tabId\)/.test(bgSrc2), true);
check("probeSelection 用 allFrames:true 注入",
  /probeSelection[\s\S]{0,600}allFrames:\s*true/.test(bgSrc2), true);
check("clipSelection 走 probeSelection（不再只问顶层）",
  /async function clipSelection\(tab\)[\s\S]{0,700}probeSelection\(tab\.id\)/.test(bgSrc2), true);
check("顶层无选区时不再直接放弃（clipSelection 直接走 probeSelection）",
  /async function clipSelection\(tab\)[\s\S]*?const hit = await probeSelection\(tab\.id\);[\s\S]*?if \(!hit\) \{\s*notify\("❌ 未检测到选中文本"/.test(bgSrc2), true);
check("有 selection-probe 消息通道",
  /action === "selection-probe"/.test(bgSrc2), true);
check("content 悬浮按钮在顶层无选区时请求 selection-probe",
  /sendMessageSafe\(\{\s*action:\s*"selection-probe"\s*\}\)/.test(contentSrc2), true);
check("registry 暴露 extractSelection（逐 frame 读取）",
  /extractSelection\(\)\s*\{/.test(registrySrc2), true);

console.log();
console.log("=== ㉘ 选中文本入库必须与「完整入库」同口径（等 wiki + 分档提示） ===");
// 踩过：右键「选中文字入库」不设 wikiSync，服务端立刻返回 success=true →
// 无论 wiki 成功/跳过/降级都弹「✅ 选中文本已入库 → undefined 字」。
// 降级时 raw 已被撤回，这是**假成功**。修复后必须等结论、按结论分档。
check("clipSelection 设了 wikiSync（与服务端等 wiki 出结论）",
  /async function clipSelection\(tab\)[\s\S]{0,1600}data\.wikiSync\s*=\s*true/.test(bgSrc2), true);
check("clipSelection 对 demoted 单独提示「未入库」",
  /async function clipSelection[\s\S]{0,2200}w\.status === "demoted"[\s\S]{0,200}未入库/.test(bgSrc2), true);
check("clipSelection 成功文案区分含 wiki / wiki 跳过 / wiki 后台",
  /已入库（含 wiki）/.test(bgSrc2) &&
  /已入库（wiki 跳过）/.test(bgSrc2) &&
  /已入库（wiki 后台生成中）/.test(bgSrc2), true);
// content.js 的 wikiTip 对选中文本同样要按 wiki 分档（此前无条件返回成功）
check("wikiTip 选中分支不再无条件报成功（含 demoted 处理）",
  /if \(isSelectionClip\) \{[\s\S]{0,700}wiki\.status === "demoted"[\s\S]{0,200}入库未完成/.test(contentSrc2), true);
check("wikiTip 选中分支含 demoted 处理",
  /if \(isSelectionClip\) \{[\s\S]{0,800}wiki\.status === "demoted"/.test(contentSrc2), true);

console.log();
console.log("=== ㉙ 服务端抓取疑似动态渲染时，飞书须主动提示改用浏览器剪藏 ===");
// 用户手机上看不到页面差异，飞书发的链接若只抓到 SPA 骨架，会以为
// 「知识库抓得不对」而反复重发。服务端识别出「动态渲染嫌疑」后必须
// 一路透传到飞书卡片。
const webSrc = fs.readFileSync(path.join(TOOLS, "platforms", "webpage.py"), "utf8");
const serverSrc3 = fs.readFileSync(path.join(TOOLS, "clip-server.py"), "utf8");
const fbSrc3 = fs.readFileSync(path.join(TOOLS, "feishu-bot.py"), "utf8");

check("网页适配器有动态渲染判定函数",
  /def _looks_dynamically_rendered\(html: str, content: str\)/.test(webSrc), true);
check("判定含 SPA 骨架标记表",
  /_SPA_MARKERS\s*=\s*\(/.test(webSrc) && /id="app"/.test(webSrc), true);
check("判定含「HTML 大 / 正文小」比值判据",
  /_HTML_TO_CONTENT_RATIO\s*=\s*20/.test(webSrc), true);
check("判定结果写入 extra.dynamic_render_suspect",
  /extra\["dynamic_render_suspect"\]\s*=\s*True/.test(webSrc), true);
check("服务端 _smart_clip 把嫌疑透传进响应",
  /dynamic_render_suspect[\s\S]{0,200}result\["dynamicRenderSuspect"\]\s*=\s*True/.test(serverSrc3), true);
check("飞书直接成功卡片渲染动态渲染提示",
  /dynamicRenderSuspect[\s\S]{0,200}dynamicRenderNote/.test(fbSrc3), true);
check("飞书嫌疑时用橙色告警标题而非绿色成功",
  /剪藏成功（内容可能不完整）/.test(fbSrc3), true);
check("飞书浏览器协助完成卡片同口径渲染提示",
  /if suspect:[\s\S]{0,200}sfields\.append\(\("提示"/.test(fbSrc3) ||
  /sfields\.append\(\("提示"[\s\S]{0,200}suspect/.test(fbSrc3), true);

console.log();
console.log("=== ㉚ 任务面板必须统计**全部渠道**，且历史可回看 ===");
// 症状①：面板只列浏览器协助队列 → 走 /api/clip 直接推送的「立即完整入库」
//   在面板里凭空消失，用户以为「任务列表没显示 = 没提交成功」。
// 症状②：任务一旦成功/失败就从面板消失，无法回看「刚才那条成没成、何时成的」。
// 修复：task_queue 增 via 列 + record_direct_clip + list_recent_tasks；
//   /api/tasks 增 scope=history；popup 增加「最近记录」区与展开按钮。
const tqSrc = fs.readFileSync(path.join(TOOLS, "task_queue.py"), "utf8");
const popupSrc = fs.readFileSync(path.join(EXT, "popup.js"), "utf8");
const popupHtml = fs.readFileSync(path.join(EXT, "popup.html"), "utf8");

check("task_queue 有 via 列迁移",
  /ALTER TABLE tasks ADD COLUMN via TEXT DEFAULT ''/.test(tqSrc), true);
check("task_queue 有 record_direct_clip（登记直接推送的剪藏）",
  /def record_direct_clip\(self, url: str, via: str = "direct"/.test(tqSrc), true);
check("task_queue 有 list_recent_tasks（活跃 + 历史统一按时间倒序）",
  /def list_recent_tasks\(self, limit: int = 50/.test(tqSrc) &&
  /COALESCE\(NULLIF\(completed_at, 0\), created_at\) DESC/.test(tqSrc), true);
check("add_task / start_server_task 接受 via",
  /def add_task\(self, url: str, via: str = ""\)/.test(tqSrc) &&
  /def start_server_task\(self, url: str, stage: str = "server-fetch",\s*\n\s*via: str = ""\)/.test(tqSrc), true);
check("服务端 /api/tasks 支持 scope=history",
  /scope = params\.get\("scope", \["active"\]\)/.test(serverSrc3) &&
  /scope == "history"[\s\S]{0,120}list_recent_tasks/.test(serverSrc3), true);
check("服务端 /api/clip 会补记任务历史",
  /task_queue\.record_direct_clip\(/.test(serverSrc3), true);
check("服务端有 _clip_via 区分渠道（不再一律写「浏览器插件」）",
  /def _clip_via\(data: dict\) -> str:/.test(serverSrc3) &&
  /data\["via"\] = _clip_via\(data\)/.test(serverSrc3), true);
check("飞书请求体带 via=飞书",
  /"via": "飞书"/.test(fbSrc3), true);
// ⚠️ 2026-10-09：任务队列**在 popup 内横向展开**（弹窗变宽，右栏并排），
// 既不是向下撑高，也不是另开窗口。
check("popup 有任务队列区块（最近记录 + 进行中）",
  /id="taskHistory"/.test(popupHtml) &&
  /id="taskList"/.test(popupHtml) &&
  /async function loadTaskHistory\(/.test(popupSrc), true);
check("popup 有「展开更多历史」按钮",
  /id="taskMoreBtn"/.test(popupHtml) &&
  /taskMoreBtn\.addEventListener/.test(popupSrc), true);
check("活跃任务请求 scope=active（不再是默认隐含全部语义）",
  /\/api\/tasks\?scope=active/.test(popupSrc), true);
check("任务队列是 popup 内的右栏，不再另开窗口",
  /id="taskPanel"/.test(popupHtml) &&
  /class="col-queue"/.test(popupHtml) &&
  !/chrome\.windows\.create\(/.test(popupSrc) &&
  !/chrome\.runtime\.getURL\("tasks\.html"\)/.test(popupSrc), true);

console.log();
console.log("=== ㉛ 弹窗不得卡死在 loading，且请求不随弹窗消失 ===");
// 症状：点击「立即完整入库」后状态框停在「正在入库并生成 wiki…」。
// 根因①：/api/clip 的 fetch **没有超时** —— 服务端若卡住，请求既不成功也不失败。
// 根因②（用户 2026-10-09 指出）：**「剪藏后随手关掉弹窗去看别的网页」是完全正常
//   的操作**，弹窗一失焦即销毁 → 若请求由弹窗自己发，会被连同弹窗一起中止，
//   服务端只收到半截连接（WinError 10053），**这次剪藏凭空丢了**。
// 修复：把请求交给 background service worker 发出（生命周期独立于弹窗），
//   并在弹窗侧保留兜底超时 + 可核实提示。与本项目 MV3 铁律一致。
// 通知：**无条件发**，与悬浮按钮 / 选中剪藏 / 浏览器协助三条路径一致 ——
//   用户可能已关掉弹窗，也可能只是切到了别的标签页；结论都该主动送达。
check("popup 把剪藏交给 background 发出（请求不随弹窗消失）",
  /chrome\.runtime\.sendMessage\(\{ action: "clip-direct", data: data, fromPopup: true \}\)/.test(popupSrc), true);
check("  → background 无条件补一条系统通知（不拿「弹窗在不在」当条件）",
  /if \(fromPopup\) \{[\s\S]{0,200}await notifyClipResult\(result\)/.test(bg) &&
  /async function notifyClipResult\(result\)/.test(bg), true);
check("  → 不再用 getContexts 判断弹窗是否存在（避免同一事件两套规则）",
  !/getContexts/.test(bg), true);
check("  → 通知结论统一取自 buildIngestOutcome（成功 / 失败 / 降级都覆盖）",
  /async function notifyClipResult\(result\) \{[\s\S]{0,400}?buildIngestOutcome\(result\)/.test(bg) &&
  /buildIngestOutcome[\s\S]{0,1600}?INGEST_TITLE\.wikiBad/.test(bg) &&
  /return \{ type: "error", title: "❌ 入库未完成"/.test(bg), true);
check("popup 侧仍保留兜底超时（后台卡死也不能停住 loading）",
  /Promise\.race\([\s\S]{0,300}__timeout__/.test(popupSrc), true);
check("超时后给出可核实的说明（而非只抛 abort）",
  /超过 3 分钟未收到结论/.test(popupSrc) &&
  /任务队列 → 最近记录/.test(popupSrc), true);
check("reportIngest 对「无 filename」不再渲染「入库成功」",
  /if \(!result\.filename\) \{[\s\S]{0,200}INGEST_TITLE\.noFile/.test(popupSrc), true);

console.log();
console.log("=== ㉜-1 「正在覆盖」不得卡住（重开弹窗要能看到真实结论） ===");
// 用户 2026-10-09：文章「已经入库成功了」，插件却还提示「正在覆盖并重新生成 wiki」。
// 成因：请求改由 background 发出后，**重开的弹窗学不到结论** —— 它只从 storage
// 读到上次写下的 loading（过程态），永远停在那儿。
// 修复：background 完成时把**最终结论**写回同一个 key（persistPopupResult）；
// 弹窗打开若读到 loading，则轮询等待这条结论出现（waitForBackgroundResult）。
check("background 完成后把最终结论写回 popup 读的 key",
  /async function persistPopupResult\(result\)/.test(bg) &&
  /lastProcessResult: \{ type, msg, at: Date\.now\(\), loading: false \}/.test(bg), true);
check("  → 结论取自 buildIngestOutcome（成功/失败同一判据，不再停在过程态）",
  /async function persistPopupResult[\s\S]{0,400}?buildIngestOutcome\(result\)[\s\S]{0,300}?loading: false/.test(bg), true);
check("弹窗读到 loading 时轮询等待 background 写入结论",
  /async function waitForBackgroundResult\(/.test(popupSrc) &&
  /waitForBackgroundResult\(last\.at \|\| 0, last\.msg\)/.test(popupSrc), true);
check("  → 只在 loading 原样显示时才覆盖（用户做了别的操作就不动它）",
  /if \(statusEl\.textContent !== placeholder\) return;/.test(popupSrc), true);

console.log();
console.log("=== ㊻ 「上次结论」回显不得自增（每开一次弹窗多一行） ===");
// 用户 2026-10-09 贴图：状态框堆了 5 条「（以上为上次操作的结论，发生于…
// 分钟前。）」，时间戳还前后不一致（1/46/1/11/1）。
// 根因：showStatus() 内部会 persistStatus() 把**渲染后的文字**写回 storage，
// 而恢复分支又拼了一遍注解 → 下次打开读到「带注解的文本」再拼一遍，
// 每开一次长一行；同时 at 被刷成当下，所以时间看着乱。
// 修复：注解只在渲染时拼（withAgeNote），回显一律 { persist: false }。
check("注解抽成 withAgeNote（渲染与存储分离）",
  /function withAgeNote\(msg, ageMin\)/.test(popupSrc), true);
check("恢复分支用 withAgeNote 且显式不落盘",
  /showStatus\(last\.type, withAgeNote\(clean, ageMin\), \{ persist: false \}\)/.test(popupSrc), true);
check("  → showStatus 支持 opts.persist === false（跳过写回）",
  /function showStatus\(type, msg, opts\)/.test(popupSrc) &&
  /if \(!opts \|\| opts\.persist !== false\)/.test(popupSrc), true);
check("  → 有 stripAgeNotes 自愈历史遗留的多行注解",
  /function stripAgeNotes\(msg\)/.test(popupSrc) &&
  /if \(clean !== last\.msg\)/.test(popupSrc) &&
  /msg: clean/.test(popupSrc), true);
check("  → background 写回的结论是干净的（不含注解）",
  !/以上为上次操作的结论/.test(bg), true);

console.log();
console.log("=== ㉜-2 最近记录与指标卡同口径（进行中/完成/失败都要有） ===");
// 用户 2026-10-09：最近记录要和指标卡一致，只显示「进行中/完成/失败」。
// 此前最近记录把活跃任务过滤掉了，于是「卡片说进行中 2、列表一条都没有」。
check("最近记录不再剔除活跃任务（涵盖进行中/完成/失败）",
  // 仍按 status 保留活跃任务（不退回「只留 done|failed」）；是否会另有
  // duplicate 过滤见 ㊴，这里只保证活跃任务没被丢掉。
  /const finished = all\.filter/.test(popupSrc) &&
  !/all\.filter\(t => t\.status === "done" \|\| t\.status === "failed"\)/.test(popupSrc), true);
check("  → 按 status 渲染对应标签（含处理中/排队中）",
  /processing: \{ cls: "", label: "⏳ 处理中" \}/.test(popupSrc) &&
  /pending: \{ cls: "", label: "⏳ 排队中" \}/.test(popupSrc), true);

console.log();
console.log("=== ㉜ 整页提取必须跨 frame（正文整块在跨域 iframe 内的站点） ===");
// 踩过：某商户经营平台 merchant.example.com 的 iUrl 套壳页，**顶层只有 132 字**的一句
// 「Please enable JavaScript」，正文（数千字）全在 h5.example.com 的跨域
// iframe 里。用户「页面上明明看得见」，扩展却报「页面内未检测到有效正文」。
// 根因：`sendMessage(tab.id, {action:"extract"})` **默认只到顶层 frame**。
// 修复：用 allFrames:true 逐 frame 提取，取正文最长的一个。
const bgSrc4 = fs.readFileSync(path.join(EXT, "background.js"), "utf8");
const popupSrc4 = fs.readFileSync(path.join(EXT, "popup.js"), "utf8");

check("background 有跨 frame 正文提取函数 extractAcrossFrames",
  /async function extractAcrossFrames\(tabId\)/.test(bgSrc4), true);
check("extractAcrossFrames 用 allFrames:true 注入",
  /extractAcrossFrames[\s\S]{0,1500}allFrames:\s*true/.test(bgSrc4), true);
check("跨 frame 结果按「正文最长」选优（去空白后比较，避免被套壳 UI 文案骗过）",
  /replace\(\/\\s\/g, ""\)\.length/.test(bgSrc4) &&
  /results\.sort\(\(a, b\) => norm\(b\.content\) - norm\(a\.content\)\)/.test(bgSrc4), true);
check("浏览器协助派发路径走 extractAcrossFrames（不再只问顶层）",
  /async function processBrowserTask\(taskId, url\)[\s\S]{0,5000}const picked = await extractAcrossFrames\(tab\.id\)/.test(bgSrc4), true);
check("悬浮按钮「入库此页」同样走 extractAcrossFrames",
  /async function clipTab\(tab\)[\s\S]{0,1200}await extractAcrossFrames\(tab\.id\)/.test(bgSrc4), true);
check("跨 frame 提取后用**顶层**标题与 URL（保证去重/回链指向用户点的那条）",
  /extractAcrossFrames\(tab\.id\)[\s\S]{0,900}getTopFrameInfo\(tab\.id\)[\s\S]{0,300}data\.title = topInfo\.title/.test(bgSrc4), true);
check("有 extract-all-frames 消息通道（供 popup 调用）",
  /msg\.action === "extract-all-frames"/.test(bgSrc4), true);
check("该通道先补注入 content script（旧页面没有 registry 会全返回 null）",
  /extract-all-frames[\s\S]{0,900}files: CONTENT_SCRIPT_FILES/.test(bgSrc4), true);
check("popup 的 extractInPage 改走跨 frame 通道",
  /extract-all-frames/.test(popupSrc4) &&
  !/chrome\.tabs\.sendMessage\(tab\.id, \{ action: "extract" \}\)/.test(popupSrc4), true);

// manifest 必须开 all_frames：否则 content script 只在顶层自动注入，
// handle 里的补注入虽然也能覆盖，但 SPA 的 waitForContent / 平台适配器
// 在 iframe 内根本不会跑，提取质量会打折。
const manifestSrc = fs.readFileSync(path.join(EXT, "manifest.json"), "utf8");
check("manifest 的 content_scripts 开了 all_frames",
  /"all_frames":\s*true/.test(manifestSrc), true);
// 反过来：开了 all_frames 后必须挡住「非顶层注入悬浮按钮」，
// 否则每个跨域 iframe 都会长出一个按钮（叠在一起 / 飘在别家 iframe 角落）。
const contentSrc3 = fs.readFileSync(path.join(EXT, "content.js"), "utf8");
check("非顶层 frame 不再注入悬浮按钮",
  /window\.self === window\.top/.test(contentSrc3) &&
  /if \(!isTop\) return;/.test(contentSrc3), true);
check("15 秒兜底分支也不再无条件注入（要复查 isTop + 内容达标）",
  /if \(isTop && hasMeaningfulContent\(\) &&[\s\S]{0,80}clip-vault-fab/.test(contentSrc3), true);

console.log();
console.log("=== ㉝ 懒加载长文必须先「唤醒」再提取（否则只抓到首屏） ===");
// 踩过：用户剪某商户经营平台同一条链接，待审区提示「本次抓取 667 字」，
// 而库里已有版本 2601 字。667 不是提取失败 —— 长正文按滚动位置分批渲染，
// 剪藏时 iframe 里只有首屏进了 DOM，抓到的是「半篇」。
// 修复：提取前逐个 frame 滚一遍把下游节点逼出来，再提取。
check("提取前有逐 frame 的滚动唤醒步骤（在 extractAcrossFrames 内）",
  /async function extractAcrossFrames\(tabId\)[\s\S]{0,600}唤醒[\s\S]{0,1200}target: \{ tabId, allFrames: true \}[\s\S]{0,1500}scrollTo/.test(bgSrc4), true);
check("唤醒步骤滚动的是**每个 frame 自己的文档**（只滚顶层对 iframe 内容无效）",
  /extractAcrossFrames[\s\S]{0,900}target: \{ tabId, allFrames: true \}[\s\S]{0,900}document\.scrollingElement/.test(bgSrc4), true);
check("唤醒后统一等待再提取（给懒加载留出渲染窗口）",
  /await new Promise\(r => setTimeout\(r, 800\)\)[\s\S]{0,400}let frames = \[\]/.test(bgSrc4), true);
check("同时滚内部的 overflow 滚动容器（正文常不在 window 上滚）",
  /scrollTop = el\.scrollHeight/.test(bgSrc4), true);
check("浏览器协助路径不再重复只滚顶层（已并入跨 frame 唤醒）",
  !/window\.scrollTo\(0, document\.body\.scrollHeight\)/.test(bgSrc4), true);

console.log();
console.log("=== ㉞ 「本次抓取偏少」只给一句可执行建议 ===");
// 踩过：用户反复报「只抓到 667 字」，而库里已有 2601 字。界面上只有一个悬殊的
// 数字，用户无从判断，只能反复重试并认定功能失效。
// 用户 2026-10-09 明确要求：**只建议「直接选中正文后剪藏」**，不要并列成因与
// 多种办法 —— 话多反而让用户不知道到底该按哪条做。
const popupSrc5 = fs.readFileSync(path.join(EXT, "popup.js"), "utf8");
check("字数相差悬殊（不足一半）时才给建议",
  /newN < oldN \/ 2/.test(popupSrc5), true);
check("建议就是「直接在页面上选中正文后剪藏」",
  /相差悬殊。建议直接在页面上选中正文后剪藏。/.test(popupSrc5), true);
check("  → 不再解释成因、不再给第二种办法（只一句）",
  !/正文尚未渲染完成/.test(popupSrc5) &&
  !/等页面完整显示后再试/.test(popupSrc5), true);
check("服务端记录本次提交字数（便于日后排查偏少投诉）",
  /本次提交：\{gate\.get\('new_word_count'\)\}/.test(fs.readFileSync(path.join(__dirname, "..", "clip-server.py"), "utf8")), true);

console.log();
console.log("=== ㉟ 正文容器选优不得被导航容器压过（踩过：只入库了 223 字导航项） ===");
// 踩过：某商户经营平台页面左侧导航、右侧正文。导航里的列表项被包成几十个 <p>，
// 而正文用 <div>。旧的 scoreElement = text.length/100 + pCount*5 让导航凭 pCount
// 拿到 200+ 分压过正文；且旧实现对候选「>200 即胜出」、从不与 body 比较，
// 于是正文整块丢失（实测 1974 字 → 223 字）。真实站点上表现为「只抓到 667 字」。
// 修复：① 评分改用「去空白文本长度」为主判据；② 候选一定含 body；
//      ③ 补齐 .article-body / [class*=article] 等常见容器。
const baseSrc = fs.readFileSync(path.join(EXT, "platforms", "base.js"), "utf8");
check("评分主判据是「去空白文本长度」（不再让 pCount 主导）",
  /scoreElement\(el\)[\s\S]{0,600}replace\(\/\\s\/g, ""\)\.length/.test(baseSrc), true);
check("pCount 只做轻微加分（不再 ×5 主导评分）",
  /Math\.min\(pCount, 40\) \* 2/.test(baseSrc), true);
check("候选容器一定包含 document.body（永远有个不丢内容的兜底）",
  /document\.body,[\s\S]{0,200}\]\.filter\(Boolean\)/.test(baseSrc), true);
check("补齐了 .article-body / [class*=article] 等常见正文容器",
  /\.article-body/.test(baseSrc) && /class\*="article"/.test(baseSrc), true);
check("不再用「候选分数 >200 就替代 body」的旧逻辑",
  !/bestScore < 200/.test(baseSrc), true);

console.log();
console.log("=== ㊱ 最近记录只展示今日（与指标卡口径一致） ===");
// 用户 2026-10-09：「最近记录只需要展示今日的，和指标卡保持一致」。
// 指标卡用 get_today_stats（今日 00:00 起），若历史列表仍按 limit 拉全部，
// 两块数据口径不一 —— 卡片说「今天 3 条」，列表里却躺着上周的记录。
const tqTodaySrc = fs.readFileSync(path.join(TOOLS, "task_queue.py"), "utf8");
check("list_recent_tasks 支持 today_only 参数",
  /def list_recent_tasks\(self,[\s\S]{0,200}today_only: bool = False/.test(tqTodaySrc), true);
check("  → 今日边界取「本地 00:00」（与 get_today_stats 一致）",
  /def list_recent_tasks[\s\S]{0,1800}hour=0, minute=0, second=0, microsecond=0/.test(tqTodaySrc), true);
check("/api/tasks 接收 today=1 并把 today_only 透传下去",
  /today_only = params\.get\("today", \["0"\]\)\[0\] == "1"/.test(cs) &&
  /list_recent_tasks\(limit=limit, today_only=today_only\)/.test(cs), true);
check("任务窗口的历史请求带上 today=1",
  /scope=history[\s\S]{0,80}today=1|todayOnly \? "&today=1"/.test(popupSrc5), true);

console.log();
console.log("=== ㊲ 客户端中途断开（WinError 10053）不得记为「失败任务」 ===");
// 用户 2026-10-09 报了一个红叉任务：「(请求体未解析成功)[WinError 10053]
// 你的主机中的软件中止了一个已建立的连接。」真实成因：弹窗在服务端读 body
// 途中被关掉，连接被客户端中止。这不是剪藏失败，不该留下一条吓人的失败记录。
check("读取请求体封装成 _read_body（返回 (data, error) 而非直接抛）",
  /def _read_body\(self\)/.test(cs), true);
check("  → ConnectionAborted 等映射为 client_disconnect",
  /return None, "client_disconnect"/.test(cs), true);
check("客户端断开时静默返回，不建失败任务",
  /if err == "client_disconnect":[\s\S]{0,300}return/.test(cs), true);
check("不再把「请求体未解析成功」当作任务失败原因写给用户",
  // 该字样现在只允许出现在解释历史的注释里，不能再进 record_direct_clip 的 message。
  !/record_direct_clip\(\s*\n?\s*"（请求体未解析成功）"/.test(cs) &&
  /"（剪藏未完成）", via="浏览器插件"/.test(cs), true);
check("请求行/请求头阶段断开也不再打一大段 traceback",
  /class VaultHTTPServer\(ThreadingHTTPServer\)/.test(cs) &&
  /def handle_error\(self, request, client_address\)[\s\S]{0,400}ConnectionAbortedError/.test(cs), true);
check("  → 其余异常仍交给默认处理（不掩盖真正的 bug）",
  /super\(\)\.handle_error\(request, client_address\)/.test(cs), true);

console.log();
console.log("=== ㊳ 懒加载图片不得丢失（微信原文有图，raw 里却没有） ===");
// 用户 2026-10-09：微信文章明明有图，raw 里一张都没有。
// 成因：elementToMarkdown 只读 img.src，而懒加载图（微信等）真实地址在
// data-src —— 此时 src 为空，图片被整段丢掉。
check("base.js 提供 pickImageSrc（依次尝试 data-src 等懒加载属性）",
  /pickImageSrc\(img\)/.test(baseSrc) && /data-src/.test(baseSrc), true);
check("  → 从 srcset 里取最后一个候选",
  /srcset[\s\S]{0,120}split\(","\)\.pop\(\)/.test(baseSrc), true);
check("elementToMarkdown 用 pickImageSrc 取地址，不再只读 img.src",
  /pickImageSrc\(img\)[\s\S]{0,300}replaceWith\(document\.createTextNode/.test(baseSrc), true);
check("  → 取不到地址的图片直接剔除（不留下 ![](空) 噪音）",
  /if \(!src\) \{[\s\S]{0,160}img\.remove\(\);[\s\S]{0,60}return;/.test(baseSrc), true);
const wechatSrc = fs.readFileSync(path.join(EXT, "platforms", "wechat.js"), "utf8");
check("wechat.js 提取图片同样走 pickImageSrc",
  /pickImageSrc\(img\)/.test(wechatSrc), true);
check("  → 图片区只补未内联的，避免重复",
  /content\.includes\(u\)/.test(wechatSrc), true);

console.log();
console.log("=== ㊴ 最近记录只列「进行中 / 完成 / 失败」，排除去重记录 ===");
// 用户 2026-10-09 两次反馈：最近记录里还挤着「已存在（未重复入库）」的记录。
// 这类记录 status 也是 done（result.duplicate=true），只看 status 过滤不掉，
// 必须按 result.duplicate 再排一次 —— 它既不是进行中，也不是一次真正的入库。
const popupSrc6 = fs.readFileSync(path.join(EXT, "popup.js"), "utf8");
check("loadTaskHistory 保留全部 status（不把活跃任务过滤掉）",
  !/const finished = all;\n/.test(popupSrc6) &&
  /const finished = all\.filter/.test(popupSrc6), true);
check("  → 按 result.duplicate 排除「已存在（未重复入库）」",
  /const finished = all\.filter\(t => !\(t\.result && t\.result\.duplicate\)\)/.test(popupSrc6), true);

console.log();
console.log("=== ㊵ 「强制覆盖」必须真的覆盖，而不是另存 -1.md ===");
// 用户 2026-10-09：点了「强制覆盖」，结果 raw 里多出一个 -1.md，旧文件还在。
// 根因：_preflight_duplicate 旧实现 `if force or not normalized_url: return None`
// 把 force 当成「跳过去重」；调用方于是走正常写盘，而 generate_card 为避免同名
// 冲突总是另存 -N.md → 覆盖变成不断堆积新文件。正确语义：force 必须返回可覆盖指令。
check("_preflight_duplicate 不再把 force 当「跳过去重」",
  !/if force or not normalized_url:\s*\n\s*return None/.test(cs), true);
check("  → force 时返回 _overwrite 指令（而非 None）",
  /if force:[\s\S]{0,300}"_overwrite": True/.test(cs), true);
check("  → 覆盖前先删旧文件（_delete_article_with_images）",
  /_overwrite[\s\S]{0,400}_delete_article_with_images/.test(cs), true);
check("  → /api/clip 覆盖分支把 _replaced 标记回传给客户端",
  /data\["_replaced"\] = True[\s\S]{0,3200}resp\["replaced"\] = True/.test(cs), true);
check("  → /api/clip-url 覆盖时沿用旧文件名（不再生成 -N.md）",
  /final_name = data\.get\("_replaced_name"\) or new_filename/.test(cs), true);

console.log();
console.log("=== ㊶ 微信「图片消息」（appmsg_type=9）也要抓到图 ===");
// 用户 2026-10-09：刚覆盖的微信公众号文章仍然没有把图片加入 raw。
// 排查结论：这篇文章是「图片消息」（小绿书，appmsg_type=9），页面**没有
// #js_content**，图片渲染在 swiper 容器（#img_list / .share_media_swiper_content）
// 里，真实地址来自 JS 的 picture_page_info_list[].cdn_url。
// 只查 `#js_content img` 的旧实现，在这类页面上**一张图都取不到**。
const wechatSrc2 = fs.readFileSync(path.join(EXT, "platforms", "wechat.js"), "utf8");
check("wechat.js 覆盖了图片消息的 swiper 容器",
  /SWIPER_CONTAINERS/.test(wechatSrc2) && /#img_list/.test(wechatSrc2), true);
check("  → 正文容器不再只认 #js_content",
  /querySelector\("#js_content"\)[\s\S]{0,120}rich_media_content/.test(wechatSrc2), true);
check("  → waitForContent 也认得 swiper（否则图片消息永远等不到内容）",
  /SWIPER_CONTAINERS[\s\S]{0,200}querySelector\(sel\)/.test(wechatSrc2), true);
check("  → extractImages 收集 swiper 里的 img",
  /#img_list img[\s\S]{0,200}swiper_item_img img/.test(wechatSrc2), true);
check("  → DOM 未渲染时从 picture_page_info_list 的 cdn_url 兜底",
  /picture_page_info_list/.test(wechatSrc2) && /cdn_url/.test(wechatSrc2), true);
check("  → 不再把图片 URL 的参数（?wx_fmt=…）截掉（会破坏去重比对）",
  !/replace\(\/\\\?wx_fmt=\\w\+\$\/, ""\)/.test(wechatSrc2), true);

console.log();
console.log("=== ㊷ 额度用尽（HTTP 429）必须给出中文结论 + 重置时间 ===");
// 用户 2026-10-09：任务失败，原因是「生成 wiki 的 apikey 限额了」。
// 实测火山方舟返回 {"error":{"code":"AccountQuotaExceeded","message":"You have
// exceeded the 5-hour usage quota. It will reset at 2026-10-09 19:08:21 +0800 CST..."}}。
// 旧实现把这段英文原文截断塞进用户可见文案（中英混排、看不懂、还看不到重置时间）。
// 现在要：① 译成中文结论；② 单独摘出 reset at 的时间（用户唯一能据此行动的信息）。
const wgSrc = fs.readFileSync(path.join(TOOLS, "wiki_gen.py"), "utf8");
check("_describe_api_error 识别额度用尽（quota / 429）",
  /quota[\s\S]{0,200}code == 429|code == 429[\s\S]{0,200}quota/.test(wgSrc), true);
check("  → 输出中文结论「模型 API 额度已用尽」",
  /模型 API 额度已用尽/.test(wgSrc), true);
check("  → 从报文里摘出 reset at 的重置时间",
  /reset\(\?:s\)\?\\s\+at\\s\+/.test(wgSrc), true);
check("  → 文案带「届时可重试」的行动指引",
  /届时可重试/.test(wgSrc), true);
check("非额度错误仍保留原样（不被误判成额度问题）",
  /return f"HTTP \{code\}"/.test(wgSrc), true);

console.log();
console.log("=== ㊸ 命中去重不得记为「失败任务」 ===");
// 用户 2026-10-09：「最近一次任务失败了，你查一下什么原因」—— 库里那条 failed
// 的 error 是 "duplicate"。可它根本不是失败：内容早已在知识库里，没丢数据也没出错。
// 成因：服务端直抓路径用 fail_task(id, "duplicate") 收尾 → 「最近记录」里凭空多出
// 一条红色失败任务，用户以为这次剪藏坏了。
// 规则：命中去重记 status="done" + result.duplicate=true（前端据此显示
// 「📌 已存在（未重复入库）」），绝不写 error。
const dupTqSrc = fs.readFileSync(path.join(TOOLS, "task_queue.py"), "utf8");
check("task_queue 提供 complete_duplicate_task",
  /def complete_duplicate_task\(/.test(dupTqSrc), true);
check("  → 内部写 status='done'",
  /def complete_duplicate_task[\s\S]{0,700}status='done'/.test(dupTqSrc), true);
check("  → 内部强制带 duplicate 标记",
  /payload\["duplicate"\] = True/.test(dupTqSrc), true);
check("  → 方法体内不调用 fail_task",
  !/def complete_duplicate_task[\s\S]{0,700}?self\.fail_task\(/.test(dupTqSrc), true);

const dupSrvSrc = fs.readFileSync(path.join(TOOLS, "clip-server.py"), "utf8");
check("服务端直抓去重分支改用 complete_duplicate_task",
  !/task_queue\.fail_task\(fetch_task_id, "duplicate"\)/.test(dupSrvSrc), true);
check("  → 传 existingFile / wordCount",
  /complete_duplicate_task\(fetch_task_id, \{[\s\S]{0,200}existingFile/.test(dupSrvSrc), true);
// 浏览器协助回传的重复分支也必须带 duplicate 标记，否则前端 historyOutcome()
// 认不出「命中去重」，会把它渲染成「✅ 已入库」。
check("浏览器协助重复分支带 duplicate: True",
  /"duplicate": True[\s\S]{0,900}?complete_duplicate_task\(task_id/.test(dupSrvSrc), true);

console.log();
console.log("=== ㊹ 同一结果的多条通知路径必须渲染一致 ===");
// 用户 2026-10-09：「插件提示 ⚠️ raw 已入库，但 wiki 未生成；浏览器通知却提示
// 成功入库」。根因：notifyClipResult 只看 result.success，**完全没判 wiki** ——
// 同一个结果在插件里是「⚠️ wiki 未生成」，在系统通知里却是「✅ 已入库」。
// 这正是本项目反复踩的「同一事件多条路径渲染不一致」。
// 现在统一到 background 的 buildIngestOutcome()，且标题常量与 popup 的
// INGEST_TITLE 逐字一致。
const uniBgSrc = fs.readFileSync(path.join(EXT, "background.js"), "utf8");
const uniPopSrc = fs.readFileSync(path.join(EXT, "popup.js"), "utf8");

check("background 提供 buildIngestOutcome（统一结论生成器）",
  /function buildIngestOutcome\(/.test(uniBgSrc), true);
check("notifyClipResult 只转发 buildIngestOutcome 的结论",
  /async function notifyClipResult\(result\) \{\s*const o = buildIngestOutcome\(result\);[\s\S]{0,400}?await notify\(o\.title, o\.body\);/.test(uniBgSrc), true);
check("persistPopupResult 也走 buildIngestOutcome",
  /async function persistPopupResult[\s\S]{0,400}buildIngestOutcome/.test(uniBgSrc), true);
check("  → 不再只看 wiki.status === 'failed'（会漏掉 skipped 等）",
  !/const bad = w\.status === "failed"/.test(uniBgSrc), true);

// 两处 INGEST_TITLE 必须逐字相同 —— 抽出键值对逐个比对
function extractIngestTitles(src) {
  const m = src.match(/const INGEST_TITLE = \{([\s\S]*?)\n\};/);
  if (!m) return null;
  const out = {};
  const re = /(\w+):\s*"([^"]*)"/g;
  let mm;
  while ((mm = re.exec(m[1])) !== null) out[mm[1]] = mm[2];
  return out;
}
const tBg = extractIngestTitles(uniBgSrc);
const tPop = extractIngestTitles(uniPopSrc);
check("两处 INGEST_TITLE 都能解析",
  !!tBg && !!tPop, true);
check("  → 键集合一致",
  tBg && tPop ? Object.keys(tBg).sort().join(",") === Object.keys(tPop).sort().join(",") : false, true);
check("  → 每个标题逐字相同（含 emoji 与标点）",
  tBg && tPop ? JSON.stringify(tBg) === JSON.stringify(tPop) : false, true);
check("wikiBad 标题与用户看到的一致",
  tBg && tBg.wikiBad === "⚠️ raw 已入库，但 wiki 未生成", true);
check("ok 标题与用户看到的一致",
  tBg && tBg.ok === "✅ 入库成功！", true);

// wikiLineText（background）与 wikiLine（popup）必须给出同一套 wiki 说明
check("background 有 wikiLineText，且六种 status 全覆盖",
  /function wikiLineText\(/.test(uniBgSrc) &&
  /status === "ok"[\s\S]{0,600}status === "pending"[\s\S]{0,600}status === "skipped"/.test(uniBgSrc), true);
check("  → demoted 分支区分「素材不完整」与「wiki 失败」",
  /demoteIsContent\(w\)[\s\S]{0,200}未生成（素材不完整：未获取到字幕\/转录）/.test(uniBgSrc), true);
check("background 也补了 demoteIsBlocked（风控拦截）",
  /function demoteIsBlocked\(/.test(uniBgSrc) &&
  /kind === "interstitial"/.test(uniBgSrc), true);

// 飞书是**第四条**到达路径，同样必须判 wiki。过去它只判 demoted，
// 于是 wiki 生成失败时仍推一张绿色「✅ 剪藏成功」卡片。
const uniFbSrc = fs.readFileSync(path.join(TOOLS, "feishu-bot.py"), "utf8");
check("飞书成功路径判 wiki（不再无条件报成功）",
  /wk_status = wk\.get\("status"\)[\s\S]{0,200}wiki_bad = bool\(wk_status\)[\s\S]{0,200}wk_status not in \("ok", "pending"\)/.test(uniFbSrc), true);
check("  → 存在「raw 已入库，但 wiki 未生成」卡片标题",
  /"⚠️ raw 已入库，但 wiki 未生成" if wiki_bad/.test(uniFbSrc), true);
check("  → 两处成功路径都判了 wiki（直接入库 + 浏览器协助回传）",
  (uniFbSrc.match(/wiki_bad = bool\(wk_status\)/g) || []).length === 2, true);
check("  → wiki 未生成时卡片用橙色而非绿色",
  /color="orange" if \(suspect or img_warn or wiki_bad\)[\s\S]{0,90}?else "green"/.test(uniFbSrc), true);

console.log();
console.log("=== ㊺ 任务队列在 popup 内横向展开（弹窗变宽，右栏并排） ===");
// 用户 2026-10-09：在 popup 里点开任务队列「展开的有点突兀」，希望
// **横向拓展原来的弹窗**。既不能向下撑高（会把入库按钮推出可视区），
// 也不该另开窗口（离开了插件本身）。
const manifestSrc5 = fs.readFileSync(path.join(EXT, "manifest.json"), "utf8");
check("展开时 body 宽度变大（Chrome 弹窗尺寸跟随文档）",
  /body\.expanded \{ width: \d+px; \}/.test(popupHtml) &&
  /transition: width/.test(popupHtml), true);
check("  → 未展开时右栏 display:none（不占空间、不改弹窗宽度）",
  /\.col-queue \{[\s\S]{0,300}display: none;/.test(popupHtml) &&
  /body\.expanded \.col-queue \{ display: block; \}/.test(popupHtml), true);
check("  → 左右两栏并排（flex 布局，左栏定宽）",
  /\.columns \{ display: flex;/.test(popupHtml) &&
  /\.col-main \{ width: \d+px; flex: 0 0 auto; \}/.test(popupHtml), true);
check("右栏有指标卡 / 进行中 / 最近记录三块",
  /id="taskStats"/.test(popupHtml) &&
  /id="taskList"/.test(popupHtml) &&
  /id="taskHistory"/.test(popupHtml), true);
check("队列按钮切换展开态（点击切换 body.expanded）",
  /function setQueueExpanded\(open\)/.test(popupSrc) &&
  /document\.body\.classList\.toggle\("expanded", queueExpanded\)/.test(popupSrc) &&
  /queueBtn\.addEventListener\("click", \(\) => \{\s*setQueueExpanded\(!queueExpanded\);/.test(popupSrc), true);
check("  → 展开时立刻渲染一次（不等 3 秒轮询）",
  /if \(queueExpanded\) \{[\s\S]{0,120}refreshTaskPanel\(\)/.test(popupSrc), true);
check("  → 收起时不拉历史（避免每 3 秒白拉 50 条）",
  /if \(queueExpanded\) loadTaskHistory\(\)\.catch/.test(popupSrc), true);
check("  → 不再使用 windows.create（不另开窗口）",
  !/chrome\.windows\.create/.test(popupSrc) &&
  !/getURL\("tasks\.html"\)/.test(popupSrc), true);
check("  → manifest 收回了 tabs 权限（不再需要）",
  !/"tabs"/.test(manifestSrc5), true);
check("  → 独立窗口页面 tasks.html / tasks.js 已删除",
  !fs.existsSync(path.join(EXT, "tasks.html")) &&
  !fs.existsSync(path.join(EXT, "tasks.js")), true);
check("右栏 historyOutcome 与去重判据仍在（区分成功/去重/失败）",
  /function historyOutcome\(t\)/.test(popupSrc) &&
  /r\.duplicate[\s\S]{0,200}已存在（未重复入库）/.test(popupSrc), true);

console.log();
console.log("=== ㊼ URL 归一化必须剥开登录墙包装（同一篇不同链接不得各存一份） ===");
// 用户 2026-10-10 报：同一篇小红书帖子，飞书用短链、插件用长链接，各入库了一份。
// 根因：短链解析后命中未登录风控 → 302 到
// `xiaohongshu.com/login?redirectPath=<编码后的原始地址>`。
// 旧 normalize_url 只认 /explore/ 与 /discovery/item/ 路径，在 /login 上匹配不到
// noteId，于是退化成「整条 URL 当 source_id」，与长链接的 `xiaohongshu:<id>`
// 对不上 → 去重闸门失灵，同内容落两份。
check("新增登录墙解包函数 _xiaohongshu_id_from_login_wall",
  /def _xiaohongshu_id_from_login_wall\(query: str, depth: int = 0\) -> str \| None:/.test(css), true);
check("从小红书登录墙的 redirectPath 里取 noteId",
  /note_id = _xiaohongshu_id_from_login_wall\(parsed\.query\)[\s\S]{0,160}return f"xiaohongshu:\{note_id\}"/.test(css), true);
check("  → redirectPath 会解一次 URL 编码（含多层）",
  /inner = unquote\(raw\)/.test(css), true);
check("  → 递归上限防止构造死循环",
  /if depth > 3:\s*\n\s*return None/.test(css), true);
check("导入里带了 unquote",
  /from urllib\.parse import[^\n]*\bunquote\b/.test(css), true);

console.log();
console.log("=== ㊽ 「图多字少」判据 + 下游 OCR（图片里的文字不得静默丢掉） ===");
// 用户 2026-10-13：小红书 / 微信有人把正文写成图片，文章本身很短。
// 旧行为：图片 markdown（尤其远程 URL）会把字数撑长 → 「文字极少」被掩盖 →
// 照常生成一页基于短文字的**垃圾 wiki**，且零提示。
// 现行为：先剔图数真实文字；命中则交**下游** OCR（有通道时），无通道则如实 skip。
const cmSrc = fs.readFileSync(path.join(TOOLS, "content_measure.py"), "utf8");
check("新增真实文字长度函数 _real_text_length",
  css.includes("def _real_text_length(content: str) -> int:"), true);
check("  → 剔除图片 markdown（URL 不再计入字数）",
  cmSrc.includes("!\\[[^\\]]*\\]\\([^)]*\\)") &&
  /def clean_for_measure[\s\S]{0,600}_IMG_MD\.sub/.test(cmSrc), true);
check("新增「图多字少」判据 _text_in_images_reason",
  css.includes("def _text_in_images_reason(data) -> str:"), true);
check("长度门禁改按**真实文字**判定（不再数含图的字数）",
  css.includes("if real_len < _AUTO_REFINE_MIN_CHARS:") &&
  !css.includes("if len(content.strip()) < _AUTO_REFINE_MIN_CHARS:"), true);
check("「图多字少」闸门排在长度门禁之前（图片会撑长字数，必须先行）",
  css.indexOf("text_in_images = _text_in_images_reason(data)") <
  css.indexOf("if real_len < _AUTO_REFINE_MIN_CHARS:"), true);
check("命中后走统一出口 _handle_text_in_images（带上 sync 与 task_id）",
  /return _handle_text_in_images\(filepath, data, text_in_images,\s*\n\s*sync=sync, task_id=task_id\)/.test(css), true);
check("无 OCR 通道时如实记为 skipped（不得静默产垃圾）",
  css.includes("无可用 OCR 通道，跳过 wiki") &&
  css.includes('_record_wiki_skipped(filepath, reason + "，未做 OCR")'), true);
check("有通道则排入下游 OCR 队列（入库链路只排队、不干活）",
  css.includes("已排入 OCR 精加工队列") && css.includes("_queue_ocr(filepath"), true);
check("OCR 惰性导入（不在模块顶层 import，别拖垮启动）",
  css.indexOf("def _handle_text_in_images") >= 0 &&
  css.indexOf("from ocr_images import") > css.indexOf("def _handle_text_in_images") &&
  !/^from ocr_images import/m.test(css), true);
check("OCR 后重跑 wiki 复用既有 wiki 通道",
  /def _after_ocr[\s\S]{0,1400}_queue_wiki_gen\(filepath, data,/.test(css), true);
check("sync 路径同步跑完 OCR+wiki 再返回（不给「界面说已入库、随后又撤回」留缝）",
  /if sync:[\s\S]{0,900}_run_wiki_gen_sync\(filepath\)/.test(css), true);

const ocrSrc = fs.readFileSync(path.join(TOOLS, "ocr_images.py"), "utf8");
check("新建下游模块 ocr_images.py",
  fs.existsSync(path.join(TOOLS, "ocr_images.py")), true);
check("  → 提供 is_available / process_raw / queue_processing",
  ocrSrc.includes("def is_available()") &&
  ocrSrc.includes("def process_raw(") &&
  ocrSrc.includes("def queue_processing("), true);
check("  → 多模态复用 wiki_gen.api_config（Tier 2）",
  ocrSrc.includes("from wiki_gen import api_config"), true);
check("  → 本地后端是可配置命令模板（{input} 被替换）",
  ocrSrc.includes("def _ocr_local(") && ocrSrc.includes('tpl.replace("{input}"'), true);
check("  → OCR 文字回填进笔记（新增/更新单一「图片文字（OCR）」节）",
  ocrSrc.includes('_OCR_SECTION_HEADING = "## 图片文字（OCR）"') &&
  ocrSrc.includes("def refill_note("), true);
check("  → 子进程带 CREATE_NO_WINDOW（Windows 不弹窗）",
  ocrSrc.includes("creationflags=NO_WINDOW"), true);
check("  → auto：多模态需显式 vision 模型，其次本地兜底",
  ocrSrc.includes('if cfg["model"] and _multimodal_ready():') &&
  ocrSrc.includes('_local_ready(cfg["local_command"])'), true);
check("配置样例 ocr-config.example.json 存在",
  fs.existsSync(path.join(TOOLS, "ocr-config.example.json")), true);

console.log();
console.log("=== ㊾ 入库主路径不得被短链解析吊死（去重联网必须有硬上限） ===");
// 2026-10-14：飞书剪小红书，服务端卡在 find_duplicate → resolve_short_link 的
// TLS 握手里 10+ 分钟（socket 进 CLOSE_WAIT），整个剪藏请求连同飞书回调一起被
// 吊死 —— 用户侧表现为「机器人毫无反应」，且 bot 回报卡片也发不出去。
check("resolve_short_link 有守护线程硬性墙钟上限",
  css.includes("def _resolve_short_link_bounded(") &&
  css.includes("t.join(timeout + 2)"), true);
check("  → 解析结果有缓存（同一 URL 只解析一次）",
  css.includes("_RESOLVE_CACHE") && css.includes("_RESOLVE_CACHE_LOCK"), true);
check("  → 超时的兜底结果不写入缓存（避免永久解析不出来）",
  /if not timed_out:[\s\S]{0,200}_RESOLVE_CACHE\[url\] = resolved/.test(css), true);
check("find_duplicate 的联网慢路径有总时间预算",
  /def find_duplicate\(normalized_url: str, total_budget: float = 20\.0\)/.test(css) &&
  css.includes("time.monotonic() < deadline"), true);
check("  → 超预算时退回直接比对并给出提示（不静默吊死）",
  css.includes("去重联网解析超出"), true);

console.log();
console.log("=== ㊿ 客户端中途断开（WinError 10053）不得被误判为抓取失败 ===");
// 2026-10-14：飞书剪小红书 5Qvl00mjnu0 —— 服务端**已经**完整落盘（8 图 + wiki），
// 但回包阶段客户端早已断开，Windows 抛 ConnectionAbortedError（WinError 10053）。
// 入库主路径的白名单只列了 BrokenPipe/ConnectionReset，于是落进通用分支被记成
// 「后端抓取失败」，进而 requeue_for_browser —— 用户没开浏览器也收到「⏳ 等待浏览器抓取」。
check("入库主路径把 ConnectionAbortedError 同等视为客户端断开",
  /except \(BrokenPipeError, ConnectionResetError, ConnectionAbortedError\):[\s\S]{0,700}客户端已断开，跳过响应/.test(css), true);
check("  → 且按完成处理（complete_task），不降级重抓",
  /客户端已断开，跳过响应[\s\S]{0,400}task_queue\.complete_task\(fetch_task_id/.test(css), true);
check("通用失败分支先判「内容是否已落盘」，已落盘则不再 requeue",
  // 窗口放宽到 1500：2026-10-21 在「已落盘」与 requeue 之间插入了
  // 「不允许降级的平台直接 fail」分支及其注释，原先 600 的窗口会把二者
  // 隔开，于是这条断言误报失败（逻辑其实没变）。
  /except Exception as e:[\s\S]{0,400}if result\.get\("filename"\):[\s\S]{0,700}不重复入队[\s\S]{0,1500}requeue_for_browser\(fetch_task_id\)/.test(css), true);
check("不允许浏览器降级的平台在通用失败分支也不 requeue（飞书 NameError 不再伪装成等待抓取）",
  /allows_browser_fallback is False[\s\S]{0,900}fail_task\(fetch_task_id, reason\)[\s\S]{0,300}return[\s\S]{0,200}requeue_for_browser\(fetch_task_id\)/.test(css), true);
check("浏览器 SSE 断开也把 10053 记作正常断开",
  /except \(BrokenPipeError, ConnectionResetError, ConnectionAbortedError\):[\s\S]{0,420}浏览器已断开连接/.test(css), true);

console.log();
console.log("=== ⓵ 飞书文档不降级浏览器 + 图片缺失不静默 ===");
// 2026-10-14（用户要求）：① 飞书文档缺 lark-cli 时若降级到浏览器，产出的残缺正文会被
// 当成完整剪藏写进 raw/articles/ —— 改为**直接报错**；② 图片下载失败/超上限时只留远程
// 地址，源站一删就没，必须写进 raw 并告知用户，不得静默。
const platformBaseSrc = fs.readFileSync(path.join(TOOLS, "platforms/base.py"), "utf8");
const feishuSrc = fs.readFileSync(path.join(TOOLS, "platforms/feishu.py"), "utf8");
const fbotSrc = fs.readFileSync(path.join(TOOLS, "feishu-bot.py"), "utf8");

check("base.py 定义 NoBrowserFallback（= 拒绝降级的信号）",
  platformBaseSrc.includes("class NoBrowserFallback"), true);
check("feishu.py 一律用 NoBrowserFallback（不再抛普通 Exception）",
  /from \.base import NoBrowserFallback/.test(feishuSrc) &&
  feishuSrc.includes("raise NoBrowserFallback(") &&
  !/raise Exception\(/.test(feishuSrc), true);
check("  → 安装命令指向真实存在的包 @larksuite/cli",
  feishuSrc.includes("@larksuite/cli") && !feishuSrc.includes("@anthropic/lark-cli"), true);
check("clip-server 捕获后直接报错：fail_task + 502，且不入浏览器队列",
  (() => {
    const i = css.indexOf("except NoBrowserFallback as e:");
    if (i < 0) return false;
    const seg = css.slice(i, i + 800);
    return seg.includes("task_queue.fail_task(fetch_task_id") &&
      seg.includes("status=502") &&
      !seg.includes("requeue_for_browser(");
  })(), true);

check("服务端直抓也记下任务原始 URL（风控退回待审区不能用验证页地址）",
  /"_origin_url": url,/.test(css), true);

check("download_images 交出「图片缺失」明细（不再是静默截断/静默失败）",
  /max_images: int = 30,[\s\S]{0,140}\) -> tuple\[str, int, dict\]/.test(css) &&
  css.includes('"truncated": truncated') &&
  css.includes('"failed": len(failed_items)'), true);
check("  → 文案唯一来源 _image_shortfall_note",
  css.includes("def _image_shortfall_note(") && css.includes("图片未全部本地化：共"), true);
check("  → 写进 raw 自动区（clip:auto:end 之前）",
  /def _apply_image_note[\s\S]{0,900}clip:auto:end/.test(css), true);
check("  → 四条入库路径都透传 imageWarning",
  (css.match(/"imageWarning"/g) || []).length >= 4, true);
check("扩展弹窗与系统通知都显示 imageWarning",
  fs.readFileSync(path.join(EXT, "popup.js"), "utf8").includes("result.imageWarning") &&
  fs.readFileSync(path.join(EXT, "background.js"), "utf8").includes("r.imageWarning"), true);
check("飞书卡片显示 imageWarning",
  fbotSrc.includes('result.get("imageWarning")') &&
  fbotSrc.includes('info.get("imageWarning")'), true);

console.log();
console.log("=== ⓶ 字数只能数「真实正文」——脚手架不得替空正文投票 ===");
// 根因（2026-10-14 实测一条小红书图文笔记）：真实正文只有 19 字，剔图后却被算成
// 237（≥200）→「图多字少」闸门漏判 → 照常生成一页垃圾 wiki，8 张图里的文字全丢。
// 撑高它的**全是脚手架**：话题标签 93 字 + 元数据裸 URL 40 字 + 元数据行 28 字
// （`> **链接：**` 这种加粗写法旧正则完全没覆盖）+ 骨架标题 14 字 + 模板尾注 29 字。
check("新增脚手架剥离函数 _clean_for_measure",
  css.includes("def _clean_for_measure(content: str) -> str:"), true);
check("  → 清洗/度量只有一份实现（content_measure.py）",
  cmSrc.includes("def clean_for_measure(content: str) -> str:") &&
  cmSrc.includes("def real_text_length(content: str) -> int:") &&
  cmSrc.includes("def count_words(content: str) -> int:"), true);
check("  → real_text_length / count_words 都走同一条清洗管线",
  /def real_text_length\(content: str\) -> int:[\s\S]{0,300}clean_for_measure\(content\)/.test(cmSrc) &&
  /def count_words\(content: str\) -> int:[\s\S]{0,600}clean_for_measure\(content\)/.test(cmSrc), true);
check("  → clip-server 只做转发（不再各写一份）",
  /def _real_text_length[\s\S]{0,300}content_measure\.real_text_length/.test(css) &&
  /def _count_words[\s\S]{0,300}content_measure\.count_words/.test(css) &&
  !/def _real_text_length[\s\S]{0,600}re\.sub\(r'!\\\[/.test(css), true);
check("  → 下游 ocr_images 复用同一判据（不得再内联一份）",
  ocrSrc.includes("from content_measure import has_text_in_images") &&
  !ocrSrc.includes("def _real_text_len"), true);
check("  → 阈值单一来源（长度门禁与 OCR 候选用同一个数字）",
  cmSrc.includes("MIN_REAL_TEXT_CHARS = 200") &&
  css.includes("_AUTO_REFINE_MIN_CHARS = content_measure.MIN_REAL_TEXT_CHARS"), true);
check("  → 剥掉裸 URL / 话题标签 / 骨架标题 / 元数据标签表",
  cmSrc.includes("https?://") && cmSrc.includes("[话题]#") &&
  cmSrc.includes("SCAFFOLD_HEADINGS = (") && cmSrc.includes("META_LABELS = ("), true);
check("  → 元数据正则覆盖加粗写法（\\*{0,2}）",
  cmSrc.includes("\\*{0,2}"), true);
check("OCR 回填后刷新字数（补完内容不得留旧数字）",
  /def _refresh_word_count[\s\S]{0,900}word_count: \{n\}/.test(ocrSrc) &&
  /def refill_note[\s\S]{0,1600}_refresh_word_count\(new\)/.test(ocrSrc), true);
check("  → 刷新口径与门禁同源（content_measure.count_words）",
  /def _refresh_word_count[\s\S]{0,400}from content_measure import count_words/.test(ocrSrc), true);
check("raw 的 word_count 改用真实口径（不再含图片 URL 的英文片段）",
  /word_count = _count_words\(content\)/.test(css), true);
check("去重比较对旧文件也按同口径重算（不信任历史 frontmatter 的虚高值）",
  /def _extract_word_count\(content: str\) -> int:[\s\S]{0,700}return _count_words\(content\)/.test(css), true);

console.log();
console.log("=== ⓷ 「新版更完整」不得自动覆盖：必须 409 + 强制覆盖按钮 ===");
// 旧行为：new_wc > old_wc*1.3 就 `_delete_article_with_images()` 删旧写新 ——
// 用户看不到任何提示，且删旧会连带丢掉旧文件里的 `## 我的笔记`（用户手写内容）。
check("_preflight_duplicate 不再有 1.3 倍静默自动覆盖分支",
  !/if new_wc > old_wc \* 1\.3 and not is_live:/.test(css), true);
check("  → _overwrite 只可能来自 force（用户已确认）",
  /if force:[\s\S]{0,300}"_overwrite": True,/.test(css), true);
check("  → 命中去重统一返回 409 且带 newer_more_complete",
  /"newer_more_complete": newer,/.test(css), true);
check("  → 409 的消息指引用户点「强制覆盖」",
  css.includes("可能更完整。如需替换请点「强制覆盖」"), true);
check("服务端把 newer_more_complete 透传给浏览器协助路径",
  /"newer_more_complete": gate\.get\("newer_more_complete", False\)/.test(css), true);
check("飞书重复卡片会说明「本次更完整，可点强制覆盖」",
  fbotSrc.includes('result.get("newer_more_complete")') &&
  fbotSrc.includes('info.get("newer_more_complete")'), true);

console.log();
console.log("=== ⓸ 影响 raw 完整性的提示必须四条通知路径齐备 ===");
// 「图片没全本地化」已四路齐全（见 ⓵）；「服务端只拿到静态骨架」此前**只有飞书
// 卡片会说**，插件弹窗与系统通知静默 —— 用户以为库里那份是完整的。
const bgSrcDynamic = fs.readFileSync(path.join(EXT, "background.js"), "utf8");
check("扩展弹窗显示 dynamicRenderNote",
  pj.includes("result.dynamicRenderNote"), true);
check("系统通知显示 dynamicRenderNote",
  bgSrcDynamic.includes("r.dynamicRenderNote"), true);
check("飞书卡片显示 dynamicRenderNote",
  fbotSrc.includes('result.get("dynamicRenderNote")') &&
  fbotSrc.includes('info.get("dynamicRenderNote")'), true);

console.log();
console.log("=== ⓹ 后台加工（wiki / OCR）落定后的二次通知 ===");
// 背景：raw 落盘那一刻 wiki / OCR 还在后台线程里跑，任务只能以
// `result.asyncPending=True` 收尾。加工结束后必须把结论回写到任务记录，
// 再由前端补发一条通知 —— 否则用户永远停在「⏳ 后台生成中」。
// （2026-10-14 用户报：OCR 排队路径跑完没有任何通知。）

check("task_queue 提供 settle_async_result（合并结论 + 清 asyncPending）",
  /def settle_async_result\(self, task_id: str, patch: dict\)[\s\S]{0,1200}current\["asyncPending"\] = False/.test(tqSrc), true);

check("clip-server 定义 _settle_task_async 作为唯一回写出口",
  cs.includes("def _settle_task_async(task_id, filepath, wiki):"), true);
check("  → demoted（raw 已撤回）时清掉文件字段",
  /demoted = wiki\.get\("status"\) == "demoted"[\s\S]{0,2000}if demoted:\s*\n\s*patch\["filename"\] = ""/.test(cs), true);

check("_after_wiki 在所有出口回写（成功 / 无通道 / 撤回）",
  (cs.match(/_settle_task_async\(task_id, filepath/g) || []).length >= 4, true);
check("  → _after_wiki 接收 task_id",
  cs.includes("def _after_wiki(filepath, data, result, task_id=None):"), true);
check("  → _after_ocr 接收并透传 task_id",
  cs.includes("def _after_ocr(filepath, data, result, task_id=None):") &&
  cs.includes("on_complete=lambda w: _after_wiki(filepath, data, w, task_id)"), true);
check("  → _handle_text_in_images 接收 task_id 并交给 OCR 回调",
  /def _handle_text_in_images\(filepath, data, reason: str, sync: bool = False,\s*\n\s*task_id=None\)/.test(cs) &&
  cs.includes("lambda r: _after_ocr(filepath, data, r, task_id)"), true);
check("  → _maybe_queue_wiki_gen 接收并透传 task_id",
  cs.includes("def _maybe_queue_wiki_gen(filepath, data, sync: bool = False, task_id=None)") &&
  cs.includes("on_complete=lambda r: _after_wiki(filepath, data, r, task_id)"), true);

check("入库收尾标出 asyncPending（直抓 / 断连 / 收尾失败 / 浏览器）",
  (cs.match(/"asyncPending"/g) || []).length >= 3, true);
check("直抓响应带 taskId（调用方据此轮询落定）",
  cs.includes('result["taskId"] = fetch_task_id'), true);

check("飞书 poll_task_status 支持 wait_settled",
  fb.includes("wait_settled: bool = False") &&
  fb.includes('not (data.get("result") or {}).get("asyncPending")'), true);
check("飞书提供 _start_settled_notify 二次通知",
  fb.includes("def _start_settled_notify(client, chat_id: str, task_id: str, url: str,"), true);
check("直抓成功且 wiki 仍 pending → 启动二次通知",
  /if wk_status == "pending" and tid:\s*\n\s*_start_settled_notify\(client, chat_id, tid, url/.test(fb), true);
check("浏览器协助路径同样等到落定",
  fb.includes("poll_task_status(task_id, wait_settled=True)"), true);
check("二次通知覆盖「未完成 / 未生成 / 完成」三种结论",
  fb.includes("⚠️ 后台加工未完成") &&
  fb.includes("⚠️ raw 已入库，但 wiki 未生成") &&
  fb.includes("✅ 剪藏完成"), true);
check("OCR 的 pending 文案与普通 wiki 分开（等待时长差一个量级）",
  fb.includes("正在提取图片文字（OCR），完成后自动生成"), true);
check("  → 同时说明「N 张图，文字在其中」（单看「约 28 字」会以为抓漏）",
  fb.includes("张（文字在其中，正在提取）"), true);
check("后台加工落定时**重算字数**（OCR 补进的文字必须计入）",
  /def _settle_task_async[\s\S]{0,1600}patch\["wordCount"\] = _count_words\(/.test(cs), true);
check("  → 第二张卡的字数标注「含图片文字」",
  /if ocr:\s*\n\s*_wc \+= "（含图片文字）"/.test(fb), true);

console.log();
console.log("=== ⓺ 导出脚本：扫描范围 = 导出范围，且误报不再压过检出 ===");
// 背景：`--scan` 原先用 rglob 扫描**整个仓库**，而导出只收录 ALLOWED_TOP_LEVEL
// 下的文件 —— 于是 .workbuddy/（工作记忆）、raw/articles/（个人剪藏）、_trash/
// 里的命中全成了假警报，导出次次被误中止，警报因此被无视（那才是真风险）。
// 另有两条正则天生撞正常文本：CSS 类名 `sk-section-title`、
// 英文短语 `sk-driven-…`（"Task-driven" 的单词边界）、文档里的线程 UUID。
const expSrc = fs.readFileSync(path.join(TOOLS, "export_opensource.py"), "utf8");

check("导出范围判据 is_exportable() 只有一份实现",
  expSrc.includes("def is_exportable(rel_parts: tuple[str, ...]) -> bool:"), true);
check("  → copy_tree 与 scan_secrets 共用它（≥3 处引用）",
  (expSrc.match(/is_exportable\(/g) || []).length >= 3, true);
check("  → scan_secrets 按它过滤（不再扫整个仓库）",
  /def scan_secrets[\s\S]{0,1200}is_exportable\(parts\)/.test(expSrc), true);

check("SECRET_PATTERNS 为三元组（正则 / 标签 / 复核函数）",
  expSrc.includes("for pattern, label, review in SECRET_PATTERNS:"), true);
check("sk- 规则带复核函数（挡住 CSS 类名与英文短语）",
  expSrc.includes("def _is_real_api_key(snippet: str) -> bool:") &&
  expSrc.includes("lambda s, line: _is_real_api_key(s)"), true);
check("  → 判据：长度 ≥20 + 含数字 + 连字符 ≤3",
  /len\(body\) < 20[\s\S]{0,220}isdigit\(\)[\s\S]{0,140}body\.count\("-"\) <= 3/.test(expSrc), true);
check("UUID 只在凭据行才报（文档里的线程 ID 不再误报）",
  expSrc.includes("def _uuid_in_credential_line(") &&
  /_CREDENTIAL_WORD_RE = re\.compile/.test(expSrc) &&
  expSrc.includes('"UUID 形态密钥", _uuid_in_credential_line)'), true);

console.log();
console.log("=== ⓻ OCR 的「默认自动提取 + 主动叫停」 ===");
// 决策依据：两种「不问就不跑」的设计都会漏（事前确认与事后按钮都默认不跑）。
// 正确形态 = **默认自动提取**（忘了选也不会漏）+ 剪藏那一刻可主动叫停。
// 「叫停」必须在剪藏瞬间：OCR 在入库后即刻排队，用户看到卡片时往往已在跑，
// 那时再点「跳过」只能白花成本。
check("服务端：ocr === false → 如实记 skipped（绝不留 pending）",
  /if data\.get\("ocr"\) is False:[\s\S]{0,400}_record_wiki_skipped\(filepath, reason \+ "，本次选择跳过图片文字提取"\)/.test(cs), true);
check("  → /api/clip-url 透传 skip_ocr（body 与适配器 data 不在一处）",
  cs.includes('skip_ocr=(data.get("ocr") is False)') &&
  /if skip_ocr:[\s\S]{0,140}data\["ocr"\] = False/.test(cs), true);

check("飞书：前缀解析抽成独立纯函数（可单测）",
  fb.includes("def parse_leading_commands(text: str) -> tuple[str, dict]:") &&
  fb.includes("_LEADING_COMMANDS = ("), true);
check("  → 认「不OCR / noocr」，且保留「跟踪」",
  /\(\("跟踪", "追踪"\), "live_source"\)[\s\S]{0,90}\(\(\"不ocr\", \"noocr\"\), \"skip_ocr\"\)/.test(fb), true);
check("  → skip_ocr 一路传到请求体",
  fb.includes('skip_ocr = opts["skip_ocr"]') &&
  fb.includes("skip_ocr=skip_ocr") &&
  fb.includes("if not ocr:") && fb.includes('payload["ocr"] = False'), true);

const htmlSrc = fs.readFileSync(path.join(EXT, "popup.html"), "utf8");
check("扩展：勾选框 ocrImages **默认勾选**（= 忘了选也不会漏）",
  /<input type="checkbox" id="ocrImages" checked>/.test(htmlSrc), true);
check("  → 三处剪藏 payload 都带 ocr 字段",
  (pj.match(/ocr: ocrImagesCheckbox\.checked|payload\.ocr = ocrImagesCheckbox\.checked/g) || []).length >= 3, true);
check("  → 记住上次选择（未设置过 = 勾选）",
  pj.includes("ocrImagesDefault") && pj.includes("result.ocrImagesDefault === false"), true);

console.log();
console.log("=== 飞书消息幂等 + 事件回调异步化（防重投） ===");
// 背景：飞书长连接对「未及时 ACK」的事件会**重投**。实测 2026-10-14：同一条
// 小红书分享在 09:39 / 15:05 / 17:11 被投了三次，用户莫名收到「📌 已存在」卡片
// （若是新链接还会被重复抓取）。两处必须一起修：
//   ① 按 message_id 幂等；② 回调只派发、不干活，让 SDK 及时 ACK。
check("按 message_id 幂等（而非 URL —— 用户可能真的又发一次）",
  fb.includes("def _is_replayed_message(message_id: str) -> bool:") &&
  fb.includes("_handled_msgs_lock"), true);
check("  → 窗口 24 小时（URL 级去重只有 5 分钟，挡不住跨小时重投）",
  fb.includes("_HANDLED_MSG_TTL = 24 * 3600"), true);
check("  → 闸门在解析内容之前（指令类消息也受保护）",
  /if _is_replayed_message\(message_id\):[\s\S]{0,240}return/.test(fb), true);
check("  → 诊断日志带 message_id（便于核对重投）",
  fb.includes("收到消息事件: msg={message.message_id}"), true);
check("  → 幂等状态**落盘**（重投恰恰跨重启发生，只放内存等于白做）",
  fb.includes("_HANDLED_MSGS_LOG") && fb.includes("def _load_handled_msgs()") &&
  fb.includes("_handled_msgs: dict[str, float] = _load_handled_msgs()"), true);
check("事件回调只派发：URL 处理放后台线程（及时 ACK）",
  fb.includes('name="feishu-clip"') &&
  /def _run_clips\(\):[\s\S]{0,420}process_url\(lark_client, chat_id, u, message_id/.test(fb), true);
check("  → 线程内自捕异常（否则会静默丢）",
  /def _run_clips\(\):[\s\S]{0,520}except Exception as exc:/.test(fb), true);

console.log();
console.log("=== 飞书单实例锁 + 守护进程探活修复 ===");
// 背景：守护进程「拉起后 3 秒探活」在网络抖动时误判（实测 feishu-bot 要 174 秒
// 才连上飞书），于是同一条被连拉三个实例、全部持有飞书长连接 —— 飞书会把同一条
// 消息**轮询投递**到不同连接，用户因此收到重复卡片（2026-10-14 报「我没剪藏却
// 收到消息」）。两处一起修：① 服务自带单实例锁；② 探活换成可靠的系统 API。
check("feishu-bot 有单实例锁（重复连接会让消息被重复投递）",
  fb.includes("def acquire_single_instance() -> bool:") &&
  fb.includes("_LOCK_FILE") && fb.includes("feishu-bot.pid"), true);
check("  → 闸门排在连飞书之前",
  /def main\(\):[\s\S]{0,400}if not acquire_single_instance\(\):\s*\n\s*return 0/.test(fb), true);
check("探活用 OpenProcess/GetExitCodeProcess（不 spawn tasklist）",
  fb.includes("PROCESS_QUERY_LIMITED_INFORMATION") &&
  auto.includes("PROCESS_QUERY_LIMITED_INFORMATION"), true);
check("守护进程：拉起前先查实例锁",
  auto.includes("def feishu_bot_alive()") &&
  /running = feishu_bot_alive\(\)/.test(auto), true);
check("  → spawn 后**总是先记 pid**（不再因探活失败就丢弃，那正是堆积的起点）",
  /pids\["feishu-bot"\] = pid\s*\n\s*save_pids\(pids\)/.test(auto), true);

console.log();
console.log("=== 卡片文案：Raw 栏 + Wiki 直接显示页面文件名 ===");
// 用户要求：把「文件」改成「Raw」、「Wiki」后面直接给 wiki 页面文件名 ——
// 有名字即代表已生成，比一句「✅ 已生成」更有信息量，也便于回查。
check("服务端提供 _wiki_ref_for（从 raw 的 wiki_refs 取**权威**值）",
  cs.includes("def _wiki_ref_for(filepath) -> str:"), true);
check("  → 解析前统一换行（Windows 文件是 CRLF，不归一是一条也匹配不到）",
  /def _wiki_ref_for[\s\S]{0,900}replace\("\\r\\n", "\\n"\)/.test(cs), true);
check("  → wikiFile 进入响应与落定回写",
  (cs.match(/"wikiFile"/g) || []).length >= 4, true);
check("飞书第一张卡用 Raw 栏",
  /\("平台", platform or "网页"\),[\s\S]{0,320}\("Raw", str\(result\.get\("filename"/.test(fb), true);
check("飞书第二张卡用 Raw 栏",
  fb.includes('fields.append(("Raw", fname[:50]))'), true);
check("飞书 Wiki 栏显示文件名（取不到才退回状态文案）",
  fb.includes('("Wiki", wiki_file + ("（含图片文字）" if ocr else ""))') &&
  fb.includes('("Wiki", _wf if _wf else "✅ 已生成")'), true);
check("插件：Raw 栏 + wiki 行显示文件名（popup 与 background 同口径）",
  pj.includes('"\\nRaw：" + result.filename') &&
  pj.includes('"\\nwiki：✅ " + (wikiFile || "已生成")') &&
  bg.includes('"Raw：" + (r.filename || r.filepath)') &&
  bg.includes('"wiki：✅ " + (wikiFile || "已生成")'), true);

console.log();
console.log("结果: " + ok + " 通过 / " + fail + " 失败");
process.exit(fail ? 1 : 0);

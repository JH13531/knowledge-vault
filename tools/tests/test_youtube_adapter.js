// 离线验证 platforms/youtube.js 的核心逻辑。
// 用真实结构（深嵌套的 ytInitialPlayerResponse + json3 字幕）做 fixture，
// 通过 vm 把适配器跑在一个假的 content-script 环境里。
const vm = require("vm");
const fs = require("fs");
const path = require("path");

const EXT = path.join(__dirname, "..", "clip-extension");
const src = fs.readFileSync(path.join(EXT, "platforms/youtube.js"), "utf8");

let ok = 0, fail = 0;
function check(name, got, want) {
  const good = JSON.stringify(got) === JSON.stringify(want);
  if (good) ok++; else fail++;
  console.log((good ? "  PASS " : "  FAIL ") + name);
  if (!good) {
    console.log("         got : " + JSON.stringify(got));
    console.log("         want: " + JSON.stringify(want));
  }
}
function checkLike(name, hay, needle) {
  check(name, String(hay).includes(needle), true);
}

// ---- 真实结构的 playerResponse（注意是深度嵌套，旧的非贪婪正则会在这里截断）----
const PLAYER = {
  videoDetails: {
    videoId: "vtR7cgYATdk",
    title: "【硬核科普】从零开始认识显卡",
    author: "某频道",
    lengthSeconds: "4325",
    shortDescription: "本期讲讲显卡。\n第二行简介。",
  },
  microformat: {
    playerMicroformatRenderer: { publishDate: "2026-09-01", ownerChannelName: "某频道" },
  },
  captions: {
    playerCaptionsTracklistRenderer: {
      captionTracks: [
        { languageCode: "en", vssId: ".en", baseUrl: "https://www.youtube.com/api/timedtext?v=x&lang=en" },
        { languageCode: "zh-Hans", vssId: "a.zh-Hans", baseUrl: "https://www.youtube.com/api/timedtext?v=x&lang=zh-Hans", kind: "asr" },
        { languageCode: "ja", vssId: ".ja", baseUrl: "https://www.youtube.com/api/timedtext?v=x&lang=ja" },
      ],
    },
  },
};

// 被 PO Token 保护的页面版（YouTube 2025+ 的真实形态）：exp=xpe / potc=1
const PLAYER_POT = {
  videoDetails: { videoId: "vtR7cgYATdk", title: "有字幕但被风控保护的视频", author: "某频道", lengthSeconds: "600", shortDescription: "简介" },
  microformat: { playerMicroformatRenderer: { publishDate: "2026-09-02" } },
  captions: {
    playerCaptionsTracklistRenderer: {
      captionTracks: [
        { languageCode: "zh-Hans", vssId: "a.zh-Hans", kind: "asr",
          baseUrl: "https://www.youtube.com/api/timedtext?v=x&lang=zh-Hans&exp=xpe&potc=1" },
      ],
    },
  },
};

// InnerTube（ANDROID）返回的同一轨：**没有 exp=xpe**，可以直接下载
const INNERTUBE_TRACKS = {
  captions: {
    playerCaptionsTracklistRenderer: {
      captionTracks: [
        { languageCode: "zh-Hans", vssId: "a.zh-Hans", kind: "asr",
          baseUrl: "https://www.youtube.com/api/timedtext?v=x&lang=zh-Hans&c=ANDROID" },
      ],
    },
  },
};

const JSON3 = {
  events: [
    { tStartMs: 3000, segs: [{ utf8: "大家好" }] },
    { tStartMs: 5000, segs: [{ utf8: "欢迎" }, { utf8: "来到" }, { utf8: "本频道" }] },
    { tStartMs: 7500, segs: [{ utf8: "欢迎来到本频道" }] },   // 滚动重复，应被去掉
    { tStartMs: 3661000, segs: [{ utf8: "已经过了一小时" }] },
    { tStartMs: 8000, segs: [] },                              // 空 segs，应跳过
  ],
};

// ---- 极简 DOMParser：只支持 xmlToText 需要的两种形态 ----
function makeDOMParserClass() {
  const parseAttrs = (s) => {
    const o = {};
    const re = /([a-zA-Z:_-]+)\s*=\s*"([^"]*)"/g;
    let m;
    while ((m = re.exec(s))) o[m[1]] = m[2];
    return o;
  };
  return class {
    parseFromString(text) {
      const strip = (s) => String(s).replace(/<[^>]*>/g, "");
      const nodes = [];
      const collect = (tag, re) => {
        let m;
        while ((m = re.exec(text))) {
          nodes.push({ tag, attrs: parseAttrs(m[1]), text: strip(m[2]) });
        }
      };
      collect("p", /<p\b([^>]*)>([\s\S]*?)<\/p>/g);
      collect("text", /<text\b([^>]*)>([\s\S]*?)<\/text>/g);
      return {
        querySelectorAll(sel) {
          return nodes.filter((n) => n.tag === sel).map((n) => ({
            textContent: n.text,
            getAttribute: (k) => (k in n.attrs ? n.attrs[k] : null),
          }));
        },
      };
    }
  };
}

// ---- 路由式 fetch 桩：按 URL 决定返回什么 ----
// routes: [{ match, respond(url, opts) -> {ok,status,text?,json?} }]
function makeFetch(routes, log) {
  return async (url, opts) => {
    const u = String(url);
    if (log) log.push(u);
    for (const r of routes) {
      if (r.match(u)) return r.respond(u, opts);
    }
    return { ok: false, status: 404, text: async () => "", json: async () => ({}) };
  };
}

function makeSandbox({ player = PLAYER, routes = null, fetchImpl = null, withScript = true,
                       pageTitle = "【硬核科普】从零开始认识显卡 - YouTube",
                       visitorData = "CgtURVNUX1ZJU0lUT1I%3D",
                       hasPlayerEl = true } = {}) {
  const scriptText = 'var ytInitialPlayerResponse = ' + JSON.stringify(player) + ';';
  const scripts = withScript ? [
    { textContent: scriptText },
    { textContent: 'ytcfg.set({"INNERTUBE_API_KEY":"AIzaSyTESTKEY_from_page",' +
        '"INNERTUBE_CLIENT_VERSION":"2.2026.01.00",' +
        '"VISITOR_DATA":"' + visitorData + '"});' },
  ] : [];

  const playerEl = { hasAttribute: () => false, getAttribute: () => "" };
  const sandbox = {
    console,
    URL, JSON, Math, String, Array, Object, Number, Boolean, Promise, RegExp, Error,
    Set, Map, parseInt, parseFloat, isNaN, DOMParser: makeDOMParserClass(),
    setTimeout, setInterval, clearInterval, encodeURIComponent, decodeURIComponent,
    location: {
      hostname: "www.youtube.com",
      origin: "https://www.youtube.com",
      href: "https://www.youtube.com/watch?v=vtR7cgYATdk",
    },
    document: {
      title: pageTitle,
      querySelectorAll: (sel) => (sel === "script" ? scripts : []),
      querySelector: (sel) => (hasPlayerEl && /movie_player|ytd-player|html5-main-video/.test(sel)
        ? playerEl : null),
    },
    fetch: fetchImpl || makeFetch(routes || [{ match: () => true, respond: () => ({ ok: true, status: 200, text: async () => "" }) }]),
    window: {},
  };
  // 隔离世界读不到 window.ytcfg，所以**刻意不挂** —— 逼代码走 DOM 兜底路径，
  // 那条路径才是扩展真实运行时能用的（content script 在 isolated world）。
  sandbox.window.__clipVault = {
    base: { cleanZeroWidth: (s) => String(s == null ? "" : s).replace(/[\u200b-\u200f\ufeff]/g, "") },
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox);
  return sandbox.window.__clipVault.platforms.youtube;
}

// 常用的两条路由
const routeInnertube = (tracks, log) => ({
  match: (u) => u.includes("/youtubei/v1/player"),
  respond: (u, opts) => {
    if (log) log.push("INNERTUBE " + ((opts && opts.method) || "GET"));
    return { ok: true, status: 200, json: async () => tracks };
  },
});
const routeInnertubeFail = (status) => ({
  match: (u) => u.includes("/youtubei/v1/player"),
  respond: () => ({ ok: false, status: status, text: async () => "", json: async () => ({}) }),
});
// 复刻真实风控：
//   来自 ANDROID/IOS 客户端的地址（c=ANDROID）能下到内容；
//   网页派生的地址**即使摘掉 exp=xpe** 也一律回 HTTP 200 + 0 字节。
//   （这正是「有字幕却提示没字幕」的现场 —— 空 body 不带任何状态码。）
const routeTimedtextRealistic = (log) => ({
  match: (u) => u.includes("/api/timedtext"),
  respond: (u) => {
    if (u.includes("c=ANDROID")) {
      if (log) log.push("OK <- " + u);
      return { ok: true, status: 200, text: async () => JSON.stringify(JSON3) };
    }
    if (log) log.push("EMPTY-200 <- " + u);
    return { ok: true, status: 200, text: async () => "" };
  },
});
const routeTimedtext = () => ({
  match: (u) => u.includes("/api/timedtext"),
  respond: () => ({ ok: true, status: 200, text: async () => JSON.stringify(JSON3) }),
});
void routeTimedtext;   // 保留给临时调试用

(async () => {
  console.log("=== ① 括号配对取 JSON（旧的非贪婪正则会在这里挂）===");
  let yt = makeSandbox();
  const nested = 'xxx ytInitialPlayerResponse = {"a":{"b":{"c":[1,2,{"d":"}"}]}},"e":"\\"}"};yyy';
  const obj = yt.extractBalancedJson(nested, nested.indexOf("=") + 1);
  check("深层嵌套 + 字符串里的花括号能正确切出", obj,
        { a: { b: { c: [1, 2, { d: "}" }] } }, e: '"}' });
  check("  → 且内含转义引号不被误判", obj.e, '"}');
  check("不存在 { 时返回 null", yt.extractBalancedJson("no braces here", 0), null);

  console.log();
  console.log("=== ② playerResponse 从 <script> 读出来 ===");
  const p = yt.playerResponse();
  check("取到 videoDetails", !!(p && p.videoDetails), true);
  check("  title", p.videoDetails.title, "【硬核科普】从零开始认识显卡");
  check("无 script 时返回 null", makeSandbox({ withScript: false }).playerResponse(), null);

  console.log();
  console.log("=== ③ 字幕轨选择（中文优先 → 英文 → 其它）===");
  const picked = yt.pickCaptionTrack(PLAYER);
  check("选中 zh-Hans", picked.languageCode, "zh-Hans");
  check("全是非中英时选剩下的第一个",
    yt.pickCaptionTrack({ captions: { playerCaptionsTracklistRenderer: { captionTracks: [
      { languageCode: "ja" }, { languageCode: "ko" }] } } }).languageCode, "ja");
  check("没有 captions 时返回 null", yt.pickCaptionTrack({ videoDetails: {} }), null);
  check("captionTracks 为空数组时返回 null",
    yt.pickCaptionTrack({ captions: { playerCaptionsTracklistRenderer: { captionTracks: [] } } }), null);

  console.log();
  console.log("=== ④ json3 → 文本（含重复行去重 / 空 segs 跳过 / 跨小时）===");
  const txt = yt.json3ToText(JSON3);
  check("相邻重复行被去掉（4 个有效事件 → 3 行）", txt.split("\n").length, 3);
  check("  [00:00:03] 大家好", txt.split("\n")[0], "[00:00:03] 大家好");
  check("  segs 拼接去空白", txt.split("\n")[1], "[00:00:05] 欢迎来到本频道");
  check("  跨小时补零", txt.split("\n")[2], "[01:01:01] 已经过了一小时");
  check("空 events 返回空串", yt.json3ToText({ events: [] }), "");

  console.log();
  console.log("=== ⑤ 时长 / 日期 / videoId ===");
  check("4325s → 1:12:05", yt.formatDuration(4325), "1:12:05");
  check("193s → 3:13", yt.formatDuration(193), "3:13");
  check("0 → 空串", yt.formatDuration(0), "");
  check("isoDate 截断", yt.isoDate("2026-09-01T10:00:00Z"), "2026-09-01");
  check("watch?v=", yt.videoId("https://www.youtube.com/watch?v=abc123&t=30s"), "abc123");
  check("youtu.be", yt.videoId("https://youtu.be/xyz789"), "xyz789");
  check("shorts", yt.videoId("https://www.youtube.com/shorts/short01"), "short01");

  console.log();
  console.log("=== ⑥ PO Token 标记识别 ===");
  check("exp=xpe 命中", yt.isPoTokenProtected("https://x/api/timedtext?v=1&exp=xpe&lang=zh"), true);
  check("potc=1 命中", yt.isPoTokenProtected("https://x/api/timedtext?v=1&potc=1"), true);
  check("普通 URL 不命中", yt.isPoTokenProtected("https://x/api/timedtext?v=1&lang=zh"), false);
  check("空值不命中", yt.isPoTokenProtected(""), false);
  check("  exp=xpe 出现在参数值里不算（避免误伤）",
    yt.isPoTokenProtected("https://x/api/timedtext?v=1&x=exp=xpe"), false);

  console.log();
  console.log("=== ⑦ OCR 组装字幕 URL（stripPot / fmt / 保留 lang）===");
  const built = yt.buildCaptionUrl("https://www.youtube.com/api/timedtext?v=x&lang=zh-Hans&exp=xpe&potc=1", { stripPot: true });
  check("摘掉了 exp", built.includes("exp=xpe"), false);
  check("摘掉了 potc", built.includes("potc=1"), false);
  check("保留了 lang", built.includes("lang=zh-Hans"), true);
  check("补上了 fmt=json3", built.includes("fmt=json3"), true);
  const built2 = yt.buildCaptionUrl("https://www.youtube.com/api/timedtext?v=x&lang=en", {});
  check("默认也写 fmt=json3", built2.includes("fmt=json3"), true);
  check("fmt='' 时删掉 fmt（用于取原始响应）",
    yt.buildCaptionUrl("https://www.youtube.com/api/timedtext?v=x&fmt=srv3", { fmt: "" }).includes("fmt="), false);
  check("原有 fmt=srv3 会被改写成 json3，不残留两个 fmt",
    (yt.buildCaptionUrl("https://www.youtube.com/api/timedtext?v=x&fmt=srv3", {}).match(/fmt=/g) || []).length, 1);

  console.log();
  console.log("=== ⑧ XML 兜底：srv3 <p t d> / srv1 <text start> / 双重转义实体 ===");
  const srv3 = '<timedtext><body><p t="1200" d="3000">大家好</p><p t="5000" d="2000">欢迎&amp;#39;来到&amp;#39;本频道</p></body></timedtext>';
  const t3 = yt.xmlToText(srv3);
  check("srv3 解析出 2 行", t3.split("\n").length, 2);
  check("  srv3 时间戳", t3.split("\n")[0], "[00:00:01] 大家好");
  check("  双重转义实体还原成单引号", t3.split("\n")[1], "[00:00:05] 欢迎'来到'本频道");
  const srv1 = '<transcript><text start="2.5" dur="1.5">Hello &amp;lt;world&amp;gt;</text></transcript>';
  check("srv1 解析成一行", yt.xmlToText(srv1), "[00:00:02] Hello <world>");
  check("坏 XML 不抛异常、返回空串", yt.xmlToText("<not xml"), "");
  check("纯文本返回空串", yt.xmlToText("hello"), "");
  check("实体解码幂等（无 & 时原样）", yt.decodeEntities("普通文本"), "普通文本");

  console.log();
  console.log("=== ⑨ 端到端（InnerTube 可用）：走 InnerTube 且不带 exp=xpe ===");
  const log = [];
  yt = makeSandbox({
    routes: [routeInnertube(INNERTUBE_TRACKS, log), routeTimedtextRealistic(log)],
  });
  check("match() 命中", yt.match(), true);
  check("从页面读到 InnerTube key", yt.innertubeApiKey(), "AIzaSyTESTKEY_from_page");
  const r1 = await yt.extractContent("https://www.youtube.com/watch?v=vtR7cgYATdk");
  const tl = log.filter((u) => u.includes("/api/timedtext"));
  check("type=video", r1.type, "video");
  check("platform=YouTube", r1.platform, "YouTube");
  check("extra.hasSubtitles=true", r1.extra.hasSubtitles, true);
  check("extra.subtitleLang=zh-Hans", r1.extra.subtitleLang, "zh-Hans");
  // TVHTML5 吃浏览器 cookie，与登录态一致 → 必须排在 ANDROID 前面优先使用
  check("extra.subtitleSource=InnerTube/TVHTML5（吃 cookie 的客户端优先）",
    r1.extra.subtitleSource, "InnerTube/TVHTML5");
  check("标题去掉了「- YouTube」后缀", r1.title, "【硬核科普】从零开始认识显卡");
  check("含 ## 字幕 章节", r1.content.includes("## 字幕（zh-Hans）"), true);
  check("含时间戳行", r1.content.includes("[00:00:03] 大家好"), true);
  check("含 ## 视频简介", r1.content.includes("## 视频简介"), true);
  check("duration=1:12:05", r1.duration, "1:12:05");
  check("publishedAt", r1.publishedAt, "2026-09-01");
  check("InnerTube 请求是 POST /youtubei/v1/player", log.includes("INNERTUBE POST"), true);
  check("真实下载用的 URL 不含 exp=xpe（否则必回空 body）",
    tl.some((u) => /[?&]exp=xpe(?:&|$)/.test(u)), false);

  console.log();
  console.log("=== ⑩ 核心回归：页面轨被 PO Token 保护，InnerTube 兜住了 ===");
  const log2 = [];
  const ytPot = makeSandbox({
    player: PLAYER_POT,                       // 页面里那条带 exp=xpe
    routes: [routeInnertube(INNERTUBE_TRACKS), routeTimedtextRealistic(log2)],
  });
  const rPot = await ytPot.extractContent("https://www.youtube.com/watch?v=vtR7cgYATdk");
  check("**有字幕**（旧实现这里必为 false）", rPot.extra.hasSubtitles, true);
  check("来源是 InnerTube", rPot.extra.subtitleSource, "InnerTube/TVHTML5");
  check("正文含字幕", rPot.content.includes("[00:00:05] 欢迎来到本频道"), true);
  check("没有出现「未获取到字幕」", rPot.content.includes("未获取到字幕"), false);
  check("  （确认风控地址根本没被拿去请求：挑轨时已被 InnerTube 的同一轨取代）",
    log2.filter((u) => /[?&]exp=xpe(?:&|$)/.test(u)).length, 0);

  console.log();
  console.log("=== ⑪ InnerTube 也失败时：不报错，且 note 说清是 PO Token 风控 ===");
  const ytDead = makeSandbox({
    player: PLAYER_POT,
    routes: [routeInnertubeFail(400), routeTimedtextRealistic([])],
  });
  const rDead = await ytDead.extractContent("https://www.youtube.com/watch?v=vtR7cgYATdk");
  check("hasSubtitles=false", rDead.extra.hasSubtitles, false);
  check("正文有「未获取到字幕」提示", rDead.content.includes("未获取到字幕"), true);
  checkLike("captionNote 点出 PO Token 风控", rDead.extra.captionNote, "PO Token");
  checkLike("captionNote 点出「响应体为空」这个指纹", rDead.extra.captionNote, "响应体为空");
  check("仍然返回标题（供降级到待审区时用）", rDead.title.length > 0, true);

  console.log();
  console.log("=== ⑫ 视频确实没有字幕轨 / 接口 403 也不炸 ===");
  const noCapLog = [];
  const noCap = Object.assign({}, PLAYER, { captions: undefined });
  const ytNoCap = makeSandbox({
    player: noCap,
    routes: [routeInnertube({ playabilityStatus: { status: "OK" } }), routeTimedtextRealistic(noCapLog)],
  });
  const rNoCap = await ytNoCap.extractContent("https://www.youtube.com/watch?v=vtR7cgYATdk");
  check("hasSubtitles=false", rNoCap.extra.hasSubtitles, false);
  checkLike("note 说明可能确实没有字幕", rNoCap.extra.captionNote, "确实没有字幕");
  check("没有任何字幕轨时，一次字幕请求都不发（别把风控额度浪费掉）",
    noCapLog.filter((u) => u.includes("/api/timedtext")).length, 0);

  const yt403 = makeSandbox({
    routes: [routeInnertube(INNERTUBE_TRACKS),
             { match: (u) => u.includes("/api/timedtext"),
               respond: () => ({ ok: false, status: 403, text: async () => "" }) }],
  });
  const r403 = await yt403.extractContent("https://www.youtube.com/watch?v=vtR7cgYATdk");
  check("hasSubtitles=false", r403.extra.hasSubtitles, false);
  checkLike("note 里带 HTTP 403", r403.extra.captionNote, "403");

  // ================================================================
  // ⑬ SPA 未刷新导致「标题张冠李戴」——库里已发生过一次真事故
  //
  // 现场：剪的是《从零开始理解人工智能》(LF9sd-2jCoY)，
  // 但页面是从《从零开始认识显卡》(vtR7cgYATdk) 点进来的、没刷新，
  // 于是 ytInitialPlayerResponse 仍是显卡那条 → raw 被封成「从零开始认识显卡」，
  // 标题/作者/发布日期全错，只有字幕是对的。wiki 因此得出
  // 「raw 标题为显卡，但正文实为 AI 科普」的正确判断。
  // ================================================================
  console.log();
  console.log("=== ⑬ SPA 未刷新：不能用另一条视频的 player 取标题 ===");

  const STALE = Object.assign({}, PLAYER, {
    videoDetails: Object.assign({}, PLAYER.videoDetails, {
      videoId: "vtR7cgYATdk",
      title: "【硬核科普】从零开始认识显卡",
      author: "硬件茶谈",
    }),
  });

  // 页面标题（document.title）反映的是**当前**视频
  const staleSandbox = makeSandbox({
    player: STALE,
    pageTitle: "从零开始理解人工智能 - YouTube",
    routes: [routeInnertube({ playabilityStatus: { status: "OK" } })],
  });
  const rStale = await staleSandbox.extractContent("https://www.youtube.com/watch?v=LF9sd-2jCoY");

  check("标题不再取自另一条视频",
    rStale.title.includes("从零开始认识显卡"), false);
  check("标题退回当前页面标题（正确的那条）",
    rStale.title.includes("从零开始理解人工智能"), true);
  check("作者不再取自另一条视频（硬件茶谈是显卡频道的）",
    rStale.author.includes("硬件茶谈"), false);
  check("发布日期被丢弃（无法确认属于本条视频）",
    rStale.publishedAt, "");
  check("标记了标题来源为页面",
    rStale.extra.titleSource, "page");
  check("标记了 titleMismatch 供上游诊断",
    rStale.extra.titleMismatch, true);
  checkLike("正文里留痕，说明元数据可能缺失",
    rStale.content, "页面未刷新");

  // 反向：player 与 URL 一致时，仍要用 player 的完整元数据（别误伤正常路径）
  console.log();
  console.log("=== ⑭ videoId 一致时仍走 player（不误伤正常路径）===");
  const okSandbox = makeSandbox({
    player: PLAYER,   // PLAYER.videoDetails.videoId === "vtR7cgYATdk"
    routes: [routeInnertube(INNERTUBE_TRACKS), routeTimedtext()],
  });
  const rOk = await okSandbox.extractContent("https://www.youtube.com/watch?v=vtR7cgYATdk");
  check("videoId 一致 → 用 player 标题", rOk.title, "【硬核科普】从零开始认识显卡");
  check("videoId 一致 → 用 player 作者", rOk.author, "某频道");
  check("videoId 一致 → 保留发布日期", rOk.publishedAt, "2026-09-01");
  check("videoId 一致 → 不标记错配", rOk.extra.titleMismatch, false);
  check("videoId 一致 → 标题来源为 player", rOk.extra.titleSource, "player");

  console.log();
  console.log("=== ⑮ videoId 缺失的响应一律不可信 ===");
  // 个别改版不带 videoId → 无法证明它属于本条视频 → 不能用它取元数据
  const noIdSandbox = makeSandbox({
    player: { videoDetails: { title: "来路不明的标题", author: "某频道" } },
    pageTitle: "正确的页面标题 - YouTube",
    routes: [routeInnertube({ playabilityStatus: { status: "OK" } })],
  });
  const rNoId = await noIdSandbox.extractContent("https://www.youtube.com/watch?v=vtR7cgYATdk");
  check("无 videoId → 不用它的标题", rNoId.title, "正确的页面标题");
  check("无 videoId → 标记 titleMismatch", rNoId.extra.titleMismatch, true);

  // ================================================================
  // ⑯ 「浏览器明明登录了却一直 LOGIN_REQUIRED」——必须用吃 cookie 的客户端
  //
  // 现场：用户在浏览器里登录了 YouTube，但扩展一直回
  //   ANDROID：无字幕轨（LOGIN_REQUIRED：请登录…）；IOS：同
  // 根因：ANDROID/IOS 是移动 App 身份，只认 OAuth 令牌，**不认浏览器 cookie**；
  //       而扩展在页面里发请求，能带上的只有 cookie → 在它们看来从没登录过。
  // 修法：改用 TVHTML5 / WEB_EMBEDDED（网页 cookie 鉴权，与登录态一致）。
  // ================================================================
  console.log();
  console.log("=== ⑯ 登录态：必须用吃 cookie 的客户端 ===");

  const src = fs.readFileSync(path.join(EXT, "platforms/youtube.js"), "utf8");
  check("客户端列表含 TVHTML5",
    /name: "TVHTML5"/.test(src), true);
  check("TVHTML5 用的是 TVHTML5_SIMPLY_EMBEDDED_PLAYER",
    /clientName: "TVHTML5_SIMPLY_EMBEDDED_PLAYER"/.test(src), true);
  check("TVHTML5 排在 ANDROID 之前（吃 cookie 的优先）",
    src.indexOf('name: "TVHTML5"') < src.indexOf('name: "ANDROID"'), true);
  check("定义了 COOKIE_AUTH_CLIENTS",
    /const COOKIE_AUTH_CLIENTS = \["TVHTML5", "WEB_EMBEDDED"\]/.test(src), true);
  check("只有吃 cookie 的客户端报 LOGIN_REQUIRED 才算「没登录」",
    /if \(isCookieAuth && \/LOGIN_REQUIRED\/\.test\(clientNote\)\)/.test(src), true);

  // 端到端：TVHTML5 说要登录 → 应输出「要求登录」，而不是「该视频可能没字幕」
  const loginSandbox = makeSandbox({
    player: { videoDetails: { videoId: "vtR7cgYATdk" } },   // 页面也没有字幕轨
    routes: [{
      match: (u) => u.includes("/youtubei/v1/player"),
      respond: () => ({
        ok: true, status: 200,
        json: async () => ({ playabilityStatus: { status: "LOGIN_REQUIRED",
          reason: "请登录，以便我们确认你不是聊天机器人" } }),
      }),
    }],
  });
  const rLogin = await loginSandbox.extractContent("https://www.youtube.com/watch?v=vtR7cgYATdk");
  check("hasSubtitles=false", rLogin.extra.hasSubtitles, false);
  check("loggedOut=true", rLogin.extra.loggedOut, true);
  checkLike("note 说「要求登录」而非「视频没字幕」", rLogin.extra.captionNote, "要求登录");
  check("note 不再倒打一耙说「可能确实没有字幕」",
    rLogin.extra.captionNote.includes("可能确实没有字幕"), false);

  // 反向：全是「没有字幕轨」而无一吃 cookie 客户端要求登录 → 仍应是「可能没字幕」
  const reallyNoCap = makeSandbox({
    player: { videoDetails: { videoId: "vtR7cgYATdk" } },
    routes: [{
      match: (u) => u.includes("/youtubei/v1/player"),
      respond: () => ({
        ok: true, status: 200,
        json: async () => ({ playabilityStatus: { status: "OK" } }),   // 正常状态、就是没轨
      }),
    }],
  });
  const rReallyNoCap = await reallyNoCap.extractContent("https://www.youtube.com/watch?v=vtR7cgYATdk");
  check("没要求登录时 loggedOut=false", rReallyNoCap.extra.loggedOut, false);
  checkLike("仍保留「该视频可能确实没有字幕」的判断",
    rReallyNoCap.extra.captionNote, "确实没有字幕");

  // ==================================================================
  // ⑰ 会话上下文：必须把页面的 visitorData 带进 InnerTube 请求
  //
  //    现场：浏览器已登录，但**服务端派发**的剪藏一直 LOGIN_REQUIRED，
  //    而**手动剪藏**（页面已充分初始化）正常。
  //    根因：`/youtubei/v1/player` 判「是不是同一会话」不只看 cookie，
  //    还要 body.context 里的 visitorData 与页面会话对得上。不带它
  //    （或带一个硬编码的），YouTube 会当成陌生上下文 → LOGIN_REQUIRED。
  // ==================================================================
  console.log();
  console.log("=== ⑰ 会话上下文：visitorData 必须随请求发出 ===");

  // 造一个能捕获请求体的 fetch：记录 context 里有没有 visitorData
  const seenBodies = [];
  const captureVisitorData = () => ({
    match: (u) => u.includes("/youtubei/v1/player"),
    respond: (u, opts) => {
      try { seenBodies.push(JSON.parse((opts && opts.body) || "{}")); } catch (e) { /* ignore */ }
      // 只在「带了 visitorData」时才返回字幕轨 —— 复刻真实行为：
      // 不带会话标识的请求会被当成陌生上下文，回 LOGIN_REQUIRED。
      const body = JSON.parse((opts && opts.body) || "{}");
      const hasVisitor = !!(body.context && body.context.visitorData);
      if (hasVisitor) {
        return {
          ok: true, status: 200,
          json: async () => ({ captions: { playerCaptionsTracklistRenderer: { captionTracks: [
            { baseUrl: "https://www.youtube.com/api/timedtext?v=x&lang=zh", languageCode: "zh" },
          ] } } }),
        };
      }
      return {
        ok: true, status: 200,
        json: async () => ({ playabilityStatus: { status: "LOGIN_REQUIRED", reason: "请登录" } }),
      };
    },
  });

  const visitorSandbox = makeSandbox({
    player: { videoDetails: { videoId: "vtR7cgYATdk" } },
    routes: [
      captureVisitorData(),
      { match: (u) => u.includes("/api/timedtext"),
        respond: () => ({ ok: true, status: 200, text: async () => JSON.stringify(JSON3) }) },
    ],
  });
  await visitorSandbox.extractContent("https://www.youtube.com/watch?v=vtR7cgYATdk");

  check("至少发过一次 InnerTube /player 请求", seenBodies.length > 0, true);
  check("请求 context 里带了 visitorData",
    !!(seenBodies[0] && seenBodies[0].context && seenBodies[0].context.visitorData), true);
  check("context.client 里也带了 visitorData",
    !!(seenBodies[0] && seenBodies[0].context && seenBodies[0].context.client &&
       seenBodies[0].context.client.visitorData), true);

  // 静态断言：代码里必须有读 visitorData / ytcfg 的实现，不能只是拼字符串
  checkLike("实现了 ytcfgValue 读取页面 ytcfg", src, "ytcfgValue");
  checkLike("实现了 visitorData 取值", src, "visitorData()");
  checkLike("从 ytcfg 读 VISITOR_DATA", src, "VISITOR_DATA");

  // ==================================================================
  // ⑱ YouTube 必须有 waitForContent：派发的后台标签页需要等播放器就绪
  //
  //    根因之二：服务端派发时扩展新建**后台标签页**（active:false），
  //    只等固定 2s + 1s 滚动就抓。后台页连 ytInitialPlayerResponse 都
  //    可能没注入完，会话更没建立 → LOGIN_REQUIRED。
  //    而手动剪藏时用户已在页面上停留，会话是热的 → 成功。
  // ==================================================================
  console.log();
  console.log("=== ⑱ YouTube 必须实现 waitForContent ===");

  check("youtube 适配器导出 waitForContent",
    typeof visitorSandbox.waitForContent === "function", true);
  checkLike("waitForContent 会检查 ytcfg 就绪", src, "innertubeApiKey()");
  checkLike("waitForContent 会等播放器元素挂载", src, "ytd-player");
  checkLike("waitForContent 会核对 playerResponse 属于本条视频", src, "playerMatches(pr, wantId)");

  // 行为断言：播放器一直不就绪 → 超时返回 ok:false 且带原因
  const noPlayerSandbox = makeSandbox({
    player: { videoDetails: { videoId: "OTHER_VIDEO" } },   // 与当前视频不符
    withScript: true,
  });
  const waited = await noPlayerSandbox.waitForContent(600);
  check("播放器不就绪时 waitForContent 返回 ok=false", waited && waited.ok, false);
  checkLike("超时会给出原因", (waited && waited.reason) || "", "就绪");

  console.log();
  console.log("结果: " + ok + " 通过 / " + fail + " 失败");
  process.exit(fail ? 1 : 0);
})();
// 离线验证 platforms/bilibili.js 的字幕链路。
//
// 重点锁三件事（都是「静默失败、真机上根本看不出原因」的类型）：
//   ① 协议相对地址 `//aisubtitle.hdslb.com/...` 必须被补成 https ——
//      否则 background 的白名单校验用 `new URL(url)` 会抛异常，判成「不在白名单」，
//      字幕永远拿不到，而界面上只显示一句「没拿到字幕」。
//   ② 失败时必须把**具体原因**写进 `extra.captionNote`，供服务端拼给用户看。
//   ③ 成功路径的正文/extra 形状。
const vm = require("vm");
const fs = require("fs");
const path = require("path");

const EXT = path.join(__dirname, "..", "clip-extension");
const src = fs.readFileSync(path.join(EXT, "platforms/bilibili.js"), "utf8");

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

const VIEW = {
  code: 0,
  data: {
    bvid: "BV1VqNt6JEwQ",
    aid: 113800000000000,
    cid: 998877,
    title: "如何建立知识结构，比知识总量重要100倍",
    desc: "这期讲讲知识结构。",
    owner: { name: "某UP主" },
    duration: 611,
    pubdate: 1760000000,
    stat: { view: 123456, danmaku: 456 },
  },
};
const NAV = {
  code: 0,
  data: { wbi_img: { img_url: "https://i0.hdslb.com/bfs/wbi/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png",
                     sub_url: "https://i0.hdslb.com/bfs/wbi/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.png" } },
};

// routes: 数组，[{ match(url), respond(url) }]，按顺序命中
function makeSandbox(routes, log) {
  const scripts = [];   // 真机上 __INITIAL_STATE__ 内联脚本已不存在
  const sandbox = {
    console,
    URL, JSON, Math, String, Array, Object, Number, Boolean, Promise, RegExp, Error,
    Set, Map, Date, parseInt, parseFloat, isNaN,
    setTimeout, setInterval, clearInterval, clearTimeout,
    encodeURIComponent, decodeURIComponent,
    location: {
      hostname: "www.bilibili.com",
      origin: "https://www.bilibili.com",
      href: "https://www.bilibili.com/video/BV1VqNt6JEwQ/",
      pathname: "/video/BV1VqNt6JEwQ/",
    },
    document: {
      title: "如何建立知识结构，比知识总量重要100倍_哔哩哔哩_bilibili",
      querySelector: () => null,
      querySelectorAll: (sel) => (sel === "script" ? scripts : []),
      body: {},
    },
    MutationObserver: class { observe() {} disconnect() {} },
    chrome: {
      runtime: {
        sendMessage: (msg) => {
          if (!msg || msg.action !== "fetch-json") return Promise.resolve(null);
          const u = String(msg.url);
          if (log) log.push(u);
          for (const r of routes) {
            if (r.match(u)) return Promise.resolve(r.respond(u));
          }
          return Promise.resolve({ success: false, error: "地址不在跨域白名单内：" + u });
        },
      },
    },
    window: {},
  };
  sandbox.window.__clipVault = {
    base: {
      cleanZeroWidth: (s) => String(s == null ? "" : s).replace(/[\u200b-\u200f\ufeff]/g, ""),
      elementToMarkdown: () => "",
    },
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox);
  return sandbox.window.__clipVault.platforms.bilibili;
}

// ---- 常用路由 ----
const routeView = () => ({ match: (u) => u.includes("/x/web-interface/view"), respond: () => ({ success: true, status: 200, json: VIEW }) });
const routeNav = () => ({ match: (u) => u.includes("/x/web-interface/nav"), respond: () => ({ success: true, status: 200, json: NAV }) });
const routePlayer = (json) => ({ match: (u) => u.includes("/x/player/wbi/v2"), respond: () => ({ success: true, status: 200, json }) });
const routeSubJson = (body = [{ from: 1.5, content: "大家好" }, { from: 3.25, content: "欢迎来到本频道" }]) =>
  ({ match: (u) => u.includes("aisubtitle") || u.includes("subtitle"), respond: () => ({ success: true, status: 200, json: { body } }) });

const PLAYER_OK = {
  code: 0,
  data: { subtitle: { subtitles: [
    { lan: "ai-zh", subtitle_url: "//aisubtitle.hdslb.com/bfs/ai_subtitle/prod/x.json?auth_key=a1" },
  ] } },
};

(async () => {
  console.log("=== ① 协议相对地址补全（真机上白名单会拒掉未补全的地址）===");
  let b = makeSandbox([routeView()]);
  check("//host/path → https://host/path",
    b.absoluteUrl("//aisubtitle.hdslb.com/bfs/x.json?auth_key=1"),
    "https://aisubtitle.hdslb.com/bfs/x.json?auth_key=1");
  check("已是绝对地址 → 原样",
    b.absoluteUrl("https://aisubtitle.hdslb.com/x.json"),
    "https://aisubtitle.hdslb.com/x.json");
  check("站内相对 → 拼当页 origin",
    b.absoluteUrl("/bfs/x.json"), "https://www.bilibili.com/bfs/x.json");
  check("空值 → 空串", b.absoluteUrl(""), "");
  check("裸域名 → 补 https", b.absoluteUrl("aisubtitle.hdslb.com/x.json"),
    "https://aisubtitle.hdslb.com/x.json");

  console.log();
  console.log("=== ② 成功路径：字幕拿到、且请求的是补全后的地址 ===");
  let log = [];
  b = makeSandbox([routeView(), routeNav(), routePlayer(PLAYER_OK), routeSubJson()], log);
  check("match() 命中", b.match(), true);
  const r1 = await b.extractContent("https://www.bilibili.com/video/BV1VqNt6JEwQ/");
  check("type=video", r1.type, "video");
  check("platform=Bilibili", r1.platform, "Bilibili");
  check("extra.hasSubtitles=true", r1.extra.hasSubtitles, true);
  check("extra.subtitleLang=ai-zh", r1.extra.subtitleLang, "ai-zh");
  check("extra.captionNote 为空（成功时不掺噪音）", r1.extra.captionNote, "");
  check("含 ## 字幕 章节", r1.content.includes("## 字幕（ai-zh）"), true);
  check("时间戳格式 [HH:MM:SS]", r1.content.includes("[00:00:01] 大家好"), true);
  check("标题来自 view 接口", r1.title, "如何建立知识结构，比知识总量重要100倍");
  check("UP主来自 view 接口", r1.author, "某UP主");
  check("cid 进了 extra", r1.extra.cid, "998877");
  check("字幕 JSON 用的是补全后的 https 地址",
    log.some(u => u.startsWith("https://aisubtitle.hdslb.com/bfs/ai_subtitle/prod/x.json")), true);

  console.log();
  console.log("=== ③ 失败原因必须进 captionNote（以前这里什么都没有）===");
  // 该视频没有字幕
  b = makeSandbox([routeView(), routeNav(),
    routePlayer({ code: 0, data: { subtitle: { subtitles: [] } } })]);
  let r = await b.extractContent("https://www.bilibili.com/video/BV1VqNt6JEwQ/");
  check("hasSubtitles=false", r.extra.hasSubtitles, false);
  checkLike("note 说清是「UP 没传、平台也没生成」", r.extra.captionNote, "没有可用字幕");
  checkLike("正文也带上同一句", r.content, "未获取到字幕");

  // 需要登录
  b = makeSandbox([routeView(), routeNav(),
    routePlayer({ code: 0, data: { subtitle: { subtitles: [] }, need_login_subtitle: true } })]);
  r = await b.extractContent("https://www.bilibili.com/video/BV1VqNt6JEwQ/");
  checkLike("note 指向登录态", r.extra.captionNote, "未登录 B 站");

  // view 接口挂了 → 拿不到 cid
  b = makeSandbox([{ match: (u) => u.includes("/x/web-interface/view"),
                     respond: () => ({ success: false, error: "后台未响应" }) }]);
  r = await b.extractContent("https://www.bilibili.com/video/BV1VqNt6JEwQ/");
  check("hasSubtitles=false", r.extra.hasSubtitles, false);
  checkLike("note 点出未能获取 cid", r.extra.captionNote, "未能获取 cid");
  checkLike("  → 并带上 view 接口的真实报错", r.extra.captionNote, "后台未响应");
  check("cid 为空", r.extra.cid, "");

  // WBI 签名接口报错（如 -403）
  b = makeSandbox([routeView(), routeNav(),
    routePlayer({ code: -403, message: "访问权限不足" })]);
  r = await b.extractContent("https://www.bilibili.com/video/BV1VqNt6JEwQ/");
  checkLike("note 带接口 code", r.extra.captionNote, "-403");
  checkLike("  → 也带 message", r.extra.captionNote, "访问权限不足");

  // nav 拿不到 WBI 密钥
  b = makeSandbox([routeView(),
    { match: (u) => u.includes("/x/web-interface/nav"),
      respond: () => ({ success: true, status: 200, json: { code: 0, data: {} } }) }]);
  r = await b.extractContent("https://www.bilibili.com/video/BV1VqNt6JEwQ/");
  checkLike("note 点出 WBI 密钥获取失败", r.extra.captionNote, "WBI");

  console.log();
  console.log("=== ④ 字幕 JSON 解析 ===");
  b = makeSandbox([routeView()]);
  const txt = b.subtitleJsonToText({ body: [
    { from: 0, content: "第一句" },
    { from: 3661, content: "一小时零一秒" },
    { from: 5, content: "   " },              // 空白，应跳过
    { from: 10 },                              // 无 content，应跳过
  ] });
  check("跳过空行，保留 2 行", txt.split("\n").length, 2);
  check("  首行", txt.split("\n")[0], "[00:00:00] 第一句");
  check("  跨小时补零", txt.split("\n")[1], "[01:01:01] 一小时零一秒");

  console.log();
  console.log("=== ⑤ AI 中文字幕标识 `aizh` 也在优先列表里 ===");
  // B 站现在实际返回的 AI 中文字幕 `lan` 是 "aizh"（没有连字符）。
  // 不在 prefer 列表里也不会全挂（有兜底），但「中文优先」就成了碰运气。
  let pickedLang = null;
  b = makeSandbox([routeView(), routeNav(),
    routePlayer({ code: 0, data: { subtitle: { subtitles: [
      { lan: "en", subtitle_url: "//aisubtitle.hdslb.com/bfs/en.json" },
      { lan: "aizh", subtitle_url: "//aisubtitle.hdslb.com/bfs/ai.json" },
    ] } } }),
    { match: (u) => u.includes("aisubtitle"), respond: (u) => {
      pickedLang = u.includes("/ai.json") ? "aizh" : "en";
      return { success: true, status: 200, json: { body: [{ from: 0, content: "中文" }] } };
    } }]);
  r = await b.extractContent("https://www.bilibili.com/video/BV1VqNt6JEwQ/");
  check("选中了 AI 中文字幕而不是第一条英文轨", pickedLang, "aizh");
  check("extra.subtitleLang 记录了实际语言", r.extra.subtitleLang, "aizh");

  console.log();
  console.log("结果: " + ok + " 通过 / " + fail + " 失败");
  process.exit(fail ? 1 : 0);
})();

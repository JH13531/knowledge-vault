// 微信公众号「图片消息」取图去重回归测试。
//
// 复现 2026-10-10 的缺陷：一篇文章只有 5 张图，raw 里却落了 18 个文件。
// 三个成因：
//   ① 代码从**整个内联脚本**里抓 `cdn_url`，把数组之外 appmsg 封面字段
//      的 cdn_url 也收了进来（封面与正文第一张图是同一张）。
//   ② 每个图条目里嵌着 `watermark_info: { cdn_url: … }`（带水印副本），
//      像素与正式版几乎一致但地址不同，按字符串去重挡不住。
//   ③ 每个图条目里还嵌着 `share_cover: { cdn_url: … }`（分享封面），
//      常与该条目自身是同一张图，哈希却不同。
//   ④ 同一张图还有 http/https、`wx_fmt=jpeg|webp`、尺寸段等写法差异。
//
// 这个测试用 jsdom 真实加载 base.js + wechat.js，跑 extractImages()。
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const EXT = path.resolve(__dirname, "..", "clip-extension");
const FILES = [
  "platforms/base.js",
  "platforms/known-sites.js",
  "platforms/feishu.js",
  "platforms/bilibili.js",
  "platforms/youtube.js",
  "platforms/xiaohongshu.js",
  "platforms/wechat.js",
  "platforms/registry.js",
];

let ok = 0, fail = 0;
function check(name, got, want) {
  const pass = got === want;
  console.log(`  ${pass ? "PASS" : "FAIL"} ${name}`);
  if (!pass) console.log(`         got : ${got}\n         want: ${want}`);
  pass ? ok++ : fail++;
}

const PAGE_URL = "https://mp.weixin.qq.com/s/R71rAgkmMFsru7ktGTd7YA";

// ---- 构造页面：3 张正文图 + 1 张封面（在数组外）+ 各种重复写法 ----
// 哈希用可读的短串，wechatImageKey 取的是 bucket/hash 第 2 段，长度无关。
const COVER = "coverHashAAA";
const P1 = "pic1HashBBB";
const P1WM = "pic1WatermarkCCC";
const P1SHARE = "pic1ShareCoverHHH";
const P2 = "pic2HashDDD";
const P2WM = "pic2WatermarkEEE";
const P3 = "pic3HashFFF";
const GIF = "mmbiz.qpic.cn/mmbiz_gif/emojiIconGGG";

const scriptText = `
var appmsgExtInfo = { create_time: '2026-10-09 09:22', cdn_url: 'https://mmbiz.qpic.cn/sz_mmbiz_jpg/${COVER}/0?wx_fmt=jpeg' };
var picture_page_info_list = [
  { cdn_url: 'https://mmbiz.qpic.cn/sz_mmbiz_jpg/${P1}/0?wx_fmt=jpeg',
    width: '1271' * 1, height: '1695' * 1,
    watermark_info: { cdn_url: 'http://mmbiz.qpic.cn/sz_mmbiz_jpg/${P1WM}/0?wx_fmt=jpeg', is_uploader: true, },
    share_cover: { cdn_url: 'https://mmbiz.qpic.cn/sz_mmbiz_jpg/${P1SHARE}/0?wx_fmt=jpeg', crop_info: {}, }, },
  { cdn_url: 'http://mmbiz.qpic.cn/mmbiz_jpg/${P2}/0?wx_fmt=jpeg',
    watermark_info: { cdn_url: 'https://mmbiz.qpic.cn/mmbiz_jpg/${P2WM}/0?wx_fmt=jpeg', is_uploader: true, }, },
  { cdn_url: 'https://mmbiz.qpic.cn/mmbiz_jpg/${P3}/640?wx_fmt=jpeg', },
];
`;

// swiper 里已渲染的是 webp 渲染版（与 P1 同一张图，仅 wx_fmt 不同）
const html = `<!DOCTYPE html><html><head><title>iPhone自带地图还能这样用，旅行规划太省心</title></head>
<body>
<div id="js_article" class="share_content_page">
  <div class="share_content_page_hd">
    <div class="share_media_swiper_content">
      <div id="img_list" class="share_media">
        <div class="swiper_item"><div class="swiper_item_img">
          <img src="https://mmbiz.qpic.cn/sz_mmbiz_jpg/${P1}/0?wx_fmt=webp">
        </div></div>
        <div class="swiper_item"><div class="swiper_item_img">
          <img src="${GIF}">
        </div></div>
      </div>
    </div>
  </div>
  <div class="share_content_page_bd" id="js_base_container">
    <p>谁还在到处扒旅行攻略，iPhone 自带地图加备忘录就能整理自由行行程。</p>
  </div>
</div>
<script>${scriptText}</script>
</body></html>`;

function loadDom() {
  const dom = new JSDOM(html, { url: PAGE_URL, runScripts: "outside-only" });
  const { window } = dom;
  Object.defineProperty(window.HTMLElement.prototype, "innerText", {
    get() { return this.textContent; },
    configurable: true,
  });
  for (const f of FILES) {
    window.eval(fs.readFileSync(path.join(EXT, f), "utf8"));
  }
  return window;
}

console.log("=== 微信「图片消息」取图去重 ===");
const window = loadDom();
const wp = window.__clipVault.platforms.wechat;
const base = window.__clipVault.base;

// ---- 适配器识别 ----
check("wechat 适配器识别该页", !!wp && wp.name, "微信公众号");
check("适配器暴露 extractImages（图片消息取图入口）",
  typeof wp.extractImages, "function");

// ---- 取图结果 ----
const imgs = wp.extractImages();
check("取图张数 = 唯一图片数（3，不是 1 封面 + 3×2 重复）", imgs.length, 3);

const joined = imgs.join("\n");
check("  → 不含水印副本 1", joined.includes(P1WM), false);
check("  → 不含水印副本 2", joined.includes(P2WM), false);
check("  → 不含 share_cover 分享封面副本（与所属条目同图）", joined.includes(P1SHARE), false);
check("  → 不含数组外的封面字段（与正文第一张图重复）", joined.includes(COVER), false);
check("  → 过滤掉 mmbiz_gif 表情图标", joined.includes("mmbiz_gif"), false);

check("  → P1 保留原图（jpeg），webp 渲染版被替换",
  imgs[0].includes(P1) && /wx_fmt=jpeg/.test(imgs[0]) && !/wx_fmt=webp/.test(imgs[0]), true);
check("  → P2 保留（http 写法原样保留）", imgs.some(u => u.includes(P2)), true);
check("  → P3 保留（带尺寸段 640 也可）", imgs.some(u => u.includes(P3)), true);
check("  → 顺序稳定：三张图各出现一次",
  imgs.filter(u => u.includes(P1)).length === 1 &&
  imgs.filter(u => u.includes(P2)).length === 1 &&
  imgs.filter(u => u.includes(P3)).length === 1, true);

// ---- wechatImageKey 归一化语义 ----
console.log();
console.log("=== wechatImageKey：同一张图的多种写法应归一 ===");
const k = (u) => base.wechatImageKey(u);
check("http 与 https 同 key",
  k(`http://mmbiz.qpic.cn/sz_mmbiz_jpg/${P1}/0?wx_fmt=jpeg`),
  k(`https://mmbiz.qpic.cn/sz_mmbiz_jpg/${P1}/0?wx_fmt=jpeg`));
check("wx_fmt=jpeg 与 wx_fmt=webp 同 key",
  k(`https://mmbiz.qpic.cn/sz_mmbiz_jpg/${P1}/0?wx_fmt=jpeg`),
  k(`https://mmbiz.qpic.cn/sz_mmbiz_jpg/${P1}/0?wx_fmt=webp`));
check("尺寸段 0 与 640 同 key",
  k(`https://mmbiz.qpic.cn/sz_mmbiz_jpg/${P1}/0?wx_fmt=jpeg`),
  k(`https://mmbiz.qpic.cn/sz_mmbiz_jpg/${P1}/640?wx_fmt=jpeg`));
check("不同 hash 不同 key（不会误合并两张不同的图）",
  k(`https://mmbiz.qpic.cn/sz_mmbiz_jpg/${P1}/0`) !==
  k(`https://mmbiz.qpic.cn/sz_mmbiz_jpg/${P2}/0`), true);
check("  → key 形如 bucket/hash", k(`https://mmbiz.qpic.cn/sz_mmbiz_jpg/${P1}/0`),
  `sz_mmbiz_jpg/${P1}`);
check("非微信图床返回 null（保持原有精确去重语义）",
  k("https://example.com/a.png?x=1"), null);
check("  → 同名后缀站点也不误判",
  k("https://evil-qpic.cn/a/b"), null);

console.log();
console.log("结果: " + ok + " 通过 / " + fail + " 失败");
process.exit(fail ? 1 : 0);

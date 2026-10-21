// 用 jsdom 真实加载扩展的提取脚本，验证「跨 frame 选优」能否挑出 iframe 正文。
//
// 复现的正是某商户经营平台那类页面：顶层是空壳，正文整块在跨域 iframe 里。
// 这里把两个 frame 的 DOM 各建一份，分别跑 registry.extractContent()，
// 再套用 background.extractAcrossFrames 的选优规则，断言 iframe 胜出。
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

// 在给定 HTML + URL 上跑扩展的提取链，返回提取结果
function extractInFrame(html, url) {
  // runScripts:"outside-only" 让 window.eval 在 window 的词法环境里执行，
  // 否则脚本里的 `window` 会解析不到（脚本按 IIFE 写法直接引用 window）。
  const dom = new JSDOM(html, { url, runScripts: "outside-only" });
  const { window } = dom;
  // jsdom 不实现 innerText（浏览器专有）。扩展的 elementToMarkdown /
  // hasMeaningfulContent 都用它，这里补一个基于 textContent 的等效实现。
  Object.defineProperty(window.HTMLElement.prototype, "innerText", {
    get() { return this.textContent; },
    configurable: true,
  });
  // registry 依赖 window.__clipVault，逐脚本注入
  for (const f of FILES) {
    const code = fs.readFileSync(path.join(EXT, f), "utf8");
    window.eval(code);
  }
  const reg = window.__clipVault.registry;
  const p = reg.detect();
  const data = (p && typeof p.extractContent === "function")
    ? p.extractContent(url)
    : window.__clipVault.base.extractGenericContent(url);
  return data;
}

console.log("=== 跨 frame 正文提取（真实加载扩展脚本） ===");

// ---- 顶层：商户后台套壳页，只有一句「Please enable JavaScript」----
const shellHtml = `<!DOCTYPE html><html><head><title>某商户经营平台</title></head>
<body><div id="app">We're sorry but merchant-platform doesn't work properly
without JavaScript enabled. Please enable it to continue.</div></body></html>`;
const shell = extractInFrame(shellHtml, "https://merchant.example.com/app/merchant-platform/f8323a25ec6a419");

// ---- iframe：真正的帮助中心正文 ----
const bodyText = Array.from({ length: 40 }, (_, i) =>
  `<p>细则第 ${i + 1} 条：星级以大数据为基础，使用商户下全部打星评价，`
  + `剔除无字与作弊数据，反映消费者对商户的满意度。诚信度、时间、质量、`
  + `数量四个因子共同决定最终评定结果。</p>`).join("");
const iframeHtml = `<!DOCTYPE html><html><head><title>帮助中心</title></head>
<body><div class="article-content"><h1>某点评平台星级评定规则</h1>
<div>星级是如何评定的</div>${bodyText}</div></body></html>`;
const iframe = extractInFrame(iframeHtml, "https://h5.example.com/app/vg-pc-platform-merchant-selfhelp/helpCenter.html#/detail?articleId=19");

const norm = s => String(s || "").replace(/\s/g, "").length;
console.log(`  顶层 frame 提取长度: ${norm(shell.content)}`);
console.log(`  iframe  提取长度: ${norm(iframe.content)}`);

check("顶层套壳页提取内容很少（<200 字）", norm(shell.content) < 200, true);
check("iframe 能提取到完整正文（>500 字）", norm(iframe.content) > 500, true);
check("iframe 提取结果含正文关键词「星级」", iframe.content.includes("星级"), true);

// ---- 应用 extractAcrossFrames 的选优规则 ----
const results = [shell, iframe]
  .filter(d => d && typeof d.content === "string")
  .sort((a, b) => norm(b.content) - norm(a.content));
check("选优后胜出的是 iframe（正文最长）", results[0] === iframe, true);
check("胜出结果的正文包含「星级评定」", results[0].content.includes("星级评定"), true);

// ---- 反向验证：普通页面（正文就在顶层）不应被改动行为 ----
const normalHtml = `<!DOCTYPE html><html><head><title>一篇普通文章</title></head>
<body><article><h1>标题</h1>${bodyText}</article></body></html>`;
const normal = extractInFrame(normalHtml, "https://example.com/article");
const normalResults = [normal, shell]
  .filter(d => d && typeof d.content === "string")
  .sort((a, b) => norm(b.content) - norm(a.content));
check("普通页面：顶层胜出（行为与改动前一致）", normalResults[0] === normal, true);

// ---- 回归：导航容器不得压过正文容器（踩过：只入库了 223 字的导航项）----
//
// 现场：页面左侧导航、右侧正文；导航里的列表项被包成几十个 <p>。
// 旧的 scoreElement = text.length/100 + pCount*5，于是导航凭 pCount 拿到高分
// 压过正文（正文若不用 <p> 则 pCount 很小），且旧实现对候选只做「>200 即胜出」、
// 从不与 body 比较 → 正文整块丢失。实测复现：从 1974 字掉到 223 字。
const navItems = Array.from({ length: 45 }, (_, i) => `<p>导航项 ${i + 1}</p>`).join("");
const realBody = Array.from({ length: 40 }, (_, i) =>
  `<div>正文第${i + 1}段：星级评定规则说明，包含诚信度、时间、质量、数量四个因子。</div>`).join("");
const trapHtml = `<!DOCTYPE html><html><head><title>商户经营平台</title></head><body>
  <main><div class="nav">${navItems}</div><div class="hero">欢迎使用商户经营平台</div></main>
  <div class="article-body">${realBody}</div>
</body></html>`;
const trap = extractInFrame(trapHtml, "https://merchant.example.com/app/merchant-platform/xxx");
console.log(`  导航陷阱页提取长度: ${norm(trap.content)}`);
check("导航容器不得压过正文（正文最后一段必须在内）",
  trap.content.includes("正文第40段"), true);
check("提取量接近正文总量（不再只剩导航那点字）", norm(trap.content) > 1000, true);

// ---- 回归：懒加载图片不得被丢弃（踩过：微信原文有图，raw 里一张都没有）----
//
// 现场：微信文章的 <img> 真实地址写在 data-src，src 为空字符串（页面还未
// 触发懒加载）。旧的 elementToMarkdown 只读 img.src → 取到空串 → 图片整段
// 丢失，用户看到「正文文字在，图全没了」。修复：pickImageSrc 依次尝试
// data-src / data-original / … / srcset。
const lazyHtml = `<!DOCTYPE html><html><head><title>微信文章</title></head><body>
  <h1 class="rich_media_title">一篇带懒加载图的微信文章</h1>
  <div id="js_content" class="rich_media_content">
    <p>${"这是正文段落，用于让容器评分选中正文。".repeat(6)}</p>
    <img data-src="https://mmbiz.qpic.cn/mmbiz_png/lazyOne/640" src="" alt="示意图一">
    <p>${"第二段正文继续填充内容以免被过滤。".repeat(6)}</p>
    <img data-original="https://mmbiz.qpic.cn/mmbiz_png/lazyTwo/640" src="" alt="示意图二">
    <img src="" alt="纯占位图无地址">
  </div>
</body></html>`;
const lazy = extractInFrame(lazyHtml, "https://mp.weixin.qq.com/s/R71rAgkmMFsru7ktGTd7YA");
console.log(`  懒加载页提取长度: ${norm(lazy.content)}`);
check("懒加载图地址来自 data-src（不再因 src 为空而丢失）",
  lazy.content.includes("https://mmbiz.qpic.cn/mmbiz_png/lazyOne/640"), true);
check("data-original 也能兜底取到", 
  lazy.content.includes("https://mmbiz.qpic.cn/mmbiz_png/lazyTwo/640"), true);
check("两张图都出现在 markdown 里", lazy.content.includes("![示意图一]") 
  && lazy.content.includes("![示意图二]"), true);
check("无地址的占位图被剔除（不产生 ![](空) ）",
  !/!\[\]\(\s*\)/.test(lazy.content), true);

// ---- 回归：微信「图片消息」（小绿书 / appmsg_type=9）也要抓到图 ----
//
// 现场：这类文章**没有 `#js_content`**，正文与图片都渲染在 swiper 容器里
// （`#img_list > .swiper_item > .swiper_item_img > img`），真实地址来自
// 页面 JS 的 `picture_page_info_list[].cdn_url`。
// 旧实现只查 `#js_content img` 作为图片来源 → 这类页面**一张图都取不到**，
// 用户看到「原文有图，raw 里却没有图片」。这里两种来源各测一遍：
//   ① swiper DOM 已渲染 → 从 img 里取；② DOM 未渲染 → 从内联脚本 cdn_url 兜底。
const imgNoteHtml = `<!DOCTYPE html><html><head><title>图片消息：旅行清单</title></head><body>
  <h1 class="rich_media_title">iPhone自带地图还能这样用</h1>
  <div id="js_article" class="share_content_page">
    <div class="share_content_page_hd" id="js_share_content_page_hd">
      <div class="img_swiper_area">
        <div id="img_swiper" class="share_media_swiper">
          <div class="share_media_swiper_content">
            <div id="img_list" class="share_media">
              <div class="swiper_item"><div class="swiper_item_img">
                <img src="https://mmbiz.qpic.cn/sz_mmbiz_jpg/noteA/0?wx_fmt=jpeg" alt="图解一">
              </div></div>
              <div class="swiper_item"><div class="swiper_item_img">
                <img data-src="https://mmbiz.qpic.cn/sz_mmbiz_jpg/noteB/0?wx_fmt=jpeg" src="" alt="图解二">
              </div></div>
            </div>
          </div>
        </div>
      </div>
    </div>
    <div class="share_content_page_bd" id="js_base_container">
      <p>谁还在到处扒旅行攻略，iPhone自带「地图+备忘录」就能整理自由行行程。</p>
      <p>很多人不知道「指南」这个功能，景点、美食都能丢进去，还能按距离排序。</p>
      <p>这样一份能直接跳转导航的旅行清单就做好了，准备出去玩的建议先收藏。</p>
    </div>
  </div>
  <script>
    var cgiData = { picture_page_info_list: [
      { cdn_url: 'https://mmbiz.qpic.cn/sz_mmbiz_jpg/noteC/0?wx_fmt=jpeg' },
      { cdn_url: 'https://mmbiz.qpic.cn/sz_mmbiz_jpg/noteD/0?wx_fmt=jpeg' }
    ]};
  </script>
</body></html>`;
const imgNote = extractInFrame(imgNoteHtml, "https://mp.weixin.qq.com/s/R71rAgkmMFsru7ktGTd7YA");
console.log(`  图片消息页提取长度: ${norm(imgNote.content)}`);
check("图片消息：正文文本被提取（不再因为没有 #js_content 而整段丢失）",
  imgNote.content.includes("iPhone自带「地图+备忘录」"), true);
check("图片消息：swiper 里已渲染的图被收进 raw",
  imgNote.content.includes("https://mmbiz.qpic.cn/sz_mmbiz_jpg/noteA/0?wx_fmt=jpeg"), true);
check("图片消息：data-src 懒加载图也收得进来",
  imgNote.content.includes("https://mmbiz.qpic.cn/sz_mmbiz_jpg/noteB/0?wx_fmt=jpeg"), true);
check("图片消息：DOM 未渲染时从 picture_page_info_list 的 cdn_url 兜底",
  imgNote.content.includes("https://mmbiz.qpic.cn/sz_mmbiz_jpg/noteC/0?wx_fmt=jpeg"), true);
check("图片消息：URL 参数不被截断（保留 ?wx_fmt=jpeg）",
  !imgNote.content.includes("noteA/0)"), true);

console.log();
console.log(`结果: ${ok} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);

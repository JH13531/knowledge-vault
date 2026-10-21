// platforms/known-sites.js — 已知平台域名（共享模块）
//
// 为什么单独抽一个文件：
//   popup.js（扩展页面上下文）和 content.js（页面 content script 上下文）是
//   **两个互不相同的 JS 作用域**，彼此拿不到对方的变量。而「这是一个已知平台、
//   该走平台化抓取」的判断两边都要用：
//     - popup.js ：「立即完整入库」按钮据此决定走后端 /api/clip-url 还是 DOM 提取
//     - content.js：页面悬浮按钮据此决定走平台脚本抓取还是通用 DOM 提取
//   所以判断逻辑必须放在一个**两边都会加载**的文件里，而不是任一侧内联。
//   （此前它内联在 popup.js 里 —— 这正是悬浮按钮一直拿不到字幕的原因：
//    悬浮按钮在 content script 里，看不到 popup.js 的那份列表。）
//
// 加载方式（两处都要加，见 manifest.json 与 popup.html）：
//   - manifest.json → content_scripts.js   ← 给 content.js 用
//   - popup.html    → <script src=...>     ← 给 popup.js 用
(function () {
  window.__clipVault = window.__clipVault || {};

  // 这些站点的正文往往不在 DOM 里（需要登录态 / 走接口才能拿全），
  // 所以必须交给平台适配器处理，不能只靠 DOM 提取。
  const PLATFORM_DOMAINS = [
    "feishu.cn", "larksuite.com",
    "mp.weixin.qq.com", "weixin.qq.com",
    "bilibili.com", "b23.tv",
    "xiaohongshu.com", "xhslink.com", "xhslink.cn", "xhs.cn",
    "youtube.com", "youtu.be",
    "douyin.com",
    "zhihu.com",
    "twitter.com", "x.com",
  ];

  // 扩展**自己带**页面内适配器（platforms/*.js）的站点。
  // 这些站点优先走页面内抓取：有登录态、不用后端解密密 cookie、不会另开标签页。
  //
  // YouTube 为什么在这里：它的字幕能**在页面里**拿到 —— 页面把
  // `ytInitialPlayerResponse` 内联在 <script> 里（DOM 可读），字幕轨 baseUrl
  // 指向 www.youtube.com/api/timedtext，**与页面同源**，直接 fetch 即可。
  // 之前把它归到「仅服务端」是错的：那要么让后端去抓（本机后端根本出不去），
  // 要么另开一个隐藏标签页去抓（等于白开一个页面）。
  const LOCAL_ADAPTER_DOMAINS = [
    "feishu.cn", "larksuite.com",
    "mp.weixin.qq.com",
    "bilibili.com", "b23.tv",
    "youtube.com", "youtu.be",
    "xiaohongshu.com", "xhslink.com", "xhslink.cn", "xhs.cn",
  ];

  // 扩展**没有**适配器、但**服务端**有专用适配器的站点。
  //
  // 为什么必须单独标出来：这类站点的正文/字幕页面内拿不到（既不在 DOM 里，
  // 扩展也没写页面内适配器），只能靠服务端的适配器去取。
  // 目前为空 —— YouTube 补了页面内适配器之后已移入 LOCAL_ADAPTER_DOMAINS。
  // 将来若新增「只有服务端能抓」的平台，往这里加。
  const SERVER_ONLY_ADAPTER_DOMAINS = [];

  function isKnownPlatform(url) {
    const lower = (url || "").toLowerCase();
    return PLATFORM_DOMAINS.some(d => lower.includes(d));
  }

  function _matchAny(url, domains) {
    const lower = (url || "").toLowerCase();
    return domains.some(d => lower.includes(d));
  }

  function hasLocalAdapter(url) {
    return _matchAny(url, LOCAL_ADAPTER_DOMAINS);
  }

  function needsServerAdapter(url) {
    return _matchAny(url, SERVER_ONLY_ADAPTER_DOMAINS);
  }

  window.__clipVault.PLATFORM_DOMAINS = PLATFORM_DOMAINS;
  window.__clipVault.LOCAL_ADAPTER_DOMAINS = LOCAL_ADAPTER_DOMAINS;
  window.__clipVault.SERVER_ONLY_ADAPTER_DOMAINS = SERVER_ONLY_ADAPTER_DOMAINS;
  window.__clipVault.isKnownPlatform = isKnownPlatform;
  window.__clipVault.hasLocalAdapter = hasLocalAdapter;
  window.__clipVault.needsServerAdapter = needsServerAdapter;
})();

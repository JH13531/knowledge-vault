// platforms/wechat.js — 微信公众号文章适配器
// 注册到 window.__clipVault.platforms.wechat
//
// 注意：微信公众号文章无法从后端直接爬取（微信验证码墙），
// 只能通过浏览器扩展在用户已登录的浏览器中从 DOM 提取。
(function () {
  window.__clipVault = window.__clipVault || {};
  window.__clipVault.platforms = window.__clipVault.platforms || {};
  const base = window.__clipVault.base;

  // 正文容器：普通图文用 #js_content；「图片消息」（小绿书 / appmsg_type=9）
  // 没有 #js_content，内容与图片都渲染在 swiper 容器里。
  const CONTENT_SELECTORS = ["#js_content", ".rich_media_content"];

  // 图片消息（appmsg_type=9）的根容器与正文文本容器。
  //
  // 为什么要单独处理：这类文章**没有 `#js_content`**，页面结构是
  //   #js_article.share_content_page
  //     ├─ .share_content_page_hd → .img_swiper_area → #img_list（图片）
  //     └─ .share_content_page_bd#js_base_container（正文文本，JS 注入）
  // 图片真实地址来自页面 JS 的 `picture_page_info_list[].cdn_url`。
  // 只查 `#js_content img` 的旧实现，在这类页面上**一张图都取不到** ——
  // 用户看到的现象正是「原文有图，raw 里却没有图片」。
  const NOTE_ROOT_SELECTORS = ["#js_article", ".share_content_page"];
  const NOTE_TEXT_SELECTORS = ["#js_base_container", ".share_content_page_bd"];

  // 图片消息的图片容器。
  const SWIPER_CONTAINERS = [
    "#img_list",
    ".share_media_swiper_content",
    "#img_swiper_content",
    ".share_media",
  ];

  // 从内联脚本文本里取出 `picture_page_info_list: [ … ]` 的**数组正文**。
  //
  // 为什么要限定在数组内：同一个脚本里数组之外还有 appmsg 封面字段
  // （`create_time: '…', cdn_url: '…'`），它与正文第一张图往往是同一张。
  // 不限定范围就会把它一起收进来，多出一份重复图。
  //
  // ⚠️ 括号配对必须**跳过字符串字面量**：脚本里的 URL 与文案都带引号，
  // 字符串内容里出现 `[` / `]` 会让朴素的计数法提前错配或永远配不上
  // （实测就是这么退化成「整段文本」的，封面因此被收进来）。
  // 取不到边界时返回 ""，由调用方决定回退策略。
  function grabArray(text, marker) {
    const at = text.indexOf(marker);
    if (at < 0) return "";
    const start = text.indexOf("[", at);
    if (start < 0) return "";
    let depth = 0;
    let quote = "";
    for (let i = start; i < text.length; i++) {
      const ch = text[i];
      if (quote) {
        if (ch === "\\") { i++; continue; }   // 跳过转义字符
        if (ch === quote) quote = "";
        continue;
      }
      if (ch === "'" || ch === '"') { quote = ch; continue; }
      if (ch === "[") depth++;
      else if (ch === "]") {
        depth--;
        if (depth === 0) return text.slice(start, i + 1);
      }
    }
    return "";
  }

  // 取「一个数组元素对象」里**第一个顶层** `cdn_url`（嵌套对象里的不算）。
  //
  // 一个图条目的结构是：
  //   { cdn_url: '本图地址', …, watermark_info: { cdn_url: '带水印副本' },
  //     share_cover: { cdn_url: '分享封面（常与本图是同一张）' } }
  // 嵌套对象里的 cdn_url **全是副本**：`watermark_info` 是带水印版，
  // `share_cover` 是与本图相同的分享封面 —— 它们地址不同、哈希不同，
  // 按字符串去重挡不住，只能靠「只取顶层」从结构上排除。
  function firstTopLevelCdn(elemText) {
    let depth = 0;
    let quote = "";
    for (let i = 0; i < elemText.length; i++) {
      const ch = elemText[i];
      if (quote) {
        if (ch === "\\") { i++; continue; }
        if (ch === quote) quote = "";
        continue;
      }
      if (ch === "'" || ch === '"') { quote = ch; continue; }
      if (ch === "{") { depth++; continue; }
      if (ch === "}") { depth--; continue; }
      if (depth === 1 && elemText.startsWith("cdn_url", i)) {
        const m = elemText.slice(i).match(/^cdn_url\s*:\s*['"]([^'"]+)['"]/);
        if (m) return m[1];
      }
    }
    return "";
  }

  // 把数组正文切成顶层元素，逐个取顶层 cdn_url。
  // 顺序 = 页面图集顺序；不改变阅读顺序。
  function topLevelCdnUrls(arrText) {
    const out = [];
    let depth = 0;
    let quote = "";
    let elemStart = -1;
    for (let i = 0; i < arrText.length; i++) {
      const ch = arrText[i];
      if (quote) {
        if (ch === "\\") { i++; continue; }
        if (ch === quote) quote = "";
        continue;
      }
      if (ch === "'" || ch === '"') { quote = ch; continue; }
      if (ch === "[") { depth++; continue; }
      if (ch === "]") { depth--; continue; }
      if (ch === "{") {
        depth++;
        if (depth === 2) elemStart = i;   // 数组（depth 1）里的元素对象
        continue;
      }
      if (ch === "}") {
        depth--;
        if (depth === 1 && elemStart >= 0) {
          const u = firstTopLevelCdn(arrText.slice(elemStart, i + 1));
          if (u) out.push(u);
          elemStart = -1;
        }
        continue;
      }
    }
    return out;
  }

  const wechat = {
    name: "微信公众号",

    // 检测是否是微信公众号文章
    match() {
      return /mp\.weixin\.qq\.com/i.test(location.hostname);
    },

    // 微信文章可能延迟加载
    skipContentCheck: true,

    // 等待微信文章内容渲染
    waitForContent(timeoutMs) {
      timeoutMs = timeoutMs || 10000;
      return new Promise((resolve) => {
        function checkContent() {
          for (const sel of CONTENT_SELECTORS) {
            const el = document.querySelector(sel);
            if (el && (el.innerText || "").trim().length > 50) {
              return el;
            }
          }
          // 图片消息：没有 #js_content，用 swiper 容器作为正文承载元素。
          for (const sel of SWIPER_CONTAINERS) {
            const el = document.querySelector(sel);
            if (el && el.querySelector("img")) {
              return el;
            }
          }
          return null;
        }

        const found = checkContent();
        if (found) { resolve(found); return; }

        let resolved = false;
        const observer = new MutationObserver(() => {
          const el = checkContent();
          if (el && !resolved) {
            resolved = true;
            observer.disconnect();
            setTimeout(() => resolve(el), 500);
          }
        });

        observer.observe(document.body, { childList: true, subtree: true });

        setTimeout(() => {
          if (!resolved) {
            resolved = true;
            observer.disconnect();
            resolve(checkContent() || document.body);
          }
        }, timeoutMs);
      });
    },

    // 提取公众号名称
    extractAccount() {
      const clean = base.cleanZeroWidth;

      const sels = [
        "#js_name",
        "a#js_name",
        ".rich_media_meta_nickname",
        ".profile_inner .nickname",
      ];
      for (const sel of sels) {
        const el = document.querySelector(sel);
        if (el) {
          const t = clean((el.innerText || "").trim());
          if (t) return t;
        }
      }
      return "";
    },

    // 提取发布时间
    extractPublishTime() {
      const el = document.querySelector("#publish_time");
      if (el) return (el.innerText || "").trim();
      return "";
    },

    // 提取标签
    extractTags() {
      const tags = [];
      document.querySelectorAll("#js_tags .tag_name, .tag_name").forEach(el => {
        const t = (el.innerText || "").trim();
        if (t) tags.push(t);
      });
      return tags;
    },

    // 图片收集（微信文章中的图片）
    //
    // 覆盖两类页面：
    //   ① 普通图文 —— 图片在 `#js_content img` 里；
    //   ② 「图片消息」（小绿书，appmsg_type=9）—— 没有 `#js_content`，
    //      图片在 swiper 容器 `#img_list > .swiper_item > .swiper_item_img > img` 里。
    //      只查 `#js_content img` 会**一张图都取不到**。
    extractImages() {
      const images = [];
      // 归一化 key → 在 images 里的下标。用于「同一张图换了更优写法」时原地替换。
      const at = new Map();

      // 微信会把原图再转一份 webp 给 swiper 渲染；两者是同一张图，应保留原图。
      const isWebp = (u) => /wx_fmt=webp/i.test(u);

      const push = (raw) => {
        let src = String(raw || "").trim();
        if (!src) return;
        // 只收绝对 http(s) 地址
        if (!/^https?:\/\//i.test(src)) return;
        // 过滤微信自带的表情 / 占位小图标，它们不是正文配图
        if (src.includes("mmbiz.qpic.cn/mmbiz_gif")) return;
        if (/res\.wx\.qq\.com/i.test(src)) return;

        // ⚠️ 必须按「归一化 key」去重，**不能按字符串全等**。
        // 微信图床的同一张图会以多种写法出现在页面里（协议 / wx_fmt /
        // 尺寸段不同），只比字符串会把一张图存成多份。
        // 2026-10-10 实测：一篇文章只有 5 张图，raw 里却落了 18 个文件。
        const key = base.wechatImageKey(src) || src;
        if (at.has(key)) {
          const i = at.get(key);
          // 同一张图：优先保留原图（非 webp），webp 只作后备；不改动已定顺序
          if (isWebp(images[i]) && !isWebp(src)) images[i] = src;
          return;
        }
        at.set(key, images.length);
        images.push(src);
      };

      // ① 普通图文的正文图
      document.querySelectorAll("#js_content img").forEach(img => {
        push(base.pickImageSrc(img));
      });

      // ② 图片消息：先收 swiper 里已渲染的 img
      const swiperImgs = document.querySelectorAll(
        "#img_list img, .share_media_swiper_content img, .swiper_item_img img"
      );
      swiperImgs.forEach(img => {
        push(base.pickImageSrc(img));
      });

      // ③ 图片消息兜底：swiper 是异步挂载的，剪藏时可能一张都没插进来，
      //    这时改从页面 JS 的 `picture_page_info_list[].cdn_url` 取地址。
      //
      //    ⚠️ 一张图在脚本里有**三种**写法，收全就会变成 3 份：
      //      a. 数组**外**还有 appmsg 封面字段 `create_time: '…', cdn_url: '…'`；
      //      b. 条目里嵌 `watermark_info: { cdn_url: '…' }`（带水印副本，
      //         与原图像素几乎一致：实测最大差 ~100、平均差 ~1.3）；
      //      c. 条目里还嵌 `share_cover: { cdn_url: '…' }`（分享封面，
      //         常与该条目自身是同一张图）。
      //    三种副本的地址与哈希都不同，**按字符串去重挡不住**。
      //    所以这里不靠「正则全局匹配 + 事后过滤」，改为
      //    「先按括号配对切出每个数组元素，再只取该元素的顶层 cdn_url」——
      //    从结构上一次性排除 a（不在数组内）、b、c（都是嵌套对象）。
      //    与 ② 的重叠由上面的归一化 key 兜住，所以这里可以放心补全，
      //    不需要「只在 ② 为空时才用」这种会漏图的条件判断。
      try {
        const marker = "picture_page_info_list";
        const scripts = document.querySelectorAll("script:not([src])");
        for (const s of scripts) {
          const text = s.textContent || "";
          if (!text.includes(marker)) continue;
          const arrText = grabArray(text, marker);
          // 首选：按元素取顶层 cdn_url
          let urls = arrText ? topLevelCdnUrls(arrText) : [];
          if (!urls.length) {
            // 兜底：数组边界拿不到（或结构异常）时退回全文，
            // 先剔除两种已知的嵌套副本再取 cdn_url（宁可多收，也别漏图）。
            const cleaned = (arrText || text).replace(
              /(?:watermark_info|share_cover)\s*:\s*\{[^{}]*\}/g, "");
            urls = (cleaned.match(/cdn_url\s*:\s*['"]([^'"]+)['"]/g) || [])
              .map(h => (h.match(/cdn_url\s*:\s*['"]([^'"]+)['"]/) || [])[1])
              .filter(Boolean);
          }
          for (const u of urls) push(u);
        }
      } catch (e) { /* 取不到就按已收集的算 */ }

      return images;
    },

    // 噪音元素
    noiseSelectors: [
      "#js_pc_qr_code",
      "#js_mobile_qr_code",
      ".qr_code_pc",
      ".qr_code_wrp",
      ".reward_area",
      ".like_comment_wrp",
      "#content_bottom_area",
      "#temp_num_area",
    ],

    // 主提取函数
    extractContent(url) {
      const clean = base.cleanZeroWidth;

      // 标题
      let title = "";
      const titleEl = document.querySelector("h1.rich_media_title");
      if (titleEl) title = clean(titleEl.innerText.trim());
      if (!title) title = clean(document.title.replace(/-? .*$/, "").trim());

      // 公众号
      const account = wechat.extractAccount();

      // 发布时间
      const publishTime = wechat.extractPublishTime();

      // 正文
      //
      // 普通图文：`#js_content`。图片消息（appmsg_type=9）：没有 `#js_content`，
      // 正文文本渲染在 `#js_base_container` / `.share_content_page_bd`，
      // 图片在 swiper（`#img_list`）。只认 `#js_content` 会让这类文章
      // 的正文与图片整段丢失。
      let content = "";
      const contentEl = document.querySelector("#js_content")
        || document.querySelector(".rich_media_content");
      if (contentEl) {
        content = base.elementToMarkdown(contentEl, wechat.noiseSelectors);
      } else {
        // 图片消息：优先取正文文本容器；取不到再退到整篇根容器。
        const noteTextEl = NOTE_TEXT_SELECTORS
          .map(sel => document.querySelector(sel))
          .find(el => el && (el.innerText || "").trim().length > 0);
        const noteRootEl = noteTextEl
          || NOTE_ROOT_SELECTORS
            .map(sel => document.querySelector(sel))
            .find(Boolean);
        if (noteRootEl) {
          content = base.elementToMarkdown(noteRootEl, wechat.noiseSelectors);
        }
      }

      // 图片
      const images = wechat.extractImages();

      // 标签
      const tags = wechat.extractTags();

      // 组装 Markdown
      const parts = [];
      parts.push("# " + title);
      parts.push("");
      if (account) parts.push("> **公众号：** " + account);
      if (publishTime) parts.push("> **发布时间：** " + publishTime);
      parts.push("> **链接：** " + url);
      parts.push("");

      if (tags.length > 0) {
        parts.push("**标签：** " + tags.map(t => "#" + t).join(" "));
        parts.push("");
      }

      if (content && content.trim().length > 20) {
        parts.push("## 正文");
        parts.push("");
        parts.push(clean(content).trim());
        parts.push("");
      }

      // 图片：`elementToMarkdown(contentEl)` 已把正文里的 <img> 就地转成
      // `![alt](真实地址)` 并写进 content，所以这里**只补正文没覆盖到的图**，
      // 否则同一张图会出现两次（正文一次、末尾「## 图片」再一次）。
      const missing = images.filter(u => !content.includes(u));
      if (missing.length > 0) {
        parts.push("## 图片");
        parts.push("");
        missing.forEach((imgUrl, i) => {
          parts.push("![" + "图片" + (i + 1) + "](" + imgUrl + ")");
        });
        parts.push("");
      }

      if (!content && images.length === 0) {
        parts.push("> ⚠️ 未获取到正文内容，请确保页面已完全加载。");
        parts.push("");
      }

      return {
        title,
        url,
        author: account,
        publishedAt: publishTime,
        content: clean(parts.join("\n")).trim().slice(0, 50000),
        type: "article",
        platform: "微信公众号",
        tags: ["微信公众号", "微信"].concat(tags),
      };
    },
  };

  window.__clipVault.platforms.wechat = wechat;
})();

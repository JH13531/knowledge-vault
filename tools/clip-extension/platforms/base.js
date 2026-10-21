// platforms/base.js — 通用提取工具和 Markdown 转换
// 注册到 window.__clipVault.base
(function () {
  window.__clipVault = window.__clipVault || {};

  const base = {

    // ========== 零宽字符清理 ==========
    //
    // ⚠️ 必须用 \u 转义**逐段写清楚**，不要图省事写成字面量字符区间。
    // 这里踩过一个很贵的坑：原来的写法是 /[-‏‪-⁯﻿᠎͏؜￹-￻]/g，
    // 字符类里**第一个字符就是普通连字符**（U+002D）—— 于是剪藏时
    // 把正文里所有的 `-` 都删掉了：
    //     `router-related-2589621` → `routerrelated2589621`
    //     `state-of-the-art`       → `stateoftheart`
    // 连 URL 一起改坏（2026-10-07 剪的 B 站文章里能看到实例）。
    // 顺带地，那个字面量 `-` 还把本该是 U+200B–U+200F 的**区间**拆成了
    // 「裸 U+200F + 一个连字符」，零宽空格 U+200B–U+200E 就没被清掉。
    //
    // 注意与 `clip-server.py` 的 `slugify()` 保持一致的那部分：那边（Python）
    // 的写法恰好把 `-` 当成了区间连接符，所以一直是对的。
    // 这里**故意不含** U+FE00–U+FE0F（变体选择符）：它不改变宽度，
    // 却是 emoji 呈现的一部分（👨‍👩‍👧 / ⚠️），删掉它就等于改动了 raw 的内容。
    cleanZeroWidth(text) {
      return String(text == null ? "" : text).replace(
        /[\u00AD\u034F\u061C\u17B4\u17B5\u180E\u200B-\u200F\u202A-\u202E\u2060-\u206F\uFEFF\uFFF9-\uFFFB]/g,
        ""
      );
    },

    // 取图片的真实地址（兼容懒加载）。
    //
    // 为什么不能直接用 img.src：懒加载图片的 src 往往是 1×1 占位图或空串，
    // 真实地址另存于 data-src / data-original / data-lazy-src 等属性。
    // 微信公众号、知乎、多数 CMS 都这么做。只读 src 会得到空地址。
    // 返回 "" 表示确实取不到地址（调用方应丢弃该图，不要写成 ![]()）。
    pickImageSrc(img) {
      if (!img) return "";
      const cands = [
        img.getAttribute("data-src"),
        img.getAttribute("data-original"),
        img.getAttribute("data-lazy-src"),
        img.getAttribute("data-lazyload"),
        img.getAttribute("data-echo"),
        img.getAttribute("data-url"),
        img.getAttribute("src"),
        // srcset 的最后一项通常是最大图；没有 src 时兜底用它
        (img.getAttribute("srcset") || "").split(",").pop(),
      ];
      for (let c of cands) {
        if (!c) continue;
        c = String(c).trim().split(/\s+/)[0];   // srcset 项可能带 " 2x"
        // 必须是绝对地址，且不是 1×1 占位图 / data: 内联图
        if (!/^https?:\/\//i.test(c)) continue;
        if (/^data:/i.test(c)) continue;
        return c;
      }
      return "";
    },

    // 微信图床的「同一张图」归一化 key（bucket/hash）。
    //
    // 为什么需要它：微信（mp.weixin.qq.com）会把**同一张图**以多种写法
    // 重复放进页面，只按字符串全等去重必然重复入库：
    //   ① 协议不同     http://…  与  https://…
    //   ② 渲染格式不同  ?wx_fmt=jpeg / ?wx_fmt=webp / ?wx_fmt=png
    //   ③ 尺寸段不同    …/<bucket>/<hash>/0 与 …/<bucket>/<hash>/640
    // 一张图实际只由「bucket + hash」唯一确定。
    // （2026-10-10 实测：一篇文章 5 张图被存成 18 个文件，就是这个原因。）
    //
    // 非微信图床返回 null —— 调用方应回退到原 URL，保持原有的精确去重语义，
    // 以免误合并那些**用查询串区分不同图片**的站点。
    wechatImageKey(raw) {
      let u;
      try {
        u = new URL(String(raw || "").trim(), location.href);
      } catch (e) {
        return null;
      }
      if (!/(^|\.)qpic\.cn$/i.test(u.hostname)) return null;
      const seg = u.pathname.split("/").filter(Boolean);
      if (seg.length < 2) return null;
      return seg[0] + "/" + seg[1];   // bucket/hash
    },

    // ========== 元素评分 ==========
    //
    // 判据是「这个元素实际能贡献多少正文」，而不是「它有多少个 <p> 标签」。
    //
    // 踩过（某商户经营平台）：页面左侧是导航、右侧是正文，导航容器里塞了几十个
    // `<p>`（列表项被包成 p），于是 `pCount * 5` 让**导航**拿到 200+ 分，
    // 压过真正的正文容器，最终入库只有导航那点字（实测复现：正文整块丢失，
    // 只提取到 223 字的导航项）。旧实现还有两个缺陷叠加：
    //   ① 候选只有硬编码的 7 个选择器，正文若挂在 `.article-body` 之类容器上就选不到；
    //   ② 只要某个候选超过 200 分就胜出，**从不与 document.body 比较** ——
    //      于是一个平庸的候选可以名正言顺地丢掉页面大部分内容。
    //
    // 现在改为：用「去掉空白后的可见文本长度」作为主判据（与最终入正文的量一致），
    // 标签数只作微弱的加分（同长度时偏向结构化容器）。句柄拿到的是同一份文本，
    // 因此这个分数可直接与 body 对比，不会再出现「选了个小容器丢内容」。
    scoreElement(el) {
      if (!el) return 0;
      const text = (el.innerText || el.textContent || "");
      const normLen = String(text).replace(/\s/g, "").length;
      // p 计数只做极轻量的偏好（同长度时偏向有段落结构的容器），
      // 不能再让导航里堆砌的 <p> 压过正文。
      const pCount = el.querySelectorAll("p").length;
      return normLen + Math.min(pCount, 40) * 2;
    },

    // ========== DOM → Markdown ==========
    elementToMarkdown(el, platformNoise) {
      const clone = el.cloneNode(true);

      // 移除插件自身的 DOM
      clone.querySelectorAll("#clip-vault-fab").forEach(n => n.remove());

      // 通用移除
      const removeSel = [
        "script", "style", "nav", "footer", "header", "aside",
        ".ad", ".advertisement", ".comment", ".comments", "#comments",
        ".sidebar", "button", "noscript", "iframe",
      ];

      // 平台特有噪音元素
      if (platformNoise) {
        removeSel.push(...platformNoise);
      }

      removeSel.forEach(sel => {
        clone.querySelectorAll(sel).forEach(n => n.remove());
      });

      // 图片
      //
      // **取真实图片地址，不能只看 img.src。** 大量站点（尤以微信公众号为典型）
      // 用懒加载：真正的地址放在 `data-src`，而 `img.src` 是占位图或空串。
      // 旧实现只读 `img.src`，于是懒加载图片会写成 `![]()`（空地址）——
      // 用户看到的现象正是「原文有图，raw 里却没有图片」。这里按常见懒加载
      // 属性依次兜底，任取到第一个可用地址即可。
      clone.querySelectorAll("img").forEach(img => {
        const alt = img.alt || "";
        const src = base.pickImageSrc(img);
        if (!src) {
          // 确实拿不到地址（纯装饰性占位图）→ 直接移除，不留下空 ![]() 噪音
          img.remove();
          return;
        }
        img.replaceWith(document.createTextNode("\n\n![" + alt + "](" + src + ")\n\n"));
      });

      // 链接
      clone.querySelectorAll("a").forEach(a => {
        const href = a.getAttribute("href") || "";
        const text = a.innerText || "";
        if (href && text) {
          a.replaceWith(document.createTextNode("[" + text + "](" + href + ")"));
        }
      });

      // 标题
      for (let i = 1; i <= 6; i++) {
        clone.querySelectorAll("h" + i).forEach(h => {
          const text = h.innerText.trim();
          if (text) {
            h.replaceWith(document.createTextNode("\n\n" + "#".repeat(i) + " " + text + "\n\n"));
          }
        });
      }

      // 段落
      clone.querySelectorAll("p").forEach(p => {
        const text = p.innerText.trim();
        if (text) {
          p.replaceWith(document.createTextNode("\n\n" + text + "\n\n"));
        }
      });

      // 列表
      clone.querySelectorAll("ul, ol").forEach(list => {
        const items = [];
        list.querySelectorAll(":scope > li").forEach((li, idx) => {
          const text = li.innerText.trim();
          if (text) {
            const prefix = list.tagName === "OL" ? (idx + 1) + ". " : "- ";
            items.push(prefix + text);
          }
        });
        list.replaceWith(document.createTextNode("\n" + items.join("\n") + "\n"));
      });

      // 代码块
      clone.querySelectorAll("pre").forEach(code => {
        const text = code.innerText;
        if (text) {
          code.replaceWith(document.createTextNode("\n```\n" + text + "\n```\n"));
        }
      });
      clone.querySelectorAll("code").forEach(code => {
        const text = code.innerText;
        if (text && code.parentElement.tagName !== "PRE") {
          code.replaceWith(document.createTextNode("`" + text + "`"));
        }
      });

      // 引用
      clone.querySelectorAll("blockquote").forEach(bq => {
        const text = bq.innerText.trim();
        if (text) {
          const quoted = text.split("\n").map(l => "> " + l).join("\n");
          bq.replaceWith(document.createTextNode("\n\n" + quoted + "\n\n"));
        }
      });

      let result = clone.innerText || clone.textContent || "";
      result = result.replace(/\n{3,}/g, "\n\n");
      return result;
    },

    // ========== 通用页面提取 ==========
    extractGenericContent(url) {
      const clean = base.cleanZeroWidth;
      const title = clean(document.title || "");
      let author = "";
      let publishedAt = "";

      // 候选容器：先按语义标签找正文，再补常见内容类名，最后**一定带上 body**。
      //
      // 为什么必须带 body：只要带上它，就永远存在一个「不丢内容」的兜底选项 ——
      // 无论上面的候选多离谱，最终都会退化成「整页文本」。旧实现只在候选
      // 全部低于阈值时才用 body，于是「一个刚过阈值的导航容器」可以顶替掉整页。
      const candidates = [
        document.querySelector("article"),
        document.querySelector("main"),
        document.querySelector('[role="main"]'),
        document.querySelector(".article-content"),
        document.querySelector(".post-content"),
        document.querySelector(".article-body"),
        document.querySelector("#content"),
        document.querySelector(".content"),
        // 类名里带 article/post/entry/content 的容器（覆盖改版后换 class 的站点）
        ...Array.from(document.querySelectorAll(
          '[class*="article"],[class*="post-"],[class*="entry-content"],[class*="markdown-body"]'
        )).slice(0, 8),
        document.body,
      ].filter(Boolean);

      let bestEl = null;
      let bestScore = 0;

      for (const el of candidates) {
        const score = base.scoreElement(el);
        if (score > bestScore) {
          bestScore = score;
          bestEl = el;
        }
      }

      // 兜底：候选全为空（理论上不会，因为 body 一定在）时仍用 body。
      if (!bestEl) bestEl = document.body;

      const authorMeta =
        document.querySelector('meta[name="author"]') ||
        document.querySelector('meta[property="article:author"]');
      if (authorMeta) author = authorMeta.content;

      const dateMeta =
        document.querySelector('meta[property="article:published_time"]') ||
        document.querySelector('meta[name="pubdate"]') ||
        document.querySelector("time[datetime]");
      if (dateMeta) {
        publishedAt = (dateMeta.getAttribute("datetime") || dateMeta.content || "").slice(0, 10);
      }

      const content = base.elementToMarkdown(bestEl);

      return {
        title,
        url,
        author,
        publishedAt,
        content: clean(content).trim().slice(0, 50000),
      };
    },
  };

  window.__clipVault.base = base;
})();

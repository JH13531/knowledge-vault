// platforms/xiaohongshu.js — 小红书笔记页适配器
// 注册到 window.__clipVault.platforms.xiaohongshu
(function () {
  window.__clipVault = window.__clipVault || {};
  window.__clipVault.platforms = window.__clipVault.platforms || {};
  const base = window.__clipVault.base;

  const xiaohongshu = {
    name: "小红书",

    // 检测是否是小红书页面
    match() {
      return /xiaohongshu\.com|xhslink\.com|xhslink\.cn|xhs\.cn/i.test(location.hostname);
    },

    // 小红书 SPA，内容可能延迟加载
    skipContentCheck: true,

    // 等待小红书内容渲染完成
    waitForContent(timeoutMs) {
      timeoutMs = timeoutMs || 10000;
      return new Promise((resolve) => {
        const selectors = [
          ".note-content",
          ".content",
          ".note-text",
          "#noteContainer",
          ".note-detail",
          ".note-scroller",
        ];

        function checkContent() {
          for (const sel of selectors) {
            const el = document.querySelector(sel);
            if (el && (el.innerText || "").trim().length > 20) {
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

    // 从 window.__INITIAL_STATE__ 提取数据
    getInitialState() {
      try {
        if (window.__INITIAL_STATE__) {
          return window.__INITIAL_STATE__;
        }
      } catch (e) {}

      try {
        const scripts = document.querySelectorAll("script");
        for (const s of scripts) {
          const text = s.textContent || "";
          if (text.includes("__INITIAL_STATE__")) {
            const match = text.match(/__INITIAL_STATE__\s*=\s*({.*?});?\s*(?:<\/script>|$)/s);
            if (match) {
              try {
                // 小红书的 __INITIAL_STATE__ 可能包含 undefined，替换为 null
                const jsonStr = match[1].replace(/undefined/g, "null");
                return JSON.parse(jsonStr);
              } catch (e) {}
            }
          }
        }
      } catch (e) {}

      return null;
    },

    // 从 state 中找到笔记数据
    findNote(state) {
      if (!state) return null;

      const noteSection = state.note || {};
      const detailMap = noteSection.noteDetailMap || {};

      // 尝试 noteDetailMap
      for (const key of Object.keys(detailMap)) {
        if (key !== "" && detailMap[key] && typeof detailMap[key] === "object") {
          return detailMap[key];
        }
      }

      // 尝试 currentNoteId
      const currentId = noteSection.currentNoteId || "";
      if (currentId && detailMap[currentId]) return detailMap[currentId];

      // 尝试第一个值
      for (const key of Object.keys(detailMap)) {
        if (typeof detailMap[key] === "object") return detailMap[key];
      }

      return null;
    },

    // 提取标题
    extractTitle(note) {
      const clean = base.cleanZeroWidth;

      if (note && note.title) {
        return clean(note.title.trim());
      }

      // 从 DOM 提取
      const sels = [
        ".title",
        ".note-title",
        "h1.title",
        ".note-content .title",
        "title",
      ];
      for (const sel of sels) {
        const el = document.querySelector(sel);
        if (el) {
          const t = clean((el.innerText || el.textContent || "").trim());
          if (t && t.length > 1 && !t.includes("小红书")) return t;
        }
      }
      return clean((document.title || "").replace(/- 小红书.*$/, "").trim());
    },

    // 提取作者
    extractAuthor(note) {
      const clean = base.cleanZeroWidth;

      if (note && note.user && note.user.nickname) {
        return clean(note.user.nickname);
      }

      const sels = [
        ".author-wrapper .username",
        ".username",
        ".author .name",
        ".user-name",
        "a.name",
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

    // 提取正文
    extractDesc(note) {
      const clean = base.cleanZeroWidth;

      if (note && note.desc) {
        return clean(note.desc.trim());
      }

      // 从 DOM 提取
      const sels = [
        ".note-text",
        ".content",
        ".desc",
        ".note-content .text",
        "#detail-desc",
      ];
      for (const sel of sels) {
        const el = document.querySelector(sel);
        if (el) {
          const t = clean((el.innerText || "").trim());
          if (t && t.length > 5) return t;
        }
      }
      return "";
    },

    // 提取图片列表
    extractImages(note) {
      const images = [];

      if (note && note.imageList) {
        for (const img of note.imageList) {
          const url = img.urlDefault || img.url || "";
          if (url) {
            // 去掉缩放参数，取原图
            const cleanUrl = url.replace(/\?.*$/, "");
            images.push(cleanUrl);
          }
        }
      }

      // DOM 回退
      if (images.length === 0) {
        const imgSels = [
          ".swiper-slide img",
          ".note-image-carousel img",
          ".carousel img",
          ".slide-item img",
        ];
        for (const sel of imgSels) {
          document.querySelectorAll(sel).forEach(img => {
            const src = img.src || img.dataset.src || "";
            if (src && src.startsWith("http") && !src.includes("avatar")) {
              images.push(src.replace(/\?.*$/, ""));
            }
          });
          if (images.length > 0) break;
        }
      }

      return [...new Set(images)];
    },

    // 提取视频地址
    extractVideo(note) {
      if (!note || note.type !== "video") return "";

      try {
        const video = note.video || {};
        const media = video.media || {};
        const stream = media.stream || {};

        for (const quality of ["h264", "h265"]) {
          const streams = stream[quality] || [];
          if (streams.length > 0) {
            return streams[0].masterUrl || "";
          }
        }

        // consumer 回退
        const consumer = video.consumer || {};
        return consumer.originVideoKey || "";
      } catch (e) {}

      // DOM 回退
      const videoEl = document.querySelector("video source, video");
      if (videoEl) {
        return videoEl.src || videoEl.querySelector("source")?.src || "";
      }

      return "";
    },

    // 提取标签
    extractTags(note) {
      const tags = [];

      if (note && note.tagList) {
        for (const t of note.tagList.slice(0, 5)) {
          const name = t.name || t.tagName || "";
          if (name) tags.push(name);
        }
      }

      // DOM 补充
      if (tags.length === 0) {
        document.querySelectorAll(".tag, .note-tag, a.tag-link").forEach(el => {
          const t = (el.innerText || "").trim().replace(/^#/, "");
          if (t) tags.push(t);
        });
      }

      return [...new Set(tags)].slice(0, 8);
    },

    // 噪音元素
    noiseSelectors: [
      ".side-bar",
      ".interact-container",
      ".comment",
      ".recommend",
      ".related",
      ".footer",
      ".sticky",
    ],

    // 主提取函数
    extractContent(url) {
      const clean = base.cleanZeroWidth;
      const state = xiaohongshu.getInitialState();
      const note = xiaohongshu.findNote(state);

      const title = xiaohongshu.extractTitle(note);
      const author = xiaohongshu.extractAuthor(note);
      const desc = xiaohongshu.extractDesc(note);
      const images = xiaohongshu.extractImages(note);
      const videoUrl = xiaohongshu.extractVideo(note);
      const tags = xiaohongshu.extractTags(note);

      const noteType = (note && note.type) || "normal";
      const isVideo = noteType === "video";

      // 组装 Markdown
      const parts = [];
      parts.push("# " + title);
      parts.push("");
      parts.push("> **作者：** " + author);
      parts.push("> **类型：** " + (isVideo ? "视频笔记" : "图文笔记"));
      parts.push("> **链接：** " + url);
      parts.push("");

      if (tags.length > 0) {
        parts.push("**标签：** " + tags.map(t => "#" + t).join(" "));
        parts.push("");
      }

      if (desc) {
        parts.push("## 正文");
        parts.push("");
        parts.push(desc);
        parts.push("");
      }

      if (images.length > 0) {
        parts.push("## 图片");
        parts.push("");
        images.forEach((imgUrl, i) => {
          parts.push("![" + "图片" + (i + 1) + "](" + imgUrl + ")");
        });
        parts.push("");
      }

      if (videoUrl) {
        parts.push("## 视频");
        parts.push("");
        parts.push("[视频链接](" + videoUrl + ")");
        parts.push("");
      }

      if (!desc && images.length === 0 && !videoUrl) {
        // 回退：尝试从 DOM 提取可见内容
        const contentSels = [".note-scroller", ".note-detail", ".main-container"];
        for (const sel of contentSels) {
          const el = document.querySelector(sel);
          if (el && (el.innerText || "").trim().length > 50) {
            const domContent = base.elementToMarkdown(el, xiaohongshu.noiseSelectors);
            if (domContent.trim().length > 50) {
              parts.push("## 页面内容");
              parts.push("");
              parts.push(clean(domContent).trim());
              parts.push("");
              break;
            }
          }
        }
      }

      if (!desc && images.length === 0 && !videoUrl) {
        parts.push("> ⚠️ 未获取到正文内容，可能需要登录态或页面反爬。");
        parts.push("");
      }

      return {
        title,
        url,
        author,
        publishedAt: "",
        content: clean(parts.join("\n")).trim().slice(0, 50000),
        type: isVideo ? "video" : "article",
        platform: "小红书",
        tags: ["小红书", isVideo ? "视频" : "图文"].concat(tags),
      };
    },
  };

  window.__clipVault.platforms.xiaohongshu = xiaohongshu;
})();

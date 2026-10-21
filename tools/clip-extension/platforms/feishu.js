// platforms/feishu.js — 飞书文档适配器
// 注册到 window.__clipVault.platforms.feishu
(function () {
  window.__clipVault = window.__clipVault || {};
  window.__clipVault.platforms = window.__clipVault.platforms || {};
  const base = window.__clipVault.base;

  const feishu = {
    name: "feishu",

    // 检测是否是飞书页面
    match() {
      return /feishu\.cn|larksuite\.com/i.test(location.hostname);
    },

    // 飞书 SPA：内容可能延迟加载，跳过初始字数检查
    skipContentCheck: true,

    // 等待飞书正文容器渲染完成
    waitForContent(timeoutMs) {
      timeoutMs = timeoutMs || 12000;
      return new Promise((resolve) => {
        const selectors = [
          "[data-content-editable-root]",
          ".doc-content-container",
          "#docx-content",
          ".doc-content",
          ".suite-markdown-content",
        ];

        function checkContent() {
          for (const sel of selectors) {
            const el = document.querySelector(sel);
            if (el && (el.innerText || "").trim().length > 200) {
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
            setTimeout(() => resolve(el), 800);
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

    // 提取飞书标题
    extractTitle() {
      const clean = base.cleanZeroWidth;
      const titleSels = [
        ".doc-title [data-content-editable-leaf]",
        ".doc-title",
        "[data-record-title] span",
        "h1.doc-title",
        "title",
      ];
      for (const sel of titleSels) {
        const el = document.querySelector(sel);
        if (el) {
          const t = clean((el.innerText || el.textContent || "").trim());
          if (t && t !== "Docs" && t.length > 1) return t;
        }
      }
      return clean((document.title || "").trim());
    },

    // 提取飞书作者
    extractAuthor() {
      const clean = base.cleanZeroWidth;
      const sels = [
        ".doc-header .user-name",
        ".doc-meta .author",
        '[data-testid="author-name"]',
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

    // 飞书特有的噪音元素选择器
    noiseSelectors: [
      ".doc-header",
      ".doc-footer",
      ".catalog-panel",
      ".comment-panel",
      ".share-panel",
      "[data-block-type='callout']",
      ".mention",
      ".emoji",
    ],

    // 飞书正文提取：先滚动采集全部内容，再转为 Markdown
    async extractContent(url) {
      const clean = base.cleanZeroWidth;
      const title = feishu.extractTitle();
      const author = feishu.extractAuthor();

      // 找到飞书滚动容器
      const scrollContainer = document.querySelector(".bear-web-x-container");
      const contentRoot = document.querySelector("[data-content-editable-root]");

      let content = "";

      if (scrollContainer && contentRoot) {
        // 滚动采集模式：飞书虚拟渲染只保留当前可见 block，需逐步滚动收集
        const seenLines = new Set();
        const scrollHeight = scrollContainer.scrollHeight;
        const steps = Math.max(30, Math.ceil(scrollHeight / 100)); // 每 ~100px 一步

        for (let i = 0; i <= steps; i++) {
          const pos = Math.round(scrollHeight * i / steps);
          scrollContainer.scrollTop = pos;
          scrollContainer.dispatchEvent(new Event("scroll", { bubbles: true }));

          // 等待渲染
          await new Promise(r => setTimeout(r, 300));

          // 收集当前可见的行
          const lines = (contentRoot.innerText || "").split("\n").map(l => l.trim()).filter(l => l.length > 2);
          for (const line of lines) {
            if (!seenLines.has(line)) {
              seenLines.add(line);
            }
          }
        }

        // 滚回顶部再收集一次开头
        scrollContainer.scrollTop = 0;
        scrollContainer.dispatchEvent(new Event("scroll", { bubbles: true }));
        await new Promise(r => setTimeout(r, 500));

        // 组装内容（按目录顺序排列标题行）
        const allLines = Array.from(seenLines);
        // 把目录行（如 "1关于对话的载体"）提到前面，清理零宽字符后匹配
        const headingLines = allLines.filter(l => /^\d+[^0-9\s]/.test(clean(l)));
        const otherLines = allLines.filter(l => !/^\d+[^0-9\s]/.test(clean(l)));
        content = [...headingLines, ...otherLines].join("\n\n");
      } else {
        // 回退：直接提取当前可见 DOM
        const contentSels = [
          "[data-content-editable-root]",
          ".doc-content-container",
          "#docx-content",
          ".doc-content",
        ];

        let bestEl = null;
        for (const sel of contentSels) {
          const el = document.querySelector(sel);
          if (el && (el.innerText || "").trim().length > 100) {
            bestEl = el;
            break;
          }
        }
        if (!bestEl) bestEl = document.body;
        content = base.elementToMarkdown(bestEl, feishu.noiseSelectors);
      }

      return {
        title,
        url,
        author,
        publishedAt: "",
        content: clean(content).trim().slice(0, 50000),
      };
    },
  };

  window.__clipVault.platforms.feishu = feishu;
})();

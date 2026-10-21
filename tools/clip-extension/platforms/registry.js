// platforms/registry.js — 平台注册表，检测当前页面并分发到对应适配器
// 注册到 window.__clipVault.registry
(function () {
  window.__clipVault = window.__clipVault || {};
  window.__clipVault.platforms = window.__clipVault.platforms || {};
  const base = window.__clipVault.base;
  const platforms = window.__clipVault.platforms;

  const registry = {

    // 获取所有已注册的平台适配器
    list() {
      return Object.values(platforms).filter(p => typeof p.match === "function");
    },

    // 检测当前页面匹配的平台，返回适配器或 null
    detect() {
      for (const p of registry.list()) {
        try {
          if (p.match()) return p;
        } catch (e) {
          console.warn("[Vault] 平台检测失败:", p.name, e);
        }
      }
      return null;
    },

    // 是否应该跳过初始内容检查（SPA 页面）
    shouldSkipContentCheck() {
      const p = registry.detect();
      return p && p.skipContentCheck;
    },

    // 等待平台内容就绪（飞书等 SPA 需要等待渲染）
    async waitForContent(timeoutMs) {
      const p = registry.detect();
      if (p && typeof p.waitForContent === "function") {
        return p.waitForContent(timeoutMs);
      }
      // 普通页面不需要等待
      return null;
    },

    // 提取内容：优先使用平台适配器，否则走通用提取
    async extractContent() {
      const url = location.href;
      const p = registry.detect();

      if (p && typeof p.extractContent === "function") {
        return p.extractContent(url);
      }

      return base.extractGenericContent(url);
    },

    // 当前平台名称（用于 UI 提示）
    currentName() {
      const p = registry.detect();
      return p ? p.name : "generic";
    },

    // ====== 当前 frame 内的选中文本 ======
    //
    // 只读取本 frame 的 window.getSelection()。跨 frame 的汇总由
    // background.js 用 chrome.scripting.executeScript({ allFrames: true })
    // 在各 frame 内分别调用本函数完成 —— 页面侧的同一源策略不允许
    // 顶层脚本读取跨域 iframe 的选区，只有扩展注入的脚本不受此限。
    //
    // 返回整个页面的选中信息：
    //   { text, title, url, frameTitle, selectedText }
    // 未选中时 text 为空串。
    extractSelection() {
      let text = "";
      try {
        const sel = window.getSelection();
        if (sel && !sel.isCollapsed) {
          text = sel.toString().trim();
        }
      } catch (e) { /* 取不到就当没选中 */ }

      return {
        text,
        selectedText: text.length > 0,
        title: document.title || "",
        url: location.href,
        frameTitle: document.title || "",
      };
    },
  };

  window.__clipVault.registry = registry;
})();

// content.js — 核心壳：悬浮按钮 + 消息通信 + 平台调度
// 依赖：platforms/base.js → platforms/feishu.js → platforms/bilibili.js → platforms/xiaohongshu.js → platforms/registry.js
(function () {
  // 两种情况：
  //   首注入     —— 正常加载
  //   重注入     —— 扩展被更新后，background 主动向已打开的页面重注入（见 background.js）
  // 重注入时必须把旧的悬浮按钮**拆掉重建**：它还绑在旧闭包上，
  // 点击会走旧代码、并且报 "Extension context invalidated"。
  const reinjected = !!window.__clipVaultInjected;
  if (reinjected) {
    const oldFab = document.getElementById("clip-vault-fab");
    if (oldFab) oldFab.remove();
    const oldStyle = document.getElementById("clip-vault-fab-style");
    if (oldStyle) oldStyle.remove();
  }
  window.__clipVaultInjected = true;

  const vault = window.__clipVault;
  const registry = vault.registry;
  // 站点判定见 known-sites.js
  const needsServerAdapter = vault.needsServerAdapter || (() => false);

  // 版本标记（每次改动 content.js 时递增）。
  // 用途：重载扩展后，**已经打开的页面仍然跑着旧 content script**，
  // 它会报 "Extension context invalidated"。在 DevTools Console 里能看到
  // 下面那行日志，才说明当前页面已经用上新代码；看不到就是还没刷新页面。
  const SCRIPT_VERSION = "2026-10-15.1";
  console.log(
    "%c[Vault]%c content script v" + SCRIPT_VERSION +
    (reinjected ? "（重新注入，旧按钮已重建）" : " 已注入") + " @ " + location.host,
    "background:#3b82f6;color:#fff;padding:1px 4px;border-radius:3px",
    "color:#3b82f6"
  );

  // ========== 与扩展后台通信 ==========

  // 扩展被重载/更新后，**已经注入本页的旧 content script 会失去与扩展的连接**，
  // 此时调 chrome.runtime.* 会抛 "Extension context invalidated"。
  // 这不是 bug，但页面不刷新就恢复不了 —— 所以必须明确告诉用户去刷页面。
  function friendlyErr(msg) {
    const m = String(msg || "");
    if (/extension context invalidated/i.test(m)) {
      return "扩展已更新，本页需刷新（F5）后方可继续";
    }
    if (/receiving end does not exist|message port closed|Could not establish connection/i.test(m)) {
      return "与扩展后台失去连接，请刷新本页（F5）";
    }
    const e = connectionHintForFab(m);
    return e;
  }

  function connectionHintForFab(m) {
    if (/failed to fetch|networkerror|load failed|无法连接/i.test(m)) {
      return m + "（浏览器未能连接本地服务：请确认 clip-server.py 正在运行，" +
        "并检查系统代理是否拦截了 localhost）";
    }
    return m;
  }

  // 统一封装 sendMessage：既处理回调式 lastError，也兜住同步抛出的
  // "Extension context invalidated" —— 后者若没兜住，按钮会永远停在 loading。
  function sendMessageSafe(msg) {
    return new Promise((resolve) => {
      try {
        chrome.runtime.sendMessage(msg, (result) => {
          if (chrome.runtime.lastError) {
            resolve({ success: false, error: chrome.runtime.lastError.message });
          } else {
            resolve(result || { success: false, error: "扩展返回空响应" });
          }
        });
      } catch (e) {
        resolve({ success: false, error: (e && e.message) || String(e) });
      }
    });
  }

  // ========== 内容检查 ==========

  function hasMeaningfulContent() {
    const url = location.href;
    if (url.startsWith("chrome://") || url.startsWith("chrome-extension://") ||
        url.startsWith("about:") || url.startsWith("moz-extension://")) return false;

    const body = document.body;
    if (!body) return false;

    try {
      if (window.self !== window.top) return false;
    } catch (e) {
      return false;
    }

    // SPA 平台跳过初始内容检查
    if (registry.shouldSkipContentCheck()) return true;

    const text = (body.innerText || "").trim();
    if (text.length < 300) return false;

    return true;
  }

  // ========== 悬浮按钮 ==========

  function injectFloatingButton() {
    const style = document.createElement("style");
    style.textContent = `
      #clip-vault-fab {
        position: fixed;
        right: 20px;
        bottom: 20px;
        z-index: 2147483647;
        width: 40px;
        height: 40px;
        display: flex;
        align-items: center;
        justify-content: center;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      }
      /* ---- 主按钮（圆形） ---- */
      #clip-vault-fab .clip-vault-main {
        width: 40px; height: 40px; border-radius: 50%;
        background: rgba(40, 40, 40, 0.65);
        border: 1px solid rgba(255, 255, 255, 0.1);
        box-shadow: 0 2px 8px rgba(0, 0, 0, 0.15);
        cursor: pointer;
        display: flex; align-items: center; justify-content: center;
        transition: all 0.2s ease;
        opacity: 0.6;
        backdrop-filter: blur(8px);
        -webkit-backdrop-filter: blur(8px);
      }
      #clip-vault-fab .clip-vault-main svg {
        width: 20px; height: 20px;
        fill: none; stroke: #e0e0e0;
        stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round;
      }
      /* ---- 悬停展开的两个选项 ---- */
      #clip-vault-fab .clip-vault-menu {
        position: absolute;
        right: 0;
        bottom: 48px;
        display: flex;
        flex-direction: column;
        gap: 6px;
        opacity: 0;
        transform: translateY(6px);
        pointer-events: none;
        transition: opacity 0.16s ease, transform 0.16s ease;
      }
      #clip-vault-fab .clip-vault-menu-item {
        white-space: nowrap;
        padding: 7px 12px;
        border-radius: 8px;
        border: 1px solid rgba(255, 255, 255, 0.12);
        background: rgba(40, 40, 40, 0.92);
        color: #f2f2f2;
        font-size: 12px;
        line-height: 1;
        cursor: pointer;
        box-shadow: 0 2px 10px rgba(0, 0, 0, 0.22);
        transition: background 0.15s ease, transform 0.15s ease;
      }
      #clip-vault-fab .clip-vault-menu-item:hover {
        background: rgba(72, 72, 72, 0.96);
        transform: translateX(-2px);
      }
      /* 悬停容器（含菜单）→ 展开。
         .menu-open 是 JS 补的（见 injectFloatingButton）—— 只靠 :hover 不行，
         因为菜单在按钮上方**外侧**，中间有 8px 空隙，鼠标移过去会先脱离命中区。 */
      #clip-vault-fab:hover .clip-vault-menu,
      #clip-vault-fab.menu-open .clip-vault-menu {
        opacity: 1; transform: translateY(0); pointer-events: auto;
      }
      #clip-vault-fab:hover .clip-vault-main,
      #clip-vault-fab.menu-open .clip-vault-main {
        opacity: 1;
        background: rgba(50, 50, 50, 0.88);
        box-shadow: 0 4px 16px rgba(0, 0, 0, 0.2);
      }
      /* 透明桥接区：把「按钮 ↔ 菜单」之间的空隙也纳入菜单的命中范围，
         这样鼠标向上移动时不会经过一段谁都命中不到的真空地带 */
      #clip-vault-fab .clip-vault-menu::after {
        content: "";
        position: absolute;
        left: 0; right: 0; bottom: -9px; height: 9px;
      }
      /* ---- 状态显示期间：把圆形按钮整个收起来 ----
         否则右下角那个 40px 的圆会压住状态气泡，尤其在长文案（duplicate / 失败原因）时。
         收起期间容器也设 pointer-events:none，点击直接穿透到页面，不挡操作。 */
      #clip-vault-fab.loading,
      #clip-vault-fab.success,
      #clip-vault-fab.error { pointer-events: none; }
      #clip-vault-fab.loading .clip-vault-main,
      #clip-vault-fab.success .clip-vault-main,
      #clip-vault-fab.error .clip-vault-main { display: none; }

      /* ---- 状态色 ---- */
      #clip-vault-fab.loading .clip-vault-main { opacity: 0.9; pointer-events: none; }
      #clip-vault-fab.loading .clip-vault-main svg { animation: clip-vault-spin 1s linear infinite; }
      #clip-vault-fab.success .clip-vault-main {
        opacity: 0.9; background: rgba(230, 250, 235, 0.88);
        border-color: rgba(76, 175, 80, 0.35);
      }
      #clip-vault-fab.success .clip-vault-main svg { stroke: #388e3c; }
      #clip-vault-fab.error .clip-vault-main {
        opacity: 0.9; background: rgba(250, 230, 230, 0.88);
        border-color: rgba(244, 67, 54, 0.35);
      }
      #clip-vault-fab.error .clip-vault-main svg { stroke: #d32f2f; }
      /* 处理中/结果态不显示菜单，避免误点 */
      #clip-vault-fab.loading .clip-vault-menu,
      #clip-vault-fab.success .clip-vault-menu,
      #clip-vault-fab.error .clip-vault-menu { display: none; }
      /* ---- 状态气泡（只在有状态时显示；平时悬停是出菜单） ---- */
      /* ---- 状态气泡 ----
         锚点必须是「按钮上方、右缘对齐、向上生长」：
         以前是 right:48px + bottom:50% + translateY(50%)（垂直居中在按钮上），
         配上可换行的长文案后气泡变高，下半截直接掉到视口底边之外被裁掉。 */
      #clip-vault-fab .clip-vault-tooltip {
        position: absolute;
        right: 0;        /* 与按钮右缘对齐，向左生长 */
        bottom: 48px;    /* 浮在按钮上方，向上生长 —— 不会被视口底边裁掉 */
        background: rgba(30, 30, 30, 0.92); color: #eee;
        padding: 6px 10px; border-radius: 6px; font-size: 12px;
        /* width:max-content：气泡按内容自然取宽。
           必须显式声明——绝对定位元素的收缩适配宽度被 containing block
           （40px 的圆钮容器）卡死，20px 的内容宽只装得下一个汉字，
           短文案会一字一行竖排。max-content 让它突破容器宽度向左生长，
           长文案仍由 max-width 兜底换行。 */
        width: max-content;
        white-space: normal; max-width: 320px; line-height: 1.45; text-align: left;
        box-shadow: 0 4px 16px rgba(0, 0, 0, 0.28);
        opacity: 0; pointer-events: none;
        transition: opacity 0.15s;
      }
      #clip-vault-fab.loading .clip-vault-tooltip,
      #clip-vault-fab.success .clip-vault-tooltip,
      #clip-vault-fab.error .clip-vault-tooltip { opacity: 1; }
      /* ---- 深色模式：按钮反色 ---- */
      @media (prefers-color-scheme: dark) {
        #clip-vault-fab .clip-vault-main {
          background: rgba(220, 220, 220, 0.65);
          border: 1px solid rgba(255, 255, 255, 0.15);
          box-shadow: 0 2px 8px rgba(0, 0, 0, 0.3);
        }
        #clip-vault-fab .clip-vault-main svg { stroke: #333; }
        #clip-vault-fab:hover .clip-vault-main { background: rgba(240, 240, 240, 0.9); }
        #clip-vault-fab.success .clip-vault-main {
          background: rgba(200, 240, 210, 0.85); border-color: rgba(76, 175, 80, 0.4);
        }
        #clip-vault-fab.error .clip-vault-main {
          background: rgba(240, 200, 200, 0.85); border-color: rgba(244, 67, 54, 0.4);
        }
      }
      @keyframes clip-vault-spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
    `;
    style.id = "clip-vault-fab-style";   // 重注入时据此移除旧样式
    document.head.appendChild(style);

    const fab = document.createElement("div");
    fab.id = "clip-vault-fab";
    fab.dataset.vaultVersion = SCRIPT_VERSION;   // 便于在 DevTools 里确认版本
    fab.innerHTML = `
      <div class="clip-vault-menu">
        <button type="button" class="clip-vault-menu-item" data-act="inbox">📥 存待审区</button>
        <button type="button" class="clip-vault-menu-item" data-act="full">⚡ 完整入库</button>
      </div>
      <div class="clip-vault-main">
        <svg viewBox="0 0 24 24">
          <path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/>
        </svg>
      </div>
      <!-- 气泡只在有状态时显示，平时留空（原来那句「入库」是冗余的：
           悬停本来就会展开「存待审区 / 完整入库」两个选项） -->
      <span class="clip-vault-tooltip"></span>
    `;

    // ===== 悬停展开/收起 =====
    //
    // 为什么不能只用 CSS :hover：`.clip-vault-menu` 是绝对定位在按钮**上方外侧**的
    // （bottom: 48px），与 40×40 的按钮之间隔着 8px 空隙。鼠标从按钮移向菜单时，
    // 会先离开容器的命中区域 → :hover 立刻失效 → 菜单被设回 pointer-events:none
    // 并淡出 —— 表现就是「两个选项出来了，但鼠标一移就消失、根本点不到」。
    //
    // 解法：JS 维护一个 .menu-open 类，离开时延迟 320ms 再收起，
    //       再配合上面 CSS 的透明桥接区（.clip-vault-menu::after）。
    let hideTimer = null;
    const openMenu = () => {
      clearTimeout(hideTimer);
      fab.classList.add("menu-open");
    };
    const closeMenuSoon = () => {
      clearTimeout(hideTimer);
      hideTimer = setTimeout(() => fab.classList.remove("menu-open"), 320);
    };
    fab.addEventListener("mouseenter", openMenu);
    fab.addEventListener("mouseleave", closeMenuSoon);
    fab.addEventListener("focusin", openMenu);       // 键盘 Tab 可达
    fab.addEventListener("focusout", closeMenuSoon);

    // 点主按钮 = 完整入库（保持一键习惯）；菜单里可明确选择两种结果之一
    fab.querySelector(".clip-vault-main").addEventListener("click", () => handleClip("full"));
    fab.querySelectorAll(".clip-vault-menu-item").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        fab.classList.remove("menu-open");   // 选完立刻收起
        handleClip(btn.dataset.act);
      });
    });

    document.body.appendChild(fab);
  }

  // SPA 页面内容延迟加载：MutationObserver 等待后注入
  function injectFloatingButtonWithRetry() {
    // 悬浮按钮**只在顶层 frame 注入**。
    //
    // 为什么必须显式挡住：manifest 里 content script 配了 all_frames:true
    // （正文常整块在跨域 iframe 内，需要在每个 frame 都注入脚本才能抓全），
    // 于是本函数会在每个 frame 各跑一遍。若不挡，页面里每个 iframe 都会长出
    // 一个悬浮按钮 —— 用户会看到按钮叠在一起或飘在别人家 iframe 的角落里。
    // 只在顶层注入既符合直觉（按钮属于「这个页面」），也避免重复。
    let isTop = true;
    try { isTop = (window.self === window.top); } catch (e) { isTop = false; }
    if (!isTop) return;

    if (hasMeaningfulContent()) {
      injectFloatingButton();
      return;
    }

    let attempts = 0;
    const observer = new MutationObserver(() => {
      attempts++;
      if (hasMeaningfulContent() || attempts >= 15) {
        observer.disconnect();
        if (!document.getElementById("clip-vault-fab")) {
          injectFloatingButton();
        }
      }
    });
    observer.observe(document.body || document.documentElement, {
      childList: true, subtree: true,
    });
    setTimeout(() => {
      observer.disconnect();
      // 兜底分支同样要确认「本 frame 仍是顶层且内容达标」——
      // 此前这里**不检查 hasMeaningfulContent()**，会无条件注入；
      // 配合 all_frames:true 就等于给每个 iframe 都装一个按钮。
      if (isTop && hasMeaningfulContent() &&
          !document.getElementById("clip-vault-fab")) {
        injectFloatingButton();
      }
    }, 15000);
  }

  // ========== 选中文本检测 ==========

  function getSelectedText() {
    const selection = window.getSelection();
    if (!selection || selection.isCollapsed) return null;

    const text = selection.toString().trim();
    if (text.length < 10) return null;  // 太短不算

    return text;
  }

  // ========== 剪藏流程 ==========

  // mode: "full"  = 完整入库（抓正文/字幕 → POST /api/clip，写入 raw 并触发 wiki）
  //       "inbox" = 存待审区（只在 raw/inbox/ 建一张指针卡片，不抓正文、不生成 wiki）
  async function handleClip(mode) {
    mode = mode === "inbox" ? "inbox" : "full";
    const fab = document.getElementById("clip-vault-fab");
    if (!fab || fab.classList.contains("loading")) return;

    // ---------- 存待审区 ----------
    if (mode === "inbox") {
      setFabState(fab, "loading", "正在存入待审区…");
      const result = await sendMessageSafe({
        action: "inbox-add",
        url: location.href,
        title: document.title || "",
      });
      if (result.success) {
        setFabState(fab, "success", "已存入待审区 ✓");
        setTimeout(() => resetFab(fab), 2500);
      } else if (result.error === "duplicate") {
        setFabState(fab, "success", "已在待审区中");
        setTimeout(() => resetFab(fab), 2500);
      } else {
        setFabState(fab, "error", "存入失败：" + friendlyErr(result.error || "未知错误"));
        setTimeout(() => resetFab(fab), 4000);
      }
      return;
    }

    // ---------- 完整入库 ----------

    // 扩展没有页面内适配器、但服务端有专用适配器的站点（目前是 YouTube），
    // 直接交给后端：页面内只能拿到 DOM，**字幕根本不在里面**，
    // 而且标题+简介通常已超过 50 字，不会触发回退，会静默产出没有字幕的废记录。
    if (needsServerAdapter(location.href) && !getSelectedText()) {
      setFabState(fab, "loading", "正在交给后端适配器抓取…");
      const r = await sendMessageSafe({ action: "clip-url", url: location.href });
      if (r && r.success && r.taskId) {
        setFabState(fab, "success", "已加入浏览器协助队列，稍后将自动完成");
        setTimeout(() => resetFab(fab), 6000);
      } else if (r && r.success) {
        const bad = isWikiBad(r.wiki);
        setFabState(fab, bad ? "error" : "success", wikiTip(r.wiki, false));
        setTimeout(() => resetFab(fab), bad ? 9000 : 3000);
      } else if (r && r.error === "duplicate") {
        setFabState(fab, "error", "已存在，未重复入库");
        setTimeout(() => resetFab(fab), 6000);
      } else {
        setFabState(fab, "error",
          "入库失败：" + friendlyErr((r && r.error) || "未知错误"));
        setTimeout(() => resetFab(fab), 6000);
      }
      return;
    }

    // 检查是否有选中文本。
    //
    // 本 frame 的 getSelectedText() 只覆盖当前 frame —— 顶层看不到
    // 跨域 iframe 里的选区。若顶层没读到，向 background 请求一次
    // 「所有 frame 的选区汇总」，覆盖正文在异构 iframe 内的站点。
    let selectedText = getSelectedText();
    if (!selectedText) {
      const r = await sendMessageSafe({ action: "selection-probe" });
      if (r && r.success && r.text) selectedText = r.text;
    }
    const isSelectionClip = !!selectedText;

    const platformName = registry.currentName();
    const isSPA = registry.shouldSkipContentCheck();
    setFabState(fab, "loading", isSelectionClip ? "正在入库选中文本…" : (isSPA ? "等待页面加载…" : "正在入库…"));

    try {
      let data;

      if (isSelectionClip) {
        // 选中文字入库
        data = {
          title: document.title || location.href,
          url: location.href,
          content: selectedText,
          type: "selection",
          platform: platformName,
          selectedText: true,
        };
      } else {
        // 剪藏整个页面
        // 等待平台内容就绪（SPA 页面）
        if (isSPA) {
          await registry.waitForContent(12000);
          await new Promise(r => setTimeout(r, 500));
        }

        // 平台站点的适配器会在页面内自己抓取（B 站会直接调字幕接口，带登录态）
        data = await registry.extractContent();
        if (!data.content || data.content.trim().length < 50) {
          setFabState(fab, "error", "未检测到有效正文");
          setTimeout(() => resetFab(fab), 3000);
          return;
        }
      }

      // 「完整入库」的定义是 raw + wiki，所以要求服务端**等 wiki 有结论再返回**。
      // 否则 API key 失效 / 额度用尽这类失败只留在服务端控制台，界面照样报「已入库」。
      data.wikiSync = true;

      // 委托给 background.js 发送（绕过页面 CSP 限制）
      const result = await sendMessageSafe({ action: "clip-direct", data });
      if (result.success) {
        const tip = wikiTip(result.wiki, isSelectionClip);
        const bad = isWikiBad(result.wiki);
        // wiki 没生成时用 error 态 —— 一片绿会把失败盖过去
        setFabState(fab, bad ? "error" : "success", tip);
        setTimeout(() => resetFab(fab), bad ? 9000 : 2500);
      } else if (result.error === "duplicate") {
        // 去重命中不是失败：文件已存在、内容没丢。以前会显示「入库失败: duplicate」
        // 只显示文件名（去掉 raw\articles\ 前缀），否则气泡被长路径撑成好几行
        const f = String(result.existing_file || "")
          .replace(/^raw[\\/](articles|inbox|notes)[\\/]/, "");
        setFabState(fab, "error", "已存在，未重复入库" + (f ? "\n" + f : ""));
        setTimeout(() => resetFab(fab), 6000);
      } else {
        setFabState(fab, "error", "入库失败：" + friendlyErr(result.error || "未知错误"));
        setTimeout(() => resetFab(fab), 6000);
      }
    } catch (e) {
      setFabState(fab, "error", "入库失败：" + friendlyErr((e && e.message) || String(e)));
      setTimeout(() => resetFab(fab), 4000);
    }
  }

  // ========== wiki 结果呈现 ==========
  //
  // 服务端现在会在响应里带上 wiki 结论：
  //   ok      —— 已生成
  //   pending —— 后台线程在跑（本页不阻塞时才会出现）
  //   skipped —— 没过质量门禁（正文太短 / 命中登录墙关键词）
  //   failed  —— 真的没生成，reason 里是原因（如 HTTP 429 额度用尽）
  function isWikiBad(wiki) {
    return !!wiki && wiki.status !== "ok" && wiki.status !== "pending";
  }

  function wikiTip(wiki, isSelectionClip) {
    // 选中文本同样要按 wiki 结论分档 —— 降级时服务端已撤回 raw，
    // 不能再报「已入库」（此前这里无条件返回成功文案，是假成功）。
    if (isSelectionClip) {
      if (!wiki) return "已入库选中文本 ✓";
      if (wiki.status === "ok") return "已入库选中文本 ✓（含 wiki）";
      if (wiki.status === "pending") return "已入库选中文本 ✓（wiki 后台生成中）";
      if (wiki.status === "skipped") {
        return "已入库选中文本 ✓｜wiki 跳过：" +
          (String(wiki.reason || "").slice(0, 120) || "未说明");
      }
      if (wiki.status === "demoted") {
        const why = wiki.kind === "content_incomplete" ? "内容不完整" : "wiki 未生成";
        return "入库未完成（" + why + "），已转存待审区\n" +
          (String(wiki.reason || "").slice(0, 120) || "未说明") +
          (wiki.inbox_card ? "\n" + wiki.inbox_card : "");
      }
      return "已入库选中文本，但 wiki 未生成：" +
        (String(wiki.reason || "").slice(0, 120) || "未知原因");
    }
    if (!wiki) return "已入库 ✓";
    const reason = String(wiki.reason || "").slice(0, 120);
    if (wiki.status === "ok") return "完整入库 ✓（raw + wiki）";
    if (wiki.status === "pending") return "raw 已入库 ✓（wiki 后台生成中）";
    if (wiki.status === "skipped") return "raw 已入库 ✓｜wiki 跳过：" + (reason || "未说明");
    // demoted：服务端已把 raw 撤回，改存待审区。
    // 两种触发：素材本身不完整（视频没字幕） / 素材完整但 wiki 生成失败。
    if (wiki.status === "demoted") {
      const why = wiki.kind === "content_incomplete" ? "内容不完整" : "wiki 未生成";
      return "入库未完成（" + why + "），已转存待审区\n" +
        (reason || "未说明") +
        (wiki.inbox_card ? "\n" + wiki.inbox_card : "");
    }
    return "raw 已入库，但 wiki 未生成：" + (reason || "未知原因");
  }

  function setFabState(fab, state, tooltip) {
    fab.classList.remove("loading", "success", "error");
    if (state !== "idle") fab.classList.add(state);
    const tip = fab.querySelector(".clip-vault-tooltip");
    if (tip) tip.textContent = tooltip;
    const svg = fab.querySelector(".clip-vault-main svg");
    if (state === "loading" && svg) {
      svg.innerHTML = '<circle cx="12" cy="12" r="9" stroke-dasharray="28 28" />';
    }
  }

  function resetFab(fab) {
    setFabState(fab, "idle", "");
    const svg = fab.querySelector(".clip-vault-main svg");
    if (svg) {
      svg.innerHTML = '<path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/>';
    }
  }

  // ========== 消息监听 ==========

  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg.action === "extract") {
      const doExtract = async () => {
        try {
          // 检查是否有选中文本
          const selectedText = getSelectedText();

          if (selectedText) {
            // 返回选中文本
            sendResponse({
              success: true,
              data: {
                title: document.title || location.href,
                url: location.href,
                content: selectedText,
                type: "selection",
                platform: registry.currentName(),
                selectedText: true,
              }
            });
          } else {
            // 提取整个页面
            if (registry.shouldSkipContentCheck()) {
              await registry.waitForContent(12000);
              await new Promise(r => setTimeout(r, 500));
            }
            const data = await registry.extractContent();
            sendResponse({ success: true, data });
          }
        } catch (e) {
          sendResponse({ success: false, error: e.message });
        }
      };
      doExtract();
      return true;
    }
    return true;
  });

  // ========== 启动 ==========
  injectFloatingButtonWithRetry();
})();

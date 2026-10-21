// background.js — 右键菜单 + 快捷键 + 浏览器协助队列

let eventSource = null;
let isConnected = false;
// connectToServer() 的并发去重：闹钟心跳与 vaultBootstrap()（任何扩展事件
// 唤醒 SW 都会执行）可能**同时**触发连接；readyState 检查挡不住「连接中」
// 状态，两个调用在 await chrome.storage 处让出后会各建一条 EventSource，
// 先建的被覆盖成孤儿但连接仍在（服务端看到「已连接 x2」的幽灵连接）。
let connectInFlight = false;

// 获取鉴权 headers
async function getAuthHeaders() {
  const settings = await chrome.storage.local.get("token");
  const token = settings.token || "";
  const headers = { "Content-Type": "application/json" };
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }
  return headers;
}

// live-source 自动检测
const LIVE_SOURCE_DOMAINS = [
  "feishu.cn", "larksuite.com",
  "notion.so", "notion.site",
  "docs.google.com",
  "confluence.", "atlassian.",
];

function autoDetectLiveSource(url) {
  const urlLower = (url || "").toLowerCase();
  return LIVE_SOURCE_DOMAINS.some(d => urlLower.includes(d));
}

// content.js 依赖的全部脚本，顺序必须与 manifest.content_scripts.js 保持一致。
// 用于 MV3 的 chrome.scripting.executeScript（按需注入 / 更新后重注入）。
const CONTENT_SCRIPT_FILES = [
  "platforms/base.js",
  "platforms/known-sites.js",
  "platforms/feishu.js",
  "platforms/bilibili.js",
  "platforms/youtube.js",
  "platforms/xiaohongshu.js",
  "platforms/wechat.js",
  "platforms/registry.js",
  "content.js",
];

// 扩展被重载/更新后，**已经打开的页面仍然跑着旧的 content script**：
// 它调 chrome.runtime.* 会抛 "Extension context invalidated"，而且页面不刷新就恢复不了。
// 这里在更新后主动把新脚本重注入所有已打开的 http(s) 页面，免去用户逐个按 F5。
// content.js 会识别「重复注入」并把旧的悬浮按钮拆掉重建，否则页面上会留着
// 绑在旧闭包上的按钮，照样报错。
async function reinjectIntoOpenTabs() {
  let tabs = [];
  try {
    tabs = await chrome.tabs.query({ url: ["http://*/*", "https://*/*"] });
  } catch (e) {
    console.warn("[Vault] 枚举标签页失败，跳过自动重注入:", e);
    return;
  }
  let ok = 0;
  let skipped = 0;
  for (const t of tabs) {
    try {
      await chrome.scripting.executeScript({
        target: { tabId: t.id },
        files: CONTENT_SCRIPT_FILES,
      });
      ok++;
    } catch (e) {
      skipped++;   // chrome:// 页、扩展商店页等无权限的标签
    }
  }
  console.log(`[Vault] 已向 ${ok} 个已打开页面重注入 content script（跳过 ${skipped} 个）`);
}

// 安装时创建右键菜单
chrome.runtime.onInstalled.addListener((details) => {
  chrome.contextMenus.create({
    id: "clip-vault-page",
    title: "入库此页",
    contexts: ["page"]
  });

  chrome.contextMenus.create({
    id: "clip-vault-selection",
    title: "选中文字入库",
    contexts: ["selection"]
  });

  chrome.contextMenus.create({
    id: "clip-vault-link",
    title: "链接入库",
    contexts: ["link"]
  });

  console.log("知识 Vault 入库插件已安装");

  // 更新/重载后，已打开的页面里是旧 content script —— 主动换新
  if (details.reason === "update" || details.reason === "install") {
    reinjectIntoOpenTabs();
  }

  // 启动时尝试连接服务端
  connectToServer();
});

// 启动时也连接
chrome.runtime.onStartup.addListener(() => {
  connectToServer();
});

// 右键菜单点击
chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId === "clip-vault-page") {
    await clipTab(tab);
  } else if (info.menuItemId === "clip-vault-selection") {
    await clipSelection(tab);
  } else if (info.menuItemId === "clip-vault-link" && info.linkUrl) {
    const newTab = await chrome.tabs.create({
      url: info.linkUrl,
      active: false
    });
    await waitForTabLoad(newTab.id);
    await clipTab(newTab);
    setTimeout(() => chrome.tabs.remove(newTab.id), 500);
  }
});

// 快捷键触发
chrome.commands.onCommand.addListener(async (command) => {
  if (command === "clip-page") {
    const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
    if (tabs[0]) await clipTab(tabs[0]);
  }
});

// ====== 浏览器协助：SSE 连接服务端 ======

async function connectToServer() {
  // 已经连着就别重连 —— 心跳定时器每分钟都会调进来一次
  if (eventSource && eventSource.readyState === 1 /* OPEN */) return;
  if (connectInFlight) return;   // 已有连接流程在跑，直接复用（防幽灵连接）
  connectInFlight = true;

  try {
    const settings = await chrome.storage.local.get(["serverUrl", "token"]);
    const serverUrl = (settings.serverUrl || "http://localhost:7878").replace(/\/$/, "");
    // EventSource 无法自定义请求头，token 通过 query 参数传递
    const token = settings.token || "";
    const connectUrl = serverUrl + "/api/browser/connect"
      + (token ? "?token=" + encodeURIComponent(token) : "");

    if (eventSource) {
      eventSource.close();
    }

    try {
      eventSource = new EventSource(connectUrl);

      eventSource.addEventListener("info", (e) => {
        const data = JSON.parse(e.data);
        isConnected = true;
        console.log("[Vault] 已连接服务端，待处理任务: " + data.pending);
      });

      eventSource.addEventListener("task", async (e) => {
        const data = JSON.parse(e.data);
        console.log("[Vault] 收到任务 [" + data.taskId + "]: " + data.url);
        await processBrowserTask(data.taskId, data.url);
      });
      eventSource.onerror = () => {
        isConnected = false;
        console.log("[Vault] 连接断开，5秒后重连...");
        setTimeout(connectToServer, 5000);
      };
    } catch (e) {
      console.log("[Vault] 连接失败，5秒后重试...", e);
      setTimeout(connectToServer, 5000);
    }
  } finally {
    connectInFlight = false;
  }
}

// ====== 在办任务的持久化（防 MV3 回收半途丢掉任务）======
//
// 这是「扩展执行了任务、服务端却收不到结果」的**真正根因**：
// 任务在 SSE 事件处理器里跑。MV3 的 SW 空闲约 30 秒就被回收，而
// `startSwKeepalive()` 用的 **`setInterval` 根本挡不住回收** ——
// Chrome 明确说明 `setTimeout`/`setInterval` 不计入 SW 保活，只有
// 扩展 API 调用与扩展事件才算。于是「等标签页加载（最长 15s）+ 2s + 1s +
// 提取 + 回传」这条链一旦越过 30 秒的空闲窗口，**整个 async 函数连同内联的
// 状态一起消失**：既没有结果、也没有报错，任务永远停在「已发送给浏览器」。
// 这解释了为什么「以前正常、最近不行」—— 以前任务恰好跑得快、没撞上回收，
// 而渲染/字幕抓取变重之后就越过了窗口。
//
// 解法：**收到任务的第一时间就把它写进 chrome.storage**（同步、瞬间完成），
// 处理结束后再删。SW 被回收后，任何一次唤醒（闹钟 / 重连 / 用户点击）都调用
// `resumePendingTasks()` 把尚未处理完的任务重新跑一遍。任务处理是幂等的：
// 结果一旦回传即成为服务端终态，重复处理不会再改状态（服务端按 taskId 更新）。
const PENDING_TASK_PREFIX = "pendingTask-";
const PENDING_TASK_MAX_AGE = 10 * 60 * 1000;   // 超过 10 分钟的在办任务视为过期
// 上报发件箱的键前缀（定义在此，供 resumePendingTasks 与发件箱共用；详见下方发件箱一节）
const OUTBOX_PREFIX = "outbox-";

async function _pendingTaskAdd(taskId, url) {
  await chrome.storage.local.set({
    [PENDING_TASK_PREFIX + taskId]: { taskId, url, ts: Date.now() },
  });
}

async function _pendingTaskRemove(taskId) {
  await chrome.storage.local.remove(PENDING_TASK_PREFIX + taskId);
}

async function _pendingTaskAll() {
  const all = await chrome.storage.local.get(null);
  return Object.keys(all)
    .filter((k) => k.startsWith(PENDING_TASK_PREFIX))
    .map((k) => all[k])
    .filter(Boolean);
}

let _resuming = false;

// SW 每次醒来都调用：把因回收而中断的在办任务重新跑一遍。
async function resumePendingTasks() {
  if (_resuming) return;          // 防重入：一次唤醒只跑一轮
  _resuming = true;
  try {
    const items = await _pendingTaskAll();
    const now = Date.now();
    for (const item of items) {
      if (now - (item.ts || 0) > PENDING_TASK_MAX_AGE) {
        await _pendingTaskRemove(item.taskId);
        continue;
      }
      // 若该任务已在上报发件箱里（说明处理已完成、只是没送达），跳过重抓，
      // 交给 flushOutbox 补发即可。
      const outbox = await chrome.storage.local.get(OUTBOX_PREFIX + item.taskId);
      if (outbox[OUTBOX_PREFIX + item.taskId]) {
        await _pendingTaskRemove(item.taskId);
        continue;
      }
      console.log("[Vault] 续跑中断的任务 [" + item.taskId + "]: " + item.url);
      try {
        await processBrowserTask(item.taskId, item.url);
      } catch (e) {
        console.warn("[Vault] 续跑任务异常 [" + item.taskId + "]:", e);
      }
    }
  } finally {
    _resuming = false;
  }
}

// ====== 浏览器协助任务 ======

// MV3 的 service worker **空闲约 30 秒就被回收，正在 await 的异步函数会一起消失**。
// 浏览器协助任务要等标签页加载（waitForTabLoad 最长 15s）+ 提取 + 回传，
// 很容易越过这个窗口。一旦 SW 在中间被回收，任务就永远停在「已发送给浏览器」，
// 直到服务端 5 分钟后判超时 —— 这正是「点了入库、等半天、最后说可能卡住」的成因。
// 每 15 秒调一次扩展 API 可以把空闲计时器顶回去（getPlatformInfo 极轻量）。
function startSwKeepalive() {
  try {
    return setInterval(() => {
      try { chrome.runtime.getPlatformInfo(() => {}); } catch (e) { /* ignore */ }
    }, 15000);
  } catch (e) {
    return null;
  }
}

function stopSwKeepalive(timer) {
  if (timer) { try { clearInterval(timer); } catch (e) { /* ignore */ } }
}

// 用户可能已经把这个页面开着了 —— 那就直接用，**不要再开一个隐藏标签页**。
// 之前无条件 chrome.tabs.create，同一个视频会凭空多出一个标签页（用户看到的就是
// 「点一下弹出一个窗」），而且那个新页面还要重新加载一遍。
function _tabUrlKey(raw) {
  try {
    const u = new URL(raw);
    const host = u.hostname.replace(/^www\./, "").toLowerCase();
    let key = host + u.pathname.replace(/\/+$/, "");
    if (host === "youtube.com" || host === "m.youtube.com") {
      const v = u.searchParams.get("v");
      if (v) key = "youtube.com/watch?v=" + v;
    } else if (host === "youtu.be") {
      key = "youtube.com/watch?v=" + u.pathname.slice(1).split("/")[0];
    } else {
      key += u.search;
    }
    return key.toLowerCase();
  } catch (e) {
    return String(raw || "").toLowerCase();
  }
}

async function findTabForUrl(url) {
  try {
    const want = _tabUrlKey(url);
    const tabs = await chrome.tabs.query({});
    // **优先当前窗口的活动标签页**：用户点「处理」时通常正开着这个页面
    // （尤其视频页），复用它既快、又不会凭空多出标签页。
    const active = tabs.find(t => t.active && t.url && _tabUrlKey(t.url) === want);
    if (active) return active;
    for (const t of tabs) {
      if (t.url && _tabUrlKey(t.url) === want) return t;
    }
  } catch (e) { /* ignore */ }
  return null;
}

async function processBrowserTask(taskId, url) {
  const keepalive = startSwKeepalive();
  let tab = null;
  let createdTab = false;
  // **第一件事**：把任务持久化。即便下面这条长链被 SW 回收打断，
  // 下次唤醒时 resumePendingTasks() 还会把它捡回来重跑（详见该函数注释）。
  try { await _pendingTaskAdd(taskId, url); } catch (e) { /* ignore */ }
  try {
    const existing = await findTabForUrl(url);
    if (existing) {
      tab = existing;
      console.log("[Vault] 任务 [" + taskId + "] 复用已打开的标签页", tab.id);
    } else {
      // **背景标签页**：active:false 表示「创建但**不激活**」——新标签在后台加载，
      // 不会把用户的当前页面顶掉、也不会抢走焦点。
      // 显式指定 lastFocusedWindow，避免在某些窗口状态下被 Chrome 提到前台。
      let winId;
      try {
        const win = await chrome.windows.getLastFocused();
        winId = win && win.id;
      } catch (e) { /* 拿不到就交给 Chrome 默认 */ }
      tab = await chrome.tabs.create({
        url,
        active: false,
        ...(winId != null ? { windowId: winId } : {}),
      });
      createdTab = true;
      await waitForTabLoad(tab.id);
    }
    // 等一下让异步内容加载
    await new Promise(r => setTimeout(r, 2000));

    // 滚动触发懒加载这一步已并入 extractAcrossFrames()：那里会对**每个 frame**
    // 滚动（只滚顶层对跨域 iframe 内的滚动容器无效），并在滚动后统一等待。
    // 不再在这里单独滚顶层，否则重复等待且对 iframe 正文毫无作用。

    // 确保 content script 就位（含平台脚本，否则 content.js 取不到 registry）
    try {
      await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        files: CONTENT_SCRIPT_FILES,
      });
    } catch (e) { /* 可能已经注入了 */ }

    // 跨 frame 汇总正文（不能只问顶层：正文可能整块在跨域 iframe 里）
    const picked = await extractAcrossFrames(tab.id);
    if (!picked || !picked.data || !picked.data.content) {
      throw new Error("提取失败（页面未返回内容）");
    }
    const response = { success: true, data: picked.data };

    // 让入库结果用**顶层**的标题与 URL —— iframe 内的 title/url 往往是
    // 「帮助中心」之类很泛的值，或指向 h5 子站，与用户点的那条链接对不上。
    // 用顶层值可保证 source_id 去重、待审区摘卡、飞书回链都指向同一处。
    try {
      const topInfo = await getTopFrameInfo(tab.id);
      if (topInfo.title) response.data.title = topInfo.title;
      if (topInfo.url) response.data.url = topInfo.url;
    } catch (e) { /* 取不到就用 frame 内的 */ }

    // 入库 = raw + wiki，所以要求服务端等 wiki 出结论再返回；
    // 失败时服务端会撤回 raw 并转存待审区，任务记录也不会指向不存在的文件。
    response.data.wikiSync = true;
    response.data.channel = "fab";

    const result = await submitResult(taskId, response.data);
    console.log("[Vault] 任务完成 [" + taskId + "]", result);
    if (result && result.success === false) {
      console.error("[Vault] 服务端未接受结果 [" + taskId + "]: " + (result.error || "未知"));
    }
    // 处理结论同时**发一条桌面通知**：弹窗一旦失焦就被销毁，用户未必还开着它，
    // 而这条任务（打开标签页 + 抓取 + 生成 wiki）可能跑了几分钟。
    // 只在后台推送到系统通知栏，用户才能确定「到底成没成、为什么没成」。
    try {
      const w = result && result.wiki;
      if (result && result.error === "duplicate") {
        await notify("📌 该链接已存在于知识库", "待审区条目已移除，可在知识库中查看原有版本。");
      } else if (w && w.status === "demoted") {
        await notify("⚠️ 未入库，已退回待审区",
          (w.reason || "内容未达标") + "（条目保留在待审区，可重试）");
      } else if (result && result.success) {
        await notify("✅ 已入库", "Raw：" + (result.filename || "") +
          "（约 " + (result.wordCount || 0) + " 字）");
      } else if (result) {
        await notify("❌ 浏览器抓取未完成", (result.reason || result.error || "服务端未说明原因"));
      }
    } catch (e) { /* 通知失败不影响主流程 */ }
  } catch (e) {
    console.error("[Vault] 任务失败 [" + taskId + "]:", e);
    const reported = await submitError(taskId, (e && e.message) || String(e));
    if (!reported) {
      console.error("[Vault] ⚠️ 失败原因没能上报给服务端 —— 该任务会一直停在" +
        "「已发送给浏览器」，直到服务端数分钟后判超时。请检查扩展设置里的服务地址与 Token。");
    }
    try { await notify("❌ 浏览器协助抓取失败", (e && e.message) || String(e)); } catch (e2) {}
  } finally {
    stopSwKeepalive(keepalive);
    // 处理已结束（成功回传或已把失败原因存进发件箱）→ 撤销在办标记，
    // 避免下次唤醒时又被续跑一遍。若上面的上报没能送达，submitResult/submitError
    // 已把它留在发件箱，由闹钟补发 —— 那条路径不需要在办标记。
    try { await _pendingTaskRemove(taskId); } catch (e) { /* ignore */ }
    // 只清理「我们自己开的」标签页：复用用户页面的情况下绝不能关掉它。
    // 之前清理只写在成功分支里，失败时会把隐藏标签页一直留在那（又一个「弹窗」来源）。
    if (createdTab && tab) {
      try { chrome.tabs.remove(tab.id).catch(() => {}); } catch (e) { /* ignore */ }
    }
  }
}

// ====== 上报发件箱（崩溃安全）======
//
// 为什么必须是「先落盘、再发送，发不出去就等闹钟重试」：
// MV3 的 service worker **空闲约 30 秒就被回收，正在 await 的 fetch 会一起消失**。
// 浏览器协助任务要等标签页加载（最长 15 秒）+ 提取 + 回传，很容易越过这个窗口；
// 一旦 SW 在 fetch 途中被回收，请求就没发出去，服务端收不到任何回音，
// 任务于是停在「已发送给浏览器」直到超时 —— 这正是实测中
// 「11 个任务被派发、0 个结果回传」的成因（扩展侧明明执行了任务，飞书却收不到结论）。
//
// 只加 keepalive 不够：keepalive 只能把空闲计时器顶回去，顶不住 SW 因其它原因
// 被回收。真正的解法是把「要上报的内容」**持久化**，再由 chrome.alarms 定时补发，
// 直到服务端明确接收（或明确拒绝）。这样即便中途 SW 死了，下次醒来也能补上。
const OUTBOX_KEY = "vaultOutbox";
const OUTBOX_MAX_AGE = 30 * 60 * 1000;   // 超过 30 分钟的陈旧条目直接丢弃，避免无限堆积

async function _outboxAll() {
  const all = await chrome.storage.local.get(null);
  return Object.keys(all)
    .filter((k) => k.startsWith(OUTBOX_PREFIX))
    .map((k) => all[k])
    .filter(Boolean)
    .sort((a, b) => (a.ts || 0) - (b.ts || 0));
}

async function _outboxAdd(taskId, payload) {
  const key = OUTBOX_PREFIX + taskId;
  await chrome.storage.local.set({
    [key]: { taskId, payload, ts: Date.now(), tries: 0, lastError: "" },
  });
  return key;
}

async function _outboxRemove(taskId) {
  await chrome.storage.local.remove(OUTBOX_PREFIX + taskId);
}

async function _outboxBump(key, lastError) {
  const got = await chrome.storage.local.get(key);
  const item = got[key];
  if (!item) return;
  item.tries = (item.tries || 0) + 1;
  item.lastError = String(lastError || "").slice(0, 200);
  await chrome.storage.local.set({ [key]: item });
}

// 尝试投递单个条目。返回 true 表示「服务端已明确接收」——收录或明确的 4xx 拒绝
// 都算终态（4xx 重试也没用，继续留着只会反复打服务端）。
async function _outboxDeliver(item) {
  const settings = await chrome.storage.local.get(["serverUrl", "token"]);
  const serverUrl = (settings.serverUrl || "http://localhost:7878").replace(/\/$/, "");
  const headers = { "Content-Type": "application/json" };
  if (settings.token) headers["Authorization"] = "Bearer " + settings.token;

  const res = await fetch(serverUrl + "/api/browser/result", {
    method: "POST",
    headers,
    body: JSON.stringify(item.payload),
  });
  if (res.ok) return true;
  if (res.status >= 400 && res.status < 500) {
    // 401（token 不对）/ 400（taskId 不合法）等 —— 重试无意义，视为终态并留日志
    console.error("[Vault] 上报被服务端拒绝 [" + item.taskId + "] HTTP " + res.status +
      "：该条不再重试，请检查扩展设置里的服务地址与 Token");
    return true;
  }
  return false;   // 5xx / 网络错误 → 保留，等下次闹钟补发
}

// 补发整个发件箱。由闹钟、SW 启动、以及每次上报后触发。
async function flushOutbox() {
  const items = await _outboxAll();
  if (!items.length) return;
  const now = Date.now();
  for (const item of items) {
    const key = OUTBOX_PREFIX + item.taskId;
    if (now - (item.ts || 0) > OUTBOX_MAX_AGE) {
      await _outboxRemove(item.taskId);
      console.warn("[Vault] 发件箱条目已过期，丢弃：[" + item.taskId + "] " + item.lastError);
      continue;
    }
    try {
      const done = await _outboxDeliver(item);
      if (done) {
        await _outboxRemove(item.taskId);
        console.log("[Vault] 已补发任务结果 [" + item.taskId + "]");
      } else {
        await _outboxBump(key, "服务端返回 5xx 或不可达");
      }
    } catch (e) {
      await _outboxBump(key, (e && e.message) || String(e));
    }
  }
}

// 落盘 → 立即尝试投递一次；投不出去就留在发件箱，由闹钟兜底。
async function _report(taskId, payload, what) {
  const keepalive = startSwKeepalive();
  try {
    await _outboxAdd(taskId, payload);
    await flushOutbox();
    const left = (await _outboxAll()).some((i) => i.taskId === taskId);
    if (left) {
      console.warn("[Vault] " + what + "[" + taskId + "] 暂未送达，已存入发件箱，" +
        "将由后台闹钟自动补发（扩展设置里请确认服务地址与 Token）");
    }
    return !left;
  } finally {
    stopSwKeepalive(keepalive);
  }
}

async function submitResult(taskId, data) {
  return await _report(taskId, { taskId, result: data }, "上报抓取结果 ");
}

async function submitError(taskId, error) {
  // 这里以前是「发出去就不管」，失败也不看、不报 —— 后果很隐蔽：
  // 服务端永远不知道任务失败了，任务会一直停在「已发送给浏览器」，
  // 界面数分钟后才冒出「可能已卡住」，而真正的原因（401 / 服务没起 / 地址不对）
  // 一个字都没留下。现在走发件箱：发不出去也会被后台闹钟补发，并留下 lastError。
  return await _report(taskId, { taskId, error }, "上报失败原因 ");
}

// 交给后端适配器抓取（POST /api/clip-url）。
// 给「扩展没有页面内适配器」的站点用 —— 例如 YouTube：
// 它的字幕既不在 DOM 里、扩展也没写页面内适配器，只能由服务端的 yt-dlp 去取。
async function sendToClipUrl(url, force, liveSource) {
  const settings = await chrome.storage.local.get("serverUrl");
  const serverUrl = (settings.serverUrl || "http://localhost:7878").replace(/\/$/, "");
  const headers = await getAuthHeaders();

  try {
    const res = await fetch(serverUrl + "/api/clip-url", {
      method: "POST",
      headers: headers,
      body: JSON.stringify({
        url: url,
        liveSource: liveSource === undefined ? autoDetectLiveSource(url) : liveSource,
        force: !!force,
        wikiSync: true,   // 入库 = raw + wiki，等结论
      }),
    });
    if (res.status === 401) {
      return { success: false, error: "鉴权失败（401）：请在扩展设置中填写与 tools/config.local.json 一致的 Token" };
    }
    return await res.json();
  } catch (e) {
    return { success: false, error: "无法连接本地服务，请确认 clip-server.py 已启动" };
  }
}

// 存待审区：在 raw/inbox/ 建一张指针卡片（不抓正文、不生成 wiki，待后续人工处理）
async function sendToInbox(payload) {
  const settings = await chrome.storage.local.get("serverUrl");
  const serverUrl = (settings.serverUrl || "http://localhost:7878").replace(/\/$/, "");
  const headers = await getAuthHeaders();

  const res = await fetch(serverUrl + "/api/inbox", {
    method: "POST",
    headers: headers,
    body: JSON.stringify(payload),
  });
  return await res.json();
}

// ====== 跨域 GET 代理（给 content script 用）======
//
// 为什么必须有它：**Chrome 85 起，content script 的网络请求按「宿主页面的 origin」发出**
// （不再借用扩展的 origin），因此受宿主页 CORS 约束 —— 从 B 站页面直连
// api.bilibili.com 会直接抛 `Failed to fetch`（实测就是这样）。
// 而 background 属于扩展上下文：只要 manifest 声明了 host_permissions，
// 它的请求**豁免 CORS**（连不带任何 CORS 头的本地服务都能读）。
//
// 本仓库的剪藏链路一直是这个模式（content script → background → localhost），
// B 站字幕也必须照做。
const PROXY_ALLOW_SUFFIX = [".bilibili.com", ".hdslb.com"];

function _proxyAllowed(url) {
  try {
    const u = new URL(url);
    if (u.protocol !== "https:") return false;
    return PROXY_ALLOW_SUFFIX.some(s => u.hostname === s.slice(1) || u.hostname.endsWith(s));
  } catch (e) {
    return false;
  }
}

async function proxyFetchJson(url, withCredentials) {
  if (!_proxyAllowed(url)) {
    return { success: false, error: "地址不在跨域白名单内：" + String(url).slice(0, 90) };
  }
  try {
    const res = await fetch(url, withCredentials === false ? {} : { credentials: "include" });
    const text = await res.text();
    let json = null;
    try { json = JSON.parse(text); } catch (e) { /* 非 JSON */ }
    return {
      success: true,
      status: res.status,
      json: json,
      text: json ? "" : text.slice(0, 300),
    };
  } catch (e) {
    return { success: false, error: String((e && e.message) || e) };
  }
}

// 监听来自 content script 的剪藏请求（悬浮按钮）
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.action === "clip-direct" && msg.data) {
    // 如果没有显式设置 liveSource，按 URL 自动判断
    if (msg.data.liveSource === undefined) {
      msg.data.liveSource = autoDetectLiveSource(msg.data.url);
    }
    // fromPopup：这条请求来自插件面板。**无条件**补一条系统通知 —— 用户可能
    // 已经关掉弹窗去看别的网页，或者切到了别的标签页；结论都该主动送达。
    // 与悬浮按钮 / 选中剪藏 / 浏览器协助三条路径保持一致（完成即通知）。
    //
    // 另一件事：请求改由 background 发出后，**重开的弹窗学不到结论** ——
    // 它只会在 storage 里读到上一次写下的 loading（「正在覆盖…」），然后一直
    // 停在那儿（用户报「已经入库成功了怎么还是提示正在覆盖」）。
    // 所以这里在完成时把**最终结论**写回弹窗读的同一个 key，重开的弹窗
    // 便能直接显示真实结果，而不是永远卡在过程态。
    const fromPopup = !!msg.fromPopup;
    sendToServer(msg.data).then(async result => {
      try { sendResponse(result); } catch (e) { /* 通道已断开 */ }
      if (fromPopup) {
        await persistPopupResult(result);
        await notifyClipResult(result);
      }
    }).catch(async e => {
      try { sendResponse({ ok: false, error: e.message }); } catch (err) { /* ignore */ }
      if (fromPopup) {
        const errResult = { success: false, error: e.message };
        await persistPopupResult(errResult);
        await notifyClipResult(errResult);
      }
    });
    return true; // 保持 sendResponse 通道
  }

  // 悬浮按钮/右键 →「跨 frame 汇总选中文本」（正文在异构 iframe 内的站点）
  //
  // 页面侧受同一源策略限制，顶层读不到跨域 iframe 的选区；只有扩展注入的
  // 脚本能进到每个 frame。这里从 sender.tab 拿当前标签页，逐 frame 询问。
  if (msg.action === "selection-probe") {
    const tabId = sender && sender.tab && sender.tab.id;
    if (tabId == null) {
      sendResponse({ success: false, error: "无法确定标签页" });
      return true;
    }
    probeSelection(tabId).then(hit => {
      sendResponse({
        success: true,
        text: (hit && hit.text) || "",
        title: (hit && hit.frameTitle) || "",
        url: (hit && hit.url) || "",
      });
    }).catch(e => {
      sendResponse({ success: false, error: e.message });
    });
    return true;
  }

  // 插件面板 →「跨 frame 汇总正文」
  //
  // 为什么要有这个通道：popup 自身跑在扩展上下文里，**没有页面的 DOM**，
  // 无法直接读 iframe 内容；而 chrome.tabs.sendMessage 默认只到顶层 frame。
  // 正文若整块在跨域 iframe（典型：某商户平台 iUrl 套壳页），顶层只能拿到骨架文字。
  // 这里由 background 用 allFrames:true 逐 frame 提取，取最丰富的一个。
  if (msg.action === "extract-all-frames") {
    const tabId = (msg.tabId != null)
      ? msg.tabId
      : (sender && sender.tab && sender.tab.id);
    if (tabId == null) {
      sendResponse({ success: false, error: "无法确定标签页" });
      return true;
    }
    (async () => {
      // 先补注入一次 content script：若该页在扩展更新前就已打开，页面里
      // 没有 registry，逐 frame 提取会全部返回 null。
      try {
        await chrome.scripting.executeScript({
          target: { tabId, allFrames: true },
          files: CONTENT_SCRIPT_FILES,
        });
      } catch (e) { /* 已注入则忽略 */ }

      const picked = await extractAcrossFrames(tabId);
      if (!picked || !picked.data) {
        sendResponse({ success: false, error: "提取失败（页面未返回内容）" });
        return;
      }
      const data = picked.data;
      try {
        const topInfo = await getTopFrameInfo(tabId);
        if (topInfo.title) data.title = topInfo.title;
        if (topInfo.url) data.url = topInfo.url;
      } catch (e) { /* 取不到就用 frame 内的 */ }
      sendResponse({ success: true, data });
    })().catch(e => sendResponse({ success: false, error: e.message }));
    return true;
  }

  // 悬浮按钮 →「存待审区」
  // 说明：完整入库不需要单独的动作 —— 内容由 content script 抽取好之后，
  // 直接走上面的 clip-direct → /api/clip 即可（这正是"页面内抓取"的好处：
  // 不需把 URL 交给后端、也就不需要后端持有登录态）。
  // content script 的跨域 GET 一律由这里代发（见上面 PROXY_ALLOW_SUFFIX 的说明）
  if (msg.action === "fetch-json" && msg.url) {
    proxyFetchJson(msg.url, msg.credentials).then(sendResponse);
    return true;
  }

  // 悬浮按钮 →「交给后端适配器抓取」（YouTube 这类扩展没有页面内适配器的站点）
  if (msg.action === "clip-url" && msg.url) {
    sendToClipUrl(msg.url, false, msg.liveSource)
      .then(sendResponse)
      .catch(e => sendResponse({ success: false, error: e.message }));
    return true;
  }

  if (msg.action === "inbox-add" && msg.url) {
    const payload = {
      url: msg.url,
      title: msg.title || "",
      liveSource: msg.liveSource === undefined
        ? autoDetectLiveSource(msg.url)
        : msg.liveSource,
    };
    sendToInbox(payload).then(result => {
      sendResponse(result);
    }).catch(e => {
      sendResponse({ success: false, error: e.message });
    });
    return true;
  }
});

// ====== 核心剪藏函数 ======

// 从标签页的**所有 frame** 汇总选中文本，返回命中 frame 的结果或 null。
//
// 为什么不能只问顶层：正文常被放在跨域 iframe 里（典型如某商户经营平台
// merchant.example.com 的 iUrl 套壳页，正文在 h5.example.com）。顶层 frame 的
// window.getSelection() **看不到**异构 iframe 里的选区 —— 用户手确实选中了，
// 但顶层读到空串，于是误报「未检测到选中文本」。
// allFrames:true 让注入脚本在每个 frame 内各自读取本 frame 的选区，
// 再取第一个非空的作为待入库正文。
//
// 返回：{ text, frameTitle, url } 或 null。
async function probeSelection(tabId) {
  const frames = await chrome.scripting.executeScript({
    target: { tabId, allFrames: true },
    func: () => {
      try {
        const sel = window.getSelection();
        if (!sel || sel.isCollapsed) return null;
        const text = sel.toString().trim();
        if (!text) return null;
        return {
          text,
          frameTitle: document.title || "",
          url: location.href,
        };
      } catch (e) {
        return null;  // 取不到就当该 frame 没选中
      }
    },
  });

  const hit = (frames || []).map(f => f && f.result).find(r => r && r.text);
  return hit || null;
}

// 从标签页的**所有 frame** 汇总正文，选出「内容最丰富」的那一个。
//
// 为什么必须跨 frame：部分站点把正文整块放进跨域 iframe，顶层只是一个套壳。
// 典型如某商户经营平台 merchant.example.com 的 iUrl 套壳页（正文在 h5.example.com）——
// 顶层静态 HTML 只有 132 字的一句「Please enable JavaScript」，iframe 里才是
// 用户看到的几千字正文。**「页面上看得见」与「扩展抓得到」是两回事**：
// 顶层脚本读不到跨域 iframe 的 DOM，只有扩展注入的脚本不受同源策略限制。
//
// 分两步：① **唤醒**每个 frame 的懒加载内容（逐 frame 滚动）；② 用 allFrames:true
// 在各 frame 内跑同一套平台适配器，各自算出本 frame 的正文长度，取最长的一个。这样：
//   - 普通页面（正文就在顶层）→ 顶层胜出，行为与改动前一致；
//   - 套壳页（正文在 iframe）→ iframe 胜出，不再误报「未检测到有效正文」；
//   - 懒加载长文 → 第二步滚完再提取，拿到的是全篇而非首屏。
//
// 返回：{ data } 或 null。
async function extractAcrossFrames(tabId) {
  // 先「唤醒」每个 frame 里的懒加载内容，再提取。
  //
  // 为什么需要这一步：长正文常按滚动位置分批渲染（无限滚动 / IntersectionObserver），
  // 而剪藏发生时用户可能只把页面滚到一半。此时 iframe 里**只有首屏**已经在 DOM 中，
  // 抓到的就是「半篇」（用户报的「只抓到 667 字」，而完整版 2601 字，正是这个形态）。
  // 这不是提取失败，是内容还没渲染 —— 所以要先滚一遍把下游节点逼出来，再提取。
  //
  // 必须逐 frame 滚：正文在跨域 iframe 里时，滚顶层窗口对 iframe 内部的滚动容器
  // 没有任何作用。用 allFrames:true 让每个 frame 滚它自己的文档。
  try {
    await chrome.scripting.executeScript({
      target: { tabId, allFrames: true },
      func: () => {
        const sleep = ms => new Promise(r => setTimeout(r, ms));
        // 滚到底再回顶：既触发懒加载，也避免把用户停在页面底部。
        // 用异步 IIFE，但 executeScript 不等待它 —— 只保证滚动**已开始**，
        // 真正的等待放在下面的固定延时里（简单可靠，不必依赖返回值）。
        (async () => {
          try {
            const doc = document.scrollingElement || document.documentElement;
            const full = Math.max(doc.scrollHeight, document.body ? document.body.scrollHeight : 0);
            const viewport = window.innerHeight || 800;
            // 分几步滚，给懒加载留出触发窗口；步数设上限，避免超长页面拖太久。
            const steps = Math.min(12, Math.max(2, Math.ceil(full / viewport)));
            for (let i = 1; i <= steps; i++) {
              window.scrollTo(0, Math.round(full * i / steps));
              await sleep(120);
            }
            window.scrollTo(0, 0);
          } catch (e) { /* 忽略 */ }
        })();
        // 同时滚内部的滚动容器（部分站点正文放在 overflow:auto 的 div 里，
        // 滚 window 无用）。只处理「明显比视口高」的少数容器，避免误伤。
        try {
          const cands = document.querySelectorAll("main, article, [class*=scroll], [class*=content], [role=main]");
          let n = 0;
          for (const el of cands) {
            if (n >= 3) break;
            if (el.scrollHeight > (window.innerHeight || 800) * 1.5) {
              el.scrollTop = el.scrollHeight;
              n++;
            }
          }
        } catch (e) { /* 忽略 */ }
      },
    });
    // 等滚动与懒加载完成。取 800ms：短到不至于让剪藏明显变慢，
    // 长到足够让一次网络请求 + DOM 插入完成。
    await new Promise(r => setTimeout(r, 800));
  } catch (e) { /* 滚不动就按原样提取 */ }

  let frames = [];
  try {
    frames = await chrome.scripting.executeScript({
      target: { tabId, allFrames: true },
      // 复用页面里的平台适配器 + 通用提取。content.js 已在本函数调用前
      // 注入到所有 frame（见调用点的 CONTENT_SCRIPT_FILES）。
      func: () => {
        try {
          const reg = window.__clipVault && window.__clipVault.registry;
          if (!reg) return null;
          // 平台适配器只在它匹配的 frame 起作用；非匹配 frame 走通用提取。
          const p = reg.detect();
          const data = (p && typeof p.extractContent === "function")
            ? p.extractContent(location.href)
            : window.__clipVault.base.extractGenericContent(location.href);
          return data;
        } catch (e) {
          return null;
        }
      },
    });
  } catch (e) {
    return null;
  }

  const results = (frames || [])
    .map(f => f && f.result)
    .filter(d => d && typeof d.content === "string");

  if (!results.length) return null;

  // 取正文最长的一个。用去空白后的长度比较：套壳页顶层常有一堆 UI 文案，
  // 直接数字符会被它骗过去。
  const norm = s => String(s || "").replace(/\s/g, "").length;
  // 留存逐 frame 的长度快照，供排查「为什么只有 N 字」——
  // 投诉时最需要知道的是「一共扫到几个 frame、各多少字、胜出的是谁」，
  // 只报一个总数无法判断是「没扫到 iframe」还是「扫到了但里面也少」。
  const diag = results.map(d => ({
    url: String(d.url || "").slice(0, 120),
    title: String(d.title || "").slice(0, 60),
    n: norm(d.content),
    type: d.type || "",
  }));
  results.sort((a, b) => norm(b.content) - norm(a.content));
  const winner = results[0];
  winner._frameDiag = diag;
  return { data: winner };
}

// 取顶层页面的标题 / URL（iframe 的 title 往往很泛，优先用顶层的）
async function getTopFrameInfo(tabId) {
  try {
    const top = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => ({ title: document.title || "", url: location.href }),
    });
    if (top && top[0] && top[0].result) return top[0].result;
  } catch (e) { /* 顶层取不到就返回空 */ }
  return { title: "", url: "" };
}

async function clipSelection(tab) {
  try {
    try {
      await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        files: CONTENT_SCRIPT_FILES,
      });
    } catch (e) { /* 已注入则忽略 */ }

    const hit = await probeSelection(tab.id);
    if (!hit) {
      notify("❌ 未检测到选中文本", "请先选中要入库的文本");
      return;
    }

    // 顶层页面自身的标题 / URL 优先，回退到命中 frame 的 title / url
    const topInfo = await getTopFrameInfo(tab.id);

    const data = {
      title: topInfo.title || hit.frameTitle || tab.title || hit.url || "选中文本",
      url: topInfo.url || tab.url || hit.url || "",
      content: hit.text,
      type: "selection",
      platform: "generic",
      selectedText: true,
      images: [],
      // 渠道标记：任务面板据此把「选中剪藏」与其他入口区分开
      channel: "selection",
    };

    // 与「入库此页」「悬浮按钮」保持同一口径：入库 = raw + wiki，
    // 要求服务端等 wiki 出结论再返回。
    //
    // 此前这里没设 wikiSync，服务端立刻返回 success=true —— 于是无论 wiki
    // 成功、跳过还是**降级**（降级时 raw 已被撤回、filename 是空串），
    // 通知都报「✅ 选中文本已入库 → undefined 字」，用户以为入库完成，
    // 实际可能什么都没留下。必须等结论、并按结论分档提示。
    data.wikiSync = true;

    await applyImageFilter(data);
    const result = await sendToServer(data);

    if (result.success) {
      const w = result.wiki;
      if (w && w.status === "demoted") {
        // 降级：raw 已撤回、条目留在待审区，绝不能报「已入库」
        const byContent = w.kind === "content_incomplete";
        notify(byContent ? "⚠️ 未入库：内容不完整" : "⚠️ 未入库：wiki 未生成",
          String(w.reason || "未知原因").slice(0, 150) +
          "\n条目已存入待审区，可在扩展面板中重试。");
      } else if (w && w.status === "skipped") {
        notify("✅ 已入库（wiki 跳过）",
          (result.wordCount || 0) + " 字 → " + (result.filename || "") +
          "\n" + String(w.reason || "").slice(0, 120));
      } else if (w && w.status === "pending") {
        notify("✅ 已入库（wiki 后台生成中）",
          (result.wordCount || 0) + " 字 → " + (result.filename || ""));
      } else {
        notify("✅ 已入库（含 wiki）",
          (result.wordCount || 0) + " 字 → " + (result.filename || ""));
      }
    } else {
      notify("❌ 入库失败", result.error || "未知错误");
    }
  } catch (e) {
    notify("❌ 入库失败", e.message);
  }
}

async function clipTab(tab) {
  try {
    try {
      await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        files: CONTENT_SCRIPT_FILES,
      });
    } catch (e) { /* 已注入则忽略 */ }

    // 同样跨 frame 汇总：悬浮按钮「入库此页」与浏览器协助走同一口径，
    // 否则同一个页面换个入口结果就不一样（用户抱怨过的那种不一致）。
    const picked = await extractAcrossFrames(tab.id);
    if (!picked || !picked.data || !picked.data.content) {
      throw new Error("提取失败（页面未返回内容）");
    }
    const response = { success: true, data: picked.data };

    try {
      const topInfo = await getTopFrameInfo(tab.id);
      if (topInfo.title) response.data.title = topInfo.title;
      if (topInfo.url) response.data.url = topInfo.url;
    } catch (e) { /* 取不到就用 frame 内的 */ }

    response.data.wikiSync = true;   // 「入库此页」= 完整入库（raw + wiki）
    response.data.channel = "fab";   // 悬浮按钮：「入库此页」
    await applyImageFilter(response.data);
    const result = await sendToServer(response.data);

    if (result.success) {
      if (result.wiki && result.wiki.status === "demoted") {
        const byContent = result.wiki.kind === "content_incomplete";
        notify(byContent ? "⚠️ 入库未完成：内容不完整" : "⚠️ 入库未完成：wiki 未生成",
          String(result.wiki.reason || "未知原因").slice(0, 150) +
          "\n已转存待审区：" + (result.wiki.inbox_card || "（条目已存在）"));
      } else if (isWikiBad(result.wiki)) {
        notify("⚠️ raw 已入库，wiki 未生成",
          String(result.wiki.reason || "未知原因").slice(0, 160) + "\n→ " + result.filename);
      } else {
        notify("✅ 完整入库",
          result.wordCount + " 字 → " + result.filename + wikiNote(result.wiki));
      }
    } else {
      notify("❌ 入库失败", result.error || "未知错误");
    }
  } catch (e) {
    notify("❌ 入库失败", e.message);
  }
}

// wiki 结论 → 通知文案后缀
function wikiNote(w) {
  if (!w) return "";
  if (w.status === "ok") return "（含 wiki）";
  if (w.status === "pending") return "（wiki 后台生成中）";
  if (w.status === "skipped") return "（wiki 跳过：" + String(w.reason || "").slice(0, 80) + "）";
  if (w.status === "demoted") {
    return w.kind === "content_incomplete"
      ? "（raw 已撤回：未获取到字幕/转录，已转存待审区）"
      : "（raw 已撤回，转存待审区）";
  }
  return "（wiki 未生成）";
}

// wiki 没成功 ≠ 入库成功 —— raw 写了但 wiki 没写，不能报「完整入库」
function isWikiBad(w) {
  return !!w && w.status !== "ok" && w.status !== "pending";
}

// 素材不完整（未获取到字幕/转录）→ 与「wiki 生成失败」分开措辞：
// 前者是来源本身没给出素材，后者是生成环节出错，用户的下一步动作不同。
function demoteIsContent(w) {
  return !!w && w.status === "demoted" && w.kind === "content_incomplete";
}

// 抓取被风控 / 人机验证拦截（kind="interstitial"）。
// 与「素材不完整」「wiki 失败」都不同：**这次根本没抓到正文**，
// 处置是「在浏览器里过掉验证再重试」，不是「重剪」也不是「补 wiki」。
function demoteIsBlocked(w) {
  return !!w && w.status === "demoted" && w.kind === "interstitial";
}

// wiki 结论 → 给用户看的一行。**必须与 popup.js 的 wikiLine() 逐字一致** ——
// 同一个结果从通知和从插件面板看到的说法不同，用户会以为出了两回事
// （2026-10-09 报过：插件显示「⚠️ raw 已入库，但 wiki 未生成」，系统通知却
// 显示「✅ 已入库」，两条通知互相打架）。
function wikiLineText(w, wikiFile) {
  if (!w) return "";
  if (w.status === "ok") return "wiki：✅ " + (wikiFile || "已生成");
  if (w.status === "pending") return "wiki：⏳ 后台生成中";
  if (w.status === "skipped") return "wiki：⏭ 已跳过（" + (w.reason || "") + "）";
  if (demoteIsContent(w)) return "wiki：⏭ 未生成（素材不完整：未获取到字幕/转录）";
  if (w.status === "demoted") return "wiki：❌ 未生成（raw 已撤回）";
  return "wiki：❌ 未生成 —— " + (w.reason || "未知原因");
}

// ⚠️ 一致性约束：这套「入库结论」标题必须与 popup.js 的 INGEST_TITLE
// **逐字一致**（含标点、空格、emoji）—— 见 popup.js 同名常量的详细说明。
// 改了任何一处都要同步另一处，test_routing2.js ㊹ 有逐条对账断言。
const INGEST_TITLE = {
  demoted: "⚠️ 入库未完成：wiki 未生成",
  demotedContent: "⚠️ 入库未完成：内容不完整",
  demotedBlocked: "⚠️ 入库未完成：抓取被风控拦截",
  noFile: "⚠️ 入库未完成：服务端未返回文件",
  wikiBad: "⚠️ raw 已入库，但 wiki 未生成",
  ok: "✅ 入库成功！",
  dup: "📌 已存在（未重复入库）",
};

// 统一的「入库结论」文案生成器。
//
// 为什么要有这个函数：一次剪藏的结果会经由**四条路径**送达用户 ——
// ① 插件面板状态框（popup reportIngest）、② 系统通知（notifyClipResult）、
// ③ 重开弹窗时的回显（persistPopupResult/popup 的 waitForBackgroundResult）、
// ④ 飞书卡片。过去每条路径各写一套 if-else，判据还不一样：
//   - popup 用 isWikiBad() 判 wiki；
//   - notifyClipResult 只看 result.success → **wiki 未生成也报「✅ 已入库」**。
// 于是同一个事件在插件里是「⚠️ wiki 未生成」，在通知里是「✅ 已入库」。
// 现在四路共用这一份：返回值 { type, title, body }，谁都不许自己拼结论。
function buildIngestOutcome(result) {
  const r = result || {};
  const w = r.wiki || null;

  if (!r.success) {
    if (r.error === "duplicate") {
      return { type: "dup", title: INGEST_TITLE.dup,
               body: r.existing_file || r.existingFile || "该链接已在知识库中" };
    }
    return { type: "error", title: "❌ 入库未完成",
             body: (r.error || "未知原因") + "（可在「📋 任务队列」查看）" };
  }

  // wiki 未生成时 raw 已被撤回，此时不能再报「已入库」
  if (w && w.status === "demoted") {
    const byContent = demoteIsContent(w);
    const byBlocked = demoteIsBlocked(w);
    return {
      type: "error",
      title: byContent ? INGEST_TITLE.demotedContent
        : byBlocked ? INGEST_TITLE.demotedBlocked
          : INGEST_TITLE.demoted,
      body: (w.reason || "未知原因") +
            "\n已转存待审区：" + (w.inbox_card || "（条目已存在）"),
    };
  }

  // 兜底：服务端回了「成功但无文件」——不能渲染成存在一个不存在的文件
  if (!r.filename && !r.filepath) {
    return {
      type: "error",
      title: INGEST_TITLE.noFile,
      body: (w && w.reason ? "原因：" + w.reason + "\n" : "") +
            "请在「📋 任务队列 → 最近记录」核实本次结果。",
    };
  }

  const body = "Raw：" + (r.filename || r.filepath) +
               "\n字数：" + (r.wordCount != null ? r.wordCount : "—") +
               (w ? "\n" + wikiLineText(w, r.wikiFile) : "") +
               // 服务端判定「有图片没能本地化」时必须说出来：那些图只留了远程地址，
               // 源站一删就没了。过去这种情况只在服务端日志里，用户以为拿到的是完整快照。
               (r.imageWarning ? "\n⚠️ " + r.imageWarning : "") +
               // 服务端只拿到静态骨架（动态渲染页）→ 这份 raw 可能不完整。
               (r.dynamicRenderNote ? "\n⚠️ " + r.dynamicRenderNote : "");

  if (isWikiBad(w)) {
    return { type: "error", title: INGEST_TITLE.wikiBad, body };
  }
  return { type: "success", title: INGEST_TITLE.ok, body };
}

// 根据设置过滤图片
async function applyImageFilter(data) {
  const settings = await chrome.storage.local.get("saveImages");
  if (settings.saveImages === false) {
    data.images = [];
    data.skipImages = true;
  }
}

async function sendToServer(data) {
  const settings = await chrome.storage.local.get("serverUrl");
  const serverUrl = (settings.serverUrl || "http://localhost:7878").replace(/\/$/, "");
  const headers = await getAuthHeaders();

  try {
    const res = await fetch(serverUrl + "/api/clip", {
      method: "POST",
      headers: headers,
      body: JSON.stringify(data),
    });
    if (res.status === 401) {
      return { success: false, error: "鉴权失败（401）：请在扩展设置中填写与 tools/config.local.json 一致的 Token" };
    }
    return await res.json();
  } catch (e) {
    return { success: false, error: "无法连接本地服务，请确认 clip-server.py 已启动" };
  }
}

async function notify(title, message) {
  try {
    const settings = await chrome.storage.local.get("showNotifications");
    if (settings.showNotifications === false) return; // 用户关闭了通知
    // iconUrl 必须是**存在的**资源，否则 chrome.notifications.create 会直接失败
    // （不报错、只是通知不出现）—— 此前 icon.png 从未随扩展分发，
    // 于是所有完成通知都静默消失了。icon128.png 现在随 manifest 一起打包。
    const iconUrl = chrome.runtime.getURL("icon128.png");
    chrome.notifications.create("", {
      type: "basic",
      iconUrl,
      title,
      message: String(message || "").slice(0, 300),
    }, (id) => {
      if (chrome.runtime.lastError) {
        console.warn("[Vault] 通知发送失败：" + chrome.runtime.lastError.message);
      }
    });
  } catch (e) {
    console.warn("[Vault] 通知异常：", e);
  }
}

// 插件面板剪藏完成后补一条系统通知。
//
// 为什么**无条件**发（与悬浮按钮 / 选中剪藏 / 浏览器协助三条路径一致）：
//   ① 用户在插件面板点「立即完整入库」后，可能**随手关掉弹窗去看别的网页**
//      —— 这是完全正常的操作。请求已在 background 里发出、会照常完成，
//      但弹窗已经不在了，用户看不到结论；补通知才不会让他以为「点了没反应」
//      而重复剪藏。
//   ② 即使弹窗还开着，用户也可能**已经切到别的标签页**，弹窗并非视线焦点。
//      结论都该主动送达，不能假设用户正盯着弹窗。
//   把「弹窗在不在」当条件反而制造了一条与其他路径不同的规则 —— 同一个事件
//   换个入口行为就不一样，正是本项目反复踩过的坑。故统一为：完成即通知。
async function notifyClipResult(result) {
  const o = buildIngestOutcome(result);
  // 命中去重不是失败，也不必打扰（与既有约定一致）。
  if (o.type === "dup") return;
  // **标题与正文都取自 buildIngestOutcome**，与插件面板同一份判据。
  // 这里绝不能再自己判一次 result.success —— 那正是「插件说 wiki 未生成、
  // 通知却说 ✅ 已入库」的成因。
  await notify(o.title, o.body);
}

// 把插件面板这次剪藏的**最终结论**写回 popup 读取的 storage key。
//
// 为什么必须由 background 来写：请求已改由 background 发出（为的是弹窗被关掉时
// 剪藏不丢），但这样一来，**重开的弹窗就学不到结论了**。它只会读到上一次写下的
// `loading:true`（「正在覆盖并重新生成 wiki…」），然后永远停在那儿 ——
// 用户看到的现象正是「明明已经入库成功，插件还提示正在覆盖」。
// 这里与 popup 的 `showStatus` 用同一个 key、同一种结构，
// 弹窗打开时便能直接显示真实结论。
async function persistPopupResult(result) {
  try {
    // 与 notifyClipResult / popup reportIngest 共用同一份结论生成器，
    // 保证「重开弹窗看到的」与「通知里说的」「面板当时显示的」三者完全一致。
    const o = buildIngestOutcome(result);
    const type = o.type === "error" ? "error" : "success";
    const msg = o.title + "\n" + o.body;
    await chrome.storage.local.set({
      lastProcessResult: { type, msg, at: Date.now(), loading: false },
    });
  } catch (e) { /* ignore */ }
}

function waitForTabLoad(tabId) {
  return new Promise((resolve) => {
    let resolved = false;
    const listener = (id, info) => {
      if (id === tabId && info.status === "complete") {
        chrome.tabs.onUpdated.removeListener(listener);
        if (!resolved) { resolved = true; resolve(); }
      }
    };
    chrome.tabs.onUpdated.addListener(listener);
    setTimeout(() => {
      if (!resolved) {
        resolved = true;
        chrome.tabs.onUpdated.removeListener(listener);
        resolve();
      }
    }, 15000);
  });
}

// ====== 保活：MV3 service worker 会被回收，SSE 也跟着断 ======
//
// 这是浏览器协助链路原本最致命的一处：connectToServer() 只挂在
// onInstalled / onStartup 上，而 MV3 的 service worker 空闲约 30 秒就会被
// 回收 —— 连同 EventSource 和那个 5 秒重连定时器一起消失。回收之后
// **再也没有任何事件**能把它叫醒，于是扩展"装好后连过一次、之后永远是断的"：
// 服务端把任务排进浏览器队列却没人来领，面板显示「进行中」，用户只能干等。
//
// 两道保险：
//   1. 顶层调用 —— SW 每次被唤醒（任何扩展事件）都会执行到这里，顺手重连
//   2. chrome.alarms 心跳 —— 每分钟把 SW 叫醒一次，发现断开就重连
const KEEPALIVE_ALARM = "vault-keepalive";

async function ensureKeepalive() {
  if (!chrome.alarms) return;              // 权限缺失时降级，不拖垮 SW
  const existing = await chrome.alarms.get(KEEPALIVE_ALARM);
  if (!existing) chrome.alarms.create(KEEPALIVE_ALARM, { periodInMinutes: 1 });
}

if (chrome.alarms) {
  chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name !== KEEPALIVE_ALARM) return;
    if (!eventSource || eventSource.readyState !== 1) {
      console.log("[Vault] 心跳：SSE 未连接，重连中…");
      connectToServer().catch(() => {});
    }
    // 补发发件箱：上一次上报若因 SW 被回收而没发出去，这里兜底补上。
    flushOutbox().catch((e) => console.warn("[Vault] 补发发件箱异常：", e));
    // 续跑被回收打断的在办任务（这才是「任务执行了却没回传」的正面解法）
    resumePendingTasks().catch((e) => console.warn("[Vault] 续跑在办任务异常：", e));
  });
}

function vaultBootstrap() {
  try {
    ensureKeepalive().catch(() => {});
    if (!eventSource || eventSource.readyState !== 1) {
      connectToServer().catch((e) => console.log("[Vault] 连接服务端失败:", e));
    }
    // SW 每次被唤醒都先把积压补发掉、并续跑被回收打断的任务（重启后尤其重要）
    flushOutbox().catch(() => {});
    resumePendingTasks().catch(() => {});
  } catch (e) {
    console.log("[Vault] 保活初始化失败:", e);
  }
}

vaultBootstrap();

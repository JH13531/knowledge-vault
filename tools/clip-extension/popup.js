const inboxBtn = document.getElementById("inboxBtn");
const clipBtn = document.getElementById("clipBtn");
const statusEl = document.getElementById("status");
const pageInfoEl = document.getElementById("pageInfo");
const serverUrlInput = document.getElementById("serverUrl");
const tokenInput = document.getElementById("token");
const liveSourceCheckbox = document.getElementById("liveSource");
// 「提取图片文字（OCR）」开关：**默认勾选**（= 命中「图多字少」就自动跑）。
// 默认必须是「跑」——它是「忘了选也不会漏」的唯一保证；取消勾选只是主动叫停。
const ocrImagesCheckbox = document.getElementById("ocrImages");
const inboxPanel = document.getElementById("inboxPanel");
const inboxList = document.getElementById("inboxList");
const processAllBtn = document.getElementById("processAllBtn");
const inboxToggle = document.getElementById("inboxToggle");
const inboxToggleCount = document.getElementById("inboxToggleCount");
const duplicatePanel = document.getElementById("duplicatePanel");
const duplicateInfo = document.getElementById("duplicateInfo");
const overwriteBtn = document.getElementById("overwriteBtn");
const settingsToggle = document.getElementById("settingsToggle");
const settingsBody = document.getElementById("settingsBody");
const queueBtn = document.getElementById("queueBtn");
const queueBtnBadge = document.getElementById("queueBtnBadge");
// 任务队列（右栏，横向展开）
const taskPanel = document.getElementById("taskPanel");
const taskStats = document.getElementById("taskStats");
const taskList = document.getElementById("taskList");
const taskHistory = document.getElementById("taskHistory");
const taskMoreBtn = document.getElementById("taskMoreBtn");
const activeCountEl = document.getElementById("activeCount");
const queueConnDot = document.getElementById("queueConnDot");
const queueConnText = document.getElementById("queueConnText");

// 保存当前重复信息（供覆盖操作使用）
let currentDuplicateInfo = null;
let currentTabUrl = "";
let currentPageTaskId = null;
const inboxProcessing = new Set();
let lastInboxHtml = "";
let lastTaskStatsHtml = "";
let lastTaskListHtml = "";
let lastTaskHistoryHtml = "";
let queueExpanded = false;
let historyLimit = 10;
let todayOnly = true;

// live-source 自动检测的平台域名
const LIVE_SOURCE_DOMAINS = [
  "feishu.cn", "larksuite.com",
  "notion.so", "notion.site",
  "docs.google.com",
  "confluence.", "atlassian.",
];

// 全局配置（从 options 页面读取）
let clipConfig = {
  saveImages: true,
  showNotifications: true,
  defaultClipMode: "ask",
};

// 加载设置
chrome.storage.local.get(["serverUrl", "token", "defaultClipMode", "saveImages", "showNotifications"]).then(result => {
  serverUrlInput.value = result.serverUrl || "http://localhost:7878";
  tokenInput.value = result.token || "";
  clipConfig.defaultClipMode = result.defaultClipMode || "ask";
  clipConfig.saveImages = result.saveImages !== false; // 默认 true
  clipConfig.showNotifications = result.showNotifications !== false;
  loadInbox();
  loadTaskStats();

  // 恢复上次的处理结论。弹窗一旦失焦就被销毁，上一次的轮询结果（含失败原因）
  // 只写在 storage 里 —— 不恢复的话，用户重开弹窗就再也看不到「为什么没成」。
  chrome.storage.local.get([LAST_RESULT_KEY]).then(r => {
    const last = r[LAST_RESULT_KEY];
    if (!last || !last.msg) return;
    // 只在状态框还空着时恢复（避免覆盖本次刚产生的状态）
    if (statusEl.textContent) return;
    const ageMin = (Date.now() - (last.at || 0)) / 60000;
    if (last.loading) {
      // 「正在入库」是**过程态**，不是结论，绝不能长期回显。
      //
      // 踩过（用户报「一打开插件就显示正在入库，我根本没点任何按钮」）：
      // 上一次剪藏把 loading 写进了 storage；若请求在中途失败，或弹窗在失败前
      // 就被关掉，这个 loading 就再也没人改写成错误态 —— 于是每次打开弹窗都
      // 原样回显，看起来像凭空开始了一个任务。
      //
      // 现在多了一层：请求已改由 background 发出，完成后会把**最终结论**写回
      // 这个 key（见 background 的 persistPopupResult）。所以重开弹窗时若读到
      // loading，先回显它、同时**轮询等待 background 写入结论** —— 否则用户会
      // 看到「已经入库成功了，插件还一直提示正在覆盖」。
      if (ageMin > 2) {
        // 超过 2 分钟仍停在 loading，多半是请求早已中断，没有再等的意义。
        try { chrome.storage.local.remove(LAST_RESULT_KEY); } catch (e) { /* ignore */ }
        return;
      }
      showStatus("loading", last.msg);
      waitForBackgroundResult(last.at || 0, last.msg);
      return;
    }
    if (ageMin > 60) return;   // 超过 1 小时的旧结论不再回显，避免误导
    // 历史遗留：这条 key 里可能已经堆了多份注解（旧版本的自增 bug 写进去的）。
    // 这里做一次**自愈** —— 只取第一份注解之前的裸结论，顺手把干净的写回去，
    // 用户不必手动清 storage 也能恢复。
    const clean = stripAgeNotes(last.msg);
    if (clean !== last.msg) {
      try {
        chrome.storage.local.set({
          [LAST_RESULT_KEY]: { type: last.type, msg: clean, at: last.at, loading: false },
        });
      } catch (e) { /* ignore */ }
    }
    // ⚠️ 「（以上为上次操作的结论…）」这行是**展示用的注解**，只能渲染在屏幕上，
    // 绝不能再写回 storage —— 否则下次打开会在这串文字后面**再拼一遍**，
    // 每开一次弹窗就多一行（用户报「怎么这么多」就是这个自增）。
    // 所以这里显式带 `{ persist: false }`，storage 里始终只存**裸结论**。
    showStatus(last.type, withAgeNote(clean, ageMin), { persist: false });
  });

  // 核实「上次未跑完的任务」现在到底成没成。
  //
  // 为什么需要：Chrome 弹窗**一失焦就被销毁**，而「处理待审区」要等浏览器抓取
  // 加服务端生成 wiki，通常几十秒到几分钟。弹窗一关，内存里的 taskId 就没了，
  // 用户重新打开只能看到一片空白 —— 既看不到「还在跑」，也看不到「已经成了」。
  // 所以把在办任务一并落盘，重开时主动去服务端问一次最终结论。
  resumeInFlightTask();

  // 根据默认模式自动执行
  if (clipConfig.defaultClipMode === "inbox") {
    setTimeout(() => inboxBtn.click(), 100);
  } else if (clipConfig.defaultClipMode === "full") {
    setTimeout(() => clipBtn.click(), 100);
  }
});

serverUrlInput.addEventListener("change", () => {
  chrome.storage.local.set({ serverUrl: serverUrlInput.value.trim() });
});

tokenInput.addEventListener("change", () => {
  chrome.storage.local.set({ token: tokenInput.value.trim() });
});

// 获取当前标签页信息
chrome.tabs.query({ active: true, currentWindow: true }).then(tabs => {
  const tab = tabs[0];
  if (tab) {
    currentTabUrl = tab.url || "";
    pageInfoEl.innerHTML = "<strong>标题：</strong>" + escapeHtml(tab.title || "") +
      "<br><strong>链接：</strong>" + escapeHtml(tab.url || "");

    // 自动检测 live-source 平台
    const url = (tab.url || "").toLowerCase();
    if (LIVE_SOURCE_DOMAINS.some(d => url.includes(d))) {
      liveSourceCheckbox.checked = true;
    }

    // 加载用户上次选择
    chrome.storage.local.get(["liveSourceDefault"]).then(result => {
      if (result.liveSourceDefault === true && !liveSourceCheckbox.checked) {
        liveSourceCheckbox.checked = true;
      }
    });
    // OCR 开关同样记住上次选择；**未设置过 = 勾选**（默认提取）
    chrome.storage.local.get(["ocrImagesDefault"]).then(result => {
      if (result.ocrImagesDefault === false) {
        ocrImagesCheckbox.checked = false;
      }
    });
  }
});

// 记住用户的手动选择
liveSourceCheckbox.addEventListener("change", () => {
  chrome.storage.local.set({ liveSourceDefault: liveSourceCheckbox.checked });
});
ocrImagesCheckbox.addEventListener("change", () => {
  chrome.storage.local.set({ ocrImagesDefault: ocrImagesCheckbox.checked });
});

// ====== 打开高级设置 ======
const openOptionsLink = document.getElementById("openOptions");
if (openOptionsLink) {
  openOptionsLink.addEventListener("click", (e) => {
    e.preventDefault();
    // 先尝试 API，失败则直接打开文件
    if (chrome.runtime.openOptionsPage) {
      chrome.runtime.openOptionsPage(() => {
        if (chrome.runtime.lastError) {
          // 如果 options_ui 未注册，直接在新标签打开
          chrome.tabs.create({ url: chrome.runtime.getURL("options.html") });
        }
      });
    } else {
      chrome.tabs.create({ url: chrome.runtime.getURL("options.html") });
    }
  });
}

// ====== 设置区折叠 ======
settingsToggle.addEventListener("click", () => {
  const open = settingsBody.classList.toggle("open");
  settingsToggle.classList.toggle("open", open);
});

// ====== 右上角任务队列按钮 ======
//
// 点击后弹窗**横向变宽**（340px → 704px），右侧并排显示任务队列。
// 为什么不是「向下展开」也不是「另开窗口」：
//   · 向下展开会把「立即完整入库」等按钮推出可视区，观感突兀（用户反馈过）；
//   · 另开窗口则离开了插件本身，多一个窗口要管理。
//   · 横向并排：入库操作与任务列表同屏可见，弹窗仍是同一个。
// Chrome 弹窗尺寸跟随文档尺寸，改 body 宽度即可让弹窗变宽。
//
// 再点一次收起回 340px；重复点击不叠加。
function setQueueExpanded(open) {
  queueExpanded = !!open;
  document.body.classList.toggle("expanded", queueExpanded);
  queueBtn.classList.toggle("open", queueExpanded);
  if (queueExpanded) {
    // 展开时立刻渲染一次，不等 3 秒轮询
    refreshTaskPanel().catch(() => {});
  }
}

queueBtn.addEventListener("click", () => {
  setQueueExpanded(!queueExpanded);
});

// 右栏的「展开更多历史」。最近记录永远只展示当日（todayOnly 恒为 true），
// 不再提供「全部」切换 —— 见 popup.html 的筛选区只留静态「今天」标签。
taskMoreBtn.addEventListener("click", () => {
  historyLimit += 20;
  loadTaskHistory().catch(() => {});
});

// 供 loadTaskStats 的 3 秒轮询调用：只在展开时刷队列内容，
// 收起状态只需更新角标与按钮锁定（避免白白拉 50 条历史）。
async function refreshTaskPanel() {
  const settings = await chrome.storage.local.get(["serverUrl", "token"]);
  const serverUrl = (settings.serverUrl || "http://localhost:7878").replace(/\/$/, "");
  const token = settings.token || "";
  const headers = {};
  if (token) headers["Authorization"] = `Bearer ${token}`;

  if (!queueExpanded) return;

  try {
    // loadTaskStats 负责指标卡 + 进行中列表 —— 必须随展开立刻刷，
    // 否则指标卡要等下一个 3 秒轮询节拍才出现（2026-10-15 用户反馈
    // 「指标卡弹出有延迟」：它此前只在轮询里渲染）。
    await Promise.all([
      loadTaskStats(),
      loadTaskList(serverUrl, headers),
      loadTaskHistory(serverUrl, headers),
    ]);
    setQueueConn(true, "已连接 " + serverUrl.replace(/^https?:\/\//, ""));
  } catch (e) {
    setQueueConn(false, "无法连接服务（" + ((e && e.message) || "未知错误") + "）");
  }
}

function setQueueConn(ok, text) {
  if (!queueConnDot) return;
  queueConnDot.className = "dot " + (ok ? "on" : "off");
  queueConnText.textContent = text;
}

// ====== 按钮：存入待审区（只创建指针卡片） ======

inboxBtn.addEventListener("click", async () => {
  showStatus("loading", "正在存入待审区…");
  inboxBtn.disabled = true;
  clipBtn.disabled = true;

  try {
    const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
    const tab = tabs[0];
    const url = tab.url || "";
    const title = tab.title || "";

    if (!url) {
      throw new Error("无法获取当前页面 URL");
    }

    const result = await sendToInbox(url, title);

    if (result.success) {
      showStatus("success", "📥 已存入待审区\nRaw：" + result.filename);
      loadInbox();
    } else if (result.error === "already_archived") {
      // 内容早已完整入库 —— 待审区的语义是「尚未入库、只留了链接待处理」，
      // 所以这里**不建卡**，也不能报成「已在待审区中」（那是另一回事）。
      showStatus("success", "📌 该链接已入库，无需再存入待审区" +
        (result.existing_file ? "\n已有文件：" + result.existing_file : ""));
    } else if (result.error === "duplicate") {
      showStatus("success", "📌 已在待审区中");
    } else {
      throw new Error(result.error || "存入失败");
    }
  } catch (e) {
    showStatus("error", "❌ " + connectionHint(e));
  } finally {
    inboxBtn.disabled = false;
    clipBtn.disabled = false;
  }
});

async function sendToInbox(url, title) {
  const settings = await chrome.storage.local.get(["serverUrl", "token"]);
  const serverUrl = (settings.serverUrl || "http://localhost:7878").replace(/\/$/, "");
  const token = settings.token || "";

  const headers = { "Content-Type": "application/json" };
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }

  const res = await fetch(serverUrl + "/api/inbox", {
    method: "POST",
    headers: headers,
    body: JSON.stringify({
      url: url,
      title: title,
      liveSource: liveSourceCheckbox.checked,
    }),
  });

  return await res.json();
}

// ====== 已知平台域名 ======
// 判断逻辑已抽到 platforms/known-sites.js（popup.html 与 manifest.json 都会加载它），
// 因为 content.js 里的悬浮按钮也要用同一份列表，而两个 JS 上下文作用域不通。
const isKnownPlatform = (...a) => window.__clipVault.isKnownPlatform(...a);
// 「只有服务端有适配器的站点」才跳过页面内抓取（判定见 known-sites.js）。
// 注意只需要这一个 —— 其余情况一律先试页面内抓取。
const needsServerAdapter = (...a) => window.__clipVault.needsServerAdapter(...a);

// fetch 失败时只会抛 "Failed to fetch"，从异常本身看不出原因，
// 所以统一翻译成可操作的排查指引（最常见是服务端没在跑）。
function connectionHint(e) {
  const msg = String((e && e.message) || e);
  if (!/failed to fetch|networkerror|load failed|无法连接/i.test(msg)) {
    return msg;
  }
  return msg +
    "\n\n浏览器未能连接本地服务（与 Token 无关），请依次排查：" +
    "\n1. clip-server.py 是否仍在运行（终端关闭或会话结束会导致其停止）" +
    "\n2. 本机是否启用了系统代理 —— 代理会拦截 localhost，请将" +
    "\n   localhost、127.0.0.1 加入代理例外名单" +
    "\n3. 服务地址是否正确（建议使用 http://127.0.0.1:7878：" +
    "\n   部分系统会将 localhost 优先解析为 IPv6 的 ::1，而服务端默认仅监听 IPv4）";
}

// ====== 按钮：立即完整入库 ======

// 隐藏重复提示面板
function hideDuplicatePanel() {
  duplicatePanel.classList.remove("open");
  currentDuplicateInfo = null;
}

// 页面内抓取：让当前标签页的 content script 抽取内容（与悬浮按钮同一套逻辑）。
// 抽不到（没有 content script / 正文过短）返回 null，由调用方决定回退策略。
async function extractInPage(tab) {
  if (!tab || tab.id === undefined) return null;
  try {
    // 走 background 的「跨 frame 汇总」通道，而不是只问顶层 frame。
    //
    // 为什么：正文常整块放在跨域 iframe 里（典型如某商户经营平台的 iUrl 套壳页，
    // 顶层只有一句「Please enable JavaScript」，正文在 h5.example.com）。
    // popup 自身没有页面 DOM，只能请 background 用 allFrames:true 逐 frame 提取；
    // 否则会误判「页面内未检测到有效正文」而白白降级。
    const res = await chrome.runtime.sendMessage({
      action: "extract-all-frames",
      tabId: tab.id,
    });
    if (!res || !res.success || !res.data) return null;
    const text = String(res.data.content || "").trim();
    if (text.length < 50) return null;
    return res.data;
  } catch (e) {
    // chrome:// 页、或扩展刚重载后该页的 content script 已失效
    return null;
  }
}

// 降级到待审区的两种原因 —— 措辞不同，处置提示也不同
function demoteIsContent(w) {
  return w && w.status === "demoted" && w.kind === "content_incomplete";
}

// 抓取被风控 / 人机验证拦截（kind="interstitial"）。
// 与「素材不完整」「wiki 失败」都不同：**这次根本没抓到正文**，
// 处置是「在浏览器里过掉验证再重试」，不是「重剪」也不是「补 wiki」。
function demoteIsBlocked(w) {
  return w && w.status === "demoted" && w.kind === "interstitial";
}

// 「内容不完整」的处置建议**必须按平台给出**。
// 此前这里固定为 YouTube 的 InnerTube 建议，导致 B 站剪藏失败时也显示该段，
// 与用户当时的操作无关，等同于没有提示。
function videoRetryHint() {
  const u = String(currentTabUrl || "").toLowerCase();
  if (/youtube\.com|youtu\.be/.test(u)) {
    return "\n\n（YouTube：请在该视频页停留数秒让它加载完成后再剪藏 ——" +
      "扩展需要从页面里读到字幕数据。若反复提示「要求登录」，" +
      "请确认浏览器已登录 YouTube，并刷新本页一次。）";
  }
  if (/bilibili\.com|b23\.tv/.test(u)) {
    return "\n\n（B 站字幕需要浏览器已登录该站点；若原因为「没有可用字幕」，" +
      "说明 UP 主未上传且平台未生成 AI 字幕，重新剪藏无效。）";
  }
  return "";
}

// wiki 结论 → 给用户看的一行
// wikiFile：wiki 页面**文件名**（服务端从 raw 的 wiki_refs 读到的权威值）。
// 有名字时直接显示它 —— 比一句「✅ 已生成」更有信息量，也便于回查。
function wikiLine(w, wikiFile) {
  if (!w) return "";
  if (w.status === "ok") return "\nwiki：✅ " + (wikiFile || "已生成");
  if (w.status === "pending") return "\nwiki：⏳ 后台生成中";
  if (w.status === "skipped") return "\nwiki：⏭ 已跳过（" + (w.reason || "") + "）";
  if (demoteIsContent(w)) return "\nwiki：⏭ 未生成（素材不完整：未获取到字幕/转录）";
  if (w.status === "demoted") return "\nwiki：❌ 未生成（raw 已撤回）";
  return "\nwiki：❌ 未生成 —— " + (w.reason || "未知原因");
}

function isWikiBad(w) {
  return !!w && w.status !== "ok" && w.status !== "pending";
}

// ⚠️ 一致性约束：下面这套「入库结论」的标题措辞必须与 background.js 的
// buildIngestOutcome() **逐字一致**（含标点、空格、emoji）。
//
// 为什么：一次剪藏的结果会经由四条路径送达用户 —— 插件面板状态框（本函数）、
// 系统通知（notifyClipResult）、重开弹窗的回显（persistPopupResult）、飞书卡片。
// 它们过去各写一套 if-else，判据还不一样，于是同一个事件会出现
// 「插件说 ⚠️ wiki 未生成、通知说 ✅ 已入库」这种互相打架的画面
// （用户 2026-10-09 报过）。改了任何一处都要同步另一处，
// test_routing2.js ㊹ 有逐条对账断言锁死。
const INGEST_TITLE = {
  demoted: "⚠️ 入库未完成：wiki 未生成",
  demotedContent: "⚠️ 入库未完成：内容不完整",
  demotedBlocked: "⚠️ 入库未完成：抓取被风控拦截",
  noFile: "⚠️ 入库未完成：服务端未返回文件",
  wikiBad: "⚠️ raw 已入库，但 wiki 未生成",
  ok: "✅ 入库成功！",
  dup: "📌 已存在（未重复入库）",
};

// 统一呈现入库结果。**wiki 失败时不报「成功」** ——
// 「完整入库」在本项目里的定义是 raw + wiki，只写进 raw 不算完整。
function reportIngest(result) {
  const w = result.wiki;

  // 服务端在入库成功（或命中去重）时会**摘掉同 URL 的待审指针** ——
  // 那条链接已经完整入库了，指针卡片就完成使命了。
  // 所以结果一出来立刻刷一次待审区，否则会同时看到「✅ 入库成功」和
  // 「待审区 2」这种自相矛盾的画面（要等 3 秒的轮询才追上）。
  loadInbox();
  // 这次已经成功入库，「要不要覆盖上一次」的问题也就不存在了
  hideDuplicatePanel();

  // demoted：服务端已把 raw 撤回、改存待审区。
  // 这不是「成功」，也不该显示一个已经不存在的文件名。
  // 两种触发：素材本身不完整（视频没字幕） / 素材完整但 wiki 生成失败。
  if (w && w.status === "demoted") {
    const byContent = demoteIsContent(w);
    const byBlocked = demoteIsBlocked(w);
    // 标题取自 INGEST_TITLE（与 background buildIngestOutcome 同源），
    // 后面的「raw 已撤回」是本面板独有的补充说明，通知里放不下故省略。
    const head = (byContent ? INGEST_TITLE.demotedContent
      : byBlocked ? INGEST_TITLE.demotedBlocked
        : INGEST_TITLE.demoted) + "，raw 已撤回";
    const hint = byContent
      ? videoRetryHint()
      : byBlocked
        ? "\n\n请在浏览器中打开该链接、完成人机验证后，再点待审区的「▶ 处理」重试。"
        : "\n\n通道恢复后可点待审区的「▶ 处理」重跑。";
    showStatus("error",
      head +
      "\n已转存待审区：" + (w.inbox_card || "（条目已存在）") +
      "\n原因：" + (w.reason || "未知原因") +
      hint +
      ((byContent || byBlocked) ? "" : "（若本次不需要生成 wiki，请改用「存入待审区」）"));
    return;
  }

  // 兜底：既没有 filename，wiki 又不是 demoted —— 说明服务端回了「成功但无文件」，
  // 这种情况绝不能渲染成「✅ 入库成功！文件：undefined」（曾经出现过）。
  // 按「未完成」处理并说明，让用户知道去哪里核实。
  if (!result.filename) {
    showStatus("error",
      INGEST_TITLE.noFile +
      (w && w.reason ? "\n原因：" + w.reason : "") +
      "\n请在「📋 任务队列 → 最近记录」核实本次结果。");
    return;
  }

  const bad = isWikiBad(w);
  showStatus(bad ? "error" : "success",
    (bad ? INGEST_TITLE.wikiBad : INGEST_TITLE.ok) +
    // 「Raw」而非「文件」：与 wiki 行并排时，三层架构（raw / wiki）更清楚
    "\nRaw：" + result.filename +
    "\n字数：" + result.wordCount + wikiLine(w, result.wikiFile) +
    // 与 background 的 buildIngestOutcome 同一份提示：图片没全本地化要说出来
    (result.imageWarning ? "\n⚠️ " + result.imageWarning : "") +
    // 服务端只拿到静态骨架时也要说：这份 raw 可能明显少于浏览器中的实际页面。
    // 此前只有飞书卡片会提示，弹窗路径静默。
    (result.dynamicRenderNote ? "\n⚠️ " + result.dynamicRenderNote : ""));
}

clipBtn.addEventListener("click", async () => {
  clipBtn.disabled = true;
  inboxBtn.disabled = true;
  hideDuplicatePanel();

  try {
    const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
    const tab = tabs[0];
    const url = tab.url || "";

    // ---- 首选：页面内抓取，和悬浮按钮**完全同一条路径** ----
    //
    // 为什么不再优先走 /api/clip-url：那条路要求**后端自己**持有登录态。
    // Windows 上 Chrome 的 cookie 是 App-Bound 加密的，yt-dlp 读不了，
    // 于是必然失败并降级到「浏览器协助」—— 而浏览器协助会**另开一个标签页**
    // 去抓，这就是画面里突然多出来的「重复网页」。
    // 页面内抓取用的是你当前标签页的登录态（cookie 本来就在页面里），
    // 不需要后端解密密 cookie、不会开新页，B 站还能直接取到字幕。
    showStatus("loading", "正在页面内抓取内容…");
    // YouTube 这类「扩展没有适配器、服务端有专用适配器」的站点**必须跳过页面内提取**：
    // 页面内只能拿到 DOM（字幕根本不在里面），而标题+简介通常已超过 50 字，
    // 不会触发任何回退 —— 结果就是静默产出一条没有字幕的废记录。
    // 置空 payload 直接走下面的后端适配器分支。
    const payload = needsServerAdapter(url) ? null : await extractInPage(tab);

    if (payload) {
      payload.liveSource = liveSourceCheckbox.checked;
      // 取消勾选 = 本次不做图片文字提取（命中「图多字少」时记为 skipped）。
      // 默认勾选，所以「忘了选」的后果是多跑一次 OCR，而不是内容残缺。
      payload.ocr = ocrImagesCheckbox.checked;
      payload.wikiSync = true;   // 「完整入库」= raw + wiki，要求服务端等出结论
      // 标明渠道：任务面板据此区分「插件面板 / 悬浮按钮 / 选中剪藏」，
      // 否则三条路径在列表里长得一样，用户无法判断结果差异来自哪里。
      payload.channel = "popup";
      if (!clipConfig.saveImages) {
        payload.images = [];
        payload.skipImages = true;
      }

      showStatus("loading", "正在入库并生成 wiki（可能需要数十秒）…");
      const result = await sendToServer(payload);

      if (result.success) {
        reportIngest(result);
      } else if (result.error === "duplicate") {
        showDuplicate(result);
      } else {
        throw new Error(result.error || "发送失败");
      }
      return;
    }

    // ---- 回退：页面内抓不到内容（无 content script / 正文过短）----
    if (isKnownPlatform(url)) {
      showStatus("loading", "页面内未提取到内容，已交由浏览器协助队列处理…");
      const result = await sendToClipUrl(url);

      // 先判 taskId：后端直抓失败会转入浏览器协助队列。
      // 注意不能先判 filename —— wiki 未生成时 raw 已被撤回、filename 是空串，
      // 那样会被后面的 else 误报成「抓取失败」。
      if (result.success && result.taskId) {
        showStatus("loading", "⏳ 后端无法直接抓取，已加入浏览器协助队列，等待浏览器处理…");
        const task = await awaitInflight(result.taskId, url);
        if (task.status === "done" && task.result) {
          const r = task.result;
          if (r.success && r.filename) {
            reportIngest(r);
          } else if (r.error === "duplicate") {
            // 任务 status=done 但 result.success=false —— 被去重闸门拦下，
            // **不是失败**：文件已存在、内容未丢失。
            // ⚠️ 此前这里只判断 status==="done"，会误报成
            //    「✅ 入库成功！文件：undefined 字数：undefined」。
            showStatus("duplicate", "📌 该链接已存在，未重复入库" +
              (r.existing_file ? "\n已有文件：" + r.existing_file : "") +
              "\n（正常去重，内容未丢失）");
          } else {
            throw new Error(r.error || "入库失败（服务端结果中缺少文件名）");
          }
        } else if (task.status === "failed") {
          throw new Error(task.error || "浏览器协助抓取失败");
        } else {
          // 任务仍在队列中 → 尚无结论。**必须给出具体原因**，不能只说
          // 「仍在队列中」而让用户无从判断（截图里那种「失败不提示原因」）。
          const why = await explainTaskStall(result.taskId);
          showStatus("loading",
            "⏳ 已加入浏览器协助队列（任务 " + result.taskId + "），尚未得到结论：\n" + why);
        }
      } else if (result.success) {
        reportIngest(result);
      } else if (result.error === "duplicate") {
        showDuplicate(result);
      } else {
        throw new Error(result.error || "抓取失败");
      }
      return;
    }

    throw new Error("页面内未检测到有效正文。" +
      "\n（搜索页、空白页、纯框架页没有可入库的正文；" +
      "若当前是文章页，请刷新页面后重试 —— 扩展更新后，未刷新的旧页面会失去连接）");
  } catch (e) {
    showStatus("error", "❌ " + connectionHint(e));
  } finally {
    clipBtn.disabled = false;
    inboxBtn.disabled = false;
  }
});

// 把剪藏 payload 交给 **background service worker** 发出去，而不是弹窗自己发。
//
// 为什么必须这样（用户 2026-10-09 的疑问）：「剪藏之后随手关掉弹窗去看别的网页」
// 是**完全正常**的操作 —— 弹窗一失焦就被销毁，如果请求是弹窗自己发起的，
// 它会被连同弹窗一起中止：服务端只收到半截连接（WinError 10053），
// **这次剪藏就凭空丢了**，而用户以为已经剪好了。
// background 的 service worker 生命周期独立于弹窗，请求发在那儿就不会跟着消失。
// 这也与本项目既有的 MV3 铁律一致：跨步骤流程必须「落盘 + 可续跑」。
async function sendToServer(data) {
  // 兜底超时：请求虽由 background 发出（不随弹窗消失），但弹窗仍在等它的结论。
  // 服务端若卡死（或连接半开），这个 Promise 会一直挂着、状态框停在 loading。
  // 超时值与后台一致地留足余量（wikiSync 实际耗时数十秒）。
  const timeoutMs = 180000;
  let resp;
  try {
    resp = await Promise.race([
      chrome.runtime.sendMessage({ action: "clip-direct", data: data, fromPopup: true }),
      new Promise((_, reject) => setTimeout(
        () => reject(new Error("__timeout__")), timeoutMs)),
    ]);
  } catch (e) {
    if (e && e.message === "__timeout__") {
      throw new Error("超过 3 分钟未收到结论" +
        "\n（内容通常已写入知识库，但 wiki 是否生成未能确认。）" +
        "\n请打开「📋 任务队列 → 最近记录」核实本次结果，再决定是否重试。");
    }
    // 扩展上下文失效（例如扩展刚被重载）时 sendMessage 会抛错。
    throw new Error("无法与后台服务通信，请在扩展管理页重载扩展后重试。");
  }
  if (!resp) {
    // 后台没有回 JSON（极少数：worker 被回收且消息通道断开）。
    // 内容可能已写入 —— 与超时同口径，引导用户去任务队列核实，不要重复剪藏。
    throw new Error("后台未返回结论" +
      "\n（内容通常已写入知识库，但无法确认。）" +
      "\n请打开「📋 任务队列 → 最近记录」核实本次结果，再决定是否重试。");
  }
  return resp;
}

// 发送 URL 到 clip-server（走平台适配器后端抓取）
async function sendToClipUrl(url, force = false) {
  const settings = await chrome.storage.local.get(["serverUrl", "token"]);
  const serverUrl = (settings.serverUrl || "http://localhost:7878").replace(/\/$/, "");
  const token = settings.token || "";

  const headers = { "Content-Type": "application/json" };
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }

  const res = await fetch(serverUrl + "/api/clip-url", {
    method: "POST",
    headers: headers,
    body: JSON.stringify({
      url: url,
      liveSource: liveSourceCheckbox.checked,
      ocr: ocrImagesCheckbox.checked,   // 取消勾选 = 本次不做图片文字提取
      force: force,
      channel: force ? "popup-overwrite" : "popup",
    }),
  });

  return await res.json();
}

async function pollTaskResult(taskId) {
  const settings = await chrome.storage.local.get(["serverUrl", "token"]);
  const serverUrl = (settings.serverUrl || "http://localhost:7878").replace(/\/$/, "");
  const token = settings.token || "";
  const headers = {};
  if (token) headers["Authorization"] = `Bearer ${token}`;

  // 等待上限必须**长于服务端真正判定卡死的时刻**，不能只写「等于阈值」：
  // 阈值是 600 秒（task_queue.STALE_PROCESSING_SECONDS），但清扫每 60 秒才跑一轮
  // （task_queue.start_sweeper），所以 failed 落库的时机落在 600～660 秒之间。
  // 写成 600000 就是撞平 —— 服务端刚要写结论、这边已经放弃，只好退回到下面的
  // STAGE_REASON 去猜阶段。取 720000 才能真正读到服务端那句精确原因
  // （实测任务 2283fe28 就是这样丢掉结论、只剩一张「尚未收到结果」的）。
  const deadline = Date.now() + 720000;
  while (Date.now() < deadline) {
    await new Promise(r => setTimeout(r, 3000));
    const res = await fetch(serverUrl + "/api/task/" + taskId, { headers });
    const task = await res.json();
    if (task.status === "done" || task.status === "failed") return task;
  }
  return { status: "timeout" };
}

// 任务迟迟没有结论时，向服务端再问一次「它现在停在哪一步」，翻译成人话。
//
// 为什么需要它：即便等待上限已延长到 720 秒（略长于服务端的 600 秒阈值 + 清扫间隔），
// 仍有极少数情况（例如浏览器在抓取中途断开、扩展被重载）会让任务停在半路，
// 服务端尚未把它判成 failed。
// 若此时只显示一句「仍在队列中」，用户永远看不到真正的原因（是没浏览器接单？
// 还是浏览器接了却没回传？）—— 也就是截图里那种「失败却没提示原因」。
// 因此超时后仍要再问一次「它现在停在哪一步」，把阶段翻译成人话。
const STAGE_REASON = {
  "": "任务刚入队，尚未开始处理。",
  "waiting": "服务端已把它排给浏览器协助，但**尚未有浏览器接单**。" +
    "请确认浏览器处于开启状态，并在 chrome://extensions 重载本扩展。",
  "dispatched": "任务**已发送给浏览器，但结果一直没有回传**。" +
    "常见原因是扩展的 Service Worker 被回收、抓取标签页被关，" +
    "或该站点在浏览器中无法访问；可在扩展的 Service Worker 控制台查看 [Vault] 日志。",
  "browser_loading": "浏览器正在加载页面，请稍候。",
  "browser_extracting": "浏览器正在提取内容，请稍候。",
  "extracting": "正在提取内容，请稍候。",
  "server-fetch": "服务端正在抓取，请稍候。",
  "saving": "内容已取到，服务端正在写入知识库，请稍候。",
};

async function explainTaskStall(taskId) {
  try {
    const settings = await chrome.storage.local.get(["serverUrl", "token"]);
    const serverUrl = (settings.serverUrl || "http://localhost:7878").replace(/\/$/, "");
    const headers = {};
    if (settings.token) headers["Authorization"] = "Bearer " + settings.token;
    const res = await fetch(serverUrl + "/api/task/" + taskId, { headers });
    const task = await res.json();
    const stage = task.stage || "";
    const line = STAGE_REASON[stage] || ("当前阶段：" + (task.stageLabel || stage || "未知") + "。");
    if (task.maybeStuck) {
      return line + "\n（服务端标记该任务可能已卡住，稍后会自动判定为失败。）";
    }
    return line;
  } catch (e) {
    return "未能向服务端确认任务状态（" + ((e && e.message) || e) + "）。" +
      "请确认 clip-server 已启动、扩展设置里的服务地址与 Token 正确。";
  }
}

// 上次处理结论的持久化键。
//
// 为什么必须落盘：Chrome 弹窗**一失焦就被销毁**（点一下别处就没了），而
// 「处理待审区」要等浏览器抓取 + 服务端生成 wiki，可能几十秒到几分钟。
// 弹窗一关，轮询里所有的结果（成功/去重/**失败原因**）都随内存一起消失；
// 用户重新打开只能看到一片空白，无从知道刚才到底成没成、为什么没成 ——
// 这正是「再点击就无法显示错误原因」的成因。
// 所以把结论写进 chrome.storage，弹窗每次打开时恢复展示。
const LAST_RESULT_KEY = "lastProcessResult";

// 等到 background 把本次剪藏的**最终结论**写回 storage。
//
// 为什么需要：请求由 background 发出（弹窗关掉也不丢），但重开的弹窗只读到
// 上一次的 loading（「正在覆盖并重新生成 wiki…」）。background 完成后会调
// persistPopupResult 覆盖这个 key，所以这里轮询等待，读到非 loading 的条目
// 就刷新状态框 —— 否则用户会看到「已经入库成功，还一直提示正在覆盖」。
//
// 只在这个 loading 还**原样显示着**时才覆盖：用户若已自己点了别的按钮、
// 产生了新的状态，就不要再动它。
async function waitForBackgroundResult(sinceAt, placeholder, timeoutMs = 180000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    await new Promise(r => setTimeout(r, 1500));
    // 状态框内容已不是我们那条 loading（用户做了别的操作）→ 停止等待。
    if (statusEl.textContent !== placeholder) return;
    let cur;
    try {
      const r = await chrome.storage.local.get([LAST_RESULT_KEY]);
      cur = r[LAST_RESULT_KEY];
    } catch (e) { return; }
    if (!cur) return;
    // 出现一条「比开始等待时更新、且不是 loading」的结论就采用它。
    if (!cur.loading && (cur.at || 0) >= sinceAt) {
      // 这条结论 background 已经写进 storage 了，这里只负责显示，
      // 不必再回写一次（回写会把「只是看了一眼」也变成一次新的时间戳）。
      showStatus(cur.type, cur.msg, { persist: false });
      return;
    }
  }
}

function persistStatus(type, msg) {
  try {
    chrome.storage.local.set({
      // loading 一并落盘并标记 loading:true —— 重新打开弹窗时才知道
      // 「上次还没跑完」，从而去服务端核实，而不是当成没有发生。
      [LAST_RESULT_KEY]: { type, msg, at: Date.now(), loading: type === "loading" },
    });
  } catch (e) { /* ignore */ }
}

// ====== 重开弹窗后，核实上一次未完成的任务 ======

// 取得在办任务的关键信息（taskId + 它的原始链接）。
// 只存 ID 是不够的：服务端任务可能已被清理，用链接还能在待审区里判断
// 「条目是不是已经被摘掉了」（摘掉 = 其实已经成功）。
const INFLIGHT_KEY = "lastInflightTask";
let inflightTask = null;

function rememberInflight(taskId, url) {
  inflightTask = { taskId, url: url || "", at: Date.now() };
  try { chrome.storage.local.set({ [INFLIGHT_KEY]: inflightTask }); } catch (e) { /* ignore */ }
}

function forgetInflight() {
  inflightTask = null;
  try { chrome.storage.local.remove(INFLIGHT_KEY); } catch (e) { /* ignore */ }
}

// 任务停在这些阶段 = 还没轮到/还没派发出去，浏览器尚未接手。
function isPreDispatchStage(stage) {
  return stage === "" || stage === "waiting" || stage === "pending";
}

async function resumeInFlightTask() {
  let saved;
  try {
    const r = await chrome.storage.local.get([INFLIGHT_KEY]);
    saved = r[INFLIGHT_KEY];
  } catch (e) { return; }
  if (!saved || !saved.taskId) return;
  // 超过 1 小时的在办记录不再追 —— 服务端早已按卡死判定为失败。
  if (Date.now() - (saved.at || 0) > 3600000) { forgetInflight(); return; }

  const settings = await chrome.storage.local.get(["serverUrl", "token"]);
  const serverUrl = (settings.serverUrl || "http://localhost:7878").replace(/\/$/, "");
  const headers = {};
  if (settings.token) headers["Authorization"] = "Bearer " + settings.token;

  let task = null;
  try {
    const res = await fetch(serverUrl + "/api/task/" + saved.taskId, { headers });
    task = await res.json();
  } catch (e) {
    // 连不上服务端时不要把在办记录丢掉 —— 可能只是服务端临时不可达，
    // 用户修好后再打开弹窗仍应看到「上次未跑完」的提示。
    showStatus("loading", "⏳ 上次的任务 " + saved.taskId + " 仍在处理中。\n" +
      "（未能连上本地服务确认最新状态，请确认 clip-server 是否在运行。）");
    return;
  }

  // ---- 已有结论 ----
  if (task.status === "done" && task.result) {
    forgetInflight();
    const r = task.result;
    if (r.error === "duplicate") {
      showStatus("success", "📌 上次的任务已完成：该链接已存在于知识库，待审区条目已移除");
    } else if (r.wiki && r.wiki.status === "demoted") {
      showStatus("error", "⚠️ 上次的任务未入库，已退回待审区\n原因：" +
        (r.wiki.reason || "服务端未说明原因"));
    } else if (r.success) {
      showStatus("success", "✅ 上次的任务已完成入库\nRaw：" + (r.filename || "") +
        "\n字数：" + (r.wordCount || 0));
    } else {
      showStatus("error", "⚠️ 上次的任务未完成\n原因：" +
        (r.reason || r.error || "服务端未说明原因") +
        (r.inbox_kept ? "\n条目仍保留在待审区，可重试。" : ""));
    }
    loadInbox();
    return;
  }
  if (task.status === "failed") {
    forgetInflight();
    showStatus("error", "❌ 上次的任务失败\n原因：" + (task.error || "服务端未说明原因") +
      "\n条目仍保留在待审区，可重试。");
    loadInbox();
    return;
  }

  // ---- 仍在处理中 ----
  if (!isPreDispatchStage(task.stage)) {
    showStatus("loading",
      "⏳ 上次的任务 " + saved.taskId + " 仍在处理中：\n" +
      (task.stageLabel || task.stage || "处理中") + "\n（可关闭本窗口，完成后会弹出通知。）");
    return;
  }

  // 停在「尚未派发」阶段：说明扫描到它时服务端还没把它发给浏览器
  // （浏览器没接单，或扩展刚被重载、SSE 连接正在重建）。这种情况浏览器一重连
  // 服务端就会自动重派（task_queue.redispatch_stale_dispatched），所以这里
  // 不必自己造一个派发接口 —— 只如实告诉用户「还差什么」，别让他干等 600 秒。
  //
  // 注意：**不要**在这里调用「重试失败任务」那个接口 —— 它只接受
  // failed/done 状态，对 processing 中的任务是无效的。
  const why = await explainTaskStall(saved.taskId);
  showStatus("loading",
    "⏳ 上次的任务 " + saved.taskId + " 仍未得到结论：\n" + why +
    "\n（浏览器重新连接服务端时会自动重派该任务；也可关闭本窗口，" +
    "稍后在待审区重新点「▶ 处理」。）");
}

// 在办任务出结论前，前台轮询等待（popup 关闭则中断，由后台通知与
// 下次打开时的 resumeInFlightTask 兜底）。
async function awaitInflight(taskId, url) {
  rememberInflight(taskId, url);
  const task = await pollTaskResult(taskId);
  forgetInflight();
  return task;
}

// 把「上次操作的结论」这行注解拼到裸结论上，仅用于**屏幕展示**。
// 抽成函数是为了让「注解」与「结论」在代码层面就分开 —— 避免有人再把
// 渲染后的字符串写回 storage（那会导致每次开弹窗多一行，见恢复逻辑注释）。
function withAgeNote(msg, ageMin) {
  return msg + "\n\n（以上为上次操作的结论，发生于 " +
    Math.max(1, Math.round(ageMin)) + " 分钟前。）";
}

// withAgeNote 的逆运算：把历史遗留的注解全部剥掉，只留第一份注解前的裸结论。
// 旧版本 bug 会把渲染结果写回 storage，于是文本可能长成这样：
//   结论\n\n（以上为…）\n\n（以上为…）\n\n（以上为…）
// 这里截到**第一次**出现注解为止（注解本身连同前面的空行一起去掉），
// 让老用户不必手动清理也能恢复正常显示。
const AGE_NOTE_RE = /\n*\s*（以上为上次操作的结论[\s\S]*$/;
function stripAgeNotes(msg) {
  if (!msg) return msg;
  return msg.replace(AGE_NOTE_RE, "").trimEnd();
}

function showStatus(type, msg, opts) {
  if (statusEl.textContent && statusEl.textContent !== msg) {
    statusEl.animate([{ opacity: 0.4 }, { opacity: 1 }], { duration: 220, easing: "ease-out" });
  }
  statusEl.className = "status " + type;
  statusEl.textContent = msg;
  // LAST_RESULT_KEY / persistStatus 定义在下方，没有提升问题：
  // 首次调用发生在异步流程里，那时整个脚本已执行完毕。
  //
  // opts.persist === false：只改屏幕、**不落盘**。用于回显上一次的结论 ——
  // 那时屏幕上的文字带了「发生于 N 分钟前」这类注解，落盘会在下次打开时
  // 被再拼一遍，越滚越多。
  if (!opts || opts.persist !== false) {
    persistStatus(type, msg);
  }
}

// 收起状态框。去重命中时用 —— 那时信息统一交给下面的面板显示，
// 不再另写一份到状态框里（见 showDuplicate 的说明）。
function clearStatus() {
  statusEl.className = "status";
  statusEl.textContent = "";
}

// 显示重复提示和覆盖选项。
//
// **只留一个提示框**：面板里既有详情（已有文件 / 字数对比）、又有「强制覆盖」按钮。
// 以前这里还会再往状态框里写一遍「📌 已存在相同来源 + 文件路径」，
// 于是屏幕上并排出现两个内容几乎一样的框，看着像一次操作出了两次错。
function showDuplicate(result) {
  clearStatus();

  if (!result.canOverwrite) {
    // 兜底：服务端未给可覆盖能力时只报一句，不给按钮
    showStatus("duplicate", "📌 已存在相同来源，未重复入库" +
      (result.message ? "\n" + result.message : ""));
    hideDuplicatePanel();
    return;
  }

  currentDuplicateInfo = {
    existingFile: result.existing_file,
    existingWordCount: result.existing_word_count,
    newWordCount: result.new_word_count,
  };

  // 把两个字数**并列对比**并给出一句结论。
  // 此前只显示「新内容约 N 字」，用户无法判断该数字与谁比较、是否需要处理。
  const oldN = Number(result.existing_word_count) || 0;
  const newN = Number(result.new_word_count) || 0;
  let verdict;
  if (newN === oldN) {
    verdict = "两版字数相同，内容通常没有变化，一般无需覆盖。";
  } else if (newN > oldN) {
    verdict = `本次比已入库版本多 ${newN - oldN} 字，上一版可能不完整，建议覆盖。`;
  } else {
    verdict = `本次比已入库版本少 ${oldN - newN} 字，上一版更完整，一般无需覆盖。`;
    // 相差悬殊（不足一半）时给一句可执行的建议。
    //
    // 为什么必须给：这类页面的正文常由 JS 异步渲染，点剪藏时若正文尚未
    // 进入 DOM，扩展只能读到已渲染的那一部分，于是「本次抓取」数字明显偏小。
    // 这**不是**抓取功能失效，但用户只看到一个悬殊的数字，会反复重试并
    // 认为功能坏了。直接告诉用户「怎么做才拿得全」最有用。
    // 只给一句、只给一个办法 —— 用户明确要求文案保持简洁（2026-10-09）。
    if (oldN > 0 && newN < oldN / 2) {
      verdict += "\n（相差悬殊。建议直接在页面上选中正文后剪藏。）";
    }
  }

  duplicateInfo.textContent =
    "📌 已存在相同来源，未重复入库\n" +
    `已有版本：${result.existing_file}（${oldN} 字）\n` +
    `本次抓取：${newN} 字\n` +
    verdict;
  duplicatePanel.classList.add("open");
}

// 强制覆盖按钮
overwriteBtn.addEventListener("click", async () => {
  if (!currentDuplicateInfo) return;

  overwriteBtn.disabled = true;
  clipBtn.disabled = true;
  inboxBtn.disabled = true;
  showStatus("loading", "正在强制覆盖…");

  try {
    const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
    const tab = tabs[0];
    const url = tab.url || "";

    // 同样优先页面内抓取，但「只有服务端有适配器」的站点（YouTube 等）要跳过
    // （理由见「立即完整入库」处的注释）
    const payload = needsServerAdapter(url) ? null : await extractInPage(tab);

    if (payload) {
      payload.liveSource = liveSourceCheckbox.checked;
      payload.ocr = ocrImagesCheckbox.checked;   // 取消勾选 = 本次不做图片文字提取
      payload.force = true;       // 强制覆盖
      payload.wikiSync = true;    // 覆盖也按「完整入库」对待
      if (!clipConfig.saveImages) {
        payload.images = [];
        payload.skipImages = true;
      }

      showStatus("loading", "正在覆盖并重新生成 wiki（可能需要数十秒）…");
      const result = await sendToServer(payload);

      if (result.success) {
        reportIngest(result);
        hideDuplicatePanel();
      } else {
        throw new Error(result.error || "覆盖失败");
      }
      return;
    }

    // 回退：已知平台走后端适配器
    if (isKnownPlatform(url)) {
      showStatus("loading", "页面内未提取到正文，改用后端适配器抓取…");
      const result = await sendToClipUrl(url, true); // force=true

      if (result.success) {
        reportIngest(result);
        hideDuplicatePanel();
      } else {
        throw new Error(result.error || "覆盖失败");
      }
      return;
    }

    throw new Error("页面内未检测到有效正文，无法覆盖。请刷新页面后重试。");
  } catch (e) {
    showStatus("error", "❌ " + e.message);
  } finally {
    overwriteBtn.disabled = false;
    clipBtn.disabled = false;
    inboxBtn.disabled = false;
  }
});

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

// ====== 待审区 ======

inboxToggle.addEventListener("click", () => {
  const open = inboxPanel.classList.toggle("open");
  inboxToggle.classList.toggle("open", open);
});

async function loadInbox() {
  try {
    const settings = await chrome.storage.local.get(["serverUrl", "token"]);
    const serverUrl = (settings.serverUrl || "http://localhost:7878").replace(/\/$/, "");
    const headers = {};
    if (settings.token) {
      headers["Authorization"] = `Bearer ${settings.token}`;
    }

    const res = await fetch(serverUrl + "/api/inbox", { headers });
    if (!res.ok) return;

    const data = await res.json();
    const items = data.items || [];
    const count = items.length;

    if (count > 0) {
      inboxToggle.style.display = "flex";
      inboxToggleCount.textContent = count;
    } else {
      inboxToggle.style.display = "none";
      inboxToggle.classList.remove("open");
    }

    // 默认收起，仅由用户点击下拉行展开
    if (count === 0) {
      inboxPanel.classList.remove("open");
      inboxList.innerHTML = "";
      lastInboxHtml = "";
      return;
    }
    renderInboxItems(items);
  } catch (e) {
    // 静默失败
  }
}

function renderInboxItems(items) {
  const html = items.map(item => {
    let domain = item.source_url || "";
    try {
      domain = new URL(item.source_url).hostname.replace(/^www\./, "");
    } catch (e) {
      // 保留原始内容
    }
    const date = item.filename.slice(0, 10);
    const shortUrl = item.source_url.length > 48
      ? item.source_url.slice(0, 48) + "…" : item.source_url;
    const tags = [
      item.note ? "📝 " + escapeHtml(item.note) : "",
    ].filter(Boolean).join(" · ");
    const busy = inboxProcessing.has(item.filename);

    return `<div class="inbox-item">
      <div class="meta-line">${escapeHtml(domain)} · ${date}${tags ? " · " + tags : ""}</div>
      <div>${escapeHtml(shortUrl)}</div>
      <button class="inbox-process-btn" data-filename="${escapeHtml(item.filename)}"
        ${busy ? "disabled" : ""}>${busy ? "处理中…" : "▶ 处理"}</button>
    </div>`;
  }).join("");

  if (html === lastInboxHtml) return;
  lastInboxHtml = html;
  inboxList.innerHTML = html;

  inboxList.querySelectorAll(".inbox-process-btn").forEach(btn => {
    btn.addEventListener("click", () => processInboxItem(btn.dataset.filename));
  });
}

async function postInboxProcess(body) {
  const settings = await chrome.storage.local.get(["serverUrl", "token"]);
  const serverUrl = (settings.serverUrl || "http://localhost:7878").replace(/\/$/, "");
  const headers = { "Content-Type": "application/json" };
  if (settings.token) {
    headers["Authorization"] = `Bearer ${settings.token}`;
  }
  const res = await fetch(serverUrl + "/api/inbox/process", {
    method: "POST",
    headers: headers,
    body: JSON.stringify(body),
  });
  return await res.json();
}

async function processInboxItem(filename) {
  inboxProcessing.add(filename);
  loadInbox();
  showStatus("loading", "正在完整抓取：" + filename + " …");

  try {
    const result = await postInboxProcess({ filename });

    // ---- 排入浏览器协助队列（视频平台走这条：不在后端跑 yt-dlp）----
    // 服务端对 YouTube / B 站不再自己抓，而是交给扩展在页面内抓 ——
    // 后端那条路会弹控制台窗口、超时，且必然失败（见 clip-server.py 的注释）。
    if (result.success && result.pending && result.taskId) {
      showStatus("loading",
        "⏳ 已加入浏览器协助队列（任务 " + result.taskId + "）\n" +
        "扩展将在后台打开标签页抓取，完成后自动入库并移出待审区。\n" +
        "请保持浏览器处于开启状态。");
      const task = await awaitInflight(result.taskId, currentTabUrl);
      if (task.status === "done" && task.result) {
        const r = task.result;
        if (r.error === "duplicate") {
          showStatus("success", "📌 该链接已存在于知识库，待审区条目已移除");
        } else if (r.success) {
          reportIngest(r);
        } else {
          showStatus("error", "❌ 浏览器抓取未完成\n原因：" +
            (r.reason || r.error || "服务端未说明原因") +
            (r.inbox_kept ? "\n条目仍保留在待审区，可重试。" : ""));
        }
      } else if (task.status === "failed") {
        showStatus("error", "❌ 浏览器协助抓取失败\n原因：" +
          (task.error || "服务端未说明原因") +
          "\n条目仍保留在待审区。");
      } else {
        // 未等到结论。**必须给出具体原因**，不能只说「仍在队列中」——
        // 用户无从判断是「浏览器没接单」「浏览器接了但没回传」还是「抓取失败」，
        // 这正是「待审区失败却看不到原因」的成因。
        // 再查一次任务状态，把 stage（服务端已知的处理阶段）如实翻译出来。
        const why = await explainTaskStall(result.taskId);
        showStatus("loading",
          "⏳ 任务 " + result.taskId + " 尚未得到结论：\n" + why +
          "\n条目仍保留在待审区，可稍后重试。");
      }
      return;
    }

    if (result.success) {
      showStatus("success", "✅ 已入库并移出待审区\nRaw：" + result.filename +
        "\n字数：" + result.wordCount);
    } else if (result.error === "duplicate") {
      showStatus("success", "📌 已存在：" + result.existing_file + "\n待审区条目已自动移除");
    } else if (result.error === "wiki_failed") {
      // wiki 未生成 → raw 已撤回，条目**仍保留在待审区**（不会被误删）
      const byContent = result.kind === "content_incomplete";
      showStatus("error",
        (byContent
          ? "⚠️ 内容不完整，条目仍保留在待审区"
          : "⚠️ 未生成 wiki，条目仍保留在待审区") +
        "\n原因：" + (result.reason || "未知原因") +
        (byContent
          ? "\n\n视频缺少字幕/转录时不应入库（仅有标题与简介，等同于书签）。"
          : "\n\n通道恢复后可重新点击「▶ 处理」。"));
    } else {
      // 关键：**不得吞掉失败原因**。此前 result.error 为空时就只剩一句
      // 「处理失败」，用户无从判断发生了什么。
      showStatus("error",
        "❌ 处理失败，条目已保留，可重试\n原因：" +
        (result.reason || result.error || "服务端未说明原因"));
    }
  } catch (e) {
    showStatus("error", "❌ " + connectionHint(e));
  } finally {
    inboxProcessing.delete(filename);
    loadInbox();
  }
}

processAllBtn.addEventListener("click", async () => {
  processAllBtn.disabled = true;
  showStatus("loading", "正在依次完整抓取待审区全部条目，请稍候…");

  try {
    const result = await postInboxProcess({ all: true });
    const results = result.results || [];
    // 三类结局要分开数：**排入浏览器队列的不等于已完成**（卡片还在待审区，
    // 等浏览器抓完才摘）。以前把它算进「成功」并宣称「待审区已清空」，是谎报。
    const queued = results.filter(r => r.pending).length;
    const okCount = results.filter(r => !r.pending && (r.success || r.error === "duplicate")).length;
    const failCount = results.length - okCount - queued;

    // 排入浏览器队列的部分**不能就此收工**：任务还在后台跑，最后可能入库、
    // 可能命中去重、也可能降级退回待审区。必须等它们各自出结论，再按真实结局
    // 分别汇报 —— 否则界面永远停在「已发送给浏览器，结果一直没有回传」，
    // 而实际上任务早已完成（用户看到的就是「处理了却毫无反应」）。
    const queuedItems = results.filter(r => r.pending && r.taskId);
    if (queuedItems.length) {
      const settled = await Promise.all(
        queuedItems.map(async (it) => ({ it, task: await awaitInflight(it.taskId, it.url || "") })));
      let doneN = 0, dupN = 0, demotedN = 0, stalledN = 0;
      const detailLines = [];
      for (const { it, task } of settled) {
        if (task.status === "done" && task.result) {
          const r = task.result;
          if (r.error === "duplicate") {
            dupN++;
            detailLines.push("· " + it.taskId + "：已存在于知识库，待审区条目已移除");
          } else if (r.wiki && r.wiki.status === "demoted") {
            demotedN++;
            detailLines.push("· " + it.taskId + "：未入库，已退回待审区 —— " +
              (r.wiki.reason || "服务端未说明原因"));
          } else if (r.success) {
            doneN++;
            detailLines.push("· " + it.taskId + "：已入库");
          } else {
            stalledN++;
            detailLines.push("· " + it.taskId + "：未完成 —— " +
              (r.reason || r.error || "服务端未说明原因"));
          }
        } else if (task.status === "failed") {
          stalledN++;
          detailLines.push("· " + it.taskId + "：失败 —— " + (task.error || "服务端未说明原因"));
        } else {
          // 确实没等到结论 → 追问它现在停在哪一步，给出具体原因
          stalledN++;
          const why = await explainTaskStall(it.taskId);
          detailLines.push("· " + it.taskId + "：" + why);
        }
      }

      const parts = [];
      if (okCount) parts.push(okCount + " 条已入库");
      if (doneN) parts.push(doneN + " 条已入库");
      if (dupN) parts.push(dupN + " 条已存在（已移除待审条目）");
      if (demotedN) parts.push(demotedN + " 条未入库（已退回待审区）");
      if (stalledN) parts.push(stalledN + " 条尚未得出结论");
      if (failCount) parts.push(failCount + " 条失败（条目已保留，可重试）");
      if (!parts.length) parts.push("没有可处理的条目");

      const bad = demotedN + stalledN + failCount;
      showStatus(bad ? (demotedN || failCount ? "error" : "loading") : "success",
        (bad ? "⚠️ " : "✅ ") + parts.join("；") +
        "\n\n明细：\n" + detailLines.join("\n") +
        (demotedN || stalledN
          ? "\n\n退回待审区的条目可在浏览器中处理阻碍后重试（如完成人机验证）。"
          : ""));
      return;
    }

    const parts = [];
    if (okCount) parts.push(okCount + " 条已入库");
    if (failCount) parts.push(failCount + " 条失败（条目已保留，可重试）");
    if (!parts.length) parts.push("没有可处理的条目");

    if (failCount > 0) {
      showStatus("error", "⚠️ " + parts.join("；"));
    } else {
      showStatus("success", "✅ 全部处理完成：" + parts.join("；"));
    }
  } catch (e) {
    showStatus("error", "❌ " + connectionHint(e));
  } finally {
    processAllBtn.disabled = false;
    loadInbox();
  }
});

// ====== 任务状态展示 ======
//
// 两件事：
//   ① 队列按钮角标显示活跃任务数（不展开也知道有没有在跑）；
//   ② 展开后渲染右栏任务队列（指标卡 + 进行中 + 最近记录）；
//   ③ 判断「本页是否有任务在跑」以锁定入库按钮，避免重复提交。
async function loadTaskStats() {
  try {
    const settings = await chrome.storage.local.get(["serverUrl", "token"]);
    const serverUrl = (settings.serverUrl || "http://localhost:7878").replace(/\/$/, "");
    const token = settings.token || "";

    const headers = {};
    if (token) {
      headers["Authorization"] = `Bearer ${token}`;
    }

    const res = await fetch(serverUrl + "/api/stats", { headers });
    if (!res.ok) return;

    const stats = (await res.json()) || {};
    const active = stats.active || 0;

    // 角标显示当前活跃任务数；按钮常驻
    queueBtnBadge.textContent = active ? active : "";
    queueBtn.style.display = "inline-block";

    // 展开时同步更新右栏指标卡
    if (queueExpanded) renderTaskStats(stats);

    // 只为「本页任务是否在跑 + 右栏进行中列表」拉一次活跃列表
    const activeTasks = await loadActiveTasks(serverUrl, headers);
    refreshCurrentPageTask(activeTasks);
    if (queueExpanded) renderActiveTasks(activeTasks);
  } catch (e) {
    // 静默失败，不影响主功能
  }
}

// 拉取活跃任务（用于本页按钮锁定判断 + 右栏「进行中」列表）
async function loadActiveTasks(serverUrl, headers) {
  try {
    const res = await fetch(serverUrl + "/api/tasks?scope=active", { headers });
    if (!res.ok) return [];
    const data = await res.json();
    return data.tasks || [];
  } catch (e) {
    return [];
  }
}

// ====== 右栏任务队列渲染（展开时用） ======
//
// ⚠️ 与「最近记录」相关的一切判据都必须与 loadTaskHistory 保持同一套，
// 别处（如飞书卡片 / background 通知）也同样判 wiki —— 本项目反复踩过
// 「同一事件多条路径说法不一致」的坑。

function renderTaskStats(s) {
  const html = `
    <div class="task-stat inbox"><span class="count">${s.inbox_pending || 0}</span>待审区</div>
    <div class="task-stat processing"><span class="count">${s.active || 0}</span>进行中</div>
    <div class="task-stat done"><span class="count">${s.today_done || 0}</span>今日完成</div>
    <div class="task-stat failed"><span class="count">${s.today_failed || 0}</span>今日失败</div>`;
  if (html !== lastTaskStatsHtml) {
    lastTaskStatsHtml = html;
    taskStats.innerHTML = html;
  }
}

function renderActiveTasks(tasks) {
  activeCountEl.textContent = tasks.length;

  if (!tasks.length) {
    const empty = '<div class="task-item-empty">暂无进行中的任务</div>';
    if (lastTaskListHtml !== empty) {
      lastTaskListHtml = empty;
      taskList.innerHTML = empty;
    }
    return;
  }

  const html = tasks.map(t => {
    const url = (t.url || "").length > 90 ? t.url.slice(0, 90) + "…" : (t.url || "（无链接）");
    const stuckClass = t.maybeStuck ? " stuck" : "";
    const stuckText = t.maybeStuck ? " ⚠️ 可能卡住" : "";
    const via = t.via ? `<span class="via-tag">${escapeHtml(t.via)}</span>` : "";
    return `<div class="task-item">
      <span class="stage${stuckClass}">${escapeHtml(t.stageLabel || t.status || "")}${stuckText}</span>${via}
      <span class="when">${escapeHtml(fmtWhen(t.created_at))}</span>
      <br><span class="url">${escapeHtml(url)}</span>
    </div>`;
  }).join("");

  if (html !== lastTaskListHtml) {
    lastTaskListHtml = html;
    taskList.innerHTML = html;
  }
}

async function loadTaskList(serverUrl, headers) {
  // 活跃列表在 loadTaskStats 里已拉过一次；这里只保证「进行中」区块
  // 在用户点开面板时立刻有内容（即使 stats 请求失败也能显示）。
  try {
    const res = await fetch(serverUrl + "/api/tasks?scope=active", { headers });
    if (!res.ok) throw new Error("tasks " + res.status);
    const data = await res.json();
    renderActiveTasks(data.tasks || []);
  } catch (e) {
    throw e;
  }
}

async function loadTaskHistory(serverUrlIn, headersIn) {
  let serverUrl = serverUrlIn, headers = headersIn;
  if (!serverUrl) {
    const settings = await chrome.storage.local.get(["serverUrl", "token"]);
    serverUrl = (settings.serverUrl || "http://localhost:7878").replace(/\/$/, "");
    headers = {};
    if (settings.token) headers["Authorization"] = `Bearer ${settings.token}`;
  }

  const q = "/api/tasks?scope=history&limit=" + Math.max(historyLimit, 20)
    + (todayOnly ? "&today=1" : "");
  const res = await fetch(serverUrl + q, { headers });
  if (!res.ok) throw new Error("history " + res.status);
  const data = await res.json();
  const all = data.tasks || [];

  // 排除命中「已存在（未重复入库）」的去重记录：它既不是进行中，也不是
  // 一次真正的入库完成（判据与服务端 historyOutcome 一致）。
  const finished = all.filter(t => !(t.result && t.result.duplicate));

  if (finished.length === 0) {
    const empty = '<div class="task-item-empty">暂无记录</div>';
    if (lastTaskHistoryHtml !== empty) {
      lastTaskHistoryHtml = empty;
      taskHistory.innerHTML = empty;
      taskMoreBtn.style.display = "none";
    }
    return;
  }

  const shown = finished.slice(0, historyLimit);
  const html = shown.map(t => {
    const o = historyOutcome(t);
    const url = (t.url || "").length > 90
      ? t.url.slice(0, 90) + "…" : (t.url || "（无链接）");
    const via = t.via ? `<span class="via-tag">${escapeHtml(t.via)}</span>` : "";
    const when = fmtWhen(t.completed_at || t.created_at);
    return `<div class="task-item ${o.cls}">
      <span class="stage ${o.cls}">${escapeHtml(o.label)}</span>${via}
      <span class="when">${escapeHtml(when)}</span>
      <br><span class="url">${escapeHtml(url)}</span>
      ${o.note ? '<div class="note">' + escapeHtml(o.note) + "</div>" : ""}
    </div>`;
  }).join("");

  if (html !== lastTaskHistoryHtml) {
    lastTaskHistoryHtml = html;
    taskHistory.innerHTML = html;
  }

  const more = finished.length > historyLimit;
  taskMoreBtn.style.display = more ? "" : "none";
  if (more) {
    taskMoreBtn.textContent =
      "展开更多历史 ▾（还有 " + (finished.length - historyLimit) + " 条）";
  }
}

// 从任务记录里抽出「这条该显示成什么」。区分三种结论：
// 成功入库 / 命中去重（内容未丢失，不是失败）/ 真失败。
// ⚠️ 与 task_queue.history_outcome、飞书卡片保持同一判据。
const HISTORY_STATUS = {
  done: { cls: "hist-done", label: "✅ 已完成" },
  failed: { cls: "hist-failed", label: "❌ 失败" },
  processing: { cls: "", label: "⏳ 处理中" },
  pending: { cls: "", label: "⏳ 排队中" },
};

function historyOutcome(t) {
  const r = t.result || {};
  if (t.status === "failed") {
    return { cls: "hist-failed", label: "❌ 失败", note: t.error || "" };
  }
  if (t.status !== "done") {
    const s = HISTORY_STATUS[t.status] || { cls: "", label: t.status };
    return { cls: s.cls, label: s.label, note: t.stageLabel || "" };
  }
  if (r.duplicate) {
    return { cls: "hist-dup", label: "📌 已存在（未重复入库）",
             note: r.wordCount ? r.wordCount + " 字" : "" };
  }
  if (r.wikiStatus && r.wikiStatus !== "ok" && r.wikiStatus !== "pending"
      && r.wikiStatus !== "skipped") {
    return { cls: "hist-failed", label: "⚠️ raw 已入库，wiki 未生成",
             note: (r.wikiReason || "") };
  }
  return { cls: "hist-done", label: "✅ 已入库",
           note: (r.wordCount ? r.wordCount + " 字" : "") +
                 (r.filename ? " · " + r.filename : "") };
}

function fmtWhen(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const p = n => String(n).padStart(2, "0");
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const hhmm = p(d.getHours()) + ":" + p(d.getMinutes());
  if (sameDay) return "今天 " + hhmm;
  return (d.getMonth() + 1) + "/" + d.getDate() + " " + hhmm;
}

function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}


// 当前页面已有队列任务时禁用按钮，避免重复提交（服务端也有同样兜底）。
// 例外：任务已被标记为「可能卡住」（超时未响应）时**不能锁死** ——
// 否则一次中断（扩展被重载 / 抓取标签页被关 / 浏览器崩溃）会让这个页面
// 永远剪不了：任务停在 processing，按钮一直灰着，用户被永久拦在门外。
function refreshCurrentPageTask(tasks) {
  if (!currentTabUrl) return;
  // 去重面板开着 = 信息统一由面板承担，状态框不要再插一嘴，
  // 否则又变成用户抱怨过的「两个框说同一件事」。
  const quiet = duplicatePanel.classList.contains("open");
  const active = (tasks || []).find(t =>
    (t.status === "pending" || t.status === "processing") && t.url === currentTabUrl);

  if (active && active.maybeStuck) {
    // 解除锁定，让用户可以重试；服务端会在下次提交时把超时任务标记为失败
    currentPageTaskId = null;
    inboxBtn.disabled = false;
    clipBtn.disabled = false;
    if (!quiet && !statusEl.classList.contains("success") && !statusEl.classList.contains("error")) {
      showStatus("error",
        `⚠️ 上次任务（${active.id}）停留在「${active.stageLabel}」超过 5 分钟，可能已中断。\n` +
        `已解除锁定：可直接重新点击「立即完整入库」，` +
        `服务端会先将超时任务标记为失败，再重新处理。`);
    }
  } else if (active) {
    currentPageTaskId = active.id;
    inboxBtn.disabled = true;
    clipBtn.disabled = true;
    if (!quiet && !statusEl.classList.contains("success")) {
      showStatus("loading", `⏳ 本页任务进行中：${active.stageLabel}（任务 ${active.id}）`);
    }
  } else if (currentPageTaskId) {
    currentPageTaskId = null;
    inboxBtn.disabled = false;
    clipBtn.disabled = false;
    if (!quiet && !statusEl.classList.contains("success") && !statusEl.classList.contains("error")) {
      showStatus("success", "✅ 本页任务已处理完成，可在 raw 目录或任务队列查看结果。");
    }
  }
}

setInterval(() => {
  loadTaskStats();
  // 展开时才拉历史（避免收起状态下每 3 秒白白拉 50 条）
  if (queueExpanded) loadTaskHistory().catch(() => {});
  loadInbox();
}, 3000);

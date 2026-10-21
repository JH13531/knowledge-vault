// options.js — 扩展配置页逻辑

const serverUrlInput = document.getElementById("serverUrl");
const tokenInput = document.getElementById("token");
const defaultClipModeSelect = document.getElementById("defaultClipMode");
const saveImagesCheckbox = document.getElementById("saveImages");
const showNotificationsCheckbox = document.getElementById("showNotifications");
const testConnectionBtn = document.getElementById("testConnection");
const serverStatus = document.getElementById("serverStatus");
const testResult = document.getElementById("testResult");
const saveBtn = document.getElementById("saveBtn");
const saveMsg = document.getElementById("saveMsg");
const healthLink = document.getElementById("healthLink");

// 默认值
const DEFAULTS = {
  serverUrl: "http://localhost:7878",
  token: "",
  defaultClipMode: "ask",
  saveImages: true,
  showNotifications: true,
};

// ====== 加载设置 ======

chrome.storage.local.get(Object.keys(DEFAULTS)).then(result => {
  serverUrlInput.value = result.serverUrl || DEFAULTS.serverUrl;
  tokenInput.value = result.token || DEFAULTS.token;
  defaultClipModeSelect.value = result.defaultClipMode || DEFAULTS.defaultClipMode;
  saveImagesCheckbox.checked = result.saveImages !== false; // 默认 true
  showNotificationsCheckbox.checked = result.showNotifications !== false; // 默认 true
  updateHealthLink();
  // 自动测试一次连接
  testConnection(false);
});

// ====== 保存设置 ======

saveBtn.addEventListener("click", async () => {
  saveBtn.disabled = true;

  try {
    const settings = {
      serverUrl: serverUrlInput.value.trim().replace(/\/$/, ""),
      token: tokenInput.value.trim(),
      defaultClipMode: defaultClipModeSelect.value,
      saveImages: saveImagesCheckbox.checked,
      showNotifications: showNotificationsCheckbox.checked,
    };

    await chrome.storage.local.set(settings);
    updateHealthLink();

    // 显示保存成功
    saveMsg.classList.add("visible");
    setTimeout(() => saveMsg.classList.remove("visible"), 2000);
  } catch (e) {
    showTestResult("error", "❌ 保存失败：" + e.message);
  } finally {
    saveBtn.disabled = false;
  }
});

// ====== 测试连接 ======

testConnectionBtn.addEventListener("click", () => testConnection(true));

async function testConnection(showLoading = true) {
  const serverUrl = serverUrlInput.value.trim().replace(/\/$/, "");
  const token = tokenInput.value.trim();

  if (showLoading) {
    showTestResult("loading", "正在连接...");
    testConnectionBtn.disabled = true;
  }

  setServerStatus("testing");

  try {
    const headers = {};
    if (token) {
      headers["Authorization"] = `Bearer ${token}`;
    }

    const res = await fetch(serverUrl + "/api/health", { headers });
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`);
    }

    const data = await res.json();
    if (!data.ok) {
      throw new Error("服务返回异常");
    }

    const platforms = (data.platforms || []).join(", ") || "无";
    const pending = data.pendingTasks || 0;

    if (!data.authRequired) {
      showTestResult("success",
        `✅ 连接成功！\n平台：${platforms}\n待处理任务：${pending}\n` +
        `服务端未启用鉴权，Token 可留空`
      );
      setServerStatus("connected");
    } else if (!token) {
      showTestResult("error",
        `⚠️ 服务已连接，但服务端启用了鉴权而 Token 为空！\n` +
        `不填 Token 能看到此状态，但剪藏请求会被拒绝（401）。\n\n` +
        `请展开上方「服务端已启用鉴权，我该填什么？」按步骤填写，值必须与 ` +
        `tools/config.local.json 中的 token 完全一致。`
      );
      setServerStatus("error");
    } else {
      // health 豁免鉴权，需再请求一个受保护接口验证 Token 本身
      const verify = await fetch(serverUrl + "/api/inbox", {
        headers: { "Authorization": `Bearer ${token}` }
      });
      if (verify.status === 401) {
        showTestResult("error",
          `❌ Token 错误（401）。\n请确认与 tools/config.local.json 中的 token ` +
          `完全一致（无多余空格），且修改后已重启 clip-server.py。`
        );
        setServerStatus("error");
      } else {
        showTestResult("success",
          `✅ 连接成功，Token 验证通过！\n平台：${platforms}\n待处理任务：${pending}`
        );
        setServerStatus("connected");
      }
    }
  } catch (e) {
    showTestResult("error",
      `❌ 连接失败：${e.message}\n\n` +
      `「Failed to fetch」表示浏览器根本没连上服务端 —— 这不是 Token 问题。按顺序排查：\n` +
      `1. clip-server.py 是否还在运行？\n` +
      `   启动时应打印「监听: http://127.0.0.1:7878」。\n` +
      `   注意：终端关掉 / 会话结束，服务就停了，用扩展前要确认它还在跑。\n` +
      `2. 本机是否配了系统代理？\n` +
      `   代理会拦截 localhost 导致连不上。把 localhost、127.0.0.1 加入代理例外，\n` +
      `   或把上面的服务地址改成 http://127.0.0.1:7878 再试。\n` +
      `3. 服务地址是否正确：${serverUrl}\n` +
      `   建议用 127.0.0.1 而不是 localhost —— 后者在某些系统会先解析到 IPv6 的 ::1，\n` +
      `   而服务端默认只监听 IPv4 的 127.0.0.1。`
    );
    setServerStatus("error");
  } finally {
    testConnectionBtn.disabled = false;
  }
}

function showTestResult(type, msg) {
  testResult.className = "status " + type;
  testResult.textContent = msg;
}

function setServerStatus(state) {
  serverStatus.className = "server-status " + state;
  const statusText = serverStatus.querySelector(".status-text");
  switch (state) {
    case "connected": statusText.textContent = "已连接"; break;
    case "error": statusText.textContent = "连接失败"; break;
    case "testing": statusText.textContent = "测试中..."; break;
    default: statusText.textContent = "未测试";
  }
}

function updateHealthLink() {
  const serverUrl = serverUrlInput.value.trim().replace(/\/$/, "");
  healthLink.href = serverUrl + "/api/health";
}

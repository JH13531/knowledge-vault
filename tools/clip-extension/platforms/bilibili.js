// platforms/bilibili.js — B站视频页适配器
// 注册到 window.__clipVault.platforms.bilibili
(function () {
  window.__clipVault = window.__clipVault || {};
  window.__clipVault.platforms = window.__clipVault.platforms || {};
  const base = window.__clipVault.base;

  // ============================================================
  // WBI 签名所需的 MD5（浏览器没有内置 MD5，WebCrypto 也不提供）
  //
  // 为什么需要：B 站字幕接口是 `GET /x/player/wbi/v2`，路径里的 `wbi` 表示
  // 参数必须签名，签名算法用 MD5。所以必须自带一份。
  //
  // 已验证：9 组标准向量（含跨越 padding 分支的 63/64/80/100/1000 字节）
  // 与 Python hashlib 逐字节一致。
  // ============================================================
  const _md5hex = (function () {
    function add32(a, b) { return (a + b) & 0xFFFFFFFF; }
    function cmn(q, a, b, x, s, t) {
      a = add32(add32(a, q), add32(x, t));
      return add32((a << s) | (a >>> (32 - s)), b);
    }
    function ff(a, b, c, d, x, s, t) { return cmn((b & c) | (~b & d), a, b, x, s, t); }
    function gg(a, b, c, d, x, s, t) { return cmn((b & d) | (c & ~d), a, b, x, s, t); }
    function hh(a, b, c, d, x, s, t) { return cmn(b ^ c ^ d, a, b, x, s, t); }
    function ii(a, b, c, d, x, s, t) { return cmn(c ^ (b | ~d), a, b, x, s, t); }

    function cycle(x, k) {
      var a = x[0], b = x[1], c = x[2], d = x[3];
      a = ff(a, b, c, d, k[0], 7, -680876936); d = ff(d, a, b, c, k[1], 12, -389564586);
      c = ff(c, d, a, b, k[2], 17, 606105819); b = ff(b, c, d, a, k[3], 22, -1044525330);
      a = ff(a, b, c, d, k[4], 7, -176418897); d = ff(d, a, b, c, k[5], 12, 1200080426);
      c = ff(c, d, a, b, k[6], 17, -1473231341); b = ff(b, c, d, a, k[7], 22, -45705983);
      a = ff(a, b, c, d, k[8], 7, 1770035416); d = ff(d, a, b, c, k[9], 12, -1958414417);
      c = ff(c, d, a, b, k[10], 17, -42063); b = ff(b, c, d, a, k[11], 22, -1990404162);
      a = ff(a, b, c, d, k[12], 7, 1804603682); d = ff(d, a, b, c, k[13], 12, -40341101);
      c = ff(c, d, a, b, k[14], 17, -1502002290); b = ff(b, c, d, a, k[15], 22, 1236535329);
      a = gg(a, b, c, d, k[1], 5, -165796510); d = gg(d, a, b, c, k[6], 9, -1069501632);
      c = gg(c, d, a, b, k[11], 14, 643717713); b = gg(b, c, d, a, k[0], 20, -373897302);
      a = gg(a, b, c, d, k[5], 5, -701558691); d = gg(d, a, b, c, k[10], 9, 38016083);
      c = gg(c, d, a, b, k[15], 14, -660478335); b = gg(b, c, d, a, k[4], 20, -405537848);
      a = gg(a, b, c, d, k[9], 5, 568446438); d = gg(d, a, b, c, k[14], 9, -1019803690);
      c = gg(c, d, a, b, k[3], 14, -187363961); b = gg(b, c, d, a, k[8], 20, 1163531501);
      a = gg(a, b, c, d, k[13], 5, -1444681467); d = gg(d, a, b, c, k[2], 9, -51403784);
      c = gg(c, d, a, b, k[7], 14, 1735328473); b = gg(b, c, d, a, k[12], 20, -1926607734);
      a = hh(a, b, c, d, k[5], 4, -378558); d = hh(d, a, b, c, k[8], 11, -2022574463);
      c = hh(c, d, a, b, k[11], 16, 1839030562); b = hh(b, c, d, a, k[14], 23, -35309556);
      a = hh(a, b, c, d, k[1], 4, -1530992060); d = hh(d, a, b, c, k[4], 11, 1272893353);
      c = hh(c, d, a, b, k[7], 16, -155497632); b = hh(b, c, d, a, k[10], 23, -1094730640);
      a = hh(a, b, c, d, k[13], 4, 681279174); d = hh(d, a, b, c, k[0], 11, -358537222);
      c = hh(c, d, a, b, k[3], 16, -722521979); b = hh(b, c, d, a, k[6], 23, 76029189);
      a = hh(a, b, c, d, k[9], 4, -640364487); d = hh(d, a, b, c, k[12], 11, -421815835);
      c = hh(c, d, a, b, k[15], 16, 530742520); b = hh(b, c, d, a, k[2], 23, -995338651);
      a = ii(a, b, c, d, k[0], 6, -198630844); d = ii(d, a, b, c, k[7], 10, 1126891415);
      c = ii(c, d, a, b, k[14], 15, -1416354905); b = ii(b, c, d, a, k[5], 21, -57434055);
      a = ii(a, b, c, d, k[12], 6, 1700485571); d = ii(d, a, b, c, k[3], 10, -1894986606);
      c = ii(c, d, a, b, k[10], 15, -1051523); b = ii(b, c, d, a, k[1], 21, -2054922799);
      a = ii(a, b, c, d, k[8], 6, 1873313359); d = ii(d, a, b, c, k[15], 10, -30611744);
      c = ii(c, d, a, b, k[6], 15, -1560198380); b = ii(b, c, d, a, k[13], 21, 1309151649);
      a = ii(a, b, c, d, k[4], 6, -145523070); d = ii(d, a, b, c, k[11], 10, -1120210379);
      c = ii(c, d, a, b, k[2], 15, 718787259); b = ii(b, c, d, a, k[9], 21, -343485551);
      x[0] = add32(a, x[0]); x[1] = add32(b, x[1]);
      x[2] = add32(c, x[2]); x[3] = add32(d, x[3]);
    }
    function blk(s) {
      var o = [], i;
      for (i = 0; i < 64; i += 4) {
        o[i >> 2] = s.charCodeAt(i) + (s.charCodeAt(i + 1) << 8) +
          (s.charCodeAt(i + 2) << 16) + (s.charCodeAt(i + 3) << 24);
      }
      return o;
    }
    var HEX = '0123456789abcdef'.split('');
    function rhex(n) {
      var s = '', j;
      for (j = 0; j < 4; j++) s += HEX[(n >> (j * 8 + 4)) & 0x0F] + HEX[(n >> (j * 8)) & 0x0F];
      return s;
    }
    // 输入按 ASCII 处理即可：WBI 的待签串由参数名/值与 32 位十六进制 key 组成
    return function (s) {
      var n = s.length, st = [1732584193, -271733879, -1732584194, 271733878], i;
      for (i = 64; i <= n; i += 64) cycle(st, blk(s.substring(i - 64, i)));
      s = s.substring(i - 64);
      var tail = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0];
      for (i = 0; i < s.length; i++) tail[i >> 2] |= s.charCodeAt(i) << ((i % 4) << 3);
      tail[i >> 2] |= 0x80 << ((i % 4) << 3);
      if (i > 55) { cycle(st, tail); for (i = 0; i < 16; i++) tail[i] = 0; }
      tail[14] = n * 8;
      cycle(st, tail);
      return st.map(rhex).join('');
    };
  })();

  // WBI 的 mixin key 置换表 —— 与 yt-dlp `bilibili.py` 中的 `mixin_key_enc_tab` 完全一致
  const MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
  ];

  const bilibili = {
    name: "Bilibili",

    // 检测是否是B站页面
    match() {
      return /bilibili\.com|b23\.tv/i.test(location.hostname);
    },

    // B站是 SPA，内容可能延迟加载
    skipContentCheck: true,

    // 等待B站内容渲染完成
    waitForContent(timeoutMs) {
      timeoutMs = timeoutMs || 10000;
      return new Promise((resolve) => {
        const selectors = [
          ".video-title",
          "h1.video-title",
          ".video-info-detail",
          "#viewbox_report",
        ];

        function checkContent() {
          for (const sel of selectors) {
            const el = document.querySelector(sel);
            if (el && (el.innerText || "").trim().length > 2) {
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

    // ============================================================
    // 从页面里取 __INITIAL_STATE__
    //
    // ⚠️ 两个必须知道的事实（都实测过）：
    //
    // 1) **content script 跑在隔离世界（isolated world）**，与页面共享 DOM、
    //    但**不共享 JS 变量**。所以 `window.__INITIAL_STATE__` 在插件里
    //    读到的永远是 `undefined`（实测：主世界 object / 隔离世界 undefined）。
    //    只有从 `<script>` 的 textContent 里抠 DOM 文本才拿得到。
    //
    // 2) 但真机上也**不能指望**这段脚本一定在 DOM 里：实测 B 站视频页
    //    （2026-10）实时 DOM 里已找不到那段 112KB 的 `window.__INITIAL_STATE__={...}`
    //    内联脚本，只剩两处无关引用。**这就是过去 metadata 只剩几十个字、
    //    cid 取不到（NO_CID）的真正原因。**
    //
    // 所以这里只是「顺手拿一下」的兜底，主数据源改用 fetchVideoInfo()（见下）。
    // ============================================================

    // 从 startIdx（必须指向 '{'）起做括号配对扫描，取出完整 JSON 文本。
    // 不能用 /{.*?}/ 这类非贪婪正则：遇到嵌套对象会在第一个 '}' 就截断，
    // JSON.parse 必然失败 —— 这正是旧实现的 bug。
    _extractBalancedJson(text, startIdx) {
      let depth = 0;
      let inStr = false;
      let esc = false;
      let quote = "";
      for (let i = startIdx; i < text.length; i++) {
        const ch = text[i];
        if (inStr) {
          if (esc) { esc = false; continue; }
          if (ch === "\\") { esc = true; continue; }
          if (ch === quote) inStr = false;
          continue;
        }
        if (ch === '"' || ch === "'") { inStr = true; quote = ch; continue; }
        if (ch === "{") depth++;
        else if (ch === "}") {
          depth--;
          if (depth === 0) return text.slice(startIdx, i + 1);
        }
      }
      return null;
    },

    getInitialState() {
      // 理论上拿不到（隔离世界），留着以备将来环境变化
      try {
        if (window.__INITIAL_STATE__) return window.__INITIAL_STATE__;
      } catch (e) {}

      try {
        const scripts = document.querySelectorAll("script");
        for (const s of scripts) {
          const text = s.textContent || "";
          const m = /__INITIAL_STATE__\s*=\s*/.exec(text);
          if (!m) continue;
          const raw = bilibili._extractBalancedJson(text, m.index + m[0].length);
          if (!raw) continue;
          try { return JSON.parse(raw); } catch (e) { /* 试下一个 */ }
        }
      } catch (e) {}

      return null;
    },

    // ============================================================
    // 主数据源：直接调 B 站 view 接口（不需要登录、不需要 WBI 签名）
    //
    // 实测（在真隔离世界里跑，等同 content script）：
    //   GET https://api.bilibili.com/x/web-interface/view?bvid=BV...
    //   → status 200, type "cors", code 0
    //   → data.cid / aid / title / desc / owner.name / pubdate / duration / stat
    //
    // 这一步同时解决了 cid（字幕接口必需）和元数据的可靠性 —— 不再依赖
    // __INITIAL_STATE__ 是否存在于 DOM。
    // ============================================================
    // ============================================================
    // 跨域请求必须经 background 代发
    //
    // Chrome 85 起，content script 的请求按**宿主页面的 origin** 发出，
    // 受页面 CORS 约束 —— 在 B 站页面里直连 api.bilibili.com 会直接
    // `Failed to fetch`（真机实测）。background 是扩展上下文，声明
    // host_permissions 后豁免 CORS，所以由它代发。
    // ============================================================
    async _bgFetchJson(url) {
      let r = null;
      try {
        r = await chrome.runtime.sendMessage({ action: "fetch-json", url: url });
      } catch (e) {
        // 扩展刚重载，本页 content script 已失去连接
        throw new Error("与扩展后台失去连接（请刷新本页后重试）");
      }
      // r 为空 = **后台根本没响应**：最常见的原因是 background service worker
      // 还是旧版本（不认识 fetch-json 这个动作）。新版 content script + 旧版 SW
      // 正是「重载扩展后只刷新页面、但 SW 没跟上」的典型组合。
      // 这里必须和「后台返回了错误」区分开，否则只会看到一句没有信息量的
      //「跨域请求失败」，完全不知道该去重载扩展。
      if (!r) {
        throw new Error("后台未响应（扩展可能刚更新：请到 chrome://extensions 点「重载」，再刷新本页）");
      }
      if (!r.success) {
        throw new Error(r.error || "跨域请求失败（后台未说明原因）");
      }
      if (!r.json) {
        throw new Error("返回内容不是 JSON（HTTP " + r.status + "）");
      }
      return r.json;
    },

    //: 最近一次 view 接口失败原因。取不到 cid 时要把它带进提示里 ——
    //: 否则接口被拦、网络不通、扩展失联…全都只剩一句「没有 cid」，没法排查。
    _lastApiError: "",

    async fetchVideoInfo(bvid) {
      if (!bvid) return null;
      try {
        const j = await bilibili._bgFetchJson(
          "https://api.bilibili.com/x/web-interface/view?bvid=" + encodeURIComponent(bvid));
        if (!j || j.code !== 0 || !j.data) {
          bilibili._lastApiError = "view 接口 code=" + (j && j.code) +
            " " + ((j && j.message) || "");
          console.warn("[Vault] B 站 view 接口返回异常:", j && j.code, j && j.message);
          return null;
        }
        bilibili._lastApiError = "";
        return j.data;
      } catch (e) {
        bilibili._lastApiError = String((e && e.message) || e);
        console.warn("[Vault] B 站 view 接口请求失败:", bilibili._lastApiError);
        return null;
      }
    },

    // 把 view 接口的数据补进 state（缺失字段才补，state 有的优先）
    mergeInApi(state, api) {
      if (!api) return state;
      const vd = Object.assign({}, (state && state.videoData) || {});
      const setIf = (k, v) => {
        if ((vd[k] === undefined || vd[k] === null || vd[k] === "") &&
            v !== undefined && v !== null && v !== "") {
          vd[k] = v;
        }
      };
      setIf("bvid", api.bvid);
      setIf("aid", api.aid);
      setIf("cid", api.cid);
      setIf("title", api.title);
      setIf("desc", api.desc);
      setIf("pubdate", api.pubdate);
      setIf("duration", api.duration);
      if (!vd.owner || !vd.owner.name) vd.owner = { name: (api.owner || {}).name || "" };
      if (!vd.stat) {
        vd.stat = { view: (api.stat || {}).view, danmaku: (api.stat || {}).danmaku };
      }

      const merged = Object.assign({}, state || {});
      merged.videoData = vd;
      if (!(merged.upData && merged.upData.name)) {
        merged.upData = { name: (api.owner || {}).name || "" };
      }
      return merged;
    },

    // 提取 BV 号
    extractBvid(state) {
      // 从 URL 提取
      const urlMatch = location.pathname.match(/\/video\/(BV[a-zA-Z0-9]+)/);
      if (urlMatch) return urlMatch[1];

      // 从 state 提取
      if (state) {
        if (state.videoData && state.videoData.bvid) return state.videoData.bvid;
        if (state.bvid) return state.bvid;
      }
      return "";
    },

    // 提取标题
    extractTitle(state) {
      const clean = base.cleanZeroWidth;

      // 从 state 提取
      if (state) {
        if (state.videoData && state.videoData.title) {
          return clean(state.videoData.title.trim());
        }
        const mediaInfo = state.mediaInfo || {};
        if (mediaInfo.title) return clean(mediaInfo.title.trim());
      }

      // 从 DOM 提取
      const sels = [
        "h1.video-title",
        ".video-title h1",
        ".video-title",
        "h1",
        "title",
      ];
      for (const sel of sels) {
        const el = document.querySelector(sel);
        if (el) {
          const t = clean((el.innerText || el.textContent || "").trim());
          if (t && t.length > 1 && !t.startsWith("bilibili")) return t;
        }
      }
      return clean((document.title || "").replace(/_bilibili.*$/, "").trim());
    },

    // 提取 UP 主
    extractAuthor(state) {
      const clean = base.cleanZeroWidth;

      if (state) {
        if (state.upData && state.upData.name) return clean(state.upData.name);
        if (state.videoData && state.videoData.owner && state.videoData.owner.name) {
          return clean(state.videoData.owner.name);
        }
      }

      const sels = [
        ".up-name",
        ".up-name-text",
        "a.up-name",
        ".username",
        "[class*='up-name']",
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

    // 提取视频描述/简介
    extractDescription(state) {
      const clean = base.cleanZeroWidth;

      if (state) {
        if (state.videoData && state.videoData.desc) {
          const desc = state.videoData.desc.trim();
          if (desc) return clean(desc);
        }
      }

      // 从 DOM 提取
      const sels = [
        ".basic-desc-info",
        ".video-desc",
        ".desc-info-text",
        "[class*='desc']",
      ];
      for (const sel of sels) {
        const el = document.querySelector(sel);
        if (el) {
          const t = clean((el.innerText || "").trim());
          if (t && t.length > 10) return t;
        }
      }
      return "";
    },

    // 提取标签
    extractTags(state) {
      const tags = [];

      if (state) {
        const tagList = state.videoData && state.videoData.dynamic || [];
        // state.tags 或 videoData.tags
        const stateTags = state.tags || (state.videoData && state.videoData.tags) || [];
        for (const t of stateTags) {
          const name = typeof t === "string" ? t : (t.tag_name || t.name || "");
          if (name) tags.push(name);
        }
      }

      // DOM 补充
      if (tags.length === 0) {
        document.querySelectorAll(".video-tag .tag-link, .tag-link, .tag-panel .tag-link").forEach(el => {
          const t = (el.innerText || "").trim();
          if (t) tags.push(t);
        });
      }

      return [...new Set(tags)].slice(0, 10);
    },

    // ============================================================
    // 字幕：页面内直取（不需要后端）
    //
    // 背景：B 站字幕**不在 DOM 里**（页面根本不渲染字幕文本），所以通用 DOM 提取
    // 拿不到它 —— 这是悬浮按钮过去只入库元数据的真正原因。
    // 而插件跑在 bilibili.com 的页面上下文里，fetch api.bilibili.com 会自动带上
    // 你的登录 cookie，于是可以直接调字幕接口：
    //   **不需要后端、也不需要解密 cookie → Windows 上同样可用**
    //   （绕开了 Chrome 的 App-Bound Encryption 限制）
    //
    // 两步（等价于后端 yt-dlp 的 `_get_subtitles`）：
    //   1. GET /x/player/wbi/v2?bvid=&cid=  → data.subtitle.subtitles[].subtitle_url
    //   2. GET <subtitle_url>               → { body: [{from, to, content}] }
    // ============================================================
    _wbiKeyCache: null,
    _wbiKeyTs: 0,

    // 从 __INITIAL_STATE__ 取 cid / aid（字幕接口必需，此前只取了 bvid）
    extractCid(state) {
      const vd = (state && state.videoData) || {};
      const cid = vd.cid || (state && state.cid) || "";
      const aid = vd.aid || (state && state.aid) || "";
      return { cid: String(cid || ""), aid: String(aid || "") };
    },

    // 取 WBI 密钥（缓存 30 分钟，避免每次剪藏都多打一次 nav 接口）
    async getWbiKey() {
      const now = Date.now();
      if (this._wbiKeyCache && now - this._wbiKeyTs < 30 * 60 * 1000) {
        return this._wbiKeyCache;
      }
      const data = await bilibili._bgFetchJson("https://api.bilibili.com/x/web-interface/nav");
      const wbi = (data && data.data && data.data.wbi_img) || {};
      const pick = (u) => String(u || "").split("/").pop().split(".")[0];
      const lookup = pick(wbi.img_url) + pick(wbi.sub_url);
      if (lookup.length < 64) {
        throw new Error("WBI 密钥获取失败（nav 接口未返回 wbi_img）");
      }
      this._wbiKeyCache = MIXIN_KEY_ENC_TAB.map(i => lookup[i]).join("").slice(0, 32);
      this._wbiKeyTs = now;
      return this._wbiKeyCache;
    },

    // WBI 签名：加 wts，过滤 !'()*，按参数名排序后拼 query，再 md5(query + key)
    _signWbi(params, key) {
      const withTs = Object.assign({}, params, {
        wts: String(Math.round(Date.now() / 1000)),
      });
      const out = {};
      Object.keys(withTs).sort().forEach(k => {
        out[k] = String(withTs[k]).replace(/[!'()*]/g, "");
      });
      const query = Object.keys(out)
        .map(k => encodeURIComponent(k) + "=" + encodeURIComponent(out[k]))
        .join("&");
      out.w_rid = _md5hex(query + key);
      return out;
    },

    // json3 字幕 → `[HH:MM:SS] 文本` 逐行（与后端 _parse_json3 格式一致，便于两条路径产出统一）
    subtitleJsonToText(data) {
      const body = (data && data.body) || [];
      const pad = (n) => String(n).padStart(2, "0");
      const lines = [];
      for (const ev of body) {
        const text = String(ev.content || "").trim();
        if (!text) continue;
        const s = Number(ev.from || 0);
        lines.push("[" + pad(Math.floor(s / 3600)) + ":" +
          pad(Math.floor((s % 3600) / 60)) + ":" + pad(Math.floor(s % 60)) + "] " + text);
      }
      return lines.join("\n");
    },

    // 把协议相对（`//host/path`）或站内相对地址补成绝对 URL。
    //
    // 为什么必须有：B 站返回的 `subtitle_url` 常年是**协议相对**的
    // `//aisubtitle.hdslb.com/bfs/ai_subtitle/....json?auth_key=...`。
    // background 的代发函数用 `new URL(url)` 做白名单校验，而 `new URL("//host/x")`
    // 没有 base 会**直接抛异常** → 被判成「地址不在白名单内」→ 字幕永远拿不到。
    // （yt-dlp 里同样有 `if url.startswith('//')` 的处理，不是我们独有的坑。）
    absoluteUrl(u) {
      const s = String(u || "").trim();
      if (!s) return "";
      if (s.startsWith("//")) return "https:" + s;
      if (/^https?:\/\//i.test(s)) return s;
      if (s.startsWith("/")) {
        return (location.origin || "https://www.bilibili.com") + s;
      }
      return "https://" + s;
    },

    // 拉字幕。返回 {lang, text}；失败抛错，由调用方决定提示文案
    async fetchSubtitles(bvid, cid, aid) {
      if (!cid || (!bvid && !aid)) throw new Error("NO_CID");

      const params = bvid ? { bvid: bvid, cid: cid } : { aid: aid, cid: cid };
      const key = await this.getWbiKey();
      const signed = this._signWbi(params, key);
      const qs = Object.keys(signed)
        .map(k => encodeURIComponent(k) + "=" + encodeURIComponent(signed[k]))
        .join("&");

      const info = await bilibili._bgFetchJson(
        "https://api.bilibili.com/x/player/wbi/v2?" + qs);
      if (!info || info.code !== 0) {
        throw new Error("字幕接口 code=" + (info && info.code) +
          " " + ((info && info.message) || ""));
      }

      const list = (info.data && info.data.subtitle && info.data.subtitle.subtitles) || [];
      // 语言优先级与后端 _SUB_LANGS 对齐。
      // `aizh` 是 B 站现在实际返回的 AI 中文字幕标识（没有连字符）——
      // 实测 2026-10-07 剪 BV1VqNt6JEwQ 时 `lan` 就是 "aizh"。
      // 漏了它也不至于全挂（下面还有「任意一条带 url 的」兜底），
      // 但那样「中文优先」就成了碰运气，所以显式列出来。
      const prefer = ["ai-zh", "aizh", "zh-Hans", "zh-CN", "zh"];
      let pick = null;
      for (const lang of prefer) {
        pick = list.find(s => s.lan === lang && s.subtitle_url);
        if (pick) break;
      }
      if (!pick) pick = list.find(s => s.subtitle_url);
      if (!pick) {
        const needLogin = !!(info.data && info.data.need_login_subtitle);
        throw new Error(needLogin ? "NOT_LOGGED_IN" : "NO_SUBTITLE");
      }

      const subJson = await bilibili._bgFetchJson(bilibili.absoluteUrl(pick.subtitle_url));
      const text = this.subtitleJsonToText(subJson);
      if (!text) throw new Error("NO_SUBTITLE");
      return { lang: pick.lan, text: text };
    },

    // 噪音元素选择器
    noiseSelectors: [
      ".video-toolbar",
      ".video-info-desc",
      ".recommend-list",
      ".comment-area",
      "#comment",
      ".reply-list",
      ".danmaku-box",
      ".video-share",
      ".elec-all",
    ],

    // 主提取函数
    // 注意：是 async 的（要 await 字幕接口）。registry.extractContent 本身是 async，
    //       会用 await 接收返回值，所以调用方无需改动。
    async extractContent(url) {
      const clean = base.cleanZeroWidth;

      // bvid 先从 URL 取（不依赖任何页面数据）
      const bvidFromUrl = bilibili.extractBvid(null);

      // 主数据源：view 接口（cid / title / desc / owner / stat … 一次拿全）。
      // 失败不阻断：退化成 state / DOM 提取，只是可能拿不到字幕。
      const api = await bilibili.fetchVideoInfo(bvidFromUrl);

      // state 只作补充 —— 隔离世界里 window.__INITIAL_STATE__ 读不到，
      // 而 DOM 里那段内联脚本在真机上也可能已被移除（详见 getInitialState 注释）
      const state = bilibili.mergeInApi(bilibili.getInitialState(), api);

      const bvid = bilibili.extractBvid(state) || bvidFromUrl;
      const title = bilibili.extractTitle(state);
      const author = bilibili.extractAuthor(state);
      const description = bilibili.extractDescription(state);
      const tags = bilibili.extractTags(state);

      // 发布时间
      let publishedAt = "";
      if (state && state.videoData && state.videoData.pubdate) {
        try {
          publishedAt = new Date(state.videoData.pubdate * 1000).toISOString().slice(0, 10);
        } catch (e) {}
      }

      // 视频时长
      let duration = "";
      if (state && state.videoData && state.videoData.duration) {
        const secs = state.videoData.duration;
        const min = Math.floor(secs / 60);
        const sec = secs % 60;
        duration = min + ":" + String(sec).padStart(2, "0");
      }

      // 播放量/弹幕等统计
      let viewCount = "";
      let danmakuCount = "";
      if (state && state.videoData && state.videoData.stat) {
        const stat = state.videoData.stat;
        viewCount = stat.view ? String(stat.view) : "";
        danmakuCount = stat.danmaku ? String(stat.danmaku) : "";
      }

      // 组装 Markdown
      const parts = [];
      parts.push("# " + title);
      parts.push("");
      parts.push("> **UP主：** " + author);
      if (bvid) parts.push("> **BV号：** " + bvid);
      if (publishedAt) parts.push("> **发布时间：** " + publishedAt);
      if (duration) parts.push("> **时长：** " + duration);
      if (viewCount) parts.push("> **播放量：** " + viewCount);
      parts.push("> **链接：** " + url);
      parts.push("");

      if (tags.length > 0) {
        parts.push("**标签：** " + tags.map(t => "#" + t).join(" "));
        parts.push("");
      }

      if (description) {
        parts.push("## 简介");
        parts.push("");
        parts.push(description);
        parts.push("");
      }

      // 提取页面可见正文（评论区上方区域的内容等）
      const contentSels = [
        ".video-info-detail",
        "#viewbox_report",
        ".video-desc-container",
      ];
      let extraContent = "";
      for (const sel of contentSels) {
        const el = document.querySelector(sel);
        if (el && (el.innerText || "").trim().length > 20) {
          extraContent = base.elementToMarkdown(el, bilibili.noiseSelectors);
          break;
        }
      }

      if (extraContent && extraContent.trim().length > 20) {
        parts.push("## 页面信息");
        parts.push("");
        parts.push(clean(extraContent).trim());
        parts.push("");
      }

      // ===== 字幕：页面内直取（失败则降级为元数据 + 明确说明，不阻断入库）=====
      let subtitleText = "";
      let subtitleLang = "";
      let subtitleNote = "";   // Markdown 版：写进正文，给读这份 raw 的人看
      let captionNote = "";    // 纯文本版：随 extra 上传，供服务端拼进「为什么不完整」
      const ids = bilibili.extractCid(state);
      try {
        const sub = await bilibili.fetchSubtitles(bvid, ids.cid, ids.aid);
        subtitleText = sub.text;
        subtitleLang = sub.lang;
      } catch (e) {
        const msg = String((e && e.message) || e);
        if (msg === "NOT_LOGGED_IN") {
          captionNote = "B 站字幕需要登录态：当前浏览器未登录 B 站，或登录状态已过期";
        } else if (msg === "NO_SUBTITLE") {
          captionNote = "该视频没有可用字幕：UP 主未上传，平台亦未生成 AI 字幕";
        } else if (msg === "NO_CID") {
          captionNote = "未能获取 cid：view 接口调用失败，页面中也没有可用的视频数据";
          if (bilibili._lastApiError) captionNote += "；view 接口错误：" + bilibili._lastApiError;
        } else {
          captionNote = msg;
        }
        subtitleNote = "> ⚠️ 未获取到字幕：" + captionNote + "。";
        console.warn("[Vault] B 站字幕获取失败:", msg);
      }

      if (subtitleText) {
        parts.push("## 字幕（" + (subtitleLang || "unknown") + "）");
        parts.push("");
        parts.push(subtitleText);
        parts.push("");
      } else {
        parts.push("---");
        parts.push("");
        parts.push(subtitleNote);
        parts.push("");
      }

      return {
        title,
        url,
        author,
        publishedAt,
        content: clean(parts.join("\n")).trim().slice(0, 50000),
        type: "video",
        platform: "Bilibili",
        tags: ["B站"].concat(tags),
        duration,
        extra: {
          bvid,
          cid: ids.cid,
          viewCount,
          danmakuCount,
          hasSubtitles: !!subtitleText,
          subtitleLang,
          // 服务端 `_content_incomplete_reason()` 会把它并进给用户看的原因。
          // 不传的话，所有字幕失败都只剩一句「视频未取到字幕/转录」——
          // 用户分不出「视频确实没字幕」和「没登录 / 接口挂了」。
          captionNote,
        },
      };
    },
  };

  window.__clipVault.platforms.bilibili = bilibili;
})();

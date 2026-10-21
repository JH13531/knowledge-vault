// platforms/youtube.js — YouTube 页面内抓取
//
// 为什么必须有这个文件（而不是交给服务端 yt-dlp）：
//   服务端抓 YouTube 要自己出网 / 持登录态，而本机后端往往**出不去**。
//   但你已经把视频页开在浏览器里了 —— 页面能打开，就说明这个浏览器出得去。
//   所以正解是**在页面里抓**，而不是另开一个隐藏标签页让服务端去抓。
//
// ⚠️ 字幕怎么拿（2025–2026 的坑，这里是本项目踩过最深的一个）
//
//   老办法：YouTube 把 `ytInitialPlayerResponse` 内联在页面 <script> 里，
//   字幕轨在 `.captions.playerCaptionsTracklistRenderer.captionTracks[]`，
//   每项带一个指向 `/api/timedtext` 的 baseUrl，直接 fetch 即可。
//
//   **这个办法现在已经失效。** YouTube 给字幕接口加了 PoToken
//   （Proof of Origin，BotGuard 风控令牌）门槛：从**网页**里读到的 baseUrl
//   会被塞进 `exp=xpe`（有时还有 `potc=1`）标记，意思是「这条 URL 需要 PO Token」。
//   拿它去请求不会 403、不会报错 —— 而是回 **HTTP 200 + 0 字节 body**。
//
//   这是**静默失败**，最难排查：解析器拿到空串 → hasSubtitles=false →
//   整条视频被判成「没字幕」→ 降级待审区，而用户明明看得见字幕。
//
//   唯一稳定的出口：InnerTube 的 `/youtubei/v1/player`，用 **ANDROID / IOS
//   客户端**身份去问。它返回的 captionTracks **不带 exp=xpe**，可以正常下载。
//   （youtube-transcript-api ≥1.1.0、youtube-transcript ≥1.3.0 也是这么切的。）
//   本扩展跑在已打开的 youtube.com 页内，调 /youtubei/v1/player 是**同源请求**，
//   不需要 cookie 解密、不需要后端出网、不需要另开标签页。
//
//   所以顺序是：① InnerTube（ANDROID → IOS）→ ② 页面内联 playerResponse 兜底。
//   两条都空时**不报错**，照常返回标题 + 简介并置 hasSubtitles=false，
//   由服务端的「内容不完整」闸门决定降级；但 `extra.captionNote` 必须把
//   **真实原因**带出去（PO Token 拦截 / HTTP 403 / 确实没有字幕轨），
//   否则用户只会看到一句笼统的「没拿到字幕」，无从判断该不该重试。
(function () {
  window.__clipVault = window.__clipVault || {};
  window.__clipVault.platforms = window.__clipVault.platforms || {};
  const base = window.__clipVault.base;

  // InnerTube 公共 API key 的兜底值。
  // 首选永远是从页面 DOM 里读到的那个（网页自己就在用，最不容易过期）。
  const FALLBACK_API_KEYS = [
    "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8",
    "AIzaSyA8eiZmM1FaDVjRy-df2KTyQ_vz_yYM39w",
  ];

  // 客户端身份。**不要用纯 WEB** —— WEB 拿到的字幕 URL 必然带 exp=xpe。
  // num 是 InnerTube 的 X-Youtube-Client-Name（ANDROID=3，IOS=5，TVHTML5=7）。
  //
  // ⚠️ 为什么必须带 TVHTML5（踩过的坑：登录了却一直 LOGIN_REQUIRED）
  //
  //   ANDROID / IOS 是**移动 App** 的客户端身份，它们只认 OAuth 刷新令牌，
  //   **不认浏览器 cookie**。而本扩展是在浏览器页面里发请求的 —— 它能带上的
  //   只有浏览器的登录 cookie（credentials:"include"）。于是出现一个非常
  //   反直觉的现象：**用户明明在浏览器里登录了 YouTube，ANDROID/IOS 客户端
  //   依然回 LOGIN_REQUIRED「请登录，以便我们确认你不是聊天机器人」** ——
  //   因为在这些客户端看来，我们从来就没登录过。
  //
  //   TVHTML5（YouTube TV / 智能电视网页版）走的是**网页 cookie 鉴权**，
  //   与浏览器会话天然一致，因此能真正吃到用户的登录态。
  //   把它排在 ANDROID/IOS 之前，登录用户就能拿到完整字幕轨。
  const INNERTUBE_CLIENTS = [
    {
      // 电视端网页：用 cookie 鉴权，与浏览器登录态一致（首选）
      name: "TVHTML5", num: 7,
      client: {
        clientName: "TVHTML5_SIMPLY_EMBEDDED_PLAYER", clientVersion: "2.0",
      },
      ua: "Mozilla/5.0 (PlayStation; PlayStation 4/12.00) AppleWebKit/605.1.15 " +
          "(KHTML, like Gecko) Version/13.0 Safari/605.1.15",
    },
    {
      name: "WEB_EMBEDDED", num: 56,
      client: { clientName: "WEB_EMBEDDED_PLAYER", clientVersion: "1.20250101.00.00" },
      ua: "",
    },
    {
      name: "ANDROID", num: 3,
      client: { clientName: "ANDROID", clientVersion: "20.10.38", androidSdkVersion: 34 },
      ua: "com.google.android.youtube/20.10.38 (Linux; U; Android 14) gzip",
    },
    {
      name: "IOS", num: 5,
      client: { clientName: "IOS", clientVersion: "20.10.4", deviceMake: "Apple", deviceModel: "iPhone16,2" },
      ua: "com.google.ios.youtube/20.10.4 (iPhone16,2; U; CPU iOS 18_3_2 like Mac OS X)",
    },
  ];

  // 这些客户端用**浏览器 cookie** 鉴权，与页面登录态一致。
  // 只有它们的 LOGIN_REQUIRED 才真能说明「用户没登录」；
  // 移动客户端（ANDROID/IOS）再怎么说 LOGIN_REQUIRED 都不能当作用户没登录的证据。
  const COOKIE_AUTH_CLIENTS = ["TVHTML5", "WEB_EMBEDDED"];

  const youtube = {
    name: "YouTube",
    priority: 20,
    skipContentCheck: true,   // SPA：切视频不整页刷新，内容晚渲染

    match() {
      const h = (location.hostname || "").replace(/^www\./, "").toLowerCase();
      return h === "youtube.com" || h === "m.youtube.com" || h === "youtu.be";
    },

    // ==================================================================
    // 等待播放器会话就绪（**这是「服务端派发失败、手动剪藏成功」的根因**）
    // ==================================================================
    //
    // 为什么必须有它：
    //
    //   `innertubePlayer()` 向 `/youtubei/v1/player` 请求字幕轨时，**必须带上
    //   有效的播放器会话上下文**（ytcfg 里的 INNERTUBE_CONTEXT / visitorData、
    //   以及由页面初始化流程写入的会话 cookie）。只用 `credentials:"include"`
    //   带上原始 cookie 是不够的 —— 会话没激活时 YouTube 一律回
    //   `playabilityStatus.status = "LOGIN_REQUIRED"`。
    //
    //   两条路径的差别就在**页面被打开后经过了多久 / 是否被真正初始化**：
    //
    //     · 手动剪藏：用户已经在那个视频页上停留、甚至播放过，
    //       页面早已完成 ytcfg / 播放器初始化 → 会话有效 → 成功。
    //     · 服务端派发：扩展新建一个**后台标签页**（active:false）后只等
    //       固定 2 秒 + 1 秒滚动就立刻抓取。后台标签页连 ytInitialPlayerResponse
    //       都可能还没注入完，会话更没有建立 → LOGIN_REQUIRED。
    //
    //   所以这里专职「等到播放器真的就绪」，而不是赌一个固定时长。
    //   判据按可信度递增：
    //     ① 页面内联 playerResponse 已出现，**且 videoId 与当前视频一致**
    //        （不一致说明还停在上一视频，SPA 尚未切换完成）
    //     ② ytcfg 已就绪（INNERTUBE_API_KEY / INNERTUBE_CONTEXT 可读到）
    //     ③ 播放器元素已挂载
    //   三者全中才认为就绪；超时则按当前状态返回，让上层带着真实原因继续。
    async waitForContent(timeoutMs) {
      const limit = Number(timeoutMs) > 0 ? Number(timeoutMs) : 12000;
      const deadline = Date.now() + limit;
      const wantId = youtube.videoId(location.href);

      const ready = () => {
        // ② ytcfg 就绪：InnerTube key 与 context 都能读到
        if (!youtube.innertubeApiKey()) return false;
        // ③ 播放器已挂载
        if (!document.querySelector("#movie_player, ytd-player, video.html5-main-video")) {
          return false;
        }
        // ① playerResponse 已出现且属于本条视频
        const pr = youtube.playerResponse();
        if (pr && wantId && youtube.playerMatches(pr, wantId)) return true;
        // playerResponse 还没到时，退回看 ytd-watch-flexy 是否已完成渲染
        const flexy = document.querySelector("ytd-watch-flexy");
        if (flexy && flexy.hasAttribute("video-id") &&
            flexy.getAttribute("video-id") === wantId) {
          return true;
        }
        return false;
      };

      // 进页面头 500ms 内大概率还没渲染，先让出一帧再轮询
      await new Promise((r) => setTimeout(r, 400));
      while (Date.now() < deadline) {
        if (ready()) return { ok: true };
        await new Promise((r) => setTimeout(r, 300));
      }
      return { ok: false, reason: "播放器未在 " + Math.round(limit / 1000) + " 秒内就绪" };
    },

    // ------------------------------------------------------------------
    // 主流程
    // ------------------------------------------------------------------
    async extractContent(url) {
      const clean = base.cleanZeroWidth;
      const videoId = youtube.videoId(url);
      const rawPlayer = youtube.playerResponse();

      // ⚠️ 必须核对 player 响应对应的**是不是当前这条视频**。
      //
      // YouTube 是 SPA：从一个视频点进另一个视频**不会整页刷新**，
      // 于是内联 <script> 里那份 `ytInitialPlayerResponse` 仍停留在
      // **上一个视频**，直到用户手动刷新页面为止。
      //
      // 后果很隐蔽：标题、作者、发布时间全部取自那份陈旧的 player，
      // 于是一个讲 AI 的视频会被写成「从零开始认识显卡」——标题、作者、
      // 发布日期全错，只有**字幕**是对的（字幕按 videoId 现拉）。
      // 库里出现「raw 标题与正文内容完全对不上」就是这里来的。
      //
      // 因此：videoId 对不上就**整份丢弃**，退回页面可见信息（document.title），
      // 宁可用一个朴素但正确的标题，也不要用一个漂亮的错标题。
      const player = youtube.playerMatches(rawPlayer, videoId) ? rawPlayer : null;
      const titleMismatch = !player && !!rawPlayer;

      const vd = (player && player.videoDetails) || {};
      const mf = (player && player.microformat && player.microformat.playerMicroformatRenderer) || {};

      const pageTitle = clean(String(document.title || ""))
        .replace(/\s*-\s*YouTube\s*$/, "").trim();
      const title = clean(vd.title || mf.title?.simpleText || pageTitle || "YouTube 视频")
        .replace(/\s*-\s*YouTube\s*$/, "");
      // 作者同样只在 player 属于本条视频时才可信
      const author = clean(vd.author || mf.ownerChannelName || "");
      const publishedAt = player ? (mf.publishDate || mf.uploadDate || "") : "";
      const description = clean(vd.shortDescription || "").trim();
      const duration = youtube.formatDuration(Number(vd.lengthSeconds) || 0);

      if (titleMismatch) {
        console.warn("[Vault] ytInitialPlayerResponse 属于另一条视频（SPA 未刷新），" +
          "已改为使用页面标题：" + pageTitle);
      }

      // 字幕。player 可能为 null（视频ID 对不上而被丢弃），但字幕本来就不依赖它：
      // grabSubtitles 内部会自己按 videoId 走 InnerTube 重新问一次。
      let got = { text: "", lang: "", source: "", note: "", potBlocked: false };
      try {
        got = await youtube.grabSubtitles(player, videoId);
      } catch (e) {
        got.note = "字幕获取失败：" + ((e && e.message) || e);
      }
      const subtitleText = got.text;
      const subtitleLang = got.lang;

      const parts = [];
      parts.push("# " + title);
      parts.push("");
      parts.push("> **频道：** " + (author || "（未知）"));
      parts.push("> **发布时间：** " + (publishedAt || "（未知）"));
      parts.push("> **时长：** " + (duration || "（未知）"));
      parts.push("> **链接：** " + url);
      parts.push("");

      // 元数据来自「上一个视频」时，必须在正文里留痕 —— 否则库里又会多出一条
      // 「标题与内容对不上」的记录，且事后完全看不出是为什么。
      if (titleMismatch) {
        parts.push("> ⚠️ 页面未刷新：YouTube 播放器仍停在另一条视频，");
        parts.push("> 标题与作者取自**当前页面标题**，发布日期等元数据可能缺失。");
        parts.push("> 如需完整元数据，请刷新页面后重新剪藏。");
        parts.push("");
      }

      if (description) {
        parts.push("## 视频简介");
        parts.push("");
        parts.push(description.slice(0, 4000));
        parts.push("");
      }

      if (subtitleText) {
        parts.push("## 字幕（" + (subtitleLang || "unknown") + "）");
        parts.push("");
        parts.push(subtitleText);
        parts.push("");
      } else {
        parts.push("---");
        parts.push("");
        parts.push("> ⚠️ 未获取到字幕：" + (got.note || "未知原因"));
        parts.push("");
      }

      return {
        title,
        url,
        author,
        publishedAt: youtube.isoDate(publishedAt),
        content: clean(parts.join("\n")).trim().slice(0, 200000),
        type: "video",
        platform: "YouTube",
        tags: ["YouTube", "视频"],
        duration,
        extra: {
          videoId,
          hasSubtitles: !!subtitleText,
          subtitleLang,
          subtitleSource: got.source || "",
          captionNote: got.note || "",
          // 登录态缺失（吃 cookie 的客户端明确要求登录）。
          // 与「视频确实没字幕」区分开 —— 服务端据此给出不同的处置建议。
          loggedOut: !!got.loggedOut,
          // 标题来源：player = 已核对属于本条视频；page = 退回页面标题（未刷新）
          titleSource: titleMismatch ? "page" : (player ? "player" : "page"),
          titleMismatch: !!titleMismatch,
        },
      };
    },

    // ------------------------------------------------------------------
    // 视频 ID / 时长 / 日期
    // ------------------------------------------------------------------
    videoId(url) {
      try {
        const u = new URL(url, location.href);
        const h = u.hostname.replace(/^www\./, "");
        if (h === "youtu.be") return u.pathname.slice(1).split("/")[0];
        const v = u.searchParams.get("v");
        if (v) return v;
        const m = u.pathname.match(/\/(embed|shorts|live|v)\/([^/?#]+)/);
        if (m) return m[2];
      } catch (e) { /* ignore */ }
      return "";
    },

    formatDuration(totalSeconds) {
      if (!totalSeconds || totalSeconds <= 0) return "";
      const h = Math.floor(totalSeconds / 3600);
      const m = Math.floor((totalSeconds % 3600) / 60);
      const s = Math.floor(totalSeconds % 60);
      const pad = (n) => String(n).padStart(2, "0");
      return h > 0 ? h + ":" + pad(m) + ":" + pad(s) : m + ":" + pad(s);
    },

    // YouTube 给的是 YYYY-MM-DD，已经是目标格式；这里只做保险
    isoDate(s) {
      if (!s) return "";
      const m = String(s).match(/^(\d{4})-(\d{2})-(\d{2})/);
      return m ? m[0] : "";
    },

    // ------------------------------------------------------------------
    // 从 <script> 里取出 ytInitialPlayerResponse
    // ------------------------------------------------------------------
    playerResponse() {
      // 1) 页面可能在全局作用域之外还留了副本（少数改版会挂到 window 上）
      try {
        if (window.ytInitialPlayerResponse) return window.ytInitialPlayerResponse;
      } catch (e) { /* 隔离世界读不到，正常 */ }

      // 2) 主路径：读内联 <script> 的文本
      const MARKERS = [
        "ytInitialPlayerResponse =",
        "ytInitialPlayerResponse=",
      ];
      try {
        for (const s of document.querySelectorAll("script")) {
          const text = s.textContent || "";
          if (!text || text.indexOf("ytInitialPlayerResponse") === -1) continue;
          for (const mk of MARKERS) {
            const i = text.indexOf(mk);
            if (i === -1) continue;
            const obj = youtube.extractBalancedJson(text, i + mk.length);
            if (obj && obj.videoDetails) return obj;
          }
        }
      } catch (e) {
        console.warn("[Vault] 读取 ytInitialPlayerResponse 失败:", e && e.message);
      }
      return null;
    },

    // 页面内那份 playerResponse 是否**确实属于** videoId 这条视频。
    //
    // YouTube 是 SPA，切视频不刷新整页 → `ytInitialPlayerResponse` 会停在
    // 上一个视频。用它取标题/作者/日期就会张冠李戴（见 extractContent 的注释）。
    // 拿 videoDetails.videoId 一对一核对即可 —— 这是最直接、最不可伪造的证据。
    //
    // 核对**只用于**元数据是否可信；字幕不依赖它（字幕按 videoId 重新拉取）。
    playerMatches(player, videoId) {
      if (!player || !videoId) return false;
      const vd = player.videoDetails || {};
      const got = String(vd.videoId || "").trim();
      if (!got) {
        // 极少数响应不带 videoId：不能证明它属于本条视频 → 一律视为不可信，
        // 宁可退回页面标题（至少是当前页面的），也不用可能错配的元数据。
        return false;
      }
      return got === String(videoId).trim();
    },

    // 从 start 处找第一个 `{`，按括号配对切出完整 JSON。
    // 不能用 /{.*?}/ —— 非贪婪会在第一个 `}` 就截断，遇到嵌套对象必挂。
    extractBalancedJson(text, start) {      const begin = text.indexOf("{", start);
      if (begin === -1) return null;
      let depth = 0;
      let inStr = false;
      let esc = false;
      for (let i = begin; i < text.length; i++) {
        const ch = text[i];
        if (inStr) {
          if (esc) { esc = false; continue; }
          if (ch === "\\") { esc = true; continue; }
          if (ch === '"') inStr = false;
          continue;
        }
        if (ch === '"') { inStr = true; continue; }
        if (ch === "{") depth++;
        else if (ch === "}") {
          depth--;
          if (depth === 0) {
            try { return JSON.parse(text.slice(begin, i + 1)); }
            catch (e) { return null; }
          }
        }
      }
      return null;
    },

    // ==================================================================
    // 字幕：通道 ① InnerTube（ANDROID / IOS）
    // ==================================================================

    // InnerTube 接口的 host。
    // 不能无脑用 location.origin —— 在 `youtu.be/xxx` 上 match() 也成立，
    // 但 youtu.be 根本没有 /youtubei/* 这个路由（会 404）。
    // 只有 youtube.com 家族才用当前 origin（保证同源、免 CORS）。
    apiOrigin() {
      const h = (location.hostname || "").toLowerCase();
      if (/(^|\.)youtube\.com$/.test(h)) {
        return location.origin || ("https://" + h);
      }
      return "https://www.youtube.com";
    },

    // 网页自己在用的 InnerTube key 就在内联 <script> 里（`ytcfg.set({...})`）。
    // DOM 是共享的，所以隔离世界读得到。
    innertubeApiKey() {
      try {
        for (const s of document.querySelectorAll("script")) {
          const t = s.textContent || "";
          if (t.indexOf("INNERTUBE_API_KEY") === -1) continue;
          const m = t.match(/"INNERTUBE_API_KEY"\s*:\s*"([A-Za-z0-9_-]+)"/);
          if (m) return m[1];
        }
      } catch (e) { /* ignore */ }
      return "";
    },

    // 读取页面 ytcfg 里已经初始化好的 InnerTube 上下文。
    //
    // **这是「浏览器已登录却被判 LOGIN_REQUIRED」的第二个根因。**
    //
    // `/youtubei/v1/player` 认定「你是不是同一个会话」靠的不只是 cookie，
    // 还要 body 里的 `context` 与页面当前会话**对得上**：
    //   · `visitorData` —— 页面初始化时由 YouTube 下发的访客标识。
    //     服务端把它和会话绑定；请求里不带（或带一个自己编的），
    //     YouTube 就当它是一个**陌生上下文**，直接回 LOGIN_REQUIRED
    //     「请登录，以便我们确认你不是聊天机器人」。
    //   · `client.*` 的具体字段（clientName / clientVersion / hl / gl /
    //     userAgent）也以页面 ytcfg 为准，浏览器与 YouTube 谈好的版本
    //     比我们硬编码的更贴合当前会话。
    //
    // 页面 ytcfg 就写在 `<script>` 里，DOM 共享，隔离世界读得到。
    ytcfgValue(key) {
      try {
        if (window.ytcfg && typeof window.ytcfg.get === "function") {
          const v = window.ytcfg.get(key);
          if (v !== undefined && v !== null) return v;
        }
      } catch (e) { /* 隔离世界拿不到 window.ytcfg 时继续走 DOM */ }
      try {
        for (const s of document.querySelectorAll("script")) {
          const t = s.textContent || "";
          if (t.indexOf(key) === -1) continue;
          const m = t.match(new RegExp('"' + key + '"\\s*:\\s*"([^"]+)"'));
          if (m) return m[1];
        }
      } catch (e) { /* ignore */ }
      return "";
    },

    // 页面会话标识：visitorData 是核心，datasyncId / delegatedSessionId 是补充。
    visitorData() {
      return String(youtube.ytcfgValue("VISITOR_DATA") ||
                    youtube.ytcfgValue("visitorData") || "");
    },

    // 单次 InnerTube 调用。**不抛异常**：返回 { tracks, note }。
    async innertubeCall(origin, key, c, videoId, withHeaders) {
      const headers = { "Content-Type": "application/json" };
      if (withHeaders) {
        headers["X-Goog-Api-Key"] = key;
        headers["X-Youtube-Client-Name"] = String(c.num);
        headers["X-Youtube-Client-Version"] = c.client.clientVersion;
      }
      const url = origin + "/youtubei/v1/player?key=" + encodeURIComponent(key) + "&prettyPrint=false";

      // context 必须带上页面会话的 visitorData，否则会被当成陌生会话。
      const visitor = youtube.visitorData();
      const clientCtx = Object.assign(
        { hl: "zh-CN", gl: "US", userAgent: c.ua }, c.client);
      if (visitor) clientCtx.visitorData = visitor;

      const ctx = { client: clientCtx };
      if (visitor) ctx.visitorData = visitor;

      let res;
      try {
        res = await fetch(url, {
          method: "POST",
          credentials: "include",
          headers,
          body: JSON.stringify({
            context: ctx,
            videoId: videoId,
            contentCheckOk: true,
            racyCheckOk: true,
          }),
        });
      } catch (e) {
        return { tracks: [], note: "请求异常：" + ((e && e.message) || e) };
      }
      if (!res.ok) return { tracks: [], note: "HTTP " + res.status };

      let j = null;
      try { j = await res.json(); } catch (e) { return { tracks: [], note: "响应不是 JSON" }; }

      const tracks = youtube.tracksOf(j);
      if (tracks.length) return { tracks, note: "" };

      const ps = (j && j.playabilityStatus) || {};
      return {
        tracks: [],
        note: "无字幕轨（" + (ps.status || "无状态") + (ps.reason ? "：" + ps.reason : "") + "）",
      };
    },

    // 向 InnerTube 问一次 `/player`，返回 { tracks, client, note }。
    //
    // 客户端顺序见 INNERTUBE_CLIENTS 的注释：先用**吃 cookie 的**电视/内嵌客户端
    // （与浏览器登录态一致），再用 ANDROID/IOS 兜底（它们不吃 cookie，
    // 但在「视频本身没有字幕轨」这类场景下仍能给出有效的空轨结论）。
    async innertubePlayer(videoId) {
      if (!videoId) return { tracks: [], client: "", note: "缺 videoId", loggedOut: false };

      const origin = youtube.apiOrigin();
      const pageKey = youtube.innertubeApiKey();
      const keys = [];
      for (const k of [pageKey].concat(FALLBACK_API_KEYS)) {
        if (k && keys.indexOf(k) === -1) keys.push(k);
      }
      // 页面 key 优先；兜底 key 只留一个，别把请求数堆高（字幕接口有频控）
      const useKeys = keys.slice(0, 2);

      const notes = [];
      // 只有**吃 cookie 的客户端**报 LOGIN_REQUIRED 才算「用户没登录」；
      // 移动客户端在任何情况下都可能这么报（它们本来就不认浏览器 cookie），
      // 拿它当证据会冤枉一个明明登录了的用户。
      let cookieClientSaysLoginRequired = false;

      for (const c of INNERTUBE_CLIENTS) {
        const isCookieAuth = COOKIE_AUTH_CLIENTS.indexOf(c.name) !== -1;
        let clientNote = "";
        for (const key of useKeys) {
          // 先带客户端头（InnerTube 在少数场景据头判客户端）；
          // 带头这一发失败就退成「只发 JSON body」的同一发，
          // 免得因为一个可选头（例如被当成跨域触发预检）整条路都走不通。
          for (const withHeaders of [true, false]) {
            const r = await youtube.innertubeCall(origin, key, c, videoId, withHeaders);
            if (r.tracks.length) {
              return { tracks: r.tracks, client: c.name, note: "", loggedOut: false };
            }
            clientNote = r.note;
            if (!withHeaders) break;   // 无头版再失败 → 换 key
          }
        }
        if (isCookieAuth && /LOGIN_REQUIRED/.test(clientNote)) {
          cookieClientSaysLoginRequired = true;
        }
        notes.push(c.name + "：" + clientNote);
      }
      return {
        tracks: [], client: "", note: notes.join("；"),
        loggedOut: cookieClientSaysLoginRequired,
      };
    },

    // ==================================================================
    // 字幕：候选轨收集 + 选择
    // ==================================================================
    tracksOf(player) {
      const t = player &&
        player.captions &&
        player.captions.playerCaptionsTracklistRenderer &&
        player.captions.playerCaptionsTracklistRenderer.captionTracks;
      return Array.isArray(t) ? t : [];
    },

    // `exp=xpe` / `potc=1` = 这条 URL 被 PO Token 保护，直接请求只会拿到空 body。
    isPoTokenProtected(url) {
      const u = String(url || "");
      return /(?:[?&])exp=xpe(?:&|$)/.test(u) || /(?:[?&])potc=1(?:&|$)/.test(u);
    },

    langScore(track) {
      const l = String((track && (track.languageCode || track.vssId)) || "").toLowerCase();
      if (l.startsWith("zh")) return 0;          // 中文优先
      if (l.startsWith("en")) return 1;          // 其次英文
      return 2;
    },

    // 历史 API：从单个 playerResponse 里挑一条轨道（离线测试与兜底路径仍在用）
    pickCaptionTrack(player) {
      const tracks = youtube.tracksOf(player);
      if (!tracks.length) return null;
      return tracks.slice().sort((a, b) => youtube.langScore(a) - youtube.langScore(b))[0];
    },

    // 组装下载 URL：统一用 URL API 改 fmt（不要手拼字符串 —— baseUrl 里
    // 可能已经有 `&fmt=srv3`，手拼会留下两个 fmt，行为取决于服务端取哪个）。
    // stripPot=true 时摘掉 exp / potc / pot（「去掉 exp=xpe 再试」是常见的兜底手段）。
    buildCaptionUrl(baseUrl, opts) {
      const o = opts || {};
      try {
        const u = new URL(baseUrl, youtube.apiOrigin());
        if (o.stripPot) {
          u.searchParams.delete("exp");
          u.searchParams.delete("potc");
          u.searchParams.delete("pot");
        }
        if (o.fmt === "") u.searchParams.delete("fmt");
        else u.searchParams.set("fmt", o.fmt || "json3");
        return u.toString();
      } catch (e) {
        return String(baseUrl || "");
      }
    },

    // 汇总所有来源的候选轨：
    //   ① InnerTube（TVHTML5 → WEB_EMBEDDED → ANDROID → IOS）—— 前两个吃 cookie
    //   ② 页面内联 playerResponse —— 几乎必然带 exp=xpe，只能兜底
    // 排序：非 PO Token 保护的排前面；同组内 zh → en → 其它。
    // 去重：按 lang+kind（两个来源的同一轨只留更好的那条），最多留 4 条。
    async collectCaptionCandidates(pagePlayer, videoId) {
      const sources = [];
      const notes = [];

      const it = await youtube.innertubePlayer(videoId);
      if (it.tracks.length) {
        sources.push({ from: "InnerTube/" + it.client, tracks: it.tracks });
      } else if (it.note) {
        notes.push("InnerTube：" + it.note);
      }

      // 页面内联 playerResponse —— **必须放在 InnerTube 之后但要标注优先级**。
      //
      // 为什么不能只靠它：页面内的 captionTracks[].baseUrl 几乎必然带
      //   `exp=xpe`（PO Token 保护），直接请求只会拿到 200 + 0 字节。
      // 为什么又不能没有它：在**手动剪藏**（页面已充分初始化）的场景下，
      //   页面那份 playerResponse 是 InnerTube 之外的唯一信息来源，
      //   而且它的 videoId 已经过上层 playerMatches() 校验，可信。
      const pageTracks = youtube.tracksOf(pagePlayer);
      if (pageTracks.length) sources.push({ from: "页面", tracks: pageTracks });

      const flat = [];
      for (const s of sources) {
        for (const t of s.tracks) {
          flat.push({ track: t, from: s.from, pot: youtube.isPoTokenProtected(t.baseUrl) });
        }
      }
      flat.sort((a, b) => {
        if (a.pot !== b.pot) return a.pot ? 1 : -1;
        return youtube.langScore(a.track) - youtube.langScore(b.track);
      });

      const seen = new Set();
      const out = [];
      for (const f of flat) {
        const key = [f.track.languageCode || "", f.track.vssId || "", f.track.kind || ""].join("|");
        if (seen.has(key)) continue;
        seen.add(key);
        out.push(f);
        if (out.length >= 4) break;
      }
      return {
        candidates: out,
        note: notes.join("；"),
        hasAnyTrack: flat.length > 0,
        // 只有吃 cookie 的客户端说「要登录」，才真的是用户没登录
        loggedOut: !!it.loggedOut,
      };
    },

    // ------------------------------------------------------------------
    // 下载并解析一条字幕轨 → { text, note }
    // ------------------------------------------------------------------
    async downloadTrack(url) {
      if (!url) return { text: "", note: "空 URL" };

      let res;
      try {
        res = await fetch(url, { credentials: "include" });
      } catch (e) {
        return { text: "", note: "字幕请求失败：" + ((e && e.message) || e) };
      }
      if (!res.ok) return { text: "", note: "字幕请求 HTTP " + res.status };

      let raw = "";
      try { raw = await res.text(); } catch (e) { return { text: "", note: "读取字幕响应失败" }; }
      if (!raw) {
        // 这一条是 PO Token 拦截的**特征指纹**：200 但 0 字节，服务端「拒绝」
        // 却连状态码都不给 —— 不把这句话写进 note，下次还是查不出来。
        return { text: "", note: "HTTP 200 但响应体为空（YouTube 的 PO Token 风控拦截的典型表现）" };
      }

      const head = raw.slice(0, 200).trimStart();
      let text = "";
      if (head.startsWith("{") || head.startsWith("[")) {
        try { text = youtube.json3ToText(JSON.parse(raw)); } catch (e) { text = ""; }
      }
      if (!text) text = youtube.xmlToText(raw);
      if (text) return { text, note: "" };

      if (/<html|<!doctype/i.test(head)) {
        return { text: "", note: "字幕请求返回的是网页而非字幕（可能被风控或登录墙拦截）" };
      }
      return { text: "", note: "字幕响应无法解析（" + raw.length + " 字节）" };
    },

    // 总入口：拿到字幕文本，或拿到一个**说清楚为什么没拿到**的 note。
    async grabSubtitles(pagePlayer, videoId) {
      const collected = await youtube.collectCaptionCandidates(pagePlayer, videoId);
      const cands = collected.candidates;

      if (!cands.length) {
        const why = collected.note || "页面未提供字幕信息";
        // 吃 cookie 的客户端说要登录 → 是**登录态**问题，不是视频没字幕。
        // 必须分开说：否则一个明明登录了的用户会被告知「这视频可能没字幕」，
        // 于是永远找不到真正的原因（见 INNERTUBE_CLIENTS 的注释）。
        if (collected.loggedOut) {
          return {
            text: "", lang: "", source: "", potBlocked: false, loggedOut: true,
            note: "YouTube 要求登录后才能读取字幕数据：" + why,
          };
        }
        return {
          text: "", lang: "", source: "", potBlocked: false, loggedOut: false,
          note: why + "（该视频可能确实没有字幕/转录）",
        };
      }

      const tried = [];
      for (const c of cands) {
        // 每条候选最多试两个 URL：带 pot 的先摘掉 pot 再试，否则直接试。
        const variants = [];
        if (c.pot) variants.push(youtube.buildCaptionUrl(c.track.baseUrl, { stripPot: true }));
        variants.push(youtube.buildCaptionUrl(c.track.baseUrl, {}));

        for (const u of variants) {
          const r = await youtube.downloadTrack(u);
          if (r.text) {
            return {
              text: r.text,
              lang: c.track.languageCode || c.track.vssId || "",
              source: c.from,
              potBlocked: false,
              loggedOut: false,
              note: "",
            };
          }
          tried.push(c.from + "/" + (c.track.languageCode || "?") + "：" + r.note);
        }
      }

      const anyPot = cands.some((c) => c.pot);
      return {
        text: "", lang: "", source: "", potBlocked: anyPot, loggedOut: false,
        note: "字幕轨道存在但未能获取内容：" + tried.slice(0, 3).join("；") +
          (anyPot ? "。该字幕受 YouTube 的 PO Token 风控保护，页面内通道未取得可用地址。" : ""),
      };
    },

    // ------------------------------------------------------------------
    // 解析：json3 / XML(srv1 & srv3)
    // ------------------------------------------------------------------

    // json3 → `[HH:MM:SS] 文本`（与后端 _parse_json3 / bilibili.js 格式一致）
    json3ToText(data) {
      const events = (data && data.events) || [];
      const lines = [];
      for (const ev of events) {
        if (!ev || !Array.isArray(ev.segs)) continue;
        const txt = ev.segs.map((s) => (s && s.utf8) || "").join("").replace(/\s+/g, " ").trim();
        if (!txt) continue;
        lines.push("[" + youtube.hms(Math.floor((ev.tStartMs || 0) / 1000)) + "] " + txt);
      }
      // 相邻重复行去掉（自动字幕滚动时会重复上一句）
      const out = [];
      for (const l of lines) {
        if (out.length && out[out.length - 1].slice(11) === l.slice(11)) continue;
        out.push(l);
      }
      return out.join("\n");
    },

    // XML 兜底。要同时认两种格式 —— 只认 `<text>` 是不够的：
    //   srv3：<p t="1200" d="3000">文字</p>     （现代 YouTube 的默认 XML 形态）
    //   srv1：<text start="1.2" dur="3">文字</text>
    // 上面的老实现只找 <text>，遇到 srv3 会得到 0 行 —— 又是一个「静默空」。
    xmlToText(text) {
      if (!text) return "";
      try {
        const doc = new DOMParser().parseFromString(text, "text/xml");

        const ps = doc.querySelectorAll("p");
        if (ps.length) {
          const lines = [];
          ps.forEach((n) => {
            const t = youtube.decodeEntities(String(n.textContent || "").replace(/\s+/g, " ").trim());
            if (!t) return;
            lines.push("[" + youtube.hms(Math.floor((Number(n.getAttribute("t")) || 0) / 1000)) + "] " + t);
          });
          if (lines.length) return lines.join("\n");
        }

        const ts = doc.querySelectorAll("text");
        if (ts.length) {
          const lines = [];
          ts.forEach((n) => {
            const t = youtube.decodeEntities(String(n.textContent || "").replace(/\s+/g, " ").trim());
            if (!t) return;
            lines.push("[" + youtube.hms(Math.floor(Number(n.getAttribute("start")) || 0)) + "] " + t);
          });
          if (lines.length) return lines.join("\n");
        }
      } catch (e) { /* 交给上层报「无法解析」 */ }
      return "";
    },

    // 字幕 XML 里的实体是**双重转义**的（`&amp;#39;`），所以要多跑一遍，
    // 且 `&amp;` 必须在每轮里最后替换，否则 `&amp;lt;` 会被先还原成 `<` 再被二次解析。
    decodeEntities(s) {
      let t = String(s || "");
      for (let pass = 0; pass < 2 && t.indexOf("&") !== -1; pass++) {
        const before = t;
        t = t
          .replace(/&#(\d+);/g, (m, d) => {
            try { return String.fromCodePoint(Number(d)); } catch (e) { return m; }
          })
          .replace(/&#x([0-9a-fA-F]+);/g, (m, h) => {
            try { return String.fromCodePoint(parseInt(h, 16)); } catch (e) { return m; }
          })
          .replace(/&quot;/g, '"')
          .replace(/&apos;/g, "'")
          .replace(/&lt;/g, "<")
          .replace(/&gt;/g, ">")
          .replace(/&amp;/g, "&");
        if (t === before) break;
      }
      return t;
    },

    hms(total) {
      const h = Math.floor(total / 3600);
      const m = Math.floor((total % 3600) / 60);
      const s = Math.floor(total % 60);
      const pad = (n) => String(n).padStart(2, "0");
      return pad(h) + ":" + pad(m) + ":" + pad(s);
    },
  };

  window.__clipVault.platforms.youtube = youtube;
})();

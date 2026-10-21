#!/usr/bin/env python3
"""
知识 Vault 剪藏接收服务
接收浏览器插件/API 发送的网页/视频内容，写入 raw/articles/ 目录。

支持：
- 浏览器插件直接推送正文内容
- URL 智能分发：已知平台后端直接抓，其他走浏览器协助
- 浏览器协助队列：SSE 长连接，浏览器开着就能后台自动抓取

用法：
    python3 tools/clip-server.py
    python3 tools/clip-server.py --port 7878
"""
from __future__ import annotations
import argparse
import hmac
import ipaddress
import json
import os
import re
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from http.server import HTTPServer, BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlunparse, urlencode, urljoin, unquote

import requests

# ========== 日志安全：敏感信息过滤 ==========

_SENSITIVE_PATTERNS = [
    (re.compile(r'(Bearer\s+)[A-Za-z0-9_\-\.]+', re.I), r'\1***'),
    (re.compile(r'(app_secret["\s:=]+)[^\s,}\]]+', re.I), r'\1***'),
    (re.compile(r'(appSecret["\s:=]+)[^\s,}\]]+', re.I), r'\1***'),
    (re.compile(r'("token"["\s:=]+)[^\s,}\]]+', re.I), r'\1***'),
    (re.compile(r'(password["\s:=]+)[^\s,}\]]+', re.I), r'\1***'),
    (re.compile(r'(api[_-]?key["\s:=]+)[^\s,}\]]+', re.I), r'\1***'),
    (re.compile(r'(Authorization["\s:=]+)[^\s,}\]]+', re.I), r'\1***'),
    (re.compile(r'(secret["\s:=]+)[^\s,}\]]+', re.I), r'\1***'),
]


def sanitize_for_log(text: str) -> str:
    """替换日志文本中可能出现的敏感信息（Token、密钥、密码等）。

    用法：print(sanitize_for_log(f"请求头: {headers}"))
    """
    if not isinstance(text, str):
        text = str(text)
    for pattern, replacement in _SENSITIVE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


# 平台适配器系统
sys.path.insert(0, str(Path(__file__).parent))
from platforms import extract_url, get_platform, list_platforms
from platforms.base import ClipResult, NoBrowserFallback
from platforms.feishu import feishu_media_token, download_media
from task_queue import queue as task_queue
# 「真实正文」的度量口径（剥脚手架、阈值）统一在 content_measure.py —— 与下游
# ocr_images.py 共用同一份实现，避免两边口径漂移（2026-10-14 踩过，见该模块说明）。
import content_measure

VAULT = Path(__file__).resolve().parent.parent
ARTICLES_DIR = VAULT / "raw" / "articles"
ARTICLES_DIR.mkdir(parents=True, exist_ok=True)

from wiki_gen import queue_wiki_generation as _queue_wiki_gen
from wiki_gen import run_wiki_generation_sync as _run_wiki_gen_sync
from wiki_gen import record_wiki_skipped as _record_wiki_skipped
from wiki_gen import is_available as _wiki_is_available

VAULT_INBOX = VAULT / "raw" / "inbox"
VAULT_INBOX.mkdir(parents=True, exist_ok=True)

# 自动精加工质量门禁：浏览器抓回登录墙/验证页时内容极短，
# 直接精加工只会白花 token 费用，改为跳过、留待人工确认。
# 阈值只在 content_measure.py 定义一处 —— ocr_images 的候选筛选读的是同一个值。
_AUTO_REFINE_MIN_CHARS = content_measure.MIN_REAL_TEXT_CHARS
_AUTO_REFINE_BLOCK_KEYWORDS = (
    "安全验证", "请登录", "登录后查看", "验证码", "环境异常",
    "权限不足", "访问被拒绝", "抓取失败",
)

# 「图多字少」判据：剔图/注释/链接/元数据后的**真实文字**少于 _AUTO_REFINE_MIN_CHARS，
# 且至少含这么几张图 —— 文字很可能写在图片里（小红书长图 / 微信图片消息常见）。
# 为什么单列而不并进普通长度门禁：图片 markdown（尤其远程 URL）会混进字数统计，
# 同一篇里几张图的链接就能顶过 200 字符，于是「文字其实极少」被掩盖、照常生成
# 一页低质 wiki。必须先把图剔掉再数，才能看出「图多字少」。
_TEXT_IN_IMAGES_MIN_IMAGES = content_measure.MIN_IMAGES

# 风控 / 人机验证拦截页的**识别指纹**。
#
# 为什么单列：这类页面本身有几百字（一段解释文字 + 排查指引），长度检查拦不住，
# 关键词表也未必命中 —— 于是被当成正常正文写进 raw、还生成了 wiki。
# 实际踩过：YouTube 抓取被 Google 验证码页劫持，`google.com/sorry/?continue=...`
# 被完整入库，raw 的 source_url 变成了验证码地址（并连带导致待审指针摘不掉）。
#
# 命中后**必须**按「本次抓取被拦截」处置：不生成 wiki、不留 raw、退回待审区，
# 并把「被谁拦的」写进给用户看的原因 —— 这决定了用户该「稍后重试」还是「换网络」。
_INTERSTITIAL_URL_MARKERS = (
    "/sorry/", "google.com/sorry", "captcha", "/challenge", "verify",
    "cf-chl", "cloudflare", "attention-required",
)
_INTERSTITIAL_TEXT_MARKERS = (
    "异常流量", "人机识别系统", "自动程序", "unusual traffic",
    "确认这些请求是由您而不是自动程序发出", "not a robot",
    "verify you are human", "checking your browser",
)


def _interstitial_reason(data) -> str:
    """判断本次抓取是否落到了风控 / 人机验证拦截页。

    返回非空字符串 = 被拦截的**原因**（直接给用户看）；空字符串 = 正常页面。
    判据取「URL 指纹」与「正文指纹」两条，任一命中即算 —— 单看 URL 漏掉
    Cloudflare 这类不改地址的拦截，单看正文则可能误伤讨论「验证码」的文章。
    """
    url = str(data.get("url") or data.get("source_id") or "").lower()
    content = str(data.get("content") or "")
    # 统一转小写再匹配：拦截页的英文提示大小写不定
    # （"Checking your browser" / "Not a robot"），而中文不受影响。
    head = content[:1500].lower()

    for m in _INTERSTITIAL_URL_MARKERS:
        if m in url:
            return f"抓取被风控拦截（地址命中「{m}」，通常是人机验证页）"
    for m in _INTERSTITIAL_TEXT_MARKERS:
        if m in head:
            return f"抓取被风控拦截（正文命中「{m}」，通常是人机验证页）"
    return ""


def _wiki_ref_for(filepath) -> str:
    """从 raw 的 frontmatter 读第一条 `wiki_refs`（wiki 页面的相对路径）。

    wiki 生成成功后 wiki_gen 会把页面路径写回 raw 的 `wiki_refs` —— 这是
    「这篇 raw 对应哪个 wiki 页」的**唯一权威来源**。不要靠标题猜 slug：
    同一篇内容的 slug 会随标题变化（实测同一篇曾先后是 `…-trial.md` 与
    `…-review.md`），猜出来的名字会指向一个不存在的文件。
    """
    try:
        text = Path(filepath).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    # 统一换行再解析：Windows 上写出的文件是 CRLF，而这些正则按 \n 写
    # （`wiki_refs:` 后跟的是 `\r\n`，不归一化会一条也匹配不到）。
    text = text.replace("\r\n", "\n")
    block = re.search(r'^wiki_refs:[ \t]*\n((?:[ \t]*-[ \t]*\S.*\n)+)', text, re.M)
    if block:
        return (block.group(1).strip().splitlines()[0]
                .lstrip("-").strip().strip('"').strip("'"))
    inline = re.search(r'^wiki_refs:[ \t]*\[(.*?)\]', text, re.M)   # 行内数组写法
    if inline:
        items = [x.strip().strip('"').strip("'")
                 for x in inline.group(1).split(",") if x.strip()]
        return items[0] if items else ""
    return ""


def _settle_task_async(task_id, filepath, wiki):
    """把后台异步加工（wiki / OCR+wiki）的最终结论写回任务记录。

    为什么必须回写：raw 落盘时 wiki 还在后台线程里跑，任务只能以
    `result.asyncPending=True` 收尾；加工结束后若不回写，调用方（飞书机器人
    的轮询、任务面板）就永远停在「⏳ 后台生成中」—— 用户不知道最后成没成。
    对「图多字少」的笔记尤其明显：OCR 要走几十秒到几分钟，而它恰恰是唯一
    能把 wiki 救回来的环节（用户 2026-10-14 报：OCR 排队路径跑完没有通知）。

    `task_id` 为空（如 dry-run、待审区同步路径）时静默跳过。
    """
    if not task_id:
        return
    wiki = wiki if isinstance(wiki, dict) else {}
    patch: dict = {"wiki": wiki, "asyncPending": False}
    name = Path(filepath).name
    try:
        patch["filepath"] = str(Path(filepath).relative_to(VAULT))
        patch["filename"] = name
    except Exception:
        pass

    demoted = wiki.get("status") == "demoted"
    # 重算字数：OCR 是**在入库之后**往笔记里补文字的（实测补进 1400+ 字），
    # 而任务记录里的 wordCount 是入库那一刻的快照。不刷新的话，后台加工完成的
    # 通知会一边写「✅ 已生成（含图片文字）」一边显示「约 28 字」—— 自相矛盾，
    # 用户会以为字数统计漏了图片内容（2026-10-14 用户就是照这张卡问的）。
    # 口径同 content_measure.count_words：剥掉图片链接/元数据/骨架/话题标签，
    # 所以补进 `## 图片文字（OCR）` 的识别文字会被计入，图片本身不计。
    if not demoted:
        try:
            patch["wordCount"] = _count_words(
                Path(filepath).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass
        # wiki 页面路径：落定时已生成（或已失败），从 raw 的 wiki_refs 取权威值，
        # 供「Wiki：<文件名>」这类展示使用。
        ref = _wiki_ref_for(filepath)
        if ref:
            patch["wikiFile"] = ref

    # 素材被撤回待审区 → 任务里不该再留一个已经不存在的文件
    if demoted:
        patch["filename"] = ""
        patch["filepath"] = ""
    try:
        task_queue.settle_async_result(task_id, patch)
        print(f"[i] 后台加工已落定，任务记录已更新 [{task_id}]："
              f"wiki={wiki.get('status', '')}")
    except Exception as e:      # 回写失败不该影响加工本身
        print(f"[!] 回写后台加工结论失败 [{task_id}]: {e}")


def _after_wiki(filepath, data, result, task_id=None):
    """wiki 生成结束后收尾：**失败时不留「孤儿 raw」**。

    规则（用户明确要求）：点「入库」就是要 raw + wiki 一起产出。
    wiki 没生成却把 raw 留在 raw/articles/，只会攒下一堆半成品，等 lint 报
    `raw:orphan` 才发现 —— 那时人早就走了。所以失败时把 raw 撤回、改存待审区：
    URL 与标题都留着，通道恢复后可一键重跑。

    但有两条**必须保留 raw** 的例外：

    1. **从未配置过任何 wiki 通道**（Tier 3 的刻意状态 —— 用户就是不想用 LLM）。
       这时按项目原设计 raw 正常入库，否则这类用户永远存不进东西。
       只有「本来有通道、这次调用失败」（额度用尽 / key 失效 / 网络）才降级。
       注意这条**只对 wiki 失败成立**：素材本身不完整（视频没字幕）时，
       无论有没有 wiki 通道都该退回待审区 —— 那是内容问题，不是通道问题。
       见 `_content_incomplete_reason()`。
    2. **本次是覆盖写入**：旧版本在写入前已被删掉，再撤新的会导致两版全丢。
       （这条在 `_demote_to_inbox()` 里，两种降级共用。）

    真正的撤回动作统一在 `_demote_to_inbox()` 里。
    """
    if not isinstance(result, dict) or result.get("status") != "failed":
        # 加工成功（ok）/ 未过门禁（skipped）—— 都是确定结论，直接落定任务记录
        _settle_task_async(task_id, filepath, result)
        return result

    if not _wiki_is_available():
        # 从未配置过 wiki 通道（刻意不用 LLM）：raw 保留，结论也是明确的
        _settle_task_async(task_id, filepath, result)
        return result

    reason = str(result.get("reason") or "wiki 未生成")
    demoted = _demote_to_inbox(filepath, data, result, reason, "wiki_failed")
    _settle_task_async(task_id, filepath,
                       demoted if isinstance(demoted, dict) else result)
    return demoted


def _demote_to_inbox(filepath, data, result: dict, reason: str, kind: str) -> dict:
    """撤回 raw → 在待审区留一张指针卡片。**两种触发共用一个出口**：

    - `kind="wiki_failed"` —— 素材完整，但 wiki 没生成（额度/key/网络）
    - `kind="content_incomplete"` —— 素材本身不完整（视频没字幕/转录）：
      连 wiki 都不该生成，raw 更没资格留在 `raw/articles/`

    这是三种触发共用的出口：

    - `kind="wiki_failed"` —— 素材完整，但 wiki 没生成（额度/key/网络）
    - `kind="content_incomplete"` —— 素材本身不完整（视频没字幕/转录）：
      连 wiki 都不该生成，raw 更没资格留在 `raw/articles/`
    - `kind="interstitial"` —— 抓回来的是风控 / 人机验证页而非正文：
      同样不是「素材」，而是「这次没抓到」，退回待审区等待重试

    共用的理由：处置完全一样（撤 raw + 建卡 + 记原因），差别只在
    给用户看的措辞（`prefix`），以及待审卡片 note 的前缀。

    **必须保留 raw 的例外**：本次是覆盖写入。旧版本在写盘前已被删掉，
    再撤新的会导致两版全丢 —— 那时宁可留一份不完整的 raw。
    """
    if data.get("_replaced"):
        result["demote_skipped"] = "本次为覆盖写入，为避免新旧两版同时丢失，保留 raw"
        print("[i] 需要撤回 raw，但本次为覆盖写入：保留 raw，可用 tools/retry-wiki.py 补充 wiki")
        return result

    url = str(data.get("url") or "")
    # 建卡用**任务原始 URL**优先：抓取可能被重定向（如验证码页），此时
    # data["url"] 已是拦截页地址，拿它建卡等于让用户下次重试的还是那张拦截页。
    # `_origin_url` 由浏览器结果处理器写入（见 handle_browser_result）。
    origin = str(data.get("_origin_url") or "").strip()
    if origin:
        url = origin
    title = str(data.get("title") or "")
    path = Path(filepath)

    try:
        _delete_article_with_images(path)
    except Exception as e:
        print(f"[!] 撤回 raw 失败，保留原文件: {e}")
        return result
    print(f"[!] {reason} → 已撤回 raw：{path.name}")

    prefix = {
        "wiki_failed": "wiki 未生成",
        "content_incomplete": "内容不完整",
        "interstitial": "抓取被拦截",
    }.get(kind, "未完成")
    card = {}
    if url:
        try:
            card = _inbox_create_card(
                url, title,
                platform=str(data.get("platform") or ""),
                note=(prefix + "：" + reason)[:160],
                live_source=bool(data.get("liveSource")),
            )
        except Exception as e:
            print(f"[!] 写入待审区失败: {e}")
    # 卡片已存在时 _inbox_create_card 会返回 duplicate 且**不改 note**，
    # 于是卡片会一直保留上一轮写下的旧话（典型：「已排入浏览器协助队列…等待
    # 浏览器抓取」）—— 用户看到的就是「处理了但毫无反应」。
    # 这里必须把本次的真实结论写回去：卡片是待办指针，note 应当反映**最近一次**
    # 尝试的结果，否则它等于在说谎。
    if not card.get("filename") and card.get("existing_file"):
        _inbox_set_note(card["existing_file"], f"{prefix}：{reason}")

    result["status"] = "demoted"
    result["kind"] = kind
    result["reason"] = reason
    result["raw_removed"] = True
    if card.get("filename"):
        result["inbox_card"] = card["filename"]
    elif card.get("existing_file"):
        result["inbox_card"] = card["existing_file"]
    return result


def _content_incomplete_reason(data) -> str:
    """内容是否**结构性不完整** —— 目前唯一的判据：视频类内容拿不到字幕/转录。

    为什么这必须当成「不完整」而不是「内容略少」：
      对视频源（B 站 / YouTube），**字幕就是正文**。标题、UP 主、播放量、
      简介这些是元数据，不是内容。拿不到字幕时剩下的东西复述一遍也生不出
      wiki —— 那不是剪藏，是书签。所以既不该生成 wiki，也不该留在
      `raw/articles/`，而应退回待审区（URL 留着，字幕通道恢复后可重跑）。

    判据优先级：
      1. `extra.has_subtitles` / `extra.hasSubtitles`（结构化，服务端与扩展各自用一套命名）
      2. 都没有时退回内容标记检测（`> ⚠️ 未取到字幕` 这类行）

    返回非空字符串 = 不完整的**原因**（直接给用户看）；空字符串 = 完整。
    """
    if str(data.get("type") or "").strip().lower() != "video":
        return ""

    extra = data.get("extra")
    has_subtitles = None
    if isinstance(extra, dict):
        for key in ("has_subtitles", "hasSubtitles"):
            if key in extra:
                has_subtitles = bool(extra[key])
                break

    if has_subtitles is True:
        return ""
    if has_subtitles is None:
        # 调用方没给结构化标记 → 退回内容标记检测。
        # 「未获取到字幕」是现行写法；「未取到字幕」保留是为了兼容此前已写入的 raw。
        content = str(data.get("content") or "")
        if "未获取到字幕" not in content and "未取到字幕" not in content:
            return ""

    # 把调用方（浏览器扩展）给出的**具体**原因并进来。
    #
    # 为什么必须要：不并的话，所有字幕失败都归成同一句话「视频未取到字幕/转录」。
    # 而这几件事的处置完全不同，用户却分不出来：
    #   - 视频确实没有字幕            → 重剪也没用
    #   - HTTP 200 但响应为空（PO Token 风控）→ 换通道 / 重试有意义
    #   - 视频页没登录 / 播放器数据没就绪 → 登录、刷新即可
    # 扩展算得出这个区别（captionNote），不让它传上来就等于把唯一的诊断信息丢掉。
    detail = ""
    if isinstance(extra, dict):
        detail = str(extra.get("caption_note") or extra.get("captionNote") or "").strip()

    # 风控拦截 ≠ 视频没有字幕。分开表述，处置建议也分开。
    if _is_login_required(extra, detail):
        # 「需要登录」比一般风控更具体：用户往往**已经在浏览器里登录了**，
        # 但还是被要求登录 —— 因为请求用的是移动端客户端身份，
        # 而它不认浏览器的登录 cookie。这种情形的处置是
        # 「确认已登录 + 在页面里正常播放一遍（把 cookie 会话激活）后重试」。
        reason = ("未能获取到字幕/转录：YouTube 要求登录后才能读取字幕数据。"
                  "请确认已在浏览器中登录 YouTube，并在该视频页停留数秒"
                  "（让页面完成加载）后重新剪藏。")
        reason += f"（{detail[:150]}）"
        return reason

    if _is_risk_control(detail):
        reason = ("未能获取到字幕/转录：来源被风控拦截。"
                  "请在浏览器中登录该站点、正常播放一遍该视频"
                  "（完成人机验证）后重新剪藏。")
        reason += f"（{detail[:150]}）"
        return reason

    reason = "视频未获取到字幕/转录，正文仅有标题与简介"
    if detail:
        reason += f"（{detail[:150]}）"
        # 排除了风控指纹之后，才可以说「可能确实没有字幕」
        reason += "（该视频可能确实没有字幕/转录）"
    return reason


# 风控指纹：出现这些关键字说明**是我们没拿到**，而不是**该视频没有字幕**。
#
# 为什么必须分开表述：两者的处置完全相反 ——
#   - 「该视频确实没有字幕」→ 重试无意义，用户应放弃这条；
#   - 「被风控拦住」→ 重试**有意义**：先在浏览器里登录该站点、正常播放一遍
#     （过掉人机验证），再重新剪藏，往往就能拿到字幕。
# 过去两者共用一句「（该视频可能确实没有字幕/转录）」，于是被风控拦截的视频
# 会被用户误判成「这视频没字幕」而直接放弃 —— 明明重试一次就可能成功。
_RISK_CONTROL_MARKERS = (
    "LOGIN_REQUIRED",   # YouTube：要求登录后读取字幕数据
    "要求登录",          # 扩展给出的中文表述（loggedOut=true 时）
    "PO Token",         # YouTube：字幕接口的 BotGuard 令牌门槛
    "响应体为空",        # PO Token 拦截的典型指纹（HTTP 200 + 0 字节）
    "人机验证",          # 通用：验证码 / 异常流量拦截页
)


def _is_risk_control(detail: str) -> bool:
    """captionNote 里是否含风控指纹。"""
    d = str(detail or "")
    return any(m in d for m in _RISK_CONTROL_MARKERS)


def _is_login_required(extra: dict, detail: str) -> bool:
    """是否是「需要登录」这一类（比一般风控更具体，处置也不同）。

    扩展在 loggedOut=true 时会带 "要求登录" 字样；旧版扩展则只会给
    LOGIN_REQUIRED。两种都认，避免升级不同步时丢掉这个判断。
    """
    if isinstance(extra, dict):
        for k in ("logged_out", "loggedOut"):
            if k in extra and bool(extra[k]):
                return True
    return "LOGIN_REQUIRED" in str(detail or "") or "要求登录" in str(detail or "")


# 以下四个是 content_measure 的转发壳：保留本文件内的私有名，实现只有一份。
def _clean_for_measure(content: str) -> str:
    """剥掉剪藏脚手架、只留可见正文文字（实现见 content_measure.clean_for_measure）。"""
    return content_measure.clean_for_measure(content)


def _real_text_length(content: str) -> int:
    """真实正文字数（实现见 content_measure.real_text_length）。"""
    return content_measure.real_text_length(content)


def _count_markdown_images(content: str) -> int:
    """正文里的 Markdown 图片数量（实现见 content_measure）。"""
    return content_measure.count_markdown_images(content)


def _count_words(content: str) -> int:
    """正文词数（实现见 content_measure.count_words）。"""
    return content_measure.count_words(content)


def _text_in_images_reason(data) -> str:
    """是否为「图多字少」（正文文字极少、但含图 —— 文字很可能写在图片里）。

    返回非空字符串 = 命中的**原因**（直接给用户看）；空字符串 = 未命中。
    命中后由 `_handle_text_in_images()` 决定：有 OCR 通道就排入下游队列，
    否则如实跳过 wiki —— 绝不静默产一页垃圾。
    """
    content = str(data.get("content") or "")
    imgs = _count_markdown_images(content)
    if imgs < _TEXT_IN_IMAGES_MIN_IMAGES:
        return ""
    real = _real_text_length(content)
    if real >= _AUTO_REFINE_MIN_CHARS:
        return ""
    return (f"正文文字极少（剔图后约 {real} 字）且含 {imgs} 张图片，"
            "文字很可能写在图片中")


def _handle_text_in_images(filepath, data, reason: str, sync: bool = False,
                           task_id=None) -> dict:
    """「图多字少」的统一出口。

    有 OCR 精加工通道 → 排入**下游** OCR 队列（入库链路只排队、不干活，保持
    轻快；OCR 完成后回填笔记再重跑 wiki），状态记为 pending；没有通道 → 如实
    记为 skipped，原因写清「未做 OCR」，绝不产垃圾 wiki。

    为什么排下游而不是当场 OCR：OCR 要解码图片、可能很慢，且与「采集只做字节
    保真、不在入库链路解码不可信图」的现有原则相冲 —— 它与 convert_to_webp.py
    同层，属于 raw 落盘之后的加工。

    `sync=True`（从待审区处理）时改为**同步**跑完 OCR + wiki 再返回：那条路径
    若先回「已入库」、之后 OCR/wiki 失败又撤回 raw，就会出现「界面说已入库、
    其实条目退回待审区」的矛盾 —— 与普通 wiki 的 sync 处理同理。

    `task_id`：异步分支把它一路带到 OCR 与 wiki 的收尾回调，好让加工落定后
    能回写任务记录、再由调用方补发结论通知（否则「⏳ 后台生成中」没有下文）。
    """
    name = Path(filepath).name

    # 本次明确跳过 OCR（插件取消勾选 / 飞书消息加「不OCR」前缀）→ 如实记 skipped。
    # 必须是**确定结论**：跳过是终态，不能让任务停在 pending —— 否则又变成
    # 「界面说后台生成中、却永远等不到结论」（与 _settle_task_async 那一族同因）。
    # 注意这是「主动叫停」而非「默认不跑」：默认仍然自动提取，所以忘了选不会漏。
    if data.get("ocr") is False:
        print(f"[i] {reason} —— 本次选择跳过图片文字提取：{name}")
        return _record_wiki_skipped(filepath, reason + "，本次选择跳过图片文字提取")

    import_ok = True
    try:
        from ocr_images import is_available as _ocr_available
        from ocr_images import process_raw as _ocr_process
        from ocr_images import queue_processing as _queue_ocr
    except Exception as e:      # 模块缺失/损坏都不该拖垮入库
        print(f"[!] OCR 模块不可用（{type(e).__name__}: {e}），按未配置处理")
        import_ok = False
        _ocr_available = _ocr_process = _queue_ocr = None

    if not import_ok or not _ocr_available():
        print(f"[i] {reason} —— 无可用 OCR 通道，跳过 wiki：{name}")
        return _record_wiki_skipped(filepath, reason + "，未做 OCR")

    if sync:
        result = _ocr_process(filepath)
        if not isinstance(result, dict) or not result.get("ok"):
            why = str((result or {}).get("reason") or "OCR 未提取到文字")
            print(f"[i] OCR 未提取到文字（{why}），跳过 wiki：{name}")
            return _record_wiki_skipped(filepath, "正文在图片中，" + why)
        print(f"[+] OCR 提取到约 {result.get('chars', 0)} 字"
              f"（{result.get('engine', '')}），生成 wiki：{name}")
        return _after_wiki(filepath, data, _run_wiki_gen_sync(filepath))

    print(f"[i] {reason} —— 已排入 OCR 精加工队列：{name}")
    _queue_ocr(filepath, lambda r: _after_ocr(filepath, data, r, task_id))
    return {"status": "pending",
            "reason": reason + "，已排入 OCR 精加工队列（完成后自动生成 wiki）",
            "tier": "ocr"}


def _after_ocr(filepath, data, result, task_id=None):
    """OCR **下游异步**任务收尾：成功则用补入的正文重跑 wiki；失败则记为 skipped。

    result 来自 ocr_images.process_raw()：{"ok": bool, "chars": int,
    "engine": str, "reason": str}。
    """
    name = Path(filepath).name
    if not isinstance(result, dict) or not result.get("ok"):
        reason = str((result or {}).get("reason") or "OCR 未提取到文字")
        print(f"[i] OCR 未提取到文字（{reason}），跳过 wiki：{name}")
        skipped = _record_wiki_skipped(filepath, "正文在图片中，" + reason)
        # OCR 是这条异步链的终点 → 必须落定任务记录，否则调用方一直等
        _settle_task_async(task_id, filepath, skipped)
        return
    print(f"[+] OCR 提取到约 {result.get('chars', 0)} 字"
          f"（{result.get('engine', '')}），重跑 wiki：{name}")
    # 复用与普通入库同一条 wiki 通道与收尾逻辑（失败同样按规则撤回 raw）。
    _queue_wiki_gen(filepath, data,
                    on_complete=lambda w: _after_wiki(filepath, data, w, task_id))


def _maybe_queue_wiki_gen(filepath, data, sync: bool = False, task_id=None) -> dict:
    """写入 raw 之后触发 wiki 精加工，返回结果字典。

    - `sync=False`（默认）：交给后台线程，立即返回 `pending`（结论未知）。
    - `sync=True`：等它跑完再返回，`ok` / `failed` 都是确定结论。

    为什么要有 sync：「完整入库」在本项目里的定义是 **raw + wiki**。
    后台线程模式下 wiki 失败（API key 失效、额度用尽、模型名不符……）
    只打印在服务端控制台，调用方一律回「入库成功」—— 等于谎报。

    `task_id`：异步分支需要它才能把最终结论回写任务记录（`_settle_task_async`），
    调用方据此补发「后台加工完成」的通知；同步分支结论当场就有，无需回写。
    """
    name = Path(filepath).name

    # 内容完整性闸门 —— 必须排在「内容过短就跳过」**之前**：
    # 视频没字幕时正文只有标题+简介（一两百字），会先被长度检查命中，
    # 于是降级成「疑似登录墙」并静默留下一个元数据 raw，真正的原因（没转录）就丢了。
    incomplete = _content_incomplete_reason(data)
    if incomplete:
        print(f"[i] {incomplete} —— 不生成 wiki，也不留在 raw/articles/：{name}")
        return _demote_to_inbox(
            filepath, data,
            {"status": "failed", "reason": incomplete, "tier": ""},
            incomplete, "content_incomplete")

    # 风控拦截页闸门 —— 同样排在长度检查之前。这类页面往往「够长」，
    # 只是内容其实是「检测到异常流量/请完成人机验证」那一套，不是正文。
    # 撤 raw 的动作统一由 _demote_to_inbox() 完成（与上面同一条出口）。
    interstitial = _interstitial_reason(data)
    if interstitial:
        print(f"[i] {interstitial} —— 不生成 wiki，也不留在 raw/articles/：{name}")
        return _demote_to_inbox(
            filepath, data,
            {"status": "failed", "reason": interstitial, "tier": ""},
            interstitial, "interstitial")

    # 「图多字少」闸门 —— 必须排在普通长度门禁**之前**：图片 markdown 会撑长
    # 字数，普通门禁（按含图的字数计）拦不住它。命中则交 OCR 下游处理。
    text_in_images = _text_in_images_reason(data)
    if text_in_images:
        return _handle_text_in_images(filepath, data, text_in_images,
                                      sync=sync, task_id=task_id)

    content = str(data.get("content", ""))
    title = str(data.get("title", ""))
    head = (title + "\n" + content)[:600]
    # 普通长度门禁：改按**真实文字**（剔图后）判定，避免被图片 URL 撑长而误判。
    real_len = _real_text_length(content)
    if real_len < _AUTO_REFINE_MIN_CHARS:
        reason = f"内容过短（剔图后约 {real_len} 字，疑似登录墙/验证页）"
        print(f"[i] {reason}，跳过自动精加工：{name}")
        return _record_wiki_skipped(filepath, reason)
    for keyword in _AUTO_REFINE_BLOCK_KEYWORDS:
        if keyword in head:
            reason = f"命中拦截关键词「{keyword}」"
            print(f"[i] {reason}，跳过自动精加工：{name}")
            return _record_wiki_skipped(filepath, reason)
    if sync:
        return _after_wiki(filepath, data, _run_wiki_gen_sync(filepath))
    # 异步也要收尾：失败时同样撤回 raw，否则任务记录会指向一个已被删掉的文件
    _queue_wiki_gen(filepath, data,
                    on_complete=lambda r: _after_wiki(filepath, data, r, task_id))
    return {"status": "pending", "reason": "已进入后台生成队列", "tier": ""}


# ========== Inbox（待审区）==========

import shutil


def _delete_article_with_images(filepath: Path):
    """删除文章文件及其同名图片目录。"""
    if filepath.exists():
        filepath.unlink()
    img_dir = filepath.parent / filepath.stem
    if img_dir.exists() and img_dir.is_dir():
        shutil.rmtree(img_dir)


def _inbox_list() -> list[dict]:
    """列出 inbox 中的所有条目。"""
    items = []
    if not VAULT_INBOX.exists():
        return items
    for md in sorted(VAULT_INBOX.glob("*.md"), reverse=True):
        if md.name == "README.md":
            continue
        try:
            content = md.read_text(encoding="utf-8", errors="replace")
            url_m = re.search(r'^source_url:\s*(.+)$', content, re.MULTILINE)
            platform_m = re.search(r'^platform:\s*(.+)$', content, re.MULTILINE)
            note_m = re.search(r'^note:\s*"(.*)"$', content, re.MULTILINE)
            items.append({
                "filename": md.name,
                "source_url": url_m.group(1).strip() if url_m else "",
                "platform": platform_m.group(1).strip() if platform_m else "",
                "note": note_m.group(1).strip() if note_m else "",
                "size": md.stat().st_size,
            })
        except Exception:
            continue
    return items


def _inbox_create_card(url: str, title: str = "", platform: str = "",
                        note: str = "", live_source: bool = False) -> dict:
    """在 raw/inbox/ 创建指针卡片。返回 {filename, ...}。"""
    import datetime as _dt

    # 去重：只比 frontmatter 的 source_url 行，并且按**规范化后的来源 ID** 比。
    #
    # 原来用 `url in content` 做整文子串匹配，两个方向都会错：
    #   - 误判：先存了 `.../page-2`，再存 `.../page`（它的前缀）会被当成重复而拒收
    #   - 漏判：同一视频带不同跟踪参数（`?trackid=…` vs `?spm_id_from=…`）字符串不同，
    #           不算重复，于是生成两张卡 —— B 站 / 小红书分享链接正好长这样
    # 规范化交给 normalize_url()：它会剥掉跟踪参数、把平台 ID 归一
    # （实测 `bilibili.com/video/BV1xxx/?trackid=…` 与不带参数都得到 `bilibili:BV1xxx`）。
    try:
        want = normalize_url(url)
    except Exception:
        want = url

    for md in VAULT_INBOX.glob("*.md"):
        try:
            content = md.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        m = re.search(r'^source_url:\s*(.+)$', content, re.MULTILINE)
        if not m:
            continue
        existing = m.group(1).strip()
        if existing == url:
            return {"success": False, "error": "duplicate",
                    "existing_file": md.name}
        try:
            if normalize_url(existing) == want:
                return {"success": False, "error": "duplicate",
                        "existing_file": md.name}
        except Exception:
            continue

    # 已完整入库（raw/articles/）的内容**不应再进待审区**。
    #
    # 为什么必须查这边：上面只查了 raw/inbox/ 内部，于是已经入库的链接
    # 每次点「存入待审区」都会新建一张指针卡 —— 界面同时出现
    # 「已入库 2601 字」和「待审区 +1」，自相矛盾，用户以为重复入库了。
    # 待审区的语义是「**尚未**入库、只留了链接待处理」，已入库就不该再有它。
    try:
        already = find_duplicate(want)
    except Exception:
        already = None
    if already:
        return {
            "success": False,
            "error": "already_archived",
            "existing_file": already["path"],
            "message": f"该链接已入库：{already['path']}",
        }

    today = _dt.date.today().isoformat()
    now = _dt.datetime.now()

    if not platform:
        platform = extract_source(url)

    # 文件名
    from urllib.parse import urlparse as _urlparse
    try:
        domain = _urlparse(url).netloc.replace("www.", "")
        domain = re.sub(r'[^a-zA-Z0-9.-]', '', domain)[:40]
    except Exception:
        domain = "unknown"

    count = len(list(VAULT_INBOX.glob(f"{today}-*.md")))
    seq = f"{count + 1:03d}"
    filename = f"{today}-{seq}-{domain}.md"
    filepath = VAULT_INBOX / filename

    live_source_fields = ""
    if live_source:
        live_source_fields = f"""fact_type: live-source
version_id: "v1_{today}"
last_checked: {today}
"""

    note_line = f"> 备注：{note}\n" if note else ""
    track_line = "> 📌 跟踪更新\n" if live_source else ""

    content = f"""---
date: {today}
source_url: {url}
platform: {platform}
note: "{note}"
status: inbox
{live_source_fields}---
# (待处理)

> 来源：[{domain}]({url})
> 平台：{platform}
> 添加时间：{now.strftime('%Y-%m-%d %H:%M:%S')}
{note_line}{track_line}"""

    filepath.write_text(content, encoding="utf-8")

    return {
        "success": True,
        "filename": filename,
        "filepath": str(filepath.relative_to(VAULT)),
        "platform": platform,
    }


def _inbox_clear_for_url(url: str) -> list[str]:
    """删掉某 URL 对应的待审指针卡片，返回被删的文件名列表。

    为什么要这个函数：待审区的卡片是**待办指针**（「这条链接还没完整抓下来」），
    不是内容本身。同一条链接后来通过**别的路径**入库成功了（页面内「立即完整
    入库」/ 后端适配器 / 浏览器协助），这张指针就完成了使命。留着只会：

      - 让待审区计数虚高：明明已经在 raw/articles/ 里、wiki 也生成了，
        界面却还显示 2 条待办
      - 让「全部处理」把已经入库的链接又抓一遍（命中去重、白跑一趟，
        还会把一次正常的去重渲染成一条「失败」）

    `_inbox_process_item()` 成功后本来就会删卡 —— 但那隐含一个错误前提：
    「入库只能从待审区的『▶ 处理』发起」。实际不是，所以删卡的时机必须
    跟着**入库这个事实**走，而不是跟着某一条路径走。
    """
    if not url or not VAULT_INBOX.exists():
        return []
    try:
        want = normalize_url(url)
    except Exception:
        want = url

    removed: list[str] = []
    for md in list(VAULT_INBOX.glob("*.md")):
        if md.name == "README.md":
            continue
        try:
            content = md.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        m = re.search(r'^source_url:\s*(.+)$', content, re.MULTILINE)
        if not m:
            continue
        existing = m.group(1).strip()
        hit = (existing == url)
        if not hit:
            try:
                hit = normalize_url(existing) == want
            except Exception:
                hit = False
        if not hit:
            continue
        try:
            md.unlink()
            removed.append(md.name)
        except OSError as e:
            print(f"[!] 移除待审区条目失败 {md.name}: {e}")
    return removed


def _settle_inbox_pointer(data: dict, wiki, origin_url: str = "") -> None:
    """入库收尾：raw 真的落地了，才摘掉同 URL 的待审指针。

    **只在 wiki 不是 demoted 时摘**：`demoted` 意味着素材被判定为不完整/wiki
    失败，raw 已被撤回、卡片正由 `_demote_to_inbox()` 建立或复用 —— 这时摘了
    等于把待办凭空抹掉，用户再也看不到这条链接。
    其余状态（ok / pending / skipped / failed 但未降级）raw 都留在盘上，
    指针即已完成使命。

    `origin_url`：任务**最初指派**的 URL。抓取过程中可能被重定向（典型如
    Google 验证码页 `google.com/sorry/?continue=<原URL>`），此时 raw 里记的
    `data["url"]` 已不是待审卡片里的那条链接。若只用 `data["url"]` 比对，
    卡片永远匹配不上、指针摘不掉，界面就会一直显示「等待浏览器抓取」。
    因此两个 URL 都试一遍。
    """
    if not isinstance(wiki, dict) or wiki.get("status") == "demoted":
        return
    urls = []
    for u in (str(data.get("url") or "").strip(), str(origin_url or "").strip()):
        if u and u not in urls:
            urls.append(u)
    removed: list[str] = []
    for u in urls:
        removed.extend(_inbox_clear_for_url(u))
    if removed:
        print(f"[i] 已入库，移除待审区条目：{', '.join(removed)}")


def _safe_inbox_path(filename: str) -> Path | None:
    """把外部传入的文件名安全解析为 inbox 内路径，防路径穿越。

    只允许单层裸文件名（.md），拒绝分隔符、..、绝对路径和隐藏文件。
    """
    if not filename or "\x00" in filename:
        return None
    if "/" in filename or "\\" in filename or filename in (".", "..") \
            or filename.startswith("."):
        return None
    candidate = (VAULT_INBOX / filename)
    try:
        resolved = candidate.resolve()
        resolved.relative_to(VAULT_INBOX.resolve())
    except (ValueError, OSError):
        return None
    if resolved.suffix != ".md":
        return None
    return resolved


# 扩展在页面内**自带适配器**、且服务端几乎必然搞不定的站点。
#
# 为什么待审区「▶ 处理」对它们必须改走浏览器协助（实测于 2026-10-07）：
#   - YouTube：服务端 spawn yt-dlp.EXE → ① Windows 上弹出一个控制台窗口
#     （clip-server 跑在 pythonw 下没有控制台，spawn 控制台程序必然新建一个）
#     ② 30 秒超时（命令行出不了网，浏览器有代理、命令行没有）→ 必然失败
#   - B 站：yt-dlp 读不到 Chrome cookie（Windows 的 App-Bound Encryption）
#     → 1.8 秒就退出；`_web_scrape` 又被 412 拦截 → 必然失败
#   两个平台的正文/字幕只有**在带登录态的浏览器页面里**才拿得到
#   （字幕分别走 InnerTube / WBI 接口），所以交给扩展在页面内抓。
#
# 与 `clip-extension/platforms/known-sites.js` 的 LOCAL_ADAPTER_DOMAINS 对应，
# 但**故意只列这两个视频站**：飞书（lark-cli）/ 微信公众号 / 小红书 的后端
# 适配器目前是可用的，不该为了统一而把能用的路也换掉。
_BROWSER_PREFERRED_DOMAINS = (
    "youtube.com", "youtu.be",
    "bilibili.com", "b23.tv",
)


def _prefer_browser(url: str) -> bool:
    """按**主机名**判断（不是子串）：`https://x.com/?q=youtube.com` 这种不该命中。"""
    try:
        host = urlparse(url).netloc.lower().split("@")[-1].split(":")[0]
    except Exception:
        return False
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in _BROWSER_PREFERRED_DOMAINS)


def _inbox_set_note(filename: str, note: str) -> None:
    """把「本次处理失败的具体原因」写回待审卡片的 note 字段。

    为什么必须写回：此前失败原因只出现在**一次 HTTP 响应**里，卡片本身
    仍是入库时的旧 note。用户关掉弹窗、或下次打开待审区，原因就消失了，
    看到的只是一条没有说明的失败记录 —— 这正是「待审区失败却看不到原因」。
    写回 frontmatter 后，`_inbox_list()` 会把 note 一起返回，界面即可常驻展示。
    """
    filepath = _safe_inbox_path(filename)
    if filepath is None or not filepath.exists():
        return
    try:
        content = filepath.read_text(encoding="utf-8", errors="replace")
        line = 'note: "' + str(note).replace('"', "'").replace("\n", " ")[:300] + '"'
        if re.search(r'^note:.*$', content, re.MULTILINE):
            content = re.sub(r'^note:.*$', line, content, count=1, flags=re.MULTILINE)
        else:
            # 没有 note 行就在 status 行前插入（frontmatter 内）
            content = re.sub(r'^(status:.*)$', line + "\n" + r'\1',
                             content, count=1, flags=re.MULTILINE)
        filepath.write_text(content, encoding="utf-8")
    except Exception as e:
        print(f"[!] 写回待审区失败原因出错 {filename}: {e}", file=sys.stderr)


def _inbox_process_item(filename: str) -> dict:
    """处理 inbox 中的单个条目：完整抓取，成功后删除指针卡片。"""
    filepath = _safe_inbox_path(filename)
    if filepath is None:
        return {"success": False, "error": "非法文件名"}
    if not filepath.exists():
        return {"success": False, "error": f"文件不存在：{filename}"}

    content = filepath.read_text(encoding="utf-8", errors="replace")
    url_m = re.search(r'^source_url:\s*(.+)$', content, re.MULTILINE)
    if not url_m:
        return {"success": False, "error": "指针卡片缺少 source_url"}
    url = url_m.group(1).strip()

    # 去重检查：如果 articles 里已有同 URL 的文件，跳过
    canonical = resolve_short_link(url)
    normalized = normalize_url(canonical)
    existing = find_duplicate(normalized)
    if existing:
        existing_path = VAULT / existing["path"]
        if existing_path.exists():
            # 删除指针卡片（已有人工入库的版本）
            filepath.unlink()
            return {
                "success": False,
                "error": "duplicate",
                "message": f"已存在：{existing['path']}",
                "existing_file": existing["path"],
            }

    # ---- 视频平台：交给浏览器协助队列，**不在后端跑 yt-dlp** ----
    # （理由见 _BROWSER_PREFERRED_DOMAINS 上面那段）
    if _prefer_browser(url):
        active = task_queue.find_active_task(url)
        if active:
            _inbox_set_note(filename, f"已排入浏览器协助队列（任务 {active}），等待浏览器抓取")
            return {
                "success": True, "pending": True, "taskId": active,
                "inbox_kept": True,
                "message": "该链接已在浏览器协助队列中，等待浏览器抓取",
            }
        task_id = task_queue.add_task(url, via="浏览器协助")
        print(f"[=] 待审区条目改由浏览器协助处理 [{task_id}]: {url[:80]}")
        _inbox_set_note(filename, f"已排入浏览器协助队列（任务 {task_id}），等待浏览器抓取")
        return {
            "success": True, "pending": True, "taskId": task_id,
            "inbox_kept": True,
            "message": "已排入浏览器协助队列，由浏览器扩展在页面内抓取",
        }

    # 调用完整抓取流程。sync_wiki=True：等 wiki 出结论，再决定卡片去留
    try:
        result = clip_from_url(url, via="inbox-process", sync_wiki=True)
    except Exception as e:
        # 以前这里直接 str(e) 就返回了，服务端日志里连一行都不留 ——
        # 用户看到一句技术报错，而排查的人不知道刚才发生了什么。
        print(f"[!] 待审区处理失败 {filename}: {e}", file=sys.stderr)
        _inbox_set_note(filename, f"处理失败：{e}")
        return {"success": False, "error": str(e), "reason": str(e)}

    if (result.get("wiki") or {}).get("status") == "demoted":
        # wiki 没生成 → raw 已被撤回。此时服务端建的卡片**就是正在处理的这张**
        # （新建时按 URL 命中去重，返回的是 existing_file），所以**不能删**：
        # 条目留在待审区，等通道恢复后重试。
        wk = result.get("wiki") or {}
        reason = wk.get("reason") or "服务端未说明原因"
        # 把原因写回卡片 —— 否则用户关掉弹窗后，界面只剩一条没有说明的失败记录。
        _inbox_set_note(filename, f"处理失败（{(wk.get('kind') or 'wiki_failed')}）：{reason}")
        return {
            "success": False,
            "error": "wiki_failed",
            # kind 决定前端措辞：素材本身不完整 vs 通道失败，处置完全不同
            "kind": wk.get("kind") or "wiki_failed",
            "reason": reason,
            "inbox_kept": True,
        }

    # 成功后删除指针卡片。
    # missing_ok=True 是必须的：clip_from_url() 收尾时已经会调
    # _settle_inbox_pointer() 把同 URL 的卡片摘掉（它不认得「正在处理的是哪一张」，
    # 只按 URL 匹配），所以走到这里时本文件通常**已经不在了**。
    # 不加 missing_ok 的话，一次成功的入库会在这里抛 FileNotFoundError，
    # 被外层 except 兜成 500 —— 界面显示失败，其实内容好好的。
    filepath.unlink(missing_ok=True)
    result["inbox_processed"] = True
    return result


# ========== 请求体限制 ==========

MAX_BODY_SIZE = 10 * 1024 * 1024  # 10MB
MAX_URL_LENGTH = 2000
MAX_TITLE_LENGTH = 500
MAX_CONTENT_LENGTH = 5 * 1024 * 1024  # 5MB
MAX_IMAGES = 30


def _validate_request(body_bytes: bytes, data: dict) -> str | None:
    """校验请求体大小和字段长度。

    返回错误消息，None 表示通过。
    """
    # 检查请求体大小
    if len(body_bytes) > MAX_BODY_SIZE:
        return f"请求体过大（{len(body_bytes)} 字节，最大 {MAX_BODY_SIZE}）"

    # 检查 URL 长度
    url = data.get("url", "")
    if len(url) > MAX_URL_LENGTH:
        return f"URL 过长（{len(url)} 字符，最大 {MAX_URL_LENGTH}）"

    # 检查标题长度
    title = data.get("title", "")
    if len(title) > MAX_TITLE_LENGTH:
        return f"标题过长（{len(title)} 字符，最大 {MAX_TITLE_LENGTH}）"

    # 检查内容长度
    content = data.get("content", "")
    if len(content) > MAX_CONTENT_LENGTH:
        return f"内容过长（{len(content)} 字符，最大 {MAX_CONTENT_LENGTH}）"

    # 检查图片数量（如果有）
    images = data.get("images", [])
    if len(images) > MAX_IMAGES:
        return f"图片过多（{len(images)} 张，最大 {MAX_IMAGES}）"

    return None


def _clip_via(data: dict) -> str:
    """从请求体推断这条剪藏是**从哪个渠道**发起的，用于任务面板分类展示。

    为什么不直接用 data["via"]：`handle_clip` 无条件把它覆写成「浏览器插件」，
    于是悬浮按钮、右键菜单、插件面板三条路径在任务列表里长得一模一样，
    用户无法分辨「同样一条内容，为什么两个入口的结果不同」。
    """
    raw = str(data.get("via") or "").strip()
    if raw and raw != "浏览器插件":
        return raw
    channel = str(data.get("channel") or "").strip().lower()
    if channel in ("feishu", "飞书"):
        return "飞书"
    if channel in ("popup-overwrite",):
        return "插件面板（覆盖）"
    if channel in ("popup", "panel", "插件"):
        return "插件面板"
    if channel in ("selection", "选中") or data.get("clipType") == "selection":
        return "选中剪藏"
    if channel in ("fab", "floating", "悬浮"):
        return "悬浮按钮"
    # 无 channel 时回退看内容类型：选中剪藏一定带 type=selection
    if str(data.get("type") or "").lower() == "selection" \
            or data.get("selectedText"):
        return "选中剪藏"
    # 无法区分时退回旧标签，保持向后兼容
    return raw or "浏览器插件"


# ========== 配置加载 ==========

def _load_config() -> dict:
    """加载配置文件。"""
    config_path = Path(__file__).resolve().parent / "config.local.json"
    if config_path.exists():
        try:
            return json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[!] 配置文件加载失败: {e}", file=sys.stderr)
    return {}


CONFIG = _load_config()
# 环境变量 CLIP_SERVER_TOKEN 优先于配置文件，避免密钥落盘（容器/CI 场景）
SERVER_TOKEN = os.environ.get("CLIP_SERVER_TOKEN", "") or CONFIG.get("token", "")
# 用户自定义 CORS 白名单（扩展 origin 与本机页面默认已放行，见 _cors_allowed_origin）
ALLOWED_ORIGINS = set(CONFIG.get("allowed_origins", []) or [])

# SSE 的 EventSource 无法自定义 Authorization 头，仅此端点允许 ?token= 传参
_QUERY_TOKEN_PATH = "/api/browser/connect"


def _check_auth(headers, path: str = "") -> bool:
    """Bearer Token 鉴权，常量时间比较防止时序攻击。

    返回 True 表示通过，False 表示未授权。
    未配置 token 时跳过鉴权（仅回环监听的开发模式）。
    SSE 端点额外支持 query 参数传 token（EventSource 无法带请求头）。
    """
    if not SERVER_TOKEN:
        return True  # 未配置 token 时跳过
    provided = ""
    auth = headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        provided = auth[7:]
    elif path.startswith(_QUERY_TOKEN_PATH + "?"):
        query = parse_qs(urlparse(path).query)
        provided = query.get("token", [""])[0]
    return bool(provided) and hmac.compare_digest(provided, SERVER_TOKEN)


def _cors_allowed_origin(origin: str) -> str:
    """校验 Origin 并返回可回显值，非白名单返回空串。

    默认放行：浏览器扩展 origin、本机 http(s) 页面；普通公网网页一律拒绝，
    防止用户浏览网页时被跨域 POST 写入本地知识库。
    """
    if not origin:
        return ""
    if origin in ALLOWED_ORIGINS:
        return origin
    parsed = urlparse(origin)
    host = (parsed.hostname or "").lower()
    if parsed.scheme in ("chrome-extension", "moz-extension", "ms-browser-extension"):
        return origin
    if parsed.scheme in ("http", "https") and host in {"localhost", "127.0.0.1", "::1"}:
        return origin
    return ""


def slugify(text: str, max_len: int = 60) -> str:
    # 移除零宽字符（飞书等平台会在标题中嵌入零宽字符）
    text = re.sub(r'[​-‏‪-‮⁠-⁯﻿­᠎឴឵︀-️￹-￻]', '', text)
    text = text.strip()
    text = re.sub(r'[\\/:*?"<>|\n\r\t]', "", text)
    text = re.sub(r"\s+", "-", text)
    if len(text) > max_len:
        text = text[:max_len].rstrip("-")
    return text or "untitled"


def extract_source(url: str) -> str:
    try:
        domain = urlparse(url).netloc
        domain = domain.replace("www.", "")
        return domain
    except Exception:
        return "web"


# ========== URL 去重 ==========

# UTM 和跟踪参数（规范化时去除）
_TRACKING_PARAMS = {
    # 通用 UTM
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_source_platform",
    # 广告跟踪
    "fbclid", "gclid", "dclid", "msclkid", "ttclid",
    # 通用分享/来源
    "ref", "referer", "source",
    "share_id", "sp", "shared_from",
    # B站
    "spm_id_from", "vd_source", "share_source", "is_story_id",
    "from_spmid", "spm_id", "tsid", "ts", "buvid", "bvid",
    # 微信
    "scene", "subscene", "sessionid", "nwr1",
    # 小红书
    "xsec_source", "xsec_token",
    # YouTube
    "feature", "si", "pp",
    # 其他
    "campaign_id", "ad_id", "igshid",
}

# 短链域名列表（需要 302 解析的）
_SHORT_LINK_DOMAINS = {
    "b23.tv",                     # B站
    "xhslink.com", "xhslink.cn", "xhs.cn",  # 小红书
    "youtu.be",                   # YouTube
    "douyin.com",                 # 抖音（部分短链格式）
    "v.douyin.com",
}


# 短链解析结果缓存：同一 URL 只解析一次。
# 为什么必须缓存：本函数在 find_duplicate() 的**文件循环**里被逐文件调用
# （去重的主路径），同一批短链在每次剪藏时都会被重复解析。
_RESOLVE_CACHE: dict[str, str] = {}
_RESOLVE_CACHE_LOCK = threading.Lock()
_RESOLVE_CACHE_MAX = 500


def resolve_short_link(url: str, timeout: float = 8) -> str:
    """解析短链 → 最终 URL（跟随 302/301 重定向）。

    只对已知短链域名发起请求，避免对所有 URL 都做一次额外请求。
    部分平台（如小红书 xhslink.cn）HEAD 返回 404 但 GET 正常重定向，
    因此使用流式 GET 并在拿到重定向后立即关闭连接。

    **必须限时（硬性墙钟上限）**：本函数在 `find_duplicate()` 的逐文件循环里
    被调用 —— 也就是说它位于**入库主路径**上。实测（2026-10-14）一次请求卡在
    TLS 握手里超过 10 分钟：对端在握手期半关连接（socket 进入 CLOSE_WAIT），
    而 requests 的 `timeout` **对 TLS 握手不生效**。结果是整个剪藏请求连同飞书
    回调一起被吊死，用户侧表现为「机器人毫无反应」。
    因此这里把网络动作放到**守护线程**里跑，主线程 join(timeout+2) 硬性兜底：
    超时就放弃解析、按原地址返回。被放弃的线程是 daemon，不阻塞进程退出。
    """
    try:
        domain = urlparse(url).netloc.replace("www.", "")
    except Exception:
        return url

    # 只对短链域名解析
    is_short = any(domain == d or domain.endswith("." + d) for d in _SHORT_LINK_DOMAINS)
    if not is_short:
        return url

    with _RESOLVE_CACHE_LOCK:
        cached = _RESOLVE_CACHE.get(url)
    if cached is not None:
        return cached

    resolved, timed_out = _resolve_short_link_bounded(url, timeout)
    if not timed_out:      # 超时只是「这次没解析出来」，别把兜底结果写进缓存
        with _RESOLVE_CACHE_LOCK:
            if len(_RESOLVE_CACHE) >= _RESOLVE_CACHE_MAX:
                _RESOLVE_CACHE.clear()
            _RESOLVE_CACHE[url] = resolved
    return resolved


def _resolve_short_link_bounded(url: str, timeout: float) -> tuple[str, bool]:
    """把短链解析放进守护线程，主线程最多等 timeout+2 秒。

    返回 (结果 URL, 是否超时)。超时不代表失败 —— 只是「这次没解析出来」，
    返回原地址即可（调用方的 normalize_url 仍会给出一个稳定的通用 source_id，
    不会中断入库）。
    """
    box = {"url": url, "timed_out": False}

    def _run():
        try:
            resp = requests.get(url, allow_redirects=True, timeout=timeout,
                                headers={"User-Agent": "Mozilla/5.0"}, stream=True)
            try:
                if resp.url and resp.url != url:
                    box["url"] = resp.url
            finally:
                resp.close()
        except Exception:
            pass

    t = threading.Thread(target=_run, daemon=True, name="resolve-short-link")
    t.start()
    t.join(timeout + 2)
    if t.is_alive():
        box["timed_out"] = True
        print(f"[i] 短链解析超时（>{timeout + 2:.0f}s），按原地址处理：{url[:70]}")
    return box["url"], box["timed_out"]


def _xiaohongshu_id_from_login_wall(query: str, depth: int = 0) -> str | None:
    """从登录墙 URL 的 redirectPath 参数里取出 noteId（兼容多层编码）。

    小红书对未登录请求会 302 到
        https://www.xiaohongshu.com/login?redirectPath=<urlencoded 原地址>
    原始地址可能是 `/explore/<id>` 或 `/discovery/item/<id>`，也可能再包一层。
    """
    if depth > 3:
        return None
    params = parse_qs(query)
    for key in ("redirectPath", "redirect_uri", "redirectUrl", "target"):
        raw = params.get(key, [None])[0]
        if not raw:
            continue
        inner = unquote(raw)
        m = re.search(r"/(?:explore|discovery/item)/([\w]+)", inner)
        if m:
            return m.group(1)
        if "?" in inner:
            got = _xiaohongshu_id_from_login_wall(inner.split("?", 1)[1], depth + 1)
            if got:
                return got
    return None


def normalize_url(url: str) -> str:
    """规范化 URL 为稳定来源 ID。

    策略：
    1. 对已知平台，提取平台稳定 ID（BV号、noteId、视频ID等）作为 source_id
    2. 对未知平台，去除跟踪参数、统一格式作为规范化 URL

    这样无论用户从手机分享（短链带跟踪参数）还是电脑复制（完整 URL），
    同一内容都会得到相同的 source_id。
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return url

    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = parsed.path

    # ===== 平台稳定 ID 提取 =====

    # B站：提取 BV号
    if "bilibili.com" in netloc:
        m = re.search(r"/video/(BV[\w]+)", path)
        if m:
            return f"bilibili:{m.group(1)}"

    # 小红书：提取 noteId（在 /explore/ 或 /discovery/item/ 路径中）
    if "xiaohongshu.com" in netloc:
        m = re.search(r"/(?:explore|discovery/item)/([\w]+)", path)
        if m:
            return f"xiaohongshu:{m.group(1)}"
        # 登录墙包装：短链（xhslink.cn / xhslink.com）解析后若命中未登录风控，
        # 会 302 到 `xiaohongshu.com/login?redirectPath=<编码后的原始地址>`。
        # 真正的 noteId 藏在 redirectPath 里 —— 不剥开就会退化成「整条 URL 当
        # source_id」，与长链接得到的 `xiaohongshu:<id>` 对不上，于是同一篇内容
        # 被当成两篇分别入库（用户 2026-10-10 报：长链接 + 短链接各存了一份）。
        note_id = _xiaohongshu_id_from_login_wall(parsed.query)
        if note_id:
            return f"xiaohongshu:{note_id}"

    # YouTube：提取视频 ID
    if "youtube.com" in netloc:
        params = parse_qs(parsed.query)
        vid = params.get("v", [None])[0]
        if vid:
            return f"youtube:{vid}"
    if netloc == "youtu.be":
        m = re.search(r"/([\w-]+)", path)
        if m:
            return f"youtube:{m.group(1)}"

    # 微信：去掉所有参数，只保留路径（/s/xxxx 或完整路径）
    if "mp.weixin.qq.com" in netloc:
        # 微信文章 URL 的 /s/ 短链格式
        if path.startswith("/s/"):
            return f"wechat:{path}"
        # 完整格式：保留 __biz + mid + sn
        params = parse_qs(parsed.query)
        biz = params.get("__biz", [None])[0]
        mid = params.get("mid", [None])[0]
        sn = params.get("sn", [None])[0]
        if biz and mid and sn:
            return f"wechat:{biz}:{mid}:{sn}"
        # 回退：只保留路径
        return f"wechat:{path}"

    # 抖音：提取视频 ID
    if "douyin.com" in netloc:
        m = re.search(r"/video/(\d+)", path)
        if m:
            return f"douyin:{m.group(1)}"
        m = re.search(r"/note/(\d+)", path)
        if m:
            return f"douyin:note:{m.group(1)}"

    # X/Twitter：提取 Status ID
    if netloc in ("twitter.com", "x.com"):
        m = re.search(r"/\w+/status/(\d+)", path)
        if m:
            return f"twitter:{m.group(1)}"

    # 飞书：提取文档 token（/docx/xxx 或 /wiki/xxx）
    if "feishu.cn" in netloc or "larksuite.com" in netloc:
        m = re.search(r"/(docx|wiki)/([\w]+)", path)
        if m:
            return f"feishu:{m.group(1)}:{m.group(2)}"

    # ===== 通用规范化（未知平台）=====
    params = parse_qs(parsed.query, keep_blank_values=False)
    clean_params = {k: v for k, v in params.items() if k not in _TRACKING_PARAMS}
    clean_query = urlencode(sorted(clean_params.items()), doseq=True)
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    return urlunparse((
        parsed.scheme.lower(),
        netloc,
        path,
        parsed.params,
        clean_query,
        "",
    ))


def _extract_word_count(content: str) -> int:
    """已入库 raw 的**真实正文字数** —— 按统一口径重算，不信任 frontmatter。

    为什么不再读 frontmatter 的 `word_count`：旧口径把图片 URL 里的英文片段
    也算进来，历史文件该字段普遍虚高（实测一条 19 字图文笔记记着 232）。去重时
    拿虚高旧值与新值比较，会得出「本次更少 → 上一版更完整」的**相反结论**。
    统一重算才能让新旧两版在同一把尺子上比。
    """
    return _count_words(content)


def _extract_wiki_refs(content: str) -> list[str]:
    """从 raw 的 frontmatter 提取 wiki_refs 列表（支持单行内联和多行块列表）。

    只解析 frontmatter（开头 --- 之间），避免误匹配正文里的示例片段。
    """
    m = re.search(r'^---\s*\n(.*?)\n---', content, re.S | re.M)
    if not m:
        return []
    fm = m.group(1)
    # 只匹配冒号后同一行的内容（[ \t]* 不跨行），块列表则行尾为空
    wm = re.search(r'^wiki_refs:[ \t]*([^\n]*)$', fm, re.M)
    if not wm:
        return []
    inline = wm.group(1).strip()
    refs: list[str] = []
    if inline.startswith('['):
        # 单行内联数组：["a", "b"] 或 []
        inner = inline.strip().strip('[]')
        refs = [r.strip().strip('"\'').strip() for r in inner.split(',') if r.strip()]
    elif inline == '':
        # 多行块列表：
        #   wiki_refs:
        #     - path1
        #     - path2
        for line in fm[wm.end():].splitlines():
            line = line.strip()
            if line.startswith('- '):
                refs.append(line[2:].strip().strip('"\'').strip())
            elif line and not line.startswith('-'):
                break
    elif inline:
        # 单值不带括号（兼容）
        refs = [inline.strip('"\'').strip()]
    return [r for r in refs if r]


def _wiki_status(content: str) -> dict:
    """从 raw 内容判断是否已生成 wiki。

    返回 {"has_wiki": bool, "existing_wiki": str}。
    """
    refs = _extract_wiki_refs(content)
    if refs:
        return {"has_wiki": True, "existing_wiki": refs[0]}
    return {"has_wiki": False, "existing_wiki": ""}


# ===== 版本管理辅助 =====

# 默认视为 live-source 的平台（协作文档，内容可能随时变化）
_LIVE_SOURCE_PLATFORMS = {
    "feishu.cn", "larksuite.com",   # 飞书
    "notion.so", "notion.site",     # Notion
    "docs.google.com",              # Google Docs
    "confluence.", "atlassian.",    # Confluence
}


def _is_live_source_platform(url: str) -> bool:
    """判断 URL 所属平台是否默认视为 live-source。

    协作文档类（飞书、Notion、Google Docs、Confluence）默认 live-source，
    因为内容可能随时被作者更新。
    新闻/视频/社交帖子等默认不是。
    """
    url_lower = url.lower()
    return any(domain in url_lower for domain in _LIVE_SOURCE_PLATFORMS)


def _extract_fact_type(content: str) -> str:
    """从 frontmatter 提取 fact_type 字段。"""
    m = re.search(r'^fact_type:\s*(.+?)$', content, re.MULTILINE)
    return m.group(1).strip() if m else ""


def _extract_version_number(filepath: Path) -> int:
    """从文件名提取版本号。如 `文章-v2.md` → 2，无后缀 → 1。"""
    stem = filepath.stem
    m = re.search(r'-v(\d+)$', stem)
    return int(m.group(1)) if m else 1


def find_duplicate(normalized_url: str, total_budget: float = 20.0) -> dict | None:
    """在 raw/articles/ 中查找是否已有相同规范化 URL 的文件。

    返回 dict：
      - path: 文件相对路径
      - is_live_source: 是否 fact_type: live-source
      - version_number: 当前版本号（无后缀为 1，-v2 为 2...）
    没找到返回 None。

    策略：
    1. 先匹配 source_id 字段（快速精确匹配，无需网络）
    2. 再匹配 source_url 字段的规范化结果（需要解析短链，较慢）

    `total_budget`：慢速路径里**联网解析短链**的总时间预算（秒）。本函数位于
    入库主路径上，最坏情况会逐文件联网 —— 藏了很多小红书/B 站短链时，一次去重
    可能等上几分钟。超过预算就不再联网，退回用原地址比对（可能漏判重复，但绝不
    把剪藏请求拖死），并打印一条提示。
    """
    search_dirs = [
        VAULT / "raw" / "articles",
    ]
    deadline = time.monotonic() + total_budget
    budget_warned = False

    for search_dir in search_dirs:
        if not search_dir.exists():
            continue
        for md_file in search_dir.glob("*.md"):
            try:
                content = md_file.read_text(encoding="utf-8", errors="replace")
                matched = False

                # 快速路径：匹配 source_id（无需网络）
                sid_m = re.search(r'^source_id:\s*(.+?)$', content, re.MULTILINE)
                if sid_m:
                    existing_sid = sid_m.group(1).strip().strip('"').strip("'")
                    if existing_sid == normalized_url:
                        matched = True

                # 慢速路径：匹配 source_url 的规范化结果
                if not matched:
                    if "source_url:" not in content:
                        continue
                    m = re.search(r'^source_url:\s*(.+?)$', content, re.MULTILINE)
                    if not m:
                        continue
                    existing_url = m.group(1).strip().strip('"').strip("'")
                    if time.monotonic() < deadline:
                        canonical = resolve_short_link(existing_url)
                    else:
                        if not budget_warned:
                            print(f"[i] 去重联网解析超出 {total_budget:.0f}s 预算，"
                                  "后续改为直接比对（可能漏判重复）")
                            budget_warned = True
                        canonical = existing_url
                    if normalize_url(canonical) == normalized_url:
                        matched = True

                if matched:
                    return {
                        "path": str(md_file.relative_to(VAULT)),
                        "is_live_source": _extract_fact_type(content) == "live-source",
                        "version_number": _extract_version_number(md_file),
                    }
            except Exception:
                continue
    return None


def _preflight_duplicate(normalized_url: str, new_content: str,
                         force: bool = False) -> dict | None:
    """统一的「写盘前」去重闸门（前置检查，不依赖任何落盘副作用）。

    这是所有入库路径（浏览器插件 / 浏览器协助 / 后端直抓 / inbox）唯一的
    去重判定入口。命中重复时返回应答字典，调用方直接 send_json 返回即可，
    绝不允许先写盘再判断。

    返回 dict：
      - 命中重复：{"_status": 409, "success": False, "error": "duplicate", ...}
      - 命中可覆盖（**仅** force，即用户已确认）：{"_status": 200, "_overwrite": True, ...}
      - 命中 live-source 新版本：{"_status": 200, "_version": N, "existing": {...}}
      - 无重复：None

    409 里带 `newer_more_complete`：本次抓到的正文明显多于已入库版本，但**覆盖仍
    需用户确认**（2026-10-14 起不再自动覆盖，理由见下方注释）。前端据此在两版
    字数并列的基础上给出「建议覆盖」的结论。
    """
    if not normalized_url:
        return None

    existing = find_duplicate(normalized_url)
    if not existing:
        return None

    existing_path = VAULT / existing["path"]
    if not existing_path.exists():
        return None

    old_content = existing_path.read_text(encoding="utf-8", errors="replace")
    old_wc = _extract_word_count(old_content)
    wiki = _wiki_status(old_content)
    new_wc = _count_words(new_content)
    is_live = existing["is_live_source"]

    # 强制覆盖：**必须走覆盖**，不能返回 None。
    #
    # 踩过（用户 2026-10-09 报「点强制覆盖却多出一个 -1.md 文件」）：
    # 旧实现 `if force: return None` 把 force 当成「跳过去重」，于是调用方
    # 直接走正常写盘，而 generate_card 为避免同名冲突会另存为 `-1.md`、`-2.md`……
    # 结果「覆盖」变成了不断堆积新文件，旧文件一个都没被替换。
    # 正确语义：force = 「我确定要替换掉已存在的那一份」，所以要返回可覆盖指令。
    if force:
        return {
            "_status": 200,
            "_overwrite": True,
            "existing": existing,
            "existing_word_count": old_wc,
            "new_word_count": new_wc,
        }

    # live-source 内容有实质增长 → 创建**新版本链**（新增 -vN 文件，旧版保留）。
    #
    # 这条是唯一允许「不经确认就动盘」的分支，因为它**非破坏**：旧版完整留着，
    # 用户区也随 `_old_user_region` 一起迁移。与「覆盖」是两回事。
    if is_live and new_wc > old_wc * 1.2:
        return {
            "_status": 200,
            "_version": existing["version_number"] + 1,
            "existing": existing,
            "existing_word_count": old_wc,
            "new_word_count": new_wc,
        }

    # 其余情况（含「本次明显更完整」）一律 409，**覆盖必须由用户确认**。
    #
    # 为什么不再自动覆盖（2026-10-14 起）：旧行为是 `new_wc > old_wc*1.3` 就
    # 直接 `_delete_article_with_images()` 删旧写新。两个问题：
    #   ① 用户**看不到任何提示**，一次手机飞书剪藏就能悄悄换掉库里那份；
    #   ② 旧文件里的 `## 我的笔记`（用户手写内容）随删旧一起没了。
    # 现在改为把决定权交回用户：面板 / 飞书卡片会把两版字数**并列**并给出建议，
    # 点「强制覆盖」才覆盖（走 force，保留旧文件名与原 URL 一致）。
    if is_live:
        message = f"已是最新版本：{existing['path']}（{old_wc} 字）"
        newer = False
    elif new_wc > old_wc * 1.3:
        message = (f"已存在相同来源：{existing['path']}（{old_wc} 字）；"
                   f"本次抓到 {new_wc} 字，可能更完整。如需替换请点「强制覆盖」。")
        newer = True
    else:
        message = f"已存在相同来源：{existing['path']}（{old_wc} 字）"
        newer = False
    return {
        "_status": 409,
        "success": False,
        "error": "duplicate",
        "message": message,
        "existing_file": existing["path"],
        "existing_word_count": old_wc,
        "new_word_count": new_wc,
        "canOverwrite": True,
        # 供前端/飞书把「可覆盖」与「建议覆盖」区分开：前者是能力，后者是判断。
        "newer_more_complete": newer,
        "has_wiki": wiki["has_wiki"],
        "existing_wiki": wiki["existing_wiki"],
    }


# ========== 自动区/人工区处理 ==========

def _extract_user_region(content: str) -> str:
    """从现有内容中提取人工区内容（包括标记）。

    返回完整的人工区文本（含标记），如果没找到返回空字符串。
    """
    pattern = r'<!-- clip:user:start -->.*?<!-- clip:user:end -->'
    match = re.search(pattern, content, re.DOTALL)
    if match:
        return match.group(0)
    return ""


def _extract_auto_region(content: str) -> str:
    """从现有内容中提取自动区内容（不包括标记）。

    返回自动区文本（不含标记），用于比较用户是否修改。
    """
    pattern = r'<!-- clip:auto:start -->(.*?)<!-- clip:auto:end -->'
    match = re.search(pattern, content, re.DOTALL)
    if match:
        return match.group(1).strip()
    return ""


def _extract_frontmatter(content: str) -> str:
    """从 Markdown 内容中提取 frontmatter（包括 --- 标记）。"""
    match = re.match(r'^---\n.*?\n---\n', content, re.DOTALL)
    if match:
        return match.group(0)
    return ""


def _merge_content(frontmatter: str, new_auto: str, existing_user: str) -> str:
    """合并 frontmatter + 新自动区 + 现有人工区。

    如果现有人工区为空，使用默认的人工区模板。
    """
    if not existing_user:
        existing_user = """<!-- clip:user:start -->
## 我的笔记

<!-- clip:user:end -->"""

    # 从新自动区内容中提取（不含标记）
    auto_match = re.search(r'<!-- clip:auto:start -->(.*?)<!-- clip:auto:end -->',
                           new_auto, re.DOTALL)
    if auto_match:
        auto_content = auto_match.group(1).strip()
    else:
        # 如果没有标记，整个内容都是自动区
        auto_content = new_auto.strip()

    return f"""{frontmatter}
<!-- clip:auto:start -->
{auto_content}
<!-- clip:auto:end -->

{existing_user}
"""


def generate_card(data: dict):
    """生成 inbox 卡片文件，返回 (文件名, 内容, 字数)。

    输出结构包含自动区和人工区标记：
    - <!-- clip:auto:start --> ... <!-- clip:auto:end --> 系统生成内容
    - <!-- clip:user:start --> ... <!-- clip:user:end --> 用户笔记区
    """
    title = data.get("title", "无标题").strip()
    url = data.get("url", "")
    author = data.get("author", "")
    published_at = data.get("publishedAt", "") or data.get("published_date", "")
    content = data.get("content", "")
    source_id = data.get("source_id", "")  # 稳定来源 ID（去重用）
    live_source = data.get("liveSource")  # 显式指定
    if live_source is None:
        # 未指定时按平台自动判断
        live_source = _is_live_source_platform(url)
    version_number = data.get("versionNumber", 1)  # 版本号（-v2 为 2）
    previous_version = data.get("previousVersion", "")  # 上一版标识

    today = date.today().isoformat()
    now = datetime.now()
    # 清理标题里的 HTML 标签
    clean_title = re.sub(r"<[^>]+>", "", title).strip()
    slug = slugify(clean_title or title)

    # 文件名（含版本号后缀）
    version_suffix = f"-v{version_number}" if version_number > 1 else ""
    filename = f"{today}-{slug}{version_suffix}.md"
    counter = 1
    while (ARTICLES_DIR / filename).exists():
        filename = f"{today}-{slug}{version_suffix}-{counter}.md"
        counter += 1

    # 字数按**真实正文**计（剥掉图片 URL / 元数据 / 骨架 / 话题标签等脚手架）。
    #
    # 旧口径把远程图片 URL 里的英文片段也算进来：一条正文只有 19 字的图文笔记
    # 会显示「约 232 字」，用户据此以为内容完整（2026-10-14 实测）。字数是对外
    # 承诺「剪到了多少」，被 URL 撑高等于谎报。
    word_count = _count_words(content)

    # 提取基本信息
    source = extract_source(url)
    content_type = data.get("type", "article")
    platform = data.get("platform", source)
    duration = data.get("duration", "")
    is_selection = data.get("selectedText", False)

    # 处理标签
    tags = data.get("tags") or ["待处理", source]
    if is_selection and "选中文本" not in tags:
        tags.append("选中文本")
    tags_str = ", ".join([f'"{t}"' for t in tags])

    source_id_line = f"source_id: \"{source_id}\"\n" if source_id else ""

    # live-source 扩展字段（AGENTS.md §4.5.1）
    live_source_fields = ""
    if live_source:
        version_id = f"v{version_number}_{today}"
        prev_version_line = f"\nprevious_version: \"{previous_version}\"" if previous_version else ""
        live_source_fields = f"""fact_type: live-source
version_id: "{version_id}"
last_checked: {today}{prev_version_line}
"""

    frontmatter = f"""---
date: {today}
source_url: {url}
{source_id_line}published_at: {published_at or today}
type: {content_type}
tags: [{tags_str}]
wiki_refs: []
status: triage
author: {author}
word_count: {word_count}
duration: {duration}
{live_source_fields}---
"""

    # 自动区：系统生成的内容
    if is_selection:
        # 选中文本剪藏
        body = f"""# {title}（选中文本）

<!-- clip:auto:start -->
> 来源：[{url}]({url})
> 抓取自：{data.get('via', 'API')}（{now.strftime('%Y-%m-%d %H:%M:%S')}）
> 类型：选中文本
> 字数：约 {word_count} 字

---

## 选中内容

{content.strip()}

> 注：本页为用户选中的文本片段，完整内容以 source_url 为准。
<!-- clip:auto:end -->

<!-- clip:user:start -->
## 我的笔记

<!-- clip:user:end -->
"""
    else:
        # 整页剪藏
        body = f"""# {title}

<!-- clip:auto:start -->
> 来源：[{url}]({url})
> 作者：{author or "未知"}
> 发布时间：{published_at or "未知"}
> 抓取自：{data.get('via', 'API')}（{now.strftime('%Y-%m-%d %H:%M:%S')}）
> 字数：约 {word_count} 字

---

## 原文快照

{content.strip()}

> 注：本页为剪藏快照，完整内容以 source_url 为准。
<!-- clip:auto:end -->

<!-- clip:user:start -->
## 我的笔记

<!-- clip:user:end -->
"""

    return filename, frontmatter + body, word_count


# ========== SSRF 防护：出站图片请求安全校验 ==========

MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 单张图片 10MB 上限
MAX_IMAGE_REDIRECTS = 3


class _UnsafeURLError(Exception):
    """URL 指向非公开地址或违反安全策略。"""


def _is_blocked_ip(ip_str: str) -> bool:
    """判断 IP 是否属于环回/私有/链路本地/保留等非公网地址。"""
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    return (
        addr.is_loopback or addr.is_private or addr.is_link_local
        or addr.is_multicast or addr.is_reserved or addr.is_unspecified
    )


def _validate_public_url(url: str) -> None:
    """校验 URL：仅 http(s)，且解析后的所有 IP 都是公网地址。

    抛出 _UnsafeURLError 表示不允许请求（可拦截访问 127.0.0.1、
    内网管理面、169.254.169.254 云元数据端点等 SSRF）。
    注意：DNS 解析与实际连接之间存在极小的 rebinding 时间窗，
    本地单用户工具场景下以此平衡实现复杂度。
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise _UnsafeURLError("仅允许 http/https 且必须包含主机名")
    host = parsed.hostname
    # 裸 IP 直接判断
    try:
        ipaddress.ip_address(host)
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror as e:
            raise _UnsafeURLError(f"DNS 解析失败: {e}") from e
        ips = {info[4][0] for info in infos}
        if not ips or any(_is_blocked_ip(ip) for ip in ips):
            raise _UnsafeURLError(f"主机名 {host} 解析到非公网地址: {sorted(ips)}")
    else:
        if _is_blocked_ip(host):
            raise _UnsafeURLError(f"目标为非公网地址: {host}")
    if parsed.username or parsed.password:
        raise _UnsafeURLError("不允许在 URL 中携带用户名/密码")


def _safe_http_get(url: str, timeout: float = 10):
    """发起经过 SSRF 校验的 GET，逐跳校验重定向目标，返回未关闭的 Response。"""
    current = url
    for _ in range(MAX_IMAGE_REDIRECTS + 1):
        _validate_public_url(current)
        resp = requests.get(
            current, timeout=timeout, stream=True,
            headers={"User-Agent": "Mozilla/5.0"},
            allow_redirects=False,
        )
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location", "")
            resp.close()
            if not location:
                raise _UnsafeURLError("重定向响应缺少 Location")
            current = urljoin(current, location)
            continue
        return resp
    raise _UnsafeURLError(f"重定向超过 {MAX_IMAGE_REDIRECTS} 次")


def download_images(content: str, md_filepath: Path, max_images: int = 30,
                    timeout: float = 10) -> tuple[str, int, dict]:
    """从 Markdown 内容中提取图片 URL 并下载到同名子目录。

    返回 (更新后的 Markdown 内容, 下载成功数量, shortfall)。

    shortfall 描述「有多少图没能本地化」，形如
    `{"total": 12, "ok": 8, "truncated": 2, "failed": 2, "max": 30}`；
    一张都不缺时返回空 dict。

    **为什么必须把它交出去**：未本地化的图片会在正文里**保留远程地址**，源站一旦
    删除就永久丢失。过去这种情况只打一行服务端日志、raw 看上去「一切正常」——
    等于把一份不完整的快照静默地当成完整剪藏入库。调用方据此写进 raw 并回报用户。

    按 AGENTS.md §7：图片放在同名子目录下。
    例如 raw/articles/2026-08-27-article.md 的图片放到
         raw/articles/2026-08-27-article/image_001.jpg
    """
    # 提取所有 Markdown 图片 URL：![alt](url)
    img_pattern = re.compile(r'!\[([^\]]*)\]\(([^)]+)\)')
    matches = img_pattern.findall(content)

    if not matches:
        return content, 0, {}

    # 只处理 http/https URL
    http_imgs = [(alt, url) for alt, url in matches
                 if url.startswith("http://") or url.startswith("https://")]

    if not http_imgs:
        return content, 0, {}

    # 限制数量：超出部分**不静默丢弃**——计入 shortfall.truncated 并告知用户
    # （这些图仍以远程地址留在正文里）。
    total = len(http_imgs)
    truncated = max(0, total - max_images)
    http_imgs = http_imgs[:max_images]

    if not http_imgs:
        # 全部被上限截掉（max_images=0）—— 不建线程池，直接如实回报缺失
        return content, 0, {"total": total, "ok": 0, "truncated": truncated,
                            "failed": 0, "max": max_images}

    # 图片目录（同名子目录，去掉 .md）；延迟到确有图片通过校验后再创建，
    # 避免全部图片被 SSRF 策略拦截时留下空目录
    img_dir_name = md_filepath.stem
    img_dir = md_filepath.parent / img_dir_name

    def _download_one(item):
        i, (alt, img_url) = item
        resp = None
        try:
            # ===== 飞书文档图片：必须走 lark-cli =====
            # `https://feishu.cn/file/<token>` 不是图片地址，未登录 GET 会被
            # 重定向到登录页（返回 HTML）。通用的 _safe_http_get 只会把这张图
            # 判成「URL 不安全」跳过 —— 用户看到的就是「17 张图全部未本地化」。
            media_token = feishu_media_token(img_url)
            if media_token:
                img_dir.mkdir(parents=True, exist_ok=True)
                saved = download_media(media_token, img_dir / f"image_{i:03d}")
                if saved.suffix.lower() == ".svg":
                    saved.unlink(missing_ok=True)
                    return None, img_url, "类型不受支持"
                if saved.stat().st_size > MAX_IMAGE_BYTES:
                    saved.unlink(missing_ok=True)
                    raise Exception(
                        f"图片超过 {MAX_IMAGE_BYTES // 1024 // 1024}MB 上限")
                return img_url, alt, saved.name

            # 确定文件扩展名（拒绝 SVG：可内嵌脚本/外链实体，不适合作为静态快照）
            path_ext = Path(urlparse(img_url).path).suffix.lower()
            if path_ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
                path_ext = ".jpg"  # 默认

            local_name = f"image_{i:03d}{path_ext}"
            local_path = img_dir / local_name

            # 安全下载（SSRF 过滤 + 重定向逐跳校验 + 大小上限）
            resp = _safe_http_get(img_url, timeout=timeout)
            resp.raise_for_status()

            # 检查 Content-Type（拒绝 SVG 等非光栅图片类型）
            ct = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if ct and not (ct.startswith("image/") and "svg" not in ct) \
                    and ct != "application/octet-stream":
                return None, img_url, "类型不受支持"

            # 写入文件，强制大小上限防止磁盘耗尽
            img_dir.mkdir(parents=True, exist_ok=True)
            downloaded_bytes = 0
            with open(local_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    downloaded_bytes += len(chunk)
                    if downloaded_bytes > MAX_IMAGE_BYTES:
                        f.close()
                        local_path.unlink(missing_ok=True)
                        raise Exception(
                            f"图片超过 {MAX_IMAGE_BYTES // 1024 // 1024}MB 上限")
                    f.write(chunk)

            return img_url, alt, local_name

        except _UnsafeURLError as e:
            print(f"[!] 图片 URL 不安全，跳过: {img_url[:60]} → {e}")
            return None, img_url, "URL 不安全"
        except Exception as e:
            print(f"[!] 图片下载失败: {img_url[:60]} → {e}")
            return None, img_url, "下载失败"
        finally:
            if resp is not None:
                resp.close()
        return None, img_url, "未知原因"

    # 并发下载：串行时 30 张图可达 50s+，会触发飞书 bot 的 30s 请求超时。
    # 飞书图片走 lark-cli（每张一个 node 进程，内存不便宜）→ 压低并发，
    # 否则 8 个 node 同时起来会吃掉 1GB+。
    has_feishu_media = any(feishu_media_token(u) for _, u in http_imgs)
    workers = min(4 if has_feishu_media else 8, len(http_imgs))
    downloaded_items = []
    failed_items = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for item in pool.map(_download_one, list(enumerate(http_imgs, 1))):
            if not item:
                continue
            if item[0] is None:          # (None, url, reason) = 这张没本地化
                failed_items.append((item[1], item[2]))
            else:
                downloaded_items.append(item)

    # 按图片序号顺序替换 URL，保证多图/重复 URL 与原串行行为一致
    updated_content = content
    for img_url, alt, local_name in sorted(downloaded_items, key=lambda it: it[2]):
        old_ref = f"![{alt}]({img_url})"
        new_ref = f"![{alt}]({img_dir_name}/{local_name})"
        updated_content = updated_content.replace(old_ref, new_ref, 1)

    shortfall = {}
    if truncated or failed_items:
        shortfall = {
            "total": total,
            "ok": len(downloaded_items),
            "truncated": truncated,
            "failed": len(failed_items),
            "max": max_images,
        }

    return updated_content, len(downloaded_items), shortfall


def _image_shortfall_note(shortfall: dict) -> str:
    """把 shortfall 渲染成一句人话；没有缺失时返回空串。

    **这是「图片没全本地化」的唯一文案来源**：raw 的自动区、服务端响应、
    飞书卡片、扩展弹窗都从这里取。各自拼一套的结果我们已经吃过苦头 ——
    同一件事在四个界面说法不一（见 background.js 顶部那段一致性约束）。
    """
    if not shortfall:
        return ""
    total = int(shortfall.get("total") or 0)
    ok = int(shortfall.get("ok") or 0)
    missing = total - ok
    if missing <= 0:
        return ""
    why = []
    if shortfall.get("truncated"):
        why.append(f"{shortfall['truncated']} 张超出单篇 {shortfall.get('max', 30)} 张上限")
    if shortfall.get("failed"):
        why.append(f"{shortfall['failed']} 张下载失败")
    tail = f"（{'；'.join(why)}）" if why else ""
    return (f"图片未全部本地化：共 {total} 张，已本地化 {ok} 张，"
            f"其余 {missing} 张保留远程地址{tail}。")


def _apply_image_note(content: str, shortfall: dict) -> tuple[str, str]:
    """把「图片未全部本地化」写进 raw 的自动区，返回 (新内容, 说明)。

    写进自动区而不是 frontmatter：frontmatter 字段受 `lint.py` 白名单约束，
    而这是「本页的一次性事实」，放正文注记即可（与既有 `> 注：…` 同层）。
    无缺失时原样返回，说明为空串。
    """
    note = _image_shortfall_note(shortfall)
    if not note:
        return content, ""
    line = f"> ⚠️ {note}\n\n"
    marker = "<!-- clip:auto:end -->"
    if marker in content:
        new = content.replace(marker, line + marker, 1)
    else:
        new = content.rstrip() + "\n\n" + line
    return new, note


def clip_from_url(url: str, via: str = "server", dry_run: bool = False,
                  sync_wiki: bool = False) -> dict:
    """通过 URL 剪藏：自动匹配平台适配器。

    注意：本函数只负责「抓取 + 落盘」。去重必须在调用方于落盘前完成
    （见 _preflight_duplicate）。dry_run=True 时只抓取与生成卡片，不写盘、
    不下载图片、不入队——供调用方先做去重判定再决定是否真正落盘。
    """
    result: ClipResult = extract_url(url)

    # 计算稳定来源 ID
    final_url = result.url or url
    canonical = resolve_short_link(final_url)
    source_id = normalize_url(canonical)

    data = {
        "title": result.title,
        "url": final_url,
        # **任务原始 URL**（调用方发来的那条）。适配器可能把 url 改成跳转后的地址
        # （小红书会解包登录墙、公众号会跟随重定向），一旦抓回来的是风控/验证页，
        # 待审卡片必须记**用户最初发的那条链接**，否则下次重试的还是那张拦截页。
        # 与 handle_browser_result 里的同名键同一语义。
        "_origin_url": url,
        "source_id": source_id,
        "author": result.author,
        "publishedAt": result.published_at,
        "content": result.content,
        "type": result.type,
        "platform": result.platform,
        "tags": result.tags,
        "duration": result.duration,
        # 平台适配器的附加信息（如 has_subtitles）。**必须带上**：
        # _content_incomplete_reason() 靠它判断「视频有没有拿到字幕/转录」，
        # 决定素材是否完整、要不要退回待审区。此前这里漏了 extra，
        # 导致服务端抓的视频永远看不出字幕缺失。
        "extra": dict(result.extra or {}),
        "via": via,
    }

    filename, content, word_count = generate_card(data)
    filepath = ARTICLES_DIR / filename

    if dry_run:
        return {
            "success": True,
            "filename": filename,
            "content": content,
            "data": data,
            "wordCount": word_count,
            "filepath": str(filepath.relative_to(VAULT)),
            "platform": result.platform,
            "type": result.type,
            "imageCount": 0,
            "dryRun": True,
        }

    # 下载图片到同名子目录
    content, img_count, img_sf = download_images(content, filepath)
    content, img_note = _apply_image_note(content, img_sf)
    if img_count > 0:
        print(f"[+] 下载 {img_count} 张图片 → {filepath.stem}/")
    if img_note:
        print(f"[!] {img_note}")

    filepath.write_text(content, encoding="utf-8")

    # wiki 精加工。
    # sync_wiki=True 给「从待审区处理」用：那条路径会在这个调用返回后删掉指针卡片，
    # 若 wiki 是异步的、之后失败又撤回 raw，就会出现「界面说已入库、其实条目退回待审区」
    # 的矛盾。等结论才能如实回报。
    wiki = _maybe_queue_wiki_gen(filepath, data, sync=sync_wiki)
    demoted = wiki.get("status") == "demoted"
    # raw 已落地 → 同 URL 的待审指针完成使命（demoted 时函数内部会跳过）
    # origin_url=url：data["url"] 可能是适配器解析后的跳转地址，而待审卡片里
    # 记的是用户最初发来的链接，两者都试一遍才不会漏摘。
    _settle_inbox_pointer(data, wiki, origin_url=url)

    return {
        "success": True,
        "wordCount": word_count,
        "filename": "" if demoted else filename,
        "filepath": "" if demoted else str(filepath.relative_to(VAULT)),
        "platform": result.platform,
        "type": result.type,
        "imageCount": 0 if demoted else img_count,
        # 图片没全本地化时必须让调用方知道（否则等于静默交付不完整快照）
        "imageWarning": "" if demoted else img_note,
        "wiki": wiki,
    }


class ClipHandler(BaseHTTPRequestHandler):

    # 豁免鉴权的路径（仅健康检查；SSE 支持 ?token= 后不再豁免）
    _AUTH_EXEMPT = {"/api/health"}

    # 读取请求体时的单次读上限与总超时（秒）。
    #
    # 为什么要自己读：BaseHTTPRequestHandler 的 `rfile.read(n)` 在客户端
    # **中途断开**时会抛 ConnectionAbortedError（Windows 上表现为
    # WinError 10053「你的主机中的软件中止了一个已建立的连接」）。
    # 触发场景极其常见：用户在弹窗里点了「立即完整入库」，请求发出后又顺手
    # 关掉了弹窗 —— Chrome 会**中止**这次 fetch，服务端读 body 时便拿到半截
    # 数据或直接抛错。这类情况**不是服务端故障**，不该记成一条刺眼的「失败」。
    def _read_body(self):
        """读取并解析 JSON 请求体。

        返回 (data, error)：
          - 成功 → (dict, None)
          - 客户端中途断开 → (None, "client_disconnect")（调用方静默处理）
          - 传输/解析错误 → (None, 人类可读的原因)
        """
        try:
            content_length = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            return None, "Content-Length 不是有效数字"
        if content_length <= 0:
            return {}, None
        # 分块读，避免 read(n) 一次性要求全部字节、遇到半包就抛错。
        self.connection.settimeout(30)
        chunks = []
        remaining = content_length
        try:
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    # 连接在读完之前就结束了 = 客户端主动断开
                    raise ConnectionError("请求体未读全")
                chunks.append(chunk)
                remaining -= len(chunk)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError,
                TimeoutError, ConnectionError, OSError):
            return None, "client_disconnect"
        raw = b"".join(chunks)
        try:
            return json.loads(raw.decode("utf-8")), None
        except Exception as e:
            return None, f"请求体不是合法 JSON：{e}"

    def do_OPTIONS(self):
        origin = _cors_allowed_origin(self.headers.get("Origin", ""))
        if not origin:
            # 非白名单源：不返回任何 CORS 头，浏览器将拦截跨域请求
            self.send_response(403)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS, GET")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _require_auth(self) -> bool:
        """检查鉴权，失败时自动发送 401 响应。返回 True 表示通过。"""
        if self.path.split("?", 1)[0] in self._AUTH_EXEMPT:
            return True
        if not _check_auth(self.headers, self.path):
            self.send_json({"error": "unauthorized"}, status=401)
            return False
        return True

    def do_GET(self):
        if not self._require_auth():
            return
        # SSE：浏览器连接拉取任务（EventSource 通过 ?token= 鉴权）
        if self.path.split("?", 1)[0] == "/api/browser/connect":
            self.handle_browser_connect()
            return
        # 任务状态查询
        if self.path.startswith("/api/task/"):
            task_id = self.path.split("/api/task/")[1]
            status = task_queue.get_task_status(task_id)
            if status:
                self.send_json(status)
            else:
                self.send_json({"error": "任务不存在"}, status=404)
            return
        # 健康检查
        if self.path == "/api/health":
            stats = task_queue.get_stats()
            self.send_json({
                "ok": True,
                "status": "running",
                "authRequired": bool(SERVER_TOKEN),
                "platforms": list_platforms(),
                "pendingTasks": stats["pending"],
                "taskStats": stats,
            })
            return
        # 任务列表
        #   ?scope=active （默认）只回活跃任务（pending + processing）
        #   ?scope=history       回全部任务（含已完成/已失败），按时间倒序
        #   ?status=xxx          按单一状态过滤
        # 为什么要有 history：任务一旦成功/失败就从前端列表消失，用户
        # 无法回看「刚才那条到底成没成」，也无法区分「没提交」与「已完成」。
        if self.path.startswith("/api/tasks"):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            status_filter = params.get("status", [""])[0]
            scope = params.get("scope", ["active"])[0]
            # today=1 时只返回今日记录，口径与「今日统计」指标卡一致
            today_only = params.get("today", ["0"])[0] == "1"
            try:
                limit = int(params.get("limit", ["50"])[0])
            except ValueError:
                limit = 50
            if status_filter:
                tasks = task_queue.list_tasks_by_status(status_filter, limit=limit)
            elif scope == "history":
                tasks = task_queue.list_recent_tasks(limit=limit, today_only=today_only)
            elif scope == "all":
                tasks = task_queue.list_recent_tasks(limit=limit, today_only=today_only)
            else:
                tasks = task_queue.list_active_tasks()
            self.send_json({
                "tasks": tasks,
                "count": len(tasks),
                "scope": scope if not status_filter else f"status:{status_filter}",
                "today_only": today_only,
            })
            return
        # 今日统计
        if self.path == "/api/stats":
            stats = task_queue.get_today_stats()
            self.send_json(stats)
            return
        # Inbox 列表
        if self.path == "/api/inbox":
            items = _inbox_list()
            self.send_json({"items": items, "count": len(items)})
            return
        # GET 方式剪藏（方便测试）
        if self.path.startswith("/api/clip-url?"):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            url = params.get("url", [""])[0]
            if not url:
                self.send_json({"success": False, "error": "缺少 url 参数"}, status=400)
                return
            self._smart_clip(url)
            return
        self.send_error(404)

    def do_POST(self):
        if not self._require_auth():
            return
        if self.path == "/api/clip":
            self.handle_clip()
        elif self.path == "/api/clip-url":
            self.handle_url_clip()
        elif self.path == "/api/browser/result":
            self.handle_browser_result()
        elif self.path.startswith("/api/task/") and self.path.endswith("/retry"):
            self.handle_task_retry()
        elif self.path.startswith("/api/task/") and self.path.endswith("/cancel"):
            self.handle_task_cancel()
        elif self.path == "/api/inbox":
            self.handle_inbox_create()
        elif self.path == "/api/inbox/process":
            self.handle_inbox_process()
        else:
            self.send_error(404)

    def handle_task_retry(self):
        """重试失败任务。POST /api/task/{id}/retry

        两条重试路径，按平台决定：
        - 允许浏览器降级（大多数）→ 重置为 pending，等浏览器扩展来领。
        - 不允许降级（飞书文档）→ 必须回到后端 lark-cli。只置 pending 的话
          任务会被浏览器领走、抓出一份残缺正文，或者干脆永远等不到。
        """
        task_id = self.path.split("/api/task/")[1].split("/retry")[0]
        info = task_queue.get_task_status(task_id)
        if not info:
            self.send_json({"success": False, "error": "任务不存在"}, status=400)
            return

        url = info.get("url") or ""
        platform = get_platform(url) if url else None
        backend_only = platform is not None and platform.allows_browser_fallback is False

        if not backend_only:
            result = task_queue.retry_task(task_id)
            if result.get("success"):
                self.send_json(result)
            else:
                self.send_json(result, status=400)
            return

        # —— 后端重试：任务留在本进程里跑，不经过浏览器 ——
        if info.get("status") not in ("failed", "done"):
            self.send_json({"success": False,
                            "error": f"任务正在处理中，无需重试（当前状态: {info['status']}）"},
                           status=400)
            return

        result = task_queue.requeue_for_backend(task_id)
        if not result.get("success"):
            self.send_json(result, status=400)
            return

        print(f"[~] 后端重试 [{platform.name}] {task_id}: {url[:80]}")
        self.send_json({
            "success": True,
            "id": task_id,
            "url": url,
            "message": f"已重新提交到后端通道（{platform.name}），请稍候",
            "statusUrl": f"/api/task/{task_id}",
        })

        # 抓取可能要几十秒（飞书长文档），绝不能占着 HTTP 线程；
        # respond 走收集器 —— 这条路径没有可写的 socket。
        via = result.get("via") or platform.name
        threading.Thread(
            target=self._backend_retry_worker,
            args=(task_id, url, via),
            daemon=True,
        ).start()

    def _backend_retry_worker(self, task_id: str, url: str, via: str):
        """后台线程：重跑后端抓取（重试专用，不回包）。"""
        responded = []
        try:
            self._smart_clip(url, via=via, reuse_task_id=task_id,
                             respond=lambda *a, **kw: responded.append((a, kw)))
        except Exception as e:
            print(f"[!] 后端重试异常 [{task_id}]: {e}", file=sys.stderr)
            task_queue.fail_task(task_id, f"重试失败：{e}")
        else:
            # _smart_clip 成功/失败都会自己写任务状态；这里只兜「静默返回」
            info = task_queue.get_task_status(task_id) or {}
            if info.get("status") == "processing":
                task_queue.fail_task(task_id, "重试未产生结果（后端通道未返回结论）")

    def handle_task_cancel(self):
        """取消待处理任务。POST /api/task/{id}/cancel"""
        task_id = self.path.split("/api/task/")[1].split("/cancel")[0]
        result = task_queue.cancel_task(task_id)
        if result.get("success"):
            self.send_json(result)
        else:
            self.send_json(result, status=400)

    def handle_inbox_create(self):
        """创建 inbox 指针卡片。POST /api/inbox"""
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            data = json.loads(body)
            url = data.get("url", "").strip()
            if not url:
                self.send_json({"success": False, "error": "缺少 url"}, status=400)
                return

            result = _inbox_create_card(
                url=url,
                title=data.get("title", ""),
                platform=data.get("platform", ""),
                note=data.get("note", ""),
                live_source=data.get("liveSource", False),
            )
            if result.get("success"):
                print(f"[+] inbox 指针卡片: {result['filename']}")
                self.send_json(result)
            elif result.get("error") in ("duplicate", "already_archived"):
                # already_archived：内容早已完整入库，无需再进待审区。
                # 仍用 409 让客户端走「已有版本」提示分支，但回传的 error
                # 不同，前端据此区分措辞（避免说成「已在待审区中」造成误解）。
                if result.get("error") == "already_archived":
                    print(f"[=] 已入库，跳过待审区建卡：{result.get('existing_file')}")
                self.send_json(result, status=409)
            else:
                self.send_json(result, status=400)
        except Exception as e:
            print(f"[!] inbox 创建失败: {e}", file=sys.stderr)
            self.send_json({"success": False, "error": str(e)}, status=500)

    def handle_inbox_process(self):
        """处理 inbox 条目。POST /api/inbox/process
        body: {"filename": "2026-08-31-001-example.com.md"} 或 {"all": true}
        """
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            data = json.loads(body)

            if data.get("all"):
                # 批量处理所有 inbox 条目
                items = _inbox_list()
                results = []
                for item in items:
                    r = _inbox_process_item(item["filename"])
                    results.append({"filename": item["filename"], **r})
                self.send_json({
                    "success": True,
                    "processed": len(results),
                    "results": results,
                })
                return

            filename = data.get("filename", "").strip()
            if not filename:
                self.send_json({"success": False, "error": "缺少 filename"}, status=400)
                return

            result = _inbox_process_item(filename)
            if result.get("success"):
                print(f"[+] inbox 处理完成: {filename}")
            else:
                print(f"[!] inbox 处理失败: {filename} → {result.get('error')}")
            self.send_json(result)
        except Exception as e:
            print(f"[!] inbox 处理异常: {e}", file=sys.stderr)
            self.send_json({"success": False, "error": str(e)}, status=500)

    def handle_clip(self):
        """浏览器插件推送已提取好的内容。"""
        body = None
        direct_task_id = None
        try:
            data, err = self._read_body()
            if err == "client_disconnect":
                # 客户端（弹窗）在请求发出后中途关闭 → Chrome 中止了 fetch。
                # 这不是服务端故障，也**不该**记成一条「失败」任务：
                # 用户看到的「❌ 失败（请求体未解析成功）[WinError 10053]」正是
                # 这样来的，会让人以为是抓取功能坏了。这里只打一行日志。
                print("[i] 客户端中途断开，忽略本次推送（通常是弹窗被关闭）")
                return
            if err:
                self.send_json({"success": False, "error": err}, status=400)
                return
            body = json.dumps(data, ensure_ascii=False)
            # 不要无条件覆写成「浏览器插件」：那会让悬浮按钮 / 右键选中 /
            # 插件面板三条路径在任务列表里无法区分。仅在客户端没传时才兜底。
            data["via"] = _clip_via(data)

            # 校验请求体
            error = _validate_request(body.encode("utf-8"), data)
            if error:
                self.send_json({"success": False, "error": error}, status=400)
                return

            # 先立 processing 占位：同步抓取 + wiki 可能耗时数分钟，
            # 任务面板的「进行中」必须从请求一开始就能看到本次剪藏。
            direct_task_id = task_queue.start_direct_clip(
                str(data.get("url") or ""), via=_clip_via(data))

            # 计算稳定来源 ID
            if not data.get("source_id") and data.get("url"):
                canonical = resolve_short_link(data["url"])
                data["source_id"] = normalize_url(canonical)

            force = data.get("force", False)

            # ===== 去重闸门（写盘前）=====
            # 先算新内容字数、再判定重复；命中重复直接返回，绝不落盘。
            new_content_text = data.get("content", "")
            gate = _preflight_duplicate(
                data.get("source_id", ""), new_content_text, force=force)
            if gate:
                if gate.get("_status") == 409:
                    existing_path = VAULT / gate["existing_file"]
                    if existing_path.exists():
                        print(f"[*] 已存在相同来源，跳过：{gate['existing_file']}"
                              f"（{gate['existing_word_count']} 字）")
                    if gate.get("newer_more_complete"):
                        # 不自动覆盖（旧版可能含用户手写笔记）——把决定权交回用户。
                        print(f"[i] 本次正文更多（{gate['existing_word_count']} → "
                              f"{gate['new_word_count']} 字），已返回 409 等用户确认覆盖")
                    # 记录「本次客户端提交了多少字」——排查「只抓到 667 字」这类
                    # 投诉时必须能一眼看到：667 是客户端提交量，不是服务端算的。
                    print(f"[*] 本次提交：{gate.get('new_word_count')} 字，"
                          f"content 原始长度 {len(str(data.get('content') or ''))}，"
                          f"channel={_clip_via(data)}")
                    # 逐 frame 长度快照（扩展侧 extractAcrossFrames 带回）。
                    # 有它才能判断是「没扫到 iframe」还是「扫到了但里面也少」。
                    _fd = data.get("_frameDiag") or []
                    if _fd:
                        for _i, _f in enumerate(_fd):
                            print(f"    frame[{_i}] n={_f.get('n')} "
                                  f"type={_f.get('type')} url={_f.get('url')}")
                    # 内容早就在库里了 → 待审指针同样是「已完成」。
                    # 与 _inbox_process_item 撞到重复时删卡保持同一规矩。
                    _inbox_clear_for_url(str(data.get("url") or "").strip())
                else:
                    # 覆盖旧版（先删旧，再走正常写盘）。走到这里**只可能是用户
                    # 主动点了「强制覆盖」（force=True）**：2026-10-14 起不再有
                    # 「新版更完整就自动覆盖」的静默路径。
                    existing_path = VAULT / gate["existing"]["path"]
                    print(f"[*] 用户确认覆盖（{gate['existing_word_count']} → "
                          f"{gate['new_word_count']} 字）：{gate['existing']['path']}")
                    _delete_article_with_images(existing_path)
                    data["_replaced"] = True
                    data["_replaced_path"] = gate["existing"]["path"]
                    gate = None
                if gate:
                    gate.pop("_status", None)
                    # 去重命中同样是「一次有结论的剪藏」，补记一条历史，
                    # 否则用户在任务面板里看不到这条（以为没提交成功）。
                    task_queue.record_direct_clip(
                        str(data.get("url") or ""), via=_clip_via(data),
                        result={"duplicate": True,
                                "existingFile": gate.get("existing_file", ""),
                                "wordCount": gate.get("existing_word_count", 0)},
                        status="done",
                        error="",
                        task_id=direct_task_id,
                    )
                    self.send_json(gate, status=409)
                    return

            filename, content, word_count = generate_card(data)
            filepath = ARTICLES_DIR / filename

            # 下载图片到同名子目录
            content, img_count, img_sf = download_images(content, filepath)
            content, img_note = _apply_image_note(content, img_sf)
            if img_count > 0:
                print(f"[+] 下载 {img_count} 张图片 → {filepath.stem}/")
            if img_note:
                print(f"[!] {img_note}")

            filepath.write_text(content, encoding="utf-8")

            # wiki 精加工：「完整入库」（wikiSync=true）等结论，其余场景仍走后台线程
            wiki = _maybe_queue_wiki_gen(filepath, data, sync=bool(data.get("wikiSync")))

            demoted = wiki.get("status") == "demoted"
            # 「立即完整入库」成功后摘掉同 URL 的待审指针 ——
            # 否则界面会同时显示「✅ 入库成功」和「待审区 2」，自相矛盾
            _settle_inbox_pointer(data, wiki)
            if wiki["status"] == "ok":
                print(f"[+] 入库成功: {filename} (约 {word_count} 字) + wiki 已生成")
            elif wiki["status"] == "pending":
                print(f"[+] 入库成功: {filename} (约 {word_count} 字)，wiki 后台生成中")
            elif demoted:
                print(f"[!] 入库未完成: wiki 未生成，raw 已撤回并存入待审区"
                      f"（{wiki.get('reason', '')}）")
            else:
                print(f"[!] raw 已入库但 wiki 未生成: {filename} (约 {word_count} 字)"
                      f" —— {wiki.get('reason', '')}")

            resp = {
                # success 表示「内容已抓到并处理」；是否算「完整入库」由 wiki 字段决定
                "success": True,
                "wordCount": word_count,
                # wiki 未生成时 raw 已被撤回，此时不应再回报一个不存在的文件
                "filename": "" if demoted else filename,
                "filepath": "" if demoted else str(filepath.relative_to(VAULT)),
                "imageCount": 0 if demoted else img_count,
                # 图片未全本地化时如实告知（前端弹窗/通知据此加一行提示）
                "imageWarning": "" if demoted else img_note,
                "wiki": wiki,
                "wikiFile": "" if demoted else _wiki_ref_for(filepath),
            }
            if data.get("_replaced"):
                resp["replaced"] = True
                resp["previous_file"] = data["_replaced_path"]

            # 补记任务历史：/api/clip 直接推送不经任务队列，过去完全不写记录，
            # 于是「立即完整入库」在任务面板里凭空消失。这里如实登记一条。
            # status：wiki 未生成（含 demoted）→ failed，让面板能把「未完整入库」
            # 与「完整入库」区分开，而不是一律显示成功。
            wiki_ok = wiki.get("status") in ("ok", "pending", "skipped")
            task_queue.record_direct_clip(
                str(data.get("url") or ""), via=_clip_via(data),
                result={
                    "wordCount": word_count,
                    "filename": resp["filename"],
                    "wikiStatus": wiki.get("status", ""),
                    "wikiReason": wiki.get("reason", ""),
                },
                status="done" if wiki_ok else "failed",
                error="" if wiki_ok else (wiki.get("reason") or "wiki 未生成"),
                task_id=direct_task_id,
            )
            self.send_json(resp)
        except Exception as e:
            # 客户端断开也可能发生在**长耗时处理之后的回包阶段**：wikiSync=true
            # 时服务端在同一个请求里同步等 wiki 生成（可达数分钟），MV3 service
            # worker 被回收会中止 fetch，于是这里的 send_json 才撞上 Broken pipe。
            # _read_body 只能覆盖读请求体阶段，拦不住这种情况。此刻成功记录已在
            # 上面写入，再补一条 failed（且 send_json 还会再抛一次）就是假失败。
            if isinstance(e, (ConnectionAbortedError, ConnectionResetError,
                              BrokenPipeError)):
                print("[i] 回包时发现客户端已断开（本次剪藏通常已成功，"
                      "见上一条入库记录），不登记失败任务")
                return
            print(f"[!] 剪藏失败: {e}", file=sys.stderr)
            # 客户端断开已经在上面静默处理了；能走到这里的是**真正的服务端错误**。
            # 仍如实登记一条失败，但错误信息用人类可读的措辞，不要再把
            # WinError 10053 这类底层 socket 错误直接甩给用户。
            task_queue.record_direct_clip(
                "（剪藏未完成）", via="浏览器插件",
                result={}, status="failed", error=str(e),
                task_id=direct_task_id,
            )
            self.send_json({"success": False, "error": str(e)}, status=500)

    def handle_url_clip(self):
        """URL 剪藏：智能判断走后端还是走浏览器。"""
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            data = json.loads(body)
            url = data.get("url", "").strip()

            if not url:
                self.send_json({"success": False, "error": "缺少 url"}, status=400)
                return

            # 校验请求体
            error = _validate_request(body.encode("utf-8"), data)
            if error:
                self.send_json({"success": False, "error": error}, status=400)
                return

            force = data.get("force", False)
            # wikiSync 必须显式透传：_smart_clip 里的 `data` 是**新抓取的结果**，
            # 不是这个请求体 —— 以前它读 data.get("wikiSync") 永远是 None，
            # 于是这条路径的 wiki 一直是异步的，客户端拿不到真实结论。
            self._smart_clip(url, force=force,
                             sync_wiki=bool(data.get("wikiSync")),
                             via=_clip_via(data),
                             # 调用方明确要求本次不做图片文字提取（插件取消勾选 /
                             # 飞书「不OCR」前缀）→ 透传到「图多字少」闸门。
                             skip_ocr=(data.get("ocr") is False))
        except Exception as e:
            print(f"[!] URL 剪藏失败: {e}", file=sys.stderr)
            self.send_json({"success": False, "error": str(e)}, status=500)

    def _smart_clip(self, url: str, force: bool = False, sync_wiki: bool = False,
                    via: str = "", skip_ocr: bool = False,
                    respond=None, reuse_task_id: str = ""):
        """智能分发：先去重，再判断走后端还是走浏览器。

        sync_wiki：由请求体的 wikiSync 透传而来。为 True 时等 wiki 出结论再返回，
        失败则撤回 raw 转待审区（与 /api/clip 的行为对齐）。

        via：请求来源渠道，写进任务记录供面板分类展示（不参与调度）。

        respond：回包函数，默认 `self.send_json`。任务重试在后台线程里调用本
        方法时没有可写的 socket，传入一个只收集结果的函数即可 —— 否则回包
        阶段会对着已关闭的连接抛 BrokenPipe。

        reuse_task_id：复用指定任务 id（重试场景）。给了就不再新建任务、也
        跳过同 URL 并发查重（那个「重复」就是它自己）。

        去重策略：
        - force=true → 跳过查重，直接覆盖
        - 新内容比旧内容多 30%+ → 覆盖旧版（认为旧版不完整）
        - live-source → 创建新版本 -v{N}
        - 其他 → 返回 duplicate
        """
        if respond is None:
            respond = self.send_json

        # ===== URL 去重：短链解析 + 规范化 =====
        canonical_url = resolve_short_link(url)
        normalized = normalize_url(canonical_url)

        platform = get_platform(url)

        # 并发保护：同 URL 已有抓取占位/队列任务时直接返回其任务 ID
        active_fetch_id = "" if reuse_task_id else task_queue.find_active_task(url)
        if active_fetch_id and not force:
            print(f"[=] 该链接正在处理中 [{active_fetch_id}]: {url[:80]}")
            respond({
                "success": True,
                "taskId": active_fetch_id,
                "via": "browser-pending",
                "message": "该链接正在处理中，请稍候",
                "statusUrl": f"/api/task/{active_fetch_id}",
            }, status=202)
            return

        # 所有平台（含普通网页）先试后端直抓，失败再降级浏览器协助队列。
        #
        # **例外：视频平台（YouTube / B 站）直接跳过后端。**
        # 实测（2026-10-07）：这条路会 spawn `yt-dlp.EXE` ——
        #   ① clip-server 跑在 pythonw 下没有控制台，spawn 控制台程序必然
        #      **弹出一个 cmd 窗口**（用户看到的就是它）；
        #   ② YouTube 在命令行根本出不了网（浏览器有代理、命令行没有）→ 30 秒超时；
        #   ③ B 站的 yt-dlp 读不到 Chrome cookie（Windows App-Bound Encryption）
        #      → 1.8 秒就退出。
        # 也就是说：走这段代码**必然**白等一次 + 弹窗 + 抛一串技术报错，
        # 然后才降级到浏览器协助 —— 而浏览器协助本来就是唯一可行的通道
        # （字幕走页面内的 InnerTube / WBI 接口）。所以直接进队列。
        if platform is not None and not _prefer_browser(url):
            if reuse_task_id:
                fetch_task_id = reuse_task_id
                task_queue.update_stage(fetch_task_id, "server-fetch")
            else:
                fetch_task_id = task_queue.start_server_task(
                    url, via=(via or "服务端直抓"))
            try:
                result: dict = {}
                # 第一次抓取：dry-run 不落盘，仅用于去重判定
                result = clip_from_url(url, via=platform.name, dry_run=True)
                result["via"] = "server-" + platform.name

                # ===== 去重闸门（写盘前）=====
                gate = _preflight_duplicate(normalized, result.get("content", ""), force=force)
                if gate and gate.get("_status") == 409:
                    print(f"[*] 已存在相同来源，跳过：{gate['existing_file']}"
                          f"（{gate['existing_word_count']} 字）")
                    if gate.get("newer_more_complete"):
                        print(f"[i] 本次正文更多（{gate['existing_word_count']} → "
                              f"{gate['new_word_count']} 字），已返回 409 等用户确认覆盖")
                    # 命中去重**不是失败**：内容早已在库里，没丢数据也没出错。
                    # 过去记 fail_task(id, "duplicate") → 「最近记录」里凭空多出
                    # 一条红色失败任务，用户会以为这次剪藏坏了（2026-10-09 报过）。
                    # 改为记 done + result.duplicate，前端据此显示
                    # 「📌 已存在（未重复入库）」。
                    task_queue.complete_duplicate_task(fetch_task_id, {
                        "existingFile": gate.get("existing_file", ""),
                        "wordCount": gate.get("new_word_count", 0),
                    })
                    _inbox_clear_for_url(url)
                    gate.pop("_status", None)
                    respond(gate, status=409)
                    return

                # 命中覆盖：删旧文件（旧文件被新文件取代，保留原文件名）。
                # 只有 force=True（用户点了「强制覆盖」）才会走到这里。
                if gate and gate.get("_overwrite"):
                    existing_path = VAULT / gate["existing"]["path"]
                    print(f"[*] 用户确认覆盖（{gate['existing_word_count']} → "
                          f"{gate['new_word_count']} 字）：{gate['existing']['path']}")
                    _delete_article_with_images(existing_path)
                    result["data"]["_replaced_path"] = gate["existing"]["path"]
                    result["data"]["_replaced_name"] = existing_path.name

                # 命中原文件 live-source：标记为新版本
                if gate and gate.get("_version"):
                    old_version = gate["existing"]["version_number"]
                    print(f"[*] live-source 更新：v{old_version} → v{gate['_version']}")
                    result["data"]["liveSource"] = True
                    result["data"]["versionNumber"] = gate["_version"]
                    result["data"]["previousVersion"] = f"v{old_version}"
                    result["data"]["_old_user_region"] = _extract_user_region(
                        (VAULT / gate["existing"]["path"]).read_text(
                            encoding="utf-8", errors="replace"))

                # ===== 正式写盘 =====
                data = result["data"]
                # 本次跳过 OCR：透传到「图多字少」闸门（data 来自适配器，不含请求体字段）
                if skip_ocr:
                    data["ocr"] = False
                new_filename, new_content, new_wc = generate_card(data)

                # 覆盖场景：沿用旧文件名，保持 URL / 来源一致性
                final_name = data.get("_replaced_name") or new_filename
                new_filepath = ARTICLES_DIR / final_name
                # 文件名若被改写，正文中的自引用也替换（generate_card 内部可能引用文件名）
                if final_name != new_filename:
                    new_content = new_content.replace(new_filename, final_name)

                new_content, img_count, img_sf = download_images(new_content, new_filepath)
                new_content, img_note = _apply_image_note(new_content, img_sf)

                # live-source 新版本：注入旧人工区
                if data.get("_old_user_region"):
                    new_content = new_content.replace(
                        "<!-- clip:user:start -->\n## 我的笔记\n<!-- clip:user:end -->",
                        data["_old_user_region"])

                new_filepath.write_text(new_content, encoding="utf-8")
                if img_count > 0:
                    print(f"[+] 下载 {img_count} 张图片 → {new_filepath.stem}/")
                if img_note:
                    print(f"[!] {img_note}")

                result["success"] = True
                result["filename"] = new_filepath.name
                result["wordCount"] = new_wc
                result["filepath"] = str(new_filepath.relative_to(VAULT))
                result["imageCount"] = img_count
                result["imageWarning"] = img_note

                # 动态渲染嫌疑：服务端只拿到静态骨架，内容可能明显少于浏览器中的
                # 实际页面。把这个结论透传给调用方（飞书机器人据此提示改用浏览器剪藏）。
                if (data.get("extra") or {}).get("dynamic_render_suspect"):
                    result["dynamicRenderSuspect"] = True
                    result["dynamicRenderNote"] = (
                        (data.get("extra") or {}).get("dynamic_render_note") or
                        "该页面疑似依赖动态渲染，服务端获取的内容可能不完整，"
                        "建议在浏览器中打开后再剪藏。")

                # wiki 放在最后：失败会撤回 raw，那时不该再回报一个不存在的文件
                result["wiki"] = _maybe_queue_wiki_gen(
                    new_filepath, data, sync=sync_wiki, task_id=fetch_task_id)
                # 响应里带上任务 id：wiki / OCR 是**异步**加工时，调用方要靠它
                # 轮询到最终结论并补发通知（否则「⏳ 后台生成中」永远没有下文）。
                result["taskId"] = fetch_task_id
                # wiki 页面路径（同步生成时立刻可读；异步时为空，落定后由
                # _settle_task_async 回写）—— 供「Wiki：<文件名>」展示。
                result["wikiFile"] = _wiki_ref_for(new_filepath)
                _settle_inbox_pointer(data, result["wiki"], origin_url=url)
                if result["wiki"].get("status") == "demoted":
                    result["filename"] = ""
                    result["filepath"] = ""
                    result["imageCount"] = 0
                    result["imageWarning"] = ""
                if data.get("_replaced_path"):
                    result["replaced"] = True
                    result["previous_file"] = data["_replaced_path"]
                if data.get("previousVersion"):
                    result["version"] = f"v{data['versionNumber']}"
                    result["previous_file"] = gate["existing"]["path"]

                task_queue.complete_task(fetch_task_id, {
                    "filename": result.get("filename"),
                    "wordCount": result.get("wordCount"),
                    "filepath": result.get("filepath"),
                    "wiki": result.get("wiki") or {},
                    # 后台还在加工（wiki / OCR）→ 标出来，供轮询方判断「还没落定」。
                    # 加工结束后由 _settle_task_async() 回写真实结论并清掉该标记；
                    # 不标的话调用方会把「⏳ 后台生成中」当成终态，永远等不到下文。
                    "asyncPending": (result.get("wiki") or {}).get("status") == "pending",
                })
                respond(result)
                return
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                # 客户端等待超时主动断开时，文件通常已落盘，不降级、不重复入队。
                # ConnectionAbortedError 是 Windows 上客户端断开最常见的表现
                # （WinError 10053），必须与另外两种同等对待 —— 否则会落进下面的
                # 通用分支，把一次**已经成功**的入库误判成「后端抓取失败」，进而
                # 错派给浏览器协助队列，用户收到的是一张莫名其妙的「⏳ 等待浏览器抓取」。
                # 实测 2026-10-14：小红书 5Qvl00mjnu0 已完整落盘（8 图 + wiki），
                # 只因回包阶段 WinError 10053 就被降级重抓。
                print(f"[i] 客户端已断开，跳过响应：{url[:80]}")
                task_queue.complete_task(fetch_task_id,
                    {"filename": result.get("filename"),
                     "wordCount": result.get("wordCount"),
                     "filepath": result.get("filepath"),
                     "wiki": result.get("wiki") or {},
                     "asyncPending": (result.get("wiki") or {}).get("status") == "pending"})
                return
            except NoBrowserFallback as e:
                # 适配器明确拒绝降级（目前只有飞书文档：缺 lark-cli / 授权失效 / 抓取失败）。
                # 浏览器降级产出的正文通常不完整，却会被当作完整剪藏写进 raw/articles/ ——
                # 用一份骗人的快照冒充入库，比明确失败更糟。所以这里**直接报错**：
                # 任务记为 failed（可在「任务队列 → 最近记录」查到原因），
                # 绝不把任务再丢进浏览器协助队列。
                reason = str(e) or "该平台不支持浏览器降级抓取"
                print(f"[!] 后端抓取无法由浏览器替代 [{platform.name}]: {reason}")
                task_queue.fail_task(fetch_task_id, reason)
                respond({
                    "success": False,
                    "error": reason,
                    "message": "该链接无法经浏览器协助降级抓取，本次未入库。",
                }, status=502)
                return
            except Exception as e:
                # 内容已落盘 → 失败只发生在回包/收尾环节，本次其实已经成功。
                # 不能降级重抓（否则同一内容会再走一遍浏览器协助、命中去重白跑）；
                # 文件已在 raw/articles 里、wiki 也已触发，按完成处理即可。
                if result.get("filename"):
                    print(f"[i] 内容已落盘，收尾失败（{e}），不降级、"
                          f"不重复入队：{url[:80]}")
                    task_queue.complete_task(fetch_task_id,
                        {"filename": result.get("filename"),
                         "wordCount": result.get("wordCount"),
                         "filepath": result.get("filepath"),
                         "wiki": result.get("wiki") or {},
                         "asyncPending":
                             (result.get("wiki") or {}).get("status") == "pending"})
                    return
                # 该平台不允许浏览器降级 → 任何后端异常都**不能**滑进浏览器队列。
                # 2026-10-21 实测：飞书适配器缺一行 `import os`，抛出的 NameError
                # 不是 NoBrowserFallback，于是落进通用分支被 requeue 给浏览器，
                # 前端显示「等待浏览器抓取」—— 一个纯粹的代码错误伪装成了
                # 「换个通道再试就能好」。这类静默降级比直接失败更难排查。
                if platform.allows_browser_fallback is False:
                    reason = f"{platform.name} 后端抓取失败：{e}"
                    print(f"[!] 后端抓取失败 [{platform.name}]: {e}"
                          f"（该平台不降级浏览器，任务直接失败）")
                    task_queue.fail_task(fetch_task_id, reason)
                    respond({"success": False, "error": reason,
                             "message": "该平台只能走后端通道，本次未入库。"},
                            status=502)
                    return
                print(f"[!] 后端抓取失败 [{platform.name}]: {e}，降级到浏览器协助")
                task_queue.requeue_for_browser(fetch_task_id)

        # 视频平台（跳过了上面那段）/ 普通网页 / 后端抓取失败
        # → 走浏览器协助队列（先查重，避免重复入队）
        existing_task = task_queue.find_active_task(url)
        if existing_task:
            print(f"[=] 该链接已在队列中 [{existing_task}]: {url[:80]}")
            respond({
                "success": True,
                "taskId": existing_task,
                "via": "browser-pending",
                "message": "该链接已在浏览器协助队列中，等待处理",
                "statusUrl": f"/api/task/{existing_task}",
            }, status=202)
            return

        # 兜底：不允许浏览器降级的平台走到这里说明调度逻辑有漏，宁可直接失败
        # 也不要扔进一个永远抓不对的队列（用户只会看到无限「等待浏览器抓取」）。
        if platform is not None and platform.allows_browser_fallback is False:
            reason = f"{platform.name} 不支持浏览器协助抓取"
            print(f"[!] {reason}：{url[:80]}")
            respond({"success": False, "error": reason,
                     "message": "该平台只能走后端通道，本次未入库。"}, status=502)
            return

        task_id = task_queue.add_task(url, via=(via or "浏览器协助"))
        print(f"[=] 放入浏览器协助队列 [{task_id}]: {url[:80]}")

        respond({
            "success": True,
            "taskId": task_id,
            "via": "browser-pending",
            "message": "已加入浏览器协助队列，等待浏览器连接后抓取",
            "statusUrl": f"/api/task/{task_id}",
        }, status=202)

    def handle_browser_connect(self):
        """SSE 长连接：给浏览器端推送任务。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        origin = _cors_allowed_origin(self.headers.get("Origin", ""))
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()

        nl = "\n"

        # 连接成功事件
        welcome = json.dumps({
            "type": "connected",
            "pending": task_queue.get_pending_count()
        }, ensure_ascii=False)
        self._send_sse("info", welcome)

        print("[*] 浏览器已连接，等待任务...")

        # 浏览器每次重连都先把「发出去却迟迟没回传」的任务退回来重发。
        # 否则 SW 被回收那一刻的任务对浏览器而言就凭空消失了（详见该方法注释）。
        try:
            task_queue.redispatch_stale_dispatched()
        except Exception as e:
            print(f"[!] 重新派发无回传任务失败: {e}", file=sys.stderr)

        try:
            while True:
                task = task_queue.get_task(timeout=25)
                if task is None:
                    # 心跳
                    self.wfile.write((": heartbeat" + nl + nl).encode("utf-8"))
                    self.wfile.flush()
                    continue

                task_data = json.dumps({
                    "type": "task",
                    "taskId": task.id,
                    "url": task.url,
                }, ensure_ascii=False)
                try:
                    self._send_sse("task", task_data)
                except Exception:
                    # 发送失败的任务必须退回队列：get_task() 已把它从内存
                    # _pending 弹出、DB 停在 stage='waiting'，而重连恢复只捞
                    # stage='dispatched' 的 —— 不退回它就永远困死在队列里
                    # （2026-10-15 Linux 实测：扩展掉线重连十几次都领不到
                    # 任务，600 秒后被误判为「浏览器协助超时」）。
                    task_queue.requeue_task(task.id)
                    raise
                task_queue.update_stage(task.id, "dispatched")
                print(f"[->] 发送任务给浏览器 [{task.id}]: {task.url[:60]}")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # 含 WinError 10053（ConnectionAbortedError）—— 浏览器关页/切网时常见，
            # 属「正常断开」，不该按「连接异常」记。
            print("[*] 浏览器已断开连接")
        except Exception as e:
            print(f"[!] 浏览器连接异常: {e}")

    def _send_sse(self, event: str, data: str):
        nl = "\n"
        msg = "event: " + event + nl + "data: " + data + nl + nl
        self.wfile.write(msg.encode("utf-8"))
        self.wfile.flush()

    def handle_browser_result(self):
        """浏览器返回抓取结果。"""
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            data = json.loads(body)
            task_id = data.get("taskId", "")
            result_data = data.get("result", {})
            error = data.get("error", "")

            if not task_id:
                self.send_json({"success": False, "error": "缺少 taskId"}, status=400)
                return

            if error:
                task_queue.fail_task(task_id, error)
                print(f"[!] 浏览器任务失败 [{task_id}]: {error}")
            else:
                task_queue.update_stage(task_id, "saving")
                result_data["via"] = "浏览器协助"
                # 记下**任务最初指派的 URL**（= 用户想剪的那条）。
                # 抓取可能被重定向（典型：Google 验证码页），此后所有「建待审卡 /
                # 摘待审卡 / 去重」都应以原始 URL 为准，否则会指向那张拦截页。
                try:
                    _t0 = task_queue.get_task_status(task_id)
                    if isinstance(_t0, dict) and _t0.get("url"):
                        result_data["_origin_url"] = str(_t0["url"])
                except Exception:
                    pass
                # 计算稳定来源 ID
                if not result_data.get("source_id") and result_data.get("url"):
                    canonical = resolve_short_link(result_data["url"])
                    result_data["source_id"] = normalize_url(canonical)

                # ===== 去重闸门（写盘前）=====
                # 浏览器协助路径过去完全没查重，导致"前端报错但仍入库"。
                force = result_data.get("force", False)
                gate = _preflight_duplicate(
                    result_data.get("source_id", ""),
                    result_data.get("content", ""), force=force)
                if gate and gate.get("_status") == 409:
                    # 必须把 _preflight_duplicate 给出的**完整字段**转发回任务结果。
                    # 此前只挑了 success/error/existing_file 三项，于是飞书的
                    # 「浏览器协助」通知路径拿不到 canOverwrite / 字数 / wiki 状态，
                    # 只能渲染一张三行卡片 —— 用户看到的正是「已存在于知识库」
                    # 却**没有「强制覆盖」按钮」」。
                    dup_info = {
                        "success": False,
                        "error": "duplicate",
                        # 关键：必须带 duplicate 标记。前端 historyOutcome() 靠
                        # result.duplicate 识别「命中去重」，缺了它这条会被当成
                        # 一次正常入库渲染成「✅ 已入库」，与事实不符。
                        "duplicate": True,
                        "message": gate.get("message", ""),
                        "existing_file": gate.get("existing_file"),
                        "existingFile": gate.get("existing_file"),
                        "existing_word_count": gate.get("existing_word_count", 0),
                        "new_word_count": gate.get("new_word_count", 0),
                        "wordCount": gate.get("new_word_count", 0),
                        "canOverwrite": gate.get("canOverwrite", False),
                        # 「建议覆盖」与「可覆盖」是两件事：前者说明本次正文更多，
                        # 飞书/面板据此提示用户——但覆盖仍需用户点按钮确认。
                        "newer_more_complete": gate.get("newer_more_complete", False),
                        "has_wiki": gate.get("has_wiki", False),
                        "existing_wiki": gate.get("existing_wiki", ""),
                    }
                    task_queue.complete_duplicate_task(task_id, dup_info)
                    print(f"[*] 浏览器结果重复，跳过入库：{gate.get('existing_file')}")
                    # 用任务原始 URL 摘卡片：抓取可能被重定向，result_data["url"]
                    # 未必等于用户最初发的链接。
                    try:
                        _t = task_queue.get_task_status(task_id)
                        _ou = str((_t or {}).get("url") or "") if isinstance(_t, dict) else ""
                    except Exception:
                        _ou = ""
                    _inbox_clear_for_url(_ou or str(result_data.get("url") or "").strip())
                    gate.pop("_status", None)
                    self.send_json(gate, status=409)
                    return
                if gate and gate.get("_overwrite"):
                    _delete_article_with_images(VAULT / gate["existing"]["path"])
                    result_data["_replaced"] = True
                    result_data["_replaced_path"] = gate["existing"]["path"]
                elif gate and gate.get("_version"):
                    result_data["liveSource"] = True
                    result_data["versionNumber"] = gate["_version"]
                    result_data["previousVersion"] = f"v{gate['existing']['version_number']}"

                filename, content, wc = generate_card(result_data)
                filepath = ARTICLES_DIR / filename

                # 下载图片到同名子目录
                content, img_count, img_sf = download_images(content, filepath)
                content, img_note = _apply_image_note(content, img_sf)
                if img_count > 0:
                    print(f"[+] 下载 {img_count} 张图片 → {filepath.stem}/")
                if img_note:
                    print(f"[!] {img_note}")

                filepath.write_text(content, encoding="utf-8")

                # wiki 精加工。插件会带 wikiSync=true（入库=raw+wiki），
                # 于是这里同步等结论；失败则撤回 raw、转待审区。
                wiki = _maybe_queue_wiki_gen(
                    filepath, result_data, sync=bool(result_data.get("wikiSync")),
                    task_id=task_id)
                demoted = wiki.get("status") == "demoted"
                # 用**任务最初指派的 URL** 摘指针：抓取可能被重定向（如 Google
                # 验证码页），此时 result_data["url"] 已不是待审卡片里那条链接。
                _origin = ""
                try:
                    _t = task_queue.get_task_status(task_id)
                    if isinstance(_t, dict):
                        _origin = str(_t.get("url") or "")
                except Exception:
                    _origin = ""
                _settle_inbox_pointer(result_data, wiki, origin_url=_origin)

                result_info = {
                    "success": True,
                    "wordCount": wc,
                    "filename": "" if demoted else filename,
                    "filepath": "" if demoted else str(filepath.relative_to(VAULT)),
                    "via": "browser",
                    "imageCount": 0 if demoted else img_count,
                    "imageWarning": "" if demoted else img_note,
                    "wiki": wiki,
                    "wikiFile": "" if demoted else _wiki_ref_for(filepath),
                    # 后台还在加工（wiki / OCR）→ 让轮询方继续等落定（见 _settle_task_async）
                    "asyncPending": wiki.get("status") == "pending",
                }
                if result_data.get("_replaced"):
                    result_info["replaced"] = True
                    result_info["previous_file"] = result_data["_replaced_path"]
                task_queue.complete_task(task_id, result_info)
                print(f"[+] 浏览器任务完成 [{task_id}]: {filename}")

            self.send_json({"success": True})
        except Exception as e:
            print(f"[!] 处理浏览器结果失败: {e}", file=sys.stderr)
            self.send_json({"success": False, "error": str(e)}, status=500)

    def send_json(self, obj, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        origin = _cors_allowed_origin(self.headers.get("Origin", ""))
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Vary", "Origin")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def log_message(self, format, *args):
        pass


class VaultHTTPServer(ThreadingHTTPServer):
    """忽略「客户端中途断开」这类预期内的错误。

    背景：用户在弹窗里发起剪藏后又随手关掉弹窗，Chrome 会**中止**这次连接。
    这种中断可能发生在请求生命周期的任意时刻 —— 读请求行、读请求头、读请求体
    都可能撞上 ConnectionAbortedError（WinError 10053）。请求体那一段已由
    `ClipHandler._read_body` 静默处理；但发生在**更早**的请求行/请求头阶段时，
    控制权还在 socketserver 手里，它会打印一大段 traceback（见 handle_error）。
    那些堆栈会让人误以为服务端出故障，实际只是客户端走了。

    这里把这两类「客户端自己走了」的异常吞掉，其余异常仍按默认方式报出，
    以免掩盖真正的服务端 bug。
    """

    def handle_error(self, request, client_address):
        import sys as _sys
        exc = _sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError,
                            BrokenPipeError)):
            # 客户端主动断开，不属于服务端故障，仅留一行日志便于排查。
            print("[i] 客户端中途断开连接（通常是在请求完成前关闭了页面）")
            return
        super().handle_error(request, client_address)


def main():
    parser = argparse.ArgumentParser(description="知识 Vault 剪藏接收服务")
    parser.add_argument("--port", type=int, default=7878, help="监听端口 (默认 7878)")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址 (默认 127.0.0.1)")
    args = parser.parse_args()

    # 非回环监听（如 0.0.0.0）必须配置 token，本机以外网络不可信
    is_loopback = args.host in {"127.0.0.1", "localhost", "::1"}
    if not is_loopback:
        try:
            is_loopback = ipaddress.ip_address(args.host).is_loopback
        except ValueError:
            is_loopback = False
    if not SERVER_TOKEN and not is_loopback:
        sys.exit(
            "❌ 拒绝启动：监听非回环地址时必须启用鉴权。\n"
            "   在 tools/config.local.json 配置 token，或设置环境变量 "
            "CLIP_SERVER_TOKEN（生成：python3 -c 'import secrets; print(secrets.token_urlsafe(32))'）"
        )

    server = VaultHTTPServer((args.host, args.port), ClipHandler)
    print("📚 知识 Vault 剪藏服务启动")
    print(f"   监听: http://{args.host}:{args.port}")
    print(f"   鉴权: {'已启用（token ' + str(len(SERVER_TOKEN)) + ' 字符）' if SERVER_TOKEN else '未启用（仅限本机回环调试，不建议）'}")
    print(f"   写入: {ARTICLES_DIR}")
    print(f"   平台: {', '.join(list_platforms())}")
    print(f"   浏览器协助: /api/browser/connect (SSE)")
    print(f"   按 Ctrl+C 停止")
    print()

    # 定时清扫卡死任务。此前清扫只在「同一 URL 再次提交」时惰性触发，
    # 导致等待浏览器接入的任务会永远停在「进行中」—— 面板显示在忙，
    # 实际什么都没发生，用户只能干等。
    task_queue.start_sweeper()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
        server.server_close()


if __name__ == "__main__":
    main()

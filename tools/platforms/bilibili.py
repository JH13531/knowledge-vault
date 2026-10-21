"""B 站适配器：yt-dlp + 浏览器 Cookie 下载字幕 + 元数据。

优先使用浏览器 Cookie 绕过 B 站 412 拦截，
下载 AI 字幕（ai-zh）→ 解析为带时间点的 Markdown。

⚠️ **当前实际不被调用（备用适配器）**：`clip-server.py` 的 `_prefer_browser()`
把 `bilibili.com` / `b23.tv` 主机名写死进浏览器协助队列，路由层根本不会走到
这个适配器 —— B 站字幕改由扩展在页面内调 WBI 签名接口取（跨平台一致）。
本文件仅作为「将来若要在服务端跑 yt-dlp」的备用实现保留，**别把它当成生效的抓取通道**。
（Linux 上 yt-dlp 本可读明文 cookie，是路由选择统一走浏览器，不是技术不可行。）
"""
from __future__ import annotations
import json
import os
import re
import shutil
import sys
import subprocess
import tempfile
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from no_window import NO_WINDOW
from .base import PlatformAdapter, ClipResult


# yt-dlp 浏览器 Cookie 来源：Linux(snap) 需显式 profile 路径；
# Windows / macOS 给浏览器名即可，yt-dlp 按平台默认位置查找
if sys.platform.startswith("linux"):
    _snap_profile = os.path.expanduser("~/snap/chromium/common/chromium")
    _BROWSER_COOKIE_SPEC = (
        f"chromium:{_snap_profile}" if os.path.isdir(_snap_profile) else "chromium"
    )
elif sys.platform == "darwin":
    _BROWSER_COOKIE_SPEC = "chrome" if os.path.isdir(
        os.path.expanduser("~/Library/Application Support/Google/Chrome")) else "edge"
else:  # win32 及其他
    _BROWSER_COOKIE_SPEC = "chrome" if os.path.isdir(
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data")) else "edge"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://www.bilibili.com",
}

# yt-dlp 字幕语言优先级（B站 AI 字幕优先）
_SUB_LANGS = ["ai-zh", "zh-Hans", "zh-CN", "zh"]


class BilibiliAdapter(PlatformAdapter):
    name = "Bilibili"
    priority = 30

    @classmethod
    def match(cls, url: str) -> bool:
        try:
            domain = urlparse(url).netloc
            return domain in ("bilibili.com", "www.bilibili.com", "m.bilibili.com") \
                or "bilibili.com" in domain or "b23.tv" in domain
        except Exception:
            return False

    @classmethod
    def extract(cls, url: str) -> ClipResult:
        info = None
        meta_err = ""
        try:
            info = cls._get_video_info(url)
        except Exception as e:
            # yt-dlp 失败，尝试网页爬取。
            # **错误不能丢**：字幕下载用的是**同一套 cookie 参数**，
            # yt-dlp 连元数据都读不到时，字幕几乎必然也读不到 ——
            # 这句就是「为什么没字幕」的答案，下面会并进 caption_note。
            meta_err = str(e)[:200]
            info = cls._web_scrape(url)

        if info is None:
            raise Exception(
                "B 站后端抓取失败（yt-dlp 与网页爬取均被拦截），"
                "将改用浏览器扩展从页面读取。"
                + (f"yt-dlp 报错：{meta_err}" if meta_err else "")
            )

        title = info.get("title", "Bilibili 视频")
        author = info.get("uploader", "") or info.get("channel", "") or info.get("author", "")
        upload_date = info.get("upload_date", "")
        if upload_date and len(upload_date) == 8:
            upload_date = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}"

        duration = info.get("duration_string", "") or str(info.get("duration", ""))
        description = info.get("description", "")
        bv_id = info.get("bv_id", info.get("bvid", ""))

        # 下载字幕（带浏览器 Cookie）
        subtitle_text, subtitle_lang, sub_err = cls._download_subtitles(url)

        # 没下到字幕时，把**真实原因**拼出来。
        # 优先用字幕下载自己的报错；它为空（yt-dlp 没报错，只是没产出文件）时
        # 退到元数据阶段的错误 —— 那通常才是根因（cookie 读不到 / 被风控）。
        sub_reason = ""
        if not subtitle_text:
            reasons = []
            if sub_err:
                reasons.append(sub_err)
            if meta_err and meta_err != sub_err:
                reasons.append("抓元数据时的同一错误：" + meta_err)
            sub_reason = "；".join(reasons[:2]) or \
                "yt-dlp 没有产出字幕文件（该视频可能确实没有可用字幕）"

        # 组装内容
        content_parts = []
        content_parts.append(f"# {title}\n")
        content_parts.append(f"> **UP主：** {author}")
        if bv_id:
            content_parts.append(f"> **BV号：** {bv_id}")
        if upload_date:
            content_parts.append(f"> **发布时间：** {upload_date}")
        if duration:
            content_parts.append(f"> **时长：** {duration}")
        content_parts.append(f"> **链接：** {url}")
        content_parts.append("")

        if description:
            content_parts.append("## 视频简介\n")
            content_parts.append(description.strip())
            content_parts.append("")

        if subtitle_text:
            content_parts.append(f"## 字幕（{subtitle_lang}）\n")
            content_parts.append(subtitle_text.strip())
            content_parts.append("")
        else:
            content_parts.append(f"> ⚠️ 未获取到字幕：{sub_reason}")
            content_parts.append("")

        content = "\n".join(content_parts)

        # 标签
        tags = ["Bilibili", "视频"]
        categories = info.get("categories", [])
        if categories:
            tags.extend(categories[:3])

        return ClipResult(
            title=title,
            url=url,
            author=author,
            published_at=upload_date,
            content=content,
            type="video",
            platform="Bilibili",
            tags=tags,
            duration=duration,
            extra={
                "bvid": bv_id,
                "has_subtitles": bool(subtitle_text),
                "subtitle_lang": subtitle_lang,
                # 供 _content_incomplete_reason() 拼给用户看的原因（不传就只剩一句
                # 笼统的「视频未取到字幕/转录」，排查时什么都看不出来）
                "caption_note": sub_reason,
            },
        )

    @classmethod
    def _yt_dlp_base_args(cls) -> list[str]:
        """构建 yt-dlp 基础参数（带浏览器 Cookie）。"""
        yt_dlp_bin = shutil.which("yt-dlp") or "yt-dlp"
        args = [
            yt_dlp_bin,
            "--cookies-from-browser", _BROWSER_COOKIE_SPEC,
            "--no-update",
        ]
        return args

    @classmethod
    def _get_video_info(cls, url: str) -> dict:
        """用 yt-dlp + 浏览器 Cookie 获取视频元数据。"""
        args = cls._yt_dlp_base_args() + ["-J", "--skip-download", url]

        try:
            result = subprocess.run(
                args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                creationflags=NO_WINDOW,
            )
        except subprocess.TimeoutExpired:
            raise Exception("yt-dlp 超时（60秒）")

        if result.returncode != 0:
            stderr = result.stderr.strip()[:300] if result.stderr else ""
            raise Exception(f"yt-dlp 失败 (exit {result.returncode}): {stderr}")

        info = json.loads(result.stdout)
        if info is None:
            raise Exception("yt-dlp 返回空数据（B站可能拦截了请求）")
        return info

    @classmethod
    def _download_subtitles(cls, url: str) -> tuple[str, str, str]:
        """下载字幕，返回 (字幕文本, 语言标签, 失败原因)。

        优先级：ai-zh > zh-Hans > zh-CN > zh

        **为什么要把失败原因也返回**：原来这里把每次尝试的异常都 `continue` 吞掉，
        全部语言都失败时只回 `("", "")` —— 于是「字幕没下到」这件事在整条链路上
        **完全没有原因**，服务端只能给用户一句「视频未取到字幕/转录」。
        而实际情况差别很大、处置也完全不同：
          - 该视频确实没有字幕（UP 没传、平台也没生成 AI 字幕）→ 重试无用
          - `--cookies-from-browser` 读不到 cookie（Windows 上 Chrome 的
            App-Bound Encryption 会让它读到空库）→ 登录态问题，换扩展路径即可
          - yt-dlp 被 B 站 412 / 风控拦 → 换通道
        所以把 yt-dlp 的 stderr 摘要带出去。
        """
        base_args = cls._yt_dlp_base_args()
        notes: list[str] = []

        with tempfile.TemporaryDirectory() as tmpdir:
            for lang in _SUB_LANGS:
                try:
                    args = base_args + [
                        "--write-subs",
                        "--sub-lang", lang,
                        "--skip-download",
                        "--sub-format", "vtt/srt/best",
                        "-o", f"{tmpdir}/sub.%(ext)s",
                        url,
                    ]
                    result = subprocess.run(
                        args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                        creationflags=NO_WINDOW,
                    )

                    # 扫描下载的字幕文件
                    for f in os.listdir(tmpdir):
                        fpath = os.path.join(tmpdir, f)
                        if f.endswith(".vtt"):
                            return cls._parse_subtitle(fpath), lang, ""
                        elif f.endswith(".srt"):
                            return cls._parse_subtitle(fpath), lang, ""
                        elif f.endswith(".json3"):
                            return cls._parse_json3(fpath), lang, ""

                    # 没产出文件 → 记下 yt-dlp 的说法（这句才是真正的原因）
                    err = (result.stderr or "").strip()
                    if err:
                        key = err.splitlines()[-1].strip()[:200]
                        if key and key not in notes:
                            notes.append(key)
                except subprocess.TimeoutExpired:
                    notes.append(f"yt-dlp 下载 {lang} 字幕超时（60秒）")
                except Exception as e:
                    notes.append(str(e)[:200])

        # 只留少量、去重后的行，别把 note 撑爆
        return "", "", "；".join(notes[-3:])

    @classmethod
    def _parse_subtitle(cls, filepath: str) -> str:
        """解析 VTT/SRT 字幕，保留时间点。"""
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        lines = []
        current_time = ""
        current_text = ""

        for line in content.split("\n"):
            line = line.strip()
            if not line:
                if current_text:
                    # 保留时间点：[HH:MM:SS] 内容
                    lines.append(f"[{current_time}] {current_text}")
                    current_time = ""
                    current_text = ""
                continue
            if line.startswith("WEBVTT") or line.startswith("NOTE") or line.startswith("Kind:") or line.startswith("Language:"):
                continue
            if re.match(r"^\d+$", line):
                continue
            if "-->" in line:
                # 提取时间点 HH:MM:SS
                m = re.match(r"(\d{2}:\d{2}:\d{2})[.,]\d+\s*-->", line)
                if m:
                    current_time = m.group(1)
                continue
            line = re.sub(r"<[^>]+>", "", line)
            if line:
                current_text += " " + line if current_text else line

        if current_text:
            lines.append(f"[{current_time}] {current_text}")

        return "\n".join(lines)

    @classmethod
    def _parse_json3(cls, filepath: str) -> str:
        """解析 B 站 json3 字幕格式。"""
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            lines = []
            for event in data.get("body", []):
                text = event.get("content", "").strip()
                start = event.get("from", 0)
                if text:
                    # 秒数转 HH:MM:SS
                    h = int(start // 3600)
                    m = int((start % 3600) // 60)
                    s = int(start % 60)
                    ts = f"{h:02d}:{m:02d}:{s:02d}"
                    lines.append(f"[{ts}] {text}")
            return "\n".join(lines)
        except Exception:
            return ""

    @classmethod
    def _web_scrape(cls, url: str) -> dict | None:
        """网页爬取 B 站页面元数据（yt-dlp 完全失败时的备用方案）。"""
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
            resp.raise_for_status()
        except Exception:
            return None

        if "wind-control" in resp.text or "412" in resp.text:
            return None

        html = resp.text
        soup = BeautifulSoup(html, "html.parser")

        # 方式1：__INITIAL_STATE__
        pattern = r"window\.__INITIAL_STATE__\s*=\s*({.*?});?\s*\(?function"
        match = re.search(pattern, html, re.DOTALL)
        if match:
            raw = match.group(1).replace("undefined", "null")
            try:
                state = json.loads(raw)
                vdata = state.get("videoData", {})
                if vdata and vdata.get("title"):
                    return {
                        "title": vdata["title"],
                        "uploader": vdata.get("owner", {}).get("name", ""),
                        "description": vdata.get("desc", ""),
                        "bvid": vdata.get("bvid", ""),
                        "duration": vdata.get("duration", 0),
                        "upload_date": "",
                        "duration_string": "",
                        "categories": [],
                    }
            except json.JSONDecodeError:
                pass

        # 方式2：DOM 提取
        title_el = soup.find("h1", class_="video-title")
        if title_el:
            title = title_el.get_text(strip=True)
            if title:
                up_el = soup.find("a", class_="up-name") or soup.find("span", class_="up-name")
                return {
                    "title": title,
                    "uploader": up_el.get_text(strip=True) if up_el else "",
                    "description": "",
                    "bvid": "",
                    "duration": 0,
                    "upload_date": "",
                    "duration_string": "",
                    "categories": [],
                }

        return None

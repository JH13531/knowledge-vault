"""YouTube 适配器：yt-dlp 下载字幕 + 元数据。

⚠️ **当前实际不被调用（备用适配器）**：`clip-server.py` 的 `_prefer_browser()`
把 `youtube.com` / `youtu.be` 主机名写死进浏览器协助队列，路由层根本不会走到
这个适配器 —— YouTube 字幕改由扩展在视频页内走 InnerTube 取（PoToken 风控所致）。
本文件仅作为「将来若要在服务端跑 yt-dlp」的备用实现保留，**别把它当成生效的抓取通道**。
"""
from __future__ import annotations
import json
import re
import shutil
import subprocess
from urllib.parse import urlparse, parse_qs

from no_window import NO_WINDOW
from .base import PlatformAdapter, ClipResult

# 解析完整路径：Windows 上 yt-dlp 是 .exe，裸名不经 shell 找不到
_YT_DLP = shutil.which("yt-dlp") or "yt-dlp"


class YouTubeAdapter(PlatformAdapter):
    name = "YouTube"
    priority = 20

    @classmethod
    def match(cls, url: str) -> bool:
        try:
            domain = urlparse(url).netloc
            return domain in (
                "youtube.com", "www.youtube.com", "m.youtube.com",
                "youtu.be", "www.youtu.be"
            ) or "youtube.com" in domain
        except Exception:
            return False

    @classmethod
    def extract(cls, url: str) -> ClipResult:
        # 先用 yt-dlp 获取视频信息
        info = cls._get_video_info(url)
        title = info.get("title", "YouTube 视频")
        author = info.get("uploader", "")
        upload_date = info.get("upload_date", "")
        if upload_date and len(upload_date) == 8:
            # YYYYMMDD -> YYYY-MM-DD
            upload_date = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}"

        duration = info.get("duration_string", "")
        description = info.get("description", "")

        # 尝试下载字幕（中文优先，其次英文，然后自动生成）
        subtitle_text = cls._download_subtitles(url)

        # 没下到字幕时给用户一句**能照着做**的话。
        # 服务端这条路对 YouTube 基本不可靠：字幕接口已加 PoToken 风控，
        # yt-dlp 在后端拿不到；正确通道是浏览器扩展在视频页内走 InnerTube。
        sub_reason = "" if subtitle_text else (
            "服务端（yt-dlp）未能获取 YouTube 字幕。请改用浏览器扩展："
            "在视频页点击「立即完整入库」，扩展将通过页面内的 InnerTube 通道获取。"
        )

        # 组装内容
        content_parts = []
        content_parts.append(f"# {title}\n")
        content_parts.append(f"> **频道：** {author}")
        content_parts.append(f"> **发布时间：** {upload_date}")
        content_parts.append(f"> **时长：** {duration}")
        content_parts.append(f"> **链接：** {url}\n")

        if description:
            content_parts.append("## 视频简介\n")
            content_parts.append(description.strip())
            content_parts.append("")

        if subtitle_text:
            content_parts.append("## 字幕\n")
            content_parts.append(subtitle_text.strip())
            content_parts.append("")
        else:
            content_parts.append(f"> ⚠️ 未获取到字幕：{sub_reason}")
            content_parts.append("")

        content = "\n".join(content_parts)

        # 标签
        tags = ["YouTube", "视频"]
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
            platform="YouTube",
            tags=tags,
            duration=duration,
            extra={
                "has_subtitles": bool(subtitle_text),
                "caption_note": sub_reason,
            },
        )

    @classmethod
    def _get_video_info(cls, url: str) -> dict:
        """获取视频元数据。"""
        try:
            result = subprocess.run(
                [_YT_DLP, "-J", "--skip-download", url],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
                creationflags=NO_WINDOW,
            )
            return json.loads(result.stdout)
        except Exception as e:
            raise Exception(f"获取 YouTube 视频信息失败: {e}")

    @classmethod
    def _download_subtitles(cls, url: str) -> str:
        """下载字幕，优先中文，其次英文，再其次自动生成。"""
        import tempfile
        import os

        with tempfile.TemporaryDirectory() as tmpdir:
            # 尝试下载字幕（手动字幕 + 自动字幕都试）
            langs_to_try = [
                ["zh-CN", "zh-Hans", "zh", "zh-CN.auto"],  # 中文
                ["en", "en-US", "en-GB", "en.auto"],        # 英文
            ]

            for lang_list in langs_to_try:
                for lang in lang_list:
                    try:
                        result = subprocess.run(
                            [_YT_DLP, "--write-subs", "--sub-lang", lang,
                             "--skip-download", "--sub-format", "vtt",
                             "-o", f"{tmpdir}/sub.%(ext)s", url],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
                            creationflags=NO_WINDOW,
                        )
                        # 找生成的字幕文件
                        for f in os.listdir(tmpdir):
                            if f.endswith(".vtt") or f.endswith(".srt"):
                                filepath = os.path.join(tmpdir, f)
                                return cls._parse_subtitle(filepath)
                    except Exception:
                        continue

            # 都没成功，试试列出所有字幕格式
            try:
                result = subprocess.run(
                    [_YT_DLP, "--list-subs", url],
                    capture_output=True, text=True, timeout=15,
                    creationflags=NO_WINDOW,
                )
                # 尝试第一个可用的自动字幕
                lines = result.stdout.split("\n")
                for line in lines:
                    line = line.strip()
                    if line.startswith("zh") or line.startswith("en"):
                        lang = line.split()[0]
                        try:
                            result2 = subprocess.run(
                                [_YT_DLP, "--write-auto-subs", "--sub-lang", lang,
                                 "--skip-download", "--sub-format", "vtt",
                                 "-o", f"{tmpdir}/sub.%(ext)s", url],
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
                                creationflags=NO_WINDOW,
                            )
                            for f in os.listdir(tmpdir):
                                if f.endswith(".vtt") or f.endswith(".srt"):
                                    filepath = os.path.join(tmpdir, f)
                                    return cls._parse_subtitle(filepath)
                        except Exception:
                            pass
                        break
            except Exception:
                pass

        # yt-dlp 全部失败，降级到 youtube-transcript-api
        try:
            return cls._download_via_transcript_api(url)
        except Exception:
            return ""

    @classmethod
    def _parse_subtitle(cls, filepath: str) -> str:
        """把 VTT/SRT 字幕转成纯文本。"""
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        lines = []
        current_text = ""

        for line in content.split("\n"):
            line = line.strip()
            # 跳过元数据行
            if not line:
                if current_text:
                    lines.append(current_text)
                    current_text = ""
                continue
            if line.startswith("WEBVTT") or line.startswith("NOTE"):
                continue
            if "-->" in line:
                continue
            if re.match(r"^\d+$", line):  # SRT 序号
                continue
            # 去掉 HTML 标签
            line = re.sub(r"<[^>]+>", "", line)
            if line:
                current_text += " " + line if current_text else line

        if current_text:
            lines.append(current_text)

        return "\n".join(lines)

    @classmethod
    def _extract_video_id(cls, url: str) -> str:
        """从 YouTube URL 中提取视频 ID。"""
        parsed = urlparse(url)
        # youtu.be/VIDEO_ID
        if "youtu.be" in parsed.netloc:
            return parsed.path.lstrip("/")
        # youtube.com/watch?v=VIDEO_ID
        if "v" in parse_qs(parsed.query):
            return parse_qs(parsed.query)["v"][0]
        # youtube.com/embed/VIDEO_ID or /v/VIDEO_ID
        path_parts = parsed.path.split("/")
        for prefix in ("embed", "v", "shorts"):
            if prefix in path_parts:
                idx = path_parts.index(prefix)
                if idx + 1 < len(path_parts):
                    return path_parts[idx + 1]
        return ""

    @classmethod
    def _download_via_transcript_api(cls, url: str) -> str:
        """降级方案：用 youtube-transcript-api 获取字幕。

        当 yt-dlp 无法获取字幕时使用，支持自动生成的字幕。
        """
        try:
            from youtube_transcript_api import YouTubeTranscriptApi
        except ImportError:
            print("[youtube] youtube-transcript-api 未安装，跳过降级")
            return ""

        video_id = cls._extract_video_id(url)
        if not video_id:
            return ""

        try:
            api = YouTubeTranscriptApi()
            # 优先中文，其次英文（含自动生成的）
            transcript = api.fetch(video_id, languages=["zh-CN", "zh", "en"])

            lines = []
            for entry in transcript:
                # FetchedTranscript 是 iterable，每个 entry 有 text/start/duration
                start_seconds = int(entry.start)
                minutes = start_seconds // 60
                seconds = start_seconds % 60
                timestamp = f"[{minutes:02d}:{seconds:02d}]"
                text = entry.text.strip()
                if text:
                    lines.append(f"{timestamp} {text}")

            if lines:
                print(f"[youtube] 通过 youtube-transcript-api 获取 {len(lines)} 条字幕")
                return "\n".join(lines)
        except Exception as e:
            print(f"[youtube] youtube-transcript-api 失败: {e}")

        return ""

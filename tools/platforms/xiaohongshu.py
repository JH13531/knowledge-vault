"""小红书适配器：网页爬取笔记内容。

小红书笔记页初始 HTML 包含 window.__INITIAL_STATE__ JSON，
其中 state.note.noteDetailMap[noteId].note 包含笔记完整数据。
无需登录即可读取公开笔记。
"""
from __future__ import annotations
import json
import re
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from .base import PlatformAdapter, ClipResult


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


class XiaohongshuAdapter(PlatformAdapter):
    name = "小红书"
    priority = 25

    @classmethod
    def match(cls, url: str) -> bool:
        try:
            domain = urlparse(url).netloc
            return (
                "xiaohongshu.com" in domain
                or "xhslink.com" in domain
                or "xhslink.cn" in domain
                or "xhs.cn" in domain
            )
        except Exception:
            return False

    @classmethod
    def extract(cls, url: str) -> ClipResult:
        # 1. 解析短链 → 真实URL
        real_url = cls._resolve_url(url)

        # 2. 获取页面 HTML
        resp = requests.get(real_url, headers=HEADERS, timeout=15, allow_redirects=True)
        resp.raise_for_status()

        # 3. 提取 __INITIAL_STATE__ JSON
        state = cls._extract_initial_state(resp.text)
        if not state:
            raise Exception("无法解析小红书页面数据（可能需要登录态）")

        # 4. 解析笔记数据（嵌套在 noteDetailMap[id].note 中）
        note = cls._find_note(state)
        if not note:
            raise Exception("未找到笔记内容")

        # 小红书 JSON 使用 camelCase 键名
        title = note.get("title", "") or "小红书笔记"
        author = note.get("user", {}).get("nickname", "")
        desc = note.get("desc", "")
        interact_info = note.get("interactInfo", {})
        liked_count = interact_info.get("likedCount", "")

        # 笔记类型
        note_type = note.get("type", "normal")
        is_video = note_type == "video"

        # 图片列表（camelCase: imageList → urlDefault）
        image_list = note.get("imageList", [])
        images = []
        for img in image_list:
            url_default = img.get("urlDefault", "") or img.get("url", "")
            if url_default:
                # 去掉签名参数（过期链接），保留基础URL
                clean_url = re.sub(r"\?[^ ]*", "", url_default)
                images.append(clean_url)

        # 视频地址
        video_url = ""
        if is_video:
            video = note.get("video", {})
            media = video.get("media", {})
            stream = media.get("stream", {})
            for quality in ["h264", "h265"]:
                streams = stream.get(quality, [])
                if streams:
                    video_url = streams[0].get("masterUrl", "")
                    break
            if not video_url:
                consumer = video.get("consumer", {})
                video_url = consumer.get("originVideoKey", "")

        # 5. 组装 Markdown
        parts = []
        parts.append(f"# {title}\n")
        parts.append(f"> **作者：** {author}")
        if liked_count:
            parts.append(f"> **点赞：** {liked_count}")
        parts.append(f"> **链接：** {real_url}")
        parts.append("")

        if desc:
            parts.append("## 正文\n")
            parts.append(desc.strip())
            parts.append("")

        if images:
            parts.append("## 图片\n")
            for i, img_url in enumerate(images, 1):
                parts.append(f"![图片{i}]({img_url})")
            parts.append("")

        if video_url:
            parts.append("## 视频\n")
            parts.append(f"[视频链接]({video_url})")
            parts.append("")

        if not desc and not images and not video_url:
            parts.append("> ️ 未获取到正文内容，可能需要登录态。")
            parts.append("")

        content = "\n".join(parts)

        # 标签（camelCase: tagList → name）
        tags = ["小红书", "视频" if is_video else "图文"]
        tag_list = note.get("tagList", [])
        for tag in tag_list[:5]:
            tag_name = tag.get("name", "")
            if tag_name:
                tags.append(tag_name)

        return ClipResult(
            title=title,
            url=real_url,
            author=author,
            published_at="",
            content=content,
            type="video" if is_video else "article",
            platform="小红书",
            tags=tags,
            extra={
                "note_id": note.get("noteId", ""),
                "image_count": len(images),
                "has_video": is_video,
            },
        )

    @classmethod
    def _resolve_url(cls, url: str) -> str:
        """解析短链，获取真实笔记页URL。"""
        try:
            resp = requests.head(
                url, headers=HEADERS, timeout=10, allow_redirects=True
            )
            return resp.url
        except Exception:
            return url

    @classmethod
    def _extract_initial_state(cls, html: str) -> dict | None:
        """从页面 HTML 提取 __INITIAL_STATE__ JSON。"""
        # 方式1：正则匹配
        pattern = r"window\.__INITIAL_STATE__\s*=\s*({.*?})\s*</script>"
        match = re.search(pattern, html, re.DOTALL)
        if match:
            raw = match.group(1)
            # 小红书用 undefined 替代 null
            raw = raw.replace("undefined", "null")
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                pass

        # 方式2：BeautifulSoup 找 script 标签
        try:
            soup = BeautifulSoup(html, "html.parser")
            for script in soup.find_all("script"):
                text = script.string or ""
                if "__INITIAL_STATE__" in text:
                    match2 = re.search(r"=\s*({.*})", text, re.DOTALL)
                    if match2:
                        raw = match2.group(1).rstrip(";").strip()
                        raw = raw.replace("undefined", "null")
                        try:
                            return json.loads(raw)
                        except json.JSONDecodeError:
                            pass
        except Exception:
            pass

        return None

    @classmethod
    def _find_note(cls, state: dict) -> dict | None:
        """从 __INITIAL_STATE__ 中找到笔记数据。

        数据结构：state.note.noteDetailMap[noteId].note
        noteDetailMap 的每个值是包装对象，包含 comments/note/currentTime 等键，
        实际笔记数据在 .note 键内。
        """
        note_section = state.get("note", {})
        detail_map = note_section.get("noteDetailMap", {})
        if not detail_map:
            return None

        # 优先用 currentNoteId / firstNoteId
        note_id = note_section.get("currentNoteId", "") or note_section.get("firstNoteId", "")

        wrapper = None
        if note_id and note_id in detail_map:
            wrapper = detail_map[note_id]
        else:
            # 取第一个非空值
            for key, val in detail_map.items():
                if key and isinstance(val, dict):
                    wrapper = val
                    break

        if not wrapper:
            return None

        # 解包：wrapper.note 才是真正的笔记数据
        if isinstance(wrapper, dict):
            if "note" in wrapper and isinstance(wrapper["note"], dict):
                return wrapper["note"]
            # 兼容：如果 wrapper 本身就有 title 键（直接就是笔记）
            if "title" in wrapper:
                return wrapper

        return None

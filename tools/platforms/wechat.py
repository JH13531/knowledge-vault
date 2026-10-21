"""微信公众号适配器。

URL 格式差异：
- /s/xxxx 短链（公开分享链接）：可以直接抓取
- ?__biz=...&mid=...&sn=... 原链：触发验证码，需通过浏览器扩展读取

当短链抓取失败（被重定向到验证码页）时，降级到浏览器扩展通道。
"""
from __future__ import annotations
import re
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from .base import PlatformAdapter, ClipResult


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


class WechatAdapter(PlatformAdapter):
    name = "微信公众号"
    priority = 20

    @classmethod
    def match(cls, url: str) -> bool:
        try:
            return "mp.weixin.qq.com" in urlparse(url).netloc
        except Exception:
            return False

    @classmethod
    def extract(cls, url: str) -> ClipResult:
        """尝试抓取，成功则返回 ClipResult，被验证码拦截则抛异常降级到浏览器。"""
        resp = requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
        resp.raise_for_status()

        # 检测是否被重定向到验证码页
        if "wappoc_appmsgcaptcha" in resp.url or "环境异常" in resp.text:
            raise Exception(
                "微信验证码拦截（原链格式 ?__biz= 触发），"
                "降级到浏览器扩展从 DOM 读取。"
                "如为 /s/ 短链格式通常可直接抓取。"
            )

        soup = BeautifulSoup(resp.text, "html.parser")

        # 标题
        title = ""
        title_el = soup.find("h1", class_="rich_media_title")
        if title_el:
            title = title_el.get_text(strip=True)
        if not title:
            title = soup.get_text("title") or "微信文章"

        # 公众号名称
        account = ""
        for sel in ["#js_name", "a#js_name", ".rich_media_meta_nickname"]:
            el = soup.select_one(sel)
            if el:
                account = el.get_text(strip=True)
                if account:
                    break

        # 发布时间
        pub_time = ""
        pub_el = soup.select_one("#publish_time")
        if pub_el:
            pub_time = pub_el.get_text(strip=True)

        # 正文
        content_el = soup.find("div", id="js_content")
        if not content_el:
            raise Exception("未找到 #js_content 正文区域，可能页面结构变化")

        # 提取图片（微信使用 data-src 懒加载）
        images = []
        for img in content_el.find_all("img"):
            src = img.get("data-src") or img.get("src") or ""
            if src.startswith("http") and "mmbiz.qpic.cn" in src:
                # 去掉压缩参数
                clean = re.sub(r"\?wx_fmt=\w+$", "", src)
                images.append(clean)

        # 标签
        tags = []
        for el in soup.select("#js_tags .tag_name, .tag_name"):
            t = el.get_text(strip=True)
            if t:
                tags.append(t)

        # 正文 Markdown（保留结构）
        text_lines = []
        for child in content_el.children:
            if hasattr(child, "name"):
                if child.name in ("h1", "h2", "h3", "h4"):
                    level = int(child.name[1])
                    text_lines.append("\n" + "#" * level + " " + child.get_text(strip=True) + "\n")
                elif child.name == "p":
                    t = child.get_text(strip=True)
                    if t:
                        text_lines.append(t + "\n")
                elif child.name == "section":
                    t = child.get_text(strip=True)
                    if t:
                        text_lines.append(t + "\n")
                elif child.name == "img":
                    pass  # 图片单独处理
                elif child.name == "blockquote":
                    t = child.get_text(strip=True)
                    if t:
                        text_lines.append("> " + t + "\n")
                elif child.name == "ul":
                    for li in child.find_all("li"):
                        t = li.get_text(strip=True)
                        if t:
                            text_lines.append("- " + t + "\n")
                elif child.name == "ol":
                    for i, li in enumerate(child.find_all("li"), 1):
                        t = li.get_text(strip=True)
                        if t:
                            text_lines.append(f"{i}. {t}\n")
                elif child.name == "pre":
                    code = child.get_text()
                    if code.strip():
                        text_lines.append("```\n" + code.strip() + "\n```\n")
            elif hasattr(child, "strip") and child.strip():
                text_lines.append(child.strip() + "\n")

        content_md = "\n".join(text_lines).strip()
        if not content_md:
            # 回退：纯文本
            content_md = content_el.get_text("\n").strip()

        # 组装 Markdown
        parts = []
        parts.append(f"# {title}\n")
        if account:
            parts.append(f"> **公众号：** {account}")
        if pub_time:
            parts.append(f"> **发布时间：** {pub_time}")
        parts.append(f"> **链接：** {url}")
        parts.append("")

        if tags:
            parts.append("**标签：** " + " ".join(f"#{t}" for t in tags))
            parts.append("")

        parts.append("## 正文\n")
        parts.append(content_md)
        parts.append("")

        if images:
            parts.append("## 图片\n")
            for i, img_url in enumerate(images, 1):
                parts.append(f"![图片{i}]({img_url})")
            parts.append("")

        return ClipResult(
            title=title,
            url=url,
            author=account,
            published_at=pub_time,
            content="\n".join(parts),
            type="article",
            platform="微信公众号",
            tags=["微信公众号", "微信"] + tags,
            extra={"image_count": len(images)},
        )

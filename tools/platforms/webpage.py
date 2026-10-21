"""普通网页适配器：trafilatura 正文提取。"""
from __future__ import annotations
import re
from datetime import datetime
from urllib.parse import urlparse

import trafilatura

from .base import PlatformAdapter, ClipResult


# ========== 「动态渲染嫌疑」判定 ==========
#
# 目的：服务端只抓静态 HTML，拿不到 JS 渲染出来的正文。若页面其实是 SPA /
# 动态加载，抓回的内容会**明显偏少**，用户却在手机上看不出问题 —— 以为
# 「知识库抓得不对」。这里主动识别这种情况，把「建议在浏览器中剪藏」
# 的提示一路带到飞书卡片上。
#
# 判据取两条，任一命中即认定（都不看内容长度本身，避免误伤真正短的页面）：
#   1. HTML 骨架标记：SPA 根节点 + 只在骨架里才出现的加载占位文案；
#   2. 「HTML 很大但正文很少」：静态 HTML 里塞了大段脚本/JSON，
#      trafilatura 却提不出对应正文 —— 典型的前端渲染页面。
_SPA_MARKERS = (
    'id="app"', "id='app'", 'id="root"', "id='root'", 'id="__next"',
    'id="__nuxt"', 'id="___gatsby"', "window.__NUXT__", "window.__INITIAL_STATE__",
    "__NEXT_DATA__", "webpackJsonp", "data-reactroot", "v-cloak",
)
_SPA_TEXT_MARKERS = (
    "正在加载", "加载中", "loading", "请稍候", "初始化",
)
# 「HTML 很大但正文很少」的阈值：HTML 体积超过正文的 20 倍，且正文本身不长。
_HTML_TO_CONTENT_RATIO = 20
_THIN_CONTENT_CHARS = 800


def _looks_dynamically_rendered(html: str, content: str) -> bool:
    """判断这次抓取是否「疑似只拿到了动态页面的静态骨架」。

    返回 True = 建议改用浏览器剪藏；False = 看不出问题。
    """
    if not html:
        return False
    lowered = html.lower()

    # 骨架标记：出现任一 SPA 根节点 / 全局状态注入，即为强信号
    if any(m.lower() in lowered for m in _SPA_MARKERS):
        return True

    # 「HTML 大、正文小」：前端渲染页把数据塞在 <script> 里，
    # trafilatura 提不出正文，于是只剩零星文字。
    text_len = len((content or "").strip())
    if text_len and text_len < _THIN_CONTENT_CHARS:
        if len(html) > text_len * _HTML_TO_CONTENT_RATIO:
            return True
    return False


def _has_loading_placeholder(html: str) -> bool:
    """HTML 正文里是否残留「正在加载」类占位文案。

    单列出来是因为它比 SPA 根节点更弱、更容易误判 —— 只有和
    「正文偏少」同时出现时才作为佐证，不单独定案。
    """
    if not html:
        return False
    try:
        # 只看可见文本，避免匹配到 <script> 里的字面量
        text = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
        text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
        text = re.sub(r"<[^>]+>", " ", text)
    except Exception:
        text = html
    lowered = text.lower()
    return any(m.lower() in lowered for m in _SPA_TEXT_MARKERS)


class WebpageAdapter(PlatformAdapter):
    name = "普通网页"
    priority = 100  # 优先级最低，作为兜底

    @classmethod
    def match(cls, url: str) -> bool:
        # 兜底：所有 http/https 链接都匹配
        return url.startswith("http://") or url.startswith("https://")

    @classmethod
    def extract(cls, url: str) -> ClipResult:
        try:
            downloaded = trafilatura.fetch_url(url)
            if not downloaded:
                raise Exception("无法下载网页内容（可能需要登录或被反爬）")

            content = trafilatura.extract(
                downloaded,
                output_format="markdown",
                include_links=True,
                include_images=True,
                include_tables=True,
            )

            if not content:
                raise Exception("无法提取正文内容")

            # 提取元数据
            metadata = trafilatura.extract_metadata(downloaded)
            title = metadata.title if metadata else ""
            if not title:
                # 从 content 第一行找
                for line in content.split("\n"):
                    line = line.strip()
                    if line.startswith("# "):
                        title = line[2:].strip()
                        break
                    elif line:
                        title = line[:80]
                        break

            author = metadata.author if metadata else ""
            date_str = ""
            if metadata and metadata.date:
                try:
                    date_str = metadata.date[:10]  # YYYY-MM-DD
                except Exception:
                    pass

            # 提取域名作为标签
            domain = urlparse(url).netloc.replace("www.", "")

            # ===== 动态渲染嫌疑判定 =====
            # 结果放进 extra，供服务端写进响应、最终由飞书卡片呈现。
            extra: dict = {}
            if _looks_dynamically_rendered(downloaded, content):
                extra["dynamic_render_suspect"] = True
                extra["dynamic_render_note"] = (
                    "该页面疑似依赖 JavaScript 动态渲染，服务端仅能获取静态骨架，"
                    "内容可能明显少于浏览器中的实际页面。建议在浏览器中打开该页面"
                    "再剪藏。")
            elif _has_loading_placeholder(downloaded) and len(content.strip()) < _THIN_CONTENT_CHARS:
                extra["dynamic_render_suspect"] = True
                extra["dynamic_render_note"] = (
                    "页面残留「正在加载」占位文案且正文偏少，疑似动态渲染未完成。"
                    "建议在浏览器中打开该页面再剪藏。")

            return ClipResult(
                title=title or "网页剪藏",
                url=url,
                author=author or "",
                published_at=date_str,
                content=f"# {title or '网页剪藏'}\n\n{content}",
                type="article",
                platform=domain,
                tags=["网页", domain],
                extra=extra,
            )

        except Exception as e:
            raise Exception(f"网页抓取失败: {e}")

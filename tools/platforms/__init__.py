"""平台适配器注册表。

每个平台适配器实现以下接口：
    name: str                     # 平台名称
    def match(url: str) -> bool   # 是否匹配该平台
    def extract(url: str) -> dict # 抓取内容，返回标准格式字典

标准格式：
    {
        "title": str,
        "url": str,
        "author": str,
        "published_at": str (YYYY-MM-DD),
        "content": str (Markdown),
        "tags": [str],
        "type": "article" | "video" | "doc" | ...,
        "platform": str,
    }
"""
from .base import ClipResult, PlatformAdapter
from .registry import get_platform, extract_url, list_platforms

__all__ = ["ClipResult", "PlatformAdapter", "get_platform", "extract_url", "list_platforms"]

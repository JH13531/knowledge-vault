"""平台适配器基类和数据结构。"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import List


@dataclass
class ClipResult:
    """剪藏结果的标准格式。"""
    title: str
    url: str
    content: str                      # Markdown 正文
    author: str = ""
    published_at: str = ""            # YYYY-MM-DD
    type: str = "article"             # article | video | doc | note
    platform: str = "web"
    tags: List[str] = field(default_factory=list)
    duration: str = ""                # 视频时长（可选）
    extra: dict = field(default_factory=dict)


class NoBrowserFallback(Exception):
    """适配器明确拒绝「降级到浏览器协助队列」。

    抛出它 = 本次失败**不是**「换个通道再试就能好」的那一类。典型场景：飞书文档
    缺 lark-cli —— 浏览器降级（扩展的 feishu.js / playwright-worker）产出的正文
    通常不完整，却被当成完整内容写进 raw/articles/，等于用一份骗人的快照冒充剪藏。

    调用方（`clip-server._smart_clip`）捕获后必须**直接报错**：把任务记为 failed
    并回明确原因，**不得**调用 `requeue_for_browser()`。
    """


class PlatformAdapter:
    """平台适配器基类。"""

    name: str = "base"
    priority: int = 100  # 数字越小优先级越高

    # 该平台的文档能否降级到浏览器协助队列。
    # 默认 True；飞书这类「浏览器抓不全」的通道置 False —— 除了抓取时抛
    # NoBrowserFallback，重试时也要据此**留走后端通道**，否则任务会一直
    # 停在浏览器队列里等一个永远抓不对的结果。
    allows_browser_fallback: bool = True

    @classmethod
    def match(cls, url: str) -> bool:
        """判断 URL 是否属于该平台。"""
        raise NotImplementedError

    @classmethod
    def extract(cls, url: str) -> ClipResult:
        """抓取内容并返回标准结果。"""
        raise NotImplementedError

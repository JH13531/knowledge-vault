#!/usr/bin/env python3
"""content_measure — 「真实正文」的统一度量口径。

## 为什么要有这个独立模块

「这段快照里到底有多少**正文**」这个判断同时被三处使用：

  1. `clip-server.py` 的长度门禁与「图多字少」闸门；
  2. `clip-server.py` 去重时比较「新旧两版谁更完整」；
  3. `ocr_images.py` 的候选筛选（该给哪篇跑 OCR）。

三处**必须用同一把尺子**。任何一处口径漂移，就会出现「一边判为内容充足、
另一边判为该 OCR」——2026-10-14 实测正是如此：clip-server 的门禁被脚手架撑过
阈值、而 ocr_images 的候选列表用的是另一份实现，两边互相以为对方会兜住，
结果一条正文只有 19 字的图文笔记悄无声息地生成了一页垃圾 wiki。

本模块**无任何副作用**（不碰网络 / 数据库 / 文件系统），可被任意模块安全导入。

## 「脚手架」是什么

剪藏快照里除正文之外的一切结构化填充物，都不该算进正文字数：

  - 图片 markdown `![alt](url)` —— 远程 URL 可达 100~160 字符/条，几张图就能
    把字数顶过任何阈值；
  - 元数据行（`> 作者：…`、`> **链接：** https://…`）与其中的裸 URL；
  - 结构骨架标题（`## 原文快照` / `## 正文` / `## 图片`）；
  - 话题标签（小红书 `#某品牌[话题]#`）；
  - 模板尾注（`> 注：本页为剪藏快照…`）；
  - frontmatter。
"""

from __future__ import annotations

import re

#: 「真实文字过少」的阈值。长度门禁（clip-server）与 OCR 候选筛选（ocr_images）
#: 共用同一个数字 —— 两边取值不同就会出现「一边判为内容充足、另一边判为该 OCR」。
MIN_REAL_TEXT_CHARS = 200

#: 至少含几张图才算「图多字少」。
MIN_IMAGES = 1

#: 结构性小标题 —— 由适配器/模板生成，不是正文。
SCAFFOLD_HEADINGS = (
    "原文快照", "正文", "图片", "选中内容", "图片文字（OCR）",
    "我的思考", "我的笔记", "来源",
)

#: blockquote 里以这些标签开头的行是元数据，不是正文。
META_LABELS = (
    "来源", "作者", "发布时间", "抓取自", "字数", "平台", "类型",
    "点赞", "收藏", "评论", "链接", "页面", "抓取时间", "注",
)

_CJK = re.compile(r"[一-鿿]")
_EN_WORD = re.compile(r"[a-zA-Z]{3,}")
_IMG_MD = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_BARE_URL = re.compile(r"https?://\S+")
_FRONTMATTER = re.compile(r"\A---\n.*?\n---[ \t]*\n", re.S)
_TOO_TOPIC_TAG = re.compile(r"#[^\s#\[\]]+\[话题\]#")
_SKELETON_HEADING = re.compile(
    r"^#{1,6}[ \t]*(" + "|".join(re.escape(x) for x in SCAFFOLD_HEADINGS) + r")[ \t]*$",
    re.M)
_META_LINE = re.compile(
    # `> 作者：…` / `> **链接：** …`（加粗写法旧实现漏掉，是实测漏判的直接原因）
    r"^>[ \t]*\*{0,2}[ \t]*(" + "|".join(re.escape(x) for x in META_LABELS)
    + r")[ \t]*\*{0,2}[ \t]*[：:].*$",
    re.M)
_NOTE_LINE = re.compile(r"^>[ \t]*注[：:].*$", re.M)


def clean_for_measure(content: str) -> str:
    """剥掉剪藏快照里的脚手架，只留可见正文文字。"""
    t = str(content or "")
    t = _FRONTMATTER.sub("", t, count=1)
    t = _IMG_MD.sub("", t)
    t = _HTML_COMMENT.sub("", t)
    t = _MD_LINK.sub(r"\1", t)          # 链接保留可见文字、丢掉 URL
    t = _BARE_URL.sub("", t)
    t = _META_LINE.sub("", t)
    t = _SKELETON_HEADING.sub("", t)
    t = _TOO_TOPIC_TAG.sub("", t)
    t = _NOTE_LINE.sub("", t)
    return t


def real_text_length(content: str) -> int:
    """**真实正文字数**：剥掉脚手架后去空白计长。"""
    return len(re.sub(r"\s", "", clean_for_measure(content)))


def count_words(content: str) -> int:
    """正文词数：CJK 逐字 + 3 字母以上英文单词（同样先剥脚手架）。

    为什么不能在原始文本上直接数：那会把图片 URL 里的英文片段（`https` /
    `xhslink` …）算成「词」，于是「新旧两版谁更完整」的差值由**图片数量**
    而非正文量决定，比较结果没有意义。
    """
    if not content:
        return 0
    t = clean_for_measure(content)
    return len(_CJK.findall(t)) + len(_EN_WORD.findall(t))


def count_markdown_images(content: str) -> int:
    """正文里的 Markdown 图片数量（`![alt](url)`）。"""
    return len(re.findall(r"!\[", content or ""))


def has_text_in_images(content: str, min_images: int = MIN_IMAGES,
                       min_chars: int = MIN_REAL_TEXT_CHARS) -> str:
    """是否为「图多字少」（含图 ≥ min_images 且真实文字 < min_chars）。

    返回非空字符串 = 命中的**原因**（可直接给用户看）；空字符串 = 未命中。
    """
    imgs = count_markdown_images(content)
    if imgs < min_images:
        return ""
    real = real_text_length(content)
    if real >= min_chars:
        return ""
    return (f"正文文字极少（剔图后约 {real} 字）且含 {imgs} 张图片，"
            "文字很可能写在图片中")

#!/usr/bin/env python3
"""给「已入库但没生成 wiki」的 raw 补生成 wiki 页。

## 为什么需要它

剪藏的完整形态是 **raw + wiki**。但 wiki 精加工依赖外部通道
（Tier 1: claude/codex，Tier 2: API key），一旦通道临时不可用 ——
最常见的是 **API 额度用尽（HTTP 429）**、key 失效、网络不通 ——
raw 会照常落盘，wiki 则生成失败。

clip-server 写完 raw 就把内容当作「已入库」，**不会自动重试**。
于是库里会攒下 wiki_refs 为空的「孤儿 raw」（`lint.py` 会报 `raw:orphan`）。

这个脚本就是补这一步：找出孤儿 raw，重跑 wiki 精加工。

## 用法

```bash
# 只看有哪些孤儿（默认 dry-run，不改任何文件）
python tools/retry-wiki.py

# 真正补生成（全部）
python tools/retry-wiki.py --apply

# 只补一个（文件名或 stem 均可）
python tools/retry-wiki.py --apply --file 2026-10-05-连赢6次，你的大脑结构就变了.md

# 正文过短的（疑似抓取不完整）默认跳过，要强行补就加 --include-short
python tools/retry-wiki.py --apply --include-short
```

退出码：0 = 全部成功或没有待办；1 = 有失败项。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
VAULT = TOOLS_DIR.parent
ARTICLES_DIR = VAULT / "raw" / "articles"

sys.path.insert(0, str(TOOLS_DIR))

# 正文过短的 raw 大概率是抓取不完整（登录墙/验证页），生成 wiki 意义不大。
# 与 clip-server 的 _AUTO_REFINE_MIN_CHARS 保持同一个量级。
MIN_CHARS = 200


def _frontmatter(text: str) -> str:
    """取 YAML frontmatter 文本（不含首尾 ---）。"""
    m = re.match(r"^---\n(.*?)\n---\s*\n", text, re.S)
    return m.group(1) if m else ""


def _has_wiki_refs(content: str) -> bool:
    """frontmatter 的 wiki_refs 是否有实际条目。

    要同时支持两种写法：
        wiki_refs: [wiki/topics/x.md]
        wiki_refs:
          - wiki/topics/x.md
    不能用 "wiki_refs: []" 子串匹配（正文里可能有示例片段）。
    """
    fm = _frontmatter(content)
    if not fm:
        return False
    lines = fm.split("\n")
    for i, line in enumerate(lines):
        m = re.match(r"^wiki_refs:[ \t]*(.*)$", line)
        if not m:
            continue
        inline = m.group(1).strip()
        if inline and inline not in ("[]", "[ ]"):
            return True
        # 块列表：后续缩进的 "- xxx" 行
        for nxt in lines[i + 1:]:
            if re.match(r"^[ \t]+-[ \t]*\S", nxt):
                return True
            if not re.match(r"^[ \t]", nxt):
                break
    return False


def _body_len(content: str) -> int:
    """去掉 frontmatter 后的正文字符数。"""
    m = re.match(r"^---\n.*?\n---\s*\n", content, re.S)
    body = content[m.end():] if m else content
    return len(re.sub(r"\s+", "", body))


def find_orphans(include_short: bool = False, only: str = "") -> tuple[list[Path], list[Path]]:
    """返回 (待补清单, 因正文过短被跳过的清单)。"""
    todo: list[Path] = []
    skipped: list[Path] = []
    if not ARTICLES_DIR.is_dir():
        return todo, skipped

    for md in sorted(ARTICLES_DIR.glob("*.md")):
        if only and only not in (md.name, md.stem):
            continue
        try:
            content = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _has_wiki_refs(content):
            continue
        if not include_short and _body_len(content) < MIN_CHARS:
            skipped.append(md)
            continue
        todo.append(md)
    return todo, skipped


def main() -> int:
    ap = argparse.ArgumentParser(
        description="给已入库但没有 wiki_refs 的 raw 补生成 wiki 页（默认 dry-run）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--apply", action="store_true",
                    help="真正执行（不加则只列出待办，不调用任何模型/接口）")
    ap.add_argument("--file", default="", help="只处理这一个文件（文件名或 stem）")
    ap.add_argument("--include-short", action="store_true",
                    help=f"连正文 < {MIN_CHARS} 字的也一起补（默认跳过，疑似抓取不完整）")
    ap.add_argument("--limit", type=int, default=0, help="最多处理几个（0 = 不限）")
    args = ap.parse_args()

    todo, skipped = find_orphans(include_short=args.include_short, only=args.file)
    if args.limit > 0:
        todo = todo[:args.limit]

    print(f"目录：{ARTICLES_DIR}")
    print(f"待补 wiki 的 raw：{len(todo)} 个")
    if skipped:
        print(f"因正文过短被跳过：{len(skipped)} 个（加 --include-short 可强行补）")
        for p in skipped[:5]:
            print(f"    - {p.name}")
        if len(skipped) > 5:
            print(f"    … 另有 {len(skipped) - 5} 个")

    if not todo:
        print("\n没有待办。")
        return 0

    print("\n清单：")
    for p in todo:
        print(f"    - {p.name}")

    if not args.apply:
        print("\n（dry-run，未改动任何文件。加 --apply 才会真正补生成）")
        return 0

    import wiki_gen  # 延迟导入：dry-run 时不必加载 requests 等依赖

    if not wiki_gen.is_available():
        print("\n❌ 当前没有任何可用的 wiki 精加工通道：")
        print("   Tier 1 需要 claude / codex 可执行文件（PATH 里找得到）")
        print("   Tier 2 需要 tools/wiki-config.local.json 或环境变量 WIKI_GEN_API_KEY")
        print("   补上其中一条再跑本脚本；raw 一直都在，不会丢。")
        return 1

    ok = 0
    failed: list[tuple[str, str]] = []
    print("\n开始补生成…\n")
    for i, p in enumerate(todo, 1):
        print(f"--- [{i}/{len(todo)}] {p.name} ---")
        result = wiki_gen.run_wiki_generation_sync(p)
        status = result.get("status")
        if status == "ok":
            ok += 1
            print(f"    ✅ 已生成（{result.get('tier', '')}）")
        else:
            failed.append((p.name, str(result.get("reason", ""))))
            print(f"    ❌ 失败：{result.get('reason', '未知原因')}")

    print("\n" + "=" * 56)
    print(f"完成：成功 {ok} / 失败 {len(failed)}（共 {len(todo)}）")
    for name, reason in failed:
        print(f"    ❌ {name}\n       {reason}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

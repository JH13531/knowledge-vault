#!/usr/bin/env python3
"""
knowledge-vault lint 脚本
默认 dry-run，仅输出报告；--fix 只自动修复 🟢 低风险项。

用法：
    python3 lint.py                # dry-run 报告
    python3 lint.py --fix          # 修复低风险项（当前支持：从文件名回填 published_at）
    python3 lint.py --json         # 机器可读输出（配合 --fix 或纯报告）

注意：推荐安装 PyYAML（pip install -r requirements.txt）。
未安装时使用内置的简化 frontmatter 解析器（仅支持扁平键值与列表，
足以覆盖本 vault 的 frontmatter 格式）。
"""
from __future__ import annotations
import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

try:
    import yaml  # type: ignore
except ImportError:
    yaml = None

VAULT = Path(__file__).resolve().parent
RAW = VAULT / "raw"
WIKI = VAULT / "wiki"

# 这些目录里的内容按定义就不该参与体检：
#   _archive       —— stale / 重复件归档（§4.4.1）
#   _webp_backup   —— convert_to_webp.py 保存的原图备份，按定义全是非 WebP
#                     （不排除的话「图片没转 WebP」那条检查会报 100+ 条噪音）
SKIP_DIRS = {"_archive", "_webp_backup"}

REQUIRED_RAW_FIELDS = ["date", "source", "type", "tags", "wiki_refs", "status"]
VALID_RAW_FIELDS = {
    "date", "source", "source_url", "published_at",
    "type", "tags", "wiki_refs", "status",
    "fact_type", "platform", "version_id", "tier",
    "subdocs_count", "last_checked",
    "source_id", "author", "word_count", "duration",
}
VALID_WIKI_FIELDS = {
    "title", "type", "created", "updated", "last_verified",
    "source_count", "tags", "contradicts", "question",
}
STATUS_ENUM = {"classified", "triage", "anomaly", "stale"}
TYPE_ENUM = {"article", "screenshot", "audio", "chat", "doc", "note", "selection", "video"}

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
URL_RE = re.compile(r"^https?://\S+")
FILENAME_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-")
RAW_REF_RE = re.compile(r"\[\[([^\]]*raw/[^\]]+)\]\]")

# wiki/ 下不属于"知识页面"的文件（从页面检查中排除）
WIKI_META_FILES = {"index.md", "log.md"}

IMAGE_EXTS = {".webp", ".png", ".jpg", ".jpeg", ".gif"}
# §7.1「大图强制压缩」的判定阈值（超过即报告，仅提示不自动改）
OVERSIZED_IMAGE_BYTES = 512 * 1024


def rel_posix(p: Path) -> str:
    """统一的 vault 根相对路径表示：始终使用正斜杠（与 wiki-link 一致，跨平台可比）。"""
    return p.relative_to(VAULT).as_posix()


def resolve_wiki_link(ref: str, page_path: Path) -> str:
    """将 wiki-link 中的路径解析为 vault 根相对路径（正斜杠）。
    支持绝对路径（raw/...、wiki/...）和相对路径（../../raw/...）。
    """
    ref = ref.strip().replace("\\", "/")
    if ref.startswith(("raw/", "wiki/", "reviews/", "templates/")):
        return ref
    # 相对路径：基于页面所在目录解析
    base = page_path.parent
    resolved = (base / ref).resolve()
    try:
        return resolved.relative_to(VAULT.resolve()).as_posix()
    except ValueError:
        return ref


def _naive_parse_scalar(v: str):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def _naive_parse_inline_list(v: str) -> list:
    inner = v.strip()[1:-1].strip()
    if not inner:
        return []
    return [_naive_parse_scalar(item) for item in inner.split(",")]


def load_frontmatter(text: str):
    """解析 frontmatter。优先 PyYAML；缺失时使用内置扁平解析器。

    内置解析器支持：key: value、key: [a, b]、key: []、块式列表（- item），
    并正确处理引号包裹的值。这是无 PyYAML 环境的兜底，而非完整 YAML。
    """
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None, text
    fm_raw = m.group(1)
    if yaml:
        try:
            return yaml.safe_load(fm_raw), text[m.end():]
        except Exception:
            return None, text
    # naive fallback：扁平 frontmatter 解析
    fm = {}
    lines = fm_raw.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k = k.strip()
        v = v.strip()
        if v.startswith("[") and v.endswith("]"):
            fm[k] = _naive_parse_inline_list(v)
        elif v == "":
            # 可能是块式列表
            items = []
            while i < len(lines) and lines[i].lstrip().startswith("- "):
                items.append(_naive_parse_scalar(lines[i].lstrip()[2:]))
                i += 1
            fm[k] = items if items else ""
        else:
            fm[k] = _naive_parse_scalar(v)
    return fm, text[m.end():]


def extract_filename_date(stem: str) -> str | None:
    m = FILENAME_DATE_RE.match(stem)
    return m.group(1) if m else None


def parse_iso_date(value) -> date | None:
    """宽松解析日期（兼容 str 与 datetime.date），非法返回 None。"""
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def fix_published_at(f: Path, date_str: str) -> bool:
    """把从文件名提取的 published_at 插入 raw frontmatter（插在 date: 行之后）。"""
    text = f.read_text(encoding="utf-8")
    m = FRONTMATTER_RE.match(text)
    if not m:
        return False
    fm_lines = m.group(1).splitlines()
    out_lines = []
    inserted = False
    for line in fm_lines:
        out_lines.append(line)
        if not inserted and re.match(r"^date\s*:", line):
            out_lines.append(f"published_at: {date_str}")
            inserted = True
    if not inserted:
        out_lines.append(f"published_at: {date_str}")
    new_text = "---\n" + "\n".join(out_lines) + "\n---\n" + text[m.end():]
    f.write_text(new_text, encoding="utf-8")
    return True


def collect_findings():
    findings = {"low": [], "medium": [], "info": []}

    # 收集 raw 文件数据（供后续跨层检查使用）
    raw_files_data = []
    raw_wiki_refs: dict[str, set] = {}  # raw 相对路径 -> frontmatter 中的 wiki_refs 集合

    # ---- 1. Raw frontmatter completeness ----
    if RAW.exists():
        for f in RAW.rglob("*.md"):
            if any(d in f.parts for d in SKIP_DIRS):
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
            fm, _ = load_frontmatter(text)
            rel = rel_posix(f)
            if fm is None:
                findings["low"].append(f"[raw:no-frontmatter] {rel}")
                continue
            # 收集数据
            raw_files_data.append({
                "rel": rel,
                "tags": set(fm.get("tags", []) or []),
                "published_at": fm.get("published_at"),
                # 供重复 raw 检测使用（§6.2 检查 23）
                "source_id": fm.get("source_id"),
                "source_url": fm.get("source_url"),
                "version_id": fm.get("version_id"),
            })
            refs = fm.get("wiki_refs", [])
            raw_wiki_refs[rel] = {str(r) for r in refs} if isinstance(refs, list) else set()
            # §4.2：`source` 仅「非 URL 来源」必填；URL 来源的 raw 以 source_url 为来源标识。
            # 因此有 source_url 的 raw 豁免 source 字段，否则会对所有剪藏件批量误报。
            required = REQUIRED_RAW_FIELDS
            if fm.get("source_url"):
                required = [k for k in required if k != "source"]
            missing = [k for k in required if k not in fm]
            if missing:
                findings["low"].append(
                    f"[raw:missing-fields] {rel} — 缺失: {', '.join(missing)}"
                )
            # 非法/多余字段检测
            extra = [k for k in fm if k not in VALID_RAW_FIELDS]
            if extra:
                findings["low"].append(
                    f"[raw:extra-fields] {rel} — 非法字段: {', '.join(extra)}"
                )
            status = fm.get("status")
            if status and status not in STATUS_ENUM:
                findings["low"].append(
                    f"[raw:bad-status] {rel} — status={status} 不在枚举 {sorted(STATUS_ENUM)}"
                )
            rtype = fm.get("type")
            if rtype and rtype not in TYPE_ENUM:
                findings["low"].append(
                    f"[raw:bad-type] {rel} — type={rtype} 不在枚举 {sorted(TYPE_ENUM)}"
                )
            # source_url 格式校验
            source_url = fm.get("source_url")
            if source_url is not None and str(source_url).strip() != "":
                if not URL_RE.match(str(source_url)):
                    findings["low"].append(
                        f"[raw:bad-source-url] {rel} — source_url 格式不合规: {source_url}"
                    )
            # published_at 校验
            pub = fm.get("published_at")
            if pub is not None and str(pub).strip() != "":
                pub_str = str(pub)
                if not DATE_RE.match(pub_str):
                    findings["low"].append(
                        f"[raw:bad-published-at] {rel} — published_at 格式非 YYYY-MM-DD: {pub_str}"
                    )
                else:
                    pub_date = parse_iso_date(pub_str)
                    if pub_date is None:
                        findings["low"].append(
                            f"[raw:bad-published-at] {rel} — published_at 日期无效: {pub_str}"
                        )
                    elif pub_date > date.today():
                        findings["low"].append(
                            f"[raw:future-published-at] {rel} — published_at={pub_str} 晚于今天"
                        )
            else:
                # 缺失时尝试从文件名提取
                fname_date = extract_filename_date(f.stem)
                if fname_date:
                    findings["info"].append(
                        f"[raw:missing-published-at] {rel} — 可从文件名提取: {fname_date}"
                    )
            # orphan raw: wiki_refs is empty
            if isinstance(refs, list) and len(refs) == 0:
                findings["medium"].append(f"[raw:orphan] {rel} — wiki_refs 为空，尚未提炼")
            # dead wiki_refs
            if isinstance(refs, list):
                for r in refs:
                    if not (VAULT / str(r)).exists():
                        findings["low"].append(
                            f"[raw:dead-wiki-ref] {rel} → {r} 不存在"
                        )
            # triage 衰减扫描
            if status == "triage":
                d = parse_iso_date(fm.get("date"))
                if d:
                    age_days = (date.today() - d).days
                    if age_days >= 30:
                        findings["medium"].append(
                            f"[raw:triage-stale] {rel} — triage 已 {age_days} 天，建议补充分类或归档"
                        )
                    elif age_days >= 7:
                        findings["medium"].append(
                            f"[raw:triage-aging] {rel} — triage 已 {age_days} 天，请尽快处理"
                        )
            # live-source 时效提醒（§4.5.4）
            if fm.get("fact_type") == "live-source":
                lc = parse_iso_date(fm.get("last_checked"))
                if lc is None:
                    findings["info"].append(
                        f"[raw:live-source-no-check] {rel} — live-source 缺少有效的 last_checked"
                    )
                else:
                    age_days = (date.today() - lc).days
                    if age_days > 180:
                        findings["medium"].append(
                            f"[raw:live-source-stale] {rel} — last_checked 已 {age_days} 天（>180），建议重新检查或标记 stale"
                        )
                    elif age_days > 90:
                        findings["info"].append(
                            f"[raw:live-source-aging] {rel} — last_checked 已 {age_days} 天（>90），可安排复查"
                        )
            # 空内容保护：正文过短告警（可能抓取失败）
            # 豁免：inbox/ 目录、_archive/、以及子目录深度 >= 3 的镜像文件（单篇短很正常）
            path_parts = Path(rel).parts
            is_deep_mirror = len(path_parts) >= 4 and path_parts[1] == 'articles'
            is_inbox = 'inbox' in path_parts
            if not is_deep_mirror and not is_inbox:
                fm_match = FRONTMATTER_RE.match(text)
                if fm_match:
                    body = text[fm_match.end():].strip()
                    # 去掉代码块
                    body_no_code = re.sub(r'```.*?```', '', body, flags=re.DOTALL)
                    # 去掉图片链接
                    body_no_img = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', body_no_code)
                    # 过滤纯格式行，统计有效内容
                    lines = [l.strip() for l in body_no_img.split('\n') if l.strip()]
                    content_lines = [l for l in lines if not l.startswith(('#', '>', '- ', '* ', '|', '---'))]
                    content_text = ' '.join(content_lines)
                    # 粗略估算：中文字符数 + 英文单词数
                    chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', content_text))
                    english_words = len(re.findall(r'[a-zA-Z]{3,}', content_text))
                    estimated_length = chinese_chars + english_words

                    # 阈值：普通文章 200 字，note/screenshot 50 字
                    threshold = 50 if rtype in ("note", "screenshot") else 200

                    if estimated_length < threshold:
                        findings["medium"].append(
                            f"[raw:thin-content] {rel} — 正文约 {estimated_length} 字，低于 {threshold} 字阈值，可能抓取失败或不完整"
                        )

                    # 关键词检测：疑似抓取失败的特征文本（只查前 2000 字符）
                    fail_keywords = ['404', '403', '权限不足', '访问被拒绝', '页面不存在', '抓取失败', '登录后查看', '付费内容']
                    body_head = body[:2000]
                    for kw in fail_keywords:
                        if kw in body_head:
                            findings["medium"].append(
                                f"[raw:fetch-failure-signal] {rel} — 正文中包含疑似抓取失败关键词: '{kw}'"
                            )
                            break

        # ---- raw/ 根目录游离文件 ----
        for f in RAW.iterdir():
            if f.is_file():
                findings["low"].append(
                    f"[raw:root-loose-file] {rel_posix(f)} 应该放到子目录"
                )

        # ---- 孤儿图片检测 + 图片引用路径检测 ----
        # 预读所有 md 文本，避免对每张图重复扫描整个 raw/（O(图×文) → O(图+文)）
        all_raw_text = "\n".join(
            md.read_text(encoding="utf-8", errors="replace")
            for md in RAW.rglob("*.md")
        )
        for f in RAW.rglob("*"):
            if f.is_file() and f.suffix.lower() in IMAGE_EXTS:
                if f.name not in all_raw_text:
                    findings["medium"].append(
                        f"[raw:orphan-image] {rel_posix(f)} 未被任何 md 引用"
                    )

    # ---- 2. Wiki 页面检查 ----
    page_raw_refs: dict[str, list[str]] = {}
    all_pages = []
    wiki_pages_data = []  # 收集数据供跨页检查
    if WIKI.exists():
        for f in WIKI.rglob("*.md"):
            # 排除 index.md 和 log.md
            if f.name in WIKI_META_FILES:
                continue
            all_pages.append(f)
            text = f.read_text(encoding="utf-8", errors="replace")
            rel = rel_posix(f)
            fm, _ = load_frontmatter(text)

            # 收集数据（无 frontmatter 也记录，避免后续 KeyError）
            if fm is None:
                findings["low"].append(f"[wiki:no-frontmatter] {rel} — 缺少 frontmatter")
                fm = {}
            else:
                # wiki 页面非法/多余字段检测
                extra = [k for k in fm if k not in VALID_WIKI_FIELDS]
                if extra:
                    findings["low"].append(
                        f"[wiki:extra-fields] {rel} — 非法字段: {', '.join(extra)}"
                    )
            sources = {
                resolve_wiki_link(m.group(1), f)
                for m in RAW_REF_RE.finditer(text)
            }
            wiki_pages_data.append({
                "path": f,
                "rel": rel,
                "stem": f.stem,
                "tags": set(fm.get("tags", []) or []),
                "sources": sources,
                "last_verified": fm.get("last_verified"),
                "contradicts": fm.get("contradicts", []) or [],
                "text": text,
            })

            if "## 来源" not in text and "## Source" not in text:
                findings["low"].append(f"[page:no-source-section] {rel}")
            # 解析 raw 引用（支持绝对路径和相对路径）
            for m in RAW_REF_RE.finditer(text):
                ref = resolve_wiki_link(m.group(1), f)
                page_raw_refs.setdefault(ref, []).append(rel)
                if not (VAULT / ref).exists():
                    findings["low"].append(
                        f"[page:dead-raw-ref] {rel} → {ref} 不存在"
                    )
            # 检测空来源：有 ## 来源章节但没有任何 raw 文件引用
            # （仅对 topics 页严格要求；query 页允许只引用 wiki 页面）
            if rel.startswith("wiki/topics/") and ("## 来源" in text or "## Source" in text):
                source_match = re.search(r"## (来源|Source)\b(.*?)(?=\n## |\Z)", text, re.DOTALL)
                if source_match:
                    source_sec = source_match.group(2)
                    has_raw = bool(re.search(r"\[\[.*raw/|raw/articles|raw/notes", source_sec))
                    if not has_raw:
                        findings["low"].append(
                            f"[page:empty-source] {rel} — 来源章节无实际 raw 文件引用"
                        )
            # 图片引用路径检测
            for img_match in re.finditer(r"!\[[^\]]*\]\(([^)]+)\)", text):
                img_path = img_match.group(1)
                if img_path.startswith(("assets/", "attachments/", "/assets/", "/attachments/")):
                    findings["low"].append(
                        f"[page:bad-image-path] {rel} → 图片引用 {img_path} 应使用相对路径，避免 assets/"
                    )

    # ---- 3. Wiki index 覆盖 ----
    index_path = WIKI / "index.md"
    idx_text = index_path.read_text(encoding="utf-8", errors="replace") if index_path.exists() else ""
    if index_path.exists():
        for p in all_pages:
            rel = rel_posix(p)
            if rel not in idx_text and p.name not in idx_text:
                findings["medium"].append(f"[page:not-in-index] {rel}")

    # ---- 4. 孤儿 page（没有任何页面引用它，且不在 queries） ----
    # 只在 topics/ 里检测
    for p in all_pages:
        rel = rel_posix(p)
        if not rel.startswith("wiki/topics/"):
            continue
        # 是否被其他 page 引用？
        referenced = False
        for other in all_pages:
            if other == p:
                continue
            t = other.read_text(encoding="utf-8", errors="replace")
            if rel in t or p.stem in t:
                referenced = True
                break
        if not referenced and rel not in idx_text and p.name not in idx_text:
            findings["medium"].append(f"[page:orphan] {rel} — 无入站引用且不在 index")

    # ---- 5. 根目录附件检测 ----
    for d in ("assets", "attachments"):
        if (VAULT / d).is_dir():
            findings["low"].append(
                f"[root:attachments-dir] 根目录下存在 {d}/ — 请检查 Obsidian 配置"
            )

    # ---- 6. 旧目录残留检测 ----
    for d in ("concepts", "entities", "summaries"):
        if (WIKI / d).is_dir():
            findings["low"].append(
                f"[wiki:legacy-dir] wiki/{d}/ 已废弃，应迁移到 wiki/topics/"
            )

    # ---- 7. last_verified 日期校验 ----
    for page in wiki_pages_data:
        if not page["rel"].startswith("wiki/topics/"):
            continue
        lv = page["last_verified"]
        if lv is None or str(lv).strip() == "":
            findings["medium"].append(
                f"[wiki:missing-last-verified] {page['rel']} — last_verified 缺失"
            )
            continue
        lv_str = str(lv)
        if not DATE_RE.match(lv_str):
            findings["low"].append(
                f"[wiki:bad-last-verified] {page['rel']} — last_verified 格式非 YYYY-MM-DD: {lv_str}"
            )
            continue
        lv_date = parse_iso_date(lv_str)
        if lv_date is None:
            findings["low"].append(
                f"[wiki:bad-last-verified] {page['rel']} — last_verified 日期无效: {lv_str}"
            )
            continue
        if lv_date > date.today():
            findings["low"].append(
                f"[wiki:future-last-verified] {page['rel']} — last_verified={lv_str} 晚于今天"
            )
            continue
        age_days = (date.today() - lv_date).days
        if age_days > 365:
            findings["medium"].append(
                f"[wiki:stale-content] {page['rel']} — last_verified 距今 {age_days} 天（>12 个月），请验证内容仍正确"
            )
        elif age_days > 180:
            findings["info"].append(
                f"[wiki:aging-content] {page['rel']} — last_verified 距今 {age_days} 天（>6 个月），建议复核"
            )

    # ---- 8. 同标签更晚的 raw 未被 wiki 引用 ----
    for page in wiki_pages_data:
        if not page["rel"].startswith("wiki/topics/"):
            continue
        page_tags = page["tags"]
        if not page_tags:
            continue
        page_lv = page["last_verified"]

        for raw in raw_files_data:
            shared = page_tags.intersection(raw["tags"])
            if len(shared) < 2:
                continue
            if raw["rel"] in page["sources"]:
                continue  # 已被引用

            # 比较日期
            raw_date = parse_iso_date(raw["published_at"])
            lv_date = parse_iso_date(page_lv)
            if raw_date and lv_date and raw_date > lv_date:
                findings["medium"].append(
                    f"[wiki:unreferenced-newer-raw] {page['rel']} — {raw['rel']} 更新（{raw_date} vs {lv_date}）且共享标签 {sorted(shared)}，但未被引用"
                )

    # ---- 9. 标签重叠但无交叉引用 ----
    # 如果两个 topic 共享 ≥2 个标签但完全没有交叉引用，值得人工审视：
    # 可能是标签太泛（应该删标签），也可能是确实该互引但漏了。
    for i in range(len(wiki_pages_data)):
        page_a = wiki_pages_data[i]
        if not page_a["rel"].startswith("wiki/topics/"):
            continue
        for j in range(i + 1, len(wiki_pages_data)):
            page_b = wiki_pages_data[j]
            if not page_b["rel"].startswith("wiki/topics/"):
                continue

            shared = page_a["tags"].intersection(page_b["tags"])
            if len(shared) < 2:
                continue

            a_refs_b = (page_b["rel"] in page_a["text"]) or (page_b["stem"] in page_a["text"])
            b_refs_a = (page_a["rel"] in page_b["text"]) or (page_a["stem"] in page_b["text"])

            if not a_refs_b and not b_refs_a:
                findings["medium"].append(
                    f"[wiki:tag-overlap-no-xref] {page_a['rel']} ↔ {page_b['rel']} 共享 {len(shared)} 个标签 {sorted(shared)} 但无交叉引用，检查是否该删标签或补引用"
                )

    # ---- 10. contradicts 字段校验（存在性 + 双向性）----
    for page in wiki_pages_data:
        contradicts = page["contradicts"]
        if not isinstance(contradicts, list) or not contradicts:
            continue
        for target in contradicts:
            target = str(target).replace("\\", "/")
            target_path = VAULT / target
            if not target_path.exists():
                findings["low"].append(
                    f"[wiki:dead-contradict] {page['rel']} → {target} 不存在"
                )
                continue
            # 校验双向性
            try:
                target_text = target_path.read_text(encoding="utf-8", errors="replace")
                target_fm, _ = load_frontmatter(target_text)
                if target_fm is None:
                    continue
                target_contradicts = target_fm.get("contradicts", []) or []
                if not isinstance(target_contradicts, list):
                    continue
                if page["rel"] not in [str(t).replace("\\", "/") for t in target_contradicts]:
                    findings["medium"].append(
                        f"[wiki:non-bidirectional-contradict] {page['rel']} 标记与 {target} 矛盾，但反向未标注"
                    )
            except Exception:
                pass

    # ---- 11. 重复主题检测（同名 stem 出现在 wiki/topics/ 多个位置）----
    by_stem: dict[str, list[str]] = defaultdict(list)
    for page in wiki_pages_data:
        if page["rel"].startswith("wiki/topics/"):
            by_stem[page["stem"]].append(page["rel"])
    for stem, paths in by_stem.items():
        if len(paths) > 1:
            findings["medium"].append(
                f"[wiki:duplicate-topic] {stem} 存在 {len(paths)} 个同名主题页: {', '.join(paths)}"
            )

    # ---- 12. wiki_refs 反向回填校验 ----
    # wiki 页 来源 里引用了 raw，但 raw 的 wiki_refs 没有回填该页 → 双向溯源断裂
    for page in wiki_pages_data:
        for src in page["sources"]:
            refs = raw_wiki_refs.get(src)
            if refs is not None and page["rel"] not in refs:
                findings["medium"].append(
                    f"[wiki:unbackfilled-wiki-refs] {page['rel']} 引用了 {src}，但 raw 的 wiki_refs 未回填该页"
                )

    # ---- 13. 重复 raw 检测（同一篇文档被重复入库）----
    # 背景：clip-server 的去重对部分链路（如飞书文档）会失效，同一篇文档以多份近乎逐字的
    # raw 落库（ADR-010 实例：5 份 Codex raw，两两相似度 0.995~0.998）。
    #
    # 判据 = 同来源 + 同规范化标题 + 正文 4-gram Jaccard 相似度 ≥ 0.95（近似逐字）。
    # 为什么不能只看「同来源」：§4.5.2 允许一份文档镜像成多个子文档 raw——实测
    #   TRAE 通识课 21 份、vibe-coding 19 份共享同一个 source_url，但正文互不相同，
    #   若按"同来源只能一份"判定，会把 40 份合法 raw 误报成错误。
    # 为什么还要比正文：`-1`/`-2` 后缀既可能是"重复件"也可能是"上篇/下篇"，
    #   文件名本身无法区分，只有内容相似度能分开（实测合法镜像对 0.01~0.09，
    #   重复件 0.99+，分离度极大，故 0.95 阈值极稳）。
    def _norm_source_url(u) -> str:
        return str(u).strip().split("#")[0].split("?")[0].rstrip("/")

    def _shingles(body_text: str, k: int = 4) -> frozenset:
        s = re.sub(r"\s+", "", body_text)
        s = re.sub(r"image_\d+", "IMG", s)  # 图片编号差异不算内容差异
        return frozenset(s[i:i + k] for i in range(max(0, len(s) - k + 1)))

    def _jaccard(a: frozenset, b: frozenset) -> float:
        return len(a & b) / len(a | b) if (a or b) else 0.0

    title_families: dict[tuple, list[str]] = defaultdict(list)
    for r in raw_files_data:
        sid, surl = r.get("source_id"), r.get("source_url")
        if sid and str(sid).strip():
            src_key = f"source_id={str(sid).strip()}"
        elif surl and str(surl).strip():
            src_key = f"source_url={_norm_source_url(surl)}"
        else:
            continue  # 无来源标识，无法判断是否同一篇文档，不参与
        stem = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", Path(r["rel"]).stem)
        title = re.sub(r"[-_]v?\d+$", "", stem)
        title_families[(src_key, title)].append(r["rel"])

    for (src_key, title), rels in title_families.items():
        if len(rels) < 2:
            continue
        shingle_sets: dict[str, frozenset] = {}
        for rel in rels:
            try:
                _, body_text = load_frontmatter(
                    (VAULT / rel).read_text(encoding="utf-8", errors="replace")
                )
            except OSError:
                continue
            shingle_sets[rel] = _shingles(body_text)
        cluster: set = set()
        best = 0.0
        for i in range(len(rels)):
            for j in range(i + 1, len(rels)):
                a, b = shingle_sets.get(rels[i]), shingle_sets.get(rels[j])
                if not a or not b:
                    continue
                jac = _jaccard(a, b)
                if jac >= 0.95:
                    cluster.update((rels[i], rels[j]))
                    best = max(best, jac)
        if cluster:
            findings["medium"].append(
                f"[raw:duplicate-source] 「{title}」有 {len(cluster)} 份近乎逐字重复的 raw"
                f"（{src_key}，两两最高相似度 {best:.1%}），疑似重复入库——保留 1 份、"
                f"其余移入 raw/_archive/；若确为 §4.5.4 的小幅改版请在 ADR 中说明:"
                f" {', '.join(sorted(cluster))}"
            )

    # ---- 14. 图片规范检测（§7.1：WebP 优先、大图强制压缩）----
    # 以聚合方式报告（逐张告警会淹没报告），仅提示不自动改——转码涉及画质取舍，需人工决定。
    if RAW.exists():
        non_webp: dict[str, int] = defaultdict(int)
        oversized: list[tuple[int, str]] = []
        for img in RAW.rglob("*"):
            if any(d in img.parts for d in SKIP_DIRS) or not img.is_file():
                continue
            ext = img.suffix.lower()
            if ext not in IMAGE_EXTS:
                continue
            try:
                size = img.stat().st_size
            except OSError:
                continue
            if ext in {".jpg", ".jpeg", ".png"}:
                non_webp[rel_posix(img.parent)] += 1
            if size > OVERSIZED_IMAGE_BYTES:
                oversized.append((size, rel_posix(img)))
        if non_webp:
            total_imgs = sum(non_webp.values())
            dirs = "、".join(f"{d or '.'}（{n} 张）" for d, n in sorted(non_webp.items(), key=lambda kv: -kv[1]))
            findings["medium"].append(
                f"[raw:image-not-webp] {total_imgs} 张图片非 WebP（§7.1 建议 WebP 优先），"
                f"分布 {len(non_webp)} 个目录: {dirs}"
                f" → 归一化：python tools/convert_to_webp.py --apply"
            )
        if oversized:
            oversized.sort(reverse=True)
            threshold_kb = OVERSIZED_IMAGE_BYTES // 1024
            top = ", ".join(f"{p}（{s / 1024 / 1024:.1f}MB）" for s, p in oversized[:5])
            more = f"，另有 {len(oversized) - 5} 张同类" if len(oversized) > 5 else ""
            findings["medium"].append(
                f"[raw:image-oversized] {len(oversized)} 张图片超过 {threshold_kb}KB（§7.1 大图强制压缩），"
                f"最大者: {top}{more}"
                f" → 归一化：python tools/convert_to_webp.py --apply"
            )
        # 长截图规范：宽高比 > 3.0 视作"长截图"（手机滚动截屏典型 1:4 ~ 1:20+），
        # 其宽度应 ≤ 1440（只限宽、不限高）。阈值取 3.0：横版幻灯片实测聚集在
        # 1:2.1~2.2，不会被误伤；真正的长截图比例远大于此。
        LONG_RATIO_LINT = 3.0
        LONG_MAX_W_LINT = 1440
        long_imgs: list[str] = []
        for img in RAW.rglob("*"):
            if any(d in img.parts for d in SKIP_DIRS) or not img.is_file():
                continue
            if img.suffix.lower() not in IMAGE_EXTS:
                continue
            try:
                from PIL import Image as _PILImage
                with _PILImage.open(img) as _im:
                    w, h = _im.size
            except Exception:
                continue
            if max(w, h) / max(min(w, h), 1) > LONG_RATIO_LINT and w > LONG_MAX_W_LINT:
                long_imgs.append(f"{rel_posix(img)}（{w}x{h}）")
        if long_imgs:
            findings["medium"].append(
                f"[raw:image-longshot-wide] {len(long_imgs)} 张长截图宽度超过 {LONG_MAX_W_LINT}"
                f"（长截图应只限宽、不限高，避免被压成不可读窄条）: "
                + "，".join(long_imgs[:5])
            )

    # ---- 15. 人工保护区规范（§5.5.1：仅 `## 我的思考` 一个章节受保护）----
    # 检查 wiki 页是否存在 `## 我的思考`，并确认它确实是"一个章节"而非缺失/重复。
    if WIKI.exists():
        for f in sorted(WIKI.rglob("*.md")):
            if f.name in {"index.md", "log.md"} or "queries" in f.parts:
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
            rel = rel_posix(f)
            n_thinking = len(re.findall(r"^## 我的思考\s*$", text, re.M))
            if n_thinking == 0:
                findings["low"].append(
                    f"[wiki:no-user-zone] {rel} 缺少 `## 我的思考` 保护区章节"
                    f"（§5.5.1，应在 `## 来源` 之前）"
                )
            elif n_thinking > 1:
                findings["medium"].append(
                    f"[wiki:dup-user-zone] {rel} 出现 {n_thinking} 个 `## 我的思考`，"
                    f"保护区应唯一（§5.5.1）"
                )
            # 保护区必须位于 `## 来源` 之前
            pos_t = text.find("## 我的思考")
            pos_s = text.find("## 来源")
            if pos_t >= 0 and pos_s >= 0 and pos_t > pos_s:
                findings["medium"].append(
                    f"[wiki:user-zone-order] {rel} 的 `## 我的思考` 位于 `## 来源` 之后，"
                    f"应置于其前（§5.5.1）"
                )

    # ---- 16. topics 拆分线提醒（§5.1：同领域 ≥5 建子文件夹）----
    # 为什么 lint 只数总数、不识别"领域"：领域归并是语义判断——
    # vibe-coding 算不算 ai-coding、信息债务算不算知识管理，正则只会又漏又错
    # （标签是自由文本、无受控词表，见 ADR-012）。这里只在根目录文件过多时
    # 提醒「该审视一次领域分布」；归并由 ingest 时的 AI 做（§9 摄入第 4 步），
    # 拆不拆由人拍板（走 ADR）。
    TOPICS_SPLIT_REVIEW_THRESHOLD = 15
    topics_dir = WIKI / "topics"
    if topics_dir.exists():
        root_pages = [p for p in topics_dir.glob("*.md") if p.is_file()]
        if len(root_pages) > TOPICS_SPLIT_REVIEW_THRESHOLD:
            findings["info"].append(
                f"[wiki:topics-split-review] wiki/topics/ 根目录已有 "
                f"{len(root_pages)} 个主题页（>{TOPICS_SPLIT_REVIEW_THRESHOLD}），"
                f"建议审视领域分布：某领域 ≥5 个时按 §5.1 拆分子文件夹并写 ADR"
            )

    return findings


def apply_fixes() -> list[str]:
    """自动修复 🟢 低风险项。当前支持：
    - 从文件名回填 raw 的 published_at（AGENTS.md §4.2 明确允许 lint 从文件名提取）
    - 为缺失保护区的 wiki 页插入空的 `## 我的思考` 占位章节（§5.5.1）
    """
    fixed = []
    if not RAW.exists():
        return fixed
    for f in RAW.rglob("*.md"):
        if any(d in f.parts for d in SKIP_DIRS):
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        fm, _ = load_frontmatter(text)
        if fm is None:
            continue
        pub = fm.get("published_at")
        if pub is not None and str(pub).strip() != "":
            continue
        fname_date = extract_filename_date(f.stem)
        if fname_date and fix_published_at(f, fname_date):
            fixed.append(f"[fix:published-at] {rel_posix(f)} ← 从文件名回填 {fname_date}")

    # 为缺少 `## 我的思考` 的 wiki 页插入空占位章节（放 `## 来源` 之前）
    if WIKI.exists():
        for f in sorted(WIKI.rglob("*.md")):
            if f.name in {"index.md", "log.md"} or "queries" in f.parts:
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
            if re.search(r"^## 我的思考\s*$", text, re.M):
                continue
            src = re.search(r"^## 来源[ \t]*\n", text, re.M)
            if not src:
                continue
            patched = (text[:src.start()]
                       + "## 我的思考\n\n## 来源\n\n"
                       + text[src.end():].lstrip("\n"))
            f.write_text(patched, encoding="utf-8")
            fixed.append(f"[fix:user-zone] {rel_posix(f)} ← 插入空的 `## 我的思考` 保护区")
    return fixed


def main():
    parser = argparse.ArgumentParser(description="knowledge-vault lint")
    parser.add_argument("--fix", action="store_true",
                        help="自动修复 🟢 低风险项（当前支持：从文件名回填 published_at）")
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    args = parser.parse_args()

    findings = collect_findings()
    fixes = apply_fixes() if args.fix else []

    if args.json:
        print(json.dumps({"findings": findings, "fixes": fixes}, ensure_ascii=False, indent=2))
    else:
        print(f"📋 knowledge-vault lint 报告（{VAULT}）")
        print("=" * 60)
        for level, emoji, title in [
            ("low", "🟢", "低风险（可 --fix 自动修复）"),
            ("medium", "🟡", "中风险（只报告，需人工处理）"),
            ("info", "ℹ️", "信息"),
        ]:
            items = findings[level]
            print(f"\n{emoji} {title}: {len(items)} 项")
            for i in items:
                print(f"   - {i}")
        if fixes:
            print(f"\n🔧 已自动修复: {len(fixes)} 项")
            for i in fixes:
                print(f"   - {i}")
        print("\n" + "=" * 60)
        total = sum(len(v) for v in findings.values())
        print(f"合计: {total} 项")

    return 0 if sum(len(v) for v in findings.values()) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

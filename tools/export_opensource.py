#!/usr/bin/env python3
"""
开源版本导出脚本 —— 白名单复制 + 脱敏 + 密钥扫描。

原则：
- 只读源目录，绝不修改/删除原始知识库的任何文件
- 白名单复制框架文件（AGENTS.md / lint.py / templates / reviews / tools 代码）
- 明确排除：raw/、wiki/ 私有内容、密钥配置、日志、备份、数据库、缓存
- 复制后执行脱敏替换与密钥扫描，发现疑似密钥则非零退出

用法：
    python3 tools/export_opensource.py            # 导出到 dist/knowledge-vault
    python3 tools/export_opensource.py --force    # 覆盖已有导出目录
    python3 tools/export_opensource.py --scan     # 只扫描当前目录，不导出
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist" / "knowledge-vault"

# ---- 白名单：只导出这些顶层文件/目录 ----
ALLOWED_TOP_LEVEL = {"README.md", "LICENSE", "requirements.txt",
                    "AGENTS.md", "lint.py", ".gitignore", "templates", "reviews", "tools"}

# ---- 复制时一律排除的目录名 ----
EXCLUDE_DIR_NAMES = {"__pycache__", "runtime", ".claude", ".codex", ".agents",
                     ".git", "dist", "node_modules", ".venv"}

# ---- 复制时排除的文件名模式 ----
EXCLUDE_FILE_PATTERNS = [
    "*.pyc", "*.pyo", "*.log", "*.bak", "*.bak-*",
    "*.db", "*.db-*", "*.sqlite", "*.sqlite-*",
    "*.cookie", "*.pem", "*.key",
    # 一切 `*.local.json` 都是本机覆盖配置，一律不进开源产物。
    #
    # 为什么用通配而不是逐个列名：原先只列了 `config.local.json`，于是
    # `wiki-config.local.json`（含真实 API Key）**会被复制进 dist**，虽有事后
    # 密钥扫描兜底，但产物已落盘、扫描失败才中止，等于把兜底当主防线。
    # 新增 `ocr-config.local.json` 时又踩到同一个洞 —— 改成通配后
    # 以后新增任何 `*.local.json` 都自动被排除。
    "*.local.json",               # clip-server token / wiki API Key / OCR 配置…
    "feishu-bot-config.json",     # 飞书真实 app_secret
]

# ---- 个人化配置：脱敏规则 + 额外排除项，全部外置到本地 JSON ----
# 公司名、本机路径、内部域名都属于私有信息。写死在脚本里会被导出流程
# 自己替换掉自己（规则退化成空操作），所以一律从 sanitize.local.json 读。
SANITIZE_CONFIG = ROOT / "tools" / "sanitize.local.json"
SANITIZE_EXAMPLE = ROOT / "tools" / "sanitize.example.json"

SANITIZE_RULES: list[tuple[re.Pattern, str]] = []
EXCLUDE_RELATIVE: set[str] = set()


def load_sanitize_config() -> None:
    """读入个人化脱敏规则与排除清单；缺失时留空并提示，其余功能照常。"""
    global SANITIZE_RULES, EXCLUDE_RELATIVE
    if not SANITIZE_CONFIG.exists():
        print(f"[!] 未找到 {SANITIZE_CONFIG.name}，本次不做自定义脱敏"
              f"（可复制 {SANITIZE_EXAMPLE.name} 自行填写）", file=sys.stderr)
        return
    try:
        data = json.loads(SANITIZE_CONFIG.read_text(encoding="utf-8"))
        SANITIZE_RULES = [(re.compile(item["pattern"]), item["repl"])
                          for item in data.get("sanitize_rules", [])]
    except (json.JSONDecodeError, KeyError, re.error) as exc:
        print(f"[!] {SANITIZE_CONFIG.name} 解析失败（{exc}），本次不做自定义脱敏",
              file=sys.stderr)
        SANITIZE_RULES = []
        return
    EXCLUDE_RELATIVE = set(data.get("exclude_files", []))
TEXT_SUFFIXES = {".md", ".py", ".json", ".sh", ".txt", ".html", ".js", ".yml", ".yaml"}

# ---- 导出目录 .gitignore 追加段：框架仓库不收录个人内容 ----
DIST_GITIGNORE_EXTRA = """
# ===== 个人知识内容（开源框架默认不收录，使用者自行入库）=====
raw/articles/*
raw/notes/*
raw/inbox/*
raw/_archive/*
wiki/topics/*
wiki/queries/*
!**/.gitkeep
"""

# ---- 导出后保留的空骨架目录 ----
SKELETON_DIRS = [
    "raw/articles", "raw/notes", "raw/inbox", "raw/_archive",
    "wiki/topics", "wiki/queries",
]

# ---- 密钥扫描规则（高置信度，占位符 xxx/替换文本不算）----
# 注意：分隔符用 [ \t] 而不是 \s ——真实的 key=value 不会跨行，而 \s 会让
# 关键字和下一行的普通单词连成假阳性，把整次导出误判成"发现密钥"中止。
#
# 每条规则是 (正则, 标签, 复核函数)。复核函数 `(snippet, line) -> bool`，
# 返回 True 才计入发现 —— 正则负责「宽」地圈出可疑片段，复核函数负责把它与
# 长得像但其实是**普通文本**的片段分开。
#
# 为什么要加复核：`sk-` 与 UUID 两条规则天生会撞上正常文本（CSS 类名
# `sk-section-title`、英文短语 `tTask-driven → sk-driven-autonomous-…`、文档里的
# 线程 UUID）。过去没有复核，导出每次都被中止，久而久之大家就学会无视这个
# 警报 —— 那才是真正的风险（警报的价值在于它一直可信）。
SECRET_WHITELIST = {"example.com"}

#: UUID 只有出现在**提到凭据的那一行**才算密钥。UUID 本身不是秘密 ——
#: 文档与正文里到处是线程 ID、页面 ID、追踪 ID，把它们一律算成密钥，
#: 只会让 `--scan` 每次都报红（实测 4 条全来自 `codex-handover-*.md` 的线程 ID）。
_CREDENTIAL_WORD_RE = re.compile(
    r"(?i)\b(api[_-]?key|apikey|secret|token|passwd|password|credential|authorization)\b")


def _is_real_api_key(snippet: str) -> bool:
    """把 `sk-` 开头的**真实 API Key** 与「长得像但其实是普通文本」分开。

    真实 key（OpenAI `sk-…`、Anthropic `sk-ant-api03-…`、网关自建 key）是
    **一整块随机串**：长度 ≥20、混有数字、连字符极少（只作前缀，如 `sk-proj-`）。

    误报则有两类，都来自正常内容：
      - CSS 类名 `sk-section-title` —— 纯字母且只有 13 字符；
      - 英文短语 `sk-driven-autonomous-agent-utilizing-gpt-4-pinec`
        （原文 "Task-driven autonomous agent…" 的单词边界正好落在 `sk-` 前）。

    三条判据（长度 / 含数字 / 连字符数）足以把这两类挡在外面，且不影响检出。
    """
    body = snippet[3:]                       # 去掉 "sk-"
    if len(body) < 20:
        return False
    if not any(c.isdigit() for c in body):
        return False
    return body.count("-") <= 3


def _uuid_in_credential_line(snippet: str, line: str) -> bool:
    """UUID 只有出现在**提到凭据的那一行**才算密钥（见上方 `_CREDENTIAL_WORD_RE`）。"""
    return bool(_CREDENTIAL_WORD_RE.search(line))


SECRET_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9_-]{12,}"), "API Key (sk-...)",
     lambda s, line: _is_real_api_key(s)),
    (re.compile(r"(?i)bearer[ \t]+[A-Za-z0-9._-]{16,}"), "Bearer Token", None),
    (re.compile(r"cli_[a-z0-9]{12,}"), "飞书 App ID", None),
    (re.compile(r"(?i)app_secret[ \t\"':=]+[A-Za-z0-9]{8,}"), "飞书 App Secret", None),
    (re.compile(r"access_key=[a-f0-9]{16,}"), "WebSocket access_key", None),
    (re.compile(r"\"token\":[ \t]*\"[A-Za-z0-9_-]{20,}\""), "配置文件 token", None),
    (re.compile(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}"),
     "UUID 形态密钥", _uuid_in_credential_line),
]
PLACEHOLDER_RE = re.compile(r"x{4,}|X{4,}|\*{4,}|_{4,}|你的|替换|已脱敏|已省略|<[^>]+>")


def is_placeholder(snippet: str) -> bool:
    return bool(PLACEHOLDER_RE.search(snippet))


def is_exportable(rel_parts: tuple[str, ...]) -> bool:
    """该路径（相对仓库根的各级组成部分）是否属于**导出收录范围**。

    `copy_tree()` 与 `scan_secrets()` 共用这一条判据。过去两者各判一次：
    导出只收录 `ALLOWED_TOP_LEVEL` 下的文件，而 `--scan` 用 `rglob("*")`
    扫了**整个仓库** —— 于是 `.workbuddy/`（工作记忆）、`raw/articles/`
    （个人剪藏正文）、`_trash/`、`runtime/` 里的每一次 `sk-` / UUID 命中
    都成了假警报，`--scan` 次次报红、导出被误中止。警报一旦不可信就没人看，
    真正的风险反而漏掉。改范围时**只改这一处**。
    """
    if not rel_parts:
        return False
    if rel_parts[0] not in ALLOWED_TOP_LEVEL:
        return False
    return not any(p in EXCLUDE_DIR_NAMES for p in rel_parts)


def is_excluded_file(rel: str, name: str) -> bool:
    if rel in EXCLUDE_RELATIVE:
        return True
    for pat in EXCLUDE_FILE_PATTERNS:
        if fnmatch_match(name, pat):
            return True
    return False


def fnmatch_match(name: str, pattern: str) -> bool:
    import fnmatch
    return fnmatch.fnmatch(name, pattern)


def sanitize_text(text: str) -> str:
    for rule, repl in SANITIZE_RULES:
        text = rule.sub(repl, text)
    return text


def copy_tree() -> tuple[int, int]:
    copied, sanitized = 0, 0
    for top in sorted(ALLOWED_TOP_LEVEL):
        src = ROOT / top
        if not src.exists():
            continue
        if src.is_file():
            dst = DIST / top
            dst.parent.mkdir(parents=True, exist_ok=True)
            n = copy_one(src, dst)
            copied += n
            if n:
                sanitized += 1
            continue
        for path in src.rglob("*"):
            if path.is_dir():
                continue
            rel = str(path.relative_to(ROOT))
            if not is_exportable(path.relative_to(ROOT).parts):
                continue
            if is_excluded_file(rel, path.name):
                continue
            dst = DIST / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if copy_one(path, dst):
                copied += 1
                sanitized += 1
    return copied, sanitized


def copy_one(src: Path, dst: Path) -> int:
    if src.suffix in TEXT_SUFFIXES:
        original = src.read_text(encoding="utf-8", errors="replace")
        cleaned = sanitize_text(original)
        # newline="\n"：强制 LF。否则在 Windows 上导出会整树写成 CRLF。
        dst.write_text(cleaned, encoding="utf-8", newline="\n")
        return 1
    shutil.copy2(src, dst)
    return 0


def _clear_writable_attributes(path: Path) -> None:
    """Windows 上 shutil.copy2 可能把源的只读/隐藏属性带过来，导致后续
    write_text 覆盖失败（PermissionError）。写之前清掉这两个位。"""
    try:
        import stat
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass
    if sys.platform == "win32":
        try:
            import ctypes
            attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
            if attrs != -1:
                # 清除 FILE_ATTRIBUTE_READONLY(0x1) 与 FILE_ATTRIBUTE_HIDDEN(0x2)
                ctypes.windll.kernel32.SetFileAttributesW(
                    str(path), attrs & ~0x1 & ~0x2)
        except Exception:
            pass


def add_skeleton():
    for dirpath in SKELETON_DIRS:
        d = DIST / dirpath
        d.mkdir(parents=True, exist_ok=True)
        keep = d / ".gitkeep"
        if not keep.exists():
            keep.write_text("", encoding="utf-8", newline="\n")
    gi = DIST / ".gitignore"
    text = gi.read_text(encoding="utf-8") if gi.exists() else ""
    _clear_writable_attributes(gi)
    if "个人知识内容" not in text:
        gi.write_text(text.rstrip() + "\n" + DIST_GITIGNORE_EXTRA,
                      encoding="utf-8", newline="\n")


def scan_secrets(base: Path) -> list[str]:
    findings = []
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in TEXT_SUFFIXES:
            continue  # 二进制不扫描
        try:
            parts = path.relative_to(base).parts
        except ValueError:
            continue
        if not is_exportable(parts):
            # 只扫**会进产物**的文件：.workbuddy/（工作记忆）、raw/articles/
            # （个人剪藏）、_trash/、runtime/ 都不在收录范围内，扫它们只会让
            # `--scan` 永远报红 —— 久而久之大家就学会无视这个警报，
            # 那才是真正的风险。范围判据与 copy_tree() 共用 is_exportable()。
            continue
        rel = str(path.relative_to(base))
        if is_excluded_file(rel, path.name):
            # 被排除的文件（*.local.json / 日志 / 任务库…）同样不会进产物。
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern, label, review in SECRET_PATTERNS:
            for match in pattern.finditer(text):
                snippet = match.group(0)
                if any(w in snippet for w in SECRET_WHITELIST) or is_placeholder(snippet):
                    continue
                line_start = text.rfind("\n", 0, match.start()) + 1
                line_end = text.find("\n", match.end())
                line = text[line_start: line_end if line_end != -1 else len(text)]
                # 复核函数负责把「长得像密钥的普通文本」挡在外面（CSS 类名 /
                # 英文短语 / 文档里的业务 UUID）—— 详见 SECRET_PATTERNS 上方注释。
                if review is not None and not review(snippet, line):
                    continue
                line_no = text[:match.start()].count("\n") + 1
                findings.append(f"  [{label}] {rel}:{line_no} → {snippet[:48]}")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="导出 knowledge-vault 开源版本")
    parser.add_argument("--force", action="store_true", help="覆盖已有导出目录")
    parser.add_argument("--scan", action="store_true", help="只扫描源目录的密钥，不导出")
    args = parser.parse_args()

    if args.scan:
        findings = scan_secrets(ROOT)
        if findings:
            print("⚠️  当前目录发现疑似密钥：")
            print("\n".join(findings))
            return 1
        print("✅ 未发现高置信度密钥特征（仍需人工复核）")
        return 0

    load_sanitize_config()

    # saved_git 必须在分支外初始化：DIST 不存在时（首次导出 / 上次被清空）
    # 下面的 `if saved_git is not None` 会读到未绑定的局部变量并抛
    # UnboundLocalError，而此时 DIST 刚建好还是空的 —— 用户看到的是
    # 「导出失败 + dist 空目录」，比不导出更迷惑。2026-10-21 实际踩到。
    saved_git = None
    git_dir = DIST / ".git"

    if DIST.exists():
        if not args.force:
            print(f"[!] 导出目录已存在：{DIST}（加 --force 覆盖）", file=sys.stderr)
            return 2
        # 若 DIST 已是 git 仓库（用户在此 git init 后推远程），--force 重导出时
        # 必须保留 .git，否则每次导出都会把版本库连根删掉。先把 .git 移到临时
        # 目录，rmtree 重建后再移回；DIST 内没有 .git 时此分支为空操作。
        if git_dir.is_dir():
            import tempfile
            saved_git = Path(tempfile.mkdtemp(prefix="kv-dist-git-")) / ".git"
            shutil.move(str(git_dir), str(saved_git))
        shutil.rmtree(DIST)
    DIST.mkdir(parents=True)

    # 把暂存的 .git 移回（无 .git 时 saved_git 为 None，跳过）
    if saved_git is not None:
        shutil.move(str(saved_git), str(git_dir))
        try:
            saved_git.parent.rmdir()
        except OSError:
            pass

    copied, _ = copy_tree()
    add_skeleton()

    findings = scan_secrets(DIST)
    print(f"[*] 已复制 {copied} 个框架文件 → {DIST}")
    if findings:
        print("\n🔴 导出产物中发现疑似密钥，已中止（请勿发布）：")
        print("\n".join(findings))
        return 1
    print("✅ 密钥扫描通过（0 命中）")
    print(f"[*] 下一步：检查 {DIST} 内容，确认无误后在该目录 git init 并发布")
    return 0


if __name__ == "__main__":
    sys.exit(main())

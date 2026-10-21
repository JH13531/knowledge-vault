"""
wiki_gen.py — 后台 wiki 精加工模块（三级回退）

Tier 1: 本机 Agent CLI（claude / codex）。Agent 自主读取 AGENTS.md、raw
        全文并浏览已有 topics，直接读写文件，交叉引用质量最高。
Tier 2: OpenAI 兼容 LLM API 直出（需 api key）。上下文组装与文件读写由
        Python 完成，质量略低于 CLI 但无需安装 Agent。
Tier 3: 两者都不可用 → 跳过。raw 正常保存为孤儿（wiki_refs 为空），
        用户日后可手动提炼。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import date
from pathlib import Path

import requests
import yaml

from no_window import NO_WINDOW

VAULT = Path(__file__).resolve().parent.parent
TOPICS_DIR = VAULT / "wiki" / "topics"
WIKI_CONFIG_PATH = VAULT / "tools" / "wiki-config.local.json"

CLI_TIMEOUT = 300
API_TIMEOUT = 180
RELATED_CONTEXT_BUDGET = 24000


# ========== AI 输出脱敏：后处理替换敏感信息 ==========

_AI_SENSITIVE_PATTERNS = [
    (re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}'), '[邮箱已脱敏]'),
    (re.compile(r'(?<!\d)1[3-9]\d{9}(?!\d)'), '[手机号已脱敏]'),
    (re.compile(r'(?<!\d)\d{17}[\dXx](?!\d)'), '[身份证已脱敏]'),
    (re.compile(r'(?:sk-|Bearer\s+)[A-Za-z0-9_\-]{20,}'), '[密钥已脱敏]'),
    (re.compile(r'((?:api[_-]?key|token|secret|password)["\s:=]+)["\']?[A-Za-z0-9_\-]{16,}["\']?', re.I),
     r'\1[已脱敏]'),
]


def redact_sensitive(text: str) -> str:
    """替换文本中可能遗漏的个人敏感信息（prompt 指令脱敏的兜底）。"""
    for pattern, replacement in _AI_SENSITIVE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _read_frontmatter(text: str) -> dict:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        return {}
    try:
        return yaml.safe_load(m.group(1)) or {}
    except Exception:
        return {}


# ========== Tier 1：Agent CLI 提供商 ==========

CLI_SYSTEM_PROMPT = """你是知识库管理员。你的任务是将原始文章提炼为 wiki 主题页。

工作流程：
1. 先读取 AGENTS.md，重点关注 §5（Wiki 层规则）
2. 读取指定的 raw 文件
3. 浏览 wiki/topics/ 目录，阅读几个可能相关的页面
4. 按照 AGENTS.md 的规则生成 wiki 主题页
5. 写入 wiki/topics/ 目录
6. 回填 raw 文件的 wiki_refs 字段
7. 更新 wiki/index.md 与 wiki/log.md

关键规则提醒：
- 交叉引用（§5.3.1）：只引用有因果/互补/层级关系的页面，宁缺毋滥
- 双向溯源（§5.4）：wiki 末尾必须有 `## 来源` 回链 raw
- 人工编辑保护（§5.5.1）：更新已有页面时，仅 `## 我的思考` **章节本身**（至下一个 ## 标题为止）不得修改；`## 来源` 属普通区，需正常维护
- frontmatter 必须包含：title, type: topic, created, updated, last_verified, tags, contradicts

安全规则（必须遵守）：
- 不要在 wiki 输出中包含任何邮箱地址、手机号、身份证号、API Key、Token、密码
- 如果原文包含此类个人信息，用 [已脱敏] 替代
- 保留原文的技术内容和知识价值，只去掉个人敏感信息
- 不要输出原文中的凭据、密钥或私人联系方式"""


def _cli_task_prompt(raw_filepath: Path) -> str:
    raw_rel = raw_filepath.relative_to(VAULT).as_posix()
    return f"""请为以下 raw 文件生成 wiki 主题页：

raw 文件路径：{raw_rel}

请按照 AGENTS.md §5 的规则完成：
1. 读取 AGENTS.md 了解完整规则
2. 读取 {raw_rel} 获取文章内容
3. 浏览 wiki/topics/ 找到可交叉引用的页面（读取内容判断关联性）
4. 生成 wiki/topics/ 下的主题页（文件名用英文关键词或拼音，如 rag-retrieval-augmented-generation.md）
5. 回填 {raw_rel} 的 wiki_refs 字段，加入新 wiki 页的路径
6. 更新 wiki/index.md 和 wiki/log.md

注意：
- 如果 wiki/topics/ 已有同主题的页面，更新它而不是创建新页面，并保留其 `## 我的思考`
- 交叉引用要精准：只引用真正有因果/互补关系的页面
- 每个引用附一句话说明关联性"""


class CliProvider:
    name = "cli"
    binary = ""

    def available(self) -> bool:
        # 注意：不能写 `self.executable() is not None` ——
        # executable() 找不到时返回的是空字符串而非 None，`"" is not None` 恒为 True，
        # 会让 available() 永远为真（未安装 claude/codex 也误报可用，
        # 于是每次都要白跑一次注定失败的子进程，并且无法区分「没装」与「PATH 找不到」）。
        return bool(self.executable())

    def executable(self) -> str:
        return shutil.which(self.binary) or ""

    def run(self, system_prompt: str, task_prompt: str) -> bool:
        raise NotImplementedError


def _run_cli(cmd: list[str]) -> subprocess.CompletedProcess | None:
    # Windows：npm 安装的 agent 是 .cmd 批处理，需经 cmd.exe 执行
    if os.name == "nt" and cmd and cmd[0].lower().endswith((".cmd", ".bat")):
        cmd = [os.environ.get("COMSPEC", "cmd.exe"), "/c", *cmd]
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            cwd=str(VAULT), timeout=CLI_TIMEOUT,
            # 没有这个，每次跑 claude/codex 都会弹出一个 cmd 窗口 ——
            # 而 wiki 精加工是「完整入库」的必经步骤，用户会以为程序出问题了。
            creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        print(f"[!] wiki_gen: CLI 超时（{CLI_TIMEOUT}s）: {' '.join(cmd[:2])}")
    except Exception as e:
        print(f"[!] wiki_gen: CLI 异常: {e}")
    return None


class ClaudeProvider(CliProvider):
    name = "claude"
    binary = "claude"

    def run(self, system_prompt: str, task_prompt: str) -> bool:
        cmd = [
            self.executable(), "-p",
            "--output-format", "json",
            "--allowedTools", "Read,Write,Edit,Glob,Grep",
            "--max-budget-usd", "1.0",
            "--system-prompt", system_prompt,
            task_prompt,
        ]
        result = _run_cli(cmd)
        if result is None or result.returncode != 0:
            if result is not None and result.stderr:
                print(f"[!] wiki_gen: claude 返回码 {result.returncode}: {result.stderr[:200]}")
            return False
        try:
            data = json.loads(result.stdout)
            if data.get("is_error"):
                print(f"[!] wiki_gen: claude 报告错误: {str(data.get('result'))[:200]}")
                return False
        except json.JSONDecodeError:
            return bool(result.stdout.strip())
        return True


class CodexProvider(CliProvider):
    name = "codex"
    binary = "codex"

    def run(self, system_prompt: str, task_prompt: str) -> bool:
        with tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=False) as f:
            last_msg_path = f.name
        prompt = system_prompt + "\n\n" + task_prompt
        cmd = [
            self.executable(), "exec", "--json",
            "--skip-git-repo-check",
            "-C", str(VAULT),
            "-s", "workspace-write",
            "--approve-for-me",
            "-o", last_msg_path,
            prompt,
        ]
        try:
            result = _run_cli(cmd)
            if result is None or result.returncode != 0:
                if result is not None and result.stderr:
                    print(f"[!] wiki_gen: codex 返回码 {result.returncode}: {result.stderr[:200]}")
                return False
            return True
        finally:
            try:
                os.unlink(last_msg_path)
            except OSError:
                pass


TIER1_PROVIDERS = [ClaudeProvider, CodexProvider]


# ========== Tier 2：OpenAI 兼容 API 直出 ==========

def api_config() -> dict | None:
    """解析 Tier 2 配置：环境变量优先，其次 tools/wiki-config.local.json。"""
    api_key = os.environ.get("WIKI_GEN_API_KEY", "")
    base_url = os.environ.get("WIKI_GEN_BASE_URL", "")
    model = os.environ.get("WIKI_GEN_MODEL", "")

    if not api_key and WIKI_CONFIG_PATH.exists():
        try:
            cfg = json.loads(WIKI_CONFIG_PATH.read_text(encoding="utf-8"))
            api_key = api_key or cfg.get("api_key", "")
            base_url = base_url or cfg.get("base_url", "")
            model = model or cfg.get("model", "")
        except Exception as e:
            print(f"[!] wiki_gen: 配置文件解析失败: {e}")

    if not api_key or not str(api_key).strip():
        return None

    return {
        "api_key": str(api_key).strip(),
        "base_url": (base_url or "https://api.openai.com/v1").rstrip("/"),
        "model": model or "gpt-4o-mini",
    }


API_SYSTEM_PROMPT = """你是知识库管理员，将 raw 文章提炼为 wiki 主题页。
严格返回一个 JSON 对象，不要输出 JSON 以外的任何文字。

内容规则（来自治理契约 AGENTS.md §5）：
- 用 Markdown，以 `##` 分区：摘要、定义/背景、核心内容、关键要点、相关页面、我的思考、来源
- 交叉引用只保留有因果/互补/层级/实践关系的页面，2-4 个，每个附一句话理由，
  使用 `[[wiki/topics/xxx.md]]` 形式；宁缺毋滥
- 必须包含空的 `## 我的思考` 占位章节（人工保护区，不要填写内容）
- `## 来源` 章节必须列出 `[[RAW_PLACEHOLDER]]`
- 文件名用英文关键词或拼音，下划线/连字符分隔；若已有同主题页面，返回已有文件名

安全规则：不得包含邮箱、手机号、身份证号、API Key、Token、密码；
遇到此类信息用 `[已脱敏]` 替代。

返回字段：
- filename: 相对 wiki/topics/ 的路径，如 `rag.md` 或 `ai-coding/rag.md`
- title: 主题标题
- tags: 3-6 个主题标签
- summary: 一句话摘要，60 字以内（用于索引）
- content: 完整页面正文（不含 frontmatter），以 `# 标题` 开头，以 `## 来源` 章节结尾"""


def _topic_catalog() -> str:
    if not TOPICS_DIR.exists():
        return "（暂无主题页）"
    lines = []
    for page_path in sorted(TOPICS_DIR.rglob("*.md")):
        title = ""
        try:
            for line_text in page_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if line_text.startswith("# "):
                    title = line_text[2:].strip()
                    break
        except Exception:
            pass
        rel = page_path.relative_to(VAULT).as_posix()
        lines.append(f"- {rel} — {title}")
    return "\n".join(lines) or "（暂无主题页）"


def _related_topics_content(tags: list) -> str:
    if not tags or not TOPICS_DIR.exists():
        return ""
    chunks = []
    used = 0
    tag_set = set(tags)
    for page_path in sorted(TOPICS_DIR.rglob("*.md")):
        try:
            page_text = page_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        page_tags = set(_read_frontmatter(page_text).get("tags") or [])
        if not (tag_set & page_tags):
            continue
        rel = page_path.relative_to(VAULT).as_posix()
        body = page_text
        if used + len(body) > RELATED_CONTEXT_BUDGET:
            body = body[:RELATED_CONTEXT_BUDGET - used]
        chunks.append(f"### {rel}\n\n{body}")
        used += len(body)
        if used >= RELATED_CONTEXT_BUDGET:
            break
    return "\n\n".join(chunks)


def _parse_json_object(text: str) -> dict:
    parsed = text.strip()
    if parsed.startswith("```"):
        parsed = re.sub(r"^```(?:json)?\s*|\s*```$", "", parsed).strip()
    return json.loads(parsed[parsed.find("{"):parsed.rfind("}") + 1])


def _yaml_quoted(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _backfill_wiki_refs(raw_filepath: Path, wiki_rel: str):
    raw_text = raw_filepath.read_text(encoding="utf-8")
    if wiki_rel in raw_text:
        return
    empty_refs = re.search(r"^wiki_refs:\s*\[\s*\]\s*$", raw_text, re.M)
    if empty_refs:
        replacement = f"wiki_refs:\n  - {wiki_rel}"
        raw_filepath.write_text(
            raw_text[:empty_refs.start()] + replacement + raw_text[empty_refs.end():],
            encoding="utf-8",
        )
        return
    list_refs = re.search(r"^(?:wiki_refs:\n(?:[ \t]+-[ \t]+.+\n)+)", raw_text, re.M)
    if list_refs:
        block = list_refs.group(0).rstrip("\n") + f"\n  - {wiki_rel}\n"
        raw_filepath.write_text(
            raw_text[:list_refs.start()] + block + raw_text[list_refs.end():],
            encoding="utf-8",
        )
        return
    print("[!] wiki_gen: 无法回填 wiki_refs（字段格式未识别）")


def _merge_user_region(new_content: str, old_content: str, raw_rel: str) -> str:
    """更新已有页面：只保留旧页面的 `## 我的思考` 章节（§5.5.1）。

    保护区仅限 `## 我的思考` 标题到下一个同级 `##` 标题之间；
    `## 来源` 属普通区，由下方逻辑正常维护（保证 AI 能回链来源）。
    """
    protected = re.search(r"^## 我的思考\s*$", old_content, re.M)
    if not protected:
        return new_content
    # 保护区起点 = 我的思考标题；终点 = 其后的下一个 ## 标题（或文件末尾）
    rest = old_content[protected.start():]
    nxt = re.search(r"^## (?!我的思考)", rest[len("## 我的思考"):], re.M)
    if nxt:
        user_region = rest[:len("## 我的思考") + nxt.start()].rstrip() + "\n"
    else:
        user_region = rest.rstrip() + "\n"

    # 新内容中，去掉自带的 我的思考 占位段，避免重复
    cut = re.search(r"^## 我的思考\s*$", new_content, re.M)
    if cut:
        tail = new_content[cut.start():]
        tail_next = re.search(r"^## (?!我的思考)", tail[len("## 我的思考"):], re.M)
        end = cut.start() + len("## 我的思考") + tail_next.start() if tail_next else len(new_content)
        head = new_content[:cut.start()].rstrip() + "\n"
        new_tail = new_content[end:]
    else:
        head = new_content.rstrip() + "\n"
        new_tail = ""

    merged = head + "\n" + user_region
    if new_tail.strip():
        merged = merged.rstrip() + "\n\n" + new_tail.lstrip()

    # 来源列表需累积：旧页面的 raw 回链不能因重写而丢失
    old_sources = _extract_source_links(old_content)
    merged = _ensure_source_links(merged, old_sources)
    if f"[[{raw_rel}]]" not in merged:
        merged = _ensure_source_links(merged, ["[[%s]]" % raw_rel])
    return merged


def _extract_source_links(content: str) -> list[str]:
    """提取 `## 来源` 章节里的所有 [[...]] 回链（去重保序）。"""
    m = re.search(r"^## 来源\s*$(.*)$", content, re.M | re.DOTALL)
    if not m:
        return []
    links, seen = [], set()
    for link in re.findall(r"\[\[[^\]]+\]\]", m.group(1)):
        if link not in seen:
            seen.add(link)
            links.append(link)
    return links


def _ensure_source_links(content: str, links: list[str]) -> str:
    """确保 `## 来源` 存在且包含全部给定回链（缺则追加）。"""
    src = re.search(r"^## 来源\s*$", content, re.M)
    if not src:
        if not links:
            return content
        body = "\n".join("- " + l for l in links)
        return content.rstrip() + f"\n\n## 来源\n\n{body}\n"
    # 逐条补齐缺失的回链，追加到该章节末尾（下一个 ## 之前）
    tail = content[src.end():]
    nxt = re.search(r"^## ", tail, re.M)
    region = tail[:nxt.start()] if nxt else tail
    after = tail[nxt.start():] if nxt else ""
    for link in links:
        if link not in content:
            region = region.rstrip() + f"\n- {link}\n"
    return content[:src.end()] + region + after


def _ensure_required_sections(content: str, raw_rel: str) -> str:
    if f"[[{raw_rel}]]" not in content:
        if "## 来源" in content:
            content = content.replace(
                "## 来源\n", f"## 来源\n\n- [[{raw_rel}]]\n", 1)
        else:
            content += f"\n\n## 来源\n\n- [[{raw_rel}]]\n"
    if "## 我的思考" not in content:
        sources = re.search(r"^## 来源", content, re.M)
        if sources:
            content = content[:sources.start()] + "## 我的思考\n\n" + content[sources.start():]
    return content


def _insert_index_bullet(filename: str, title: str, summary: str):
    index_path = VAULT / "wiki" / "index.md"
    if not index_path.exists():
        return
    index_text = index_path.read_text(encoding="utf-8")
    heading = "### AI Coding（子目录 `wiki/topics/ai-coding/`）" \
        if filename.startswith("ai-coding/") else "### 非 AI 主题"
    lines = index_text.splitlines(keepends=True)
    try:
        heading_at = next(k for k, line in enumerate(lines) if line.strip() == heading)
    except StopIteration:
        return
    insert_at = heading_at + 1
    while insert_at < len(lines) and not lines[insert_at].startswith("#"):
        insert_at += 1
    wiki_rel = f"wiki/topics/{filename}"
    lines[insert_at:insert_at] = [f"- [{title}]({wiki_rel}) — {summary}\n"]
    index_path.write_text("".join(lines), encoding="utf-8")


def _prepend_log_entry(filename: str, title: str):
    log_path = VAULT / "wiki" / "log.md"
    if not log_path.exists():
        return
    log_text = log_path.read_text(encoding="utf-8")
    today = date.today().isoformat()
    entry = (f"## {today} — API 直出自动提炼：{title}\n\n"
             f"- 新增主题页 `wiki/topics/{filename}`（Tier 2，未安装 Agent CLI）\n\n")
    first_entry = re.search(r"^## ", log_text, re.M)
    pos = first_entry.start() if first_entry else len(log_text)
    log_path.write_text(log_text[:pos] + entry + log_text[pos:], encoding="utf-8")


def generate_via_api(raw_filepath: Path) -> bool:
    cfg = api_config()
    if cfg is None:
        return False

    raw_text = raw_filepath.read_text(encoding="utf-8", errors="replace")
    raw_rel = raw_filepath.relative_to(VAULT).as_posix()
    raw_tags = _read_frontmatter(raw_text).get("tags") or []

    system_prompt = API_SYSTEM_PROMPT.replace("RAW_PLACEHOLDER", raw_rel)
    user_prompt = f"""请提炼以下 raw 文件。

## 现有主题页目录（判断重名与交叉引用用）
{_topic_catalog()}

## 可能相关的已有页面全文（可无）
{_related_topics_content(raw_tags) or '（无）'}

## Raw 文件：{raw_rel}

{raw_text}

严格返回 JSON 对象，字段：filename, title, tags, summary, content。"""

    try:
        response = requests.post(
            f"{cfg['base_url']}/chat/completions",
            headers={
                "Authorization": f"Bearer {cfg['api_key']}",
                "Content-Type": "application/json",
            },
            json={
                "model": cfg["model"],
                "temperature": 0.3,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            },
            timeout=API_TIMEOUT,
        )
        response.raise_for_status()
        message = response.json()["choices"][0]["message"]["content"]
        page = _parse_json_object(message)
    except requests.exceptions.RequestException as e:
        print(f"[!] wiki_gen: API 请求失败: {e}")
        global _TIER2_LAST_ERROR
        _TIER2_LAST_ERROR = _describe_api_error(e)
        return False
    except (json.JSONDecodeError, KeyError) as e:
        print(f"[!] wiki_gen: API 返回解析失败: {e}")
        _TIER2_LAST_ERROR = f"API 返回内容解析失败（{type(e).__name__}）"
        return False

    filename = str(page.get("filename", "")).strip().lstrip("/")
    if not filename or not re.fullmatch(r"[A-Za-z0-9_./\-]+", filename):
        print("[!] wiki_gen: API 返回了非法 filename")
        return False
    if ".." in filename:
        return False
    if not filename.endswith(".md"):
        filename += ".md"

    content = str(page.get("content", "")).strip()
    if not content:
        print("[!] wiki_gen: API 返回空 content")
        return False

    target_path = TOPICS_DIR / filename
    if target_path.exists():
        content = _merge_user_region(
            content, target_path.read_text(encoding="utf-8", errors="replace"), raw_rel)
    else:
        content = _ensure_required_sections(content, raw_rel)
    content = redact_sensitive(content)

    title = str(page.get("title", "")).strip()
    if not title:
        title_match = re.search(r"^# (.+)$", content, re.M)
        title = title_match.group(1).strip() if title_match else "主题页"
    page_tags = page.get("tags") or raw_tags[:3] or ["未分类"]
    summary = str(page.get("summary", "")).strip() or title

    today = date.today().isoformat()
    frontmatter = (
        "---\n"
        f"title: {_yaml_quoted(title)}\n"
        "type: topic\n"
        f"created: {today}\n"
        f"updated: {today}\n"
        f"last_verified: {today}\n"
        "source_count: 1\n"
        f"tags: {json.dumps(list(page_tags), ensure_ascii=False)}\n"
        "contradicts: []\n"
        "---\n\n"
    )

    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(frontmatter + content.rstrip() + "\n", encoding="utf-8")

    wiki_rel = f"wiki/topics/{filename}"
    _backfill_wiki_refs(raw_filepath, wiki_rel)
    _insert_index_bullet(filename, title, summary)
    _prepend_log_entry(filename, title)
    print(f"[+] wiki 精加工完成（Tier 2 API 直出）: {wiki_rel}")
    return True


def _redact_wiki_output():
    """对最近 5 分钟内修改的 wiki 文件执行脱敏（Tier 1 CLI 输出兜底）。"""
    import time
    if not TOPICS_DIR.exists():
        return
    threshold = time.time() - 300
    for md_file in TOPICS_DIR.rglob("*.md"):
        try:
            if md_file.stat().st_mtime < threshold:
                continue
            content = md_file.read_text(encoding="utf-8", errors="replace")
            redacted = redact_sensitive(content)
            if redacted != content:
                md_file.write_text(redacted, encoding="utf-8")
                print(f"[*] wiki 脱敏已应用: {md_file.relative_to(VAULT)}")
        except Exception:
            continue


# ===== wiki 生成结果登记 =====
#
# 为什么需要：`queue_wiki_generation()` 是**后台线程**，调用方（clip-server）
# 写完 raw 就立刻回「入库成功」了。于是 API key 额度用尽 / 模型名不符 / 网络不通
# 这些失败**只打印在服务端控制台**，客户端一无所知 —— 等于对用户谎报成功。
# 这里把每次生成的结果按 raw 文件名登记下来，供 clip-server 如实回报。

_WIKI_RESULTS: dict[str, dict] = {}
_WIKI_RESULTS_ORDER: list[str] = []
_WIKI_RESULTS_LOCK = threading.Lock()
_WIKI_RESULTS_MAX = 200

#: Tier 2 最近一次调用的失败原因（供上报用，避免只 print 不落账）
_TIER2_LAST_ERROR = ""


def _describe_api_error(e: Exception) -> str:
    """把 API 异常整理成一句**可读**原因（尽量取出服务端返回的 code / message）。

    典型场景：额度用尽会返回
        {"code":"AccountQuotaExceeded","message":"You have exceeded the 5-hour
         usage quota. It will reset at 2026-10-09 19:08:21 +0800 CST. ..."}
    只 print 的话客户端看不到，所以这里把它压成一句供上报。

    对「额度用尽」这类**用户需要据此行动**的错误，额外做两件事：
      ① 译成中文结论（用户是中文用户，英文原文照抄只会更难看懂）；
      ② 若报文里带重置时间（reset at ...），把它单独摘出来 —— 这是用户
         唯一能采取的动作就是「等到那个时间点再试」，必须让他一眼看到。
    原文另存 `detail` 供排查，不再整段塞进用户可见文案。
    """
    resp = getattr(e, "response", None)
    if resp is None:
        return f"{type(e).__name__}: {e}"
    code = getattr(resp, "status_code", "")
    detail = ""
    err_code = ""
    try:
        body = resp.text or ""
    except Exception:
        body = ""
    if body:
        try:
            j = json.loads(body)
            if isinstance(j, dict):
                err = j.get("error")
                if isinstance(err, dict):
                    detail = str(err.get("message") or err.get("code") or "")
                    err_code = str(err.get("code") or "")
                if not detail:
                    detail = str(j.get("message") or j.get("code") or "")
        except Exception:
            detail = body
    detail = " ".join(str(detail).split())

    # 额度用尽：给一句中文结论 + 重置时间（能用得上、能据此行动的那一条）。
    if "quota" in err_code.lower() or "quota" in detail.lower() or code == 429:
        reset = ""
        m = re.search(
            r"reset(?:s)?\s+at\s+([0-9]{4}-[0-9]{2}-[0-9]{2}\s+[0-9:]+(?:\s*[+-][0-9:]+)?)",
            detail)
        if m:
            reset = m.group(1).strip()
        window = ""
        mw = re.search(r"exceeded\s+the\s+([0-9]+-hour|[a-z]+)\s+usage\s+quota", detail)
        if mw:
            window = mw.group(1)
        parts = ["模型 API 额度已用尽"]
        if window:
            parts.append(f"（{window} 滚动额度）")
        if reset:
            parts.append(f"，将于 {reset} 重置，届时可重试")
        else:
            parts.append("，请等待额度重置或升级套餐后重试")
        return "".join(parts)

    return f"HTTP {code}" + (f"：{detail[:160]}" if detail else "")


def _record_wiki_result(name: str, status: str, reason: str = "", tier: str = "") -> None:
    """登记一次 wiki 生成结果。status: pending / ok / failed / skipped。"""
    with _WIKI_RESULTS_LOCK:
        _WIKI_RESULTS[name] = {
            "status": status,
            "reason": reason,
            "tier": tier,
            "at": time.time(),
        }
        _WIKI_RESULTS_ORDER.append(name)
        while len(_WIKI_RESULTS_ORDER) > _WIKI_RESULTS_MAX:
            _WIKI_RESULTS.pop(_WIKI_RESULTS_ORDER.pop(0), None)


def record_wiki_skipped(raw_filepath, reason: str) -> dict:
    """登记「没过质量门禁、未生成 wiki」的结果，返回结果字典。"""
    name = Path(raw_filepath).name
    _record_wiki_result(name, "skipped", reason)
    return wiki_result_for(name) or {"status": "skipped", "reason": reason, "tier": ""}


def wiki_result_for(name: str) -> dict | None:
    """取某个 raw 文件最近一次 wiki 生成结果（取不到返回 None）。"""
    with _WIKI_RESULTS_LOCK:
        r = _WIKI_RESULTS.get(name)
        return dict(r) if r else None


def is_available() -> bool:
    """是否有任意一级 wiki 精加工能力可用。"""
    if any(provider().available() for provider in TIER1_PROVIDERS):
        return True
    return api_config() is not None


def generate_wiki_topic(raw_filepath: Path, raw_content: str = "") -> bool:
    """同步执行三级回退，生成 wiki 主题页。成功返回 True。

    结果（成功 / 失败 / 原因）会登记到 `wiki_result_for(文件名)`，
    供 clip-server 把「wiki 到底生成了没有」如实回报给客户端 ——
    否则失败只留在服务端控制台，客户端一律看到「入库成功」。
    """
    global _TIER2_LAST_ERROR
    raw_filepath = Path(raw_filepath)
    name = raw_filepath.name

    for provider_cls in TIER1_PROVIDERS:
        provider = provider_cls()
        if not provider.available():
            continue
        print(f"[*] wiki 精加工启动（Tier 1: {provider.name}）: {raw_filepath.name}")
        try:
            ok = provider.run(CLI_SYSTEM_PROMPT, _cli_task_prompt(raw_filepath))
        except Exception as e:
            print(f"[!] wiki_gen: {provider.name} 异常: {e}")
            ok = False
        if ok:
            print(f"[+] wiki 精加工完成（Tier 1: {provider.name}）")
            _redact_wiki_output()
            _record_wiki_result(name, "ok", "", f"tier1:{provider.name}")
            return True
        print(f"[i] {provider.name} 未成功，尝试下一个精加工通道…")

    tier2_configured = api_config() is not None
    tier2_failed = False
    if tier2_configured:
        print("[*] wiki 精加工启动（Tier 2: API 直出）")
        _TIER2_LAST_ERROR = ""
        try:
            if generate_via_api(raw_filepath):
                _record_wiki_result(name, "ok", "", "tier2:api")
                return True
            tier2_failed = True   # 配置了但没成功（generate_via_api 内部已打印原因）
        except Exception as e:
            print(f"[!] wiki_gen: API 直出异常: {e}")
            _TIER2_LAST_ERROR = f"{type(e).__name__}: {e}"
            tier2_failed = True

    # 文案要区分「没配」和「配了但失败」，否则会自相矛盾
    #（此前 Tier 2 已配置却调用失败时，这里仍打印「未配置 API key」）
    if tier2_failed:
        print("[i] Tier 2 已配置但调用失败 —— 原因见上方错误行（常见：额度用尽 / 模型名不符 / 网络）。")
        print("[i] raw 已保存，可稍后重试，或按 AGENTS.md 手动提炼。")
        _record_wiki_result(
            name, "failed",
            _TIER2_LAST_ERROR or "Tier 2 API 调用失败（常见：额度用尽 / 模型名不符 / 网络）",
            "tier2:api",
        )
    else:
        print("[i] 无可用的 wiki 精加工通道：PATH 中找不到 claude / codex 可执行文件，"
              "且未配置 Tier 2 API key（tools/wiki-config.local.json 或 WIKI_GEN_API_KEY）。")
        print("[i] 若你确信已安装 claude / codex，多半是运行本进程的 PATH 里没有它 —— "
              "systemd 服务的 PATH 常与登录 shell 不同，给服务单元补 Environment=PATH=… 即可。")
        print("[i] raw 已保存，可日后按 AGENTS.md 手动提炼。")
        _record_wiki_result(
            name, "failed",
            "未配置任何精加工通道（PATH 中无 claude/codex，也未配置 API key）",
            "",
        )
    return False


def queue_wiki_generation(raw_filepath: Path, data: dict, on_complete=None):
    """在后台线程中执行三级回退（由 clip-server 写入 raw 后调用）。

    on_complete: 可选回调，收到最终结果字典
        {"status": "ok"|"failed", "reason": str, "tier": str}。
        调用方（clip-server）靠它在**异步**路径下也能处理「wiki 没生成」的情况
        —— 例如把刚写入的 raw 撤回待审区，避免留下孤儿。
        回调里抛异常不会影响主流程。
    """
    name = Path(raw_filepath).name
    _record_wiki_result(name, "pending", "已进入后台生成队列")

    def _run():
        try:
            generate_wiki_topic(raw_filepath)
        except Exception as e:
            print(f"[!] wiki_gen 后台任务失败: {e}")
            import traceback
            traceback.print_exc()
            _record_wiki_result(name, "failed", f"{type(e).__name__}: {e}")

        if on_complete:
            try:
                on_complete(wiki_result_for(name) or
                            {"status": "failed", "reason": "未知原因", "tier": ""})
            except Exception as e:
                print(f"[!] wiki 完成回调异常: {e}")

    thread = threading.Thread(target=_run, daemon=True, name="wiki-gen")
    thread.start()
    print(f"[*] wiki 精加工已启动（后台线程）: {name}")


def run_wiki_generation_sync(raw_filepath: Path) -> dict:
    """**同步**执行三级回退并返回结果，供「完整入库」这类需要如实回报的调用方使用。

    返回 {"status": "ok"|"failed", "reason": str, "tier": str}。
    """
    name = Path(raw_filepath).name
    _record_wiki_result(name, "pending", "正在生成")
    try:
        generate_wiki_topic(raw_filepath)
    except Exception as e:
        print(f"[!] wiki_gen: 同步生成异常: {e}")
        _record_wiki_result(name, "failed", f"{type(e).__name__}: {e}")
    return wiki_result_for(name) or {"status": "failed", "reason": "未知原因", "tier": ""}

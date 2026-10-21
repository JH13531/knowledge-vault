"""飞书文档/知识库适配器。

优先级：lark-cli（应用身份 API，完整内容）。

⚠️ **飞书文档不降级到浏览器**：lark-cli 缺失或失败时抛 `NoBrowserFallback`，
由 clip-server 直接报错，**不进浏览器协助队列**。

理由：浏览器降级产出的正文通常不完整 —— 扩展的 `platforms/feishu.js` 靠在内层
滚动容器上逐步滚动、按行去重（依赖飞书 DOM 类名，且必须已登录），
`tools/playwright-worker.py` 更是完全没有飞书适配器（trafilatura 抓整页 +
`window.scrollTo`，而飞书是「内层容器滚动 + 虚拟列表」，window 滚动无效，只拿首屏）。
这类残缺结果会被当作完整剪藏写进 raw/articles/，比明确失败更糟。
**飞书文档的可靠通道只有 lark-cli。**
"""
from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
from urllib.parse import urlparse

from pathlib import Path

from no_window import NO_WINDOW
from .base import NoBrowserFallback, PlatformAdapter, ClipResult


# ===== 文档内图片（file_token）下载 =====
#
# 飞书导出的 markdown 里图片是 `![](https://feishu.cn/file/<file_token>)`。
# 这个 URL **不是可下载的图片地址** —— 未登录 GET 会 301→302 三次后落到
# accounts.feishu.cn 的登录页，返回 HTML。所以：
#   · 通用 HTTP 下载器要么被 SSRF 的重定向上限判「不安全」，要么把登录页
#     当成图片存下来（更糟）；
#   · 唯一可靠通道同样是 lark-cli：`docs +media-preview --token <file_token>`
#     用应用身份直接取素材流（实测 bot 身份可用；`+media-download` 反而 403）。
_FEISHU_HOSTS = ("feishu.cn", "larksuite.com", "larkoffice.com")
_MEDIA_PATH_RE = re.compile(r"/file/([A-Za-z0-9_-]+)")


def feishu_media_token(url: str) -> str:
    """从 `https://<租户>.feishu.cn/file/<token>` 取出 file_token；非飞书返回 ""。"""
    try:
        parsed = urlparse(url)
    except Exception:
        return ""
    host = (parsed.hostname or "").lower()
    if not any(host == h or host.endswith("." + h) for h in _FEISHU_HOSTS):
        return ""
    m = _MEDIA_PATH_RE.search(parsed.path or "")
    return m.group(1) if m else ""


def download_media(token: str, output_base: Path, timeout: float = 30) -> Path:
    """用 lark-cli 把文档素材下载到 output_base（不带扩展名，自动补全）。

    成功返回实际落盘路径（带扩展名），失败抛异常。
    """
    lark_cli = shutil.which("lark-cli")
    if not lark_cli:
        raise RuntimeError("lark-cli 未安装，无法下载飞书文档图片")

    # lark-cli 拒绝绝对路径：`--output` 必须是**当前目录内的相对路径**，
    # 否则报 `unsafe output path ... must be a relative path within the
    # current directory`。所以 cd 到目标目录、只传文件名。
    output_base.parent.mkdir(parents=True, exist_ok=True)
    cmd = [lark_cli, "docs", "+media-preview",
           "--token", token, "--output", f"./{output_base.name}", "--format", "json"]
    # Windows：npm 全局安装的是 .cmd 批处理，必须经 cmd.exe 执行
    if os.name == "nt" and lark_cli.lower().endswith((".cmd", ".bat")):
        cmd = [os.environ.get("COMSPEC", "cmd.exe"), "/c", *cmd]

    result = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, creationflags=NO_WINDOW, cwd=str(output_base.parent),
    )
    if result.returncode != 0:
        raise RuntimeError(f"lark-cli 退出码 {result.returncode}: "
                           f"{(result.stderr or result.stdout or '')[:120]}")
    data = json.loads(result.stdout)
    if not data.get("ok"):
        err = data.get("error") or {}
        raise RuntimeError(f"lark-cli 返回错误: {err.get('message') or err or data}")

    saved = (data.get("data") or {}).get("saved_path") or ""
    if not saved or not Path(saved).is_file():
        raise RuntimeError("lark-cli 未返回已保存的文件路径")
    return Path(saved)


class FeishuAdapter(PlatformAdapter):
    name = "飞书"
    priority = 10
    # 浏览器抓不全（见模块 docstring）→ 不走浏览器协助队列，
    # 重试也必须回到后端 lark-cli 通道。
    allows_browser_fallback = False

    # 通用域名即可：匹配逻辑对 xxx.feishu.cn 等租户子域名走 endswith 兜底，
    # 不要在此硬编码具体租户域名（开源会泄露内部租户信息）。
    FEISHU_DOMAINS = {
        "feishu.cn",
        "larkoffice.com",
        "larksuite.com",
    }

    @classmethod
    def match(cls, url: str) -> bool:
        try:
            domain = urlparse(url).netloc
            for fd in cls.FEISHU_DOMAINS:
                if domain == fd or domain.endswith("." + fd):
                    return True
            return False
        except Exception:
            return False

    @classmethod
    def extract(cls, url: str) -> ClipResult:
        """使用 lark-cli（应用身份 API）抓取。失败时抛 NoBrowserFallback ——
        由 clip-server 直接报错，**不**降级到浏览器协助队列（见模块 docstring）。"""
        # 判断文档类型
        doc_type = "docx"
        if "/wiki/" in url:
            doc_type = "wiki"
        elif "/sheets/" in url:
            doc_type = "sheet"
        elif "/docx/" in url:
            doc_type = "docx"
        elif "/base/" in url:
            doc_type = "bitable"

        # 检查 lark-cli 是否可用
        lark_cli_path = shutil.which("lark-cli")
        if not lark_cli_path:
            raise NoBrowserFallback(
                "lark-cli 未安装，无法抓取飞书文档。"
                "安装命令：npm install -g @larksuite/cli"
            )

        try:
            cmd = [lark_cli_path, "docs", "+fetch", "--doc", url,
                   "--doc-format", "markdown", "--detail", "simple"]
            # Windows：npm 安装的 lark-cli 是 .cmd 批处理，需经 cmd.exe 执行
            if os.name == "nt" and lark_cli_path.lower().endswith((".cmd", ".bat")):
                cmd = [os.environ.get("COMSPEC", "cmd.exe"), "/c", *cmd]
            result = subprocess.run(
                cmd,
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
                creationflags=NO_WINDOW,
            )

            if result.returncode != 0:
                stderr = result.stderr.strip()[:200] if result.stderr else ""
                raise NoBrowserFallback(f"lark-cli 执行失败 (exit {result.returncode}): {stderr}")

            data = json.loads(result.stdout)

            if not data.get("ok"):
                error_msg = data.get("error", data.get("message", str(data)))
                raise NoBrowserFallback(f"lark-cli 返回错误: {error_msg}")

            doc = data["data"]["document"]
            content = doc.get("content", "")

            if not content.strip():
                raise NoBrowserFallback("lark-cli 返回空内容")

            title = ""
            # 清理飞书导出的 <title> 标签
            content = content.replace("<title>", "").replace("</title>", "")
            # 从内容第一行提取标题
            for line in content.split("\n"):
                line = line.strip()
                if line.startswith("# "):
                    title = line[2:].strip()
                    break
                elif line and not line.startswith("<"):
                    title = line[:80]
                    break

            if not title:
                title = "飞书文档"

            return ClipResult(
                title=title,
                url=url,
                author="",
                published_at="",
                content=content,
                type="doc",
                platform="飞书",
                tags=["飞书", doc_type],
            )

        except subprocess.TimeoutExpired:
            raise NoBrowserFallback("lark-cli 抓取超时（超过 30 秒），请稍后重试")
        except json.JSONDecodeError:
            raise NoBrowserFallback(
                f"lark-cli 输出解析失败: {result.stdout[:200] if result.stdout else 'no output'}"
            )

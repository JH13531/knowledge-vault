#!/usr/bin/env python3
"""ocr_images.py — 下游 OCR 精加工：把「写在图片里」的文字提取出来，补进 raw 笔记。

## 定位（与 convert_to_webp.py 同层）

**不在入库链路做。** 图片先由 clip-server 原样字节保真落盘，本模块在 raw 落盘
之后运行：读同名图片目录 → OCR → 把识别文字补进笔记（新增/更新
`## 图片文字（OCR）` 一节）→ 交给 wiki_gen 重跑精加工。

为什么必须放在下游：OCR 要解码图片、可能很慢，且与「采集只做字节保真、不在
入库链路解码不可信图」的现有原则相冲。它与有损转码同属 raw 落盘之后的加工。

## 两个后端

- **multimodal（默认）**：复用 wiki_gen 的 Tier 2 配置（api_key / base_url），
  换成 **vision 模型** 读图。无本地重依赖、中文质量好；代价是图片外发。
- **local**：配置里填任意 OCR 命令模板，如
  `tesseract {input} stdout -l chi_sim+eng`。零绑定、跨平台、可换任意工具。

## 配置

`tools/ocr-config.local.json`（也可用环境变量 OCR_ENGINE / OCR_MODEL /
OCR_LOCAL_COMMAND 覆盖）：

```json
{
  "engine": "auto",
  "model": "",
  "max_images": 10,
  "max_image_bytes": 4194304,
  "local_command": "tesseract {input} stdout -l chi_sim+eng",
  "timeout": 120
}
```

`engine` 取值：`auto`（有 Tier 2 API 就用多模态、否则本地命令可用就用本地）、
`multimodal`、`local`、`off`（显式关闭，则「图多字少」只记 skipped）。

## 命令行

```bash
# 列出「图多字少」的候选（默认 dry-run）
python tools/ocr_images.py

# 只处理一篇：OCR + 回填 + 重跑 wiki
python tools/ocr_images.py --apply --file 2026-10-10-xxx.md
```

退出码：0 = 成功或没有待办；1 = 有失败项。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
VAULT = TOOLS_DIR.parent
sys.path.insert(0, str(TOOLS_DIR))

from no_window import NO_WINDOW

CONFIG_PATH = TOOLS_DIR / "ocr-config.local.json"
ARTICLES_DIR = VAULT / "raw" / "articles"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

DEFAULT_TIMEOUT = 120
DEFAULT_MAX_IMAGES = 10
DEFAULT_MAX_IMAGE_BYTES = 4 * 1024 * 1024
_DOWNSCALE_MAX_EDGE = 2000

#: 回填进笔记的章节标题。改写它要同步改 test_routing2.js 的断言。
_OCR_SECTION_HEADING = "## 图片文字（OCR）"

_OCR_PROMPT = (
    "请提取这些图片中的所有可见文字，按图片顺序原样输出，保留换行与标点。"
    "只输出文字本身，不要任何解释、标注或 Markdown 代码块。"
    "若某张图没有文字，跳过它。"
)


# ========== 配置 ==========

def _load_file_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception as e:
        print(f"[!] ocr: 配置解析失败（{type(e).__name__}: {e}），按默认处理")
        return {}


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def ocr_config() -> dict:
    """解析 OCR 配置：环境变量优先，其次 tools/ocr-config.local.json。"""
    cfg = _load_file_config()
    engine = (os.environ.get("OCR_ENGINE") or cfg.get("engine") or "auto").strip().lower()
    model = (os.environ.get("OCR_MODEL") or cfg.get("model") or "").strip()
    local_command = (os.environ.get("OCR_LOCAL_COMMAND")
                     or cfg.get("local_command") or "").strip()
    return {
        "engine": engine,
        "model": model,
        "local_command": local_command,
        "max_images": max(1, _as_int(cfg.get("max_images"), DEFAULT_MAX_IMAGES)),
        "max_image_bytes": max(1024, _as_int(cfg.get("max_image_bytes"),
                                             DEFAULT_MAX_IMAGE_BYTES)),
        "timeout": max(5, _as_int(cfg.get("timeout"), DEFAULT_TIMEOUT)),
    }


# ========== 可用性判定 ==========

def _multimodal_ready() -> bool:
    """多模态后端是否就绪：复用 wiki_gen 的 Tier 2 API 配置。"""
    try:
        from wiki_gen import api_config
    except Exception:
        return False
    return api_config() is not None


def _local_exe(local_command: str) -> str:
    if not local_command:
        return ""
    exe = local_command.split()[0].strip('"')
    return shutil.which(exe) or ""


def _local_ready(local_command: str) -> bool:
    return bool(_local_exe(local_command))


def resolve_engine(cfg: dict | None = None) -> str:
    """返回实际使用的引擎：`multimodal` / `local` / `""`（不可用）。

    `auto` 下，多模态**要求显式配置 vision 模型**（`model`）才启用：wiki 的
    Tier 2 model 常是纯文本模型（如 DeepSeek），拿它读图只会白跑一次注定失败的
    调用、还把这条「图多字少」记成 skipped —— 不如直接判定为「无 OCR 通道」。
    显式 `engine=multimodal` 时不设此限（留空 model 即复用 wiki 的，用户自担）。
    """
    cfg = cfg or ocr_config()
    engine = cfg["engine"]
    if engine in ("off", "none", "disabled", "false"):
        return ""
    if engine == "multimodal":
        return "multimodal" if _multimodal_ready() else ""
    if engine == "local":
        return "local" if _local_ready(cfg["local_command"]) else ""
    # auto：多模态（需显式 vision 模型）优先，其次本地命令兜底
    if cfg["model"] and _multimodal_ready():
        return "multimodal"
    if _local_ready(cfg["local_command"]):
        return "local"
    return ""


def is_available() -> bool:
    """是否有任意一种可用的 OCR 后端。"""
    return bool(resolve_engine())


def describe() -> str:
    """一句话描述当前后端（供日志/CLI 展示）。"""
    cfg = ocr_config()
    engine = resolve_engine(cfg)
    if engine == "multimodal":
        model = cfg["model"] or "（沿用 wiki 的 model）"
        return f"多模态 API（vision 模型：{model}）"
    if engine == "local":
        return f"本地命令：{cfg['local_command']}"
    if cfg["engine"] in ("off", "none", "disabled", "false"):
        return "已关闭（engine=off）"
    return ("不可用：可在 tools/ocr-config.local.json 填 vision 模型（多模态）"
            "或可用的 local_command（本地）")


# ========== 图片发现 ==========

def _images_of(raw_path: Path) -> list[Path]:
    """取这篇 raw 的同名图片目录下的图片（按文件名排序）。"""
    img_dir = Path(raw_path).parent / Path(raw_path).stem
    if not img_dir.is_dir():
        return []
    files = [p for p in img_dir.iterdir()
             if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    return sorted(files, key=lambda p: p.name)


# ========== 多模态后端 ==========

def _downscale_jpeg(path: Path, max_edge: int = _DOWNSCALE_MAX_EDGE,
                    quality: int = 80) -> bytes | None:
    """用 Pillow 缩到最长边 ≤ max_edge 并转 JPEG；失败返回 None（可选依赖）。"""
    try:
        import io

        from PIL import Image
    except Exception:
        return None
    try:
        with Image.open(path) as im:
            im = im.convert("RGB")
            if max(im.size) > max_edge:
                ratio = max_edge / max(im.size)
                im = im.resize((max(1, round(im.width * ratio)),
                                max(1, round(im.height * ratio))), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=quality)
            return buf.getvalue()
    except Exception:
        return None


def _to_data_url(path: Path, max_bytes: int) -> str | None:
    """把本地图片转成 data URL；过大则尝试降采样，仍不行则跳过。"""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    mime = {".png": "image/png", ".gif": "image/gif",
            ".webp": "image/webp", ".bmp": "image/bmp"}.get(path.suffix.lower(),
                                                            "image/jpeg")
    if len(raw) > max_bytes:
        shrunk = _downscale_jpeg(path)
        if shrunk is None or len(shrunk) > max_bytes:
            print(f"[i] ocr: 图片过大且无法压缩，跳过 {path.name}")
            return None
        raw, mime = shrunk, "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


def _ocr_multimodal(images: list[Path], cfg: dict) -> tuple[str, str]:
    """返回 (文字, 错误)。"""
    import requests

    from wiki_gen import api_config
    api = api_config()
    if api is None:
        return "", "未配置 wiki Tier 2 API（tools/wiki-config.local.json）"
    model = cfg["model"] or api["model"]

    content: list[dict] = [{"type": "text", "text": _OCR_PROMPT}]
    for p in images:
        du = _to_data_url(p, cfg["max_image_bytes"])
        if du:
            content.append({"type": "image_url", "image_url": {"url": du}})
    if len(content) == 1:
        return "", "没有可发送的图片"

    try:
        resp = requests.post(
            f"{api['base_url']}/chat/completions",
            headers={"Authorization": f"Bearer {api['api_key']}",
                     "Content-Type": "application/json"},
            json={"model": model, "temperature": 0,
                  "messages": [{"role": "user", "content": content}]},
            timeout=cfg["timeout"],
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"]
    except requests.exceptions.RequestException as e:
        return "", f"多模态 API 请求失败：{e}"
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
        return "", f"多模态 API 返回解析失败（{type(e).__name__}）"

    return _clean(text), ""


# ========== 本地后端 ==========

def _ocr_local(images: list[Path], cfg: dict) -> tuple[str, str]:
    """逐个跑本地 OCR 命令，拼接 stdout。返回 (文字, 错误)。"""
    tpl = cfg["local_command"]
    texts: list[str] = []
    errs = 0
    for p in images:
        cmd = tpl.replace("{input}", f'"{p}"')
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               timeout=cfg["timeout"], creationflags=NO_WINDOW)
            if r.returncode == 0 and r.stdout.strip():
                texts.append(r.stdout.strip())
            else:
                errs += 1
        except subprocess.TimeoutExpired:
            errs += 1
            print(f"[!] ocr: 本地 OCR 超时（{cfg['timeout']}s）: {p.name}")
        except Exception as e:
            errs += 1
            print(f"[!] ocr: 本地 OCR 失败 {p.name}: {e}")
    if not texts:
        return "", f"本地 OCR 未产出文字（{errs} 张失败/无输出）"
    return _clean("\n".join(texts)), ""


# ========== 公共 ==========

def _clean(text: str) -> str:
    t = str(text or "").strip()
    t = re.sub(r"^```[a-zA-Z0-9]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t).strip()
    return t


def extract_text(raw_path: Path, cfg: dict | None = None) -> tuple[str, str, str]:
    """对某篇 raw 的图片跑 OCR。返回 (文字, 引擎, 错误)。"""
    cfg = cfg or ocr_config()
    engine = resolve_engine(cfg)
    if not engine:
        return "", "", "无可用 OCR 通道"
    images = _images_of(raw_path)
    if not images:
        return "", engine, "未找到图片"
    images = images[:cfg["max_images"]]
    if engine == "multimodal":
        text, err = _ocr_multimodal(images, cfg)
    else:
        text, err = _ocr_local(images, cfg)
    if err:
        return "", engine, err
    return text, engine, ""


def refill_note(raw_path: Path, text: str) -> bool:
    """把 OCR 文字写进笔记：新增/更新 `## 图片文字（OCR）` 一节（auto 区末尾）。

    幂等：重复运行只替换该节内容，不会追加多份。返回是否发生改动。
    """
    raw_path = Path(raw_path)
    original = raw_path.read_text(encoding="utf-8")
    body = str(text or "").strip()
    block = (f"{_OCR_SECTION_HEADING}\n\n"
             "> 由 OCR 自动提取，可能有个别识别误差；图片原文未改动。\n\n"
             f"{body}\n")

    # 已存在该节 → 替换（终止于下一个 ## 标题 / HTML 注释 / 注记行 / 文件末尾）
    pat = re.compile(
        rf"^{re.escape(_OCR_SECTION_HEADING)}[ \t]*$\n.*?"
        r"(?=^##[ \t]|^<!--|^>[ \t]*注：|\Z)",
        re.M | re.S)
    if pat.search(original):
        new = pat.sub(block + "\n", original, count=1)
    else:
        marker = "<!-- clip:auto:end -->"
        if marker in original:
            new = original.replace(marker, "\n" + block + "\n" + marker, 1)
        else:
            new = original.rstrip() + "\n\n" + block
    if new == original:
        return False
    new = _refresh_word_count(new)
    raw_path.write_text(new, encoding="utf-8")
    return True


def _refresh_word_count(text: str) -> str:
    """OCR 回填改变了正文字数，顺手刷新 frontmatter 与头部展示的字数。

    不刷新就会出现「正文已补进 1671 字，头部还写着约 232 字」—— 字数是对外承诺
    「剪到了多少」，补完内容却留着旧数字，与「不谎报」的原则相冲。口径与
    `content_measure.count_words` 一致（含刚补进来的 OCR 文字，它是真实内容）。
    """
    from content_measure import count_words

    n = count_words(text)
    text = re.sub(r"^word_count:[ \t]*\d+", f"word_count: {n}", text,
                  count=1, flags=re.M)
    text = re.sub(r"^>[ \t]*字数：约[ \t]*\d+[ \t]*字", f"> 字数：约 {n} 字",
                  text, count=1, flags=re.M)
    return text


def process_raw(raw_path: Path, cfg: dict | None = None) -> dict:
    """OCR → 回填笔记（不负责跑 wiki）。

    返回 {"ok": bool, "chars": int, "engine": str, "reason": str}。
    """
    raw_path = Path(raw_path)
    cfg = cfg or ocr_config()
    try:
        text, engine, err = extract_text(raw_path, cfg)
    except Exception as e:
        return {"ok": False, "chars": 0, "engine": "", "reason": f"{type(e).__name__}: {e}"}
    if err or not text:
        return {"ok": False, "chars": 0, "engine": engine,
                "reason": err or "OCR 未提取到文字"}
    try:
        refill_note(raw_path, text)
    except Exception as e:
        return {"ok": False, "chars": len(text), "engine": engine,
                "reason": f"回填笔记失败：{type(e).__name__}: {e}"}
    return {"ok": True, "chars": len(re.sub(r"\s", "", text)),
            "engine": engine, "reason": ""}


def queue_processing(raw_path: Path, on_complete=None):
    """在后台线程里跑 process_raw，完成后回调结果字典。

    on_complete 抛异常不影响主流程。供 clip-server 在入库链路里「只排队不干活」。
    """
    raw_path = Path(raw_path)

    def _run():
        result = process_raw(raw_path)
        if on_complete:
            try:
                on_complete(result)
            except Exception as e:
                print(f"[!] ocr 完成回调异常: {e}")

    threading.Thread(target=_run, daemon=True, name="ocr-images").start()
    print(f"[*] OCR 精加工已启动（后台线程）: {raw_path.name}")


# ========== CLI ==========

def _candidates() -> list[Path]:
    """列出「图多字少」的候选 raw：真实文字过少、且含图。

    判据**直接复用 content_measure**（与 clip-server 的门禁同源）。过去这里内联
    了一份旧实现，两边口径漂移 → 本命令列不出 clip-server 判为「图多字少」的
    笔记，于是「下游补救」这一环静默失效（2026-10-14 实测漏掉一条小红书图文笔记）。
    """
    from content_measure import has_text_in_images

    out: list[Path] = []
    for md in sorted(ARTICLES_DIR.glob("*.md")):
        try:
            t = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if has_text_in_images(t):
            out.append(md)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description="下游 OCR：把「写在图片里」的文字提取出来补进 raw 笔记（默认 dry-run）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--apply", action="store_true",
                    help="真正执行（不加则只列出候选，不调用任何 OCR）")
    ap.add_argument("--file", default="", help="只处理这一个文件（文件名或 stem）")
    ap.add_argument("--limit", type=int, default=0, help="最多处理几篇（0 = 不限）")
    args = ap.parse_args()

    print(f"OCR 后端：{describe()}")

    if args.file:
        cands = [p for p in ARTICLES_DIR.glob("*.md")
                 if args.file in (p.name, p.stem)]
    else:
        cands = _candidates()
    if args.limit > 0:
        cands = cands[:args.limit]

    print(f"候选（图多字少）: {len(cands)} 篇")
    for p in cands:
        print(f"    - {p.name}")

    if not args.apply:
        print("\n（dry-run，未改动任何文件。加 --apply 才会真正 OCR 并回填）")
        return 0
    if not cands:
        print("\n没有待办。")
        return 0
    if not is_available():
        print("\n❌ 当前没有可用的 OCR 后端：")
        print("   多模态：配好 tools/wiki-config.local.json（Tier 2 API）即可")
        print("   本地  ：在 tools/ocr-config.local.json 填 local_command")
        return 1

    import wiki_gen  # 延迟导入

    failed: list[tuple[str, str]] = []
    ok = 0
    print("\n开始 OCR…\n")
    for i, p in enumerate(cands, 1):
        print(f"--- [{i}/{len(cands)}] {p.name} ---")
        r = process_raw(p)
        if not r.get("ok"):
            failed.append((p.name, str(r.get("reason", ""))))
            print(f"    ❌ OCR 失败：{r.get('reason', '未知原因')}")
            continue
        print(f"    ✅ 提取约 {r.get('chars', 0)} 字（{r.get('engine', '')}），重跑 wiki…")
        w = wiki_gen.run_wiki_generation_sync(p)
        if w.get("status") == "ok":
            ok += 1
            print(f"    ✅ wiki 已生成（{w.get('tier', '')}）")
        else:
            failed.append((p.name, str(w.get("reason", ""))))
            print(f"    ❌ wiki 失败：{w.get('reason', '未知原因')}")

    print("\n" + "=" * 56)
    print(f"完成：成功 {ok} / 失败 {len(failed)}（共 {len(cands)}）")
    for name, reason in failed:
        print(f"    ❌ {name}\n       {reason}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""把 raw/articles 下引用的非 WebP 图片转成 WebP，并同步改写 md 引用。

安全设计：
- 默认 dry-run，只打印计划；--apply 才真正执行
- 只处理 raw/articles 下**被 md 引用**的图片（_archive 一律跳过，§4.4 冻结）
- 转码成功后才删除原文件；任何一张失败即中止该张，不影响其他
- 原图在替换前先备份到 raw/_webp_backup/（可回滚）
- md 引用改写：只替换已成功转码的图片路径（含可选的目录名修正）
"""
import argparse
import re
import shutil
import sys
from pathlib import Path

VAULT = Path(__file__).resolve().parent.parent
ARTICLES = VAULT / "raw" / "articles"
BACKUP = VAULT / "raw" / "_webp_backup"
EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp"}
IMG_REF_RE = re.compile(r'!\[([^\]]*)\]\(([^)]+\.(?:jpg|jpeg|png|gif|bmp))\)', re.I)

# ---- 尺寸策略 ----
# 与 lint.py 的 LONG_RATIO_LINT / LONG_MAX_W_LINT 保持一致
MAX_EDGE = 1920          # 普通图最长边上限（§7.1）
LONG_RATIO = 3.0         # 宽高比超过此值即视为"长截图"
LONG_MAX_WIDTH = 1440    # 长截图：限制**宽度**（保阅读），不限制高度
LONG_MIN_WIDTH = 720     # 长截图宽度下限（仅作说明，不主动放大）


def _plan_size(w: int, h: int) -> tuple[int, int, str]:
    """返回 (目标宽, 目标高, 策略标签)。

    普通图：最长边 ≤ MAX_EDGE（横竖通用）
    长截图：宽高比 > LONG_RATIO 时，只压宽度到 LONG_MAX_WIDTH，高度不动
            （手机滚动截屏 1080×20000 若按最长边缩放会变成 1080×1920，
             每行压成 100px，完全不可读——长图必须只限宽度）
    """
    ratio = max(w, h) / max(min(w, h), 1)
    if ratio > LONG_RATIO:
        # 长图：以宽度为准（长边通常是高）
        if w > LONG_MAX_WIDTH:
            nh = round(h * LONG_MAX_WIDTH / w)
            return LONG_MAX_WIDTH, nh, "long-width"
        return w, h, "long-keep"
    # 普通图：限制最长边
    if max(w, h) > MAX_EDGE:
        if w >= h:
            nh = round(h * MAX_EDGE / w)
            return MAX_EDGE, nh, "edge"
        nw = round(w * MAX_EDGE / h)
        return nw, MAX_EDGE, "edge"
    return w, h, "keep"


def _to_webp(src: Path, quality: int = 80, lossless: bool = False) -> tuple[bool, int]:
    """把单张图转 WebP（含按策略缩放），返回 (成功, 新字节数)。原地生成同名 .webp。"""
    from PIL import Image
    dst = src.with_suffix(".webp")
    try:
        with Image.open(src) as im:
            # 保留透明通道（PNG/GIF 可能带 alpha）
            if im.mode in ("RGBA", "LA", "P") and "transparency" in im.info:
                im = im.convert("RGBA")
            elif im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGB")
            tw, th, _ = _plan_size(*im.size)
            if (tw, th) != im.size:
                im = im.resize((tw, th), Image.LANCZOS)
            save_kw = {}
            if lossless:
                save_kw["lossless"] = True
            else:
                save_kw["quality"] = quality
                save_kw["method"] = 6
            im.save(dst, "WEBP", **save_kw)
        return True, dst.stat().st_size
    except Exception as e:
        print(f"    [!] 转码失败 {src.name}: {e}")
        if dst.exists():
            dst.unlink()
        return False, 0


def _repair_broken_ref(md: Path, old_rel: str) -> tuple[Path, str] | None:
    """修复"目录名 -N 后缀"错配：尝试去掉目录的 -N 后缀或改挂到同名目录。

    返回 (修正后的真实文件路径, 修正后的 md 内引用串)，无法修复返回 None。
    """
    parts = old_rel.replace("\\", "/").split("/")
    if len(parts) < 2:
        return None
    for i in range(len(parts) - 1):
        d = parts[i]
        mm = re.match(r"^(.*)-(\d+)$", d)
        if not mm:
            continue
        for cand in (mm.group(1),):          # 去掉 -N
            trial = parts[:i] + [cand] + parts[i + 1:]
            trial_rel = "/".join(trial)
            if (md.parent / trial_rel).exists():
                return (md.parent / trial_rel), trial_rel
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正执行（默认 dry-run）")
    ap.add_argument("--quality", type=int, default=80)
    ap.add_argument("--lossless", action="store_true",
                    help="无损模式（截图/线条图适用，体积较大）")
    args = ap.parse_args()

    if not ARTICLES.exists():
        sys.exit("找不到 raw/articles")

    # 1. 收集所有 md 引用（含死链，用于目录修正）
    plans = []          # (md_path, old_rel, new_rel, img_src_or_None)
    seen_imgs = set()
    for md in sorted(ARTICLES.glob("*.md")):
        text = md.read_text(encoding="utf-8")
        for m in IMG_REF_RE.finditer(text):
            old_rel = m.group(2)
            if old_rel.startswith(("http://", "https://")):
                continue
            src = (md.parent / old_rel)
            if not src.exists():
                # 死链修正：目录名带 "-N" 后缀但实际目录无后缀（clip-server 落盘差异）
                fixed = _repair_broken_ref(md, old_rel)
                if fixed is not None:
                    src = fixed[0]
                    old_rel = fixed[1]   # 用修正后的旧路径参与后续替换
            if src.exists():
                seen_imgs.add(src)
                new_rel = re.sub(r'\.(jpg|jpeg|png|gif|bmp)$', ".webp",
                                 old_rel, flags=re.I)
                plans.append((md, old_rel, new_rel, src))
            else:
                plans.append((md, old_rel, None, None))

    print(f"待转图片 {len(seen_imgs)} 张，涉及 md 引用 {len(plans)} 条")
    if not args.apply:
        print("\n[dry-run] 预览（前 10 条）：")
        for md, old, new, src in plans[:10]:
            tag = "OK " if src else "死链"
            print(f"  [{tag}] {md.name[:28]:30} {old[-46:]}")
        print("\n加 --apply 执行")
        return

    BACKUP.mkdir(parents=True, exist_ok=True)
    converted, failed, saved = {}, [], 0
    for src in sorted(seen_imgs):
        before = src.stat().st_size
        ok, after = _to_webp(src, args.quality, args.lossless)
        if not ok:
            failed.append(src)
            continue
        # 备份原图（保留相对结构）
        bak = BACKUP / src.relative_to(ARTICLES)
        bak.parent.mkdir(parents=True, exist_ok=True)
        if not bak.exists():
            shutil.copy2(src, bak)
        converted[src] = src.with_suffix(".webp")
        saved += before - after
        print(f"  [+] {src.name:16} {before/1024:6.0f} -> {after/1024:5.0f} KB")

    # 2. 改写 md 引用（用 plans 里记录的原始引用串，含死链修正后的路径）
    conv_names = {s.name for s in converted}
    touched = 0
    for md, old_rel, new_rel, src in plans:
        if src is None or new_rel is None or src.name not in conv_names:
            continue
        text = md.read_text(encoding="utf-8")
        # 同时替换「修正后路径」与「原始引用串」，确保死链也被修复
        replaced = text.replace(f"]({old_rel})", f"]({new_rel})")
        if replaced != text:
            md.write_text(replaced, encoding="utf-8")
            touched += 1

    # 3. 删除已成功转码的原图
    for src in converted:
        src.unlink()
        # 顺带清理 clip-server 可能遗留的空目录
        if src.parent.exists() and not any(src.parent.iterdir()):
            src.parent.rmdir()

    print(f"\n完成：转换 {len(converted)} 张，改写 {touched} 个 md，"
          f"节省 {saved/1024/1024:.1f} MB，失败 {len(failed)}")
    print(f"原图备份：{BACKUP.relative_to(VAULT)}")


if __name__ == "__main__":
    main()

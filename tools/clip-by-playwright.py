#!/usr/bin/env python3
"""
用 Playwright 剪藏网页 — 替代浏览器扩展的另一种方式。

优势：
- 不依赖浏览器扩展权限，不会有 CORS/NetworkError 问题
- 可以用你自己的浏览器 profile（带登录态）
- 和 playwright-worker 是同一套技术栈
- 可以命令行调用、也可以被其他脚本调用

用法：
    # 剪藏单个 URL
    python3 tools/clip-by-playwright.py https://example.com/article

    # 批量剪藏（从文件读，每行一个 URL）
    python3 tools/clip-by-playwright.py --file urls.txt

    # 指定 Firefox profile
    python3 tools/clip-by-playwright.py --profile ~/.mozilla/firefox/xxx.default URL

    # 用 Chrome
    python3 tools/clip-by-playwright.py --browser chromium --profile ~/.config/google-chrome URL
"""
from __future__ import annotations
import argparse
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import trafilatura
from playwright.sync_api import sync_playwright

# 复用服务端的 inbox 写入逻辑
sys.path.insert(0, str(Path(__file__).parent))
from task_queue import queue  # noqa: F401  (避免 import 错误，实际不用)


def extract_with_playwright(page, url: str) -> dict:
    """用 Playwright 打开页面并提取内容。"""
    print(f"  → 打开页面...")
    page.goto(url, wait_until="domcontentloaded", timeout=30000)

    # 等网络空闲
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass

    # 滚动到底部（触发懒加载）
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1000)
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(500)
    except Exception:
        pass

    # 拿完整 HTML
    html = page.content()
    title = page.title()

    # 作者
    author = page.evaluate("""() => {
        const meta = document.querySelector('meta[name="author"]') ||
                    document.querySelector('meta[property="article:author"]');
        return meta ? meta.content : '';
    }""")

    # 发布日期
    published_at = page.evaluate("""() => {
        const meta = document.querySelector('meta[property="article:published_time"]') ||
                    document.querySelector('meta[name="pubdate"]') ||
                    document.querySelector('time[datetime]');
        if (meta) {
            const val = meta.getAttribute('datetime') || meta.content || meta.getAttribute('content');
            if (val) return val.slice(0, 10);
        }
        return '';
    }""")

    # 用 trafilatura 提取正文（Markdown 格式）
    content = ""
    if html:
        content = trafilatura.extract(
            html,
            output_format="markdown",
            include_links=True,
            include_images=True,
            include_tables=True,
        ) or ""

    # 如果 trafilatura 没提取到，回退到 body.innerText
    if not content or len(content) < 100:
        content = page.evaluate("""() => {
            const sels = ['article', 'main', '[role="main"]',
                '.article-content', '.post-content', '#content', '.content'];
            let best = null;
            let bestScore = 0;
            for (const sel of sels) {
                const el = document.querySelector(sel);
                if (el) {
                    const score = el.innerText.length;
                    if (score > bestScore) { bestScore = score; best = el; }
                }
            }
            if (!best || bestScore < 500) best = document.body;
            return best.innerText;
        }""")

    # 字数
    chinese = len(re.findall(r"[\u4e00-\u9fff]", content))
    english = len(re.findall(r"[a-zA-Z]{3,}", content))
    word_count = chinese + english

    # 平台
    domain = urlparse(url).netloc.replace("www.", "")

    return {
        "title": title or "网页剪藏",
        "url": url,
        "author": author,
        "publishedAt": published_at,
        "content": content,
        "type": "article",
        "platform": domain,
        "tags": ["网页", domain],
        "wordCount": word_count,
        "via": "Playwright",
    }


def write_to_inbox(data: dict) -> Path:
    """写入 inbox，复用 clip-server 的逻辑。"""
    # 直接复用 clip-server 的 generate_card
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "clip_server",
        Path(__file__).parent / "clip-server.py"
    )
    mod = importlib.util.module_from_spec(spec)
    # 不执行 main
    sys.argv = ["clip-server"]  # 防止 argparse 抢参数
    spec.loader.exec_module(mod)

    filename, content, wc = mod.generate_card(data)
    filepath = mod.INBOX_DIR / filename
    filepath.write_text(content, encoding="utf-8")
    return filepath


def main():
    parser = argparse.ArgumentParser(description="Playwright 剪藏工具")
    parser.add_argument("url", nargs="?", help="要剪藏的 URL")
    parser.add_argument("--file", help="从文件批量读取 URL（每行一个）")
    parser.add_argument("--browser", default="firefox",
                        choices=["firefox", "chromium"], help="用哪个浏览器 (默认 firefox)")
    parser.add_argument("--profile", default=None,
                        help="用户配置目录（带登录态），不指定用干净浏览器")
    parser.add_argument("--headless", action="store_true", help="无头模式")
    parser.add_argument("--connect", default=None,
                        help="连接已运行的浏览器（CDP 端点，如 http://localhost:9222）。"
                             "Firefox 启动方式：firefox --remote-debugging-port=9222")
    args = parser.parse_args()

    if not args.url and not args.file:
        parser.print_help()
        sys.exit(1)

    urls = []
    if args.url:
        urls.append(args.url)
    if args.file:
        urls.extend(line.strip() for line in open(args.file, encoding="utf-8") if line.strip())

    print(f"📚 Playwright 剪藏 ({args.browser})")
    print(f"   共 {len(urls)} 个 URL")
    if args.profile:
        print(f"   Profile: {args.profile}")
    else:
        print(f"   Profile: 干净浏览器（无登录态）")
    print()

    with sync_playwright() as p:
        # 连接已运行的浏览器（优先）
        if args.connect:
            print(f"   连接到: {args.connect}")
            browser = p.firefox.connect_over_cdp(args.connect)
            # 用默认 context 或者新建一个
            contexts = browser.contexts
            if contexts:
                context = contexts[0]
            else:
                context = browser.new_context()
        elif args.profile:
            # 持久化 profile
            if args.browser == "firefox":
                context = p.firefox.launch_persistent_context(
                    user_data_dir=args.profile,
                    headless=args.headless,
                )
            else:
                context = p.chromium.launch_persistent_context(
                    user_data_dir=args.profile,
                    headless=args.headless,
                )
        else:
            if args.browser == "firefox":
                browser = p.firefox.launch(headless=args.headless)
            else:
                browser = p.chromium.launch(headless=args.headless)
            context = browser.new_context()

        success_count = 0
        for i, url in enumerate(urls, 1):
            print(f"[{i}/{len(urls)}] {url[:80]}")
            try:
                page = context.new_page()
                data = extract_with_playwright(page, url)
                filepath = write_to_inbox(data)
                print(f"  ✅ {data['title'][:50]} ({data['wordCount']} 字)")
                print(f"     → {filepath.relative_to(Path.cwd())}")
                success_count += 1
                page.close()
            except Exception as e:
                print(f"  ❌ 失败: {e}")

            # 加点间隔，别太猛
            if i < len(urls):
                time.sleep(0.5)

        context.close()
        if args.connect:
            # 连接模式不关原浏览器
            pass
        elif args.profile is None:
            browser.close()

    print(f"\n完成：{success_count}/{len(urls)} 成功")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Playwright 浏览器协助 Worker
连接到剪藏服务的 SSE 端点，自动抓取需要浏览器登录态的页面。

原理：用你自己的 Chrome 用户配置文件（带 cookie），服务端发来任务就开新标签页抓，
抓完提交结果并关掉。相当于一个自动化的"浏览器插件"。

用法：
    # 首次使用需要安装 Playwright
    pip install --break-system-packages playwright httpx trafilatura
    playwright install chromium

    # 启动 worker（用你自己的 Chrome profile）
    python3 tools/playwright-worker.py --profile ~/.config/google-chrome

    # 无头模式不推荐（登录态网站容易检测），默认有头运行
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

# 复用平台系统的提取逻辑？不，Playwright 里用 trafilatura 或直接读 DOM
try:
    import trafilatura
    HAS_TRAFILATURA = True
except ImportError:
    HAS_TRAFILATURA = False


class BrowserWorker:
    def __init__(self, server_url: str, profile_dir: str = None,
                 headless: bool = False, token: str = ""):
        self.server_url = server_url.rstrip("/")
        self.token = token
        self.profile_dir = profile_dir
        self.headless = headless
        self.browser = None
        self.context = None

    def start(self):
        from playwright.sync_api import sync_playwright
        self._playwright = sync_playwright().start()

        launch_kwargs = {"headless": self.headless}

        if self.profile_dir:
            # 使用持久化用户配置（带 cookie、登录态）
            self.context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=self.profile_dir,
                **launch_kwargs,
            )
            print(f"🐞 Playwright worker 启动（使用 profile: {self.profile_dir}）")
        else:
            # 干净浏览器（无登录态，仅做普通网页抓取测试）
            self.browser = self._playwright.chromium.launch(**launch_kwargs)
            self.context = self.browser.new_context()
            print("🐞 Playwright worker 启动（干净浏览器）")

        print(f"   服务端: {self.server_url}")
        print("   按 Ctrl+C 停止")
        print()

    def run(self):
        """主循环：连接 SSE，接收任务并处理。"""
        self.start()
        # EventSource 不支持自定义请求头，token 走 query 参数
        sse_url = f"{self.server_url}/api/browser/connect"
        if self.token:
            sse_url += "?token=" + self.token

        # 指数退避重连
        retry_delay = 2
        max_retry = 30

        while True:
            try:
                print(f"🔗 连接到 {sse_url} ...")
                with httpx.stream("GET", sse_url, timeout=None) as r:
                    retry_delay = 2  # 连接成功，重置退避
                    current_event = None
                    data_buffer = []

                    for line in r.iter_lines():
                        line = line.strip()
                        if not line:
                            # 事件结束
                            if current_event and data_buffer:
                                self._handle_event(current_event, "\n".join(data_buffer))
                            current_event = None
                            data_buffer = []
                            continue

                        if line.startswith("event: "):
                            current_event = line[7:]
                        elif line.startswith("data: "):
                            data_buffer.append(line[6:])
                        elif line.startswith(":"):
                            # 心跳，忽略
                            pass

            except httpx.ConnectError:
                print(f"❌ 连接失败，{retry_delay}秒后重试...")
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, max_retry)
            except KeyboardInterrupt:
                print("\n👋 Worker 停止")
                break
            except Exception as e:
                print(f"❌ 连接异常: {e}，{retry_delay}秒后重试...")
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, max_retry)

        self._cleanup()

    def _handle_event(self, event: str, data: str):
        if event == "info":
            try:
                info = json.loads(data)
                pending = info.get("pending", 0)
                print(f"✅ 已连接，待处理任务: {pending}")
            except Exception:
                print(f"ℹ️  {data}")

        elif event == "task":
            try:
                task = json.loads(data)
                task_id = task.get("taskId")
                url = task.get("url")
                print(f"📨 收到任务 [{task_id}]: {url[:80]}")
                self._process_task(task_id, url)
            except Exception as e:
                print(f"❌ 处理任务失败: {e}")

    def _process_task(self, task_id: str, url: str):
        page = None
        try:
            page = self.context.new_page()

            # 设置合理的超时
            page.set_default_timeout(30000)

            # 访问页面
            print(f"   → 打开页面...")
            page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # 等一下网络空闲
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass  # 网络一直不空闲也不等了

            # 滚动到底部触发懒加载
            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(1000)
                page.evaluate("window.scrollTo(0, 0)")
                page.wait_for_timeout(500)
            except Exception:
                pass

            # 提取内容
            print(f"   → 提取内容...")
            result = self._extract_content(page, url)

            # 提交结果
            print(f"   → 提交结果（约 {result['wordCount']} 字）...")
            self._submit_result(task_id, result)

            print(f"✅ 任务完成 [{task_id}]: {result['title'][:50]}")

        except Exception as e:
            print(f"❌ 任务失败 [{task_id}]: {e}")
            self._submit_error(task_id, str(e))
        finally:
            if page:
                page.close()

    def _extract_content(self, page, url: str) -> dict:
        """从页面提取内容。"""
        title = page.title()

        # 先拿完整 HTML
        html = page.content()

        # 用 trafilatura 提取正文（如果可用）
        content = ""
        if HAS_TRAFILATURA:
            content = trafilatura.extract(
                html,
                output_format="markdown",
                include_links=True,
                include_images=True,
                include_tables=True,
            ) or ""

        # 如果 trafilatura 没提取到，回退到 innerText
        if not content or len(content) < 100:
            content = page.evaluate("""() => {
                // 尝试找正文容器
                const selectors = ['article', 'main', '[role="main"]',
                    '.article-content', '.post-content', '#content', '.content'];
                let best = null;
                let bestScore = 0;
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el) {
                        const score = el.innerText.length;
                        if (score > bestScore) {
                            bestScore = score;
                            best = el;
                        }
                    }
                }
                if (!best || bestScore < 500) {
                    best = document.body || document.documentElement;
                }
                return best ? best.innerText : '';
            }""")

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

        # 估算字数
        import re
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
        }

    def _auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def _submit_result(self, task_id: str, result: dict):
        httpx.post(
            f"{self.server_url}/api/browser/result",
            json={"taskId": task_id, "result": result},
            headers=self._auth_headers(),
            timeout=10,
        )

    def _submit_error(self, task_id: str, error: str):
        try:
            httpx.post(
                f"{self.server_url}/api/browser/result",
                json={"taskId": task_id, "error": error},
                headers=self._auth_headers(),
                timeout=10,
            )
        except Exception:
            pass

    def _cleanup(self):
        try:
            if self.context:
                self.context.close()
            if self.browser:
                self.browser.close()
            if hasattr(self, "_playwright"):
                self._playwright.stop()
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description="Playwright 浏览器协助 Worker")
    parser.add_argument("--server", default="http://localhost:7878",
                        help="剪藏服务地址 (默认 http://localhost:7878)")
    parser.add_argument("--profile", default=None,
                        help="Chrome 用户配置目录（带登录态），不指定则用干净浏览器")
    parser.add_argument("--headless", action="store_true",
                        help="无头模式（不推荐，登录态网站容易被检测）")
    parser.add_argument("--token", default="",
                        help="clip-server 鉴权 token（默认依次读环境变量 "
                             "CLIP_SERVER_TOKEN、tools/config.local.json）")
    args = parser.parse_args()

    if not args.token:
        args.token = os.environ.get("CLIP_SERVER_TOKEN", "")
    if not args.token:
        config_path = Path(__file__).resolve().parent / "config.local.json"
        try:
            args.token = json.loads(config_path.read_text(
                encoding="utf-8")).get("token", "")
        except Exception:
            pass

    worker = BrowserWorker(
        server_url=args.server,
        profile_dir=args.profile,
        headless=args.headless,
        token=args.token,
    )
    worker.run()


if __name__ == "__main__":
    main()

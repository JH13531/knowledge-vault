#!/usr/bin/env python3
"""
飞书知识库剪藏机器人

通过 WebSocket 长连接接收飞书群消息，提取 URL 并调用 clip-server 完成剪藏。

功能：
- 群消息中 @机器人 + URL → 自动创建剪藏任务
- 回复任务状态（进度、成功/失败）
- 支持指令：查询、重试、取消
- 消息去重（相同 URL 不重复处理）
- 忽略机器人自身的消息

用法：
    # 使用 lark-cli 当前 app 的凭据
    python3 tools/feishu-bot.py

    # 手动指定凭据
    python3 tools/feishu-bot.py --app-id cli_xxx --app-secret xxx

    # 指定 clip-server 地址
    python3 tools/feishu-bot.py --clip-server http://127.0.0.1:7878

前置条件：
1. 飞书开放平台创建应用，启用「机器人」能力
2. 应用开启「长连接」模式（事件订阅 → 使用长连接接收事件）
3. 添加事件订阅：im.message.receive_v1（接收消息）
4. 添加权限：im:message, im:message.group_at_msg, im:message:send_as_bot
5. 发布应用并添加到目标群
"""
from __future__ import annotations
import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

# ========== 日志安全：敏感信息过滤 ==========

_SENSITIVE_PATTERNS = [
    (re.compile(r'(Bearer\s+)[A-Za-z0-9_\-\.]+', re.I), r'\1***'),
    (re.compile(r'(app_secret["\s:=]+)[^\s,}\]]+', re.I), r'\1***'),
    (re.compile(r'(appSecret["\s:=]+)[^\s,}\]]+', re.I), r'\1***'),
    (re.compile(r'("token"["\s:=]+)[^\s,}\]]+', re.I), r'\1***'),
    (re.compile(r'(password["\s:=]+)[^\s,}\]]+', re.I), r'\1***'),
    (re.compile(r'(api[_-]?key["\s:=]+)[^\s,}\]]+', re.I), r'\1***'),
    (re.compile(r'(Authorization["\s:=]+)[^\s,}\]]+', re.I), r'\1***'),
    (re.compile(r'(secret["\s:=]+)[^\s,}\]]+', re.I), r'\1***'),
]


def sanitize_for_log(text: str) -> str:
    """替换日志文本中可能出现的敏感信息。"""
    if not isinstance(text, str):
        text = str(text)
    for pattern, replacement in _SENSITIVE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


# 卡片回调事件模型
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)

# ===== 配置 =====

VAULT = Path(__file__).resolve().parent.parent
CLIP_SERVER = "http://127.0.0.1:7878"
POLL_INTERVAL = 5  # 轮询任务状态间隔（秒）
# 最长轮询时间必须**长于服务端真正判定卡死的时刻**，不能只写「等于阈值」：
# 服务端阈值是 600 秒（task_queue.STALE_PENDING_SECONDS / STALE_PROCESSING_SECONDS），
# 但清扫是**每 60 秒一轮**（task_queue.start_sweeper），所以 failed 真正落库的
# 时机落在 600～660 秒之间 —— 只写 600 就是撞平，轮询会先放弃。
# 实测（任务 2283fe28）：服务端在第 606.8 秒写入「等待浏览器协助超时（超过 600 秒
# 仍无浏览器接入）」，而轮询上限也是 600 秒，于是那句精确原因被丢掉，用户最终
# 拿到的是一张什么原因都没写的兜底卡片。
# 故取 720 秒 = 600（阈值）+ 60（清扫抖动）+ 60（余量）。
# 正常抓取（等标签页 + 提取 + 生成 wiki）只需 1～2 分钟，走到这一步的必然是异常任务。
MAX_POLL_TIME = 720  # 最长轮询时间（秒）

# URL 提取正则
URL_PATTERN = re.compile(
    r'https?://[^\s<>"\')\]，。！？、；：""''（）【】《》　]+'
)

# 已处理 URL 缓存（防重复）
_processed_urls: dict[str, float] = {}
_processed_lock = threading.Lock()

# 任务 ID → 飞书消息关联
_task_chat_map: dict[str, str] = {}  # task_id → chat_id
_task_map_lock = threading.Lock()

# 飞书客户端全局引用（供卡片回调使用）
_lark_client = None


def get_lark_credentials() -> tuple[str, str]:
    """按优先级获取飞书凭据：
    1. 环境变量 LARK_APP_ID / LARK_APP_SECRET
    2. 配置文件 tools/feishu-bot-config.json
    3. 从 lark-cli 读取（如果 keychain 可用）
    """
    # 1. 环境变量
    env_id = os.environ.get("LARK_APP_ID", "")
    env_secret = os.environ.get("LARK_APP_SECRET", "")
    if env_id and env_secret:
        return env_id, env_secret

    # 2. 配置文件
    config_path = Path(__file__).resolve().parent / "feishu-bot-config.json"
    if config_path.exists():
        try:
            with open(config_path, encoding="utf-8") as f:
                cfg = json.load(f)
            app_id = cfg.get("app_id", "")
            app_secret = cfg.get("app_secret", "")
            if app_id and app_secret:
                return app_id, app_secret
        except Exception:
            pass

    # 3. lark-cli 配置
    lark_config_path = Path.home() / ".lark-cli" / "config.json"
    if lark_config_path.exists():
        try:
            with open(lark_config_path, encoding="utf-8") as f:
                config = json.load(f)
            current_name = config.get("currentApp", "")
            for app in config.get("apps", []):
                if app.get("name") == current_name:
                    app_id = app.get("appId", "")
                    secret_val = app.get("appSecret", "")
                    # 如果直接是字符串（非 keychain 引用）
                    if isinstance(secret_val, str) and secret_val and secret_val != "****":
                        return app_id, secret_val
        except Exception:
            pass

    return "", ""


def get_clip_token() -> str:
    """读取 clip-server 鉴权 token：环境变量 CLIP_SERVER_TOKEN 优先，其次 config.local.json。"""
    env_token = os.environ.get("CLIP_SERVER_TOKEN", "")
    if env_token:
        return env_token
    config_path = Path(__file__).resolve().parent / "config.local.json"
    if config_path.exists():
        try:
            with open(config_path, encoding="utf-8") as f:
                cfg = json.load(f)
            return cfg.get("token", "")
        except Exception:
            pass
    return ""


def _clip_headers() -> dict:
    """构造调用 clip-server 所需的鉴权头（未配置 token 时为空）。"""
    token = get_clip_token()
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


# ===== 机器人身份 / @提及 门禁 =====

FEISHU_OPEN_API = "https://open.feishu.cn"

# 机器人自身身份缓存（避免每次消息都查询）
_BOT_PROFILE: dict | None = None
_bot_profile_lock = threading.Lock()


def get_bot_profile() -> dict | None:
    """查询机器人自身身份（open_id / app_name），用于精确识别「@机器人」。

    流程：用 app_id/app_secret 换 tenant_access_token → GET bot/v3/info。
    成功后缓存；失败返回 None，由调用方按宽松策略处理并打印告警。
    """
    global _BOT_PROFILE
    with _bot_profile_lock:
        if _BOT_PROFILE is not None:
            return _BOT_PROFILE

    profile = None
    try:
        import requests as _req

        app_id, app_secret = get_lark_credentials()
        if not app_id or not app_secret:
            print("[!] 无法读取飞书凭据，跳过机器人身份查询")
            return None

        # 1) 换取 tenant_access_token
        r = _req.post(
            f"{FEISHU_OPEN_API}/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=10,
        )
        data = r.json()
        if data.get("code") != 0 or not data.get("tenant_access_token"):
            print(f"[!] 获取 tenant_access_token 失败: code={data.get('code')} msg={data.get('msg')}")
            return None
        token = data["tenant_access_token"]

        # 2) 查机器人自身信息（含 open_id）
        r = _req.get(
            f"{FEISHU_OPEN_API}/open-apis/bot/v3/info",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        data = r.json()
        if data.get("code") != 0:
            print(f"[!] 获取 bot/v3/info 失败: code={data.get('code')} msg={data.get('msg')}")
            return None
        # 新版返回 { "code":0, "bot": {...} }；旧版可能用 "data"，双兼容
        d = data.get("bot") or data.get("data") or {}
        profile = {
            "open_id": d.get("open_id", ""),
            "app_name": d.get("app_name", ""),
        }
        print(f"[*] 机器人身份: open_id={profile['open_id']} name={profile['app_name']}")
    except Exception as e:
        print(f"[!] 查询机器人身份异常: {e}")

    if profile and profile.get("open_id"):
        with _bot_profile_lock:
            _BOT_PROFILE = profile
        return profile
    return None


def is_self_mention(message) -> bool:
    """判断本条消息是否触发到本机器人。

    - p2p（私聊）：机器人与用户一对一，直接放行，无需 @；
    - group（群聊）：message.mentions 中必须存在 open_id == 本机器人 open_id 的条目；
    - 拿不到机器人身份时，退化为「群内存在任意 @」并打印告警（宁宽勿哑，避免机器人彻底失声）。
    """
    try:
        chat_type = getattr(message, "chat_type", "") or ""
        if chat_type != "group":
            return True

        mentions = getattr(message, "mentions", None) or []
        if not mentions:
            return False

        profile = get_bot_profile()
        if not profile:
            print("[!] 无法识别机器人身份，按「存在任意 @」放行")
            return True

        bot_open_id = profile.get("open_id", "")
        for m in mentions:
            mid = getattr(m, "id", None)
            oid = None
            if mid is not None:
                oid = getattr(mid, "open_id", None)
                if oid is None and isinstance(mid, dict):
                    oid = mid.get("open_id")
            if oid and oid == bot_open_id:
                return True
        return False
    except Exception as e:
        print(f"[!] is_self_mention 判断异常，按「未 @」忽略: {e}")
        return False


#: 消息开头可用的指令前缀 → 剪藏行为。可组合、顺序任意。
#:
#: 为什么「跳过 OCR」做成**前缀（剪藏那一刻表态）而不是卡片按钮**：
#: OCR 在 raw 落盘后即刻排队，用户看到卡片时它往往已经在跑了 —— 那时点
#: 「跳过」只能白花成本。而默认是**自动提取**，所以这个前缀是「主动叫停」
#: 而不是「授权」：不写不会漏，写了才省下这一次的调用与外发。
_LEADING_COMMANDS = (
    (("跟踪", "追踪"), "live_source"),
    (("不ocr", "noocr"), "skip_ocr"),
)


def parse_leading_commands(text: str) -> tuple[str, dict]:
    """剥掉消息开头的指令前缀，返回（剩余文本, 选项字典）。

    例：「不OCR 跟踪 https://…」→ ("https://…", {"live_source": True,
    "skip_ocr": True})。前缀最多剥 3 层（防意外死循环），未识别的前缀原样保留。
    """
    opts = {"live_source": False, "skip_ocr": False}
    text = (text or "").strip()
    for _ in range(3):
        low = text.lower()
        for prefixes, key in _LEADING_COMMANDS:
            hit = next((p for p in prefixes if low.startswith(p)), None)
            if hit:
                opts[key] = True
                text = text[len(hit):].strip()   # 按原文本切片，长度与 low 一致
                break
        else:
            break
    return text, opts


def extract_urls(text: str) -> list[str]:
    """从消息文本中提取所有 URL。"""
    urls = URL_PATTERN.findall(text or "")
    # 去重并保持顺序
    seen = set()
    result = []
    for url in urls:
        # 去掉尾部的标点
        url = url.rstrip(".,;:!?")
        if url not in seen:
            seen.add(url)
            result.append(url)
    return result


def is_duplicate(url: str, within_seconds: float = 300) -> bool:
    """检查 URL 是否在指定时间内已处理过。"""
    with _processed_lock:
        last_time = _processed_urls.get(url, 0)
        if time.time() - last_time < within_seconds:
            return True
        _processed_urls[url] = time.time()
        return False


#: 已处理过的**事件 message_id** → 时间戳。
#:
#: 为什么必须按 message_id 幂等：飞书长连接对「未及时 ACK」的事件会**重投**，
#: 同一条消息可能被投多次。实测 2026-10-14：同一条小红书分享在 09:39 / 15:05 /
#: 17:11 被投了三次（用户莫名其妙收到「📌 已存在」卡片；若是新链接还会被重复
#: 抓取、重复计费）。重投的事件 message_id 不变，而 URL 永远是那个 URL ——
#: 按 ID 去重才不会误杀「用户真的又发了一次」。
#: 与 is_duplicate() 的分工：后者是「同一链接短时间内别重复处理」，窗口 5 分钟；
#: 本机制针对**同一条消息的重投**，窗口 24 小时。
#:
#: 为什么还要落盘：重投**恰恰是跨进程重启发生的**（09:39 那条事件在 15:05、
#: 17:11 又出现，中间服务重启过多次）。只放内存的话，重启后重投又会被当成新
#: 消息处理一遍 —— 幂等就白做了。
_HANDLED_MSGS_LOG = Path(__file__).resolve().parent / "runtime" / "feishu-handled-msgs.jsonl"
_HANDLED_MSG_TTL = 24 * 3600
_handled_msgs_lock = threading.Lock()


def _load_handled_msgs() -> dict[str, float]:
    """从磁盘恢复「已处理消息」表（启动时调用一次）。"""
    out: dict[str, float] = {}
    try:
        if _HANDLED_MSGS_LOG.exists():
            for line in _HANDLED_MSGS_LOG.read_text(
                    encoding="utf-8", errors="replace").splitlines():
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                mid = str(rec.get("id") or "")
                if mid:
                    out[mid] = float(rec.get("at") or 0)
    except OSError as e:
        print(f"[!] 读取已处理消息记录失败（{e}），本次不做跨重启去重")
    return out


def _rewrite_handled_msgs(records: dict[str, float]) -> None:
    """重写记录文件（清理过期项）。写入临时文件再替换，避免半截文件。"""
    try:
        _HANDLED_MSGS_LOG.parent.mkdir(parents=True, exist_ok=True)
        tmp = _HANDLED_MSGS_LOG.with_suffix(".tmp")
        tmp.write_text("".join(
            json.dumps({"id": k, "at": v}, ensure_ascii=False) + "\n"
            for k, v in records.items()), encoding="utf-8")
        tmp.replace(_HANDLED_MSGS_LOG)
    except OSError as e:
        print(f"[!] 重写已处理消息记录失败（{e}）")


_handled_msgs: dict[str, float] = _load_handled_msgs()


def _is_replayed_message(message_id: str) -> bool:
    """该 message_id 是否已处理过（含飞书重投）；首次见到即登记并落盘。"""
    if not message_id:
        return False
    now = time.time()
    with _handled_msgs_lock:
        expired = [k for k, t in _handled_msgs.items() if now - t > _HANDLED_MSG_TTL]
        for k in expired:
            _handled_msgs.pop(k, None)
        if expired:
            _rewrite_handled_msgs(_handled_msgs)
        if message_id in _handled_msgs:
            return True
        _handled_msgs[message_id] = now
        try:                      # 追加一行即可，无需重写整个文件
            _HANDLED_MSGS_LOG.parent.mkdir(parents=True, exist_ok=True)
            with _HANDLED_MSGS_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"id": message_id, "at": now},
                                   ensure_ascii=False) + "\n")
        except OSError as e:
            print(f"[!] 记录已处理消息失败（{e}）—— 重启后可能重复处理")
        return False


def call_clip_server(url: str, live_source: bool = False, force: bool = False,
                     timeout: float | tuple = (5, 60), ocr: bool = True) -> dict:
    """调用 clip-server 创建剪藏任务。

    返回统一结构：成功时含 filename / taskId；失败时含 error，并尽量附带
    message / detail / _http_status 以便上层展示更详细的错误原因。

    `ocr=False`（消息里加了「不OCR」前缀）表示本次**主动跳过**图片文字提取；
    默认 True —— 命中「图多字少」就自动提取，用户什么都不用做。
    """
    import requests

    # via 让服务端把这条任务标记为「飞书」来源，任务面板才能按渠道分类展示，
    # 而不是把飞书、插件、悬浮按钮的结果混在一起（用户会以为结果不一致）。
    payload = {"url": url, "liveSource": live_source, "via": "飞书"}
    if not ocr:
        payload["ocr"] = False
    if force:
        payload["force"] = True

    try:
        resp = requests.post(
            f"{CLIP_SERVER}/api/clip-url",
            json=payload,
            headers={"Content-Type": "application/json", **_clip_headers()},
                timeout=timeout,
        )
        data = resp.json()
        if isinstance(data, dict):
            data.setdefault("_http_status", resp.status_code)
        return data
    except requests.exceptions.Timeout:
        return {"success": False, "error": "clip-server 超时",
                "detail": "请求超时（连接 5s / 读取 60s），文件可能已保存，请发「查询」确认"}
    except requests.exceptions.ConnectionError:
        return {"success": False, "error": "无法连接 clip-server",
                "detail": f"请确认 clip-server 已启动（{CLIP_SERVER}）"}
    except Exception as e:
        return {"success": False, "error": "clip-server 请求失败", "detail": str(e)}


def poll_task_status(task_id: str, timeout: float = MAX_POLL_TIME,
                     wait_settled: bool = False) -> dict | None:
    """轮询任务状态直到完成或超时。

    `wait_settled=True` 时还要求**后台异步加工**（wiki / OCR）已经落定：
    raw 落盘那一刻任务就可能已标 `done`，但 wiki / OCR 仍在后台线程里跑
    （`result.asyncPending=True`）。只判 done/failed 会读到「⏳ 后台生成中」
    这个**中间态**就返回，于是用户在飞书上再也等不到最终结论。
    """
    import requests

    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = requests.get(
                f"{CLIP_SERVER}/api/task/{task_id}",
                timeout=5,
                headers=_clip_headers(),
            )
            data = resp.json()
            if data.get("status") in ("done", "failed"):
                if not wait_settled or not (data.get("result") or {}).get("asyncPending"):
                    return data
        except Exception:
            pass
        time.sleep(POLL_INTERVAL)
    return None


def send_feishu_message(client, chat_id: str, text: str):
    """通过飞书 API 发送消息到群。"""
    try:
        import lark_oapi as lark
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest, CreateMessageRequestBody
        )

        body = CreateMessageRequestBody.builder() \
            .receive_id(chat_id) \
            .msg_type("text") \
            .content(json.dumps({"text": text})) \
            .build()

        request = CreateMessageRequest.builder() \
            .receive_id_type("chat_id") \
            .request_body(body) \
            .build()

        response = client.im.v1.message.create(request)
        if not response.success():
            print(f"[!] 发送消息失败: {response.code} - {response.msg}")
    except Exception as e:
        print(f"[!] 发送消息异常: {e}")


def send_feishu_card(client, chat_id: str, title: str, fields: list[tuple[str, str]],
                     color: str = "blue", buttons: list[dict] | None = None):
    """发送飞书交互卡片。

    buttons 格式：[{"text": "按钮文字", "value": {"action": "xxx", ...}, "type": "primary|default"}]
    """
    try:
        import lark_oapi as lark
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest, CreateMessageRequestBody
        )

        # 构建卡片元素
        elements = []
        for label, value in fields:
            elements.append({
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**{label}：** {value}"
                }
            })

        # 添加按钮（action 元素）
        if buttons:
            elements.append({
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": btn["text"]},
                        "type": btn.get("type", "default"),
                        "value": btn.get("value", {}),
                    }
                    for btn in buttons
                ]
            })

        # 颜色映射
        color_map = {
            "blue": "blue",
            "green": "green",
            "red": "red",
            "orange": "orange",
        }

        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": color_map.get(color, "blue"),
            },
            "elements": elements,
        }

        body = CreateMessageRequestBody.builder() \
            .receive_id(chat_id) \
            .msg_type("interactive") \
            .content(json.dumps(card)) \
            .build()

        request = CreateMessageRequest.builder() \
            .receive_id_type("chat_id") \
            .request_body(body) \
            .build()

        # 发卡片是用户**唯一的反馈通道**：网络抖一下就静默失败，用户眼里就是
        # 「机器人毫无反应」（实测 2026-10-14：一次瞬时 HTTPS 抖动让失败卡片
        # 发不出去，用户什么都没收到）。因此网络层异常重试一次。
        response = None
        last_err = None
        for attempt in range(2):
            try:
                response = client.im.v1.message.create(request)
                break
            except Exception as e:
                last_err = e
                if attempt == 0:
                    time.sleep(1.5)
        if response is None:
            raise last_err if last_err else RuntimeError("发送卡片失败")
        if not response.success():
            print(f"[!] 发送卡片失败: {response.code} - {response.msg}")
    except Exception as e:
        print(f"[!] 发送卡片异常: {e}")


# ===== 事件处理 =====

def _wiki_ref_of(raw_path: Path) -> str:
    """从 raw 的 frontmatter 读第一条 wiki_refs（与 clip-server._wiki_ref_for 同口径）。

    「重新生成 wiki」的回执要报 **wiki 页名**而非 raw 文件名 —— 用户关心的
    是解读产物在哪，raw 名他在卡片上已经看过（2026-10-15 用户反馈）。
    wiki 页路径的权威来源是 wiki_gen 回填的 wiki_refs，不靠标题猜 slug。
    """
    try:
        text = raw_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    text = text.replace("\r\n", "\n")
    block = re.search(r'^wiki_refs:[ \t]*\n((?:[ \t]*-[ \t]*\S.*\n)+)', text, re.M)
    if block:
        return (block.group(1).strip().splitlines()[0]
                .lstrip("-").strip().strip('"').strip("'"))
    inline = re.search(r'^wiki_refs:[ \t]*\[(.*?)\]', text, re.M)   # 行内数组写法
    if inline:
        items = [x.strip().strip('"').strip("'")
                 for x in inline.group(1).split(",") if x.strip()]
        return items[0] if items else ""
    return ""


def handle_card_action(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
    """处理卡片按钮点击回调。

    通过 WebSocket 接收，不需要 HTTP 服务器。
    data.event.action.value 包含按钮的 value 字典。
    """
    try:
        event = data.event
        if not event or not event.action:
            return P2CardActionTriggerResponse({})

        action_value = event.action.value or {}
        action_type = action_value.get("action", "")

        chat_id = ""
        if event.context:
            chat_id = event.context.open_chat_id or ""

        print(f"[*] 卡片回调: action={action_type}, value={sanitize_for_log(str(action_value))}")

        if action_type == "view_wiki":
            wiki_path = action_value.get("wiki_path", "未知")
            return P2CardActionTriggerResponse({
                "toast": {"type": "info", "content": f"📄 Wiki: {wiki_path}"}
            })

        elif action_type == "reprocess":
            filepath = action_value.get("filepath", "")
            if filepath and chat_id:
                # 后台触发 wiki 重新生成
                def _do_reprocess():
                    try:
                        from wiki_gen import generate_wiki_topic
                        raw_path = (VAULT / filepath).resolve()
                        if VAULT not in raw_path.parents:
                            raise ValueError("文件路径越界")
                        if raw_path.exists():
                            raw_content = raw_path.read_text(encoding="utf-8")
                            ok = generate_wiki_topic(raw_path, raw_content)
                            if ok:
                                # 报 wiki 页名（解读产物），取不到才退回 raw 名
                                wiki_rel = _wiki_ref_of(raw_path)
                                wiki_name = Path(wiki_rel).name if wiki_rel else ""
                                send_feishu_message(
                                    _lark_client, chat_id,
                                    f"✅ 已重新解读完成：{wiki_name or raw_path.name}"
                                    + (f"\n📄 {wiki_rel}" if wiki_rel else "")
                                )
                            else:
                                send_feishu_message(
                                    _lark_client, chat_id,
                                    f"❌ 重新解读失败：{raw_path.name}\n"
                                    "三级精加工通道均不可用（PATH 中找不到 "
                                    "claude/codex，且未配置 API key），raw 保留，"
                                    "可稍后重试。"
                                )
                        else:
                            send_feishu_message(
                                _lark_client, chat_id,
                                f"❌ 文件不存在：{filepath}"
                            )
                    except Exception as e:
                        send_feishu_message(
                            _lark_client, chat_id,
                            f"❌ 重新解读失败：{e}"
                        )

                t = threading.Thread(target=_do_reprocess, daemon=True)
                t.start()

            return P2CardActionTriggerResponse({
                "toast": {"type": "info", "content": "🔄 正在重新解读，完成后通知…"}
            })

        elif action_type == "open_url":
            url = action_value.get("url", "")
            return P2CardActionTriggerResponse({
                "toast": {"type": "info", "content": f"🔗 原文链接已复制"}
            })

        elif action_type == "retry_task":
            task_id = action_value.get("task_id", "")
            if task_id and chat_id:
                def _do_retry():
                    try:
                        import requests as req
                        resp = req.post(f"{CLIP_SERVER}/api/task/{task_id}/retry", timeout=10, headers=_clip_headers())
                        data = resp.json()
                        if data.get("success"):
                            send_feishu_message(_lark_client, chat_id,
                                f"🔄 任务 {task_id[:8]} 已重新入队")
                            # 重试只是重新入队 —— 结局（成功/失败/超时）也要有人盯。
                            # 过去这里漏了重挂 watcher，点完「重试」后飞书就永远
                            # 没有下文（2026-10-15 实测：重试其实失败了，但用户
                            # 毫不知情）。与入队路径共用同一个 watcher。
                            with _task_map_lock:
                                _task_chat_map[task_id] = chat_id
                            threading.Thread(
                                target=_watch_browser_task,
                                args=(_lark_client, chat_id, task_id,
                                      str(data.get("url") or "")),
                                daemon=True).start()
                        else:
                            send_feishu_message(_lark_client, chat_id,
                                f"❌ 重试失败: {data.get('error', '未知')}")
                    except Exception as e:
                        send_feishu_message(_lark_client, chat_id, f"❌ 重试失败: {e}")
                threading.Thread(target=_do_retry, daemon=True).start()
            return P2CardActionTriggerResponse({
                "toast": {"type": "info", "content": "🔄 正在重试…"}
            })

        elif action_type == "cancel_task":
            task_id = action_value.get("task_id", "")
            if task_id and chat_id:
                def _do_cancel():
                    try:
                        import requests as req
                        resp = req.post(f"{CLIP_SERVER}/api/task/{task_id}/cancel", timeout=10, headers=_clip_headers())
                        data = resp.json()
                        if data.get("success"):
                            send_feishu_message(_lark_client, chat_id,
                                f"❌ 任务 {task_id[:8]} 已取消")
                        else:
                            send_feishu_message(_lark_client, chat_id,
                                f"❌ 取消失败: {data.get('error', '未知')}")
                    except Exception as e:
                        send_feishu_message(_lark_client, chat_id, f"❌ 取消失败: {e}")
                threading.Thread(target=_do_cancel, daemon=True).start()
            return P2CardActionTriggerResponse({
                "toast": {"type": "info", "content": "正在取消…"}
            })

        elif action_type == "force_overwrite":
            url = action_value.get("url", "")
            if url and chat_id:
                def _do_force_overwrite():
                    try:
                        result = call_clip_server(url, live_source=False, force=True)
                        handle_clip_result(_lark_client, chat_id, url, result)
                    except Exception as e:
                        send_feishu_message(_lark_client, chat_id, f"❌ 强制覆盖失败: {e}")
                threading.Thread(target=_do_force_overwrite, daemon=True).start()
            return P2CardActionTriggerResponse({
                "toast": {"type": "info", "content": "⚠️ 正在强制覆盖，完成后通知…"}
            })

        else:
            return P2CardActionTriggerResponse({})

    except Exception as e:
        print(f"[!] 卡片回调处理异常: {e}")
        return P2CardActionTriggerResponse({})


def make_event_handler(lark_client):
    """创建飞书事件处理器。"""
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

    def handle_message(data: P2ImMessageReceiveV1):
        """处理收到的消息事件。"""
        try:
            event = data.event
            message = event.message
            sender = event.sender

            # 诊断日志：确认事件是否到达（带 message_id，便于核对重投）
            print(f"[*] 收到消息事件: msg={message.message_id}, "
                  f"type={message.message_type}, "
                  f"sender_type={sender.sender_type}, chat={message.chat_id}, "
                  f"content={sanitize_for_log(message.content)[:120]}")

            # 忽略机器人自己的消息
            if sender.sender_type == "app":
                return

            chat_id = message.chat_id
            message_type = message.message_type
            message_id = message.message_id

            # 只处理文本消息
            if message_type != "text":
                return

            # 幂等闸门：飞书对「未及时 ACK」的事件会**重投**，同一条消息可能被
            # 处理多次（实测 2026-10-14：同一条小红书分享在 09:39 / 15:05 / 17:11
            # 被投了三次）。必须挡在最前面 —— 否则指令会被重复执行、链接会被
            # 重复抓取，用户还会收到莫名其妙的卡片。详见 _is_replayed_message()。
            if _is_replayed_message(message_id):
                print(f"[*] 忽略重复投递的消息: msg={message_id} "
                      f"text={sanitize_for_log(message.content)[:60]}")
                return

            # 解析消息内容
            try:
                content = json.loads(message.content)
                text = content.get("text", "")
            except (json.JSONDecodeError, AttributeError):
                return

            # 去掉 @机器人 的部分
            text = re.sub(r"@_user_\d+\s*", "", text).strip()

            # @提及 门禁：群聊中必须显式 @到本机器人本尊，否则直接忽略。
            # （指令、URL 剪藏统一受此约束；私聊 p2p 无需 @）
            if not is_self_mention(message):
                print(f"[*] 未 @到机器人，忽略: msg={message_id} chat={chat_id} text={text[:50]}")
                return

            # 检查是否包含指令
            if text.startswith("帮助") or text.startswith("help"):
                handle_help_command(lark_client, chat_id)
                return
            if text.startswith("统计"):
                handle_stats_command(lark_client, chat_id)
                return
            if text.startswith("查询"):
                handle_query_command(lark_client, chat_id, text)
                return
            if text.startswith("重试"):
                handle_retry_command(lark_client, chat_id, text)
                return
            if text.startswith("取消"):
                handle_cancel_command(lark_client, chat_id, text)
                return

            # 剥掉开头的指令前缀（「跟踪」「不OCR」，可组合、顺序任意）
            text, opts = parse_leading_commands(text)

            # 提取 URL
            urls = extract_urls(text)
            if not urls:
                return

            # **立即返回，让 SDK 尽快 ACK。** process_url() 里要做网络抓取
            # （可达几十秒），在事件回调里同步跑会拖过飞书的 ACK 超时，事件
            # 于是被服务端反复重投 —— 2026-10-14 实测同一条分享被投了三次
            # （09:39 / 15:05 / 17:11），用户收到莫名其妙的「已存在」卡片。
            # 与 _watch_browser_task 的异步化同理：回调只负责派发，不负责干活。
            live_source = opts["live_source"]
            skip_ocr = opts["skip_ocr"]

            def _run_clips():
                for u in urls:
                    try:
                        process_url(lark_client, chat_id, u, message_id,
                                    live_source=live_source, skip_ocr=skip_ocr)
                    except Exception as exc:
                        print(f"[!] 处理 URL 异常（{u[:50]}）: {exc}")

            threading.Thread(target=_run_clips, daemon=True,
                             name="feishu-clip").start()

        except Exception as e:
            print(f"[!] 消息处理异常: {e}")
            import traceback
            traceback.print_exc()

    dispatcher = lark.EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(handle_message) \
        .register_p2_card_action_trigger(handle_card_action) \
    .build()

    return dispatcher


def _start_settled_notify(client, chat_id: str, task_id: str, url: str,
                          ocr: bool = False):
    """后台加工（wiki / OCR）落定后补发第二张结论卡。

    「完整入库」= raw + wiki，而入库那一刻 wiki 往往还在后台线程里跑 ——
    先发的那张卡只能说「⏳ 后台生成中」。若不再补一张，用户就永远不知道
    最后到底成没成。图多字少的笔记尤其明显：OCR 要走几十秒到几分钟，
    跑完却零通知（用户 2026-10-14 报）。

    服务端在加工落定时会把真实结论回写任务记录并清掉 `asyncPending`
    （见 clip-server._settle_task_async），这里等那个时刻再读一次。
    """
    def _run():
        settled = poll_task_status(task_id, wait_settled=True)
        # 轮询到上限仍没落定 → 必须补一张兜底卡，不能让「⏳ 后台生成中」
        # 成为用户看到的最后一句话。
        if not settled:
            send_feishu_card(client, chat_id, "⌛ 后台加工仍未出结论", [
                ("任务", task_id),
                ("说明", "超过 12 分钟仍未收到后台加工的结论。"
                         "可发送「查询 " + task_id + "」查看最新状态，"
                         "或点「重试」重新执行。"),
                ("链接", url[:60]),
            ], color="orange", buttons=[{
                "text": "🔄 重试",
                "value": {"action": "retry_task", "task_id": task_id},
                "type": "primary",
            }])
            return

        info = settled.get("result") or {}
        wk = info.get("wiki") or {}
        st = str(wk.get("status") or "")
        fname = str(info.get("filename") or "")
        filepath = str(info.get("filepath") or "")

        # wiki 页面文件名（落定时由 _settle_task_async 从 raw 的 wiki_refs 回写）
        wiki_file = Path(str(info.get("wikiFile") or "")).name

        fields = [("任务", task_id)]
        if fname:
            fields.append(("Raw", fname[:50]))
        if info.get("wordCount"):
            # OCR 补进来的文字算在字数里（口径见 content_measure.count_words），
            # 不标注的话用户会以为字数统计漏了图片内容。
            _wc = f"约 {info.get('wordCount')} 字"
            if ocr:
                _wc += "（含图片文字）"
            fields.append(("字数", _wc))

        # ① 未完成：wiki 失败 / 素材被撤回待审区
        if settled.get("status") == "failed" or st in ("failed", "demoted"):
            kind = str(wk.get("kind") or "")
            reason = str(wk.get("reason") or "未知原因")
            fields.append(("原因", reason[:150]))
            if kind == "content_incomplete":
                fields.append(("说明", "素材不完整，未入库。待审区保留了原始链接，"
                                       "补齐后可重试。"))
            elif kind == "interstitial":
                fields.append(("说明", "抓取被风控拦截，未入库。请在浏览器中打开"
                                       "该链接、完成人机验证后重新剪藏。"))
            else:
                fields.append(("说明", "内容已存入 raw，但 wiki 未生成。"
                                       "可到「任务队列」查看后重试。"))
            fields.append(("链接", url[:60]))
            send_feishu_card(client, chat_id, "⚠️ 后台加工未完成", fields,
                             color="orange")
            return

        # ② 完整入库
        if st == "ok":
            # Wiki 栏直接给页面文件名（见第一张卡的同源注释）
            if wiki_file:
                fields.append(("Wiki", wiki_file + ("（含图片文字）" if ocr else "")))
            else:
                fields.append(("Wiki", "✅ 已生成（含图片文字）" if ocr else "✅ 已生成"))
            fields.append(("链接", url[:60]))
            send_feishu_card(client, chat_id, "✅ 剪藏完成", fields, color="green")
            return

        # ③ 没过质量门禁（skipped）→ raw 保留，wiki 未生成
        fields.append(("Wiki", "⚠️ 未生成 —— " +
                       str(wk.get("reason") or "未通过质量门禁")[:100]))
        fields.append(("说明", "内容已存入 raw。若正文主要在图片中，"
                               "可配置 OCR 通道后重新生成 wiki。"))
        fields.append(("链接", url[:60]))
        buttons = None
        if filepath:
            buttons = [{
                "text": "🔄 重新生成 wiki",
                "value": {"action": "reprocess", "filepath": filepath},
                "type": "primary",
            }]
        send_feishu_card(client, chat_id, "⚠️ raw 已入库，但 wiki 未生成",
                         fields, color="orange", buttons=buttons)

    threading.Thread(target=_run, daemon=True, name="feishu-settled").start()


def process_url(client, chat_id: str, url: str, message_id: str,
                live_source: bool = False, skip_ocr: bool = False):
    """处理单个 URL：去重 → 调用 clip-server → 通知结果。"""
    # 去重
    if is_duplicate(url):
        print(f"[*] 跳过重复 URL: {url[:60]}")
        return

    tags = ("" if not live_source else " (live-source)") + (" (不OCR)" if skip_ocr else "")
    print(f"[+] 处理 URL: {url[:60]}{tags}")

    # 调用 clip-server
    result = call_clip_server(url, live_source=live_source, ocr=not skip_ocr)
    handle_clip_result(client, chat_id, url, result, live_source=live_source)


def _watch_browser_task(client, chat_id: str, task_id: str, url: str):
    """后台轮询浏览器协助任务直到落定，并补发结论卡。

    原先是 handle_clip_result() 里「放入浏览器协助队列」分支的闭包；
    retry_task 回调过去只重新入队、不重挂本 watcher —— 用户点完「重试」
    后无论成败都再没下文（2026-10-15 实测：重试失败用户毫不知情）。
    提炼为模块级函数，入队与重试两处共用。
    """
    # wait_settled：浏览器抓完之后 wiki / OCR 还在后台跑时，任务已标 done
    # 但结论未定。必须等到落定，否则这里读到「⏳ 后台生成中」就发卡走人，
    # 用户再也等不到最终结论（与直抓路径同一口径）。
    task_result = poll_task_status(task_id, wait_settled=True)
    # 轮询到上限仍无结论时 poll_task_status() 返回 None。
    # 这里**必须**补一张卡片：否则用户只看到「⏳ 等待浏览器抓取」，
    # 之后永远没有下文 —— 无法判断是仍在排队、已超时，还是通知丢了。
    # （服务端在 processing 超时后会把任务置为 failed，但那一刻轮询往往
    #   也刚好到期，两边擦肩而过，于是结果既没被读到、也没被告知。）
    if not task_result:
        send_feishu_card(client, chat_id, "⌛ 尚未收到抓取结果", [
            ("任务", task_id),
            ("状态", "超过 12 分钟仍未收到结果回传"),
            ("说明", "浏览器未在超时前接入。请打开浏览器并确认 Vault 扩展"
                     "已加载（chrome://extensions 中可见），然后点「重试」。"),
            ("链接", url[:60]),
        ], color="orange", buttons=[{
            "text": "🔄 重试",
            "value": {"action": "retry_task", "task_id": task_id},
            "type": "primary",
        }])
        return

    if task_result.get("status") == "done":
        info = task_result.get("result", {})
        if info.get("error") == "duplicate":
            # 与「直接入库」路径保持一致：都要给出「强制覆盖」按钮。
            # 浏览器协助路径过去只渲染三行文字、不带按钮，用户看到
            # 「已存在于知识库」却无从下手 —— 明明有可用操作却藏起来了。
            old_wc = info.get("existing_word_count", 0)
            new_wc = info.get("new_word_count", 0)
            has_wiki = info.get("has_wiki", False)
            existing_wiki = info.get("existing_wiki", "")
            dfields = [
                ("提示", "该内容已在知识库中"),
                ("已有文件", str(info.get("existing_file", ""))[:50]),
                ("链接", url[:60]),
            ]
            if old_wc:
                dfields.append(("已有字数", f"约 {old_wc} 字"))
            if new_wc:
                dfields.append(("新内容字数", f"约 {new_wc} 字"))
            if info.get("newer_more_complete"):
                dfields.append(("说明", "本次抓到的内容更完整，上一版可能不完整；"
                                        "如需替换请点「强制覆盖」。"))
            if has_wiki:
                dfields.append(
                    ("Wiki", f"✅ 已生成 {existing_wiki}" if existing_wiki else "✅ 已生成"))
            else:
                dfields.append(("Wiki", "⚠️ 未生成（孤儿 raw）"))
            dbuttons = []
            if info.get("canOverwrite"):
                dbuttons.append({
                    "text": "⚠️ 强制覆盖",
                    "value": {"action": "force_overwrite", "url": url},
                    "type": "danger",
                })
            if not has_wiki and info.get("existing_file"):
                dbuttons.append({
                    "text": "🔄 重新生成 wiki",
                    "value": {"action": "reprocess",
                              "filepath": info.get("existing_file")},
                    "type": "primary",
                })
            send_feishu_card(client, chat_id, "📌 已存在", dfields,
                             color="orange", buttons=dbuttons or None)
        elif info.get("success"):
            # ⚠️ 必须先判 demoted。降级（未入库、raw 已撤回）时服务端会把
            # filename 刻意置成空串表达「没有文件」，但 success 仍是 True。
            # 过去这里直接读 filename，于是渲染出一张绿色的「✅ 剪藏完成」
            # 而「文件」一栏是空白的卡片 —— 用户以为成功入了一个空文件。
            # 与 popup.js 保持同一口径：成功 / 降级 / 失败三分支。
            wk = info.get("wiki") or {}
            if wk.get("status") == "demoted" or not str(info.get("filename") or "").strip():
                kind = wk.get("kind") or ""
                reason = wk.get("reason") or "服务端未说明原因"
                if kind == "content_incomplete":
                    head = "⚠️ 未入库：内容不完整"
                    if "要求登录" in str(reason):
                        hint = ("请确认浏览器已登录 YouTube，并在该视频页停留数秒"
                                "（让页面加载完成）后重新剪藏。")
                    else:
                        hint = ("视频缺少字幕/转录时不应入库"
                                "（仅有标题与简介，等同于书签）。")
                elif kind == "interstitial":
                    head = "⚠️ 未入库：抓取被风控拦截"
                    hint = "请在浏览器中打开该链接、完成人机验证后，再重新剪藏。"
                else:
                    head = "⚠️ 未入库：wiki 未生成，raw 已撤回"
                    hint = "通道恢复后可重新剪藏。"
                dfields = [
                    ("任务", task_id),
                    ("原因", str(reason)[:150]),
                    ("说明", hint),
                    ("链接", url[:60]),
                ]
                card = wk.get("inbox_card")
                if card:
                    dfields.append(("待审区条目", str(card)[:50]))
                else:
                    dfields.append(("待审区条目", "条目仍保留在待审区，可重试"))
                send_feishu_card(client, chat_id, head, dfields, color="orange")
            else:
                sfields = [
                    ("任务", task_id),
                    ("Raw", str(info.get("filename", ""))[:50]),
                    ("字数", f"约 {info.get('wordCount', 0)} 字"),
                ]
                # 与「直接成功」路径保持同一口径：服务端抓到动态渲染嫌疑
                # 骨架时要主动提示改用浏览器剪藏（浏览器协助路径正常
                # 不会有这个标记，此处仅为两条路径渲染一致而保留）。
                suspect = bool(info.get("dynamicRenderSuspect"))
                if suspect:
                    sfields.append(("提示", info.get("dynamicRenderNote") or
                                   "该页面疑似依赖动态渲染，内容可能不完整，"
                                   "建议在浏览器中打开后再剪藏。"))
                img_warn = str(info.get("imageWarning") or "")
                if img_warn:
                    sfields.append(("图片", "⚠️ " + img_warn))
                # ⚠️ 「完整入库」= raw + wiki。raw 写了但 wiki 没写
                # **不能报「剪藏完成」** —— 与插件面板、系统通知保持同一
                # 判据（popup 的 isWikiBad / background 的 buildIngestOutcome）。
                # 过去这里只判了 demoted，于是 wiki 生成失败时飞书卡片
                # 仍是一张绿色的「✅ 剪藏完成」，与插件里的
                # 「⚠️ raw 已入库，但 wiki 未生成」互相矛盾
                # （用户 2026-10-09 报「通知和插件说的不一样」）。
                wk_status = wk.get("status") or ""
                wiki_bad = bool(wk_status) and wk_status not in ("ok", "pending")
                if wiki_bad:
                    sfields.append(("Wiki", "❌ 未生成 —— " +
                                    str(wk.get("reason") or "未知原因")[:120]))
                    sfields.append(("说明", "内容已存入 raw，但 wiki 未生成；"
                                   "可在待审区或「任务队列」重试。"))
                elif wk_status == "pending":
                    sfields.append(("Wiki", "⏳ 后台生成中"))
                elif wk_status == "ok":
                    _wf = Path(str(info.get("wikiFile") or "")).name
                    sfields.append(("Wiki", _wf if _wf else "✅ 已生成"))

                head = ("⚠️ raw 已入库，但 wiki 未生成" if wiki_bad
                        else ("✅ 剪藏完成（内容可能不完整）"
                              if (suspect or img_warn)
                              else "✅ 剪藏完成"))
                send_feishu_card(client, chat_id,
                                 head,
                                 sfields,
                                 color="orange" if (suspect or img_warn or wiki_bad)
                                 else "green")
        else:
            send_feishu_card(client, chat_id, "❌ 浏览器抓取未完成", [
                ("任务", task_id),
                ("原因", str(info.get("reason") or info.get("error") or "服务端未说明原因")[:100]),
                ("链接", url[:60]),
            ], color="red")
    else:
        # 浏览器协助任务的失败：根因几乎都是「浏览器没开 / 没登录目标站点」，
        # 而非代码缺陷。过去这里只回一句技术报错（如 No current window），
        # 用户对着晦涩文案发懵。改成人话 + 一键重试（默认动作）。
        ferr = str(task_result.get("error") or "未知错误")
        fhead, fhint = _browser_assist_failure_hint(ferr)
        # 「原因」只取服务端判词的第一句：其后半段常写的是**另一种**成因
        # （如「若该站点在本机无法访问，浏览器侧同样抓不到」），与
        # 「提示只说一种成因、只给一个可执行动作」相冲；该做什么交给下面的
        # 「说明」。浏览器任务失败（如 No current window）不含分隔符，原样保留。
        fcause = ferr.split("——")[0].strip() or "未知错误"
        send_feishu_card(client, chat_id, fhead, [
            ("任务", task_id),
            ("原因", fcause[:100]),
            ("说明", fhint),
            ("链接", url[:60]),
        ], color="red", buttons=[{
            "text": "🔄 重试",
            "value": {"action": "retry_task", "task_id": task_id},
            "type": "primary",
        }])


def handle_clip_result(client, chat_id: str, url: str, result: dict, live_source: bool = False):
    """根据 clip-server 的返回结果，发送对应的飞书卡片。"""
    # 直接成功
    if result.get("success") and result.get("filename"):
        title = result.get("filename", "").replace(".md", "")
        word_count = result.get("wordCount", 0)
        platform = result.get("platform", "")
        version = result.get("version", "")
        filepath = result.get("filepath", "")

        fields = [
            ("平台", platform or "网页"),
            ("字数", f"约 {word_count} 字"),
            # 「Raw」而非「文件」：与 wiki 层并列时，三层架构（raw/wiki）更清楚，
            # 也避免与「已有文件」（去重场景）混淆。
            ("Raw", str(result.get("filename") or "")[:50]),
            ("链接", url[:60]),
        ]
        if live_source:
            fields.append(("跟踪", "✅ live-source"))
        if version:
            fields.append(("版本", version))

        # 动态渲染嫌疑：服务端只抓到静态骨架，内容可能明显少于浏览器中的实际页面。
        # 这不是失败（内容已入库），但**必须主动告知** —— 用户是在手机上看不到
        # 页面差异的，若不给提示会以为「知识库抓得不对」，进而反复重发链接。
        if result.get("dynamicRenderSuspect"):
            fields.append(("提示", result.get("dynamicRenderNote") or
                           "该页面疑似依赖动态渲染，服务端获取的内容可能不完整，"
                           "建议在浏览器中打开后再剪藏。"))

        # 交互按钮
        buttons = [
            {
                "text": "🔄 重新解读",
                "value": {"action": "reprocess", "filepath": filepath},
                "type": "primary",
            },
            {
                "text": "🔗 打开原文",
                "value": {"action": "open_url", "url": url},
                "type": "default",
            },
        ]

        suspect = bool(result.get("dynamicRenderSuspect"))
        # 图片未全部本地化：raw 里保留了远程地址，源站一旦删除就永久丢失。
        # 与「动态渲染嫌疑」同理 —— 内容确实入库了，但**必须主动告知**，
        # 不能让用户以为手里是一份完整快照（过去只写服务端日志，用户看不到）。
        img_warn = str(result.get("imageWarning") or "")
        if img_warn:
            fields.append(("图片", "⚠️ " + img_warn))

        # ⚠️ 「完整入库」= raw + wiki —— 与插件面板 / 系统通知同一判据。
        # raw 写了但 wiki 没写时不能报「剪藏成功」（详见另一处同源注释）。
        wk = result.get("wiki") or {}
        wk_status = wk.get("status") or ""
        wiki_bad = bool(wk_status) and wk_status not in ("ok", "pending")
        if wiki_bad:
            fields.append(("Wiki", "❌ 未生成 —— " +
                           str(wk.get("reason") or "未知原因")[:120]))
            fields.append(("说明", "内容已存入 raw，但 wiki 未生成；"
                           "可在待审区或「任务队列」重试。"))
        elif wk_status == "pending":
            if wk.get("tier") == "ocr":
                # 图多字少的笔记，单看「字数：约 28 字」会让人以为抓漏了 ——
                # 必须同时说明「内容在图片里、正在提取」，否则用户会怀疑剪藏没抓全。
                _imgs = result.get("imageCount") or 0
                if _imgs:
                    fields.append(("图片", f"{_imgs} 张（文字在其中，正在提取）"))
                fields.append(("Wiki", "⏳ 正在提取图片文字（OCR），完成后自动生成"))
            else:
                # OCR 与普通 wiki 都叫 pending，但用户等的时长差一个量级，
                # 文案要分开说，否则「后台生成中」会让人以为马上就好。
                fields.append(("Wiki", "⏳ 后台生成中，完成后发结论卡"))
        elif wk_status == "ok":
            # Wiki 栏直接给**页面文件名**（有名字即代表已生成，比「✅ 已生成」
            # 更有信息量）；取不到名字时才退回状态文案。
            _wf = Path(str(result.get("wikiFile") or "")).name
            fields.append(("Wiki", _wf if _wf else "✅ 已生成"))

        # pending 时第一张卡是**中间态**，不能喊「剪藏成功」——与浏览器协助
        # 链路的「⏳ 等待浏览器抓取」对齐：两条链路都只在 wiki 落定后由
        # 结论卡（✅ 剪藏完成 / ⚠️ 未完成）宣告最终结果（2026-10-15 用户反馈：
        # 小红书第一张卡就说成功，与 bilibili 体验不一致）。
        pending = (wk_status == "pending")
        head = ("⚠️ raw 已入库，但 wiki 未生成" if wiki_bad
                else ("⏳ 剪藏处理中" if pending
                      else ("✅ 剪藏成功（内容可能不完整）" if (suspect or img_warn)
                            else "✅ 剪藏成功")))
        color = ("orange" if (suspect or img_warn or wiki_bad)
                 else ("blue" if pending else "green"))
        send_feishu_card(client, chat_id,
                         head,
                         fields,
                         color=color,
                         buttons=buttons)

        # 后台仍在加工 → 先发上面这张，再挂一个轮询，等落定后补发**第二张**结论卡。
        # 不做的话用户看完这张就再没下文：图多字少的笔记 OCR 要跑几十秒到几分钟，
        # 那段时间里界面只有「⏳」，跑完（成功或失败）都没人告诉用户。
        tid = str(result.get("taskId") or "")
        if wk_status == "pending" and tid:
            _start_settled_notify(client, chat_id, tid, url,
                                  ocr=(wk.get("tier") == "ocr"))
        return

    # 重复：展示 wiki 是否已生成，并按需给出「强制覆盖」/「重新生成 wiki」按钮
    if result.get("error") == "duplicate":
        existing = result.get("existing_file", "")
        old_wc = result.get("existing_word_count", 0)
        new_wc = result.get("new_word_count", 0)
        has_wiki = result.get("has_wiki", False)
        existing_wiki = result.get("existing_wiki", "")

        fields = [
            ("提示", "该内容已在知识库中"),
            ("已有文件", existing[:50]),
            ("链接", url[:60]),
        ]
        if old_wc:
            fields.append(("已有字数", f"约 {old_wc} 字"))
        if new_wc:
            fields.append(("新内容字数", f"约 {new_wc} 字"))
        # 本次正文明显更多时给一句结论：用户才判断得出「要不要覆盖」。
        # 覆盖**不会自动发生**，必须由用户点下面的按钮（2026-10-14 起）。
        if result.get("newer_more_complete"):
            fields.append(("说明", "本次抓到的内容更完整，上一版可能不完整；"
                                   "如需替换请点「强制覆盖」。"))
        if has_wiki:
            fields.append(("Wiki", f"✅ 已生成 {existing_wiki}" if existing_wiki else "✅ 已生成"))
        else:
            fields.append(("Wiki", "⚠️ 未生成（孤儿 raw）"))

        buttons = []
        if result.get("canOverwrite"):
            buttons.append({
                "text": "⚠️ 强制覆盖",
                "value": {"action": "force_overwrite", "url": url},
                "type": "danger",
            })
        if not has_wiki and existing:
            buttons.append({
                "text": "🔄 重新生成 wiki",
                "value": {"action": "reprocess", "filepath": existing},
                "type": "primary",
            })

        send_feishu_card(client, chat_id, "📌 已存在", fields,
                         color="orange", buttons=buttons or None)
        return

    # 放入浏览器协助队列
    if result.get("success") and result.get("taskId"):
        task_id = result["taskId"]
        with _task_map_lock:
            _task_chat_map[task_id] = chat_id

        send_feishu_card(client, chat_id, "⏳ 等待浏览器抓取", [
            ("任务", task_id),
            ("提示", "已加入浏览器协助队列，等待浏览器连接后自动抓取"),
            ("链接", url[:60]),
        ], color="blue")

        # 后台轮询任务状态（与 retry_task 回调共用同一个 watcher）
        t = threading.Thread(target=_watch_browser_task,
                             args=(client, chat_id, task_id, url), daemon=True)
        t.start()
        return

    # 失败：尽量展示详细原因
    _send_failure_card(client, chat_id, url, result)


def _send_failure_card(client, chat_id: str, url: str, result: dict):
    """失败卡片：展示 error / message / detail / HTTP 状态，并附排查提示。"""
    error = result.get("error", "未知错误")
    message = result.get("message", "")
    detail = result.get("detail", "")
    status = result.get("_http_status", 0)

    fields = [("错误", str(error)[:200])]
    if message:
        fields.append(("说明", str(message)[:200]))
    if detail:
        fields.append(("详情", str(detail)[:200]))
    if status:
        fields.append(("HTTP 状态", str(status)))
    fields.append(("链接", url[:60]))

    hint = _error_hint(error, status)
    if hint:
        fields.append(("排查", hint))

    send_feishu_card(client, chat_id, "❌ 剪藏失败", fields, color="red")


def _browser_assist_failure_hint(error: str) -> tuple[str, str]:
    """把浏览器协助任务的失败 error 翻译成用户能懂的提示。

    视频类页面（bilibili / YouTube 等）的字幕 / 正文必须**已登录**的浏览器
    才能取到；浏览器没打开则连窗口都没有（No current window）。两类都是
    「环境前提没满足」，不是代码缺陷 —— 过去只回一句技术报错，用户看不懂。
    """
    err = str(error)
    low = err.lower()
    # ① 服务端自己的判词：等不到浏览器接入（任务一直停在 pending，被清扫判失败）。
    #    原文「等待浏览器协助超时（超过 600 秒仍无浏览器接入）—— 请确认…」虽是中文，
    #    但说的是「现象」，用户需要的是「该做什么」。
    if "无浏览器接入" in err or "等待浏览器协助超时" in err:
        return ("❌ 浏览器协助失败",
                "超时前没有浏览器接入。请打开浏览器并确认 Vault 扩展已加载"
                "（chrome://extensions 中可见），然后点「重试」。")
    # ② 服务端判词：已经发给浏览器，但结果没有回传（任务停在 processing）。
    #    这一类的处置方式不同 —— 浏览器是开着的，要保的是那张抓取标签页。
    if "超时未响应" in err or "结果没有回传" in err:
        return ("❌ 浏览器协助失败",
                "结果没有回传。常见原因是抓取标签页被关闭或扩展被重载，"
                "请保留抓取标签页，然后点「重试」。")
    # ③ 浏览器没窗口 / 没连接 → 先强调打开浏览器
    if "no current window" in low or "window" in low or "连接" in err:
        return ("❌ 浏览器协助失败",
                "未检测到可用的浏览器窗口。请打开浏览器并确保 Vault 扩展已连接"
                "（chrome://extensions 中可见），然后点「重试」。")
    # ④ 页面没返回内容 → 多半是没登录（视频字幕需要登录态）
    if "提取失败" in err or "未返回内容" in err or "content" in low:
        return ("❌ 浏览器协助失败",
                "页面内容未能取回。视频类站点（bilibili / YouTube 等）需在"
                "**已登录**的浏览器中打开才能取到字幕 / 正文。请先登录目标站点，"
                "再点「重试」。")
    # 兜底：给出通用排查 + 重试
    return ("❌ 浏览器协助失败",
            "浏览器协助抓取未成功完成（可能是浏览器未就绪或未登录目标站点）。"
            "请确认浏览器已打开并已登录该网站，然后点「重试」。")


def _error_hint(error, status: int) -> str:
    """根据错误类型返回一句排查提示。"""
    err = str(error).lower()
    if status == 401 or "unauthorized" in err:
        return "clip-server 鉴权失败，确认 config.local.json 的 token 正确"
    # 飞书文档拒降级：后端抓取失败后不会再经浏览器重试，必须装上 lark-cli 才行。
    if "lark-cli" in err:
        return "安装并授权 lark-cli：npm install -g @larksuite/cli，再执行 lark-cli auth login"
    if "连接" in str(error) or "connection" in err:
        return "clip-server 未启动或不可达"
    if "超时" in str(error) or "timeout" in err:
        return "clip-server 处理超时，可稍后重试"
    return ""


def handle_help_command(client, chat_id: str):
    """显示帮助信息。"""
    fields = [
        ("📥 入库", "@机器人 URL（默认自动提取图片文字）"),
        ("📌 跟踪更新", "跟踪 URL — 内容变更时创建新版本"),
        ("⏭️ 跳过 OCR", "不OCR URL — 本次不提取图片文字"),
        ("📊 统计", "查看今日入库统计"),
        ("🔍 查询", "查询 <任务ID> — 查单个任务"),
        ("", "查询全部 — 列出所有活跃任务"),
        ("", "查询失败 — 列出失败任务"),
        ("🔄 重试", "重试 <任务ID> — 重新执行失败任务"),
        ("❌ 取消", "取消 <任务ID> — 取消待处理任务"),
    ]
    send_feishu_card(client, chat_id, "📖 可用指令", fields, color="blue")


def handle_stats_command(client, chat_id: str):
    """显示统计信息。"""
    import requests
    try:
        resp = requests.get(f"{CLIP_SERVER}/api/stats", timeout=5, headers=_clip_headers())
        stats = resp.json()

        fields = [
            ("今日入库", f"{stats.get('today_done', 0)} 成功 / {stats.get('today_failed', 0)} 失败 / 共 {stats.get('today_created', 0)}（成功率 {stats.get('today_success_rate', 0)}%）"),
            ("活跃任务", f"{stats.get('active', 0)} 个待处理/处理中"),
            ("知识库", f"{stats.get('wiki_topics', 0)} 篇 wiki / {stats.get('orphan_raw', 0)} 个孤儿 raw"),
        ]
        send_feishu_card(client, chat_id, "📊 知识库统计", fields, color="green")
    except Exception as e:
        send_feishu_message(client, chat_id, f"查询统计失败: {e}")


def handle_query_command(client, chat_id: str, text: str):
    """处理查询指令：查询 <id> / 查询全部 / 查询失败。"""
    arg = text.replace("查询", "").strip()
    import requests

    # 查询全部：列出活跃任务
    if arg == "全部" or arg == "all":
        try:
            resp = requests.get(f"{CLIP_SERVER}/api/tasks", timeout=5, headers=_clip_headers())
            data = resp.json()
            tasks = data.get("tasks", [])
            if not tasks:
                send_feishu_message(client, chat_id, "✅ 没有活跃任务")
                return
            fields = [(t["id"][:8], f"{t['stageLabel']} | {t['url'][:40]}") for t in tasks[:10]]
            if len(tasks) > 10:
                fields.append(("…", f"还有 {len(tasks) - 10} 个任务"))
            send_feishu_card(client, chat_id, f"📋 活跃任务 ({len(tasks)})", fields)
        except Exception as e:
            send_feishu_message(client, chat_id, f"查询失败: {e}")
        return

    # 查询失败：列出失败任务
    if arg == "失败" or arg == "failed":
        try:
            resp = requests.get(f"{CLIP_SERVER}/api/health", timeout=5, headers=_clip_headers())
            health = resp.json()
            # 用 list_tasks_by_status 需要直接调 task_queue，通过 stats API 间接获取
            # 简单做法：直接查 clip-server 的 tasks 接口过滤
            resp2 = requests.get(f"{CLIP_SERVER}/api/tasks?status=failed", timeout=5, headers=_clip_headers())
            data = resp2.json()
            tasks = data.get("tasks", [])
            if not tasks:
                send_feishu_message(client, chat_id, "✅ 没有失败任务")
                return
            fields = [(t["id"][:8], f"{t.get('error', '未知')[:40]} | {t['url'][:30]}") for t in tasks[:10]]
            send_feishu_card(client, chat_id, f"❌ 失败任务 ({len(tasks)})", fields, color="red")
        except Exception as e:
            send_feishu_message(client, chat_id, f"查询失败: {e}")
        return

    # 查询单个任务
    if not arg:
        send_feishu_message(client, chat_id, "用法：查询 <任务ID> / 查询全部 / 查询失败")
        return

    try:
        resp = requests.get(f"{CLIP_SERVER}/api/task/{arg}", timeout=5, headers=_clip_headers())
        data = resp.json()
        if "error" in data and data.get("error") != "任务不存在":
            # 可能有 error 字段表示任务本身的错误
            pass
        if resp.status_code == 404:
            send_feishu_message(client, chat_id, f"任务不存在：{arg}")
            return

        status_map = {
            "pending": "⏳ 等待处理",
            "processing": "🔄 处理中",
            "done": "✅ 完成",
            "failed": "❌ 失败",
        }
        status = status_map.get(data.get("status", ""), data.get("status", ""))
        stage = data.get("stageLabel", "")
        error = data.get("error", "")

        fields = [
            ("状态", f"{status} ({stage})" if stage else status),
            ("链接", data.get("url", "")[:60]),
        ]
        if error:
            fields.append(("错误", error[:100]))
        if data.get("maybeStuck"):
            fields.append(("⚠️", "任务可能卡住（>5分钟未更新）"))

        buttons = []
        if data.get("status") == "failed":
            buttons.append({
                "text": "🔄 重试",
                "value": {"action": "retry_task", "task_id": arg},
                "type": "primary",
            })
        if data.get("status") in ("pending", "processing"):
            buttons.append({
                "text": "❌ 取消",
                "value": {"action": "cancel_task", "task_id": arg},
                "type": "danger",
            })

        send_feishu_card(client, chat_id, f"🔍 任务 {arg[:8]}", fields,
                         buttons=buttons or None)
    except Exception as e:
        send_feishu_message(client, chat_id, f"查询失败: {e}")


def handle_retry_command(client, chat_id: str, text: str):
    """处理重试指令。"""
    task_id = text.replace("重试", "").strip()
    if not task_id:
        send_feishu_message(client, chat_id, "请提供任务 ID，例如：重试 abcd1234\n提示：发送「查询失败」可查看失败任务")
        return

    import requests
    try:
        resp = requests.post(f"{CLIP_SERVER}/api/task/{task_id}/retry", timeout=10, headers=_clip_headers())
        data = resp.json()
        if data.get("success"):
            send_feishu_card(client, chat_id, "🔄 已重新入队", [
                ("任务", task_id),
                ("链接", data.get("url", "")[:60]),
            ], color="blue")
        else:
            send_feishu_message(client, chat_id, f"❌ 重试失败: {data.get('error', '未知错误')}")
    except Exception as e:
        send_feishu_message(client, chat_id, f"重试请求失败: {e}")


def handle_cancel_command(client, chat_id: str, text: str):
    """处理取消指令。"""
    task_id = text.replace("取消", "").strip()
    if not task_id:
        send_feishu_message(client, chat_id, "请提供任务 ID，例如：取消 abcd1234\n提示：发送「查询全部」可查看活跃任务")
        return

    import requests
    try:
        resp = requests.post(f"{CLIP_SERVER}/api/task/{task_id}/cancel", timeout=10, headers=_clip_headers())
        data = resp.json()
        if data.get("success"):
            send_feishu_card(client, chat_id, "❌ 已取消", [
                ("任务", task_id),
                ("链接", data.get("url", "")[:60]),
            ], color="orange")
        else:
            send_feishu_message(client, chat_id, f"❌ 取消失败: {data.get('error', '未知错误')}")
    except Exception as e:
        send_feishu_message(client, chat_id, f"取消请求失败: {e}")


# ===== 主流程 =====

#: 单实例锁：同一时刻只允许一个 feishu-bot 持有飞书长连接。
#:
#: 为什么必须有：多个实例同时在线时，飞书会把同一条消息**轮询投递**到不同连接
#: —— 用户收到重复卡片、链接被重复抓取。而守护进程的「拉起后 3 秒探活」在网络
#: 抖动时会误判（实测 2026-10-14：实例启动后约 3 分钟才连上飞书，期间被判成
#: 「启动后立即退出」，守护进程连拉了三个实例，全部连上飞书）。
_LOCK_FILE = Path(__file__).resolve().parent / "runtime" / "feishu-bot.pid"


def _pid_alive(pid: int) -> bool:
    """进程是否存活。

    Windows 上走 OpenProcess/GetExitCodeProcess：**不 spawn 子进程**。用
    `tasklist` 判活不仅有几十毫秒开销，还可能超时 —— 而超时被 `except` 当成
    「进程已死」，正是实例堆积的诱因。
    """
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def acquire_single_instance() -> bool:
    """抢单实例锁；已有活着的实例时返回 False（调用方应直接退出）。"""
    try:
        if _LOCK_FILE.exists():
            first = _LOCK_FILE.read_text(encoding="utf-8").split()[0]
            old = int(first) if first.isdigit() else 0
            if old and old != os.getpid() and _pid_alive(old):
                print(f"[!] 已有实例在运行（pid {old}），本进程退出以避免重复长连接")
                return False
    except (OSError, ValueError, IndexError) as e:
        print(f"[!] 读取实例锁失败（{e}）—— 继续启动")
    try:
        _LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        _LOCK_FILE.write_text(f"{os.getpid()}\n", encoding="utf-8")
    except OSError as e:
        print(f"[!] 写入实例锁失败（{e}）—— 无法保证单实例")
    return True


def main():
    global CLIP_SERVER

    # 单实例闸门必须排在**连飞书之前**：重复连接的代价是消息被重复投递。
    if not acquire_single_instance():
        return 0

    parser = argparse.ArgumentParser(description="飞书知识库剪藏机器人")
    parser.add_argument("--app-id", default="", help="飞书 App ID（默认从 lark-cli 读取）")
    parser.add_argument("--app-secret", default="", help="飞书 App Secret（默认从 lark-cli 读取）")
    parser.add_argument("--clip-server", default=CLIP_SERVER, help=f"clip-server 地址（默认 {CLIP_SERVER}）")
    args = parser.parse_args()

    CLIP_SERVER = args.clip_server.rstrip("/")

    # 获取凭据
    app_id = args.app_id
    app_secret = args.app_secret
    if not app_id or not app_secret:
        app_id, app_secret = get_lark_credentials()

    if not app_id or not app_secret:
        print("❌ 无法获取飞书凭据。请通过以下方式之一提供：")
        print("   1. 配置 lark-cli（lark-cli config set）")
        print("   2. 命令行参数：--app-id cli_xxx --app-secret xxx")
        sys.exit(1)

    print("🤖 飞书知识库剪藏机器人")
    print(f"   App ID: {app_id[:10]}...")
    print(f"   clip-server: {CLIP_SERVER}")
    print(f"   连接模式: WebSocket 长连接")
    print()

    # 导入 SDK
    import lark_oapi as lark
    from lark_oapi.ws import Client as WsClient

    # 创建事件处理器
    lark_client = lark.Client.builder() \
        .app_id(app_id) \
        .app_secret(app_secret) \
        .log_level(lark.LogLevel.INFO) \
        .build()

    # 设置全局引用（供卡片回调使用）
    global _lark_client
    _lark_client = lark_client

    dispatcher = make_event_handler(lark_client)

    # 创建 WebSocket 客户端并启动
    ws_client = WsClient(
        app_id=app_id,
        app_secret=app_secret,
        event_handler=dispatcher,
        log_level=lark.LogLevel.INFO,
        auto_reconnect=True,
    )

    print("[*] 正在连接飞书长连接...")
    ws_client.start()


if __name__ == "__main__":
    main()

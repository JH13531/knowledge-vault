#!/usr/bin/env python3
"""知识库后台服务启动器（开机自启 / 手动启动 / 守护）。

管两件事：
  1. `tools/clip-server.py` —— 剪藏服务，监听 127.0.0.1:7878。
     浏览器扩展、飞书机器人、任务面板全靠它。**必须常驻**。
  2. `tools/feishu-bot.py`  —— 飞书机器人（手机发链接入库）。**可选**：
     只有检测到凭据时才启动，否则跳过并说明原因。

用法：
    python tools/autostart/vault-autostart.py            # 启动并守护（默认）
    python tools/autostart/vault-autostart.py --once     # 只启动，不驻留
    python tools/autostart/vault-autostart.py --status   # 看状态
    python tools/autostart/vault-autostart.py --stop     # 停止守护 + 服务

## 为什么默认是「守护」而不是「启动完就退」

这不是偏好，是被 Windows 的进程回收机制逼出来的：

- 计划任务（Task Scheduler）会把一次任务放进 **job object**，任务一「结束」
  就连带杀掉里面所有子进程。如果启动器启动完就退出，它起的服务会被一起收走 ——
  表现为「开机自启看着跑了，但 7878 永远没人监听」。
- 同理，从某些终端/IDE 启动时，父进程退出会连带回收整棵进程树。

所以默认让启动器自己**驻留**：任务一直是「运行中」，job 不结束，子进程自然活着；
顺带还能在服务意外挂掉时自动拉起。子进程另外尝试 `CREATE_BREAKAWAY_FROM_JOB`
做第二道保险。

## 其它设计要点（都是本项目踩过的坑）

- **幂等**：先探端口/进程再启动。重复执行（自启 + 手动又点一次）不会起出
  第二个实例 —— 第二个会因端口占用直接崩，日志里丢一堆看不懂的 traceback。
- **解释器用 sys.executable**：本机 anaconda 的 python 和别的 venv 并存，
  「pip 装到哪个」和「脚本用哪个跑」必须一致，否则一启动就是 ModuleNotFoundError。
- **强制子进程 UTF-8**：本机 PowerShell 是 cp936，不设的话中文日志会乱码，
  甚至直接抛 UnicodeEncodeError 把服务打死。
- **日志落盘**：后台跑没人盯控制台，stdout/stderr 统一进 `tools/runtime/logs/`。
- **回环地址绕开代理**：本机配了 HTTP_PROXY，连自己的 7878 也会被劫持成 502。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"
RUNTIME = TOOLS / "runtime"
LOGS = RUNTIME / "logs"
PIDFILE = RUNTIME / "autostart.pids"
LOCKFILE = RUNTIME / "autostart.lock"

CLIP_SERVER = TOOLS / "clip-server.py"
FEISHU_BOT = TOOLS / "feishu-bot.py"
FEISHU_CFG = TOOLS / "feishu-bot-config.json"
LARK_CFG = Path.home() / ".lark-cli" / "config.json"

HOST, PORT = "127.0.0.1", 7878
CHECK_INTERVAL = 5.0      # 守护模式下检查子进程的间隔（秒）


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------

def log(msg: str) -> None:
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        with (LOGS / "autostart.log").open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def port_in_use(host: str = HOST, port: int = PORT) -> bool:
    with socket.socket() as s:
        s.settimeout(1.5)
        return s.connect_ex((host, port)) == 0


def pid_alive(pid) -> bool:
    """判断进程是否存活。

    **不能用 `os.kill(pid, 0)`** —— Windows 上 os.kill 只认 CTRL_C/CTRL_BREAK，
    其它信号值会直接把进程 TerminateProcess 掉，拿它做探活等于随机杀服务。

    Windows 上直接调 OpenProcess/GetExitCodeProcess：**不 spawn 子进程**。
    原先这里跑 `tasklist`，一旦它超时就被 `except` 归成「进程已死」—— 实测
    2026-10-14 因此把「启动慢」误判成「启动即崩」（feishu-bot 要几分钟才连上
    飞书），同一条被连拉三个实例、全部连上飞书，消息于是被重复投递。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        # os.kill(pid, 0) 对**僵尸进程**同样返回成功（pid 被父进程回收前仍在
        # 进程表里）。守护进程自己 spawn 的服务死后先是它的僵尸子进程，仅靠
        # 信号探活会把「已死」看成「活着」，于是永远不会自动拉起 ——
        # 2026-10-15 Linux 实测：kill 掉 feishu-bot 后守护一直显示「运行中」。
        # /proc/<pid>/stat 的 state 字段为 'Z' 即僵尸。
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            # comm 可能含空格与右括号，state 取最后一个 ')' 之后的第一个字段
            return stat.rsplit(")", 1)[1].split()[0] != "Z"
        except (OSError, IndexError):
            # /proc 不可用（如 macOS）或读取失败 → 退化为信号探活的结果
            return True
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


def feishu_bot_alive() -> str:
    """飞书机器人是否已有实例在跑 → 返回其 pid 字符串（否则空串）。

    读的是 feishu-bot 自己的**实例锁**（`runtime/feishu-bot.pid`），比仅凭
    监督者记的 pid 更可靠：锁由服务自己维护，进程只要活着就有效。没有这道
    检查时，一旦「拉起后探活」误判，主循环会每 5 秒再拉一个，直到堆满。
    """
    try:
        first = (RUNTIME / "feishu-bot.pid").read_text(encoding="utf-8").split()[0]
    except (OSError, IndexError):
        return ""
    return first if first.isdigit() and pid_alive(int(first)) else ""


def load_pids() -> dict:
    try:
        data = json.loads(PIDFILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_pids(pids: dict) -> None:
    try:
        RUNTIME.mkdir(parents=True, exist_ok=True)
        PIDFILE.write_text(json.dumps(pids, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    except Exception as e:
        log("!! 写入 PID 文件失败：%s" % e)


def child_env() -> dict:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("PYTHONUNBUFFERED", "1")
    existing = env.get("NO_PROXY") or env.get("no_proxy") or ""
    merged = ",".join(p for p in (existing, "127.0.0.1,localhost") if p)
    env["NO_PROXY"] = merged
    env["no_proxy"] = merged
    return env


# Windows 上 spawn 任何控制台程序，只要不带 CREATE_NO_WINDOW，
# 就会**弹一个黑框**。守护循环每 5 秒探一次进程，于是屏幕上就是
# 「几秒钟闪一次控制台」—— 用户会以为中招了。
# 注意：capture_output=True 只管管道，**挡不住窗口**，必须给 creationflags。
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def run_hidden(cmd: list[str], timeout: float = 25) -> subprocess.CompletedProcess:
    """跑辅助命令（tasklist / netstat / taskkill），**保证不弹窗**。"""
    kwargs = dict(capture_output=True, timeout=timeout,
                  encoding="utf-8", errors="replace")
    if _NO_WINDOW:
        kwargs["creationflags"] = _NO_WINDOW
    return subprocess.run(cmd, **kwargs)


def spawn(name: str, script: Path) -> int:
    """后台拉起一个脚本，输出重定向到 logs/<name>.log。返回 pid。"""
    LOGS.mkdir(parents=True, exist_ok=True)
    logfile = (LOGS / (name + ".log")).open("ab")
    logfile.write(("\n===== %s 启动 =====\n"
                   % datetime.now().strftime("%Y-%m-%d %H:%M:%S")).encode("utf-8"))
    logfile.flush()

    cmd = [sys.executable, "-u", str(script)]
    base = dict(cwd=str(ROOT), stdout=logfile, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, env=child_env())

    if os.name == "nt":
        breakaway = 0x01000000  # CREATE_BREAKAWAY_FROM_JOB
        try:
            # 第一道保险：逃出父进程所在的 job object。
            # 若该 job 不允许 breakaway，CreateProcess 会直接失败 —— 那就退回普通方式。
            p = subprocess.Popen(cmd, creationflags=_NO_WINDOW | breakaway, **base)
        except OSError:
            p = subprocess.Popen(cmd, creationflags=_NO_WINDOW, **base)
    else:
        p = subprocess.Popen(cmd, start_new_session=True, **base)

    log("   解释器: %s" % sys.executable)
    log("   命令  : %s" % " ".join(cmd))
    return p.pid


# --------------------------------------------------------------------------
# 各项服务（幂等）
# --------------------------------------------------------------------------

def ensure_clip_server(pids: dict, announce: bool = True) -> None:
    if port_in_use():
        if announce:
            log("· clip-server 已在监听 %s:%d —— 跳过" % (HOST, PORT))
        return

    log("· 启动 clip-server …")
    pid = spawn("clip-server", CLIP_SERVER)

    # 后台启动最怕「看着成功、其实崩了」—— 必须探到端口才算数
    for _ in range(20):
        time.sleep(0.5)
        if port_in_use():
            pids["clip-server"] = pid
            save_pids(pids)
            log("✔ clip-server 已就绪：http://%s:%d（pid %d）" % (HOST, PORT, pid))
            return
        if not pid_alive(pid):
            log("✘ clip-server 启动后立即退出（pid %d）—— 见 %s"
                % (pid, LOGS / "clip-server.log"))
            return
    log("△ clip-server 已拉起（pid %d）但 10 秒内端口未就绪 —— 见 %s"
        % (pid, LOGS / "clip-server.log"))


def feishu_credentials() -> tuple[bool, str]:
    if FEISHU_CFG.is_file():
        try:
            cfg = json.loads(FEISHU_CFG.read_text(encoding="utf-8"))
            if cfg.get("app_id") and cfg.get("app_secret"):
                return True, "tools/feishu-bot-config.json"
            return False, "tools/feishu-bot-config.json 里 app_id/app_secret 为空"
        except Exception as e:
            return False, "凭据文件解析失败：%s" % e
    if LARK_CFG.is_file():
        return True, "~/.lark-cli/config.json"
    return False, "未找到飞书凭据"


def ensure_feishu_bot(pids: dict, announce: bool = True) -> None:
    ok, why = feishu_credentials()
    if not ok:
        if announce:
            log("· 跳过 feishu-bot：%s（启用方式见 tools/docs/feishu-bot-setup.md）" % why)
        return

    # 幂等：优先看**服务自己的实例锁**，其次才是监督者记的 pid。
    # 只信 pids 的话，一旦某轮没写成功就会重复拉起（见下方 spawn 处注释）。
    running = feishu_bot_alive() or (
        str(pids.get("feishu-bot")) if pid_alive(pids.get("feishu-bot")) else "")
    if running:
        if announce:
            log("· feishu-bot 已在运行（pid %s）—— 跳过" % running)
        pids["feishu-bot"] = int(running)
        return

    log("· 启动 feishu-bot（凭据：%s）…" % why)
    pid = spawn("feishu-bot", FEISHU_BOT)
    # spawn 后**总是**先记录 pid，再探活。
    # 为什么不能「探活失败就不记录」：feishu-bot 要几十秒到几分钟才连上飞书
    # （实测 2026-10-14 一次等了 174 秒），期间进程虽然活着、却可能被判成
    # 「启动后立即退出」；一旦不记录，主循环下一轮又拉一个新的 —— 于是同时
    # 出现多个实例持有飞书长连接，同一条消息被轮询投递到不同连接，用户收到
    # 重复卡片、链接被重复抓取。
    # 现在即使真崩了也不会堆积：下一轮 ensure 会先看实例锁，锁里的 pid 已死 →
    # 判空 → 才会重新拉起。
    pids["feishu-bot"] = pid
    save_pids(pids)
    time.sleep(1)
    if pid_alive(pid):
        log("✔ feishu-bot 已拉起（pid %d）" % pid)
    else:
        log("⚠ feishu-bot 进程已退出（pid %d）—— 见 %s"
            % (pid, LOGS / "feishu-bot.log"))


def ensure_all(pids: dict, announce: bool = True) -> None:
    ensure_clip_server(pids, announce)
    ensure_feishu_bot(pids, announce)


# --------------------------------------------------------------------------
# 子命令
# --------------------------------------------------------------------------

def other_supervisor_running(pids: dict) -> bool:
    sup = pids.get("supervisor")
    return bool(sup) and pid_alive(sup) and int(sup) != os.getpid()


def cmd_start(once: bool) -> int:
    pids = load_pids()

    if other_supervisor_running(pids):
        log("· 已有守护进程在运行（pid %s）—— 本次只做一次检查，不重复驻留"
            % pids["supervisor"])
        ensure_all(pids)
        return 0

    log("=== 知识库后台服务启动（%s）===" % ROOT)
    pids["supervisor"] = os.getpid()
    save_pids(pids)
    ensure_all(pids)

    if once:
        pids.pop("supervisor", None)
        save_pids(pids)
        log("=== 完成（--once：不驻留）。日志目录：%s ===" % LOGS)
        return 0

    log("=== 进入守护模式：每 %ds 检查一次，服务挂掉会自动拉起 ===" % int(CHECK_INTERVAL))
    log("    停止：Ctrl+C，或 `--stop`")
    try:
        while True:
            time.sleep(CHECK_INTERVAL)
            ensure_all(pids, announce=False)
    except KeyboardInterrupt:
        log("收到中断 —— 退出守护，已启动的服务保持运行（用 --stop 才会停）")
    return 0


def cmd_status() -> int:
    pids = load_pids()
    print("监听 %s:%d        : %s" % (HOST, PORT, "是" if port_in_use() else "否"))
    for name in ("supervisor", "clip-server", "feishu-bot"):
        pid = pids.get(name)
        print("  %-12s pid=%-8s %s"
              % (name, pid if pid else "-",
                 "运行中" if (pid and pid_alive(pid)) else "未运行"))
    print("日志目录            : %s" % LOGS)
    ok, why = feishu_credentials()
    print("飞书凭据            : %s" % (why if not ok else "可用（%s）" % why))
    return 0


def port_owner_pid(port: int = PORT) -> str | None:
    """查出监听该端口的进程 PID（用 netstat，不依赖 psutil）。"""
    try:
        out = run_hidden(["netstat", "-ano"]).stdout or ""
    except Exception:
        return None
    suffix = ":%d" % port
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(suffix) and "LISTENING" in line.upper():
            return parts[-1]
    return None


def cmd_stop() -> int:
    pids = load_pids()
    stopped, failed = [], []

    # 顺序很重要：**先停被守护的服务，再停守护本身**。
    # 反过来做的话，杀掉 supervisor 之后可能连带影响到子进程，
    # 于是后续对子进程的 taskkill 全部落空，报告里就会漏报
    # 「服务其实已经停了」—— 用户会以为它们还在跑。
    for name in ("feishu-bot", "clip-server", "supervisor"):
        pid = pids.get(name)
        if not pid or not pid_alive(pid):
            continue
        r = run_hidden(["taskkill", "/PID", str(pid), "/F"])
        (stopped if r.returncode == 0 else failed).append("%s(pid %s)" % (name, pid))

    # 收尾核实：端口若还被占着，说明有本脚本之外的实例（手动起的、
    # 或上一轮遗留的），必须报出来并清掉 —— 否则用户重启后又会撞端口占用。
    time.sleep(1)
    if port_in_use():
        owner = port_owner_pid()
        if owner and owner not in ("0", ""):
            r = run_hidden(["taskkill", "/PID", owner, "/F"])
            if r.returncode == 0:
                stopped.append("端口 %d 的占用者(pid %s)" % (PORT, owner))
            else:
                failed.append("端口 %d 的占用者(pid %s)" % (PORT, owner))

    try:
        PIDFILE.unlink()
    except Exception:
        pass

    print("已停止：" + ("、".join(stopped) if stopped else "（没有由本脚本启动的进程）"))
    if failed:
        print("停止失败：" + "、".join(failed))
    print("端口 %d：%s" % (PORT, "仍被占用 ✘" if port_in_use() else "已释放 ✔"))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="知识库后台服务启动器")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--status", action="store_true", help="查看服务状态")
    g.add_argument("--stop", action="store_true", help="停止守护与它启动的服务")
    g.add_argument("--once", action="store_true",
                   help="只启动、不驻留（供一次性调用；计划任务不要用）")
    args = ap.parse_args()

    if args.status:
        return cmd_status()
    if args.stop:
        return cmd_stop()
    return cmd_start(once=args.once)


if __name__ == "__main__":
    sys.exit(main())

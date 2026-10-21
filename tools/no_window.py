"""Windows 上「启动子进程但不弹出控制台窗口」的统一开关。

为什么单独一个模块：
    Windows 下只要 spawn 一个**控制台程序**（yt-dlp.exe / claude.cmd / codex.cmd …），
    系统就会为它**新建一个控制台**并显示出来。表现就是：
      - 用户点一次待审区「▶ 处理」→ 跳出一个 cmd 窗口
      - 后台守护每隔几秒跑一次 tasklist → 黑框一闪一闪
      - 走 wiki 精加工时 claude/codex 的 .cmd 被 cmd.exe /c 拉起 → 又是一个窗口

    `capture_output=True` / `stdout=PIPE` 只管**管道**，
    **完全挡不住窗口** —— 必须显式传 `creationflags=subprocess.CREATE_NO_WINDOW`。

    这个标志只在 Windows 有意义，其余平台传 0（subprocess 接受 0，等价于不设）。
    集中在这里是为了避免每个调用点各写一遍、日后改的时候漏掉某一处
    （本项目已经因为漏掉一处，让守护脚本每 5 秒闪一个黑框）。
"""
from __future__ import annotations

import os
import subprocess

#: 直接传给 subprocess.run / Popen 的 creationflags。非 Windows 平台为 0。
NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

# 开机自启（Windows）

让 `clip-server` 和 `feishu-bot` 在登录后自动常驻，不用每次开机手动开终端。

## 两个按钮（日常只用这两个）

| 双击这个 | 效果 | 下次登录 |
|---|---|---|
| `enable-autostart.bat` | 注册登录自启 **并立即启动服务** | 自动运行 |
| `disable-autostart.bat` | 停止服务 **并移除登录自启** | 不再自动运行 |

双击后会弹「是否允许此应用对你的设备进行更改」，点「是」即可。整个过程不用打开命令行。

> 前提：本机已安装 Python。脚本会按 `VAULT_PYTHON` → `tools\.venv` → `C:\software\anaconda3` → 系统 PATH 的顺序找。
> 找不到时 `enable-autostart.bat` 会直接报错退出，不会留下一个"看着装好、其实没跑"的任务。

## 它到底做了什么

两个按钮背后是同一个脚本 `manage-autostart.ps1`：

- `-Action install`：注册计划任务 `KnowledgeVault-Autostart`（登录触发，运行 `pythonw.exe vault-autostart.py`），随后立即启动它。
- `-Action uninstall`：先停掉服务（`vault-autostart.py --stop`），再移除计划任务。

真正的服务逻辑在 `vault-autostart.py`：

- 启动 `tools/clip-server.py`（监听 `127.0.0.1:7878`）
- 检测到飞书凭据时启动 `tools/feishu-bot.py`
- 守护模式：每 5 秒检查一次，服务挂了自动拉起

## 为什么是「登录触发」而不是「开机」

计划任务用的是 `AtLogOn`（当前用户登录时触发）。体感就是：开机 → 登录 → 服务自动跑起来。
开机但没登录时不会跑；别的账户登录也不会跑。

## 文件说明

| 文件 | 用途 |
|---|---|
| `vault-autostart.py` | 核心：启动 / 守护 / `--stop` / `--status` |
| `manage-autostart.ps1` | 注册与移除计划任务的实现（内部用，不要手点） |
| `enable-autostart.bat` | 双击入口：注册 + 启动 |
| `disable-autostart.bat` | 双击入口：移除 + 停止 |

## 高级用法（开发者）

```bash
python tools/autostart/vault-autostart.py --status   # 看状态
python tools/autostart/vault-autostart.py            # 启动 + 守护
python tools/autostart/vault-autostart.py --stop     # 停服务
python tools/autostart/vault-autostart.py --once     # 只启动、不驻留
```

日志在 `tools/runtime/logs/`：

- `autostart.log`：启动器的动作记录（最常看）
- `clip-server.log`
- `feishu-bot.log`

## 设计要点（都是踩过的坑）

- **为什么用 `pythonw.exe` 常驻**：Task Scheduler 会把任务放进 job object，主进程一退出就整棵进程树被收走。用「启动即退出」的包装器（wscript/bat）会出现「自启跑了但 7878 没人监听」。让 `pythonw` 常驻，job 不结束，服务才活着。
- **幂等**：启动前先探端口/进程，重复执行不会起第二个实例。
- **强制 UTF-8**：子进程设 `PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8`，否则中文日志乱码甚至打死服务。
- **NO_PROXY**：回环地址绕开本机 `HTTP_PROXY`，否则连自己的 7878 会被代理劫持成 502。

## Linux / macOS

本目录是 Windows 专用。Linux 用 systemd `--user` 单元，macOS 用 launchd；`vault-autostart.py` 本身跨平台。

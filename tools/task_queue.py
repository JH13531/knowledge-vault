"""
任务队列：管理需要浏览器协助抓取的任务。
SQLite 持久化实现：服务重启不丢任务，自动恢复中断中的任务。
"""
from __future__ import annotations
import json
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# DB 位置：tools/runtime/clip-tasks.db
_DB_PATH = Path(__file__).resolve().parent / "runtime" / "clip-tasks.db"
_DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# 清理策略（秒）
_DONE_RETENTION = 7 * 86400    # 完成任务保留 7 天
_FAILED_RETENTION = 30 * 86400  # 失败任务保留 30 天（调试用）

# 阶段中文标签
_STAGE_LABELS = {
    "": "等待处理",
    # "waiting" 只由 requeue_for_browser() 设置 —— 即「后端抓不动、已排给浏览器」。
    # 标签写清楚，避免用户看到「等待处理」以为是服务端在忙。
    "waiting": "等待浏览器协助",
    "dispatched": "已发送给浏览器",
    "browser_loading": "浏览器正在加载",
    "browser_extracting": "正在提取内容",
    "extracting": "正在提取内容",
    "server-fetch": "后端抓取中",
    "saving": "正在写入知识库",
    "done": "已完成",
    "failed": "已失败",
}


def _stage_label(stage: str) -> str:
    """获取阶段的中文标签。"""
    return _STAGE_LABELS.get(stage, stage or "等待处理")


# 卡死阈值：超过这个秒数仍未更新就判定为卡死并清理。
#   processing —— 浏览器协助依赖浏览器回传结果。扩展被重载 / 浏览器崩溃 /
#                 抓取标签页被关，结果就永远回不来；
#   pending    —— 浏览器没连接时任务会一直排队等。
# 两者都会永久占用 find_active_task 的去重判断，导致同一个 URL 再也提交不了。
#
# processing 阈值必须**宽于一次正常抓取的耗时**：浏览器侧要等标签页加载
# （最长 15 秒）+ 懒加载滚动（约 3 秒）+ 页面内提取，且回传时带 `wikiSync=true`
# —— 服务端要生成完 wiki 才回响应，API 直出亦需数十秒。整条链 1～2 分钟属正常。
# 定 300 秒会把「正在生成 wiki」的任务误判为卡死（用户看到「今日失败 +1」，
# 其实内容马上就入库了）。故取 600 秒，并在中途靠 redispatch（240 秒）兜一次。
STALE_PROCESSING_SECONDS = 600
STALE_PENDING_SECONDS = 600


def _extract_wiki_refs(content: str) -> list[str]:
    """从 raw 的 frontmatter 提取 wiki_refs 列表（支持单行内联和多行块列表）。

    只解析 frontmatter（开头 --- 之间），避免误匹配正文里的示例片段。
    与 clip-server.py 中的同名函数保持一致的解析逻辑。
    """
    m = re.search(r'^---\s*\n(.*?)\n---', content, re.S | re.M)
    if not m:
        return []
    fm = m.group(1)
    # 只匹配冒号后同一行的内容（[ \t]* 不跨行），块列表则行尾为空
    wm = re.search(r'^wiki_refs:[ \t]*([^\n]*)$', fm, re.M)
    if not wm:
        return []
    inline = wm.group(1).strip()
    refs: list[str] = []
    if inline.startswith('['):
        # 单行内联数组：["a", "b"] 或 []
        inner = inline.strip().strip('[]')
        refs = [r.strip().strip('"\'').strip() for r in inner.split(',') if r.strip()]
    elif inline == '':
        # 多行块列表：
        #   wiki_refs:
        #     - path1
        #     - path2
        for line in fm[wm.end():].splitlines():
            line = line.strip()
            if line.startswith('- '):
                refs.append(line[2:].strip().strip('"\'').strip())
            elif line and not line.startswith('-'):
                break
    elif inline:
        # 单值不带括号（兼容）
        refs = [inline.strip('"\'').strip()]
    return [r for r in refs if r]

# SQLite 连接（WAL 模式，check_same_thread=False 由 lock 保护）
_db_conn: Optional[sqlite3.Connection] = None
_db_lock = threading.Lock()


def _get_db() -> sqlite3.Connection:
    """获取数据库连接（单例）。"""
    global _db_conn
    if _db_conn is None:
        _db_conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
        _db_conn.execute("PRAGMA journal_mode=WAL")
        _db_conn.execute("PRAGMA synchronous=NORMAL")
        _init_db(_db_conn)
        _recover_interrupted(_db_conn)
        _cleanup_old_tasks(_db_conn)
    return _db_conn


def _init_db(conn: sqlite3.Connection):
    """初始化数据库表。"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            stage TEXT DEFAULT '',
            result TEXT DEFAULT '{}',
            error TEXT DEFAULT '',
            created_at REAL NOT NULL,
            completed_at REAL DEFAULT 0,
            updated_at REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)
    """)
    # 迁移：旧表可能没有 stage 列
    try:
        conn.execute("ALTER TABLE tasks ADD COLUMN stage TEXT DEFAULT ''")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # 列已存在
    # 迁移：via 列记录「这条任务从哪来的」（飞书 / 插件 / 浏览器协助 / 直接推送）。
    # 用途：任务面板要展示全部渠道，不再只显示浏览器协助队列 —— 否则
    # 「立即完整入库」（走 /api/clip 直接推送）的结果在面板里凭空消失。
    try:
        conn.execute("ALTER TABLE tasks ADD COLUMN via TEXT DEFAULT ''")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # 列已存在


def _recover_interrupted(conn: sqlite3.Connection):
    """启动恢复：把中断的 processing 任务重置为 pending。"""
    # direct-clip 占位（/api/clip 直接推送）无法重试：浏览器提取好的正文随请求
    # 一起丢了，转 pending 也没有 worker 能重放。直接判失败，请用户重新剪藏。
    cursor0 = conn.execute("""
        UPDATE tasks SET status='failed', stage='done',
               error='服务重启导致本次剪藏中断，请重新剪藏',
               updated_at=?, completed_at=?
        WHERE status='processing' AND stage='direct-clip'
    """, (time.time(), time.time()))
    if cursor0.rowcount > 0:
        print(f"[*] 恢复 {cursor0.rowcount} 个中断的直接推送任务（direct-clip → failed）")
        conn.commit()
    cursor = conn.execute("""
        UPDATE tasks SET status='pending', updated_at=?
        WHERE status='processing'
    """, (time.time(),))
    if cursor.rowcount > 0:
        print(f"[*] 恢复 {cursor.rowcount} 个中断任务（processing → pending）")
    # 同 _cleanup_old_tasks：DML 即使 0 行也会隐式开事务，必须无条件提交
    conn.commit()


def _cleanup_old_tasks(conn: sqlite3.Connection):
    """清理过期任务，防止数据库无限增长。

    策略：
    - done 任务：超过 7 天删除
    - failed 任务：超过 30 天删除（保留更久便于调试）
    - pending/processing：不删除（活跃任务）
    """
    now = time.time()
    done_cutoff = now - _DONE_RETENTION
    failed_cutoff = now - _FAILED_RETENTION

    # 删除过期 done 任务
    cursor1 = conn.execute("""
        DELETE FROM tasks WHERE status='done' AND completed_at > 0 AND completed_at < ?
    """, (done_cutoff,))

    # 删除过期 failed 任务
    cursor2 = conn.execute("""
        DELETE FROM tasks WHERE status='failed' AND completed_at > 0 AND completed_at < ?
    """, (failed_cutoff,))

    deleted = cursor1.rowcount + cursor2.rowcount
    if deleted > 0:
        print(f"[*] 清理 {cursor1.rowcount} 个过期完成任务，{cursor2.rowcount} 个过期失败任务")
    # 即使删除 0 行也必须 commit：sqlite3 在 DML 前就隐式开启了写事务，
    # 不提交会让这条单例连接一直持有写锁。2026-10-16 实测：重启后若无剪藏，
    # 外部进程（含手工清理 SQL）对该库的全部写入都会 "database is locked"。
    conn.commit()


@dataclass
class Task:
    id: str
    url: str
    status: str = "pending"   # pending | processing | done | failed
    stage: str = ""            # 处理阶段细化描述
    result: dict = field(default_factory=dict)
    error: str = ""
    created_at: float = field(default_factory=time.time)
    completed_at: float = 0


class TaskQueue:
    def __init__(self):
        self._pending: list[str] = []  # 内存中的待处理队列（加速）
        self._lock = _db_lock
        self._event = threading.Event()
        self._load_pending()

    def _load_pending(self):
        """从数据库加载 pending 任务到内存。"""
        with self._lock:
            conn = _get_db()
            cursor = conn.execute("""
                SELECT id FROM tasks WHERE status='pending'
                ORDER BY created_at ASC
            """)
            self._pending = [row[0] for row in cursor.fetchall()]
            if self._pending:
                print(f"[*] 加载 {len(self._pending)} 个待处理任务")

    def add_task(self, url: str, via: str = "") -> str:
        """添加新任务，返回任务 ID。

        via：这条任务从哪个渠道进来（飞书 / 浏览器协助 / …）。仅用于面板分类
        展示，不参与任何调度逻辑，所以允许为空（旧调用点不受影响）。
        """
        task_id = str(uuid.uuid4())[:8]
        now = time.time()
        with self._lock:
            conn = _get_db()
            conn.execute("""
                INSERT INTO tasks (id, url, status, created_at, updated_at, via)
                VALUES (?, ?, 'pending', ?, ?, ?)
            """, (task_id, url, now, now, via))
            conn.commit()
            self._pending.append(task_id)
        self._event.set()
        return task_id

    def start_server_task(self, url: str, stage: str = "server-fetch",
                          via: str = "") -> str:
        """后端直抓前置占位：直接登记为 processing（浏览器 worker 不会领取）。

        作用：抓取进行中即可在任务面板展示，也能被并发请求查重命中。
        """
        task_id = str(uuid.uuid4())[:8]
        now = time.time()
        with self._lock:
            conn = _get_db()
            conn.execute("""
                INSERT INTO tasks (id, url, status, stage, created_at, updated_at, via)
                VALUES (?, ?, 'processing', ?, ?, ?, ?)
            """, (task_id, url, stage, now, now, via))
            conn.commit()
        return task_id

    def start_direct_clip(self, url: str, via: str = "浏览器插件") -> str:
        """直接推送（/api/clip）前置占位：请求一进来就登记 processing。

        为什么需要：与 start_server_task 同理。「立即完整入库」在同一个请求里
        同步完成抓取 + wiki（wikiSync=true 时可达数分钟），此前这段时间任务面板
        的「进行中」始终是 0 —— 用户直觉是「正在剪藏就该算进行中」。
        浏览器 worker 只领取 pending，不会误领本占位；find_active_task 也显式
        排除 stage='direct-clip'（队列级去重对直接推送不适用，它有内容去重闸门）。
        """
        task_id = str(uuid.uuid4())[:8]
        now = time.time()
        with self._lock:
            conn = _get_db()
            conn.execute("""
                INSERT INTO tasks (id, url, status, stage, created_at, updated_at, via)
                VALUES (?, ?, 'processing', 'direct-clip', ?, ?, ?)
            """, (task_id, url, now, now, via))
            conn.commit()
        return task_id

    def record_direct_clip(self, url: str, via: str = "direct",
                           result: Optional[dict] = None,
                           status: str = "done", error: str = "",
                           task_id: Optional[str] = None) -> str:
        """登记一次「直接推送」剪藏（走 /api/clip，不经浏览器协助队列）。

        为什么需要：面板的「立即完整入库」走 /api/clip 直接推送正文，过去
        **完全不写任务记录** → 任务面板（待审区/进行中/今日完成）统计不到它，
        用户以为「任务凭空消失」。这里补一条已完成的记录，让面板能如实反映
        所有渠道的剪藏历史。

        与 add_task/start_server_task 的区别：那两者登记的是**待执行**任务；
        本方法登记的是**已有结论**的一次操作（默认 done），只用于展示历史。
        """
        now = time.time()
        payload = json.dumps(result or {}, ensure_ascii=False)
        with self._lock:
            conn = _get_db()
            if task_id:
                # 落定 start_direct_clip 写入的 processing 占位
                cursor = conn.execute("""
                    UPDATE tasks SET status=?, stage='done', result=?, error=?,
                           completed_at=?, updated_at=?, via=?
                    WHERE id=? AND status='processing'
                """, (status, payload, error, now, now, via, task_id))
                if cursor.rowcount > 0:
                    conn.commit()
                    return task_id
                # 占位不存在（理论上不应发生）→ 落到下面补插一条，保证历史不丢
            task_id = str(uuid.uuid4())[:8]
            conn.execute("""
                INSERT INTO tasks (id, url, status, stage, result, error,
                                   created_at, completed_at, updated_at, via)
                VALUES (?, ?, ?, 'done', ?, ?, ?, ?, ?, ?)
            """, (task_id, url, status, payload, error, now, now, now, via))
            conn.commit()
        return task_id

    def requeue_for_browser(self, task_id: str):
        """后端直抓失败：processing 占位转 pending，交给浏览器 worker。"""
        with self._lock:
            conn = _get_db()
            conn.execute("""
                UPDATE tasks SET status='pending', stage='waiting', updated_at=?
                WHERE id=?
            """, (time.time(), task_id))
            conn.commit()
            self._pending.append(task_id)
        self._event.set()

    def sweep_stale_tasks(self, proc_age: float = STALE_PROCESSING_SECONDS,
                          pend_age: float = STALE_PENDING_SECONDS) -> list[str]:
        """把卡死的任务置为 failed，返回被清理的 task id 列表。

        为什么必须有这个：浏览器协助路径依赖浏览器回传结果。一旦扩展被重载、
        浏览器崩溃或抓取标签页被关，结果永远不会回来，任务就永久停在
        processing；同理浏览器未连接时任务会一直停在 pending。而
        find_active_task 把这两种状态都当作「进行中」，导致同一个 URL
        再也提交不了（去重锁死，用户被永久拦在门外）。
        """
        now = time.time()
        swept: list[str] = []
        with self._lock:
            conn = _get_db()
            for status, max_age, reason in (
                ("processing", proc_age,
                 f"浏览器协助超时未响应（超过 {int(proc_age)} 秒）"
                 "—— 结果没有回传，可能是抓取标签页被关或扩展被重载"),
                ("pending", pend_age,
                 f"等待浏览器协助超时（超过 {int(pend_age)} 秒仍无浏览器接入）"
                 "—— 请确认浏览器已打开且扩展已加载；"
                 "若该站点在本机无法访问，浏览器侧同样抓不到"),
            ):
                cutoff = now - max_age
                rows = conn.execute(
                    "SELECT id FROM tasks WHERE status=? "
                    "AND updated_at > 0 AND updated_at < ?",
                    (status, cutoff),
                ).fetchall()
                if rows:
                    conn.execute(
                        "UPDATE tasks SET status='failed', error=?, "
                        "updated_at=?, completed_at=? "
                        "WHERE status=? AND updated_at > 0 AND updated_at < ?",
                        (reason, now, now, status, cutoff),
                    )
                    swept.extend(r[0] for r in rows)
            if swept:
                conn.commit()
            # 同步内存队列：被清扫掉的任务**必须**移出 _pending，
            # 否则浏览器稍后接入时还会把它弹出来重抓一遍，
            # 出现「DB 说已失败、浏览器却真去抓了」的矛盾状态。
            for tid in swept:
                if tid in self._pending:
                    self._pending.remove(tid)
        if swept:
            print(f"[*] 清理 {len(swept)} 个卡死任务（超时未响应）：{', '.join(swept)}")
        return swept

    def start_sweeper(self, interval: float = 60.0) -> None:
        """后台定时清扫卡死任务。

        为什么必须是**定时**而不是惰性：清扫原先只在 find_active_task() 里调用，
        也就是「同一个 URL 再次提交时」才发生。于是等待浏览器接入的任务会
        永远停在 pending —— 任务面板显示「进行中」，用户看到的却是「等了很久
        没反应」，而服务端什么都不做。这和「检测到异常却不处置」是同一个病。
        """
        def _loop():
            while True:
                try:
                    time.sleep(interval)
                    self.sweep_stale_tasks()
                except Exception as e:      # 清扫失败不能拖垮服务
                    print(f"[!] 卡死任务清扫异常: {e}")

        t = threading.Thread(target=_loop, daemon=True, name="task-sweeper")
        t.start()
        print(f"[*] 卡死任务清扫已启动（每 {int(interval)} 秒一次；"
              f"阈值 processing>{int(STALE_PROCESSING_SECONDS)}s / pending>{int(STALE_PENDING_SECONDS)}s）")

    def requeue_task(self, task_id: str) -> None:
        """把「已取出但没能发给浏览器」的任务退回待处理队列。

        handle_browser_connect 里 get_task() 弹出任务后、_send_sse 发送前，
        浏览器连接可能刚好断开（MV3 service worker 被回收时极常见）。不退回
        的话：任务既不在内存 `_pending`、DB 又停在 `stage='waiting'` ——
        redispatch_stale_dispatched 只捞 `stage='dispatched'` 的，后续无论
        多少次重连都看不见它，只能干等 600 秒被清扫判失败
        （2026-10-15 Linux 实测：任务 70fb49ff 挂着扩展十余次重连都拿不到，
        最终「浏览器协助超时未响应」）。
        """
        with self._lock:
            conn = _get_db()
            conn.execute(
                "UPDATE tasks SET status='pending', stage='waiting', updated_at=?"
                " WHERE id=?",
                (time.time(), task_id))
            conn.commit()
            if task_id not in self._pending:
                self._pending.append(task_id)
        self._event.set()

    def redispatch_stale_dispatched(self, min_age: float = 240.0) -> list[str]:
        """把「已发给浏览器但迟迟没有回传」的任务重新放回内存队列。

        为什么需要它：任务一旦被 dispatch 就进入 processing，服务端只会从内存
        `_pending` 里取新任务发给浏览器 —— 已经发出去的那条**不会重发**。于是当
        扩展的 Service Worker 在抓取途中被 MV3 回收（结果永远回不来）时，
        该任务对浏览器而言凭空消失，只能干等 600 秒（STALE_PROCESSING_SECONDS）
        后被清扫判失败。

        min_age 的取值必须**足够宽**：这条链路一次正常的抓取要等标签页加载
        （waitForTabLoad 最长 15 秒）+ 懒加载滚动（约 3 秒）+ 页面内提取，
        而且浏览器回传是 `wikiSync=true` —— **服务端要生成完 wiki 才回响应**，
        API 直出也要数十秒。整条链正常就需要 1～2 分钟。
        min_age 定得太小（例如 45 秒）会把**仍在正常处理**的任务重复派发，
        导致同一个页面被打开两次、抓两遍。故这里取 240 秒，明显长于正常耗时，
        又短于 600 秒的清扫阈值 —— 这是「还没被判死、但已经明显不正常」的窗口。

        注意：重发只是把任务退回 pending 再发一次，**不改任务 ID**，
        浏览器侧与服务端按同一 taskId 收敛，重复处理是幂等的。
        """
        now = time.time()
        cutoff = now - min_age
        redispatch: list[str] = []
        with self._lock:
            conn = _get_db()
            rows = conn.execute(
                "SELECT id FROM tasks WHERE stage='dispatched' "
                "AND status='processing' AND updated_at > 0 AND updated_at < ?",
                (cutoff,),
            ).fetchall()
            for (tid,) in rows:
                conn.execute(
                    "UPDATE tasks SET stage='waiting', updated_at=? WHERE id=?",
                    (now, tid),
                )
                redispatch.append(tid)
            if redispatch:
                conn.commit()
                for tid in redispatch:
                    if tid not in self._pending:
                        self._pending.append(tid)
        if redispatch:
            print(f"[*] 浏览器重连：{len(redispatch)} 个任务无回传，重新派发："
                  f"{', '.join(redispatch)}")
            self._event.set()
        return redispatch

    def find_active_task(self, url: str) -> Optional[str]:
        """队列查重：同 URL 已有 pending/processing 任务时返回其 ID。

        先清扫卡死任务 —— 否则一次中断就会让这个 URL 永远无法再入库。
        注意排除 direct-clip 占位：它属于 /api/clip 直接推送（自带内容级去重
        闸门），且结果结构与队列任务不同，被轮询方误读会报假失败。
        """
        self.sweep_stale_tasks()
        with self._lock:
            conn = _get_db()
            row = conn.execute(
                "SELECT id FROM tasks WHERE status IN ('pending','processing') AND url=? "
                "AND COALESCE(stage,'') != 'direct-clip' "
                "ORDER BY created_at LIMIT 1",
                (url,),
            ).fetchone()
        return row[0] if row else None

    def get_task(self, timeout: float = 30) -> Optional[Task]:
        """获取一个待处理任务（阻塞等待）。"""
        while True:
            with self._lock:
                if self._pending:
                    task_id = self._pending.pop(0)
                    conn = _get_db()
                    cursor = conn.execute("""
                        SELECT id, url, created_at FROM tasks WHERE id=?
                    """, (task_id,))
                    row = cursor.fetchone()
                    if row:
                        conn.execute("""
                            UPDATE tasks SET status='processing', stage='waiting', updated_at=?
                            WHERE id=?
                        """, (time.time(), task_id))
                        conn.commit()
                        return Task(id=row[0], url=row[1], status="processing",
                                    stage="waiting", created_at=row[2])
            # 没有任务，等待通知或超时
            self._event.clear()
            got = self._event.wait(timeout=timeout)
            if not got:
                return None

    def update_stage(self, task_id: str, stage: str):
        """更新任务处理阶段。

        常用阶段：
        - waiting: 等待处理
        - dispatched: 已发送给浏览器
        - extracting: 正在提取内容
        - saving: 正在写入知识库
        - done: 完成
        - failed: 失败
        """
        with self._lock:
            conn = _get_db()
            conn.execute("""
                UPDATE tasks SET stage=?, updated_at=? WHERE id=?
            """, (stage, time.time(), task_id))
            conn.commit()

    def complete_task(self, task_id: str, result: dict):
        """标记任务完成。"""
        with self._lock:
            conn = _get_db()
            conn.execute("""
                UPDATE tasks SET status='done', stage='done', result=?, completed_at=?, updated_at=?
                WHERE id=?
            """, (json.dumps(result, ensure_ascii=False), time.time(), time.time(), task_id))
            conn.commit()

    def complete_duplicate_task(self, task_id: str, result: dict):
        """标记任务为「已完成（命中重复，未重复入库）」。

        为什么不能沿用 fail_task：命中去重**不是失败** —— 目标内容早已在知识库
        里，既没有丢数据也没有出错。过去服务端直抓路径用 `fail_task(id, "duplicate")`
        收尾，于是「最近记录」里凭空多出一条红色失败任务，用户会以为这次剪藏坏了
        （用户 2026-10-09 报「最近一次任务失败了」，实际就是这条）。

        状态仍记 `done`，靠 `result.duplicate` 与真正入库的记录区分开
        （前端据此显示「📌 已存在（未重复入库）」）。
        """
        payload = dict(result or {})
        payload["duplicate"] = True
        with self._lock:
            conn = _get_db()
            conn.execute("""
                UPDATE tasks SET status='done', stage='done', result=?, completed_at=?, updated_at=?
                WHERE id=?
            """, (json.dumps(payload, ensure_ascii=False), time.time(), time.time(), task_id))
            conn.commit()

    def settle_async_result(self, task_id: str, patch: dict) -> bool:
        """把**后台异步加工**（wiki / OCR）的最终结论合并进已有任务记录。

        为什么需要它：raw 落盘时 wiki 往往还在后台跑，任务那一刻只能以
        `result.asyncPending=True` 收尾（结论未知）。加工结束后若没人回写，
        「⏳ 后台生成中」就永远停在那里 —— 飞书卡片、任务面板、扩展弹窗
        都看不到最终成没成（用户 2026-10-14 报：OCR 排队跑完没有任何通知）。

        只改 result 与 updated_at，不动 status：任务本身早已完成，
        「有没有完整入库」由 result.wiki 表达（与 /api/clip 的口径一致）。
        合并时一并把 asyncPending 置回 False，供轮询方判断已落定。
        """
        with self._lock:
            conn = _get_db()
            row = conn.execute("SELECT result FROM tasks WHERE id=?",
                               (task_id,)).fetchone()
            if not row:
                return False
            try:
                current = json.loads(row[0]) if row[0] else {}
            except Exception:
                current = {}
            if not isinstance(current, dict):
                current = {}
            current.update(patch or {})
            current["asyncPending"] = False
            conn.execute("""
                UPDATE tasks SET result=?, updated_at=? WHERE id=?
            """, (json.dumps(current, ensure_ascii=False), time.time(), task_id))
            conn.commit()
            return True

    def fail_task(self, task_id: str, error: str):
        """标记任务失败。"""
        with self._lock:
            conn = _get_db()
            conn.execute("""
                UPDATE tasks SET status='failed', stage='failed', error=?, completed_at=?, updated_at=?
                WHERE id=?
            """, (error, time.time(), time.time(), task_id))
            conn.commit()

    def get_task_status(self, task_id: str) -> Optional[dict]:
        """查询任务状态。"""
        with self._lock:
            conn = _get_db()
            cursor = conn.execute("""
                SELECT id, url, status, stage, result, error, created_at, completed_at, updated_at
                FROM tasks WHERE id=?
            """, (task_id,))
            row = cursor.fetchone()
            if not row:
                return None
            status = row[2]
            stage = row[3] or ""
            created_at = row[6]
            updated_at = row[8]

            # 超时检测：沿用清扫阈值 —— 「可能卡住」的判定不该比「判死」更激进，
            # 否则会出现「界面说卡住、其实还在正常生成 wiki」的矛盾。
            maybe_stuck = False
            if status == "processing" and updated_at > 0:
                elapsed = time.time() - updated_at
                if elapsed > STALE_PROCESSING_SECONDS:
                    maybe_stuck = True

            return {
                "id": row[0],
                "url": row[1],
                "status": status,
                "stage": stage,
                "stageLabel": _stage_label(stage),
                "result": json.loads(row[4]) if row[4] else {},
                "error": row[5],
                "created_at": created_at,
                "completed_at": row[7],
                "maybeStuck": maybe_stuck,
            }

    def get_pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def wait_for_completion(self, task_id: str, timeout: float = 120) -> Optional[dict]:
        """等待任务完成，返回结果。"""
        start = time.time()
        while time.time() - start < timeout:
            with self._lock:
                conn = _get_db()
                cursor = conn.execute("""
                    SELECT status, result, error FROM tasks WHERE id=?
                """, (task_id,))
                row = cursor.fetchone()
                if row and row[0] in ("done", "failed"):
                    return {
                        "status": row[0],
                        "result": json.loads(row[1]) if row[1] else {},
                        "error": row[2],
                    }
            time.sleep(0.5)
        return None

    def cleanup(self) -> dict:
        """手动触发清理过期任务。返回清理统计。"""
        with self._lock:
            conn = _get_db()
            now = time.time()
            done_cutoff = now - _DONE_RETENTION
            failed_cutoff = now - _FAILED_RETENTION

            cursor1 = conn.execute("""
                DELETE FROM tasks WHERE status='done' AND completed_at > 0 AND completed_at < ?
            """, (done_cutoff,))
            cursor2 = conn.execute("""
                DELETE FROM tasks WHERE status='failed' AND completed_at > 0 AND completed_at < ?
            """, (failed_cutoff,))
            conn.commit()
            return {"deleted_done": cursor1.rowcount, "deleted_failed": cursor2.rowcount}

    def get_stats(self) -> dict:
        """获取任务统计信息。"""
        with self._lock:
            conn = _get_db()
            cursor = conn.execute("""
                SELECT status, COUNT(*) FROM tasks GROUP BY status
            """)
            counts = dict(cursor.fetchall())

            # 获取 DB 文件大小
            db_size = _DB_PATH.stat().st_size if _DB_PATH.exists() else 0

            return {
                "pending": counts.get("pending", 0),
                "processing": counts.get("processing", 0),
                "done": counts.get("done", 0),
                "failed": counts.get("failed", 0),
                "total": sum(counts.values()),
                "db_size_bytes": db_size,
                "db_path": str(_DB_PATH),
            }

    def list_active_tasks(self) -> list[dict]:
        """列出所有活跃任务（pending + processing）。"""
        with self._lock:
            conn = _get_db()
            cursor = conn.execute("""
                SELECT id, url, status, stage, created_at, updated_at, via
                FROM tasks WHERE status IN ('pending', 'processing')
                ORDER BY created_at ASC
            """)
            now = time.time()
            tasks = []
            for row in cursor.fetchall():
                updated_at = row[5]
                maybe_stuck = False
                if row[2] == "processing" and updated_at > 0:
                    maybe_stuck = (now - updated_at) > STALE_PROCESSING_SECONDS
                tasks.append({
                    "id": row[0],
                    "url": row[1],
                    "status": row[2],
                    "stage": row[3] or "",
                    "stageLabel": _stage_label(row[3] or ""),
                    "created_at": row[4],
                    "maybeStuck": maybe_stuck,
                    "via": row[6] or "",
                })
            return tasks

    def list_recent_tasks(self, limit: int = 50,
                          include_active: bool = True,
                          today_only: bool = False) -> list[dict]:
        """把所有任务按时间倒序返回（活跃 + 已完成 + 已失败），供面板展示历史。

        为什么需要：`list_active_tasks` 只回活跃任务，任务一旦成功/失败就从
        面板消失，用户无法回看「刚才那条到底成没成、什么时候成的」。这里
        把 done / failed 一并返回（DB 侧保留 done 7 天、failed 30 天）。

        today_only=True 时只回**今天**的任务，口径与顶部指标卡（`get_today_stats`）
        完全一致（同一套「今天 00:00」边界），避免「指标卡说今天 3 条、列表里
        却混着昨天的记录」这种对不上的情况。
        """
        where = "" if include_active else \
            "WHERE status IN ('done', 'failed')"
        params = []
        if today_only:
            import datetime as _d
            today_start = _d.datetime.now().replace(
                hour=0, minute=0, second=0, microsecond=0).timestamp()
            # 与 get_today_stats 一致：已完成看 completed_at，其余看 created_at。
            where = (where + " AND " if where else "WHERE ") + \
                "COALESCE(NULLIF(completed_at, 0), created_at) >= ?"
            params.append(today_start)
        with self._lock:
            conn = _get_db()
            cursor = conn.execute(f"""
                SELECT id, url, status, stage, error, result,
                       created_at, completed_at, updated_at, via
                FROM tasks {where}
                ORDER BY COALESCE(NULLIF(completed_at, 0), created_at) DESC
                LIMIT ?
            """, (*params, limit))
            now = time.time()
            tasks = []
            for row in cursor.fetchall():
                status = row[2]
                updated_at = row[8] or 0
                maybe_stuck = False
                if status == "processing" and updated_at > 0:
                    maybe_stuck = (now - updated_at) > STALE_PROCESSING_SECONDS
                tasks.append({
                    "id": row[0],
                    "url": row[1],
                    "status": status,
                    "stage": row[3] or "",
                    "stageLabel": _stage_label(row[3] or ""),
                    "error": row[4] or "",
                    "result": json.loads(row[5]) if row[5] else {},
                    "created_at": row[6],
                    "completed_at": row[7] or 0,
                    "via": row[9] or "",
                    "maybeStuck": maybe_stuck,
                })
            return tasks

    def list_tasks_by_status(self, status: str, limit: int = 20) -> list[dict]:
        """按状态列出任务。status 可以是 'done', 'failed', 'pending', 'processing'。"""
        with self._lock:
            conn = _get_db()
            cursor = conn.execute("""
                SELECT id, url, status, stage, error, result, created_at, completed_at, via
                FROM tasks WHERE status=?
                ORDER BY created_at DESC
                LIMIT ?
            """, (status, limit))
            tasks = []
            for row in cursor.fetchall():
                tasks.append({
                    "id": row[0],
                    "url": row[1],
                    "status": row[2],
                    "stage": row[3] or "",
                    "stageLabel": _stage_label(row[3] or ""),
                    "error": row[4],
                    "result": json.loads(row[5]) if row[5] else {},
                    "created_at": row[6],
                    "completed_at": row[7],
                    "via": row[8] or "",
                })
            return tasks

    def retry_task(self, task_id: str) -> dict:
        """重置失败任务为 pending，重新入队。返回操作结果。"""
        with self._lock:
            conn = _get_db()
            cursor = conn.execute("""
                SELECT id, url, status FROM tasks WHERE id=?
            """, (task_id,))
            row = cursor.fetchone()
            if not row:
                return {"success": False, "error": "任务不存在"}
            if row[2] not in ("failed", "done"):
                return {"success": False, "error": f"只能重试失败/完成的任务，当前状态: {row[2]}"}

            now = time.time()
            conn.execute("""
                UPDATE tasks SET status='pending', stage='', error='', completed_at=0, updated_at=?
                WHERE id=?
            """, (now, task_id))
            conn.commit()
            if task_id not in self._pending:
                self._pending.append(task_id)
            self._event.set()
            return {"success": True, "id": task_id, "url": row[1]}

    def requeue_for_backend(self, task_id: str) -> dict:
        """把任务退回「后端直抓」通道（而不是浏览器协助队列）。

        用于飞书这类 `allows_browser_fallback=False` 的平台：它们的 pending
        只会被浏览器 SSE 领取，一旦误入队列就再也回不到 lark-cli。重试时改用
        本方法把任务直接置回 processing/server-fetch，并**不**放进 `_pending`，
        由调用方在后台线程里跑后端抓取。
        """
        with self._lock:
            conn = _get_db()
            row = conn.execute(
                "SELECT id, url, via FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if not row:
                return {"success": False, "error": "任务不存在"}

            now = time.time()
            conn.execute("""
                UPDATE tasks SET status='processing', stage='server-fetch',
                                 error='', completed_at=0, updated_at=?
                WHERE id=?
            """, (now, task_id))
            conn.commit()
            # 万一它已被塞进内存队列（例如上一轮已 requeue_for_browser），
            # 必须摘出来，否则浏览器一连接还会把这个任务领走。
            if task_id in self._pending:
                self._pending.remove(task_id)
            return {"success": True, "id": row[0], "url": row[1], "via": row[2] or ""}

    def cancel_task(self, task_id: str) -> dict:
        """取消待处理任务。返回操作结果。"""
        with self._lock:
            conn = _get_db()
            cursor = conn.execute("""
                SELECT id, url, status FROM tasks WHERE id=?
            """, (task_id,))
            row = cursor.fetchone()
            if not row:
                return {"success": False, "error": "任务不存在"}
            if row[2] not in ("pending", "processing"):
                return {"success": False, "error": f"只能取消待处理/处理中的任务，当前状态: {row[2]}"}

            now = time.time()
            conn.execute("""
                UPDATE tasks SET status='failed', stage='failed', error='用户取消', completed_at=?, updated_at=?
                WHERE id=?
            """, (now, now, task_id))
            conn.commit()
            if task_id in self._pending:
                self._pending.remove(task_id)
            return {"success": True, "id": task_id, "url": row[1]}

    def get_today_stats(self) -> dict:
        """获取今日统计信息。"""
        import datetime
        today_start = datetime.datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp()

        with self._lock:
            conn = _get_db()
            # 今日完成
            cursor = conn.execute("""
                SELECT COUNT(*) FROM tasks WHERE status='done' AND completed_at >= ?
            """, (today_start,))
            today_done = cursor.fetchone()[0]
            # 今日失败
            cursor = conn.execute("""
                SELECT COUNT(*) FROM tasks WHERE status='failed' AND completed_at >= ?
            """, (today_start,))
            today_failed = cursor.fetchone()[0]
            # 今日新增
            cursor = conn.execute("""
                SELECT COUNT(*) FROM tasks WHERE created_at >= ?
            """, (today_start,))
            today_created = cursor.fetchone()[0]
            # 当前活跃
            cursor = conn.execute("""
                SELECT COUNT(*) FROM tasks WHERE status IN ('pending', 'processing')
            """)
            active = cursor.fetchone()[0]
            # 总任务数
            cursor = conn.execute("SELECT COUNT(*) FROM tasks")
            total = cursor.fetchone()[0]
            # 总完成数
            cursor = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='done'")
            total_done = cursor.fetchone()[0]
            # 总失败数
            cursor = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='failed'")
            total_failed = cursor.fetchone()[0]
            # 孤儿 raw 数量（仅当 frontmatter 的 wiki_refs 为空/缺失时才算孤儿，
            # 不能用 "wiki_refs: []" 子串匹配正文里的示例片段）
            articles_dir = Path(__file__).resolve().parent.parent / "raw" / "articles"
            orphan_count = 0
            if articles_dir.exists():
                for md in articles_dir.glob("*.md"):
                    try:
                        content = md.read_text(encoding="utf-8", errors="replace")
                        if not _extract_wiki_refs(content):
                            orphan_count += 1
                    except Exception:
                        pass
            # wiki topics 数量
            wiki_dir = Path(__file__).resolve().parent.parent / "wiki" / "topics"
            wiki_count = 0
            if wiki_dir.exists():
                wiki_count = len(list(wiki_dir.glob("**/*.md")))
            # inbox 待审区数量
            inbox_dir = Path(__file__).resolve().parent.parent / "raw" / "inbox"
            inbox_count = 0
            if inbox_dir.exists():
                inbox_count = len([f for f in inbox_dir.glob("*.md") if f.name != "README.md"])

            success_rate = 0
            if today_done + today_failed > 0:
                success_rate = round(today_done / (today_done + today_failed) * 100)

            return {
                "today_done": today_done,
                "today_failed": today_failed,
                "today_created": today_created,
                "today_success_rate": success_rate,
                "active": active,
                "total": total,
                "total_done": total_done,
                "total_failed": total_failed,
                "orphan_raw": orphan_count,
                "wiki_topics": wiki_count,
                "inbox_pending": inbox_count,
            }


# 全局单例
queue = TaskQueue()

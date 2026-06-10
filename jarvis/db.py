"""贾维斯 SQLite 存储层。

契约见实施计划 1.3(schema) / 1.4(公开接口)：
- 同步 sqlite3，check_same_thread=False + 全局锁（self.lock）
- WAL 模式；时间一律 datetime.now().astimezone().isoformat()；id 一律 uuid4().hex
- 所有返回 dict 的方法返回该行全部列（列名为 key）
"""
import os
import sqlite3
import threading
import uuid
from datetime import datetime

# 1.3 节锁定 schema
SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY, codex_thread_id TEXT, title TEXT,
  created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,
  role TEXT CHECK(role IN ('user','jarvis')), content TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS tasks(
  id TEXT PRIMARY KEY, session_id TEXT, source TEXT, prompt TEXT,
  status TEXT CHECK(status IN ('running','done','failed','cancelled')),
  result TEXT, error TEXT, usage_json TEXT, started_at TEXT, finished_at TEXT);
CREATE TABLE IF NOT EXISTS task_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, type TEXT,
  payload TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS approvals(
  id TEXT PRIMARY KEY, task_id TEXT, action TEXT, detail TEXT,
  risk_level TEXT, status TEXT CHECK(status IN ('pending','approved','denied','expired')),
  created_at TEXT, decided_at TEXT, decided_via TEXT);
CREATE TABLE IF NOT EXISTS cron_jobs(
  id TEXT PRIMARY KEY, name TEXT, cron TEXT, prompt TEXT,
  enabled INTEGER DEFAULT 1, created_at TEXT, last_run_at TEXT, last_status TEXT);
"""

# update_cron 允许修改的列（防止 **fields 注入任意 SQL 列名）
_CRON_FIELDS = {"name", "cron", "prompt", "enabled", "last_run_at", "last_status"}


def _now() -> str:
    """统一时间格式：本地时区 ISO 字符串。"""
    return datetime.now().astimezone().isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


class Database:
    def __init__(self, path: str):
        # 建目录、连库、执行 schema、WAL
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---------- 内部工具 ----------

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """加锁执行写语句并提交。"""
        with self.lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def _query_one(self, sql: str, params: tuple = ()) -> dict | None:
        with self.lock:
            row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def _query_all(self, sql: str, params: tuple = ()) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    # ---------- sessions ----------

    def create_session(self, title: str) -> dict:
        sid, now = _new_id(), _now()
        self._execute(
            "INSERT INTO sessions(id, codex_thread_id, title, created_at, updated_at)"
            " VALUES(?, NULL, ?, ?, ?)",
            (sid, title, now, now),
        )
        return self.get_session(sid)

    def get_session(self, session_id: str) -> dict | None:
        return self._query_one("SELECT * FROM sessions WHERE id=?", (session_id,))

    def set_session_thread(self, session_id: str, thread_id: str) -> None:
        self._execute(
            "UPDATE sessions SET codex_thread_id=?, updated_at=? WHERE id=?",
            (thread_id, _now(), session_id),
        )

    def list_sessions(self, limit: int = 50) -> list[dict]:
        # 新→旧（同时间戳用 rowid 兜底）
        return self._query_all(
            "SELECT * FROM sessions ORDER BY updated_at DESC, rowid DESC LIMIT ?",
            (limit,),
        )

    # ---------- messages ----------

    def add_message(self, session_id: str, role: str, content: str) -> dict:
        now = _now()
        cur = self._execute(
            "INSERT INTO messages(session_id, role, content, created_at)"
            " VALUES(?, ?, ?, ?)",
            (session_id, role, content, now),
        )
        # 会话有新消息 → 顺带刷新会话活跃时间
        self._execute(
            "UPDATE sessions SET updated_at=? WHERE id=?", (now, session_id)
        )
        return self._query_one(
            "SELECT * FROM messages WHERE id=?", (cur.lastrowid,)
        )

    def list_messages(self, session_id: str, limit: int = 200) -> list[dict]:
        # 取最近 limit 条，按时间正序返回
        rows = self._query_all(
            "SELECT * FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        )
        return list(reversed(rows))

    # ---------- tasks ----------

    def create_task(self, session_id: str | None, source: str, prompt: str) -> dict:
        tid = _new_id()
        self._execute(
            "INSERT INTO tasks(id, session_id, source, prompt, status, started_at)"
            " VALUES(?, ?, ?, ?, 'running', ?)",
            (tid, session_id, source, prompt, _now()),
        )
        return self.get_task(tid)

    def finish_task(self, task_id: str, status: str, result: str = "",
                    error: str = "", usage_json: str = "") -> None:
        self._execute(
            "UPDATE tasks SET status=?, result=?, error=?, usage_json=?, finished_at=?"
            " WHERE id=?",
            (status, result, error, usage_json, _now(), task_id),
        )

    def get_task(self, task_id: str) -> dict | None:
        return self._query_one("SELECT * FROM tasks WHERE id=?", (task_id,))

    def list_tasks(self, limit: int = 50) -> list[dict]:
        # 新→旧
        return self._query_all(
            "SELECT * FROM tasks ORDER BY started_at DESC, rowid DESC LIMIT ?",
            (limit,),
        )

    def add_task_event(self, task_id: str, type: str, payload: str) -> dict:
        cur = self._execute(
            "INSERT INTO task_events(task_id, type, payload, created_at)"
            " VALUES(?, ?, ?, ?)",
            (task_id, type, payload, _now()),
        )
        return self._query_one(
            "SELECT * FROM task_events WHERE id=?", (cur.lastrowid,)
        )

    def list_task_events(self, task_id: str) -> list[dict]:
        # 插入序（时间正序）
        return self._query_all(
            "SELECT * FROM task_events WHERE task_id=? ORDER BY id ASC", (task_id,)
        )

    # ---------- approvals ----------

    def create_approval(self, task_id: str | None, action: str, detail: str,
                        risk_level: str) -> dict:
        aid = _new_id()
        self._execute(
            "INSERT INTO approvals(id, task_id, action, detail, risk_level,"
            " status, created_at) VALUES(?, ?, ?, ?, ?, 'pending', ?)",
            (aid, task_id, action, detail, risk_level, _now()),
        )
        return self.get_approval(aid)

    def decide_approval(self, approval_id: str, status: str, via: str) -> dict | None:
        # 仅 pending 可决（WHERE 条件保证幂等/防并发双决）；非 pending 返回 None
        cur = self._execute(
            "UPDATE approvals SET status=?, decided_at=?, decided_via=?"
            " WHERE id=? AND status='pending'",
            (status, _now(), via, approval_id),
        )
        if cur.rowcount == 0:
            return None
        return self.get_approval(approval_id)

    def get_approval(self, approval_id: str) -> dict | None:
        return self._query_one("SELECT * FROM approvals WHERE id=?", (approval_id,))

    def list_approvals(self, status: str | None = None, limit: int = 50) -> list[dict]:
        # 新→旧；可按 status 过滤
        if status is None:
            return self._query_all(
                "SELECT * FROM approvals ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,),
            )
        return self._query_all(
            "SELECT * FROM approvals WHERE status=?"
            " ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (status, limit),
        )

    # ---------- cron ----------

    def create_cron(self, name: str, cron: str, prompt: str) -> dict:
        jid = _new_id()
        self._execute(
            "INSERT INTO cron_jobs(id, name, cron, prompt, enabled, created_at)"
            " VALUES(?, ?, ?, ?, 1, ?)",
            (jid, name, cron, prompt, _now()),
        )
        return self.get_cron(jid)

    def update_cron(self, job_id: str, **fields) -> dict | None:
        cols = {k: v for k, v in fields.items() if k in _CRON_FIELDS}
        if cols:
            assignments = ", ".join(f"{k}=?" for k in cols)
            cur = self._execute(
                f"UPDATE cron_jobs SET {assignments} WHERE id=?",
                (*cols.values(), job_id),
            )
            if cur.rowcount == 0:
                return None
        return self.get_cron(job_id)

    def delete_cron(self, job_id: str) -> bool:
        cur = self._execute("DELETE FROM cron_jobs WHERE id=?", (job_id,))
        return cur.rowcount > 0

    def list_cron(self) -> list[dict]:
        return self._query_all(
            "SELECT * FROM cron_jobs ORDER BY created_at ASC, rowid ASC"
        )

    def get_cron(self, job_id: str) -> dict | None:
        return self._query_one("SELECT * FROM cron_jobs WHERE id=?", (job_id,))

    def mark_cron_run(self, job_id: str, status: str) -> None:
        self._execute(
            "UPDATE cron_jobs SET last_run_at=?, last_status=? WHERE id=?",
            (_now(), status, job_id),
        )

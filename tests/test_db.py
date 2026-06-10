"""jarvis/db.py 单元测试 —— 契约见实施计划 1.3(schema) / 1.4(接口)。

约定：用 tmp_path 建临时 DB，不碰真实数据目录。
"""
import sqlite3
import threading

import pytest

from jarvis.db import Database


@pytest.fixture()
def db(tmp_path):
    # 故意用不存在的子目录，顺带验证 __init__ 会自动建目录
    return Database(str(tmp_path / "data" / "jarvis.db"))


# ---------- schema ----------

def test_schema_tables_created(db):
    rows = db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    names = {r["name"] for r in rows}
    assert {"sessions", "messages", "tasks", "task_events",
            "approvals", "cron_jobs"} <= names


def test_wal_mode_enabled(db):
    mode = db.conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


# ---------- sessions ----------

def test_create_and_get_session(db):
    s = db.create_session("测试会话")
    assert len(s["id"]) == 32  # uuid4().hex
    assert s["title"] == "测试会话"
    assert s["codex_thread_id"] is None
    assert s["created_at"] and s["updated_at"]

    got = db.get_session(s["id"])
    assert got == s


def test_get_session_missing_returns_none(db):
    assert db.get_session("nonexistent") is None


def test_set_session_thread(db):
    s = db.create_session("t")
    db.set_session_thread(s["id"], "thread-abc")
    assert db.get_session(s["id"])["codex_thread_id"] == "thread-abc"


def test_list_sessions_limit(db):
    ids = [db.create_session(f"s{i}")["id"] for i in range(5)]
    rows = db.list_sessions(limit=3)
    assert len(rows) == 3
    # 新→旧：最后创建的排最前
    assert rows[0]["id"] == ids[-1]


# ---------- messages ----------

def test_add_and_list_messages(db):
    s = db.create_session("chat")
    m1 = db.add_message(s["id"], "user", "你好")
    m2 = db.add_message(s["id"], "jarvis", "大哥好")
    assert m1["role"] == "user" and m1["content"] == "你好"
    assert m1["session_id"] == s["id"]
    assert m1["created_at"]

    msgs = db.list_messages(s["id"])
    assert [m["id"] for m in msgs] == [m1["id"], m2["id"]]  # 时间正序


def test_list_messages_limit_returns_latest(db):
    s = db.create_session("chat")
    for i in range(5):
        db.add_message(s["id"], "user", f"m{i}")
    msgs = db.list_messages(s["id"], limit=2)
    # 取最近 2 条，仍按时间正序返回
    assert [m["content"] for m in msgs] == ["m3", "m4"]


def test_message_role_check_constraint(db):
    s = db.create_session("chat")
    with pytest.raises(sqlite3.IntegrityError):
        db.add_message(s["id"], "alien", "bad role")


# ---------- tasks ----------

def test_create_task_defaults(db):
    t = db.create_task(None, "chat", "echo hi")
    assert len(t["id"]) == 32
    assert t["session_id"] is None
    assert t["source"] == "chat"
    assert t["prompt"] == "echo hi"
    assert t["status"] == "running"
    assert t["started_at"]
    assert t["finished_at"] is None


def test_finish_task(db):
    t = db.create_task("sess1", "chat", "p")
    db.finish_task(t["id"], "done", result="ok", usage_json='{"input_tokens":1}')
    got = db.get_task(t["id"])
    assert got["status"] == "done"
    assert got["result"] == "ok"
    assert got["usage_json"] == '{"input_tokens":1}'
    assert got["finished_at"]


def test_finish_task_failed_with_error(db):
    t = db.create_task(None, "cron", "p")
    db.finish_task(t["id"], "failed", error="boom")
    got = db.get_task(t["id"])
    assert got["status"] == "failed"
    assert got["error"] == "boom"


def test_finish_task_invalid_status_rejected(db):
    t = db.create_task(None, "chat", "p")
    with pytest.raises(sqlite3.IntegrityError):
        db.finish_task(t["id"], "exploded")


def test_get_task_missing_returns_none(db):
    assert db.get_task("missing") is None


def test_list_tasks_newest_first_with_limit(db):
    ids = [db.create_task(None, "chat", f"p{i}")["id"] for i in range(4)]
    rows = db.list_tasks(limit=2)
    assert len(rows) == 2
    assert rows[0]["id"] == ids[-1]
    assert rows[1]["id"] == ids[-2]


# ---------- task_events ----------

def test_add_and_list_task_events(db):
    t = db.create_task(None, "chat", "p")
    e1 = db.add_task_event(t["id"], "thread.started", '{"thread_id":"x"}')
    e2 = db.add_task_event(t["id"], "turn.completed", "{}")
    assert e1["task_id"] == t["id"]
    assert e1["type"] == "thread.started"
    assert e1["payload"] == '{"thread_id":"x"}'
    assert e1["created_at"]

    events = db.list_task_events(t["id"])
    assert [e["id"] for e in events] == [e1["id"], e2["id"]]  # 插入序


def test_list_task_events_scoped_to_task(db):
    t1 = db.create_task(None, "chat", "p1")
    t2 = db.create_task(None, "chat", "p2")
    db.add_task_event(t1["id"], "a", "{}")
    db.add_task_event(t2["id"], "b", "{}")
    assert len(db.list_task_events(t1["id"])) == 1


# ---------- approvals ----------

def test_create_approval_pending(db):
    a = db.create_approval("task1", "删除文件", "rm /tmp/x", "high")
    assert len(a["id"]) == 32
    assert a["task_id"] == "task1"
    assert a["action"] == "删除文件"
    assert a["detail"] == "rm /tmp/x"
    assert a["risk_level"] == "high"
    assert a["status"] == "pending"
    assert a["created_at"]
    assert a["decided_at"] is None
    assert a["decided_via"] is None


def test_create_approval_task_id_nullable(db):
    a = db.create_approval(None, "act", "d", "critical")
    assert a["task_id"] is None


def test_decide_approval(db):
    a = db.create_approval(None, "act", "d", "high")
    updated = db.decide_approval(a["id"], "approved", "web")
    assert updated["status"] == "approved"
    assert updated["decided_via"] == "web"
    assert updated["decided_at"]


def test_decide_approval_idempotent(db):
    """仅 pending 可决：二次决定返回 None。"""
    a = db.create_approval(None, "act", "d", "high")
    assert db.decide_approval(a["id"], "approved", "web") is not None
    assert db.decide_approval(a["id"], "denied", "web") is None
    # 状态保持第一次的决定
    assert db.get_approval(a["id"])["status"] == "approved"


def test_decide_approval_missing_returns_none(db):
    assert db.decide_approval("missing", "approved", "web") is None


def test_list_approvals_filter_and_limit(db):
    a1 = db.create_approval(None, "a1", "d", "high")
    a2 = db.create_approval(None, "a2", "d", "high")
    db.decide_approval(a1["id"], "denied", "web")

    pending = db.list_approvals(status="pending")
    assert [a["id"] for a in pending] == [a2["id"]]

    everything = db.list_approvals()
    assert len(everything) == 2
    assert len(db.list_approvals(limit=1)) == 1


# ---------- cron ----------

def test_create_cron(db):
    j = db.create_cron("早报", "0 8 * * *", "播报新闻")
    assert len(j["id"]) == 32
    assert j["name"] == "早报"
    assert j["cron"] == "0 8 * * *"
    assert j["prompt"] == "播报新闻"
    assert j["enabled"] == 1
    assert j["created_at"]
    assert j["last_run_at"] is None
    assert j["last_status"] is None


def test_update_cron(db):
    j = db.create_cron("n", "* * * * *", "p")
    updated = db.update_cron(j["id"], name="新名字", enabled=0)
    assert updated["name"] == "新名字"
    assert updated["enabled"] == 0
    assert updated["cron"] == "* * * * *"  # 未传字段不变


def test_update_cron_missing_returns_none(db):
    assert db.update_cron("missing", name="x") is None


def test_delete_cron(db):
    j = db.create_cron("n", "* * * * *", "p")
    assert db.delete_cron(j["id"]) is True
    assert db.get_cron(j["id"]) is None
    assert db.delete_cron(j["id"]) is False


def test_list_and_get_cron(db):
    j1 = db.create_cron("n1", "* * * * *", "p1")
    j2 = db.create_cron("n2", "* * * * *", "p2")
    ids = {j["id"] for j in db.list_cron()}
    assert ids == {j1["id"], j2["id"]}
    assert db.get_cron(j1["id"])["name"] == "n1"


def test_mark_cron_run(db):
    j = db.create_cron("n", "* * * * *", "p")
    db.mark_cron_run(j["id"], "done")
    got = db.get_cron(j["id"])
    assert got["last_status"] == "done"
    assert got["last_run_at"]


# ---------- 线程安全 ----------

def test_thread_safety_concurrent_writes(db):
    """check_same_thread=False + 全局锁：多线程并发写不崩、数据不丢。"""
    s = db.create_session("mt")
    errors = []

    def worker(n):
        try:
            for i in range(20):
                db.add_message(s["id"], "user", f"w{n}-{i}")
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(db.list_messages(s["id"], limit=1000)) == 160

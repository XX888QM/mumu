"""Task E 测试：jarvis/server.py（REST/WS/任务执行流）+ cli/jarvis。

契约依据：实施计划 1.11(REST)/1.12(WS)/1.13(任务执行流语义)/1.15(CLI)。
不碰真实 codex/Bark/APScheduler：全部组件用 Fake，按 1.4/1.5/1.6/1.9 锁定签名实现，
在 TestClient 启动前预注入 app.state.{db,engine,gateway,scheduler}；
server 的 lifespan 检测到已注入则跳过真实构造、只补挂回调。
"""
import asyncio
import importlib.util
import json
import os
import time
import uuid
from datetime import datetime
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from jarvis.config import settings

TOKEN = "test-token-1234"
H = {"Authorization": f"Bearer {TOKEN}"}
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _now() -> str:
    return datetime.now().astimezone().isoformat()


# ---------------- Fake 组件（签名与计划 1.4/1.5/1.6/1.9 锁定接口一致） ----------------

class FakeDatabase:
    """内存版 Database：实现 server 用到的全部 1.4 契约方法。"""

    def __init__(self):
        self.sessions: dict[str, dict] = {}
        self.messages: list[dict] = []
        self.tasks: dict[str, dict] = {}
        self.task_events: list[dict] = []
        self.approvals: dict[str, dict] = {}
        self.cron_jobs: dict[str, dict] = {}
        self._auto_id = 0

    def _next_id(self) -> int:
        self._auto_id += 1
        return self._auto_id

    # sessions
    def create_session(self, title: str) -> dict:
        row = {"id": uuid.uuid4().hex, "codex_thread_id": None, "title": title,
               "created_at": _now(), "updated_at": _now()}
        self.sessions[row["id"]] = row
        return dict(row)

    def get_session(self, session_id: str):
        row = self.sessions.get(session_id)
        return dict(row) if row else None

    def set_session_thread(self, session_id: str, thread_id: str) -> None:
        if session_id in self.sessions:
            self.sessions[session_id]["codex_thread_id"] = thread_id
            self.sessions[session_id]["updated_at"] = _now()

    def list_sessions(self, limit: int = 50) -> list[dict]:
        return [dict(r) for r in list(reversed(self.sessions.values()))[:limit]]

    # messages
    def add_message(self, session_id: str, role: str, content: str) -> dict:
        row = {"id": self._next_id(), "session_id": session_id, "role": role,
               "content": content, "created_at": _now()}
        self.messages.append(row)
        return dict(row)

    def list_messages(self, session_id: str, limit: int = 200) -> list[dict]:
        rows = [dict(m) for m in self.messages if m["session_id"] == session_id]
        return rows[:limit]

    # tasks
    def create_task(self, session_id, source: str, prompt: str) -> dict:
        row = {"id": uuid.uuid4().hex, "session_id": session_id, "source": source,
               "prompt": prompt, "status": "running", "result": "", "error": "",
               "usage_json": "", "started_at": _now(), "finished_at": None}
        self.tasks[row["id"]] = row
        return dict(row)

    def finish_task(self, task_id: str, status: str, result: str = "",
                    error: str = "", usage_json: str = "") -> None:
        row = self.tasks.get(task_id)
        if row:
            row.update(status=status, result=result, error=error,
                       usage_json=usage_json, finished_at=_now())

    def get_task(self, task_id: str):
        row = self.tasks.get(task_id)
        return dict(row) if row else None

    def list_tasks(self, limit: int = 50) -> list[dict]:
        return [dict(r) for r in list(reversed(self.tasks.values()))[:limit]]

    def add_task_event(self, task_id: str, type: str, payload: str) -> dict:
        row = {"id": self._next_id(), "task_id": task_id, "type": type,
               "payload": payload, "created_at": _now()}
        self.task_events.append(row)
        return dict(row)

    def list_task_events(self, task_id: str) -> list[dict]:
        return [dict(e) for e in self.task_events if e["task_id"] == task_id]

    # approvals
    def create_approval(self, task_id, action: str, detail: str, risk_level: str) -> dict:
        row = {"id": uuid.uuid4().hex, "task_id": task_id, "action": action,
               "detail": detail, "risk_level": risk_level, "status": "pending",
               "created_at": _now(), "decided_at": None, "decided_via": None}
        self.approvals[row["id"]] = row
        return dict(row)

    def decide_approval(self, approval_id: str, status: str, via: str):
        row = self.approvals.get(approval_id)
        if row is None or row["status"] != "pending":
            return None
        row.update(status=status, decided_at=_now(), decided_via=via)
        return dict(row)

    def get_approval(self, approval_id: str):
        row = self.approvals.get(approval_id)
        return dict(row) if row else None

    def list_approvals(self, status=None, limit: int = 50) -> list[dict]:
        rows = [dict(a) for a in reversed(self.approvals.values())
                if status is None or a["status"] == status]
        return rows[:limit]

    # cron
    def create_cron(self, name: str, cron: str, prompt: str) -> dict:
        row = {"id": uuid.uuid4().hex, "name": name, "cron": cron, "prompt": prompt,
               "enabled": 1, "created_at": _now(), "last_run_at": None, "last_status": None}
        self.cron_jobs[row["id"]] = row
        return dict(row)

    def update_cron(self, job_id: str, **fields):
        row = self.cron_jobs.get(job_id)
        if row is None:
            return None
        if "enabled" in fields and fields["enabled"] is not None:
            fields["enabled"] = int(fields["enabled"])
        row.update({k: v for k, v in fields.items() if v is not None or k in ("last_status",)})
        return dict(row)

    def delete_cron(self, job_id: str) -> bool:
        return self.cron_jobs.pop(job_id, None) is not None

    def list_cron(self) -> list[dict]:
        return [dict(r) for r in self.cron_jobs.values()]

    def get_cron(self, job_id: str):
        row = self.cron_jobs.get(job_id)
        return dict(row) if row else None

    def mark_cron_run(self, job_id: str, status: str) -> None:
        row = self.cron_jobs.get(job_id)
        if row:
            row.update(last_run_at=_now(), last_status=status)


# FakeEngine 默认事件流：模拟真实 codex JSONL（计划 0 节样本）
DEFAULT_EVENTS = [
    {"type": "thread.started", "thread_id": "th-fake-001"},
    {"type": "turn.started"},
    {"type": "item.completed",
     "item": {"id": "item_0", "type": "agent_message", "text": "大哥，搞定了"}},
    {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 20}},
]


class FakeEngineResult:
    """与 1.6 EngineResult 字段一致。"""

    def __init__(self, thread_id, final_message, usage):
        self.thread_id = thread_id
        self.final_message = final_message
        self.usage = usage


class FakeEngine:
    """run/cancel 签名与 1.6 CodexEngine 一致；on_event 由 server lifespan 注入。"""

    def __init__(self, events=None, final_message="大哥，搞定了",
                 thread_id="th-fake-001", usage=None, error=None, blocking=False):
        self.on_event = None
        self.events = DEFAULT_EVENTS if events is None else events
        self.final_message = final_message
        self.thread_id = thread_id
        self.usage = {"input_tokens": 100, "output_tokens": 20} if usage is None else usage
        self.error = error
        self.proceed = not blocking   # blocking=True 时 run 挂起，测试置 True 放行
        self.run_calls: list[dict] = []
        self.cancelled: list[str] = []

    async def run(self, prompt: str, task_id: str, thread_id=None, timeout=None):
        self.run_calls.append({"prompt": prompt, "task_id": task_id,
                               "thread_id": thread_id, "timeout": timeout})
        for ev in self.events:
            if self.on_event:
                await self.on_event(task_id, ev)
        while not self.proceed:
            await asyncio.sleep(0.01)
        if self.error is not None:
            raise self.error
        return FakeEngineResult(self.thread_id, self.final_message, dict(self.usage))

    def cancel(self, task_id: str) -> bool:
        self.cancelled.append(task_id)
        # 模拟子进程被 terminate：run 以异常返回（真实引擎抛 EngineError）
        self.error = RuntimeError("process terminated")
        self.proceed = True
        return True


class FakeApprovalGateway:
    """request/decide/expire_stale 签名与 1.5 一致；回调由 server lifespan 注入。"""

    def __init__(self, db, on_request=None, on_resolve=None):
        self.db = db
        self.on_request = on_request
        self.on_resolve = on_resolve

    async def request(self, task_id, action: str, detail: str, risk_level: str) -> dict:
        approval = self.db.create_approval(task_id, action, detail, risk_level)
        if self.on_request:
            await self.on_request(approval)
        return approval

    async def decide(self, approval_id: str, decision: str, via: str):
        approval = self.db.decide_approval(approval_id, decision, via)
        if approval and self.on_resolve:
            await self.on_resolve(approval)
        return approval

    def expire_stale(self) -> int:
        return 0


class FakeScheduler:
    """签名与 1.9 JarvisScheduler 一致；run_job 由 server lifespan 注入。"""

    def __init__(self, db):
        self.db = db
        self.run_job = None
        self.started = False
        self.triggered: list[str] = []

    def start(self) -> None:
        self.started = True

    def shutdown(self) -> None:
        self.started = False

    @staticmethod
    def _validate(cron: str) -> None:
        if len(cron.split()) != 5:
            raise ValueError(f"invalid cron: {cron!r}")

    def add(self, name: str, cron: str, prompt: str) -> dict:
        self._validate(cron)
        return self.db.create_cron(name, cron, prompt)

    def update(self, job_id: str, **fields):
        if fields.get("cron") is not None:
            self._validate(fields["cron"])
        return self.db.update_cron(job_id, **fields)

    def remove(self, job_id: str) -> bool:
        return self.db.delete_cron(job_id)

    def trigger_now(self, job_id: str) -> None:
        self.triggered.append(job_id)
        job = self.db.get_cron(job_id)
        if job and self.run_job:
            asyncio.get_running_loop().create_task(self.run_job(job))


# ---------------- fixtures ----------------

@pytest.fixture
def make_client(monkeypatch, tmp_path):
    """工厂：预注入 Fake 组件后启动 TestClient（触发 lifespan）。"""
    import jarvis.server as server_mod
    created = []

    def _make(engine=None):
        db = FakeDatabase()
        engine = engine or FakeEngine()
        scheduler = FakeScheduler(db)
        gateway = FakeApprovalGateway(db)
        monkeypatch.setattr(settings, "jarvis_token", TOKEN)
        monkeypatch.setattr(settings, "workspace", str(tmp_path / "workspace"))
        pushes: list[dict] = []

        async def _fake_push(title, body, url=None, level="active"):
            pushes.append({"title": title, "body": body, "url": url, "level": level})
            return True

        monkeypatch.setattr(server_mod, "bark_push", _fake_push)
        app = server_mod.app
        app.state.db = db
        app.state.engine = engine
        app.state.scheduler = scheduler
        app.state.gateway = gateway
        client = TestClient(app)
        client.__enter__()
        created.append(client)
        return SimpleNamespace(client=client, db=db, engine=engine,
                               scheduler=scheduler, gateway=gateway, pushes=pushes)

    yield _make
    for c in created:
        c.__exit__(None, None, None)
    # 清除注入，避免污染后续测试
    for attr in ("db", "engine", "scheduler", "gateway"):
        setattr(server_mod.app.state, attr, None)


def wait_for(predicate, timeout=5.0):
    """轮询等待条件成立（测试内短轮询）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def wait_for_task(client, task_id, timeout=5.0) -> dict:
    """等任务进入终态并返回任务详情。"""
    holder = {}

    def _done():
        r = client.get(f"/api/tasks/{task_id}", headers=H)
        if r.status_code == 200 and r.json()["status"] in ("done", "failed", "cancelled"):
            holder["task"] = r.json()
            return True
        return False

    assert wait_for(_done, timeout), f"任务 {task_id} 未在 {timeout}s 内结束"
    return holder["task"]


# ---------------- 认证 / healthz ----------------

def test_healthz_no_auth(make_client):
    env = make_client()
    r = env.client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_api_requires_token(make_client):
    env = make_client()
    # 无 token
    r = env.client.get("/api/sessions")
    assert r.status_code == 401
    assert r.json() == {"detail": "unauthorized"}
    # 错 token
    r = env.client.get("/api/sessions", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
    assert r.json() == {"detail": "unauthorized"}
    # 对 token
    r = env.client.get("/api/sessions", headers=H)
    assert r.status_code == 200


# ---------------- chat 任务执行流（1.13） ----------------

def test_chat_full_flow(make_client):
    env = make_client()
    r = env.client.post("/api/chat", json={"message": "你好贾维斯"}, headers=H)
    assert r.status_code == 202
    body = r.json()
    task_id, session_id = body["task_id"], body["session_id"]

    task = wait_for_task(env.client, task_id)
    assert task["status"] == "done"
    assert task["result"] == "大哥，搞定了"
    assert json.loads(task["usage_json"]) == {"input_tokens": 100, "output_tokens": 20}
    assert task["finished_at"]
    # 事件全部入库（GET /api/tasks/{id} 带 events）
    assert len(task["events"]) == len(DEFAULT_EVENTS)
    assert task["events"][0]["type"] == "thread.started"
    assert json.loads(task["events"][0]["payload"])["thread_id"] == "th-fake-001"

    # user + jarvis 两条消息
    r = env.client.get(f"/api/sessions/{session_id}/messages", headers=H)
    msgs = r.json()
    assert [m["role"] for m in msgs] == ["user", "jarvis"]
    assert msgs[0]["content"] == "你好贾维斯"
    assert msgs[1]["content"] == "大哥，搞定了"
    assert set(msgs[0].keys()) == {"role", "content", "created_at"}

    # thread.started → set_session_thread
    r = env.client.get("/api/sessions", headers=H)
    sess = [s for s in r.json() if s["id"] == session_id][0]
    assert sess["codex_thread_id"] == "th-fake-001"

    # engine.run 调用参数（1.13：新会话 thread_id=None）
    call = env.engine.run_calls[0]
    assert call["prompt"] == "你好贾维斯"
    assert call["thread_id"] is None
    assert call["timeout"] == settings.jarvis_task_timeout


def test_chat_resume_uses_session_thread(make_client):
    env = make_client()
    r = env.client.post("/api/chat", json={"message": "第一句"}, headers=H)
    sid = r.json()["session_id"]
    wait_for_task(env.client, r.json()["task_id"])

    r = env.client.post("/api/chat", json={"message": "第二句", "session_id": sid}, headers=H)
    assert r.status_code == 202
    assert r.json()["session_id"] == sid
    wait_for_task(env.client, r.json()["task_id"])
    # 续会话必须带上已存线程 id
    assert env.engine.run_calls[1]["thread_id"] == "th-fake-001"


def test_chat_unknown_session_404(make_client):
    env = make_client()
    r = env.client.post("/api/chat", json={"message": "hi", "session_id": "nonexistent"}, headers=H)
    assert r.status_code == 404


def test_chat_busy_409(make_client):
    env = make_client(engine=FakeEngine(blocking=True))
    r1 = env.client.post("/api/chat", json={"message": "慢任务"}, headers=H)
    assert r1.status_code == 202
    sid = r1.json()["session_id"]
    # 同会话再发 → busy
    r2 = env.client.post("/api/chat", json={"message": "again", "session_id": sid}, headers=H)
    assert r2.status_code == 409
    assert r2.json() == {"detail": "busy"}
    # 放行后任务正常完成，busy 解除
    env.engine.proceed = True
    wait_for_task(env.client, r1.json()["task_id"])
    r3 = env.client.post("/api/chat", json={"message": "third", "session_id": sid}, headers=H)
    assert r3.status_code == 202
    wait_for_task(env.client, r3.json()["task_id"])


def test_chat_failure_marks_failed_and_pushes(make_client):
    env = make_client(engine=FakeEngine(error=RuntimeError("引擎爆炸了")))
    r = env.client.post("/api/chat", json={"message": "干活"}, headers=H)
    task = wait_for_task(env.client, r.json()["task_id"])
    assert task["status"] == "failed"
    assert "引擎爆炸了" in task["error"]
    # 1.13 第4步：失败必须 bark_push
    assert wait_for(lambda: any(p["title"] == "贾维斯任务失败" for p in env.pushes))
    # 不应误触发重登提醒
    assert not any("重新登录" in p["title"] for p in env.pushes)


def test_chat_login_error_triggers_relogin_push(make_client):
    env = make_client(engine=FakeEngine(error=RuntimeError("stream error: 401 Unauthorized")))
    r = env.client.post("/api/chat", json={"message": "干活"}, headers=H)
    task = wait_for_task(env.client, r.json()["task_id"])
    assert task["status"] == "failed"
    # 1.13 第5步：error 含 401/unauthorized → 提醒重新登录 codex
    assert wait_for(lambda: any(p["title"] == "大哥需要重新登录 codex" for p in env.pushes))


# ---------------- tasks ----------------

def test_tasks_list_and_detail(make_client):
    env = make_client()
    r = env.client.post("/api/chat", json={"message": "任务一"}, headers=H)
    task_id = r.json()["task_id"]
    wait_for_task(env.client, task_id)

    r = env.client.get("/api/tasks", headers=H)
    assert r.status_code == 200
    assert any(t["id"] == task_id for t in r.json())

    r = env.client.get("/api/tasks?limit=1", headers=H)
    assert len(r.json()) == 1

    r = env.client.get(f"/api/tasks/{task_id}", headers=H)
    detail = r.json()
    assert detail["id"] == task_id
    assert isinstance(detail["events"], list)
    assert {"type", "payload", "created_at"} <= set(detail["events"][0].keys())

    assert env.client.get("/api/tasks/nope", headers=H).status_code == 404


def test_task_cancel(make_client):
    env = make_client(engine=FakeEngine(blocking=True))
    r = env.client.post("/api/chat", json={"message": "取消我"}, headers=H)
    task_id = r.json()["task_id"]

    r = env.client.post(f"/api/tasks/{task_id}/cancel", headers=H)
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert env.engine.cancelled == [task_id]

    task = wait_for_task(env.client, task_id)
    assert task["status"] == "cancelled"
    # 取消不算失败，不应推送失败通知
    assert not any(p["title"] == "贾维斯任务失败" for p in env.pushes)

    # 非 running → 404；不存在 → 404
    assert env.client.post(f"/api/tasks/{task_id}/cancel", headers=H).status_code == 404
    assert env.client.post("/api/tasks/nope/cancel", headers=H).status_code == 404


# ---------------- approvals ----------------

def test_approval_flow(make_client):
    env = make_client()
    r = env.client.post("/api/internal/approvals",
                        json={"action": "删除文件", "detail": "rm /tmp/x", "risk_level": "high"},
                        headers=H)
    assert r.status_code == 200
    aid = r.json()["approval_id"]

    # 列表过滤 pending
    r = env.client.get("/api/approvals?status=pending", headers=H)
    assert any(a["id"] == aid for a in r.json())

    # 单条查询（1.10 MCP 轮询用）
    r = env.client.get(f"/api/approvals/{aid}", headers=H)
    assert r.status_code == 200
    assert r.json()["status"] == "pending"
    assert env.client.get("/api/approvals/nope", headers=H).status_code == 404

    # 决定
    r = env.client.post(f"/api/approvals/{aid}/decide", json={"decision": "approved"}, headers=H)
    assert r.status_code == 200
    assert r.json()["status"] == "approved"
    assert r.json()["decided_via"] == "console"

    # 幂等：二次决定 → 409
    r = env.client.post(f"/api/approvals/{aid}/decide", json={"decision": "denied"}, headers=H)
    assert r.status_code == 409

    # 授权请求应触发 Bark 推送
    assert any("授权" in p["title"] for p in env.pushes)


def test_approval_decide_validation(make_client):
    env = make_client()
    r = env.client.post("/api/internal/approvals",
                        json={"action": "a", "detail": "d", "risk_level": "high"}, headers=H)
    aid = r.json()["approval_id"]
    # decision 只能是 approved/denied
    r = env.client.post(f"/api/approvals/{aid}/decide", json={"decision": "maybe"}, headers=H)
    assert r.status_code == 422


# ---------------- cron ----------------

def test_cron_crud_and_run(make_client):
    env = make_client()
    # 非法 cron → 422
    r = env.client.post("/api/cron", json={"name": "坏", "cron": "not a cron", "prompt": "x"},
                        headers=H)
    assert r.status_code == 422

    # 创建
    r = env.client.post("/api/cron",
                        json={"name": "晨报", "cron": "0 8 * * *", "prompt": "汇报天气"},
                        headers=H)
    assert r.status_code == 200
    job = r.json()
    assert job["name"] == "晨报" and job["cron"] == "0 8 * * *" and job["enabled"] == 1

    # 列表
    r = env.client.get("/api/cron", headers=H)
    assert any(j["id"] == job["id"] for j in r.json())

    # 更新
    r = env.client.patch(f"/api/cron/{job['id']}", json={"name": "晚报", "enabled": False},
                         headers=H)
    assert r.status_code == 200
    assert r.json()["name"] == "晚报" and r.json()["enabled"] == 0
    # 非法 cron 更新 → 422；不存在 → 404
    assert env.client.patch(f"/api/cron/{job['id']}", json={"cron": "bad"},
                            headers=H).status_code == 422
    assert env.client.patch("/api/cron/nope", json={"name": "x"}, headers=H).status_code == 404

    # 立即运行 → run_job 执行 cron 任务流（1.13）
    r = env.client.post(f"/api/cron/{job['id']}/run", headers=H)
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert env.scheduler.triggered == [job["id"]]

    def _cron_done():
        tasks = env.client.get("/api/tasks", headers=H).json()
        return any(t["source"] == "cron" and t["status"] == "done" for t in tasks)

    assert wait_for(_cron_done)
    cron_task = [t for t in env.client.get("/api/tasks", headers=H).json()
                 if t["source"] == "cron"][0]
    # cron prompt 必须带自动执行后缀（1.13 锁定文案）
    assert cron_task["prompt"].startswith("汇报天气")
    assert "(定时任务【晚报】自动执行，完成后用 notify 工具汇报结果摘要)" in cron_task["prompt"]
    # 完成后 mark_cron_run
    job_after = env.client.get("/api/cron", headers=H).json()[0]
    assert job_after["last_status"] == "done" and job_after["last_run_at"]

    # 删除
    assert env.client.delete(f"/api/cron/{job['id']}", headers=H).json() == {"ok": True}
    assert env.client.delete(f"/api/cron/{job['id']}", headers=H).status_code == 404
    # 对不存在的 job run → 404
    assert env.client.post(f"/api/cron/{job['id']}/run", headers=H).status_code == 404


# ---------------- internal 端点 ----------------

def test_internal_notify(make_client):
    env = make_client()
    r = env.client.post("/api/internal/notify", json={"title": "测试", "body": "内容"}, headers=H)
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert env.pushes[-1]["title"] == "测试" and env.pushes[-1]["body"] == "内容"


def test_internal_remember(make_client, tmp_path):
    env = make_client()
    r = env.client.post("/api/internal/remember", json={"content": "大哥喜欢黑色"}, headers=H)
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    memory = tmp_path / "workspace" / "memory.md"
    assert memory.exists()
    text = memory.read_text(encoding="utf-8")
    assert "大哥喜欢黑色" in text
    # 带时间戳（ISO 格式年份开头）
    assert str(datetime.now().year) in text


def test_internal_schedule(make_client):
    env = make_client()
    r = env.client.post("/api/internal/schedule",
                        json={"name": "夜巡", "cron": "0 2 * * *", "prompt": "巡检"}, headers=H)
    assert r.status_code == 200
    assert r.json()["name"] == "夜巡"
    assert env.client.get("/api/cron", headers=H).json()[0]["id"] == r.json()["id"]
    # 非法 cron → 422
    r = env.client.post("/api/internal/schedule",
                        json={"name": "坏", "cron": "bad", "prompt": "x"}, headers=H)
    assert r.status_code == 422


# ---------------- system ----------------

def test_system(make_client):
    env = make_client()
    r = env.client.get("/api/system", headers=H)
    assert r.status_code == 200
    data = r.json()
    assert {"cpu_percent", "mem_percent", "disk_percent", "uptime_sec",
            "codex_auth", "active_tasks"} <= set(data.keys())
    assert data["codex_auth"] in ("ok", "unknown", "error")
    assert data["active_tasks"] == 0


# ---------------- WebSocket（1.12） ----------------

def test_ws_bad_token_closed_4401(make_client):
    env = make_client()
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with env.client.websocket_connect("/ws?token=wrong") as ws:
            ws.receive_json()
    assert exc_info.value.code == 4401


def test_ws_ping_pong(make_client):
    env = make_client()
    with env.client.websocket_connect(f"/ws?token={TOKEN}") as ws:
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}


def test_ws_task_stream(make_client):
    env = make_client()
    with env.client.websocket_connect(f"/ws?token={TOKEN}") as ws:
        r = env.client.post("/api/chat", json={"message": "流式测试"}, headers=H)
        task_id = r.json()["task_id"]
        msgs = []
        for _ in range(20):
            m = ws.receive_json()
            msgs.append(m)
            if m["type"] == "task_done":
                break
        assert msgs[0]["type"] == "task_started"
        assert msgs[0]["task"]["id"] == task_id
        events = [m for m in msgs if m["type"] == "task_event"]
        assert len(events) == len(DEFAULT_EVENTS)
        assert all(m["task_id"] == task_id for m in events)
        assert events[0]["event"] == DEFAULT_EVENTS[0]  # codex 原始事件透传
        done = msgs[-1]
        assert done["type"] == "task_done"
        assert done["task_id"] == task_id
        assert done["status"] == "done"
        assert done["result"] == "大哥，搞定了"
        assert done["usage"] == {"input_tokens": 100, "output_tokens": 20}


def test_ws_approval_broadcast(make_client):
    env = make_client()
    with env.client.websocket_connect(f"/ws?token={TOKEN}") as ws:
        r = env.client.post("/api/internal/approvals",
                            json={"action": "发邮件", "detail": "给客户发报价", "risk_level": "critical"},
                            headers=H)
        aid = r.json()["approval_id"]
        m = ws.receive_json()
        assert m["type"] == "approval_request"
        assert m["approval"]["id"] == aid and m["approval"]["status"] == "pending"

        env.client.post(f"/api/approvals/{aid}/decide", json={"decision": "denied"}, headers=H)
        m2 = ws.receive_json()
        assert m2["type"] == "approval_resolved"
        assert m2["approval"]["status"] == "denied"


def test_ws_cron_changed_broadcast(make_client):
    env = make_client()
    with env.client.websocket_connect(f"/ws?token={TOKEN}") as ws:
        env.client.post("/api/cron",
                        json={"name": "j", "cron": "* * * * *", "prompt": "p"}, headers=H)
        assert ws.receive_json() == {"type": "cron_changed"}


# ---------------- CLI（1.15） ----------------

def _load_cli():
    path = str(PROJECT_ROOT / "cli" / "jarvis")
    loader = SourceFileLoader("jarvis_cli", path)
    spec = importlib.util.spec_from_loader("jarvis_cli", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_cli_file_executable_with_venv_shebang():
    path = PROJECT_ROOT / "cli" / "jarvis"
    assert path.exists()
    assert os.access(path, os.X_OK), "cli/jarvis 必须 chmod +x"
    first_line = path.read_text(encoding="utf-8").splitlines()[0]
    assert first_line.startswith("#!")
    assert ".venv/bin/python" in first_line, "shebang 必须用 venv python 绝对路径"


def test_cli_parser():
    cli = _load_cli()
    p = cli.build_parser()
    args = p.parse_args(["现在几点"])
    assert args.message == "现在几点" and args.session is None
    args = p.parse_args(["-s", "abc123", "继续"])
    assert args.session == "abc123" and args.message == "继续"
    assert p.parse_args(["--status"]).status is True
    assert p.parse_args(["--approvals"]).approvals is True


def test_cli_load_env(tmp_path):
    cli = _load_cli()
    f = tmp_path / ".env"
    f.write_text('JARVIS_PORT=9999\nJARVIS_TOKEN="tok123"\n# 注释行\n无效行\n',
                 encoding="utf-8")
    env = cli.load_env(f)
    assert env["JARVIS_PORT"] == "9999"
    assert env["JARVIS_TOKEN"] == "tok123"

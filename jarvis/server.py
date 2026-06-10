"""贾维斯 HTTP/WS 服务：REST API + WebSocket 实时广播 + 静态控制台托管。

模块组装（实施计划 Task E 锁定）：
- ``app = FastAPI()``，组件在 lifespan 中初始化并互相注入，全局单例放 ``app.state``。
- 测试可在启动前把 Fake 组件预放入 ``app.state.{db,engine,gateway,scheduler}``，
  lifespan 检测到已注入则跳过真实构造、只补挂回调（on_event / on_request / on_resolve / run_job）。
- REST 契约见计划 1.11；WS 协议见 1.12；任务执行流语义见 1.13。
"""
import asyncio
import contextlib
import hmac
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

import psutil
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.websockets import WebSocketDisconnect

from jarvis.config import settings

# 让 jarvis.* 的 INFO 日志可见（如 Bark 降级记录）；root 已有 handler 时为 no-op
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

logger = logging.getLogger("jarvis.server")

# 延迟绑定 jarvis.push.bark_push（契约 1.8）；测试 monkeypatch 本模块属性即可拦截推送
bark_push = None


async def _bark(title: str, body: str, url: Optional[str] = None, level: str = "active") -> bool:
    """统一推送入口：懒加载 push 模块；任何异常吞掉只记日志（推送绝不打断主流程）。"""
    global bark_push
    try:
        if bark_push is None:
            from jarvis.push import bark_push as _bp
            bark_push = _bp
        return await bark_push(title, body, url=url, level=level)
    except Exception:
        logger.exception("Bark 推送失败：%s", title)
        return False


# ---------------------------------------------------------------------------
# lifespan：组件初始化与互相注入（单例放 app.state）
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 运行态容器
    app.state.ws_clients = set()          # 在线 WebSocket 连接
    app.state.running_sessions = set()    # 正在跑任务的会话 id（busy 判定）
    app.state.running_tasks = set()       # 正在跑的 task id（active_tasks 统计）
    app.state.cancelled_tasks = set()     # 已请求取消的 task id（cancelled 状态判定）
    app.state.bg_tasks = set()            # 后台 asyncio.Task 强引用（防 GC 丢任务）
    app.state.started_at = time.monotonic()

    # db：测试可预注入 Fake，否则按契约 1.4 构造真库
    if getattr(app.state, "db", None) is None:
        from jarvis.db import Database
        app.state.db = Database(settings.db_path)

    # 授权网关（契约 1.5）：回调=WS 广播 + Bark 推送
    if getattr(app.state, "gateway", None) is None:
        from jarvis.approval import ApprovalGateway
        app.state.gateway = ApprovalGateway(
            app.state.db, on_request=_on_approval_request, on_resolve=_on_approval_resolved)
    else:
        app.state.gateway.on_request = _on_approval_request
        app.state.gateway.on_resolve = _on_approval_resolved
    # 启动时清理超时未决的 pending（契约 1.5）
    app.state.gateway.expire_stale()

    # codex 引擎（契约 1.6）：on_event=事件入库 + WS 广播
    if getattr(app.state, "engine", None) is None:
        from jarvis.engine import CodexEngine
        app.state.engine = CodexEngine(on_event=_on_engine_event)
    else:
        app.state.engine.on_event = _on_engine_event

    # 调度器（契约 1.9）：run_job=cron 任务流（1.13）
    if getattr(app.state, "scheduler", None) is None:
        from jarvis.scheduler import JarvisScheduler
        app.state.scheduler = JarvisScheduler(app.state.db, _run_cron_job)
    else:
        app.state.scheduler.run_job = _run_cron_job
    app.state.scheduler.start()

    # 每 5 秒向所有 WS 客户端广播系统状态（契约 1.12 system 事件）
    system_task = asyncio.create_task(_system_loop())
    try:
        yield
    finally:
        system_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await system_task
        app.state.scheduler.shutdown()


app = FastAPI(title="J.A.R.V.I.S.", lifespan=lifespan)


# ---------------------------------------------------------------------------
# WS 广播与回调
# ---------------------------------------------------------------------------

def _spawn(coro) -> asyncio.Task:
    """创建后台任务并持强引用。

    asyncio 事件循环只持任务的弱引用，create_task 后不保存返回值的话，
    运行中的任务可能被垃圾回收静默丢弃（官方文档明确警告）。
    统一收口到 app.state.bg_tasks，完成后自动移除。
    """
    task = asyncio.create_task(coro)
    app.state.bg_tasks.add(task)
    task.add_done_callback(app.state.bg_tasks.discard)
    return task


async def _broadcast(message: dict) -> None:
    """向所有在线 WS 客户端广播一条 JSON；发送失败的连接直接剔除。"""
    dead = []
    for ws in list(app.state.ws_clients):
        try:
            await ws.send_json(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        app.state.ws_clients.discard(ws)


async def _on_engine_event(task_id: str, event: dict) -> None:
    """引擎逐行事件回调（1.13 第2步）：入库 + 广播；thread.started 时回填会话线程。"""
    st = app.state
    st.db.add_task_event(task_id, event.get("type", "unknown"),
                         json.dumps(event, ensure_ascii=False))
    if event.get("type") == "thread.started" and event.get("thread_id"):
        task = st.db.get_task(task_id)
        if task and task.get("session_id"):
            session = st.db.get_session(task["session_id"])
            if session and not session.get("codex_thread_id"):
                st.db.set_session_thread(task["session_id"], event["thread_id"])
    await _broadcast({"type": "task_event", "task_id": task_id, "event": event})


async def _on_approval_request(approval: dict) -> None:
    """新授权请求：WS 红卡 + Bark 时效推送。"""
    await _broadcast({"type": "approval_request", "approval": approval})
    await _bark("贾维斯请求授权",
                f"[{approval.get('risk_level')}] {approval.get('action')}\n{approval.get('detail')}",
                level="timeSensitive")


async def _on_approval_resolved(approval: dict) -> None:
    """授权已决（批准/拒绝/过期）：WS 广播。"""
    await _broadcast({"type": "approval_resolved", "approval": approval})


def _system_info() -> dict:
    """系统状态快照（GET /api/system 与 WS system 事件共用）。"""
    try:
        codex_auth = "ok" if os.path.exists(os.path.expanduser("~/.codex/auth.json")) else "unknown"
    except Exception:
        codex_auth = "error"
    return {
        "cpu_percent": psutil.cpu_percent(interval=None),
        "mem_percent": psutil.virtual_memory().percent,
        "disk_percent": psutil.disk_usage("/").percent,
        "uptime_sec": int(time.monotonic() - app.state.started_at),
        "codex_auth": codex_auth,
        "active_tasks": len(app.state.running_tasks),
    }


async def _system_loop() -> None:
    while True:
        await asyncio.sleep(5)
        try:
            await _broadcast({"type": "system", "data": _system_info()})
        except Exception:
            logger.exception("system 广播失败")


# ---------------------------------------------------------------------------
# 任务执行流（1.13 锁定语义，chat 与 cron 共用核心）
# ---------------------------------------------------------------------------

async def _run_task(task: dict, prompt: str, *, session_id: Optional[str] = None,
                    thread_id: Optional[str] = None, job: Optional[dict] = None) -> None:
    st = app.state
    task_id = task["id"]
    status, result_text, usage = "done", "", {}
    try:
        result = await st.engine.run(prompt=prompt, task_id=task_id, thread_id=thread_id,
                                     timeout=settings.jarvis_task_timeout)
        result_text = result.final_message
        usage = result.usage or {}
        # 成功：写回 jarvis 消息 + finish(done)
        if session_id is not None:
            st.db.add_message(session_id, "jarvis", result_text)
        st.db.finish_task(task_id, "done", result=result_text,
                          usage_json=json.dumps(usage, ensure_ascii=False))
    except Exception as exc:  # EngineTimeout/EngineError 及其他异常统一按失败处理
        error_text = str(exc) or exc.__class__.__name__
        # 主动取消的任务标记 cancelled，不算失败
        status = "cancelled" if task_id in st.cancelled_tasks else "failed"
        st.db.finish_task(task_id, status, error=error_text)
        if status == "failed":
            logger.warning("任务 %s 失败：%s", task_id, error_text)
            await _bark("贾维斯任务失败", error_text[-300:])
            low = error_text.lower()
            if "401" in low or "unauthorized" in low or "login" in low:
                await _bark("大哥需要重新登录 codex", "codex login")
    finally:
        if session_id is not None:
            st.running_sessions.discard(session_id)
        st.running_tasks.discard(task_id)
        st.cancelled_tasks.discard(task_id)
    if job is not None:
        st.db.mark_cron_run(job["id"], status)
    await _broadcast({"type": "task_done", "task_id": task_id, "status": status,
                      "result": result_text, "usage": usage})


async def _run_cron_job(job: dict) -> None:
    """cron 任务流（1.13）：拼接自动执行后缀，无会话（thread_id=None 单次会话）。"""
    prompt = job["prompt"] + f"\n\n(定时任务【{job['name']}】自动执行，完成后用 notify 工具汇报结果摘要)"
    task = app.state.db.create_task(None, "cron", prompt)
    app.state.running_tasks.add(task["id"])
    await _broadcast({"type": "task_started", "task": task})
    await _run_task(task, prompt, job=job)


# ---------------------------------------------------------------------------
# 认证与请求模型
# ---------------------------------------------------------------------------

async def require_auth(request: Request) -> None:
    """除 /healthz 外全部要求 Bearer token（契约 1.11）。

    用 hmac.compare_digest 做常数时间比较，避免逐字节短路泄露 token 前缀（时序攻击）。
    """
    auth = request.headers.get("authorization", "")
    expected = f"Bearer {settings.jarvis_token}"
    if not settings.jarvis_token or not hmac.compare_digest(auth.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="unauthorized")


class ChatIn(BaseModel):
    message: str
    session_id: Optional[str] = None


class DecideIn(BaseModel):
    decision: Literal["approved", "denied"]


class CronIn(BaseModel):
    name: str
    cron: str
    prompt: str


class CronPatch(BaseModel):
    name: Optional[str] = None
    cron: Optional[str] = None
    prompt: Optional[str] = None
    enabled: Optional[bool] = None


class ApprovalIn(BaseModel):
    task_id: Optional[str] = None
    action: str
    detail: str
    risk_level: str = "high"


class NotifyIn(BaseModel):
    title: str
    body: str


class RememberIn(BaseModel):
    content: str


router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])


# ---------------------------------------------------------------------------
# chat / sessions / tasks
# ---------------------------------------------------------------------------

@router.post("/chat", status_code=202)
async def chat(body: ChatIn, request: Request):
    st = request.app.state
    if body.session_id:
        session = st.db.get_session(body.session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        session_id = body.session_id
        thread_id = session.get("codex_thread_id") or None
    else:
        session = st.db.create_session(title=body.message[:30])
        session_id = session["id"]
        thread_id = None
    # 该会话已有 running 任务 → busy（契约 1.11）
    if session_id in st.running_sessions:
        raise HTTPException(status_code=409, detail="busy")

    # 1.13 第1步：写 user 消息、建任务、广播 task_started
    st.db.add_message(session_id, "user", body.message)
    task = st.db.create_task(session_id, "chat", body.message)
    st.running_sessions.add(session_id)
    st.running_tasks.add(task["id"])
    await _broadcast({"type": "task_started", "task": task})
    # 后台执行，立即返回 202（_spawn 持强引用，防任务被 GC）
    _spawn(_run_task(task, body.message, session_id=session_id, thread_id=thread_id))
    return {"task_id": task["id"], "session_id": session_id}


@router.get("/sessions")
async def list_sessions(request: Request):
    return request.app.state.db.list_sessions()


@router.get("/sessions/{session_id}/messages")
async def list_messages(session_id: str, request: Request):
    rows = request.app.state.db.list_messages(session_id)
    return [{"role": m["role"], "content": m["content"], "created_at": m["created_at"]}
            for m in rows]


@router.get("/tasks")
async def list_tasks(request: Request, limit: int = 50):
    return request.app.state.db.list_tasks(limit)


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, request: Request):
    task = request.app.state.db.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    events = request.app.state.db.list_task_events(task_id)
    return {**task, "events": [{"type": e["type"], "payload": e["payload"],
                                "created_at": e["created_at"]} for e in events]}


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, request: Request):
    st = request.app.state
    task = st.db.get_task(task_id)
    if task is None or task["status"] != "running":
        raise HTTPException(status_code=404, detail="task not running")
    st.cancelled_tasks.add(task_id)
    st.engine.cancel(task_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# approvals
# ---------------------------------------------------------------------------

@router.get("/approvals")
async def list_approvals(request: Request, status: Optional[str] = None):
    return request.app.state.db.list_approvals(status or None)


@router.get("/approvals/{approval_id}")
async def get_approval(approval_id: str, request: Request):
    """单条授权查询（MCP 桥轮询用，见计划 1.10）。"""
    approval = request.app.state.db.get_approval(approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="approval not found")
    return approval


@router.post("/approvals/{approval_id}/decide")
async def decide_approval(approval_id: str, body: DecideIn, request: Request):
    approval = await request.app.state.gateway.decide(approval_id, body.decision, via="console")
    if approval is None:  # 非 pending（或不存在）→ 409（契约 1.11）
        raise HTTPException(status_code=409, detail="approval not pending")
    return approval


# ---------------------------------------------------------------------------
# cron
# ---------------------------------------------------------------------------

@router.get("/cron")
async def list_cron(request: Request):
    return request.app.state.db.list_cron()


@router.post("/cron")
async def create_cron(body: CronIn, request: Request):
    try:
        job = request.app.state.scheduler.add(body.name, body.cron, body.prompt)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"invalid cron: {exc}")
    await _broadcast({"type": "cron_changed"})
    return job


@router.patch("/cron/{job_id}")
async def update_cron(job_id: str, body: CronPatch, request: Request):
    fields = body.model_dump(exclude_unset=True)
    try:
        job = request.app.state.scheduler.update(job_id, **fields)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"invalid cron: {exc}")
    if job is None:
        raise HTTPException(status_code=404, detail="cron not found")
    await _broadcast({"type": "cron_changed"})
    return job


@router.delete("/cron/{job_id}")
async def delete_cron(job_id: str, request: Request):
    if not request.app.state.scheduler.remove(job_id):
        raise HTTPException(status_code=404, detail="cron not found")
    await _broadcast({"type": "cron_changed"})
    return {"ok": True}


@router.post("/cron/{job_id}/run")
async def run_cron(job_id: str, request: Request):
    if request.app.state.db.get_cron(job_id) is None:
        raise HTTPException(status_code=404, detail="cron not found")
    request.app.state.scheduler.trigger_now(job_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# system / internal
# ---------------------------------------------------------------------------

@router.get("/system")
async def get_system():
    return _system_info()


@router.post("/internal/approvals")
async def internal_approvals(body: ApprovalIn, request: Request):
    approval = await request.app.state.gateway.request(
        body.task_id, body.action, body.detail, body.risk_level)
    return {"approval_id": approval["id"]}


@router.post("/internal/notify")
async def internal_notify(body: NotifyIn):
    ok = await _bark(body.title, body.body)
    return {"ok": bool(ok)}


@router.post("/internal/schedule")
async def internal_schedule(body: CronIn, request: Request):
    try:
        job = request.app.state.scheduler.add(body.name, body.cron, body.prompt)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"invalid cron: {exc}")
    await _broadcast({"type": "cron_changed"})
    return job


@router.post("/internal/remember")
async def internal_remember(body: RememberIn):
    """带时间戳追加 workspace/memory.md（契约 1.11）。"""
    ws_dir = Path(settings.workspace)
    ws_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().astimezone().isoformat()
    with open(ws_dir / "memory.md", "a", encoding="utf-8") as f:
        f.write(f"- [{ts}] {body.content}\n")
    return {"ok": True}


app.include_router(router)


# ---------------------------------------------------------------------------
# healthz / WebSocket / 静态托管
# ---------------------------------------------------------------------------

@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    token = websocket.query_params.get("token", "")
    if not settings.jarvis_token or not hmac.compare_digest(
            token.encode(), settings.jarvis_token.encode()):
        # 契约 1.12：token 错直接关闭 code 4401（先 accept 再关，确保客户端能读到关闭码）
        await websocket.accept()
        await websocket.close(code=4401)
        return
    await websocket.accept()
    app.state.ws_clients.add(websocket)
    try:
        while True:
            try:
                msg = await websocket.receive_json()
            except json.JSONDecodeError:
                continue  # 非 JSON 消息忽略
            if isinstance(msg, dict) and msg.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        app.state.ws_clients.discard(websocket)


# 静态控制台：/ 挂 web/（index.html 默认页）。check_dir=False：web/ 由 F 队友交付，
# 集成前目录可能还不存在，不能让 import 崩掉。必须放在所有路由之后挂载。
app.mount("/", StaticFiles(directory=str(Path(settings.jarvis_root) / "web"),
                           html=True, check_dir=False), name="web")

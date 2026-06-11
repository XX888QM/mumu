"""Task D 测试：jarvis/mcp_server.py（MCP 工具桥）

契约见实施计划 1.10 / 第2节 Task D：
- 4 个工具对木木服务的 HTTP 请求 method/path/headers/body 必须正确（httpx.MockTransport）
- request_approval 轮询直到 approved（mock 序列 pending→pending→approved）
- 环境变量缺失时报清晰错误
- FastMCP 实例能 import 不崩、4 个工具全部注册

不起真实 stdio 进程、不碰真实网络。
"""

import asyncio
import json

import httpx
import pytest

import jarvis.mcp_server as mcp_server

BASE_ENV = {
    "JARVIS_URL": "http://127.0.0.1:8777",
    "JARVIS_TOKEN": "tok-test-1234",
    "JARVIS_TASK_ID": "task-abc",
}


@pytest.fixture
def env(monkeypatch):
    """注入引擎会提供的三个环境变量，并把轮询间隔缩到 0（测试不真等 2 秒）。"""
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(mcp_server, "POLL_INTERVAL", 0)
    yield


def install_transport(monkeypatch, handler):
    """把 MockTransport 挂进模块级测试钩子，让所有 HTTP 走假传输层。"""
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(mcp_server, "_transport", transport)
    return transport


# ---------- import 与注册 ----------


def test_fastmcp_instance_and_tools_registered():
    """模块可 import 不崩；FastMCP 实例存在且 4 个工具全部注册。"""
    from mcp.server.fastmcp import FastMCP

    assert isinstance(mcp_server.mcp, FastMCP)
    tools = asyncio.run(mcp_server.mcp.list_tools())
    names = {tool.name for tool in tools}
    assert names == {"request_approval", "notify", "schedule_task", "remember"}
    # docstring 是写给模型看的，必须非空
    for tool in tools:
        assert tool.description and tool.description.strip()


# ---------- notify ----------


def test_notify_request_shape(env, monkeypatch):
    """notify：POST /api/internal/notify，带 Bearer 头，body={"title","body"}，ok→"ok"。"""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    install_transport(monkeypatch, handler)
    assert mcp_server.notify("标题", "内容") == "ok"
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/internal/notify"
    assert captured["auth"] == "Bearer tok-test-1234"
    assert captured["body"] == {"title": "标题", "body": "内容"}


def test_notify_failed_when_ok_false(env, monkeypatch):
    """服务端返回 ok=false（如 BARK_KEY 未配）→ "failed"，不抛错。"""
    install_transport(monkeypatch, lambda req: httpx.Response(200, json={"ok": False}))
    assert mcp_server.notify("t", "b") == "failed"


def test_notify_failed_on_http_error(env, monkeypatch):
    """服务端 5xx → "failed"，不抛错。"""
    install_transport(monkeypatch, lambda req: httpx.Response(500, json={"detail": "boom"}))
    assert mcp_server.notify("t", "b") == "failed"


# ---------- schedule_task ----------


def test_schedule_task_request_shape(env, monkeypatch):
    """schedule_task：POST /api/internal/schedule，返回新 job 的 id。"""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"id": "job-001", "name": "每日巡检", "cron": "0 9 * * *", "prompt": "巡检并汇报", "enabled": 1},
        )

    install_transport(monkeypatch, handler)
    assert mcp_server.schedule_task("每日巡检", "0 9 * * *", "巡检并汇报") == "job-001"
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/internal/schedule"
    assert captured["auth"] == "Bearer tok-test-1234"
    assert captured["body"] == {"name": "每日巡检", "cron": "0 9 * * *", "prompt": "巡检并汇报"}


# ---------- remember ----------


def test_remember_request_shape(env, monkeypatch):
    """remember：POST /api/internal/remember，body={"content"}，成功→"ok"。"""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    install_transport(monkeypatch, handler)
    assert mcp_server.remember("大哥喜欢简洁的表格汇报") == "ok"
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/internal/remember"
    assert captured["auth"] == "Bearer tok-test-1234"
    assert captured["body"] == {"content": "大哥喜欢简洁的表格汇报"}


# ---------- request_approval ----------


def test_request_approval_polls_until_approved(env, monkeypatch):
    """先 POST 建审批单，再轮询 GET /api/approvals/{id}：pending→pending→approved。"""
    calls = []
    statuses = iter(["pending", "pending", "approved"])

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.headers.get("Authorization")))
        if request.method == "POST":
            assert request.url.path == "/api/internal/approvals"
            body = json.loads(request.content)
            assert body == {
                "task_id": "task-abc",
                "action": "删除测试文件",
                "detail": "rm /tmp/jarvis_test.txt",
                "risk_level": "critical",
            }
            return httpx.Response(200, json={"approval_id": "ap-1"})
        assert request.method == "GET"
        assert request.url.path == "/api/approvals/ap-1"
        return httpx.Response(200, json={"id": "ap-1", "status": next(statuses)})

    install_transport(monkeypatch, handler)
    result = mcp_server.request_approval("删除测试文件", "rm /tmp/jarvis_test.txt", "critical")
    assert result == "approved"
    get_calls = [c for c in calls if c[0] == "GET"]
    assert len(get_calls) == 3  # pending、pending、approved 各轮询一次
    assert all(auth == "Bearer tok-test-1234" for _, _, auth in calls)


def test_request_approval_default_risk_level_high(env, monkeypatch):
    """不传 risk_level 时默认 "high"；denied 立即返回。"""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"approval_id": "ap-2"})
        return httpx.Response(200, json={"id": "ap-2", "status": "denied"})

    install_transport(monkeypatch, handler)
    assert mcp_server.request_approval("发邮件", "给客户发报价邮件") == "denied"
    assert captured["body"]["risk_level"] == "high"


def test_request_approval_timeout_returns_expired(env, monkeypatch):
    """一直 pending 且超过 APPROVAL_TIMEOUT → 返回 "expired"，且回写服务端置过期。"""
    monkeypatch.setenv("APPROVAL_TIMEOUT", "0")
    posts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts.append(request.url.path)
            return httpx.Response(200, json={"approval_id": "ap-3", "id": "ap-3",
                                             "status": "expired"})
        return httpx.Response(200, json={"id": "ap-3", "status": "pending"})

    install_transport(monkeypatch, handler)
    assert mcp_server.request_approval("危险操作", "细节") == "expired"
    # 审查修复：超时必须回写 pending→expired（防脏数据滞留到下次服务重启）
    assert posts[-1] == "/api/internal/approvals/ap-3/expire"


def test_request_approval_timeout_writeback_failure_tolerated(env, monkeypatch):
    """超时回写接口网络错时仍返回 "expired"（回写是 best-effort）。"""
    monkeypatch.setenv("APPROVAL_TIMEOUT", "0")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/expire"):
            raise httpx.ConnectError("server gone")
        if request.method == "POST":
            return httpx.Response(200, json={"approval_id": "ap-4"})
        return httpx.Response(200, json={"id": "ap-4", "status": "pending"})

    install_transport(monkeypatch, handler)
    assert mcp_server.request_approval("危险操作", "细节") == "expired"


# ---------- 环境变量缺失 ----------


def test_missing_jarvis_url_raises_clear_error(monkeypatch):
    """缺 JARVIS_URL → RuntimeError，错误信息点名缺哪个变量。"""
    monkeypatch.delenv("JARVIS_URL", raising=False)
    monkeypatch.setenv("JARVIS_TOKEN", "x")
    with pytest.raises(RuntimeError, match="JARVIS_URL"):
        mcp_server.notify("t", "b")


def test_missing_jarvis_token_raises_clear_error(monkeypatch):
    """缺 JARVIS_TOKEN → RuntimeError 点名变量。"""
    monkeypatch.setenv("JARVIS_URL", "http://127.0.0.1:8777")
    monkeypatch.delenv("JARVIS_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="JARVIS_TOKEN"):
        mcp_server.remember("内容")


def test_missing_task_id_raises_clear_error(monkeypatch):
    """request_approval 必须带 task_id；缺 JARVIS_TASK_ID → RuntimeError 点名变量。"""
    monkeypatch.setenv("JARVIS_URL", "http://127.0.0.1:8777")
    monkeypatch.setenv("JARVIS_TOKEN", "x")
    monkeypatch.delenv("JARVIS_TASK_ID", raising=False)
    with pytest.raises(RuntimeError, match="JARVIS_TASK_ID"):
        mcp_server.request_approval("动作", "细节")

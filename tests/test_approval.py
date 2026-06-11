"""jarvis/approval.py 单元测试 —— 契约见实施计划 1.5。

测试要点：request 触发 on_request；decide 幂等（二次返回 None）；
wait() approved/denied/超时三路径（超时用 0.1s 短超时）；expire_stale。
"""
import asyncio
from datetime import datetime, timedelta

import pytest

from jarvis.approval import ApprovalGateway
from jarvis.db import Database


@pytest.fixture()
def db(tmp_path):
    return Database(str(tmp_path / "jarvis.db"))


# ---------- request ----------

@pytest.mark.asyncio
async def test_request_creates_pending_and_fires_on_request(db):
    seen = []

    async def on_request(approval):
        seen.append(approval)

    gw = ApprovalGateway(db, on_request=on_request)
    a = await gw.request("task1", "删除文件", "rm /tmp/x", "high")

    assert a["status"] == "pending"
    assert a["task_id"] == "task1"
    assert a["action"] == "删除文件"
    assert a["risk_level"] == "high"
    # 回调收到的就是这条 approval
    assert seen == [a]
    # 已落库
    assert db.get_approval(a["id"])["status"] == "pending"


@pytest.mark.asyncio
async def test_request_without_callbacks(db):
    """未注入回调时不报错（不阻塞等待）。"""
    gw = ApprovalGateway(db)
    a = await gw.request(None, "act", "d", "critical")
    assert a["status"] == "pending"


# ---------- decide ----------

@pytest.mark.asyncio
async def test_decide_approves_and_fires_on_resolve(db):
    resolved = []

    async def on_resolve(approval):
        resolved.append(approval)

    gw = ApprovalGateway(db, on_resolve=on_resolve)
    a = await gw.request(None, "act", "d", "high")
    updated = await gw.decide(a["id"], "approved", "web")

    assert updated["status"] == "approved"
    assert updated["decided_via"] == "web"
    assert updated["decided_at"]
    assert resolved == [updated]


@pytest.mark.asyncio
async def test_decide_idempotent_second_returns_none(db):
    resolved = []

    async def on_resolve(approval):
        resolved.append(approval)

    gw = ApprovalGateway(db, on_resolve=on_resolve)
    a = await gw.request(None, "act", "d", "high")
    assert await gw.decide(a["id"], "denied", "web") is not None
    # 幂等：非 pending 返回 None，且不重复触发 on_resolve
    assert await gw.decide(a["id"], "approved", "bark") is None
    assert len(resolved) == 1
    assert db.get_approval(a["id"])["status"] == "denied"


@pytest.mark.asyncio
async def test_decide_missing_returns_none(db):
    gw = ApprovalGateway(db)
    assert await gw.decide("missing", "approved", "web") is None


# ---------- wait 三路径 ----------

@pytest.mark.asyncio
async def test_wait_returns_approved(db):
    gw = ApprovalGateway(db)
    a = await gw.request(None, "act", "d", "high")
    await gw.decide(a["id"], "approved", "web")
    # 已决：立即返回最终状态
    status = await gw.wait(a["id"], timeout=5)
    assert status == "approved"


@pytest.mark.asyncio
async def test_wait_polls_until_denied(db):
    """wait 挂起期间另一协程做出决定，wait 轮询拿到 denied。"""
    gw = ApprovalGateway(db)
    a = await gw.request(None, "act", "d", "high")

    async def decide_later():
        await asyncio.sleep(0.05)
        await gw.decide(a["id"], "denied", "web")

    task = asyncio.ensure_future(decide_later())
    status = await gw.wait(a["id"], timeout=5)
    await task
    assert status == "denied"


@pytest.mark.asyncio
async def test_wait_timeout_sets_expired_and_fires_on_resolve(db):
    resolved = []

    async def on_resolve(approval):
        resolved.append(approval)

    gw = ApprovalGateway(db, on_resolve=on_resolve)
    a = await gw.request(None, "act", "d", "high")
    status = await gw.wait(a["id"], timeout=0.1)

    assert status == "expired"
    assert db.get_approval(a["id"])["status"] == "expired"
    assert len(resolved) == 1
    assert resolved[0]["status"] == "expired"


# ---------- expire_stale ----------

def test_expire_stale(db):
    gw = ApprovalGateway(db)
    old = db.create_approval(None, "旧申请", "d", "high")
    fresh = db.create_approval(None, "新申请", "d", "high")
    decided = db.create_approval(None, "已决申请", "d", "high")
    db.decide_approval(decided["id"], "approved", "web")

    # 把 old 的 created_at 改成 2 天前（远超 APPROVAL_TIMEOUT 默认 1800s）
    two_days_ago = (datetime.now().astimezone() - timedelta(days=2)).isoformat()
    with db.lock:
        db.conn.execute(
            "UPDATE approvals SET created_at=? WHERE id=?",
            (two_days_ago, old["id"]),
        )
        db.conn.commit()

    n = gw.expire_stale()
    assert n == 1
    assert db.get_approval(old["id"])["status"] == "expired"
    assert db.get_approval(fresh["id"])["status"] == "pending"
    assert db.get_approval(decided["id"])["status"] == "approved"


def test_expire_stale_nothing_to_do(db):
    gw = ApprovalGateway(db)
    assert gw.expire_stale() == 0


# ---------- expire（2026-06-11 审查修复：MCP 桥超时回写） ----------

@pytest.mark.asyncio
async def test_expire_pending_sets_expired_and_fires_on_resolve(db):
    resolved = []

    async def on_resolve(approval):
        resolved.append(approval)

    gw = ApprovalGateway(db, on_resolve=on_resolve)
    a = await gw.request(None, "act", "d", "high")
    updated = await gw.expire(a["id"])

    assert updated["status"] == "expired"
    assert updated["decided_via"] == "timeout"
    assert resolved == [updated]
    assert db.get_approval(a["id"])["status"] == "expired"


@pytest.mark.asyncio
async def test_expire_non_pending_is_idempotent_none(db):
    """已决申请再 expire → None 且不触发回调、不改状态。"""
    resolved = []

    async def on_resolve(approval):
        resolved.append(approval)

    gw = ApprovalGateway(db, on_resolve=on_resolve)
    a = await gw.request(None, "act", "d", "high")
    await gw.decide(a["id"], "approved", "console")
    resolved.clear()

    assert await gw.expire(a["id"]) is None
    assert resolved == []
    assert db.get_approval(a["id"])["status"] == "approved"

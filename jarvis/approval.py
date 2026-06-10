"""分级授权网关：高危操作的"申请-决定-等待"状态机。

契约见实施计划 1.5：
- request：创建 pending 记录，触发 on_request，立即返回（不阻塞等待）
- decide：仅 pending 可决（幂等，非 pending 返回 None），触发 on_resolve
- wait：轮询 db 每 1s 直到非 pending 或超时；超时置 expired 并触发 on_resolve
- expire_stale：启动时清理超过 APPROVAL_TIMEOUT 的 pending
"""
import asyncio
import inspect
import logging
import time
from datetime import datetime

from jarvis.config import settings
from jarvis.db import Database

logger = logging.getLogger(__name__)

# 轮询间隔（契约锁定 1s）
_POLL_INTERVAL = 1.0


class ApprovalGateway:
    def __init__(self, db: Database, on_request=None, on_resolve=None):
        # on_request(approval: dict)、on_resolve(approval: dict)：
        # 异步回调，由 server 注入（广播 WS + Bark 推送）
        self.db = db
        self.on_request = on_request
        self.on_resolve = on_resolve

    async def _fire(self, callback, approval: dict) -> None:
        """触发回调；兼容 async/sync；回调异常只记日志不影响主流程。"""
        if callback is None:
            return
        try:
            result = callback(approval)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception("授权回调执行失败: approval_id=%s", approval.get("id"))

    async def request(self, task_id: str | None, action: str, detail: str,
                      risk_level: str) -> dict:
        """创建 pending 授权申请，触发 on_request，返回 approval dict（不阻塞等待）。"""
        approval = self.db.create_approval(task_id, action, detail, risk_level)
        await self._fire(self.on_request, approval)
        return approval

    async def decide(self, approval_id: str, decision: str, via: str) -> dict | None:
        """做出决定。decision in ('approved','denied')；幂等：非 pending 返回 None。"""
        if decision not in ("approved", "denied"):
            raise ValueError(f"非法 decision: {decision!r}（只接受 approved/denied）")
        updated = self.db.decide_approval(approval_id, decision, via)
        if updated is None:
            return None
        await self._fire(self.on_resolve, updated)
        return updated

    async def wait(self, approval_id: str, timeout: float) -> str:
        """轮询 db 每 1s 直到非 pending 或超时；超时置 expired 并触发 on_resolve。

        返回最终 status（approved/denied/expired）。
        """
        deadline = time.monotonic() + timeout
        while True:
            row = self.db.get_approval(approval_id)
            if row is None:
                raise ValueError(f"approval 不存在: {approval_id}")
            if row["status"] != "pending":
                return row["status"]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # 超时：置 expired；若恰好被并发决定（decide 抢先），以最终状态为准
                expired = self.db.decide_approval(approval_id, "expired", "timeout")
                if expired is not None:
                    await self._fire(self.on_resolve, expired)
                    return expired["status"]
                return self.db.get_approval(approval_id)["status"]
            await asyncio.sleep(min(_POLL_INTERVAL, remaining))

    def expire_stale(self) -> int:
        """启动时清理：把超过 APPROVAL_TIMEOUT 的 pending 置 expired，返回清理条数。"""
        now = datetime.now().astimezone()
        count = 0
        for row in self.db.list_approvals(status="pending", limit=10000):
            try:
                created = datetime.fromisoformat(row["created_at"])
            except (TypeError, ValueError):
                # created_at 损坏的脏数据：视为过期清掉
                created = None
            if created is None or (now - created).total_seconds() > settings.approval_timeout:
                if self.db.decide_approval(row["id"], "expired", "timeout") is not None:
                    count += 1
        return count

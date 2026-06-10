"""jarvis/scheduler.py —— 定时任务调度器（契约 1.9）。

封装 APScheduler AsyncIOScheduler：
- cron 任务持久化在 SQLite（cron_jobs 表），进程重启后由 start() 从库恢复 enabled 任务；
- 真正的执行逻辑（组 prompt 调引擎、mark_cron_run、notify 汇报）由 server 注入的
  run_job 异步回调承担，调度器本身只负责"到点调 run_job(job)"。
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

if TYPE_CHECKING:  # 仅类型标注用，避免运行期硬依赖（db.py 由 Agent A 提供）
    from jarvis.db import Database

logger = logging.getLogger("jarvis.scheduler")


class JarvisScheduler:
    def __init__(self, db: Database, run_job):
        # run_job: async (job: dict) -> None，由 server 注入
        self.db = db
        self.run_job = run_job
        self.scheduler = AsyncIOScheduler()
        # trigger_now 创建的后台任务强引用（asyncio 只持弱引用，防 GC 丢任务）
        self._bg_tasks: set[asyncio.Task] = set()

    def start(self) -> None:
        """启动调度器，并从数据库恢复全部 enabled=1 的定时任务。"""
        for job in self.db.list_cron():
            if job.get("enabled"):
                self._register(job)
        self.scheduler.start()
        logger.info("调度器已启动，恢复 %d 个定时任务", len(self.scheduler.get_jobs()))

    def shutdown(self) -> None:
        """停止调度器（幂等：未启动时直接返回）。"""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def add(self, name: str, cron: str, prompt: str) -> dict:
        """新建定时任务：先校验 cron（非法抛 ValueError），再写库 + 注册。"""
        CronTrigger.from_crontab(cron)  # 非法 cron 在落库前抛 ValueError
        job = self.db.create_cron(name, cron, prompt)
        self._register(job)
        logger.info("新建定时任务 %s(%s): %s", job["name"], job["id"], job["cron"])
        return job

    def update(self, job_id: str, **fields) -> dict | None:
        """更新定时任务：改库后按 enabled 重注册/暂停；job 不存在返回 None。"""
        if "cron" in fields:
            CronTrigger.from_crontab(fields["cron"])  # 先校验，避免脏数据落库
        job = self.db.update_cron(job_id, **fields)
        if job is None:
            return None
        self._unregister(job_id)
        if job.get("enabled"):
            self._register(job)
        return job

    def remove(self, job_id: str) -> bool:
        """删除定时任务：取消注册 + 删库。"""
        self._unregister(job_id)
        return self.db.delete_cron(job_id)

    def trigger_now(self, job_id: str) -> None:
        """立即异步执行一次（不等 cron 到点）；job 不存在抛 ValueError。"""
        job = self.db.get_cron(job_id)
        if job is None:
            raise ValueError(f"定时任务不存在: {job_id}")
        task = asyncio.get_running_loop().create_task(self._execute(job_id))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    # ---------- 内部 ----------

    def _register(self, job: dict) -> None:
        """把一条 cron_jobs 行注册进 APScheduler（id 复用库主键，重复注册覆盖）。"""
        self.scheduler.add_job(
            self._execute,
            trigger=CronTrigger.from_crontab(job["cron"]),
            id=job["id"],
            args=[job["id"]],
            name=job.get("name"),
            misfire_grace_time=60,
            coalesce=True,
            replace_existing=True,
        )

    def _unregister(self, job_id: str) -> None:
        """从 APScheduler 移除（不存在时静默）。"""
        try:
            self.scheduler.remove_job(job_id)
        except JobLookupError:
            pass

    async def _execute(self, job_id: str) -> None:
        """到点/手动触发的统一入口：取最新行喂给 run_job，异常只记日志不上抛。"""
        job = self.db.get_cron(job_id)
        if job is None:
            return  # 触发与删除竞态：任务已被删，直接放弃
        try:
            await self.run_job(job)
        except Exception:  # noqa: BLE001 —— 单次任务失败不能炸掉调度器
            logger.exception("定时任务执行异常: %s(%s)", job.get("name"), job_id)

"""tests/test_scheduler.py —— jarvis/scheduler.py 单元测试（契约 1.9）。

说明：jarvis/db.py 归 Agent A 所有且并行开发中，为保证本测试自包含，
这里用严格按契约 1.4 cron 接口实现的 FakeDB（内存版），不依赖真实 SQLite。
"""
import asyncio
import datetime
import uuid

import pytest

from jarvis.scheduler import JarvisScheduler


def _now() -> str:
    return datetime.datetime.now().astimezone().isoformat()


class FakeDB:
    """按契约 1.4 的 cron 接口实现的内存假数据库（列名与 1.3 schema 一致）。"""

    def __init__(self):
        self.rows: dict[str, dict] = {}

    def create_cron(self, name: str, cron: str, prompt: str) -> dict:
        job = {
            "id": uuid.uuid4().hex,
            "name": name,
            "cron": cron,
            "prompt": prompt,
            "enabled": 1,
            "created_at": _now(),
            "last_run_at": None,
            "last_status": None,
        }
        self.rows[job["id"]] = job
        return dict(job)

    def update_cron(self, job_id: str, **fields) -> dict | None:
        if job_id not in self.rows:
            return None
        self.rows[job_id].update(fields)
        return dict(self.rows[job_id])

    def delete_cron(self, job_id: str) -> bool:
        return self.rows.pop(job_id, None) is not None

    def list_cron(self) -> list[dict]:
        return [dict(j) for j in self.rows.values()]

    def get_cron(self, job_id: str) -> dict | None:
        j = self.rows.get(job_id)
        return dict(j) if j else None

    def mark_cron_run(self, job_id: str, status: str) -> None:
        if job_id in self.rows:
            self.rows[job_id]["last_run_at"] = _now()
            self.rows[job_id]["last_status"] = status


@pytest.fixture
def db():
    return FakeDB()


@pytest.fixture
def run_calls():
    """记录 run_job 被调用的 job dict 列表。"""
    return []


@pytest.fixture
def sched(db, run_calls):
    async def run_job(job: dict) -> None:
        run_calls.append(job)

    s = JarvisScheduler(db, run_job)
    yield s
    s.shutdown()


# ---------- add ----------

def test_add_persists_and_registers(sched, db):
    """add：写库 + 注册 APScheduler 任务，返回完整行 dict。"""
    job = sched.add("早报", "0 8 * * *", "汇总今日新闻")
    assert job["name"] == "早报"
    assert job["cron"] == "0 8 * * *"
    assert job["prompt"] == "汇总今日新闻"
    assert job["enabled"] == 1
    # 落库
    assert db.get_cron(job["id"]) is not None
    # 注册到 APScheduler（未 start 时为 pending，可查）
    apjob = sched.scheduler.get_job(job["id"])
    assert apjob is not None
    assert apjob.misfire_grace_time == 60
    assert apjob.coalesce is True
    assert "hour='8'" in str(apjob.trigger)


def test_add_invalid_cron_raises_and_not_persisted(sched, db):
    """非法 cron 抛 ValueError，且不落库不注册。"""
    with pytest.raises(ValueError):
        sched.add("坏任务", "not a cron", "x")
    assert db.list_cron() == []
    assert sched.scheduler.get_jobs() == []


# ---------- update ----------

def test_update_cron_reregisters(sched, db):
    """update 改 cron：落库 + APScheduler 触发器同步更新。"""
    job = sched.add("早报", "0 8 * * *", "p")
    updated = sched.update(job["id"], cron="30 9 * * *")
    assert updated is not None
    assert updated["cron"] == "30 9 * * *"
    assert db.get_cron(job["id"])["cron"] == "30 9 * * *"
    apjob = sched.scheduler.get_job(job["id"])
    assert apjob is not None
    assert "hour='9'" in str(apjob.trigger)
    assert "minute='30'" in str(apjob.trigger)


def test_update_disable_unregisters(sched, db):
    """enabled=0：从 APScheduler 移除，但保留库记录。"""
    job = sched.add("早报", "0 8 * * *", "p")
    updated = sched.update(job["id"], enabled=0)
    assert updated["enabled"] == 0
    assert sched.scheduler.get_job(job["id"]) is None
    assert db.get_cron(job["id"]) is not None


def test_update_enable_reregisters(sched, db):
    """enabled=0 → 1：重新注册。"""
    job = sched.add("早报", "0 8 * * *", "p")
    sched.update(job["id"], enabled=0)
    updated = sched.update(job["id"], enabled=1)
    assert updated["enabled"] == 1
    assert sched.scheduler.get_job(job["id"]) is not None


def test_update_invalid_cron_raises(sched, db):
    """update 传非法 cron：抛 ValueError，库不变。"""
    job = sched.add("早报", "0 8 * * *", "p")
    with pytest.raises(ValueError):
        sched.update(job["id"], cron="61 25 * * *")
    assert db.get_cron(job["id"])["cron"] == "0 8 * * *"


def test_update_missing_returns_none(sched):
    """update 不存在的 job 返回 None。"""
    assert sched.update("deadbeef", name="x") is None


# ---------- remove ----------

def test_remove_deletes_and_unregisters(sched, db):
    """remove：删库 + 取消注册，返回 True；重复删返回 False。"""
    job = sched.add("早报", "0 8 * * *", "p")
    assert sched.remove(job["id"]) is True
    assert db.get_cron(job["id"]) is None
    assert sched.scheduler.get_job(job["id"]) is None
    assert sched.remove(job["id"]) is False


# ---------- start：从库恢复 ----------

@pytest.mark.asyncio
async def test_start_restores_only_enabled(db, run_calls):
    """start()：仅恢复 enabled=1 的任务，且带 misfire_grace_time=60/coalesce。"""
    on = db.create_cron("开着的", "*/5 * * * *", "p1")
    off = db.create_cron("关着的", "0 0 * * *", "p2")
    db.update_cron(off["id"], enabled=0)

    async def run_job(job):
        run_calls.append(job)

    s = JarvisScheduler(db, run_job)
    try:
        s.start()
        ids = {j.id for j in s.scheduler.get_jobs()}
        assert ids == {on["id"]}
        apjob = s.scheduler.get_job(on["id"])
        assert apjob.misfire_grace_time == 60
        assert apjob.coalesce is True
    finally:
        s.shutdown()


# ---------- trigger_now ----------

@pytest.mark.asyncio
async def test_trigger_now_calls_run_job(sched, run_calls):
    """trigger_now：立即异步执行一次，run_job 收到完整 job dict。"""
    job = sched.add("立即任务", "0 0 1 1 *", "马上干活")
    sched.trigger_now(job["id"])
    await asyncio.sleep(0.05)  # 让 create_task 的协程跑完
    assert len(run_calls) == 1
    assert run_calls[0]["id"] == job["id"]
    assert run_calls[0]["prompt"] == "马上干活"


def test_trigger_now_missing_raises(sched):
    """trigger_now 不存在的 job：抛 ValueError。"""
    with pytest.raises(ValueError):
        sched.trigger_now("deadbeef")


@pytest.mark.asyncio
async def test_run_job_exception_swallowed(db):
    """run_job 抛异常：调度器吞掉并记日志，不向上炸。"""
    async def bad_run_job(job):
        raise RuntimeError("boom")

    s = JarvisScheduler(db, bad_run_job)
    try:
        job = s.add("会炸的", "0 0 1 1 *", "p")
        s.trigger_now(job["id"])  # 不应抛错
        await asyncio.sleep(0.05)
    finally:
        s.shutdown()

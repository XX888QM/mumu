"""Task B 测试：jarvis/engine.py（codex 子进程引擎）。

约定（见实施计划 2 节）：
- 不调真实 codex：事件解析用 FakeProcess 喂 tests/fixtures/codex_events.jsonl；
  超时/取消/非零退出用 monkeypatch 把子进程换成本机 /bin/sh 假进程。
- 命令行组装必须与契约 1.7 完全一致，逐参数断言。
"""
import asyncio
import json
from pathlib import Path

import pytest

from jarvis.config import settings
from jarvis.engine import CodexEngine, EngineError, EngineResult, EngineTimeout

FIXTURE = Path(__file__).parent / "fixtures" / "codex_events.jsonl"


# ---------------------------------------------------------------------------
# 假进程工具（不碰真实 codex）
# ---------------------------------------------------------------------------

class _FakeStdout:
    """模拟 asyncio 子进程 stdout：逐行吐出预置字节行，吐完返回 b'' (EOF)。"""

    def __init__(self, lines: list[bytes]):
        self._lines = list(lines)

    async def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        return b""


class _FakeStderr:
    """模拟 asyncio 子进程 stderr：一次性返回全部数据，之后 EOF。"""

    def __init__(self, data: bytes = b""):
        self._data = data

    async def read(self, n: int = -1) -> bytes:
        data, self._data = self._data, b""
        return data


class FakeProcess:
    """模拟 asyncio.subprocess.Process，行为足够引擎消费。"""

    def __init__(self, stdout_lines: list[bytes], returncode: int = 0,
                 stderr: bytes = b""):
        self.stdout = _FakeStdout(stdout_lines)
        self.stderr = _FakeStderr(stderr)
        self.returncode: int | None = None
        self._rc = returncode
        self.terminate_called = False
        self.kill_called = False

    async def wait(self) -> int:
        self.returncode = -15 if self.terminate_called else self._rc
        return self.returncode

    def terminate(self) -> None:
        self.terminate_called = True

    def kill(self) -> None:
        self.kill_called = True


def _fixture_lines() -> list[bytes]:
    return [line + b"\n" for line in FIXTURE.read_bytes().splitlines()]


def _patch_spawn(monkeypatch, factory):
    """把 asyncio.create_subprocess_exec 换成 factory(*cmd, **kw)，并记录 cmd。"""
    captured: list[list[str]] = []

    async def fake_exec(*cmd, **kwargs):
        captured.append(list(cmd))
        return await factory(*cmd, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return captured


def _patch_spawn_fake(monkeypatch, proc: FakeProcess):
    """spawn 返回指定 FakeProcess。"""
    async def factory(*cmd, **kwargs):
        return proc

    return _patch_spawn(monkeypatch, factory)


def _patch_spawn_shell(monkeypatch, script: str):
    """spawn 忽略原命令，改起真实 /bin/sh 假进程（用于超时/取消/退出码路径）。"""
    real_exec = asyncio.create_subprocess_exec

    async def factory(*cmd, **kwargs):
        return await real_exec("/bin/sh", "-c", script, **kwargs)

    return _patch_spawn(monkeypatch, factory)


def _expected_cmd(task_id: str) -> list[str]:
    """契约 1.7 锁定的命令行（公共前缀部分），逐字面量照抄。"""
    return [
        settings.codex_bin, "exec", "--json", "--ignore-user-config",
        "-C", settings.workspace, "--skip-git-repo-check",
        "-m", settings.jarvis_model, "-s", settings.jarvis_sandbox,
        "-c", f'model_reasoning_effort="{settings.jarvis_reasoning}"',
        "-c", f'mcp_servers.jarvis.command="{settings.venv_py}"',
        "-c", f'mcp_servers.jarvis.args=["{settings.jarvis_root}/jarvis/mcp_server.py"]',
        "-c", ('mcp_servers.jarvis.env={ JARVIS_URL = "http://127.0.0.1:%d", '
               'JARVIS_TOKEN = "%s", JARVIS_TASK_ID = "%s" }'
               % (settings.jarvis_port, settings.jarvis_token, task_id)),
    ]


# ---------------------------------------------------------------------------
# 1.7 命令行组装（逐参数断言）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cmd_new_session(monkeypatch):
    """新会话：公共前缀 + [prompt]，每个参数与契约 1.7 一字不差。"""
    captured = _patch_spawn_fake(monkeypatch, FakeProcess(_fixture_lines()))
    engine = CodexEngine()
    await engine.run("你好贾维斯", task_id="task-abc")

    assert len(captured) == 1
    cmd = captured[0]
    expected = _expected_cmd("task-abc") + ["你好贾维斯"]
    assert len(cmd) == len(expected)
    for i, (got, want) in enumerate(zip(cmd, expected)):
        assert got == want, f"cmd[{i}] 不符契约: {got!r} != {want!r}"


@pytest.mark.asyncio
async def test_cmd_resume_session(monkeypatch):
    """续会话：全局参数必须在 resume 子命令之前，结尾为 resume <thread_id> <prompt>。"""
    captured = _patch_spawn_fake(monkeypatch, FakeProcess(_fixture_lines()))
    engine = CodexEngine()
    await engine.run("继续干活", task_id="task-xyz",
                     thread_id="019eb332-306a-7ca3-b792-434c9f1351d7")

    cmd = captured[0]
    expected = _expected_cmd("task-xyz") + [
        "resume", "019eb332-306a-7ca3-b792-434c9f1351d7", "继续干活",
    ]
    assert len(cmd) == len(expected)
    for i, (got, want) in enumerate(zip(cmd, expected)):
        assert got == want, f"cmd[{i}] 不符契约: {got!r} != {want!r}"
    # 再显式确认 resume 在所有 -c 全局参数之后
    assert cmd[-3:] == ["resume", "019eb332-306a-7ca3-b792-434c9f1351d7", "继续干活"]


# ---------------------------------------------------------------------------
# 事件流解析（喂 fixtures 样本）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_parses_fixture_events(monkeypatch):
    """fixtures 8 行：7 行 JSON 全部回调（含未知类型原样透传），1 行噪音忽略；
    EngineResult 的 thread_id / final_message / usage 与样本一致。"""
    received: list[tuple[str, dict]] = []

    async def on_event(task_id: str, event: dict) -> None:
        received.append((task_id, event))

    _patch_spawn_fake(monkeypatch, FakeProcess(_fixture_lines()))
    engine = CodexEngine(on_event=on_event)
    result = engine_result = await engine.run("测试", task_id="t1")

    # 噪音行（非 JSON）被忽略：8 行只回调 7 次
    assert len(received) == 7
    assert all(tid == "t1" for tid, _ in received)

    # 回调内容与 fixture 中 JSON 行逐条一致（顺序保持）
    expected_events = []
    for raw in FIXTURE.read_text(encoding="utf-8").splitlines():
        try:
            expected_events.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    assert [ev for _, ev in received] == expected_events

    # 未知 item 类型必须原样透传
    unknown = [ev for _, ev in received
               if ev.get("item", {}).get("type") == "some_future_unknown_type"]
    assert len(unknown) == 1
    assert unknown[0]["item"]["data"] == "parser must pass this through"

    # EngineResult 三字段
    assert isinstance(engine_result, EngineResult)
    assert result.thread_id == "019eb332-306a-7ca3-b792-434c9f1351d7"
    assert result.final_message == "大哥，上一条我回复的是：`在线`\n\n命令输出是：`JARVIS_TEST`"
    assert result.usage == {
        "input_tokens": 95260,
        "cached_input_tokens": 41088,
        "output_tokens": 358,
        "reasoning_output_tokens": 266,
    }


@pytest.mark.asyncio
async def test_run_without_on_event(monkeypatch):
    """on_event=None 时不回调也不崩。"""
    _patch_spawn_fake(monkeypatch, FakeProcess(_fixture_lines()))
    engine = CodexEngine()
    result = await engine.run("测试", task_id="t2")
    assert result.thread_id == "019eb332-306a-7ca3-b792-434c9f1351d7"


@pytest.mark.asyncio
async def test_run_no_agent_message_no_usage(monkeypatch):
    """无 agent_message / turn.completed 时：final_message 为空串、usage 为空 dict。"""
    lines = [
        b'{"type":"thread.started","thread_id":"th-1"}\n',
        b'{"type":"turn.started"}\n',
        b'not json at all\n',
    ]
    _patch_spawn_fake(monkeypatch, FakeProcess(lines))
    engine = CodexEngine()
    result = await engine.run("测试", task_id="t3")
    assert result.thread_id == "th-1"
    assert result.final_message == ""
    assert result.usage == {}


# ---------------------------------------------------------------------------
# 失败路径
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_nonzero_exit_raises_engine_error(monkeypatch):
    """退出码非 0 → EngineError，消息携带 stderr 内容（真实 sh 假进程验证）。"""
    _patch_spawn_shell(
        monkeypatch,
        'echo \'{"type":"turn.started"}\'; echo "FATAL: codex boom" 1>&2; exit 7',
    )
    engine = CodexEngine()
    with pytest.raises(EngineError) as exc_info:
        await engine.run("测试", task_id="t4")
    assert "FATAL: codex boom" in str(exc_info.value)


@pytest.mark.asyncio
async def test_engine_error_stderr_tail_500(monkeypatch):
    """EngineError 只带 stderr 尾部 500 字：开头被截掉、结尾保留。"""
    stderr = ("HEAD_MARKER_" + "x" * 600 + "_TAIL_MARKER").encode()
    _patch_spawn_fake(monkeypatch, FakeProcess([], returncode=3, stderr=stderr))
    engine = CodexEngine()
    with pytest.raises(EngineError) as exc_info:
        await engine.run("测试", task_id="t5")
    msg = str(exc_info.value)
    assert "_TAIL_MARKER" in msg
    assert "HEAD_MARKER_" not in msg


@pytest.mark.asyncio
async def test_timeout_raises_engine_timeout(monkeypatch):
    """超时路径：sleep 假进程 + 0.3s 超时 → EngineTimeout，子进程被 terminate。"""
    procs: list = []
    real_exec = asyncio.create_subprocess_exec

    async def factory(*cmd, **kwargs):
        proc = await real_exec("/bin/sh", "-c", "sleep 30", **kwargs)
        procs.append(proc)
        return proc

    _patch_spawn(monkeypatch, factory)
    engine = CodexEngine()
    with pytest.raises(EngineTimeout):
        await engine.run("测试", task_id="t6", timeout=0.3)
    # 子进程必须已被终止（returncode 已落定，非 None）
    assert procs[0].returncode is not None


def test_engine_timeout_is_engine_error():
    """契约 1.6：EngineTimeout 是 EngineError 的子类。"""
    assert issubclass(EngineTimeout, EngineError)
    assert issubclass(EngineError, Exception)


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancel_terminates_process(monkeypatch):
    """cancel(task_id) terminate 对应子进程：run 因非零退出抛 EngineError。"""
    _patch_spawn_shell(monkeypatch, "sleep 30")
    engine = CodexEngine()
    run_task = asyncio.create_task(engine.run("测试", task_id="t7"))
    await asyncio.sleep(0.3)  # 等子进程起来

    assert engine.cancel("t7") is True
    with pytest.raises(EngineError):
        await asyncio.wait_for(run_task, 10)


@pytest.mark.asyncio
async def test_cancel_unknown_task_returns_false():
    """没有对应子进程 → False。"""
    engine = CodexEngine()
    assert engine.cancel("no-such-task") is False


@pytest.mark.asyncio
async def test_cancel_after_finish_returns_false(monkeypatch):
    """任务已正常结束后 cancel → False（进程表已清理）。"""
    _patch_spawn_fake(monkeypatch, FakeProcess(_fixture_lines()))
    engine = CodexEngine()
    await engine.run("测试", task_id="t8")
    assert engine.cancel("t8") is False

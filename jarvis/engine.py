"""codex 子进程引擎（契约 1.6 / 1.7 锁定）。

职责：组装 codex exec 命令行 → spawn 子进程 → 逐行解析 stdout JSONL 事件
（噪音行/未知类型容忍）→ 汇总 EngineResult；支持超时终止与按 task_id 取消。
只解析 stdout；stderr 仅在非零退出时取尾部 500 字拼入异常。
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
from dataclasses import dataclass, field

from jarvis.config import settings

# 子进程单行读取上限（aggregated_output 可能很大，默认 64KB 不够）
_STREAM_LIMIT = 10 * 1024 * 1024
# terminate 后的宽限期，超过则 kill（契约 1.6 锁定为 5 秒）
_KILL_GRACE = 5.0


class EngineError(Exception):
    """引擎执行失败（子进程非零退出等）。"""


class EngineTimeout(EngineError):
    """引擎执行超时（子进程已被强制终止）。"""


@dataclass
class EngineResult:
    thread_id: str | None
    final_message: str
    usage: dict = field(default_factory=dict)  # turn.completed 的 usage，可能为空 dict


class CodexEngine:
    def __init__(self, on_event=None):
        # on_event(task_id: str, event: dict)：每解析到一行 JSONL 调用（异步回调）
        self.on_event = on_event
        # task_id -> 运行中的子进程（结束后立即清理，供 cancel 查找）
        self._procs: dict[str, asyncio.subprocess.Process] = {}

    # ------------------------------------------------------------------
    # 命令行组装（契约 1.7，一字不差）
    # ------------------------------------------------------------------
    def build_command(self, prompt: str, task_id: str,
                      thread_id: str | None = None) -> list[str]:
        cmd = [
            settings.codex_bin, "exec", "--json", "--ignore-user-config",
            "-C", settings.workspace, "--skip-git-repo-check",
            "-m", settings.jarvis_model, "-s", settings.jarvis_sandbox,
            "-c", f'model_reasoning_effort="{settings.jarvis_reasoning}"',
            "-c", f'mcp_servers.jarvis.command="{settings.venv_py}"',
            "-c", f'mcp_servers.jarvis.args=["{settings.jarvis_root}/jarvis/mcp_server.py"]',
            # token 不进 argv（ps 可见）：MCP 桥按 JARVIS_RUNTIME 指向的 0600 文件取 url/token
            "-c", ('mcp_servers.jarvis.env={ JARVIS_RUNTIME = "%s", JARVIS_TASK_ID = "%s" }'
                   % (settings.runtime_file, task_id)),
        ]
        if thread_id:
            # 全局参数必须在 resume 子命令之前
            cmd += ["resume", thread_id, prompt]
        else:
            cmd += [prompt]
        return cmd

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------
    async def run(self, prompt: str, task_id: str, thread_id: str | None = None,
                  timeout: float | None = None) -> EngineResult:
        cmd = self.build_command(prompt, task_id, thread_id)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_STREAM_LIMIT,
        )
        self._procs[task_id] = proc

        # 解析状态（_consume_stdout 内更新）
        result_thread_id: str | None = None
        final_message = ""
        usage: dict = {}
        effective_timeout = timeout if timeout is not None else settings.jarvis_task_timeout

        async def _drain_stderr() -> bytes:
            """并发吸干 stderr，防止管道写满阻塞子进程。"""
            chunks: list[bytes] = []
            while True:
                chunk = await proc.stderr.read(65536)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)

        async def _consume_stdout() -> None:
            nonlocal result_thread_id, final_message, usage
            while True:
                line = await proc.stdout.readline()
                if not line:
                    return  # EOF：子进程已关 stdout
                try:
                    event = json.loads(line.decode("utf-8", "replace"))
                except (json.JSONDecodeError, ValueError):
                    continue  # 非 JSON 噪音行，忽略
                if not isinstance(event, dict):
                    continue  # JSON 但不是事件对象（如裸数字），忽略
                # 每行事件回调（异步回调；未知类型原样透传）
                if self.on_event is not None:
                    ret = self.on_event(task_id, event)
                    if inspect.isawaitable(ret):
                        await ret
                etype = event.get("type")
                if etype == "thread.started":
                    result_thread_id = event.get("thread_id")
                elif etype == "item.completed":
                    item = event.get("item") or {}
                    if item.get("type") == "agent_message":
                        # 收集最后一条 agent_message 文本
                        final_message = item.get("text") or ""
                elif etype == "turn.completed":
                    usage = event.get("usage") or {}

        async def _consume_and_wait() -> int:
            await _consume_stdout()
            return await proc.wait()

        stderr_task = asyncio.create_task(_drain_stderr())
        stderr_data = b""
        try:
            returncode = await asyncio.wait_for(_consume_and_wait(), effective_timeout)
        except (asyncio.TimeoutError, TimeoutError):
            await self._terminate(proc)
            raise EngineTimeout(
                f"任务 {task_id} 超时（{effective_timeout} 秒），子进程已终止"
            ) from None
        except BaseException:
            # 任何意外（含外层 task 被 cancel）都不留僵尸子进程
            await self._terminate(proc)
            raise
        finally:
            self._procs.pop(task_id, None)
            # 任何退出路径都回收 stderr_task（不收会在 GC 时报
            # "Task was destroyed but it is pending!"）。此时进程已退出或已被
            # _terminate 杀死，正常应秒到 EOF；宽限 _KILL_GRACE 后强制 cancel。
            try:
                stderr_data = await asyncio.wait_for(stderr_task, _KILL_GRACE)
            except BaseException:
                stderr_task.cancel()
                with contextlib.suppress(BaseException):
                    await stderr_task

        if returncode != 0:
            tail = stderr_data.decode("utf-8", "replace")[-500:]
            raise EngineError(
                f"codex 子进程退出码 {returncode}（任务 {task_id}）：{tail}"
            )
        return EngineResult(thread_id=result_thread_id,
                            final_message=final_message, usage=usage)

    # ------------------------------------------------------------------
    # 取消
    # ------------------------------------------------------------------
    def cancel(self, task_id: str) -> bool:
        """terminate 对应子进程；无此任务或进程已结束返回 False。"""
        proc = self._procs.get(task_id)
        if proc is None or proc.returncode is not None:
            return False
        try:
            proc.terminate()
        except ProcessLookupError:
            return False
        return True

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    @staticmethod
    async def _terminate(proc) -> None:
        """terminate 子进程，宽限 5 秒仍不退则 kill。"""
        if proc.returncode is not None:
            return
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), _KILL_GRACE)
        except (asyncio.TimeoutError, TimeoutError):
            try:
                proc.kill()
            except ProcessLookupError:
                return
            await proc.wait()

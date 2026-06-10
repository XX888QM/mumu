"""贾维斯 MCP 工具桥（独立 stdio 进程）。

由 codex 引擎按契约 1.7 以子进程方式拉起（FastMCP over stdio），
赋予模型四种主动能力：申请授权 / 推送到大哥 iPhone / 创建定时任务 / 写长期记忆。

硬性约束（契约 1.10）：
- 只从环境变量取配置：JARVIS_URL / JARVIS_TOKEN / JARVIS_TASK_ID（引擎注入），
  APPROVAL_TIMEOUT 可选（默认 1800 秒）。
- 禁止 import jarvis 包其他模块——本文件必须能以脚本路径独立运行。
- 用 httpx 同步客户端回调贾维斯服务，Header: Authorization: Bearer $JARVIS_TOKEN。
"""

import os
import time

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("jarvis")

# 授权轮询间隔（秒）；测试会打补丁缩短
POLL_INTERVAL = 2.0
# 授权等待默认超时（秒），可被环境变量 APPROVAL_TIMEOUT 覆盖
DEFAULT_APPROVAL_TIMEOUT = 1800.0

# 测试钩子：注入 httpx.MockTransport；生产保持 None 走真实网络
_transport: httpx.BaseTransport | None = None


def _env(name: str) -> str:
    """读取必需环境变量，缺失时报清晰错误（点名缺哪个变量）。"""
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少环境变量 {name}：MCP 工具桥必须由贾维斯引擎注入 {name} 后启动")
    return value


def _approval_timeout() -> float:
    """授权等待超时秒数：环境变量 APPROVAL_TIMEOUT，缺省 1800。"""
    raw = os.environ.get("APPROVAL_TIMEOUT", "").strip()
    if not raw:
        return DEFAULT_APPROVAL_TIMEOUT
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_APPROVAL_TIMEOUT


def _client() -> httpx.Client:
    """构造指向贾维斯服务的同步 httpx 客户端（带 Bearer 认证头）。"""
    return httpx.Client(
        base_url=_env("JARVIS_URL"),
        headers={"Authorization": f"Bearer {_env('JARVIS_TOKEN')}"},
        timeout=15.0,
        transport=_transport,
    )


@mcp.tool()
def request_approval(action: str, detail: str, risk_level: str = "high") -> str:
    """高危操作前必须调用此工具申请大哥批准。action=一句话动作名，detail=精确清单(做什么/影响什么)，
    risk_level=high|critical。返回 approved/denied/expired，非 approved 时禁止执行该操作。"""
    timeout = _approval_timeout()
    task_id = _env("JARVIS_TASK_ID")
    with _client() as client:
        # 1) 创建审批单
        resp = client.post(
            "/api/internal/approvals",
            json={"task_id": task_id, "action": action, "detail": detail, "risk_level": risk_level},
        )
        resp.raise_for_status()
        approval_id = resp.json()["approval_id"]
        # 2) 每 POLL_INTERVAL 秒轮询一次，直到非 pending 或超时
        deadline = time.monotonic() + timeout
        while True:
            time.sleep(POLL_INTERVAL)
            poll = client.get(f"/api/approvals/{approval_id}")
            poll.raise_for_status()
            status = poll.json().get("status", "pending")
            if status != "pending":
                return status
            if time.monotonic() >= deadline:
                return "expired"


@mcp.tool()
def notify(title: str, body: str) -> str:
    """主动推送消息到大哥的 iPhone（Bark）。任务完成汇报、重要发现、需要大哥注意的事用这个。"""
    with _client() as client:
        resp = client.post("/api/internal/notify", json={"title": title, "body": body})
        if resp.status_code >= 400:
            return "failed"
        return "ok" if resp.json().get("ok") else "failed"


@mcp.tool()
def schedule_task(name: str, cron: str, prompt: str) -> str:
    """创建定时任务。cron=标准5段表达式(分 时 日 月 周)，prompt=到点时交给贾维斯执行的完整指令。"""
    with _client() as client:
        resp = client.post("/api/internal/schedule", json={"name": name, "cron": cron, "prompt": prompt})
        resp.raise_for_status()
        return str(resp.json()["id"])


@mcp.tool()
def remember(content: str) -> str:
    """写入长期记忆(跨会话不忘)。大哥的偏好、重要事实、未完成事项都应记录。"""
    with _client() as client:
        resp = client.post("/api/internal/remember", json={"content": content})
        resp.raise_for_status()
        return "ok"


if __name__ == "__main__":
    # stdio 传输：由 codex 以子进程拉起，走标准输入输出通信
    mcp.run()

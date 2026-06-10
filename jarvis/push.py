"""jarvis/push.py —— Bark iPhone 推送（契约 1.8）。

- BARK_KEY 为空：降级为日志记录并返回 False（不抛错）。
- 任何 HTTP/网络失败：logging.warning 并返回 False（不抛错）。
推送失败永远不能影响主流程。
"""
import logging

import httpx

from jarvis.config import settings

logger = logging.getLogger("jarvis.push")

# 测试钩子：单测注入 httpx.MockTransport；生产环境保持 None（走真实网络）
_transport: httpx.AsyncBaseTransport | None = None


async def bark_push(title: str, body: str, url: str | None = None, level: str = "active") -> bool:
    """推送一条消息到大哥的 iPhone（Bark）。

    返回 True=推送成功；False=未配置 key（降级为日志）或推送失败。
    """
    if not settings.bark_key:
        # 降级：没有 BARK_KEY 时只记日志，方便集成阶段在服务日志里确认
        logger.info("BARK_KEY 未配置，推送降级为日志: [%s] %s", title, body)
        return False

    payload = {
        "device_key": settings.bark_key,
        "title": title,
        "body": body,
        "level": level,
        "group": "jarvis",
        **({"url": url} if url else {}),
    }
    try:
        async with httpx.AsyncClient(timeout=10, transport=_transport) as client:
            resp = await client.post(f"{settings.bark_server}/push", json=payload)
            resp.raise_for_status()
            return True
    except Exception as exc:  # noqa: BLE001 —— 契约要求任何失败都不抛错
        logger.warning("Bark 推送失败: [%s] %s -> %r", title, body, exc)
        return False

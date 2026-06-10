"""tests/test_push.py —— jarvis/push.py 单元测试（契约 1.8）。

约定：Bark 推送一律走 httpx.MockTransport，绝不真实外发。
"""
import json
import logging

import httpx
import pytest

from jarvis import push
from jarvis.config import settings


class Recorder:
    """记录 MockTransport 收到的全部请求，并按配置返回响应/抛异常。"""

    def __init__(self, response: httpx.Response | None = None, exc: Exception | None = None):
        self.requests: list[httpx.Request] = []
        self.response = response if response is not None else httpx.Response(200, json={"code": 200})
        self.exc = exc

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.exc is not None:
            raise self.exc
        return self.response


@pytest.fixture
def recorder(monkeypatch):
    """默认成功响应的 MockTransport，注入 push._transport 测试钩子。"""
    rec = Recorder()
    monkeypatch.setattr(push, "_transport", httpx.MockTransport(rec.handler))
    # 固定 bark 配置，避免读到真实 .env 的值
    monkeypatch.setattr(settings, "bark_key", "testkey0001")
    monkeypatch.setattr(settings, "bark_server", "https://bark.test")
    return rec


@pytest.mark.asyncio
async def test_no_key_returns_false_and_logs(monkeypatch, caplog):
    """BARK_KEY 为空：降级为日志、返回 False、绝不发起 HTTP 请求。"""
    rec = Recorder()
    monkeypatch.setattr(push, "_transport", httpx.MockTransport(rec.handler))
    monkeypatch.setattr(settings, "bark_key", "")
    with caplog.at_level(logging.INFO, logger="jarvis.push"):
        ok = await push.bark_push("测试标题", "测试内容")
    assert ok is False
    assert rec.requests == []  # 没有任何真实请求
    assert "测试标题" in caplog.text  # 降级日志里能看到推送内容


@pytest.mark.asyncio
async def test_push_payload_correct(recorder):
    """有 key 时 POST {BARK_SERVER}/push，payload 字段与契约 1.8 完全一致。"""
    ok = await push.bark_push("标题A", "内容B")
    assert ok is True
    assert len(recorder.requests) == 1
    req = recorder.requests[0]
    assert req.method == "POST"
    assert str(req.url) == "https://bark.test/push"
    body = json.loads(req.content)
    assert body == {
        "device_key": "testkey0001",
        "title": "标题A",
        "body": "内容B",
        "level": "active",
        "group": "jarvis",
    }  # url=None 时不得出现 url 字段


@pytest.mark.asyncio
async def test_push_with_url_and_level(recorder):
    """显式传 url 和 level 时进入 payload。"""
    ok = await push.bark_push("t", "b", url="https://example.com/x", level="timeSensitive")
    assert ok is True
    body = json.loads(recorder.requests[0].content)
    assert body["url"] == "https://example.com/x"
    assert body["level"] == "timeSensitive"


@pytest.mark.asyncio
async def test_push_http_error_returns_false(monkeypatch, caplog):
    """HTTP 非 2xx：logging.warning 并返回 False，不抛错。"""
    rec = Recorder(response=httpx.Response(500, text="boom"))
    monkeypatch.setattr(push, "_transport", httpx.MockTransport(rec.handler))
    monkeypatch.setattr(settings, "bark_key", "testkey0001")
    monkeypatch.setattr(settings, "bark_server", "https://bark.test")
    with caplog.at_level(logging.WARNING, logger="jarvis.push"):
        ok = await push.bark_push("t", "b")
    assert ok is False
    assert len(rec.requests) == 1
    assert "Bark" in caplog.text


@pytest.mark.asyncio
async def test_push_network_error_returns_false(monkeypatch):
    """网络异常（连接失败等）：返回 False，不抛错。"""
    rec = Recorder(exc=httpx.ConnectError("connection refused"))
    monkeypatch.setattr(push, "_transport", httpx.MockTransport(rec.handler))
    monkeypatch.setattr(settings, "bark_key", "testkey0001")
    monkeypatch.setattr(settings, "bark_server", "https://bark.test")
    ok = await push.bark_push("t", "b")
    assert ok is False

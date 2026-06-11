"""对 jarvis-server 的轻量客户端（REST + WS），契约见 Phase 2 计划 1.5 节。

运行环境为 .venv-voice（无 python-dotenv，不 import jarvis.config）：
base_url / token 由调用方（daemon.py）解析 .env 后显式传入。

固定语音会话策略（契约：首次按 title="语音会话" 建/找）：
- Phase 1 REST 没有"按标题建会话"端点（POST /api/chat 无 session_id 时
  标题取 message[:30]），故采用三级定位：
  ① 本地持久化文件 data/voice_session.json 里的 session_id（重启稳定）；
  ② GET /api/sessions 按 title=="语音会话" 找；
  ③ 都没有 → 首条 chat 不带 session_id 新建会话，并把返回的 id 持久化。
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

import httpx

logger = logging.getLogger("voice.client")

VOICE_SESSION_TITLE = "语音会话"
_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SESSION_FILE = _ROOT / "data" / "voice_session.json"


class JarvisClient:
    """jarvis-server 客户端：chat / decide / tts / WS listen。"""

    def __init__(self, base_url: str, token: str, session_file=None):
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._http = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(30.0),
        )
        self._session_id: str | None = None
        self._session_file = Path(session_file) if session_file else DEFAULT_SESSION_FILE
        self._stop = threading.Event()
        self._ws_thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # 固定语音会话
    # ------------------------------------------------------------------

    @property
    def session_id(self) -> str | None:
        return self._session_id

    def _load_persisted_session(self) -> str | None:
        try:
            data = json.loads(self._session_file.read_text(encoding="utf-8"))
            return data.get("session_id") or None
        except (OSError, ValueError):
            return None

    def _persist_session(self, session_id: str) -> None:
        try:
            self._session_file.parent.mkdir(parents=True, exist_ok=True)
            self._session_file.write_text(
                json.dumps({"session_id": session_id}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            logger.warning("语音会话 id 持久化失败：%s", self._session_file)

    def _find_voice_session(self) -> str | None:
        """找固定语音会话：本地持久化 id 优先，其次按标题匹配。"""
        sessions = self._http.get("/api/sessions").json()
        ids = {s.get("id") for s in sessions}
        persisted = self._load_persisted_session()
        if persisted and persisted in ids:
            return persisted
        for s in sessions:
            if s.get("title") == VOICE_SESSION_TITLE:
                return s.get("id")
        return None

    # ------------------------------------------------------------------
    # REST
    # ------------------------------------------------------------------

    def chat(self, message: str) -> dict:
        """发消息到固定语音会话；server busy(409) 时返回 {"busy": True}。"""
        if self._session_id is None:
            try:
                self._session_id = self._find_voice_session()
            except httpx.HTTPError as exc:
                logger.warning("查找语音会话失败（将新建）：%s", exc)
        payload: dict = {"message": message}
        if self._session_id:
            payload["session_id"] = self._session_id
        resp = self._http.post("/api/chat", json=payload)
        if resp.status_code == 409:
            return {"busy": True}
        if resp.status_code == 404 and self._session_id:
            # 本地记录的会话已不存在 → 丢弃缓存重建
            logger.warning("语音会话 %s 已失效，新建会话", self._session_id)
            self._session_id = None
            resp = self._http.post("/api/chat", json={"message": message})
            if resp.status_code == 409:
                return {"busy": True}
        resp.raise_for_status()
        data = resp.json()
        sid = data.get("session_id")
        if sid and sid != self._session_id:
            self._session_id = sid
            self._persist_session(sid)
        return data

    def decide(self, approval_id: str, decision: str) -> bool:
        """决定授权：decision in ('approved','denied')；非 pending(409)/不存在(404) 返回 False。"""
        resp = self._http.post(
            f"/api/approvals/{approval_id}/decide", json={"decision": decision}
        )
        if resp.status_code != 200:
            logger.warning("授权决定失败 %s %s：HTTP %s", approval_id, decision, resp.status_code)
            return False
        return True

    def tts(self, text: str) -> bytes:
        """POST /api/voice/tts → wav 字节。合成慢（暖机后短句约 4.5s），超时放宽到 120s。"""
        resp = self._http.post(
            "/api/voice/tts", json={"text": text}, timeout=httpx.Timeout(120.0)
        )
        resp.raise_for_status()
        return resp.content

    # ------------------------------------------------------------------
    # WebSocket（后台线程；首消息 auth；断线 3s 重连）
    # ------------------------------------------------------------------

    def listen(self, on_event) -> None:
        """起后台线程跑 WS；on_event(msg_dict) 在该线程回调（调用方自行入队）。"""
        if self._ws_thread and self._ws_thread.is_alive():
            return
        self._stop.clear()
        self._ws_thread = threading.Thread(
            target=self._ws_loop, args=(on_event,), daemon=True, name="jarvis-ws"
        )
        self._ws_thread.start()

    def _ws_url(self) -> str:
        scheme = "wss" if self.base_url.startswith("https") else "ws"
        return scheme + "://" + self.base_url.split("://", 1)[1] + "/ws"

    def _ws_loop(self, on_event) -> None:
        # 延迟导入：仅 WS 路径需要 websockets
        from websockets.sync.client import connect

        while not self._stop.is_set():
            try:
                with connect(self._ws_url(), open_timeout=10) as ws:
                    # 契约 1.12：连上后首条消息 auth，token 不走 URL query
                    ws.send(json.dumps({"type": "auth", "token": self._token}))
                    for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except ValueError:
                            continue
                        try:
                            on_event(msg)
                        except Exception:                      # noqa: BLE001
                            logger.exception("WS 事件回调异常：%s", msg)
            except Exception as exc:                           # noqa: BLE001
                logger.warning("WS 连接断开：%s（3 秒后重连）", exc)
            if self._stop.wait(3.0):                           # 契约：断线 3s 重连
                break

    def close(self) -> None:
        """停止 WS 重连线程并关闭 HTTP 连接池。"""
        self._stop.set()
        self._http.close()

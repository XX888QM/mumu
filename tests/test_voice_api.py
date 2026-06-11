"""V3 测试：server 语音端点 + voice/tts_worker handler + web 语音增量。

契约依据：Phase 2 实施计划 1.3（tts_worker）/ 1.6（voice 端点与网页）；测试要点见第 2 节 V3。
纪律：不加载真 whisper / 真 IndexTTS2 模型——
  - /api/voice/transcribe：monkeypatch 伪 whisper 单例，断言透传；
  - /api/voice/tts：httpx MockTransport 断言代理转发与 503/502 路径；
  - tts_worker：不起 subprocess（真模型加载 ~18.5s 留给集成），handler 抽成的
    纯函数注入假合成引擎测 JSON / 白名单 / 错误路径；
  - web：node --check 语法校验 + DOM id 交叉校验 + summarize() 行为（node 沙箱执行）。
跑法：`.venv/bin/python -m pytest tests/test_voice_api.py -v`
"""
import ast
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from jarvis.config import settings
# 复用 Task E 测试的 Fake 组件（签名与 Phase 1 契约 1.4/1.5/1.6/1.9 锁定一致），不重复造轮子
from tests.test_api import (H, TOKEN, FakeApprovalGateway, FakeDatabase,
                            FakeEngine, FakeScheduler)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKER_PATH = PROJECT_ROOT / "voice" / "tts_worker.py"
WEB_DIR = PROJECT_ROOT / "web"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch, tmp_path):
    """预注入 Fake 组件后启动 TestClient（触发 lifespan），与 test_api.make_client 同构。"""
    import jarvis.server as server_mod
    db = FakeDatabase()
    monkeypatch.setattr(settings, "jarvis_token", TOKEN)
    monkeypatch.setattr(settings, "workspace", str(tmp_path / "workspace"))
    app = server_mod.app
    app.state.db = db
    app.state.engine = FakeEngine()
    app.state.scheduler = FakeScheduler(db)
    app.state.gateway = FakeApprovalGateway(db)
    c = TestClient(app)
    c.__enter__()
    yield c
    c.__exit__(None, None, None)
    for attr in ("db", "engine", "scheduler", "gateway"):
        setattr(app.state, attr, None)


class FakeWhisper:
    """伪 faster-whisper 模型：记录调用，返回固定分段。"""

    def __init__(self, texts=("大哥", "，现在几点了")):
        self.calls = []
        self.texts = texts

    def transcribe(self, audio, language=None, **kwargs):
        data = audio.read() if hasattr(audio, "read") else audio
        self.calls.append({"data": data, "language": language})
        segments = iter(SimpleNamespace(text=t) for t in self.texts)
        return segments, SimpleNamespace(language=language)


# ---------------------------------------------------------------------------
# /api/voice/transcribe（契约 1.6）
# ---------------------------------------------------------------------------

def test_transcribe_requires_auth(client):
    r = client.post("/api/voice/transcribe")
    assert r.status_code == 401
    assert r.json()["detail"] == "unauthorized"


def test_transcribe_passthrough(client, monkeypatch):
    """伪 whisper 单例：固定文本透传为 {"text": ...}；字节与 language=zh 正确传入。"""
    import jarvis.server as server_mod
    fake = FakeWhisper()
    monkeypatch.setattr(server_mod, "_get_whisper", lambda: fake)
    payload = b"\x1aE\xdf\xa3fake-webm-bytes"
    r = client.post("/api/voice/transcribe", headers=H,
                    files={"file": ("speech.webm", payload, "audio/webm")})
    assert r.status_code == 200
    assert r.json() == {"text": "大哥，现在几点了"}
    assert len(fake.calls) == 1
    assert fake.calls[0]["data"] == payload      # 上传字节原样进模型
    assert fake.calls[0]["language"] == "zh"     # 中文识别（与 daemon 规则一致）


def test_transcribe_missing_file_422(client):
    r = client.post("/api/voice/transcribe", headers=H)
    assert r.status_code == 422


def test_transcribe_empty_file_422(client, monkeypatch):
    import jarvis.server as server_mod
    fake = FakeWhisper()
    monkeypatch.setattr(server_mod, "_get_whisper", lambda: fake)
    r = client.post("/api/voice/transcribe", headers=H,
                    files={"file": ("speech.webm", b"", "audio/webm")})
    assert r.status_code == 422
    assert fake.calls == []  # 空音频不进模型


def test_transcribe_too_large_413(client, monkeypatch):
    """审查修复（high）：上传音频超过 MAX_AUDIO_BYTES → 413，不进内存/模型（防 OOM DoS）。"""
    import jarvis.server as server_mod
    fake = FakeWhisper()
    monkeypatch.setattr(server_mod, "_get_whisper", lambda: fake)
    monkeypatch.setattr(server_mod, "MAX_AUDIO_BYTES", 1024)   # 缩小阈值便于测试
    r = client.post("/api/voice/transcribe", headers=H,
                    files={"file": ("big.webm", b"x" * 2048, "audio/webm")})
    assert r.status_code == 413
    assert fake.calls == []  # 超限音频不进模型


def test_transcribe_at_limit_ok(client, monkeypatch):
    """恰好等于上限 → 正常转写（边界不误杀）。"""
    import jarvis.server as server_mod
    fake = FakeWhisper()
    monkeypatch.setattr(server_mod, "_get_whisper", lambda: fake)
    monkeypatch.setattr(server_mod, "MAX_AUDIO_BYTES", 1024)
    r = client.post("/api/voice/transcribe", headers=H,
                    files={"file": ("ok.webm", b"x" * 1024, "audio/webm")})
    assert r.status_code == 200
    assert len(fake.calls) == 1
    assert len(fake.calls[0]["data"]) == 1024


# ---------------------------------------------------------------------------
# /api/voice/tts 代理（契约 1.6：转发 127.0.0.1:{tts_port}/tts，离线 503）
# ---------------------------------------------------------------------------

def _patch_tts_transport(monkeypatch, handler):
    import jarvis.server as server_mod
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(server_mod, "_tts_client",
                        lambda: httpx.AsyncClient(transport=transport))


def test_tts_requires_auth(client):
    r = client.post("/api/voice/tts", json={"text": "测试"})
    assert r.status_code == 401


def test_tts_proxy_forwards_and_returns_wav(client, monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, content=b"RIFF\x10\x00\x00\x00WAVEfake",
                              headers={"Content-Type": "audio/wav"})

    _patch_tts_transport(monkeypatch, handler)
    r = client.post("/api/voice/tts", headers=H, json={"text": "测试"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("audio/wav")
    assert r.content.startswith(b"RIFF")
    # 转发目标与 body 正确
    assert seen["url"] == f"http://127.0.0.1:{settings.tts_port}/tts"
    assert seen["json"] == {"text": "测试"}


def test_tts_worker_offline_503(client, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _patch_tts_transport(monkeypatch, handler)
    r = client.post("/api/voice/tts", headers=H, json={"text": "测试"})
    assert r.status_code == 503
    assert r.json()["detail"] == "tts worker offline"


def test_tts_text_too_long_422(client, monkeypatch):
    """审查修复（high）：text 超 500 字 → 422，且不打到 tts_worker（防串行锁被超长文本占死）。"""
    seen = {"called": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["called"] += 1
        return httpx.Response(200, content=b"RIFF", headers={"Content-Type": "audio/wav"})

    _patch_tts_transport(monkeypatch, handler)
    r = client.post("/api/voice/tts", headers=H, json={"text": "测" * 501})
    assert r.status_code == 422
    assert seen["called"] == 0


def test_tts_text_at_limit_forwarded(client, monkeypatch):
    """恰好 500 字 → 正常转发（边界不误杀）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"RIFF\x10\x00\x00\x00WAVEfake",
                              headers={"Content-Type": "audio/wav"})

    _patch_tts_transport(monkeypatch, handler)
    r = client.post("/api/voice/tts", headers=H, json={"text": "测" * 500})
    assert r.status_code == 200


def test_tts_worker_error_502(client, monkeypatch):
    """worker 合成失败返回 500 JSON → 代理以 502 透出错误信息（契约未覆盖路径的补充行为）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "synth boom"})

    _patch_tts_transport(monkeypatch, handler)
    r = client.post("/api/voice/tts", headers=H, json={"text": "测试"})
    assert r.status_code == 502
    assert "synth boom" in r.json()["detail"]


# ---------------------------------------------------------------------------
# voice/tts_worker.py（契约 1.3）：handler 抽成纯函数 + 假合成引擎
# ---------------------------------------------------------------------------

def _load_worker():
    """按文件路径加载（worker 在 index-tts 的 venv 里跑，不依赖 voice 包导入）。"""
    spec = importlib.util.spec_from_file_location("tts_worker_under_test", WORKER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeTtsEngine:
    """假合成引擎：兼容 IndexTTS2.infer(spk_audio_prompt, text, output_path) 签名。"""

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def infer(self, spk_audio_prompt=None, text=None, output_path=None):
        self.calls.append({"ref": spk_audio_prompt, "text": text})
        if self.fail:
            raise RuntimeError("synth boom")
        Path(output_path).write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt fakewav")


class SpyLock:
    """记录进入次数的假锁（断言合成串行加锁）。"""

    def __init__(self):
        self.entered = 0

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, *args):
        return False


@pytest.fixture
def worker_env(tmp_path):
    """构造白名单目录 + 默认参考音色 wav。"""
    allowed = tmp_path / "voice"
    allowed.mkdir()
    ref = allowed / "jarvis_ref.wav"
    ref.write_bytes(b"RIFF\x24\x00\x00\x00WAVEref")
    return SimpleNamespace(mod=_load_worker(), allowed=str(allowed), ref=str(ref),
                           tmp=tmp_path)


def test_worker_module_stdlib_only():
    """契约 1.3：tts_worker 顶层 import 只允许 stdlib（不 pip 进对方 venv）。"""
    tree = ast.parse(WORKER_PATH.read_text(encoding="utf-8"))
    top_imports = set()
    for node in tree.body:  # 只查模块顶层；indextts 在 main() 内按需 import 不算
        if isinstance(node, ast.Import):
            top_imports.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_imports.add(node.module.split(".")[0])
    non_std = top_imports - set(sys.stdlib_module_names)
    assert not non_std, f"tts_worker.py 顶层 import 出现非 stdlib 模块: {non_std}"


def test_worker_health_payload(worker_env):
    assert worker_env.mod.health_payload() == {"ok": True, "model_loaded": True}


def test_worker_binds_loopback_only(worker_env):
    """安全：worker 只绑 127.0.0.1（计划第 4 节安全审查项）。"""
    assert worker_env.mod.BIND_HOST == "127.0.0.1"


def test_worker_handle_tts_ok_default_ref(worker_env):
    engine = FakeTtsEngine()
    body = json.dumps({"text": "你好大哥"}).encode()
    status, ctype, payload = worker_env.mod.handle_tts(
        body, engine, SpyLock(), worker_env.ref, worker_env.allowed)
    assert status == 200
    assert ctype == "audio/wav"
    assert payload.startswith(b"RIFF")
    assert engine.calls == [{"ref": worker_env.ref, "text": "你好大哥"}]  # 缺省用 VOICE_REF


def test_worker_handle_tts_custom_ref_in_whitelist(worker_env):
    custom = Path(worker_env.allowed) / "custom.wav"
    custom.write_bytes(b"RIFF\x24\x00\x00\x00WAVEcustom")
    engine = FakeTtsEngine()
    body = json.dumps({"text": "测试", "ref": str(custom)}).encode()
    status, _, _ = worker_env.mod.handle_tts(
        body, engine, SpyLock(), worker_env.ref, worker_env.allowed)
    assert status == 200
    assert engine.calls[0]["ref"] == str(custom)


def test_worker_handle_tts_ref_outside_whitelist_400(worker_env):
    """安全：ref 路径注入——白名单目录之外一律拒绝（计划第 4 节）。"""
    outside = worker_env.tmp / "evil.wav"
    outside.write_bytes(b"RIFF evil")
    engine = FakeTtsEngine()
    body = json.dumps({"text": "测试", "ref": str(outside)}).encode()
    status, ctype, payload = worker_env.mod.handle_tts(
        body, engine, SpyLock(), worker_env.ref, worker_env.allowed)
    assert status == 400
    assert ctype == "application/json"
    assert "error" in json.loads(payload)
    assert engine.calls == []  # 没碰合成


def test_worker_handle_tts_ref_traversal_400(worker_env):
    """白名单目录拼 ../ 逃逸也要被 realpath 归一化拦下。"""
    outside = worker_env.tmp / "evil.wav"
    outside.write_bytes(b"RIFF evil")
    sneaky = str(Path(worker_env.allowed) / ".." / "evil.wav")
    engine = FakeTtsEngine()
    body = json.dumps({"text": "测试", "ref": sneaky}).encode()
    status, _, _ = worker_env.mod.handle_tts(
        body, engine, SpyLock(), worker_env.ref, worker_env.allowed)
    assert status == 400
    assert engine.calls == []


def test_worker_handle_tts_ref_not_wav_400(worker_env):
    bad = Path(worker_env.allowed) / "notes.txt"
    bad.write_text("not a wav")
    engine = FakeTtsEngine()
    body = json.dumps({"text": "测试", "ref": str(bad)}).encode()
    status, _, _ = worker_env.mod.handle_tts(
        body, engine, SpyLock(), worker_env.ref, worker_env.allowed)
    assert status == 400
    assert engine.calls == []


def test_worker_handle_tts_bad_json_400(worker_env):
    engine = FakeTtsEngine()
    status, ctype, payload = worker_env.mod.handle_tts(
        b"not-json{", engine, SpyLock(), worker_env.ref, worker_env.allowed)
    assert status == 400
    assert "error" in json.loads(payload)


def test_worker_handle_tts_empty_text_400(worker_env):
    engine = FakeTtsEngine()
    body = json.dumps({"text": "   "}).encode()
    status, _, payload = worker_env.mod.handle_tts(
        body, engine, SpyLock(), worker_env.ref, worker_env.allowed)
    assert status == 400
    assert "error" in json.loads(payload)


def test_worker_handle_tts_text_too_long_400(worker_env):
    """审查修复（high）双层防线：worker 端也拒绝超长 text（不无限占用 MPS/CPU）。"""
    engine = FakeTtsEngine()
    body = json.dumps({"text": "测" * (worker_env.mod.MAX_TEXT + 1)}).encode()
    status, ctype, payload = worker_env.mod.handle_tts(
        body, engine, SpyLock(), worker_env.ref, worker_env.allowed)
    assert status == 400
    assert ctype == "application/json"
    assert "error" in json.loads(payload)
    assert engine.calls == []  # 没碰合成


def test_worker_handle_tts_engine_failure_500(worker_env):
    engine = FakeTtsEngine(fail=True)
    body = json.dumps({"text": "测试"}).encode()
    status, ctype, payload = worker_env.mod.handle_tts(
        body, engine, SpyLock(), worker_env.ref, worker_env.allowed)
    assert status == 500
    assert ctype == "application/json"
    assert "synth boom" in json.loads(payload)["error"]


def test_worker_synthesize_serialized_with_lock(worker_env):
    """契约 1.3：合成串行加锁（模型非线程安全）。"""
    engine = FakeTtsEngine()
    lock = SpyLock()
    wav = worker_env.mod.synthesize(engine, lock, "你好", worker_env.ref)
    assert lock.entered == 1
    assert wav.startswith(b"RIFF")


def test_worker_warmup_one_shot(worker_env):
    """契约 1.3 + 第 0 节实测事实：启动后必须立即暖机合成一次（结果丢弃）。"""
    engine = FakeTtsEngine()
    worker_env.mod.warmup(engine, SpyLock(), worker_env.ref)
    assert len(engine.calls) == 1
    assert engine.calls[0]["text"] == worker_env.mod.WARMUP_TEXT
    assert engine.calls[0]["ref"] == worker_env.ref


# ---------------------------------------------------------------------------
# web 增量（契约 1.6）：node --check + DOM id 交叉校验 + summarize() 行为
# ---------------------------------------------------------------------------

def test_web_dom_ids_cross_check():
    """新增 DOM id 在 index.html 与 app.js 双向对得上（沿用 F 的验法）。"""
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    for el_id in ("mic-btn", "btn-speak"):
        assert f'id="{el_id}"' in html, f"index.html 缺 #{el_id}"
        assert f"#{el_id}" in js, f"app.js 未引用 #{el_id}"
    # 关键行为锚点
    assert "/api/voice/transcribe" in js
    assert "/api/voice/tts" in js
    assert "jarvis_speak" in js          # 朗读开关 localStorage key（契约 1.6）
    assert "MediaRecorder" in js         # 按住录音用 MediaRecorder audio/webm
    assert "function summarize" in js    # 摘要抽成函数（与 daemon 规则一致）
    css = (WEB_DIR / "style.css").read_text(encoding="utf-8")
    assert ".reactor.recording" in css   # 录音中反应堆红脉冲


@pytest.mark.skipif(shutil.which("node") is None, reason="node 不可用")
def test_web_app_js_node_syntax():
    r = subprocess.run(["node", "--check", str(WEB_DIR / "app.js")],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"app.js 语法错误:\n{r.stderr}"


NODE_HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
// 浏览器全局桩：app.js 顶层只注册 DOMContentLoaded，不应执行其它副作用
const documentStub = {
  addEventListener() {}, querySelector() { return null; },
  querySelectorAll() { return []; }, createElement() { return {}; },
};
const fn = new Function(
  "document", "window", "localStorage", "location", "WebSocket", "navigator",
  src + "\n;return summarize;");
const summarize = fn(documentStub, {}, { getItem() { return null; }, setItem() {}, removeItem() {} },
  { protocol: "http:", host: "test" }, function () {}, {});
const cases = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
process.stdout.write(JSON.stringify(cases.map((c) => summarize(c))));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node 不可用")
def test_web_summarize_behavior(tmp_path):
    """summarize()：去 markdown 符号、取前两句（与 daemon 摘要规则一致）。"""
    harness = tmp_path / "harness.js"
    harness.write_text(NODE_HARNESS, encoding="utf-8")
    cases = [
        "**大哥**，搞定了。服务已重启。日志在 `logs/` 目录。",   # 0: 三句取两句 + 去粗体/反引号
        "结果如下：\n```bash\nls -la\n```\n一切正常。",            # 1: 代码块整段丢弃
        "# 标题\n- 项目一\n- 项目二",                              # 2: 标题/列表符号剥掉
        "",                                                        # 3: 空入空出
    ]
    cases_file = tmp_path / "cases.json"
    cases_file.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    r = subprocess.run(["node", str(harness), str(WEB_DIR / "app.js"), str(cases_file)],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"node 执行失败:\n{r.stderr}"
    out = json.loads(r.stdout)
    assert out[0] == "大哥，搞定了。服务已重启。"   # 前两句、markdown 符号已去
    assert "`" not in out[1] and "ls -la" not in out[1] and "一切正常" in out[1]
    assert "#" not in out[2] and "项目一" in out[2]
    assert out[3] == ""

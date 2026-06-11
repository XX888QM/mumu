"""IndexTTS-2 合成 worker（契约 Phase 2 计划 1.3）。

运行方式（在 index-tts 仓库自带的 .venv 里跑，本文件顶层**只用 stdlib**，不 pip 进对方 venv）：
    <index-tts>/.venv/bin/python <root>/voice/tts_worker.py

配置全部经环境变量传入（plist EnvironmentVariables 注入）：
    INDEX_TTS_DIR  index-tts 仓库绝对路径（cwd 切过去 + sys.path 注入）
    VOICE_REF      默认参考音色 wav 绝对路径（ref 缺省时使用；其父目录即 ref 白名单目录）
    TTS_PORT       监听端口（默认 8778）

HTTP 接口（ThreadingHTTPServer，仅绑 127.0.0.1）：
    GET  /healthz → 200 {"ok":true,"model_loaded":true}
    POST /tts     body JSON {"text":"...", "ref":"可选wav绝对路径"} → 200 audio/wav 字节
                  请求非法（坏 JSON/空 text/超长 text/ref 不在白名单）→ 400 JSON {"error":...}
                  合成失败 → 500 JSON {"error":...}

第 0 节实测事实：设备自动选 mps；模型加载 ~18.5s；首次合成 ~7s（含参考音色缓存）、
暖机后短句 ~4.5s——故启动后必须立即暖机合成一次（结果丢弃）。
"""
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 安全（计划第 4 节）：worker 只绑本机回环，不暴露局域网
BIND_HOST = "127.0.0.1"
# 暖机用短句（结果丢弃，只为预热参考音色缓存与计算图）
WARMUP_TEXT = "木木语音系统已就绪。"
# 审查修复（high）：text 长度上限（与 jarvis/server.py MAX_TTS_TEXT 对齐的双层防线）
# ——合成串行加锁，超长文本会无限占用 MPS/CPU 并把队列占死
MAX_TEXT = 500

_JSON = "application/json"


def health_payload() -> dict:
    """GET /healthz 响应体：能服务即模型已加载（main 里模型加载完才起 HTTP）。"""
    return {"ok": True, "model_loaded": True}


def _jbytes(obj: dict) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def validate_ref(ref, allowed_dir: str, default_ref: str) -> str:
    """ref 白名单校验（防路径注入，计划第 4 节）。

    - ref 缺省/为空 → 回退默认 VOICE_REF；
    - 自定义 ref 必须是 allowed_dir（默认音色所在目录）下真实存在的 .wav，
      realpath 归一化后比对，杜绝 ../ 逃逸与符号链接绕过。
    不合法抛 ValueError。
    """
    if not ref:
        return default_ref
    path = os.path.realpath(str(ref))
    allowed = os.path.realpath(allowed_dir)
    if not path.lower().endswith(".wav"):
        raise ValueError("ref 必须是 .wav 文件")
    try:
        inside = os.path.commonpath([path, allowed]) == allowed
    except ValueError:  # 不同盘符等异常情况一律视为越界
        inside = False
    if not inside:
        raise ValueError("ref 不在允许目录内")
    if not os.path.isfile(path):
        raise ValueError("ref wav 文件不存在")
    return path


def synthesize(engine, lock, text: str, ref: str) -> bytes:
    """合成一段文本 → wav 字节。串行加锁——IndexTTS2 模型非线程安全（契约 1.3）。

    engine.infer 落盘到临时 wav 后读回字节（IndexTTS2 的 API 以 output_path 输出）。
    """
    with lock:
        with tempfile.TemporaryDirectory(prefix="jarvis_tts_") as td:
            out_path = os.path.join(td, "out.wav")
            engine.infer(spk_audio_prompt=ref, text=text, output_path=out_path)
            with open(out_path, "rb") as f:
                return f.read()


def handle_tts(body: bytes, engine, lock, default_ref: str, allowed_dir: str):
    """POST /tts 核心逻辑（从 HTTP handler 抽出便于单测，计划第 2 节 V3）。

    返回 (status:int, content_type:str, payload:bytes)。
    """
    try:
        req = json.loads((body or b"").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 400, _JSON, _jbytes({"error": "body 必须是合法 JSON"})
    if not isinstance(req, dict):
        return 400, _JSON, _jbytes({"error": "body 必须是 JSON 对象"})
    text = str(req.get("text") or "").strip()
    if not text:
        return 400, _JSON, _jbytes({"error": "text 不能为空"})
    if len(text) > MAX_TEXT:
        return 400, _JSON, _jbytes({"error": f"text 过长（上限 {MAX_TEXT} 字）"})
    try:
        ref = validate_ref(req.get("ref"), allowed_dir, default_ref)
    except ValueError as exc:
        return 400, _JSON, _jbytes({"error": str(exc)})
    try:
        wav = synthesize(engine, lock, text, ref)
    except Exception as exc:  # 合成失败 → 500 JSON（契约 1.3）
        return 500, _JSON, _jbytes({"error": f"{type(exc).__name__}: {exc}"})
    return 200, "audio/wav", wav


def warmup(engine, lock, ref: str) -> None:
    """启动后立即暖机合成一次（结果丢弃）——第 0 节实测：暖机后短句 ~4.5s。"""
    synthesize(engine, lock, WARMUP_TEXT, ref)


def make_handler(engine, lock, default_ref: str, allowed_dir: str):
    """构造注入了依赖的 BaseHTTPRequestHandler 子类（依赖经闭包传入，方便测试替身）。"""

    class TtsHandler(BaseHTTPRequestHandler):
        server_version = "JarvisTTS/1.0"

        def _send(self, status: int, ctype: str, payload: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):  # noqa: N802（http.server 固定方法名）
            if self.path == "/healthz":
                self._send(200, _JSON, _jbytes(health_payload()))
            else:
                self._send(404, _JSON, _jbytes({"error": "not found"}))

        def do_POST(self):  # noqa: N802
            if self.path != "/tts":
                self._send(404, _JSON, _jbytes({"error": "not found"}))
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            body = self.rfile.read(length) if length > 0 else b""
            status, ctype, payload = handle_tts(body, engine, lock, default_ref, allowed_dir)
            self._send(status, ctype, payload)

        def log_message(self, fmt, *args):  # 精简访问日志 → stderr（launchd 收进 tts.err.log）
            sys.stderr.write("[tts_worker] %s - %s\n" % (self.address_string(), fmt % args))

    return TtsHandler


def main() -> int:
    index_tts_dir = (os.environ.get("INDEX_TTS_DIR") or "").strip()
    voice_ref = (os.environ.get("VOICE_REF") or "").strip()
    port = int(os.environ.get("TTS_PORT") or "8778")
    if not index_tts_dir or not os.path.isdir(index_tts_dir):
        print(f"[tts_worker] INDEX_TTS_DIR 无效: {index_tts_dir!r}", file=sys.stderr)
        return 2
    if not voice_ref or not os.path.isfile(voice_ref):
        print(f"[tts_worker] VOICE_REF 无效: {voice_ref!r}", file=sys.stderr)
        return 2

    # 契约 1.3：cwd 切到 INDEX_TTS_DIR + sys.path 注入（checkpoints/ 相对路径与包导入都靠它）
    os.chdir(index_tts_dir)
    sys.path.insert(0, index_tts_dir)

    t0 = time.time()
    # 只在运行时（index-tts 自己的 venv 里）才 import 第三方包，顶层保持纯 stdlib
    from indextts.infer_v2 import IndexTTS2
    engine = IndexTTS2(cfg_path="checkpoints/config.yaml", model_dir="checkpoints")
    print(f"[tts_worker] 模型加载完成 {time.time() - t0:.1f}s", flush=True)

    lock = threading.Lock()
    t0 = time.time()
    warmup(engine, lock, voice_ref)  # 启动暖机一次（结果丢弃）——契约 1.3
    print(f"[tts_worker] 暖机合成完成 {time.time() - t0:.1f}s", flush=True)

    default_ref = os.path.realpath(voice_ref)
    allowed_dir = os.path.dirname(default_ref)
    httpd = ThreadingHTTPServer((BIND_HOST, port),
                                make_handler(engine, lock, default_ref, allowed_dir))
    print(f"[tts_worker] 监听 http://{BIND_HOST}:{port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

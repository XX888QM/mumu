#!/usr/bin/env python
"""木木语音守护进程（Phase 2 V2，契约 1.5 状态机语义锁定）。

状态机：
  LISTEN     rec.stream() 喂 WakeDetector；分 > threshold → WAKE
  WAKE       播 wake ack → RECORD（带 pre_roll）
  RECORD     record_until_silence → 空/过短(<0.4s) → LISTEN；否则 TRANSCRIBE
  TRANSCRIBE Transcriber.transcribe(language="zh") → 文本
  DISPATCH   有 pending_approval：含拒绝词 → denied，含批准词 → approved
             （"不行"同时含"行"，必须先判拒绝）；都不含 → 追问一次，
             再不清 → 放弃（红卡仍在控制台）；
             否则 client.chat(文本)：busy → 播 busy ack；成功 → 播 accept ack → LISTEN
  WS 回调    listen 线程 → 主循环队列：
             task_done(本会话)：done → 摘要(去 markdown 取前两句)→tts→播；failed → 播 fail ack
             approval_request：pending=approval → 实时合成 approval_prompt(action) → 播 → RECORD
             （审查修复：实时 tts 合成+播放在后台线程执行，主音频循环不阻塞；
               授权提示播完才进 RECORD，录音与扬声器播放绝不重叠——防回声误决裁）
  打断       Player.playing 期间 VAD 检出人声 → Player.stop() → 免唤醒直接 RECORD

CLI：
  python voice/daemon.py                          常驻主循环（需麦克风）
  python voice/daemon.py --selftest               自检（不碰麦克风），全绿 exit 0
  python voice/daemon.py --once-from-wav x.wav    跳过唤醒/录音，把 wav 当 RECORD 产物
                                                  走完整后续流程（测试钩子；
                                                  环境变量 JARVIS_ONCE_GRACE 控制
                                                  派发前等 WS 事件的窗口秒数，默认 2）
  python voice/daemon.py --say "文本"             文本→TTS→播放（调试音色）

注：运行在 .venv-voice（无 python-dotenv，不能 import jarvis.config），
    用 stdlib 解析项目根 .env，字段名与契约 1.2 一致。
"""
from __future__ import annotations

import argparse
import collections
import dataclasses
import logging
import os
import queue
import re
import sys
import threading
import time
from pathlib import Path

# 契约 CLI 为 `python voice/daemon.py ...`：脚本方式运行时 sys.path 里只有 voice/，
# 手动把项目根补进去才能以包形式导入 voice.*
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from voice import acks
from voice.client import JarvisClient

logger = logging.getLogger("voice.daemon")

_ROOT = Path(__file__).resolve().parent.parent

# 授权口令关键词（契约 1.5 锁定）
APPROVE_WORDS = ("批准", "同意", "可以", "行")
DENY_WORDS = ("拒绝", "不行", "取消", "否")

_SENTENCE_ENDS = "。！？!?"

# 实时合成文本上限：与 /api/voice/tts 的 max_length（jarvis/server.py MAX_TTS_TEXT）
# 对齐，超长直接钳断，避免服务端 422 导致播报整段丢失（审查修复配套）
MAX_SPEAK_CHARS = 500


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------

def classify_decision(text: str) -> str | None:
    """授权口令分类：返回 'approved' / 'denied' / None（没听清）。

    必须先判拒绝词："不行" 同时包含批准词 "行"，拒绝优先才不会误批。
    """
    if any(w in text for w in DENY_WORDS):
        return "denied"
    if any(w in text for w in APPROVE_WORDS):
        return "approved"
    return None


def summarize(text: str, max_sentences: int = 2) -> str:
    """task_done 播报摘要：去 markdown 符号，取前两句（与网页端 summarize 规则一致）。"""
    t = text or ""
    t = re.sub(r"```.*?```", " ", t, flags=re.S)            # 代码块整体丢弃
    t = re.sub(r"`([^`]*)`", r"\1", t)                       # 行内代码去反引号
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", t)              # 图片
    t = re.sub(r"\[([^\]]*)\]\(([^)]*)\)", r"\1", t)         # 链接保留文字
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)      # 标题井号
    t = re.sub(r"^\s*[-*+]\s+", "", t, flags=re.M)           # 无序列表符
    t = re.sub(r"^\s*\d+[.、]\s+", "", t, flags=re.M)        # 有序列表符
    t = re.sub(r"[*_~#>|]", "", t)                            # 余下 markdown 符号
    t = re.sub(r"\s+", " ", t).strip()                        # 折叠空白
    if not t:
        return ""
    sentences: list[str] = []
    buf = ""
    for ch in t:
        buf += ch
        if ch in _SENTENCE_ENDS:
            sentences.append(buf.strip())
            buf = ""
            if len(sentences) >= max_sentences:
                break
    if len(sentences) < max_sentences and buf.strip():
        sentences.append(buf.strip())
    return "".join(sentences[:max_sentences])


# ---------------------------------------------------------------------------
# 配置（stdlib 解析 .env，环境变量优先；字段契约 1.2）
# ---------------------------------------------------------------------------

def _read_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return env


@dataclasses.dataclass
class VoiceConfig:
    base_url: str
    token: str
    # sherpa-onnx keywords_threshold 语义：默认 0.25；调大更难触发（防误唤醒 0.35-0.5）
    wake_threshold: float = 0.25
    asr_model: str = "large-v3-turbo"
    voice_cache_dir: str = str(_ROOT / "data" / "voice_cache")
    session_file: str = str(_ROOT / "data" / "voice_session.json")
    sample_rate: int = 16000
    block_ms: int = 80

    @classmethod
    def load(cls, env_path=None) -> "VoiceConfig":
        env = _read_env_file(Path(env_path) if env_path else _ROOT / ".env")

        def get(key: str, default: str) -> str:
            return os.environ.get(key) or env.get(key) or default

        port = int(get("JARVIS_PORT", "8777"))
        threshold = float(get("WAKE_THRESHOLD", "0.25"))
        if not 0.0 < threshold <= 0.6:
            # 实测 sherpa 概率上限不到 0.7：调到 0.7+ 唤醒会静默哑火（服务看着正常）
            logger.warning("WAKE_THRESHOLD=%s 超出有效调参区间 (0, 0.6]，唤醒可能完全失效；"
                           "误唤醒建议 0.35-0.5，漏唤醒调小", threshold)
        return cls(
            base_url=f"http://127.0.0.1:{port}",
            token=get("JARVIS_TOKEN", ""),
            wake_threshold=threshold,
            asr_model=get("ASR_MODEL", "large-v3-turbo"),
        )


# ---------------------------------------------------------------------------
# 状态机主体（依赖全部可注入，测试用 Fake）
# ---------------------------------------------------------------------------

class VoiceDaemon:
    """语音守护状态机。依赖注入：

    client       JarvisClient 兼容对象（chat/decide/tts/listen）
    player       voice.audio.Player 兼容对象（play_wav/stop/playing）
    wake         voice.wake.WakeDetector 兼容对象（feed/reset）
    transcriber  voice.asr.Transcriber 兼容对象（transcribe）
    vad          SileroVAD 兼容对象（is_speech）
    record_fn    voice.audio.record_until_silence 兼容函数
    recorder     voice.audio.Recorder（仅 run_forever 需要；测试可不传）
    cache        acks.ensure_cache 产物（key → wav 路径列表）
    """

    def __init__(self, client, player, wake, transcriber, vad, record_fn,
                 recorder=None, cache=None, wake_threshold: float = 0.25,
                 sample_rate: int = 16000, min_command_sec: float = 0.4,
                 pre_roll_blocks: int = 6):
        self.client = client
        self.player = player
        self.wake = wake
        self.transcriber = transcriber
        self.vad = vad
        self.record_fn = record_fn
        self.recorder = recorder
        self.cache = cache or {}
        self.wake_threshold = wake_threshold
        self.sample_rate = sample_rate
        self.min_command_sec = min_command_sec

        self.events: queue.Queue = queue.Queue()       # WS 线程 → 主循环
        self.pending_approval: dict | None = None
        self.active_task_id: str | None = None
        self.allow_record = True                        # --once-from-wav 模式置 False（无麦克风）
        self.playback_wait_sec = 5.0                    # 录音前等播放结束的超时上限（防回声）
        self._pre_roll: collections.deque = collections.deque(maxlen=pre_roll_blocks)
        self._stream = None
        self._stop = threading.Event()
        # 审查修复（high）：实时 TTS 挪到后台线程，主音频循环不被阻塞
        self._decision_pending = False                  # 授权提示播完 → 主循环免唤醒进 RECORD
        self._speech_thread: threading.Thread | None = None
        self._speech_lock = threading.Lock()            # 串行化后台播报，避免多条互相重叠

    # ------------------------------------------------------------------
    # 播放
    # ------------------------------------------------------------------

    def play_ack(self, key: str) -> None:
        """播一条该场景的缓存应答；缓存缺失时回退实时合成。"""
        try:
            path = acks.pick(self.cache, key)
        except KeyError:
            self.speak(acks.template_text(key))
            return
        self.player.play_wav(path, blocking=False)

    def speak(self, text: str) -> None:
        """实时合成并播放（同步原语；play_ack 缓存缺失兜底用）。

        注意：本方法会阻塞调用线程（暖机后短句合成 ~4.5s）。主循环/WS 事件
        路径一律改用 speak_async（审查修复：主循环阻塞会导致 InputStream
        溢出、唤醒与打断失效）。
        """
        if not text:
            return
        text = text[:MAX_SPEAK_CHARS]                      # 与服务端 max_length 对齐
        try:
            wav = self.client.tts(text)
        except Exception:                                  # noqa: BLE001
            logger.exception("TTS 合成失败：%s", text[:50])
            return
        self.player.play_wav(wav, blocking=False)

    def speak_async(self, text: str, *, then=None) -> None:
        """后台线程合成并**阻塞**播放，播完后回调 then（可选）。

        审查修复（high）：TTS HTTP（短句 ~4.5s，worker 慢时 20s+）若在主音频
        循环内同步执行，期间 InputStream 停止消费会溢出、唤醒词/打断全部丢失。
        挪到独立线程后主循环持续收音；_speech_lock 串行化避免多条播报重叠。

        播放用 blocking=True：播完才回调 then（如授权场景进入 RECORD），
        天然防回声——录音绝不与扬声器播放重叠。TTS 失败时不回调 then
        （大哥没听到提示就不自动开录；授权红卡仍在控制台可处理）。
        """
        if not text:
            return
        text = text[:MAX_SPEAK_CHARS]                      # 与服务端 max_length 对齐

        def worker():
            with self._speech_lock:
                try:
                    wav = self.client.tts(text)
                except Exception:                          # noqa: BLE001
                    logger.exception("TTS 合成失败：%s", text[:50])
                    return
                self.player.play_wav(wav, blocking=True)
            if then is not None:
                then()

        t = threading.Thread(target=worker, daemon=True, name="voice-speak")
        self._speech_thread = t
        t.start()

    def wait_speech(self, timeout: float | None = None) -> None:
        """等最近一次后台播报线程结束（once_from_wav 收尾与测试用）。"""
        t = self._speech_thread
        if t is not None:
            t.join(timeout)

    def _wait_playback(self, timeout: float | None = None) -> None:
        """轮询等当前播放结束（带超时上限），录音前调用。

        审查修复（high）：授权追问 ack 以非阻塞播出后若立即录音，麦克风会采到
        扬声器回声，"批准还是拒绝"被 ASR 转写命中关键词 → 未经大哥开口就误决裁。
        """
        timeout = self.playback_wait_sec if timeout is None else timeout
        deadline = time.monotonic() + timeout
        while self.player.playing and time.monotonic() < deadline:
            time.sleep(0.05)

    # ------------------------------------------------------------------
    # LISTEN（含打断）
    # ------------------------------------------------------------------

    def process_chunk(self, chunk) -> str:
        """处理 LISTEN 态一个音频块，返回转移：'listen' / 'wake' / 'interrupt'。"""
        if self.player.playing:
            if self.vad.is_speech(chunk):
                # 打断：停播 → 免唤醒直接 RECORD（触发块作为 pre_roll）
                self.player.stop()
                self.run_interaction(pre_roll=[chunk])
                return "interrupt"
            return "listen"                                # 播放期间不喂唤醒检测器
        if self._decision_pending:
            # 授权提示已播完（speak_async 阻塞播放后置位）→ 免唤醒进 RECORD 听决定。
            # 标志只消费一次；若提示播放中已被打断路径提前决裁（pending 清空）则跳过。
            self._decision_pending = False
            if self.pending_approval is not None:
                self.run_interaction(pre_roll=[chunk])
                return "decision"
        score = self.wake.feed(chunk)
        self._pre_roll.append(chunk)
        if score > self.wake_threshold:
            # WAKE：播应答 → RECORD（带监听期缓存块）
            self.play_ack("wake")
            self.wake.reset()
            pre_roll = list(self._pre_roll)
            self._pre_roll.clear()
            self.run_interaction(pre_roll=pre_roll)
            return "wake"
        return "listen"

    # ------------------------------------------------------------------
    # RECORD → TRANSCRIBE → DISPATCH
    # ------------------------------------------------------------------

    def _record_and_transcribe(self, pre_roll=None) -> str | None:
        """收音到静音并转写；空/过短(<0.4s)/空白文本 → None（回 LISTEN）。"""
        if not self.allow_record:
            return None                                    # --once-from-wav：无麦克风
        audio = self.record_fn(self._stream, self.vad, pre_roll=pre_roll)
        if audio is None or len(audio) < int(self.min_command_sec * self.sample_rate):
            return None
        text = self.transcriber.transcribe(audio, language="zh")
        text = (text or "").strip()
        return text or None

    def run_interaction(self, pre_roll=None) -> str | None:
        """一次完整交互：RECORD → TRANSCRIBE → DISPATCH。返回派发结果标签。"""
        text = self._record_and_transcribe(pre_roll)
        if text is None:
            return None
        logger.info("识别：%s", text)
        return self.dispatch(text)

    def dispatch(self, text: str) -> str:
        """DISPATCH：优先处理待决授权，否则交给 chat。"""
        if self.pending_approval is not None:
            return self._dispatch_approval(text)
        try:
            resp = self.client.chat(text)
        except Exception:                                  # noqa: BLE001
            logger.exception("chat 请求失败")
            self.play_ack("fail")
            return "error"
        if resp.get("busy"):
            self.play_ack("busy")
            return "busy"
        self.active_task_id = resp.get("task_id") or self.active_task_id
        self.play_ack("accept")
        return "accepted"

    def _dispatch_approval(self, text: str) -> str:
        """授权三路径：批准 / 拒绝 / 两次听不清放弃。"""
        approval_id = (self.pending_approval or {}).get("id")
        decision = classify_decision(text)
        if decision:
            self.client.decide(approval_id, decision)
            self.pending_approval = None
            return decision
        # 第一次没听清 → 追问一次
        self.play_ack("approval_unclear")
        # 审查修复（high）：等追问语播完再开录，否则录到扬声器回声里的
        # "批准还是拒绝"会被 classify_decision 命中 → 误决裁
        self._wait_playback()
        retry_text = self._record_and_transcribe()
        decision = classify_decision(retry_text) if retry_text else None
        if decision:
            self.client.decide(approval_id, decision)
            self.pending_approval = None
            return decision
        # 再不清 → 放弃，清 pending（红卡仍在控制台）
        self.play_ack("approval_giveup")
        self.pending_approval = None
        return "giveup"

    # ------------------------------------------------------------------
    # WS 事件（listen 线程入队 → 主循环消费）
    # ------------------------------------------------------------------

    def on_ws_event(self, msg: dict) -> None:
        """JarvisClient.listen 的回调（运行在 WS 线程）：只入队，不直接处理。"""
        self.events.put(msg)

    def drain_events(self) -> None:
        """主循环消费事件队列。"""
        while True:
            try:
                msg = self.events.get_nowait()
            except queue.Empty:
                return
            try:
                self.handle_event(msg)
            except Exception:                              # noqa: BLE001
                logger.exception("处理 WS 事件失败：%s", msg)

    def handle_event(self, msg: dict) -> None:
        mtype = msg.get("type")
        if mtype == "task_done":
            if not self.active_task_id or msg.get("task_id") != self.active_task_id:
                return                                     # 非本会话任务 → 忽略
            self.active_task_id = None
            status = msg.get("status")
            if status == "done":
                text = summarize(msg.get("result") or "")
                if text:
                    # 审查修复：后台合成播报，不阻塞主音频循环
                    self.speak_async(text)
            elif status == "failed":
                self.play_ack("fail")
            # cancelled → 静默（契约只规定 done/failed 的播报）
        elif mtype == "approval_request":
            approval = msg.get("approval") or {}
            self.pending_approval = approval               # 先置 pending：合成期间抢答也走授权分支
            # 模板句不缓存 → 实时合成（契约 1.5）；审查修复：后台合成+阻塞播放，
            # 播完（且未被打断提前决裁）由主循环免唤醒进入 RECORD——录音不与播放重叠（防回声）
            prompt = acks.template_text("approval_prompt", action=approval.get("action", ""))
            then = self._arm_decision_record if self.allow_record else None
            self.speak_async(prompt, then=then)
        # 其余类型（auth_ok/system/task_event/...）忽略

    def _arm_decision_record(self) -> None:
        """speak_async(approval_prompt) 播放完毕的回调：通知主循环进 RECORD 听决定。"""
        self._decision_pending = True

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def run_forever(self) -> None:
        """常驻：起 WS 监听线程 + 麦克风流主循环。"""
        self.client.listen(self.on_ws_event)
        stream_obj = self.recorder.stream()
        # Recorder.stream() 契约为"上下文管理器/生成器"，两种形态都兼容
        if hasattr(stream_obj, "__enter__"):
            with stream_obj as stream:
                self._run_loop(stream)
        else:
            self._run_loop(stream_obj)

    def _run_loop(self, stream) -> None:
        self._stream = stream
        for chunk in stream:
            self.drain_events()
            self.process_chunk(chunk)
            if self._stop.is_set():
                break

    def stop(self) -> None:
        self._stop.set()


# ---------------------------------------------------------------------------
# 真实依赖组装（仅运行期调用；测试全用 Fake 注入，不会走到这里）
# ---------------------------------------------------------------------------

def build_daemon(cfg: VoiceConfig | None = None, *, need_audio: bool = True) -> VoiceDaemon:
    """组装真实依赖。V1 模块（audio/wake/asr）延迟导入，避免测试期硬依赖。"""
    from voice.asr import Transcriber
    from voice.audio import Player, Recorder, SileroVAD, record_until_silence
    from voice.wake import WakeDetector

    cfg = cfg or VoiceConfig.load()
    client = JarvisClient(cfg.base_url, cfg.token, session_file=cfg.session_file)
    player = Player()
    recorder = Recorder(sample_rate=cfg.sample_rate, block_ms=cfg.block_ms) if need_audio else None
    wake = WakeDetector(threshold=cfg.wake_threshold)
    transcriber = Transcriber(cfg.asr_model)
    vad = SileroVAD()

    try:
        cache = acks.ensure_cache(client.tts, cache_dir=cfg.voice_cache_dir)
    except Exception:                                      # noqa: BLE001
        logger.exception("应答缓存生成失败（tts worker 离线？）；将逐条实时合成兜底")
        cache = {}

    return VoiceDaemon(
        client=client, player=player, wake=wake, transcriber=transcriber,
        vad=vad, record_fn=record_until_silence, recorder=recorder,
        cache=cache, wake_threshold=cfg.wake_threshold, sample_rate=cfg.sample_rate,
    )


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------

def _wav_to_int16_16k(data: bytes):
    """wav 字节 → 16kHz 单声道 int16 numpy 数组（selftest 喂唤醒模型用）。"""
    import io
    import wave

    import numpy as np

    with wave.open(io.BytesIO(data)) as wav:
        if wav.getsampwidth() != 2:
            raise ValueError(f"仅支持 16-bit PCM，实际 sampwidth={wav.getsampwidth()}")
        rate = wav.getframerate()
        channels = wav.getnchannels()
        frames = wav.readframes(wav.getnframes())
    samples = np.frombuffer(frames, dtype=np.int16)
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1).astype(np.int16)
    if rate != 16000:
        target_len = int(len(samples) * 16000 / rate)
        x_old = np.arange(len(samples), dtype=np.float64)
        x_new = np.linspace(0, len(samples) - 1, target_len)
        samples = np.interp(x_new, x_old, samples.astype(np.float64)).astype(np.int16)
    return samples


def selftest() -> int:
    """自检（不碰麦克风）：server/TTS/ASR 闭环/唤醒模型/VAD/应答缓存。全绿 exit 0。"""
    import tempfile

    import numpy as np

    cfg = VoiceConfig.load()
    client = JarvisClient(cfg.base_url, cfg.token, session_file=cfg.session_file)
    results: list[tuple[str, bool, str]] = []
    state: dict = {}

    def check(name, fn):
        try:
            detail = fn() or "ok"
            results.append((name, True, str(detail)))
        except Exception as exc:                           # noqa: BLE001
            results.append((name, False, f"{type(exc).__name__}: {exc}"))

    def c_server():
        import httpx
        resp = httpx.get(cfg.base_url + "/healthz", timeout=5)
        resp.raise_for_status()
        return "木木主服务在线"

    def c_tts():
        data = client.tts("木木语音自检")
        assert data[:4] == b"RIFF" and len(data) > 1000, f"非 wav 或过短（{len(data)} 字节）"
        state["tts_wav"] = data
        return f"{len(data)} 字节 wav"

    def c_asr():
        from voice.asr import Transcriber
        assert "tts_wav" in state, "依赖 TTS 步骤先通过"
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(state["tts_wav"])
            path = f.name
        try:
            transcriber = Transcriber(cfg.asr_model)
            text = transcriber.transcribe_file(path, language="zh").strip()
        finally:
            os.unlink(path)
        assert text, "转写为空"
        return f"TTS↔ASR 闭环识别：{text[:40]}"

    def c_wake():
        from voice.wake import WakeDetector
        detector = WakeDetector(threshold=cfg.wake_threshold)
        samples = _wav_to_int16_16k(client.tts("木木"))
        # KWS 离线喂入需尾部补静音（tail padding），否则末尾帧不出解码结果
        samples = np.concatenate([samples, np.zeros(16000, dtype=np.int16)])
        best = 0.0
        for i in range(0, max(len(samples) - 1280, 1), 1280):
            best = max(best, float(detector.feed(samples[i:i + 1280])))
        assert best > 0.5, "合成『木木』音频未触发唤醒（检查模型/keywords.txt/阈值）"
        return f"合成『木木』音频成功触发唤醒（keywords_threshold={cfg.wake_threshold}）"

    def c_vad():
        from voice.audio import SileroVAD
        vad = SileroVAD()
        silent = np.zeros(1280, dtype=np.int16)
        assert not vad.is_speech(silent), "静音被误判为人声"
        return "静音判定正确"

    def c_cache():
        cache = acks.ensure_cache(client.tts, cache_dir=cfg.voice_cache_dir)
        total = sum(len(v) for v in cache.values())
        assert total > 0, "缓存为空"
        missing = [p for paths in cache.values() for p in paths if not Path(p).exists()]
        assert not missing, f"缺文件：{missing}"
        return f"{total} 条应答缓存就绪"

    check("server /healthz", c_server)
    check("TTS 合成", c_tts)
    check("ASR 转写（TTS↔ASR 闭环）", c_asr)
    check("唤醒模型加载/喂入", c_wake)
    check("SileroVAD 静音判定", c_vad)
    check("应答语缓存", c_cache)

    all_ok = all(ok for _, ok, _ in results)
    print("=" * 64)
    for name, ok, detail in results:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    print("=" * 64)
    print("自检全绿" if all_ok else "自检存在失败项")
    client.close()
    return 0 if all_ok else 1


def once_from_wav(wav_path: str) -> int:
    """测试钩子：跳过唤醒与录音，把 wav 当作 RECORD 产物走完整后续流程。

    流程：连 WS → 等 JARVIS_ONCE_GRACE 秒（给 approval_request 等事件到达窗口）
    → 转写 wav → DISPATCH → 若派发了 chat 任务则等 task_done 并播报后退出。
    """
    path = Path(wav_path)
    if not path.exists():
        print(f"wav 不存在：{path}", file=sys.stderr)
        return 2
    daemon = build_daemon(need_audio=False)
    daemon.allow_record = False                            # 无麦克风：禁用自动 RECORD
    daemon.client.listen(daemon.on_ws_event)

    grace = float(os.environ.get("JARVIS_ONCE_GRACE", "2"))
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        daemon.drain_events()
        time.sleep(0.1)

    text = daemon.transcriber.transcribe_file(str(path), language="zh").strip()
    if not text:
        print("wav 转写为空，无法派发", file=sys.stderr)
        return 1
    print(f"转写：{text}")
    result = daemon.dispatch(text)
    print(f"派发结果：{result}")

    if result == "accepted":
        timeout = float(os.environ.get("JARVIS_ONCE_TIMEOUT", "600"))
        deadline = time.monotonic() + timeout
        while daemon.active_task_id and time.monotonic() < deadline:
            daemon.drain_events()
            time.sleep(0.2)
        if daemon.active_task_id:
            print("等待 task_done 超时", file=sys.stderr)
            return 1
        # 等后台播报线程收尾（合成+阻塞播放；FAKE_AUDIO 模式写文件即返回）
        daemon.wait_speech(timeout=60)
        return 0
    daemon.wait_speech(timeout=60)
    return 0 if result in ("approved", "denied") else 1


def say(text: str) -> int:
    """文本 → TTS → 播放（调试音色）。"""
    from voice.audio import Player

    cfg = VoiceConfig.load()
    client = JarvisClient(cfg.base_url, cfg.token, session_file=cfg.session_file)
    wav = client.tts(text)
    Player().play_wav(wav, blocking=True)
    client.close()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="木木语音守护进程（Phase 2）")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--selftest", action="store_true",
                       help="自检（不碰麦克风），全绿 exit 0")
    group.add_argument("--once-from-wav", metavar="WAV",
                       help="跳过唤醒与录音，把 wav 当作 RECORD 产物走完整后续流程（测试钩子）")
    group.add_argument("--say", metavar="TEXT", help="文本→TTS→播放（调试音色）")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.selftest:
        return selftest()
    if args.once_from_wav:
        return once_from_wav(args.once_from_wav)
    if args.say:
        return say(args.say)

    daemon = build_daemon()
    logger.info("木木语音守护启动：唤醒词 木木，keywords_threshold %s", daemon.wake_threshold)
    daemon.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""音频采集/播放/VAD（契约：Phase 2 计划 1.4 节，sounddevice 16k 单声道 int16）。

包含：
- Recorder            : 麦克风采集，stream() 同时支持 with 上下文与直接 for 迭代
- Player              : wav 播放（路径或字节）；JARVIS_VOICE_FAKE_AUDIO=1 时改为写文件（集成测试钩子）
- SileroVAD           : silero-vad 真模型封装（包内置 jit 权重，无需联网），is_speech(chunk)->bool
- record_until_silence: 从流收音直到连续静音 silence_ms 或总时长 max_sec

本模块不依赖 jarvis.config（.venv-voice 里没有 dotenv），配置由调用方传参/环境变量。
"""
import io
import os
import time
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd

# 全链路统一 16k 采样率（唤醒/VAD/ASR 模型都按 16k 训练）
SAMPLE_RATE = 16000

# 项目根（voice/ 的上一级），fake-audio 钩子默认落盘目录用
_ROOT = Path(__file__).resolve().parent.parent


def _fake_audio_mode() -> bool:
    """集成测试钩子开关（计划第3节第4条）：=1 时 Player 不出声、播放内容写文件。"""
    return os.environ.get("JARVIS_VOICE_FAKE_AUDIO") == "1"


def _fake_audio_dir() -> Path:
    return Path(os.environ.get("JARVIS_VOICE_FAKE_AUDIO_DIR",
                               str(_ROOT / "data" / "voice_fake")))


class _RecorderStream:
    """Recorder.stream() 的返回对象：既是上下文管理器又是迭代器。

    用法 A：with rec.stream() as s: for chunk in s: ...
    用法 B：for chunk in rec.stream(): ...（惰性开流，由 GC/close 收尾）
    """

    def __init__(self, sample_rate: int, blocksize: int):
        self._sr = sample_rate
        self._blocksize = blocksize
        self._stream = None

    def _ensure_open(self):
        if self._stream is None:
            # 注意：方法体内引用模块级 sd，测试可 monkeypatch voice.audio.sd
            # latency="high"：加大 PortAudio 内部缓冲（审查缓解项——主循环短暂
            # 停顿时降低输入溢出/丢帧概率；根治靠 daemon 把 TTS 挪出主循环）
            self._stream = sd.InputStream(samplerate=self._sr,
                                          blocksize=self._blocksize,
                                          channels=1, dtype="int16",
                                          latency="high")
            self._stream.start()

    # ---- 上下文管理器协议 ----
    def __enter__(self):
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    # ---- 迭代器协议 ----
    def __iter__(self):
        return self

    def __next__(self) -> np.ndarray:
        self._ensure_open()
        data, _overflowed = self._stream.read(self._blocksize)
        # sounddevice 返回 (frames, channels)，拍平成一维 int16
        return np.asarray(data, dtype=np.int16).reshape(-1)

    def close(self):
        if self._stream is not None:
            try:
                self._stream.stop()
            finally:
                self._stream.close()
                self._stream = None


class Recorder:
    """麦克风采集：16k 单声道 int16，默认 80ms 块（=1280 样本；KWS 内部自动攒帧，块大小任意）。"""

    def __init__(self, sample_rate: int = SAMPLE_RATE, block_ms: int = 80):
        self.sample_rate = int(sample_rate)
        self.block_ms = int(block_ms)
        self.blocksize = self.sample_rate * self.block_ms // 1000

    def stream(self) -> _RecorderStream:
        """返回可迭代的音频块流（yield 一维 np.int16），支持 with。"""
        return _RecorderStream(self.sample_rate, self.blocksize)


def _decode_wav(data: bytes):
    """解析 wav 字节 → (ndarray, samplerate)。多声道时形状为 (n, ch)。"""
    with wave.open(io.BytesIO(data), "rb") as wf:
        sr = wf.getframerate()
        ch = wf.getnchannels()
        sw = wf.getsampwidth()
        frames = wf.readframes(wf.getnframes())
    if sw == 2:
        arr = np.frombuffer(frames, dtype=np.int16)
    elif sw == 4:
        arr = np.frombuffer(frames, dtype=np.int32)
    elif sw == 1:  # 8bit wav 是无符号，转有符号 int16
        arr = ((np.frombuffer(frames, dtype=np.uint8).astype(np.int16) - 128) * 256)
    else:
        raise ValueError(f"不支持的 wav 位宽: {sw * 8}bit")
    if ch > 1:
        arr = arr.reshape(-1, ch)
    return arr, sr


class Player:
    """wav 播放器。

    - play_wav(path_or_bytes, blocking=False)：路径或 wav 字节
    - stop()：立即停播（打断用）
    - playing：是否正在播
    - 测试钩子：环境变量 JARVIS_VOICE_FAKE_AUDIO=1 时不碰声卡，把"播放"的 wav 字节
      原样写入 JARVIS_VOICE_FAKE_AUDIO_DIR（默认 <root>/data/voice_fake），
      路径记录在 self.last_fake_path；此模式下 playing 恒 False、stop() 为空操作。
    """

    def __init__(self):
        self._fake_seq = 0
        self.last_fake_path: str | None = None

    def play_wav(self, path_or_bytes, blocking: bool = False) -> None:
        if isinstance(path_or_bytes, (bytes, bytearray)):
            data = bytes(path_or_bytes)
        else:
            data = Path(path_or_bytes).read_bytes()

        if _fake_audio_mode():
            out_dir = _fake_audio_dir()
            out_dir.mkdir(parents=True, exist_ok=True)
            self._fake_seq += 1
            name = f"play_{os.getpid()}_{self._fake_seq:03d}_{int(time.time() * 1000)}.wav"
            out = out_dir / name
            out.write_bytes(data)
            self.last_fake_path = str(out)
            return

        arr, sr = _decode_wav(data)
        sd.play(arr, samplerate=sr)
        if blocking:
            sd.wait()

    def stop(self) -> None:
        """立即停播（守护进程打断路径用）。"""
        if _fake_audio_mode():
            return
        sd.stop()

    @property
    def playing(self) -> bool:
        if _fake_audio_mode():
            return False
        try:
            stream = sd.get_stream()  # sd.play 创建的全局流；从未播放过会抛错
        except Exception:
            return False
        return bool(stream.active)


class SileroVAD:
    """silero-vad 封装（计划 1.4：vad 不单设模块，放 audio.py 内）。

    权重为 silero-vad pip 包内置 jit 文件，加载不联网。
    模型一次吃 512 样本（16k）；is_speech 对任意长度块按 512 开窗取最大概率。
    torch 在 __init__ 内懒加载，避免拖慢只用 Recorder/Player 的进程。
    """

    _WINDOW = 512  # 16k 下模型固定窗口

    def __init__(self, threshold: float = 0.5, sample_rate: int = SAMPLE_RATE):
        import torch  # 懒加载重依赖
        from silero_vad import load_silero_vad
        self._torch = torch
        self._model = load_silero_vad()  # jit 模型，CPU 即可
        self.threshold = float(threshold)
        self.sample_rate = int(sample_rate)

    def is_speech(self, chunk) -> bool:
        """块内任一 512 窗口语音概率 ≥ threshold 即判定有人声。"""
        arr = np.asarray(chunk).reshape(-1)
        if arr.size == 0:
            return False
        if arr.dtype == np.int16:
            arr = arr.astype(np.float32) / 32768.0
        else:
            arr = arr.astype(np.float32)
        torch = self._torch
        best = 0.0
        with torch.no_grad():
            for i in range(0, len(arr), self._WINDOW):
                win = arr[i:i + self._WINDOW]
                if len(win) < self._WINDOW:
                    win = np.pad(win, (0, self._WINDOW - len(win)))
                prob = float(self._model(torch.from_numpy(win), self.sample_rate).item())
                if prob > best:
                    best = prob
                if best >= self.threshold:  # 提前命中即可返回
                    break
        return best >= self.threshold

    def reset(self) -> None:
        """清 LSTM 状态（换话轮时调）。"""
        self._model.reset_states()


def record_until_silence(rec_stream, vad, max_sec: float = 15.0, silence_ms: int = 900,
                         pre_roll: list | None = None) -> np.ndarray:
    """从音频块流收音，直到"连续静音 ≥ silence_ms"或"总时长 ≥ max_sec"。

    参数：
    - rec_stream: 可迭代的一维 int16 块（Recorder.stream() 或任意迭代器）
    - vad      : 有 is_speech(chunk)->bool 的对象（SileroVAD 或测试假体）
    - pre_roll : 唤醒后已缓存的块列表，原样拼在结果最前（不参与 VAD 判定）

    返回一维 np.int16：
    - 流里始终没检出语音 → 返回空数组（守护进程据此回 LISTEN）
    - 否则返回 pre_roll + 流内容，并裁掉触发截断的尾部连续静音块
      （这样守护进程"<0.4s 过短"判断针对的是有效内容）
    """
    collected: list[np.ndarray] = []
    if pre_roll:
        for c in pre_roll:
            collected.append(np.asarray(c, dtype=np.int16).reshape(-1))

    total_ms = sum(len(c) for c in collected) * 1000.0 / SAMPLE_RATE
    speech_seen = False
    silence_acc = 0.0   # 当前连续静音时长（ms）
    trailing = 0        # 结尾连续静音块数（用于裁尾）

    for chunk in rec_stream:
        chunk = np.asarray(chunk, dtype=np.int16).reshape(-1)
        collected.append(chunk)
        chunk_ms = len(chunk) * 1000.0 / SAMPLE_RATE
        total_ms += chunk_ms

        if vad.is_speech(chunk):
            speech_seen = True
            silence_acc = 0.0
            trailing = 0
        else:
            silence_acc += chunk_ms
            trailing += 1
            if silence_acc >= silence_ms:
                break
        if total_ms >= max_sec * 1000.0:
            break

    if not speech_seen:
        return np.zeros(0, dtype=np.int16)
    if trailing:
        collected = collected[:-trailing]
    if not collected:
        return np.zeros(0, dtype=np.int16)
    return np.concatenate(collected)

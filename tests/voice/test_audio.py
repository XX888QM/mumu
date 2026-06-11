# -*- coding: utf-8 -*-
"""voice/audio.py 单元测试（V1）。

纪律：
- Recorder/Player 全部 monkeypatch 假 sounddevice，不碰真实麦克风/扬声器；
- record_until_silence 用合成正弦+静音数组验证截断逻辑（FakeVAD，确定性）；
- SileroVAD 用真模型（包内置 jit，无需下载）：静音=False、白噪+正弦混合样本不崩；
- Player 必须实现 JARVIS_VOICE_FAKE_AUDIO=1 测试钩子：播放=写文件，不出声。
"""
import io
import wave

import numpy as np
import pytest

import voice.audio as audio_mod
from voice.audio import Player, Recorder, SileroVAD, record_until_silence

SR = 16000
BLOCK = 1280  # 80ms @ 16k


# ---------------------------------------------------------------------------
# 假 sounddevice（不碰真音频设备）
# ---------------------------------------------------------------------------

class FakeInputStream:
    """假输入流：read() 返回以计数器填充的 int16 块，便于断言数据流向。"""

    def __init__(self, samplerate=None, blocksize=None, channels=None, dtype=None,
                 latency=None):
        self.kwargs = dict(samplerate=samplerate, blocksize=blocksize,
                           channels=channels, dtype=dtype, latency=latency)
        self.started = False
        self.closed = False
        self._counter = 0

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def close(self):
        self.closed = True

    def read(self, frames):
        self._counter += 1
        data = np.full((frames, 1), self._counter, dtype=np.int16)
        return data, False


class FakeOutStream:
    def __init__(self, active=True):
        self.active = active


class FakeSD:
    """替身 sounddevice 模块：记录所有调用。"""

    def __init__(self):
        self.input_streams = []
        self.played = []
        self.stop_calls = 0
        self.wait_calls = 0
        self._out_stream = None

    def InputStream(self, **kwargs):
        s = FakeInputStream(**kwargs)
        self.input_streams.append(s)
        return s

    def play(self, data, samplerate=None, **kwargs):
        self.played.append((data, samplerate))
        self._out_stream = FakeOutStream(active=True)

    def stop(self):
        self.stop_calls += 1
        if self._out_stream is not None:
            self._out_stream.active = False

    def wait(self):
        self.wait_calls += 1

    def get_stream(self):
        if self._out_stream is None:
            raise RuntimeError("no stream")
        return self._out_stream


@pytest.fixture()
def fake_sd(monkeypatch):
    fake = FakeSD()
    monkeypatch.setattr(audio_mod, "sd", fake)
    return fake


def make_wav_bytes(arr: np.ndarray, sr: int = SR) -> bytes:
    """生成 16bit 单声道 wav 字节。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(arr.astype(np.int16).tobytes())
    return buf.getvalue()


def sine_chunk(n=BLOCK, freq=440.0, amp=8000) -> np.ndarray:
    t = np.arange(n) / SR
    return (np.sin(2 * np.pi * freq * t) * amp).astype(np.int16)


def silence_chunk(n=BLOCK) -> np.ndarray:
    return np.zeros(n, dtype=np.int16)


# ---------------------------------------------------------------------------
# Recorder（假 sounddevice）
# ---------------------------------------------------------------------------

class TestRecorder:
    def test_stream_yields_int16_blocks(self, fake_sd):
        rec = Recorder()
        with rec.stream() as s:
            chunks = [next(s) for _ in range(3)]
        assert len(fake_sd.input_streams) == 1
        st = fake_sd.input_streams[0]
        # 16k 单声道 int16，80ms 块=1280 样本；latency=high 加大内部缓冲
        # （审查缓解项：主循环短暂停顿时降低 InputStream 溢出概率）
        assert st.kwargs == dict(samplerate=16000, blocksize=1280,
                                 channels=1, dtype="int16", latency="high")
        for c in chunks:
            assert isinstance(c, np.ndarray)
            assert c.dtype == np.int16
            assert c.ndim == 1 and c.shape == (1280,)
        # 三块内容来自 read 计数器（数据没被串改）
        assert chunks[0][0] == 1 and chunks[2][0] == 3

    def test_stream_context_manager_closes(self, fake_sd):
        rec = Recorder()
        with rec.stream() as s:
            next(s)
            st = fake_sd.input_streams[0]
            assert st.started
        assert st.closed

    def test_custom_block_ms(self, fake_sd):
        rec = Recorder(sample_rate=16000, block_ms=40)
        with rec.stream() as s:
            c = next(s)
        assert c.shape == (640,)
        assert fake_sd.input_streams[0].kwargs["blocksize"] == 640

    def test_stream_plain_iteration_without_with(self, fake_sd):
        # 契约写"上下文管理器/生成器"：直接 for 迭代也要能用
        rec = Recorder()
        it = iter(rec.stream())
        c = next(it)
        assert c.dtype == np.int16 and c.shape == (1280,)


# ---------------------------------------------------------------------------
# Player（假 sounddevice）
# ---------------------------------------------------------------------------

class TestPlayer:
    def test_play_wav_bytes_nonblocking(self, fake_sd, monkeypatch):
        monkeypatch.delenv("JARVIS_VOICE_FAKE_AUDIO", raising=False)
        wav = make_wav_bytes(sine_chunk(SR), sr=SR)
        p = Player()
        p.play_wav(wav, blocking=False)
        assert len(fake_sd.played) == 1
        data, sr = fake_sd.played[0]
        assert sr == SR
        assert isinstance(data, np.ndarray) and data.dtype == np.int16
        assert len(data) == SR
        assert fake_sd.wait_calls == 0  # 非阻塞不等待

    def test_play_wav_blocking_waits(self, fake_sd, monkeypatch):
        monkeypatch.delenv("JARVIS_VOICE_FAKE_AUDIO", raising=False)
        p = Player()
        p.play_wav(make_wav_bytes(sine_chunk()), blocking=True)
        assert fake_sd.wait_calls == 1

    def test_play_wav_from_path(self, fake_sd, tmp_path, monkeypatch):
        monkeypatch.delenv("JARVIS_VOICE_FAKE_AUDIO", raising=False)
        f = tmp_path / "a.wav"
        f.write_bytes(make_wav_bytes(sine_chunk()))
        p = Player()
        p.play_wav(str(f))
        assert len(fake_sd.played) == 1

    def test_playing_and_stop(self, fake_sd, monkeypatch):
        monkeypatch.delenv("JARVIS_VOICE_FAKE_AUDIO", raising=False)
        p = Player()
        assert p.playing is False  # 从未播放过
        p.play_wav(make_wav_bytes(sine_chunk()))
        assert p.playing is True
        p.stop()
        assert fake_sd.stop_calls == 1
        assert p.playing is False


class TestPlayerFakeAudioHook:
    """集成钩子（计划第3节第4条）：JARVIS_VOICE_FAKE_AUDIO=1 时播放=写文件。"""

    def test_fake_mode_writes_file_instead_of_playing(self, fake_sd, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_VOICE_FAKE_AUDIO", "1")
        monkeypatch.setenv("JARVIS_VOICE_FAKE_AUDIO_DIR", str(tmp_path))
        wav = make_wav_bytes(sine_chunk(), sr=SR)
        p = Player()
        p.play_wav(wav, blocking=True)
        # 不碰 sounddevice
        assert fake_sd.played == [] and fake_sd.wait_calls == 0
        # 写出的文件内容与输入一致
        assert p.last_fake_path is not None
        out = tmp_path / p.last_fake_path.split("/")[-1]
        assert out.exists()
        assert out.read_bytes() == wav
        # 假模式下 playing 恒 False、stop 不出错
        assert p.playing is False
        p.stop()
        assert fake_sd.stop_calls == 0

    def test_fake_mode_accepts_path_input(self, fake_sd, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_VOICE_FAKE_AUDIO", "1")
        monkeypatch.setenv("JARVIS_VOICE_FAKE_AUDIO_DIR", str(tmp_path / "out"))
        src = tmp_path / "src.wav"
        wav = make_wav_bytes(sine_chunk())
        src.write_bytes(wav)
        p = Player()
        p.play_wav(str(src))
        from pathlib import Path
        assert Path(p.last_fake_path).read_bytes() == wav

    def test_fake_mode_paths_unique(self, fake_sd, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_VOICE_FAKE_AUDIO", "1")
        monkeypatch.setenv("JARVIS_VOICE_FAKE_AUDIO_DIR", str(tmp_path))
        p = Player()
        wav = make_wav_bytes(sine_chunk())
        p.play_wav(wav)
        first = p.last_fake_path
        p.play_wav(wav)
        assert p.last_fake_path != first


# ---------------------------------------------------------------------------
# record_until_silence（合成数组 + FakeVAD，验证截断逻辑）
# ---------------------------------------------------------------------------

class EnergyVAD:
    """确定性假 VAD：平均绝对幅度 > 100 视为语音。"""

    def is_speech(self, chunk) -> bool:
        return float(np.abs(np.asarray(chunk, dtype=np.int32)).mean()) > 100


class TestRecordUntilSilence:
    def test_stops_after_silence_and_trims_tail(self):
        speech = [sine_chunk() for _ in range(5)]
        stream = iter(speech + [silence_chunk() for _ in range(20)])
        out = record_until_silence(stream, EnergyVAD(), max_sec=15.0, silence_ms=240)
        # 连续 240ms（3 块）静音即截断，且尾部静音被裁掉
        assert out.dtype == np.int16
        assert np.array_equal(out, np.concatenate(speech))

    def test_no_speech_returns_empty(self):
        stream = iter([silence_chunk() for _ in range(50)])
        out = record_until_silence(stream, EnergyVAD(), max_sec=15.0, silence_ms=240)
        assert out.size == 0

    def test_max_sec_cap(self):
        def forever():
            while True:
                yield sine_chunk()

        out = record_until_silence(forever(), EnergyVAD(), max_sec=0.5, silence_ms=900)
        assert out.size > 0
        # 允许末块溢出一块（80ms）
        assert out.size <= int((0.5 + 0.08) * SR)

    def test_pre_roll_prepended(self):
        pre = sine_chunk(freq=200.0)  # 频率不同便于核对开头
        stream = iter([sine_chunk(freq=440.0)] + [silence_chunk() for _ in range(10)])
        out = record_until_silence(stream, EnergyVAD(), silence_ms=240, pre_roll=[pre])
        assert np.array_equal(out[:BLOCK], pre)
        assert out.size == 2 * BLOCK  # pre_roll + 1 块语音，尾部静音裁掉

    def test_silence_run_in_middle_kept(self):
        # 中间短静音（未达 silence_ms）不截断、不丢失
        s1, s2 = sine_chunk(), sine_chunk(freq=300.0)
        stream = iter([s1, silence_chunk(), s2] + [silence_chunk() for _ in range(10)])
        out = record_until_silence(stream, EnergyVAD(), silence_ms=240)
        assert out.size == 3 * BLOCK
        assert np.array_equal(out[2 * BLOCK:], s2)


# ---------------------------------------------------------------------------
# SileroVAD（真模型，包内置无需下载；不出声）
# ---------------------------------------------------------------------------

class TestSileroVADReal:
    def test_silence_false_and_noisy_speechlike_no_crash(self):
        vad = SileroVAD()
        # 纯静音必须判 False
        assert vad.is_speech(np.zeros(BLOCK, dtype=np.int16)) is False
        # 白噪+正弦混合"类语音"样本：只要求不崩、返回 bool（不断言 True）
        rng = np.random.default_rng(42)
        noisy = (rng.normal(0, 1000, BLOCK * 4)
                 + np.sin(2 * np.pi * 220 * np.arange(BLOCK * 4) / SR) * 6000)
        result = vad.is_speech(noisy.astype(np.int16))
        assert isinstance(result, bool)
        # reset 与零长输入不崩
        vad.reset()
        assert vad.is_speech(np.zeros(0, dtype=np.int16)) is False
        # float32 输入（-1..1）同样接受
        assert isinstance(vad.is_speech(np.zeros(512, dtype=np.float32)), bool)

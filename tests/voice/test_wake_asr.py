# -*- coding: utf-8 -*-
"""voice/wake.py + voice/asr.py 真模型测试（V1）。

计划第2节：真模型加载各 1 条（模型在 models/ 下，不进 git），
断言可推理出类型正确的结果，不断言识别准确率。不碰麦克风/扬声器。
"""
import wave
from pathlib import Path

import numpy as np

from voice.asr import Transcriber
from voice.wake import WakeDetector

SR = 16000
BLOCK = 1280  # 80ms 帧（块大小任意，KWS 内部自动攒帧）
_FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_wake_detector_real_model():
    """sherpa-onnx KWS 真模型：喂静音帧得到 0.0/1.0 浮点分；reset 可用。"""
    det = WakeDetector(threshold=0.25)
    assert det.threshold == 0.25
    scores = [det.feed(np.zeros(BLOCK, dtype=np.int16)) for _ in range(5)]
    for s in scores:
        assert isinstance(s, float)
        assert 0.0 <= s <= 1.0
    # 静音不该触发唤醒
    assert max(scores) < det.threshold
    det.reset()
    # reset 后还能继续喂；float 输入也接受（内部转 float32 归一）
    t = np.arange(BLOCK) / SR
    s = det.feed((np.sin(2 * np.pi * 440 * t) * 0.3).astype(np.float32))
    assert isinstance(s, float) and 0.0 <= s <= 1.0


def test_wake_detector_hits_mumu():
    """合成『木木』fixture 必须触发唤醒；命中后 reset 不影响继续喂流。"""
    det = WakeDetector(threshold=0.25)
    with wave.open(str(_FIXTURES / "mumu_16k.wav")) as wf:
        assert wf.getframerate() == SR and wf.getnchannels() == 1
        samples = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
    # KWS 离线喂入需尾部补静音（tail padding），否则末尾帧不出解码结果
    audio = np.concatenate([samples, np.zeros(SR, dtype=np.int16)])
    best = 0.0
    for i in range(0, len(audio) - BLOCK, BLOCK):
        best = max(best, det.feed(audio[i:i + BLOCK]))
    assert best == 1.0, "合成『木木』音频未触发唤醒"
    det.reset()
    assert det.feed(np.zeros(BLOCK, dtype=np.int16)) == 0.0


def test_transcriber_real_model(tmp_path):
    """faster-whisper large-v3-turbo（cpu/int8）真模型：int16/float32/文件三种入口都返回 str。"""
    tr = Transcriber("large-v3-turbo")

    # int16 一维数组（1 秒静音）→ str（不断言内容）
    text = tr.transcribe(np.zeros(SR, dtype=np.int16), language="zh")
    assert isinstance(text, str)

    # float32 输入同样接受
    t = np.arange(SR // 2) / SR
    sine = (np.sin(2 * np.pi * 440 * t) * 0.3).astype(np.float32)
    assert isinstance(tr.transcribe(sine, language="zh"), str)

    # transcribe_file：临时 wav 文件
    f = tmp_path / "t.wav"
    with wave.open(str(f), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes((sine * 32767).astype(np.int16).tobytes())
    assert isinstance(tr.transcribe_file(str(f), language="zh"), str)

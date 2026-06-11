# -*- coding: utf-8 -*-
"""语音转写（契约：Phase 2 计划 1.4 节，faster-whisper，device=cpu compute_type=int8）。

模型已由脚手架预下载到 HuggingFace 缓存（走 hf-mirror）；
构造时先 local_files_only=True 用本地缓存，缺文件才回退在线下载，测试/离线都不卡网络。
"""
import numpy as np


class Transcriber:
    """faster-whisper 封装：np 数组与文件两种入口，统一返回拼接后的纯文本。"""

    def __init__(self, model_name: str):
        from faster_whisper import WhisperModel  # 懒加载重依赖
        kwargs = dict(device="cpu", compute_type="int8")
        try:
            # 优先只用本地缓存（脚手架已预下载），避免测试时碰网络
            self._model = WhisperModel(model_name, local_files_only=True, **kwargs)
        except Exception:
            self._model = WhisperModel(model_name, **kwargs)

    def transcribe(self, audio: np.ndarray, language: str = "zh") -> str:
        """转写 16k 音频数组；int16 / float32（-1..1）均接受。"""
        arr = np.asarray(audio).reshape(-1)
        if arr.dtype == np.int16:
            arr = arr.astype(np.float32) / 32768.0
        else:
            arr = arr.astype(np.float32)
        segments, _info = self._model.transcribe(arr, language=language)
        return "".join(seg.text for seg in segments).strip()

    def transcribe_file(self, path: str, language: str = "zh") -> str:
        """转写音频文件（wav/webm 等，解码靠 faster-whisper 自带 av）。"""
        segments, _info = self._model.transcribe(str(path), language=language)
        return "".join(seg.text for seg in segments).strip()

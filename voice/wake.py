# -*- coding: utf-8 -*-
"""唤醒词检测（契约：Phase 2 计划 1.4 节，openwakeword hey_jarvis）。

模型文件已由脚手架预置在 openwakeword 包 resources/models/ 下，加载不联网。
macOS 上无 tflite_runtime，固定走 onnx 推理（onnxruntime 已装）。
"""
import numpy as np


class WakeDetector:
    """hey_jarvis 唤醒检测：feed 一帧返回当帧最高分，阈值比较由调用方做。"""

    def __init__(self, threshold: float):
        from openwakeword.model import Model  # 懒加载，避免 import 即拉起 onnxruntime
        self.threshold = float(threshold)
        self._model = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")

    def feed(self, chunk: np.ndarray) -> float:
        """喂一帧 16k int16 音频（推荐 1280 样本=80ms），返回当帧所有唤醒词里的最高分。"""
        arr = np.asarray(chunk).reshape(-1)
        if arr.dtype != np.int16:
            if np.issubdtype(arr.dtype, np.floating):
                # float（-1..1）→ int16
                arr = (np.clip(arr, -1.0, 1.0) * 32767.0).astype(np.int16)
            else:
                arr = arr.astype(np.int16)
        prediction = self._model.predict(arr)
        if not prediction:
            return 0.0
        return float(max(prediction.values()))

    def reset(self) -> None:
        """清预测缓冲与特征缓存（唤醒成功转入录音前调用，防回声二次触发）。"""
        self._model.reset()

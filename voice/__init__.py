# -*- coding: utf-8 -*-
"""木木语音侧组件包（运行于 .venv-voice，python3.12）。

模块一览（契约见 docs/superpowers/plans/2026-06-11-jarvis-phase2-voice.md 1.4/1.5）：
- voice.audio  : Recorder / Player / SileroVAD / record_until_silence（sounddevice 16k 单声道 int16）
- voice.wake   : WakeDetector（sherpa-onnx KWS，中文唤醒词"木木"）
- voice.asr    : Transcriber（faster-whisper, cpu/int8）
- voice.daemon : 语音守护状态机（V2）
- voice.client : 对 jarvis-server 的 REST/WS 客户端（V2）
- voice.acks   : 固定应答语与 TTS 缓存（V2）
- voice.tts_worker : IndexTTS-2 工作进程（V3，跑在 index-tts 自带 venv，仅 stdlib）

注意：此处不做任何子模块导入，避免在只用部分功能时拖入 torch 等重依赖。
"""

__all__ = ["audio", "wake", "asr"]

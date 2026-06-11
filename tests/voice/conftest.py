"""tests/voice 收集守卫（集成阶段加）。

语音测试依赖 .venv-voice（sounddevice/openwakeword/faster-whisper 等），
服务侧 .venv 没装这些包。为了让计划第 3 节的
`.venv/bin/python -m pytest tests/ -q` 能一把全绿，
依赖缺失时整目录跳过收集；语音测试请用：
    .venv-voice/bin/python -m pytest tests/voice/ -q
"""
import importlib.util

if importlib.util.find_spec("sounddevice") is None:
    collect_ignore_glob = ["test_*.py"]

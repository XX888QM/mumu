"""贾维斯语音应答语料与本地 TTS 缓存（Phase 2 契约 1.5，锁定）。

- ACKS：各场景应答句池（文案锁定，不得改动）
- ensure_cache(tts_fn)：非模板句逐句合成存 data/voice_cache/<md5>.wav，
  返回 key → wav 路径列表；已存在的文件直接复用不重复合成。
- pick(cache, key)：从缓存池随机取一条 wav 路径；
  带 {action} 模板的句子不缓存，由调用方实时合成（见 template_text）。

注意：本模块运行在 .venv-voice（无 python-dotenv，不 import jarvis.config）；
voice_cache 目录为契约 1.2 的派生路径 <root>/data/voice_cache，直接由文件位置推导。
"""
from __future__ import annotations

import hashlib
import random
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
# 契约 1.2：settings.voice_cache_dir = <root>/data/voice_cache（派生路径，非环境变量）
DEFAULT_CACHE_DIR = _ROOT / "data" / "voice_cache"

# 契约 1.5 锁定文案
ACKS = {
    "wake": ["在", "大哥请讲"],
    "accept": ["好的大哥，这就办", "收到，马上处理"],
    "busy": ["上一件事还没办完，稍等"],
    "approval_prompt": ["大哥，需要授权：{action}。批准还是拒绝？"],
    "approval_unclear": ["没听清，批准还是拒绝？"],
    "approval_giveup": ["那大哥稍后在控制台处理"],
    "fail": ["任务出岔子了，详情在控制台"],
}


def _is_template(text: str) -> bool:
    """带 {action} 占位符的模板句：不缓存，实时合成。"""
    return "{action}" in text


def ensure_cache(tts_fn, cache_dir=None) -> dict[str, list[str]]:
    """逐句合成应答语并缓存到磁盘，返回 key → wav 绝对路径列表。

    tts_fn(text:str) -> bytes：合成函数（通常是 JarvisClient.tts）。
    已存在的缓存文件直接复用；模板句跳过（对应 key 的列表可能为空）。
    """
    directory = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
    directory.mkdir(parents=True, exist_ok=True)

    cache: dict[str, list[str]] = {}
    for key, phrases in ACKS.items():
        paths: list[str] = []
        for text in phrases:
            if _is_template(text):
                continue
            name = hashlib.md5(text.encode("utf-8")).hexdigest() + ".wav"
            path = directory / name
            if not path.exists():
                data = tts_fn(text)
                if not data:
                    raise RuntimeError(f"TTS 返回空数据，拒绝写出空缓存：{text}")
                path.write_bytes(data)
            paths.append(str(path))
        cache[key] = paths
    return cache


def pick(cache: dict[str, list[str]], key: str) -> str:
    """随机取一条该场景的缓存 wav 路径。

    模板句（如 approval_prompt）没有缓存 → 抛 KeyError，调用方应实时合成。
    """
    paths = cache.get(key) or []
    if not paths:
        raise KeyError(f"应答场景 {key!r} 无缓存（模板句需实时合成或缓存未生成）")
    return random.choice(paths)


def template_text(key: str, **fields) -> str:
    """随机取一条该场景的原始句并填充模板字段（实时合成用）。"""
    return random.choice(ACKS[key]).format(**fields)

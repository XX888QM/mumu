"""voice/acks.py 单元测试（契约 1.5：ACKS / ensure_cache / pick）。

全部用假 tts_fn，不起真 TTS 模型、不碰真设备。
"""
import hashlib
from pathlib import Path

import pytest

from voice import acks


def fake_tts(text: str) -> bytes:
    """假 TTS：返回带 RIFF 头的伪 wav 字节。"""
    return b"RIFF" + text.encode("utf-8")


def all_plain_phrases() -> list[str]:
    """ACKS 中所有非模板句（不含 {action} 的句子）。"""
    return [t for phrases in acks.ACKS.values() for t in phrases if "{action}" not in t]


# ---------------------------------------------------------------------------
# ACKS 契约内容
# ---------------------------------------------------------------------------

def test_acks_keys_locked():
    """ACKS 的 key 集合与契约 1.5 完全一致。"""
    assert set(acks.ACKS) == {
        "wake", "accept", "busy", "approval_prompt",
        "approval_unclear", "approval_giveup", "fail",
    }


def test_acks_locked_phrases():
    """逐条核对锁定文案。"""
    assert acks.ACKS["wake"] == ["在", "大哥请讲"]
    assert acks.ACKS["accept"] == ["好的大哥，这就办", "收到，马上处理"]
    assert acks.ACKS["busy"] == ["上一件事还没办完，稍等"]
    assert acks.ACKS["approval_prompt"] == ["大哥，需要授权：{action}。批准还是拒绝？"]
    assert acks.ACKS["approval_unclear"] == ["没听清，批准还是拒绝？"]
    assert acks.ACKS["approval_giveup"] == ["那大哥稍后在控制台处理"]
    assert acks.ACKS["fail"] == ["任务出岔子了，详情在控制台"]


# ---------------------------------------------------------------------------
# ensure_cache
# ---------------------------------------------------------------------------

def test_ensure_cache_creates_md5_wav_files(tmp_path):
    """非模板句逐句合成落盘，文件名为 md5(<句子>).wav。"""
    calls: list[str] = []

    def tts(text: str) -> bytes:
        calls.append(text)
        return fake_tts(text)

    cache = acks.ensure_cache(tts, cache_dir=tmp_path)

    # 所有非模板句都被合成（每句恰好一次）
    assert sorted(calls) == sorted(all_plain_phrases())
    for key, phrases in acks.ACKS.items():
        plain = [t for t in phrases if "{action}" not in t]
        assert len(cache[key]) == len(plain)
        for p in cache[key]:
            path = Path(p)
            assert path.exists()
            assert path.suffix == ".wav"
            assert path.parent == tmp_path
    # md5 命名核对
    first = acks.ACKS["wake"][0]
    expect = tmp_path / (hashlib.md5(first.encode("utf-8")).hexdigest() + ".wav")
    assert str(expect) in cache["wake"]
    # 文件内容就是 tts_fn 返回的字节
    assert expect.read_bytes() == fake_tts(first)


def test_ensure_cache_reuses_existing_files(tmp_path):
    """第二次调用复用已有缓存，不再触发 tts_fn。"""
    acks.ensure_cache(fake_tts, cache_dir=tmp_path)

    second_calls: list[str] = []

    def counting_tts(text: str) -> bytes:
        second_calls.append(text)
        return fake_tts(text)

    cache = acks.ensure_cache(counting_tts, cache_dir=tmp_path)
    assert second_calls == []                       # 全部命中缓存
    assert sum(len(v) for v in cache.values()) == len(all_plain_phrases())


def test_ensure_cache_skips_template_phrases(tmp_path):
    """带 {action} 模板的句子不缓存（实时合成）。"""
    cache = acks.ensure_cache(fake_tts, cache_dir=tmp_path)
    assert cache["approval_prompt"] == []
    # 缓存目录里没有模板句对应的文件
    template = acks.ACKS["approval_prompt"][0]
    md5name = hashlib.md5(template.encode("utf-8")).hexdigest() + ".wav"
    assert not (tmp_path / md5name).exists()


def test_ensure_cache_rejects_empty_tts_output(tmp_path):
    """tts_fn 返回空字节视为失败，不允许写出空 wav。"""
    with pytest.raises(Exception):
        acks.ensure_cache(lambda text: b"", cache_dir=tmp_path)


# ---------------------------------------------------------------------------
# pick / template_text
# ---------------------------------------------------------------------------

def test_pick_returns_random_cached_path(tmp_path):
    cache = acks.ensure_cache(fake_tts, cache_dir=tmp_path)
    seen = {acks.pick(cache, "wake") for _ in range(30)}
    assert seen <= set(cache["wake"])
    assert len(cache["wake"]) == 2          # "在" / "大哥请讲" 两条都在池里


def test_pick_raises_for_template_or_missing_key(tmp_path):
    cache = acks.ensure_cache(fake_tts, cache_dir=tmp_path)
    with pytest.raises(KeyError):
        acks.pick(cache, "approval_prompt")  # 模板句不缓存 → 实时合成
    with pytest.raises(KeyError):
        acks.pick(cache, "no_such_key")


def test_template_text_fills_action():
    text = acks.template_text("approval_prompt", action="删除测试文件")
    assert "删除测试文件" in text
    assert "{action}" not in text
    assert text.startswith("大哥，需要授权：")

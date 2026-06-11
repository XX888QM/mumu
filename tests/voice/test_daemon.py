"""voice/daemon.py 状态机测试（契约 1.5 逐转移断言）。

全部用 Fake（FakeClient/FakePlayer/FakeTranscriber/FakeWake/FakeVAD + 假 record_fn），
不碰真实麦克风/扬声器/模型。
"""
import time

import numpy as np
import pytest

from voice import acks
from voice.daemon import VoiceDaemon, classify_decision, summarize


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakePlayer:
    def __init__(self):
        self.played: list = []      # play_wav 收到的 path/bytes 序列
        self.blocking_flags: list = []  # 每次 play_wav 的 blocking 参数
        self.stop_calls = 0
        self._playing = False
        self.playing_polls = 0      # >0 时 playing 先返回 True 并递减（模拟播放尚未结束）

    def play_wav(self, path_or_bytes, blocking=False):
        self.played.append(path_or_bytes)
        self.blocking_flags.append(blocking)

    def stop(self):
        self.stop_calls += 1
        self._playing = False

    @property
    def playing(self):
        if self.playing_polls > 0:
            self.playing_polls -= 1
            return True
        return self._playing


class FakeClient:
    def __init__(self, busy=False):
        self.busy = busy
        self.chats: list[str] = []
        self.decisions: list[tuple] = []
        self.tts_texts: list[str] = []
        self.on_event = None

    def chat(self, message):
        self.chats.append(message)
        if self.busy:
            return {"busy": True}
        return {"task_id": f"task-{len(self.chats)}", "session_id": "s-voice"}

    def decide(self, approval_id, decision):
        self.decisions.append((approval_id, decision))
        return True

    def tts(self, text):
        self.tts_texts.append(text)
        return b"RIFF-fake-" + text.encode("utf-8")

    def listen(self, on_event):
        self.on_event = on_event

    @property
    def session_id(self):
        return "s-voice"


class FakeWake:
    def __init__(self, scores=()):
        self.scores = list(scores)
        self.feed_calls = 0
        self.reset_calls = 0

    def feed(self, chunk):
        self.feed_calls += 1
        return self.scores.pop(0) if self.scores else 0.0

    def reset(self):
        self.reset_calls += 1


class FakeTranscriber:
    def __init__(self, texts=()):
        self.texts = list(texts)
        self.calls: list[tuple] = []    # (样本数, language)

    def transcribe(self, audio, language="zh"):
        self.calls.append((len(audio), language))
        return self.texts.pop(0) if self.texts else ""


class FakeVAD:
    def __init__(self, speech=False):
        self.speech = speech            # bool 或 list[bool]（按调用顺序弹出）

    def is_speech(self, chunk):
        if isinstance(self.speech, list):
            return self.speech.pop(0) if self.speech else False
        return self.speech


def make_record_fn(audio_seq):
    """假 record_until_silence：按序返回预置音频，并记录 pre_roll 参数。"""
    seq = list(audio_seq)
    calls: list[dict] = []

    def record(stream, vad, max_sec=15.0, silence_ms=900, pre_roll=None):
        calls.append({"stream": stream, "vad": vad, "pre_roll": pre_roll})
        return seq.pop(0) if seq else np.zeros(0, dtype=np.int16)

    record.calls = calls
    return record


SPEECH_1S = np.ones(16000, dtype=np.int16)          # 1 秒语音（>0.4s 下限）
SHORT_BLIP = np.ones(1000, dtype=np.int16)          # 过短（<0.4s*16000=6400）
LISTEN_CHUNK = np.zeros(1280, dtype=np.int16)       # 80ms 监听块


@pytest.fixture
def cache(tmp_path):
    """真 ensure_cache + 假 tts 生成的缓存（顺带覆盖二者协作）。"""
    return acks.ensure_cache(lambda t: b"RIFF" + t.encode("utf-8"), cache_dir=tmp_path)


def make_daemon(cache, *, client=None, player=None, wake=None, transcriber=None,
                vad=None, record_fn=None, threshold=0.5):
    client = client or FakeClient()
    player = player or FakePlayer()
    wake = wake or FakeWake()
    transcriber = transcriber if transcriber is not None else FakeTranscriber(["打开客厅的灯"])
    vad = vad or FakeVAD(False)
    record_fn = record_fn or make_record_fn([SPEECH_1S])
    d = VoiceDaemon(client=client, player=player, wake=wake, transcriber=transcriber,
                    vad=vad, record_fn=record_fn, cache=cache, wake_threshold=threshold)
    return d


# ---------------------------------------------------------------------------
# 主链路：唤醒 → ack → 录音 → 转写 → chat → accept
# ---------------------------------------------------------------------------

def test_wake_to_chat_happy_path(cache):
    wake = FakeWake([0.2, 0.9])
    record_fn = make_record_fn([SPEECH_1S])
    tr = FakeTranscriber(["打开客厅的灯"])
    client = FakeClient()
    player = FakePlayer()
    d = make_daemon(cache, client=client, player=player, wake=wake,
                    transcriber=tr, record_fn=record_fn)

    # 第一块分 0.2 < 0.5 → 留在 LISTEN
    assert d.process_chunk(LISTEN_CHUNK) == "listen"
    assert client.chats == []
    # 第二块分 0.9 > 0.5 → WAKE → RECORD → TRANSCRIBE → DISPATCH
    assert d.process_chunk(LISTEN_CHUNK) == "wake"

    # 播了 wake ack（取自缓存池）
    assert player.played[0] in cache["wake"]
    # 唤醒后 reset 检测器、清 pre_roll
    assert wake.reset_calls == 1
    assert len(d._pre_roll) == 0
    # RECORD 带 pre_roll（监听期缓存块，含触发块本身）
    assert record_fn.calls[0]["pre_roll"]
    assert len(record_fn.calls[0]["pre_roll"]) >= 1
    # TRANSCRIBE 用中文
    assert tr.calls == [(16000, "zh")]
    # chat 收到转写文本
    assert client.chats == ["打开客厅的灯"]
    # 成功 → 播 accept ack → 回 LISTEN
    assert player.played[1] in cache["accept"]
    assert d.active_task_id == "task-1"
    assert d.pending_approval is None


def test_busy_path_plays_busy_ack(cache):
    client = FakeClient(busy=True)
    player = FakePlayer()
    d = make_daemon(cache, client=client, player=player)

    assert d.dispatch("查一下天气") == "busy"
    assert client.chats == ["查一下天气"]
    assert player.played[-1] in cache["busy"]
    assert d.active_task_id is None


def test_record_too_short_back_to_listen(cache):
    """RECORD 产物 <0.4s → 不转写不派发，直接回 LISTEN。"""
    tr = FakeTranscriber(["不该被调用"])
    client = FakeClient()
    record_fn = make_record_fn([SHORT_BLIP])
    d = make_daemon(cache, client=client, transcriber=tr, record_fn=record_fn)

    assert d.run_interaction() is None
    assert tr.calls == []
    assert client.chats == []


def test_record_empty_transcript_back_to_listen(cache):
    """转写为空白 → 不派发。"""
    tr = FakeTranscriber(["   "])
    client = FakeClient()
    d = make_daemon(cache, client=client, transcriber=tr)

    assert d.run_interaction() is None
    assert client.chats == []


# ---------------------------------------------------------------------------
# 授权三路径
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("批准", "approved"), ("我同意", "approved"), ("可以的", "approved"), ("行", "approved"),
    ("拒绝", "denied"), ("不行", "denied"), ("取消吧", "denied"), ("否", "denied"),
    ("嗯哼", None), ("今天天气不错", None),
])
def test_classify_decision(text, expected):
    assert classify_decision(text) == expected


def test_approval_approved(cache):
    client = FakeClient()
    d = make_daemon(cache, client=client)
    d.pending_approval = {"id": "ap1", "action": "删除测试文件"}

    assert d.dispatch("批准") == "approved"
    assert client.decisions == [("ap1", "approved")]
    assert d.pending_approval is None
    assert client.chats == []                       # 不会误走 chat


def test_approval_denied_deny_wins_over_xing(cache):
    """'不行' 同时含拒绝词'不行'与批准词'行'，必须判 denied。"""
    client = FakeClient()
    d = make_daemon(cache, client=client)
    d.pending_approval = {"id": "ap2", "action": "重启服务器"}

    assert d.dispatch("不行") == "denied"
    assert client.decisions == [("ap2", "denied")]
    assert d.pending_approval is None


def test_approval_unclear_then_approved(cache):
    """第一次没听清 → 追问一次 → 听清批准。"""
    client = FakeClient()
    player = FakePlayer()
    tr = FakeTranscriber(["同意"])                   # 追问后录到的回答
    record_fn = make_record_fn([SPEECH_1S])
    d = make_daemon(cache, client=client, player=player,
                    transcriber=tr, record_fn=record_fn)
    d.pending_approval = {"id": "ap3", "action": "发邮件"}

    assert d.dispatch("呃这个嘛") == "approved"
    assert player.played[0] in cache["approval_unclear"]
    assert client.decisions == [("ap3", "approved")]
    assert d.pending_approval is None


def test_approval_unclear_twice_gives_up(cache):
    """两次都不清 → 播放弃语，清 pending（红卡仍在控制台）。"""
    client = FakeClient()
    player = FakePlayer()
    tr = FakeTranscriber(["嗯嗯啊啊"])               # 追问后依旧含混
    record_fn = make_record_fn([SPEECH_1S])
    d = make_daemon(cache, client=client, player=player,
                    transcriber=tr, record_fn=record_fn)
    d.pending_approval = {"id": "ap4", "action": "清空日志"}

    assert d.dispatch("那个什么") == "giveup"
    assert player.played[0] in cache["approval_unclear"]
    assert player.played[1] in cache["approval_giveup"]
    assert client.decisions == []                   # 没有做出任何决定
    assert d.pending_approval is None


# ---------------------------------------------------------------------------
# WS 事件：task_done / approval_request
# ---------------------------------------------------------------------------

def test_task_done_speaks_summary(cache):
    client = FakeClient()
    player = FakePlayer()
    d = make_daemon(cache, client=client, player=player)
    d.active_task_id = "t1"

    result_md = "# 报告\n**已完成**灯光控制。客厅灯已打开！\n- 其余细节略。"
    d.handle_event({"type": "task_done", "task_id": "t1",
                    "status": "done", "result": result_md})
    d.wait_speech(timeout=5)                        # 播报在后台线程（审查修复：不阻塞主循环）

    # 摘要 = 去 markdown 取前两句 → tts → 播
    assert client.tts_texts == [summarize(result_md)]
    spoken = client.tts_texts[0]
    assert "灯光控制" in spoken and "客厅灯已打开" in spoken
    assert "#" not in spoken and "*" not in spoken
    assert "其余细节略" not in spoken                # 第三句被截掉
    assert player.played[-1] == b"RIFF-fake-" + spoken.encode("utf-8")
    assert d.active_task_id is None


def test_task_done_other_task_ignored(cache):
    client = FakeClient()
    player = FakePlayer()
    d = make_daemon(cache, client=client, player=player)
    d.active_task_id = "t1"

    d.handle_event({"type": "task_done", "task_id": "other",
                    "status": "done", "result": "别的会话的结果。"})
    d.wait_speech(timeout=5)
    assert client.tts_texts == []
    assert player.played == []
    assert d.active_task_id == "t1"                 # 仍在等自己的任务


def test_task_failed_plays_fail_ack(cache):
    player = FakePlayer()
    d = make_daemon(cache, player=player)
    d.active_task_id = "t2"

    d.handle_event({"type": "task_done", "task_id": "t2",
                    "status": "failed", "result": ""})
    assert player.played[-1] in cache["fail"]
    assert d.active_task_id is None


def test_approval_request_event_prompts_and_records(cache):
    """approval_request → 实时合成 approval_prompt(action) →（阻塞）播完 → 主循环进 RECORD。

    审查修复回归：合成+播放挪到后台线程（不阻塞主循环），播放用 blocking=True
    防回声；播完后由主循环下一块免唤醒进入 RECORD 听决定。
    """
    client = FakeClient()
    player = FakePlayer()
    tr = FakeTranscriber(["批准"])
    record_fn = make_record_fn([SPEECH_1S])
    d = make_daemon(cache, client=client, player=player,
                    transcriber=tr, record_fn=record_fn)

    d.handle_event({"type": "approval_request",
                    "approval": {"id": "ap9", "action": "git push 到生产"}})
    d.wait_speech(timeout=5)

    # 模板句不走缓存：实时合成且文本含 action
    assert len(client.tts_texts) == 1
    assert "git push 到生产" in client.tts_texts[0]
    assert player.played[0] == b"RIFF-fake-" + client.tts_texts[0].encode("utf-8")
    # 防回声：提示必须阻塞播放（播完才进 RECORD）
    assert player.blocking_flags[0] is True
    # handle_event 本身不录音；主循环下一块才免唤醒进 RECORD
    assert record_fn.calls == []
    assert d.process_chunk(LISTEN_CHUNK) == "decision"
    assert record_fn.calls
    assert client.decisions == [("ap9", "approved")]
    assert d.pending_approval is None
    # 决定标志只消费一次：后续块回到正常 LISTEN
    assert d.process_chunk(LISTEN_CHUNK) == "listen"


def test_ws_events_flow_through_queue(cache):
    """on_ws_event（listen 线程回调）入队，drain_events 在主循环消费。"""
    client = FakeClient()
    player = FakePlayer()
    d = make_daemon(cache, client=client, player=player)
    d.active_task_id = "t3"

    d.on_ws_event({"type": "auth_ok"})              # 未知类型应被安全忽略
    d.on_ws_event({"type": "task_done", "task_id": "t3",
                   "status": "done", "result": "搞定了。"})
    d.drain_events()
    d.wait_speech(timeout=5)
    assert client.tts_texts == ["搞定了。"]


# ---------------------------------------------------------------------------
# 打断路径
# ---------------------------------------------------------------------------

def test_interrupt_while_playing(cache):
    """播放中 VAD 检出人声 → stop → 免唤醒直接 RECORD。"""
    client = FakeClient()
    player = FakePlayer()
    player._playing = True
    wake = FakeWake([0.9])                          # 不应被消费
    vad = FakeVAD(True)
    tr = FakeTranscriber(["换个歌"])
    record_fn = make_record_fn([SPEECH_1S])
    d = make_daemon(cache, client=client, player=player, wake=wake,
                    vad=vad, transcriber=tr, record_fn=record_fn)

    assert d.process_chunk(LISTEN_CHUNK) == "interrupt"
    assert player.stop_calls == 1                   # 立即停播
    assert wake.feed_calls == 0                     # 免唤醒：没喂检测器
    assert client.chats == ["换个歌"]               # 直接 RECORD → DISPATCH


def test_playing_without_speech_keeps_listening(cache):
    """播放中无人声 → 不打断、不喂唤醒检测器。"""
    player = FakePlayer()
    player._playing = True
    wake = FakeWake([0.9])
    d = make_daemon(cache, player=player, wake=wake, vad=FakeVAD(False))

    assert d.process_chunk(LISTEN_CHUNK) == "listen"
    assert player.stop_calls == 0
    assert wake.feed_calls == 0


# ---------------------------------------------------------------------------
# play_ack 缓存缺失兜底
# ---------------------------------------------------------------------------

def test_play_ack_falls_back_to_realtime_tts(cache):
    client = FakeClient()
    player = FakePlayer()
    d = make_daemon({}, client=client, player=player)   # 空缓存

    d.play_ack("busy")
    assert client.tts_texts == [acks.ACKS["busy"][0]]   # busy 池只有一条
    assert isinstance(player.played[0], bytes)


# ---------------------------------------------------------------------------
# 审查修复回归：回声防护（录音前等播放结束）+ 主循环不被 TTS 阻塞
# ---------------------------------------------------------------------------

def test_approval_unclear_waits_playback_before_retry_record(cache):
    """授权追问 ack 播完之前不得开录——否则麦克风录到扬声器回声里的
    "批准还是拒绝"，ASR 命中关键词会未经大哥开口就误决裁（审查 high）。"""
    client = FakeClient()
    player = FakePlayer()
    tr = FakeTranscriber(["同意"])
    playing_at_record: list = []

    def record(stream, vad, max_sec=15.0, silence_ms=900, pre_roll=None):
        playing_at_record.append(player.playing)   # 录音开始时必须已停播
        return SPEECH_1S

    d = make_daemon(cache, client=client, player=player,
                    transcriber=tr, record_fn=record)
    d.pending_approval = {"id": "ap5", "action": "发邮件"}
    player.playing_polls = 3                       # 模拟追问语音还要 3 次轮询才播完

    assert d.dispatch("呃这个嘛") == "approved"
    assert playing_at_record == [False]
    assert client.decisions == [("ap5", "approved")]


def test_wait_playback_has_timeout(cache):
    """播放状态卡死也不能无限等：超时上限后继续。"""
    player = FakePlayer()
    player._playing = True                         # 永远"在播"
    d = make_daemon(cache, player=player)
    t0 = time.monotonic()
    d._wait_playback(timeout=0.2)
    assert time.monotonic() - t0 < 1.0             # 没有挂死


def test_task_done_summary_does_not_block_main_loop(cache):
    """task_done 摘要的 TTS 合成慢时不得阻塞主音频循环（审查 high：
    阻塞期间 InputStream 缓冲溢出、唤醒/打断全部失效）。"""
    class SlowTtsClient(FakeClient):
        def tts(self, text):
            time.sleep(0.5)
            return super().tts(text)

    client = SlowTtsClient()
    player = FakePlayer()
    d = make_daemon(cache, client=client, player=player)
    d.active_task_id = "t9"

    t0 = time.monotonic()
    d.handle_event({"type": "task_done", "task_id": "t9",
                    "status": "done", "result": "慢合成的结果。"})
    assert time.monotonic() - t0 < 0.3             # handle_event 立即返回
    d.wait_speech(timeout=5)
    assert client.tts_texts == ["慢合成的结果。"]
    assert player.played[-1] == b"RIFF-fake-" + "慢合成的结果。".encode("utf-8")


def test_approval_request_does_not_block_main_loop(cache):
    """approval_request 的提示合成同样挪到后台线程：handle_event 立即返回。"""
    class SlowTtsClient(FakeClient):
        def tts(self, text):
            time.sleep(0.5)
            return super().tts(text)

    client = SlowTtsClient()
    player = FakePlayer()
    tr = FakeTranscriber(["批准"])
    record_fn = make_record_fn([SPEECH_1S])
    d = make_daemon(cache, client=client, player=player,
                    transcriber=tr, record_fn=record_fn)

    t0 = time.monotonic()
    d.handle_event({"type": "approval_request",
                    "approval": {"id": "ap10", "action": "rm -rf 临时目录"}})
    assert time.monotonic() - t0 < 0.3
    # pending 立即生效（合成期间大哥喊唤醒词抢答也能正确走授权分支）
    assert d.pending_approval == {"id": "ap10", "action": "rm -rf 临时目录"}
    d.wait_speech(timeout=5)
    assert d.process_chunk(LISTEN_CHUNK) == "decision"
    assert client.decisions == [("ap10", "approved")]


def test_decision_flag_skipped_if_already_decided(cache):
    """提示播放中被打断并已决裁 → 播完后的免唤醒 RECORD 不再触发。"""
    client = FakeClient()
    player = FakePlayer()
    record_fn = make_record_fn([SPEECH_1S])
    d = make_daemon(cache, client=client, player=player, record_fn=record_fn)
    d._decision_pending = True                     # 播完回调已置位
    d.pending_approval = None                      # 但打断路径已完成决裁

    assert d.process_chunk(LISTEN_CHUNK) == "listen"
    assert record_fn.calls == []                   # 不重复录音
    assert d._decision_pending is False            # 标志被消费


def test_speak_text_clamped_to_server_limit(cache):
    """speak 文本钳到 500 字内（与 /api/voice/tts 的 max_length 对齐，防 422）。"""
    client = FakeClient()
    player = FakePlayer()
    d = make_daemon(cache, client=client, player=player)
    d.speak("长" * 600)
    assert len(client.tts_texts[0]) == 500


# ---------------------------------------------------------------------------
# summarize 纯函数
# ---------------------------------------------------------------------------

def test_summarize_strips_markdown_symbols():
    assert summarize("**好的**`大哥`") == "好的大哥"


def test_summarize_takes_first_two_sentences():
    assert summarize("第一句。第二句！第三句。") == "第一句。第二句！"


def test_summarize_removes_code_blocks():
    text = "结论在此。\n```python\nprint('噪音')\n```\n后续说明。还有一句。"
    out = summarize(text)
    assert out == "结论在此。后续说明。"
    assert "print" not in out


def test_summarize_strips_headings_and_lists():
    text = "# 标题\n- 列表项完成。\n剩下的不要。多余的也不要。"
    out = summarize(text)
    assert out.startswith("标题")
    assert "#" not in out and "-" not in out


def test_summarize_keeps_link_text():
    assert summarize("[官网](https://example.com)已上线。其余略。") == "官网已上线。其余略。"


def test_summarize_no_terminator_returns_whole_text():
    assert summarize("没有标点的一句话") == "没有标点的一句话"


def test_summarize_empty():
    assert summarize("") == ""
    assert summarize("   \n  ") == ""

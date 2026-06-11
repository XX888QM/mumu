# 贾维斯系统 Phase 2 语音实施计划（全屋钢铁侠版）

> **For agentic workers:** 多 agent 团队执行。只动所有权表分给你的文件；契约锁定；
> 公共纪律沿用 Phase 1 计划（docs/superpowers/plans/2026-06-11-jarvis-phase1.md）第 2 节开头那段：
> 先测试后实现、不 git 操作、汇报文件清单与测试结果。

**Goal:** 喊"Jarvis"→说指令→贾维斯应声接活→办完贾维斯音色播报；语音授权；网页🎤；全本地推理。

**Tech:** openwakeword(hey_jarvis) + silero-vad + faster-whisper(large-v3-turbo) + IndexTTS-2(worker进程) + sounddevice。

---

## 0. 已验证事实（直接信，勿重验）

- IndexTTS-2 API：`IndexTTS2(cfg_path="checkpoints/config.yaml", model_dir="checkpoints").infer(spk_audio_prompt=<wav>, text=<str>, output_path=<wav路径>)`，仓库 `~/Desktop/开发/index-tts`，自带 `.venv`。
- TTS 实测耗时见集成节冒烟记录（脚手架已跑通 /tmp/jarvis_tts_smoke.wav）。
- `.venv-voice`（python3.12）由脚手架建好，已装：openwakeword、faster-whisper、silero-vad、sounddevice、httpx、websockets、numpy、pytest、pytest-asyncio。
- 唤醒模型与 whisper 模型已由脚手架预下载（whisper 走 HF_ENDPOINT=https://hf-mirror.com）。
- Phase 1 契约全部沿用：REST/WS/认证/DB 不变；WS 认证=首消息 auth。
- 服务器 venv（.venv）新增 faster-whisper（仅 /api/voice/transcribe 用，懒加载单例）。

## 1. 接口契约（锁定）

### 1.1 文件所有权

| Agent | 文件 |
|-------|------|
| scaffold | `.venv-voice/` `reqs-voice.txt` `jarvis/config.py`(增量) `.env`(增量) `workspace/voice/jarvis_ref.wav`(默认音色副本) |
| V1 | `voice/__init__.py` `voice/audio.py` `voice/wake.py` `voice/asr.py` `tests/voice/test_audio.py` `tests/voice/test_wake_asr.py` |
| V2 | `voice/daemon.py` `voice/client.py` `voice/acks.py` `tests/voice/test_daemon.py` `tests/voice/test_acks.py` |
| V3 | `voice/tts_worker.py` `jarvis/server.py`(仅新增 voice 路由段) `web/`(🎤+朗读，三文件小增量) `tests/test_voice_api.py` |
| V4 | `deploy/com.yunxin.jarvis.voice.plist` `deploy/com.yunxin.jarvis.tts.plist` `deploy/install.sh`(增量) `deploy/uninstall.sh`(增量) `workspace/AGENTS.md`(语音风格段) `README.md`(语音章节) |

tests/voice/ 用 `.venv-voice/bin/python -m pytest tests/voice/ -v` 跑；tests/test_voice_api.py 用 `.venv/bin/python`。

### 1.2 .env / config.py 增量（scaffold 已写好，字段名锁定）

```
VOICE_ENABLED=1            → settings.voice_enabled (bool)
TTS_PORT=8778              → settings.tts_port
INDEX_TTS_DIR=/Users/yunxin/Desktop/开发/index-tts → settings.index_tts_dir
VOICE_REF=<root>/workspace/voice/jarvis_ref.wav    → settings.voice_ref
ASR_MODEL=large-v3-turbo   → settings.asr_model
WAKE_THRESHOLD=0.5         → settings.wake_threshold
# 派生：settings.venv_voice_py、settings.voice_cache_dir=<root>/data/voice_cache
```

### 1.3 voice/tts_worker.py（在 index-tts 的 .venv 里跑，**只用 stdlib**，不 pip 进对方 venv）

- 启动：`<index-tts>/.venv/bin/python <root>/voice/tts_worker.py`；进程 cwd 必须切到 INDEX_TTS_DIR；
  `sys.path.insert(0, INDEX_TTS_DIR)`；加载 IndexTTS2 一次。
- http.server ThreadingHTTPServer 监听 127.0.0.1:TTS_PORT：
  - `GET /healthz` → 200 `{"ok":true,"model_loaded":true}`
  - `POST /tts` body JSON `{"text":"...", "ref":"可选wav绝对路径"}` → 200 `audio/wav` 字节
    （ref 缺省用环境变量 VOICE_REF；合成串行加锁——模型非线程安全）
  - 失败 → 500 JSON {"error":...}
- 配置经环境变量传入：INDEX_TTS_DIR、VOICE_REF、TTS_PORT（plist/EnvironmentVariables 注入）。

### 1.4 V1 模块接口（.venv-voice）

```python
# voice/audio.py（sounddevice 16k 单声道 int16）
class Recorder:
    def __init__(self, sample_rate=16000, block_ms=80): ...
    def stream(self):            # 上下文管理器/生成器：yield np.int16 一维块
class Player:
    def play_wav(self, path_or_bytes, blocking=False) -> None
    def stop(self) -> None       # 立即停播（打断用）
    @property
    def playing(self) -> bool
def record_until_silence(rec_stream, vad, max_sec=15.0, silence_ms=900,
                          pre_roll: list | None = None) -> "np.ndarray"
    # 从流收音直到连续静音 silence_ms 或 max_sec；pre_roll 为唤醒后已缓存块

# voice/wake.py（openwakeword hey_jarvis）
class WakeDetector:
    def __init__(self, threshold: float): ...
    def feed(self, chunk: "np.ndarray") -> float   # 返回当帧最高分；调用方比阈值
    def reset(self) -> None

# voice/asr.py（faster-whisper）
class Transcriber:
    def __init__(self, model_name: str): ...       # device=cpu, compute_type=int8
    def transcribe(self, audio: "np.ndarray", language="zh") -> str  # 16k float32/int16 均接受
    def transcribe_file(self, path: str, language="zh") -> str

# voice/vad.py 不单设：silero-vad 封装放 audio.py 内（类 SileroVAD.is_speech(chunk)->bool）
```

### 1.5 V2 模块接口

```python
# voice/client.py（对 jarvis-server）
class JarvisClient:
    def __init__(self, base_url, token): ...
    def chat(self, message: str) -> dict            # 固定语音会话：首次按 title="语音会话" 建/找，busy 时返回 {"busy": True}
    def decide(self, approval_id, decision) -> bool
    def tts(self, text: str) -> bytes               # POST /api/voice/tts
    def listen(self, on_event) -> None              # 后台线程跑 WS（首消息 auth；断线 3s 重连），
                                                    # on_event(msg_dict) 在该线程回调
    @property
    def session_id(self) -> str | None

# voice/acks.py
ACKS = {"wake": ["在", "大哥请讲"], "accept": ["好的大哥，这就办", "收到，马上处理"],
        "busy": ["上一件事还没办完，稍等"], "approval_prompt": ["大哥，需要授权：{action}。批准还是拒绝？"],
        "approval_unclear": ["没听清，批准还是拒绝？"], "approval_giveup": ["那大哥稍后在控制台处理"],
        "fail": ["任务出岔子了，详情在控制台"]}
def ensure_cache(tts_fn) -> dict[str, list[str]]   # 逐句存 data/voice_cache/<md5>.wav，返回 key→wav路径列表
def pick(cache, key) -> str                        # 随机取一条；带{action}模板的不缓存、实时合成

# voice/daemon.py 状态机（主循环，锁定语义）
# LISTEN: rec.stream() 喂 WakeDetector；分>threshold → WAKE
# WAKE:   播 wake ack → RECORD（带 pre_roll）
# RECORD: record_until_silence → 空/过短(<0.4s) → LISTEN；否则 TRANSCRIBE
# TRANSCRIBE: Transcriber.transcribe(language="zh") → 文本
# DISPATCH:
#   若 pending_approval 非空 → 含"批准/同意/可以/行" → decide(approved)；
#     含"拒绝/不行/取消/否" → decide(denied)；都不含 → 追问一次(approval_unclear)，
#     再不清 → approval_giveup，清 pending（红卡仍在控制台）
#   否则 → client.chat(文本)；busy → 播 busy ack；成功 → 播 accept ack → LISTEN
# WS 回调（listen 线程→主循环队列）：
#   task_done(本会话) → status=done：摘要=result 去 markdown 符号取前两句 → tts → 播；failed → 播 fail ack
#   approval_request → pending_approval=id → 实时合成 approval_prompt(action) → 播 → 进入 RECORD
# 打断：Player.playing 期间 SileroVAD 检测人声 → Player.stop() → 直接 RECORD（免唤醒）
# CLI: python voice/daemon.py [--selftest | --once-from-wav <wav> | --say <text>]
#   --once-from-wav：跳过唤醒与录音，把 wav 当作 RECORD 产物走完整后续流程（测试钩子）
#   --say：文本→tts→播放（调试音色）
# selftest（见 spec §5）：不碰麦克风，全绿 exit 0
```

### 1.6 V3：server 语音端点 + 网页

- `POST /api/voice/transcribe`（要 Bearer）：multipart `file`（webm/wav）→ `{"text": "..."}`；
  faster-whisper 懒加载单例（settings.asr_model，cpu int8）；解码靠 faster-whisper 自带 av。
- `POST /api/voice/tts`（要 Bearer）：`{"text":"..."}` → 代理 `http://127.0.0.1:{tts_port}/tts` → 透传 audio/wav；
  worker 不可达 → 503 {"detail":"tts worker offline"}。
- 网页：输入框旁 🎤 按钮——按住录（MediaRecorder audio/webm），松开→transcribe→文本入框并自动发送；
  录音中反应堆变红脉冲。顶栏新增"朗读"开关（localStorage jarvis_speak=1）：开启时本会话 task_done →
  fetch /api/voice/tts 播放（去 markdown 取前两句，与 daemon 摘要规则一致，抽成 app.js 函数 summarize(text)）。
  浏览器无麦权限/非安全上下文 → 🎤 置灰带 title 提示。

### 1.7 V4：部署

- `com.yunxin.jarvis.tts.plist`：[<index-tts>/.venv/bin/python, <root>/voice/tts_worker.py]，
  WorkingDirectory=INDEX_TTS_DIR，EnvironmentVariables: INDEX_TTS_DIR/VOICE_REF/TTS_PORT，KeepAlive，
  日志 logs/tts.{out,err}.log。模板占位符 + install.sh sed 渲染（沿用 Phase 1 方式）。
- `com.yunxin.jarvis.voice.plist`：[<root>/.venv-voice/bin/python, <root>/voice/daemon.py]，
  WorkingDirectory=<root>，KeepAlive，日志 logs/voice.{out,err}.log。
- install.sh 增量：VOICE_ENABLED=1 时渲染加载两个新 plist；健康检查 8778 /healthz（重试 60 次×2s，模型加载慢）；
  voice daemon 起来后打印"首次需在弹窗允许麦克风"。uninstall.sh 卸三个。
- AGENTS.md 增量段（锁定文案要点）：会话标题为"语音会话"时——回复第一句必须是适合朗读的一句话结论，
  禁用 markdown 符号/表格/代码块（控制台会话不受影响）。

## 2. 团队工序（测试要点）

- **V1**：Recorder/Player 用 monkeypatch 假 sounddevice（不碰真音频设备）；record_until_silence 用合成
  正弦+静音数组验证截断逻辑；WakeDetector/Transcriber 真模型加载各 1 条（模型已预下载，断言可推理出类型正确结果，
  不断言识别准确率）；SileroVAD 真模型对静音=False、白噪+正弦混合语音样本不崩。
- **V2**：daemon 状态机全用 Fake（FakeClient/FakePlayer/FakeTranscriber/FakeWake），逐转移断言：
  唤醒→ack→录→转写→chat→accept；busy 路径；授权三路径（批准/拒绝/两次不清放弃）；task_done 播报含摘要；
  打断路径（playing 时 vad 真→stop 被调）。summarize() 纯函数单测（去 markdown、取两句）。
  acks.ensure_cache 用假 tts_fn 断言缓存文件生成与复用。
- **V3**：TestClient——transcribe 无 token 401；伪 whisper 单例 monkeypatch 返回固定文本断言透传；
  tts 代理用 httpx MockTransport 断言转发与 503 路径。tts_worker 单测：subprocess 起真 worker？
  不行（模型加载慢）——worker 的 handler 抽成可测函数，假 tts 引擎注入测 JSON/错误路径；
  真模型合成验证留给集成 selftest。web 部分 node --check + DOM id 交叉校验（沿用 F 的做法）。
- **V4**：bash -n、plutil -lint、plistlib 字段断言、sed 渲染产物校验（照 Phase 1 G 的验法）。

## 3. 集成（单 agent）

1. 两套 pytest 全绿：`.venv/bin/python -m pytest tests/ -q`（Phase1 107 条不回归 + voice_api 新增）
   与 `.venv-voice/bin/python -m pytest tests/voice/ -q`。
2. 起 tts_worker（真模型）→ curl /healthz → POST /tts 合成"测试"落盘 wav 验证 RIFF 头。
3. `jarvis-voice --selftest` 真跑全绿（TTS↔ASR 闭环、hey_jarvis 加载与合成唤醒音频喂入、缓存生成）。
4. `--once-from-wav`：TTS 合成"现在几点了"→ 喂入 → 断言 chat 任务创建并 done、播报 wav 落盘
   （Player 在 JARVIS_VOICE_FAKE_AUDIO=1 时把"播放"写文件而非出声——audio.py 留此测试钩子，V1 实现）。
5. 语音授权闭环：curl 造 pending → daemon 收 approval_request →（fake audio 模式）合成"批准"喂入 → approval=approved。
6. bash deploy/install.sh 重跑 → 三服务在线、kill 自愈。
7. git add -A && git commit。

## 4. 审查 → 修复 → 回归（沿用 Phase 1 流程，含安全：新端点认证、worker 仅绑 127.0.0.1、
   路径注入（ref 参数限制在白名单目录）、subprocess 注入）。

## 验收对照 spec §7 逐条打勾后汇报大哥（真人麦克风项列为"待大哥配合"）。

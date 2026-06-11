# 木木系统 Phase 2 语音设计文档（全屋钢铁侠版）

日期：2026-06-11
状态：方向已获大哥拍板（全屋唤醒版 + 网页语音入口顺带）；细节按"按推荐不停"授权由我定

## 1. 目标

像电影一样：在房间里喊"木木"，说出指令，木木应声接活、出声答复；
任务办完用木木音色主动播报；高危授权可以用嘴批准。全链路本地推理，零额外费用。

## 2. 已勘察事实（直接信）

| 项 | 结论 |
|----|------|
| TTS | `~/Desktop/开发/index-tts`：IndexTTS-2 权重齐全（gpt.pth/s2mel.pth/config.yaml），自带 .venv，API=`IndexTTS2(cfg_path, model_dir).infer(spk_audio_prompt, text, output_path)`，examples/ 有参考音色 |
| ASR | faster-whisper 1.2.1 可装（CTranslate2，M4 Max CPU int8 快） |
| 唤醒词 | 旧方案 openwakeword 0.6.0（预训练 hey_jarvis 模型）；现役方案为 sherpa-onnx KWS 中文关键词"木木"（kws-zipformer-wenetspeech 模型，关键词表 voice/keywords.txt） |
| VAD | silero-vad 6.2.1 可装（torch CPU） |
| 麦克风 | MV-SILICON 设备在线，2 输入通道 |
| 隔离原则 | index-tts 的 torch 栈**不并入** jarvis venv：TTS 以 worker 进程跑在 index-tts 自己的 .venv 里 |

## 3. 架构（双层：实时语音层 + 干活层）

```
                    ┌──────────────────────────────────────────────┐
 麦克风 ──► jarvis-voice 守护进程（.venv-voice，LaunchAgent 常驻）     │
            │ sherpa-onnx KWS（中文关键词"木木"）持续监听              │
            │   └─ 唤醒 → 播应答声("在") → silero-VAD 录音到静音       │
            │       └─ faster-whisper 本地转写                       │
            │           ├─ 命中授权语境("批准/拒绝") → decide API      │
            │           └─ 普通指令 → POST /api/chat                 │
            │               └─ 即刻播预合成应答("好的大哥，这就办")     │
            │ WS 订阅 jarvis-server：                                │
            │   ├─ task_done(语音会话) → 结果摘要 → TTS → 播放        │
            │   ├─ approval_request → TTS 播报"需要授权…" → 听答复    │
            │   └─ 播放中检测到人声 → 立即停播（打断）                 │
            └──────────────┬───────────────────────────────────────┘
                           │ HTTP(127.0.0.1)
   ┌───────────────────────▼──────────┐   ┌───────────────────────────┐
   │ jarvis-server (8777, Phase 1)     │──►│ tts-worker (8778,          │
   │ + POST /api/voice/transcribe      │   │ index-tts .venv 内常驻，    │
   │ + POST /api/voice/tts → 代理 8778 │   │ 模型加载一次，文本→wav)     │
   └───────────────────────────────────┘   └───────────────────────────┘
 网页控制台：🎤 按钮（MediaRecorder→transcribe→发送）+ 回复自动朗读开关
```

要点：
- **秒级对话感**：唤醒应答与接单应答用**预合成缓存 wav**（开机生成一次），零合成延迟；
  真正干活的几秒~几分钟里大哥不用等着，办完木木主动开口。
- **语音授权**：approval_request 时播报"大哥，需要授权：<action>，批准还是拒绝？"，
  识别"批准/同意/可以"→approved，"拒绝/不行/取消"→denied，听不清追问一次后放弃（红卡仍在）。
- **打断**：TTS 播放期间 VAD 检测到人声 → 停播放（基础全双工）。
- **会话连续**：语音指令固定走名为"语音会话"的 session（resume 续上下文），
  90 秒内再次唤醒免重复上下文。

## 4. 组件与契约要点

| 组件 | 位置/运行环境 | 职责 |
|------|--------------|------|
| voice/daemon.py | `.venv-voice`（新建：sherpa-onnx+faster-whisper+silero-vad+sounddevice+httpx+websockets） | 主循环状态机：LISTEN→WAKE→RECORD→TRANSCRIBE→DISPATCH→SPEAK |
| voice/tts_worker.py | **index-tts 的 .venv** 执行（sys.path 注入 index-tts 仓库根） | FastAPI 127.0.0.1:8778：POST /tts {text} → wav bytes；启动加载模型一次 |
| voice/audio.py | .venv-voice | 录音流、环形缓冲、VAD 封装、播放（sounddevice）+ 打断停止 |
| voice/acks.py | .venv-voice | 预合成语料管理（"在/好的大哥，这就办/收到/大哥，需要授权"等），开机检查缓存缺则调 tts_worker 生成到 data/voice_cache/ |
| server 扩展 | jarvis venv | /api/voice/transcribe（faster-whisper 加入 server venv，供网页）；/api/voice/tts 代理 8778 |
| web 扩展 | 静态 | 🎤 按住说话按钮 + "回复朗读"开关（task_done 后 fetch /api/voice/tts 播放） |
| deploy | — | com.yunxin.jarvis.voice.plist + install.sh 增量；uninstall 同步 |
| .env 增量 | — | VOICE_ENABLED/VOICE_REF（默认 index-tts examples 男声，换木木音色=替换 workspace/voice/jarvis_ref.wav）/ASR_MODEL(默认 large-v3-turbo)/WAKE_THRESHOLD/TTS_PORT=8778 |
| AGENTS.md 增量 | — | 语音会话回复风格：先一句话结论、口语化、不输出 markdown 符号 |

## 5. 自测设计（无人声环境下的真实验证）

**TTS→ASR 闭环自测**（`jarvis-voice --selftest`）：
1. tts_worker 合成"打开下载文件夹" → wav
2. faster-whisper 转写该 wav → 断言含"下载"
3. sherpa-onnx KWS 加载中文关键词"木木"（keywords.txt）→ 用 TTS 合成的"木木"音频喂入 → 断言命中关键词
4. 预合成缓存生成齐全
（真实麦克风首跑需要大哥在 macOS 弹窗点一次"允许"，并喊一嗓子实测——交付时说明）

## 6. 风险与对策

| 风险 | 对策 |
|------|------|
| 旧方案 hey_jarvis 对中式发音灵敏度不足 | 已换 sherpa-onnx KWS 中文关键词"木木"；WAKE_THRESHOLD 可调；自测用 TTS 多发音变体校准 |
| TTS 合成长文慢 | 语音播报只读"结果摘要"（AGENTS.md 约束语音会话先给一句话结论；播报截前 2 句） |
| 麦克风 TCC 权限 | 首跑弹窗大哥点允许；selftest 不依赖麦克风 |
| 手机网页收音需 HTTPS | Phase 2 桌面 localhost 即安全上下文可用；手机语音留 enable-https 可选脚本（自签证书），不阻塞主线 |
| 两个模型常驻内存 | whisper(~1.5G)+indextts(~4G)，128G 的 M4 Max 无压力 |

## 7. 验收标准

1. `jarvis-voice --selftest` 全绿（TTS↔ASR 闭环、唤醒模型、缓存）。
2. 控制台 🎤 按住说话→转写正确→任务执行；回复朗读开关生效（浏览器实测）。
3. 喂入合成语音文件模拟全流程：唤醒→指令→chat 任务创建→done→TTS 播报（落盘 wav 验证）。
4. 语音授权流：approval_request → 播报 → 合成"批准"音频喂入 → approval 变 approved。
5. LaunchAgent 双服务常驻，kill 自愈。
6. pytest 新增用例全绿且 Phase 1 107 条不回归。
7. 真人实测项（交付后大哥配合 1 分钟）：首跑允许麦克风 → 喊"木木"下一条真指令。

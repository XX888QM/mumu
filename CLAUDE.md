# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# 木木系统（MUMU）项目规则

私人 AI 管家：网页全息控制台 + 中文语音唤醒"木木" + codex(GPT-5.5) 引擎 + 分级授权 + 定时任务。

## 部署坐标（线上真值以实际进程/配置为准）
- **项目真实位置：`~/Desktop/开发/木木`**（2026-06-11 傍晚大哥拍板从 ~/jarvis 搬回，
  知晓 TCC 风险）；`~/jarvis` 现在是指向它的**反向软链**（保 cli shebang/文档路径兼容）
- **当前状态：三个服务已停止、plist 已卸载（大哥指示"先不运行木木"）**；
  重新启动＝`bash deploy/install.sh`（一条命令，幂等）
- 三个 LaunchAgent（运行时均 KeepAlive 常驻开机自启）：
  `com.yunxin.jarvis`（主服务 8777）/ `com.yunxin.jarvis.tts`（TTS worker 127.0.0.1:8778）/
  `com.yunxin.jarvis.voice`（语音守护：唤醒词"木木" → ASR → chat → TTS 播报）
- 端口：8777（监听 0.0.0.0 局域网）；控制台 http://localhost:8777
- 访问令牌：`.env` 的 `JARVIS_TOKEN`（只打印掩码）
- 日志：`logs/{jarvis,tts,voice}.{out,err}.log`（0600）；数据库：`data/jarvis.db`（SQLite WAL，0600）
- TTS 运行时：`tts-rt/`＝APFS 克隆的 checkpoints（零空间）+ uv py3.10 独立 venv；
  大哥的 `~/Desktop/开发/index-tts` 项目本体未动
- 语音旋钮（.env）：`WAKE_THRESHOLD`＝sherpa-onnx keywords_threshold（默认 0.25，
  误唤醒调大 0.35-0.5，漏唤醒调小；超出 (0, 0.6] 启动告警）；
  换音色＝替换 `workspace/voice/jarvis_ref.wav` 后重启 tts 服务

## 常用命令
```bash
# 主测试套（API/引擎/调度/授权/MCP 桥，全 Fake 注入零真实 codex 调用）
.venv/bin/python -m pytest tests/ -v
# 语音测试套（sherpa-onnx/whisper 真模型；tests/voice/conftest.py 按 sounddevice
# 存在性守卫——主 venv 跑全套时自动跳过本目录）
.venv-voice/bin/python -m pytest tests/voice/ -q
# 单测
.venv/bin/python -m pytest tests/test_api.py::test_approval_flow -v
# 前端 JS 语法（pytest 套内也有 node --check 用例）
node --check web/app.js
# 语音链路自检（不开麦；需三服务在线）：server/TTS/ASR 闭环/唤醒/VAD/应答缓存
.venv-voice/bin/python voice/daemon.py --selftest
# 试音色 / 测完整派发流程（不走唤醒）
.venv-voice/bin/python voice/daemon.py --say "文本"
.venv-voice/bin/python voice/daemon.py --once-from-wav x.wav
# CLI
cli/jarvis "指令"   |   cli/jarvis --status   |   cli/jarvis --approvals
# 服务装载/卸载（幂等；install 含 venv/音色/KWS 模型前置检查 + 权限收紧）
bash deploy/install.sh   |   bash deploy/uninstall.sh
```

## 架构大图（三进程、三 venv，互不 import）
1. **主服务**（`.venv` py3.12，uvicorn `jarvis.server:app` :8777）：REST + WS 广播 +
   静态托管 web/ + APScheduler(cron) + faster-whisper 懒加载（网页转写 /api/voice/transcribe）
2. **TTS worker**（`tts-rt/.venv` py3.10，`voice/tts_worker.py` 127.0.0.1:8778）：
   IndexTTS2 合成，**只用 stdlib**（不 pip 进对方 venv），合成串行加锁；
   模型加载 ~18.5s + 暖机，前端对 503 自动重试 5 次（覆盖被 Jetsam 杀后的重启窗口）
3. **语音守护**（`.venv-voice` py3.12，`voice/daemon.py`）：状态机
   LISTEN→WAKE→RECORD→TRANSCRIBE→DISPATCH；防回声是设计核心——播放期不喂唤醒检测、
   授权提示播完才进 RECORD、实时 TTS 在后台线程（主音频循环绝不阻塞）

**任务执行流**：POST /api/chat(202) → 后台 `codex exec --json` 子进程（契约 1.7 命令行
锁定，含 `model_reasoning_summary="detailed"` 让思考摘要进事件流）→ 逐行 JSONL 经
on_event 入库 + WS 全量广播 `task_event` → `task_done`(result/usage)。
前端把 reasoning item 渲染成打字机思考流（.think-stream），回复打字机上屏。

**MCP 工具桥**（`jarvis/mcp_server.py`，stdio 子进程，禁止 import jarvis 包其他模块）：
request_approval / notify / schedule_task / remember 四工具回调主服务 /api/internal/*；
凭据走 `JARVIS_RUNTIME` 环境变量指向的 `data/.runtime.json`（0600，lifespan 启动时写），
**token 严禁进子进程 argv**；request_approval 轮询超时会回写 pending→expired（防脏数据）。

**唤醒词引擎**（`voice/wake.py`）：sherpa-onnx KeywordSpotter，关键词在
`voice/keywords.txt`——**必须是 ppinyin token**（如 `m ù m ù @木木`，行内可加
`:boost #threshold`）；直接写汉字会被 sherpa C++ 层 exit(-1) 杀进程，构造前有
OOV 预校验拦截。模型 `models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/`
**不进 git**（install.sh 有预检与代理下载命令）。feed() 命中返回 1.0/未命中 0.0。

**前端**（web/，零依赖纯静态）：事件→DOM 映射表锁定在 app.js 文件头注释；
语音 HUD：#voice-orb 点击说话（转写直发不经输入框）、键盘 #kbd-toggle 兜底、
打字机全局串行队列 typewriteQueued（元素不可见即快进，防折叠阻塞）、
语音发起任务（state.voiceTasks）完成自动 TTS 播报（与 DOM 解耦）。

- 木木人格：`workspace/AGENTS.md`；长期记忆：`workspace/memory.md`（remember 工具追加写）
- 接口契约：`docs/superpowers/plans/2026-06-11-jarvis-phase1.md` 与同目录 phase2 文档
  第 1 节（**改接口先改契约文档**）

## 开发规则
- 测试：两套都要绿（命令见上）；改完必须全绿
- **TestClient fixture 必须 monkeypatch `settings.runtime_file` 到 tmp_path**——
  否则 lifespan 会用测试 token 覆写真实 `data/.runtime.json`，线上 MCP 桥全 401
  （2026-06-11 已踩，两处 fixture 都有此行，新增 fixture 照抄）
- WS 认证是首消息 `{"type":"auth","token":...}`，不是 URL query——前端/CLI/测试三处一致
- 真实 codex 调用消耗大哥的 Pro 额度，测试用 mock/fixtures，真实冒烟点到为止
- BARK_KEY 为空时推送降级为日志（`logs/jarvis.err.log` 搜 "推送降级"）
- 网页麦克风受浏览器安全上下文限制：仅 localhost（或 HTTPS）可录音，否则 orb 置灰自动展开键盘

## GitHub 远程（2026-06-11 公开）
- origin = https://github.com/XX888QM/mumu（**公开仓库**）；**git push 必须先经大哥同意**
- 永不入库：`.env`、`data/`、`logs/`、`models/`、`workspace/voice/`（音色音频）、
  `workspace/memory.md`、两个 venv；jarvis_ref.wav 已于发布前 filter-branch 从全历史抹除，
  **别再把任何音色/人声音频提交进来**

## TCC 血泪教训（macOS launchd 必读）
- **launchd 拉起的进程读 ~/Desktop（及 Documents/Downloads）下文件会被 TCC 卡死在内核 open()**：
  uv 管理的 python 解释器 venv 在 Desktop 下时连 Py_Initialize 都过不去（site 读 .pth 即挂）。
  **终端手动跑正常 ≠ launchd 正常**（终端会话有宿主 App 的 TCC 权限）。
- 解法＝运行时全部迁出 TCC 保护区（本项目曾因此搬到 ~/jarvis）。
- **2026-06-11 傍晚搬回 Desktop 实测**：从终端 `install.sh` 装载后三服务健康检查全过——
  但从终端 load 可能借了终端的 TCC 上下文，**重启电脑后 launchd 冷启动才是真考验**；
  若开机后三服务全卡死（有 PID 无响应/日志空白）＝TCC 复发，出路二选一：
  ① 系统设置→隐私与安全性→完全磁盘访问权限，放行 `.venv`/`.venv-voice`/`tts-rt/.venv`
  三个 python 解释器；② 搬回 `mv ~/Desktop/开发/木木 ~/jarvis` 后重跑 install.sh。
- 麦克风是独立 TCC 权限：python 首次开麦需大哥在 系统设置→隐私与安全性→麦克风 放行。
- 多 agent 教训：队员 `git add -A` 曾把两万个 venv 文件提交进仓库（.gitignore 漏了 .venv-voice），
  已 filter-branch 清洗。**收队员提交必须看 `git show --stat`。**

## 阶段状态
- Phase 1（控制台+引擎+调度+授权+CLI）已交付验收
- Phase 2 语音已交付（2026-06-11：唤醒词"木木"/sherpa-onnx、语音 HUD、思考流打字机）
- Phase 3 消息通道（Telegram/微信 OpenClaw 桥/企微）：另立 spec，等大哥发话

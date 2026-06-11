# 木木系统项目规则

## 部署坐标（线上真值以实际进程/配置为准）
- **项目真实位置：`~/jarvis`**；`~/Desktop/开发/贾维斯系统` 是指向它的软链
  （搬迁原因见下方"TCC 血泪教训"，勿搬回 Desktop）
- 三个 LaunchAgent（均 KeepAlive 常驻开机自启）：
  `com.yunxin.jarvis`（主服务 8777）/ `com.yunxin.jarvis.tts`（TTS worker 127.0.0.1:8778）/
  `com.yunxin.jarvis.voice`（语音守护：唤醒词"木木" → ASR → chat → TTS 播报）
- 端口：8777（监听 0.0.0.0 局域网）；控制台 http://localhost:8777
- 访问令牌：`.env` 的 `JARVIS_TOKEN`（只打印掩码）
- 日志：`logs/{jarvis,tts,voice}.{out,err}.log`；数据库：`data/jarvis.db`（SQLite WAL）
- 重启/重装：`bash deploy/install.sh`（幂等）；卸载：`bash deploy/uninstall.sh`
- TTS 运行时：`tts-rt/`＝APFS 克隆的 checkpoints（零空间）+ uv py3.10 独立 venv；
  大哥的 `~/Desktop/开发/index-tts` 项目本体未动
- 语音旋钮（.env）：`WAKE_THRESHOLD`（sherpa-onnx keywords_threshold 语义，默认 0.25，误唤醒调大 0.35-0.5，漏唤醒调小）、
  换音色＝替换 `workspace/voice/jarvis_ref.wav` 后重启 tts 服务

## TCC 血泪教训（macOS launchd 必读）
- **launchd 拉起的进程读 ~/Desktop（及 Documents/Downloads）下文件会被 TCC 卡死在内核 open()**：
  uv 管理的 python 解释器 venv 在 Desktop 下时连 Py_Initialize 都过不去（site 读 .pth 即挂）。
  **终端手动跑正常 ≠ launchd 正常**（终端会话有宿主 App 的 TCC 权限）。
- 解法＝运行时全部迁出 TCC 保护区（本项目因此搬到 ~/jarvis）。常驻服务永远别部署在 Desktop。
- 麦克风是独立 TCC 权限：python 首次开麦需大哥在 系统设置→隐私与安全性→麦克风 放行。
- 多 agent 教训：队员 `git add -A` 曾把两万个 venv 文件提交进仓库（.gitignore 漏了 .venv-voice），
  已 filter-branch 清洗。**收队员提交必须看 `git show --stat`。**

## 架构速查
- 引擎：`codex exec --json --ignore-user-config`（GPT-5.5，ChatGPT Pro 订阅，非 API）；resume 续会话
- MCP 工具桥：`jarvis/mcp_server.py`（request_approval / notify / schedule_task / remember）；
  凭据走 `data/.runtime.json`（0600，server 启动时写），**token 严禁进子进程 argv**
- 木木人格：`workspace/AGENTS.md`；长期记忆：`workspace/memory.md`
- 接口契约：`docs/superpowers/plans/2026-06-11-jarvis-phase1.md` 第 1 节（改接口先改契约文档）

## 开发规则
- venv：`.venv`（python3.12）；测试：`.venv/bin/python -m pytest tests/ -v`，改完必须全绿
- WS 认证是首消息 `{"type":"auth","token":...}`，不是 URL query——前端/CLI/测试三处保持一致
- 真实 codex 调用消耗大哥的 Pro 额度，测试用 mock/fixtures，真实冒烟点到为止
- BARK_KEY 为空时推送降级为日志（`logs/jarvis.err.log` 搜 "推送降级"）

## 阶段状态
- Phase 1（控制台+引擎+调度+授权+CLI）已交付验收
- Phase 2 语音、Phase 3 消息通道（Telegram/微信 OpenClaw 桥/企微）：另立 spec，等大哥发话

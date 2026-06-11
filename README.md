# 贾维斯系统（J.A.R.V.I.S.）

常驻 Mac 的 AI 管家：科幻网页控制台指挥 GPT-5.5（codex CLI）干活，支持实时执行过程展示、定时任务、分级授权（Bark 推送 / 控制台确认）和命令行入口。

## 架构

```
 iPhone(Bark推送/Safari主屏)      Chrome 控制台            终端 CLI
        ▲    │                        │                      │
        │    └──────── HTTP/WS ───────┼───── HTTP/WS ────────┘
        │                             ▼
   Bark 服务 ◄────────── FastAPI 单进程服务 (jarvis/server.py, :8777)
                          │  REST API + WebSocket + 静态托管 web/
                          │
        ┌─────────────────┼──────────────────┬───────────────┐
        ▼                 ▼                  ▼               ▼
  SQLite(WAL)       CodexEngine        APScheduler      ApprovalGateway
  data/jarvis.db    codex exec --json  定时任务调度       授权状态机
  (jarvis/db.py)    子进程引擎          (scheduler.py)    (approval.py)
                          │
                          ▼
                 codex CLI (GPT-5.5) ──工作目录──► workspace/
                          │                        ├ AGENTS.md  人格
                          │ stdio MCP              └ memory.md  长期记忆
                          ▼
              jarvis/mcp_server.py（4 个主动工具，回调 server API）
              request_approval / notify / schedule_task / remember
```

- **服务**：FastAPI 单进程承载 REST + WebSocket + 静态控制台，LaunchAgent 常驻（`com.yunxin.jarvis`，RunAtLoad + KeepAlive）。
- **引擎**：每个任务 spawn 一个 `codex exec --json` 子进程，逐行解析 JSONL 事件实时入库并经 WS 广播到前端。
- **主动能力**：codex 进程通过注入的 stdio MCP 工具桥回调本服务，获得申请授权、推送 iPhone、建定时任务、写长期记忆四种能力。
- **授权**：模型触发高危操作（清单见 `workspace/AGENTS.md`）→ 创建 pending 授权 → 控制台红色警示卡 + Bark 推送 → 大哥批准/拒绝 → 模型继续/停手；超时自动 expired。

## 快速开始

### 方式一：一键安装（推荐，开机自启常驻）

```bash
cd "/Users/yunxin/Desktop/开发/贾维斯系统"
/opt/homebrew/bin/python3.12 -m venv .venv          # 已有 venv 可跳过
.venv/bin/pip install -r requirements.txt           # 已装过可跳过
bash deploy/install.sh
```

install.sh 幂等可重跑，做这几件事：检查 venv → 生成/补全 `.env`（自动产随机 token）→ 建 `data/ logs/` 目录 → 按 `.env` 的 host/port 渲染 plist 装入 `~/Library/LaunchAgents/` → `launchctl` 重载 → `/healthz` 重试 10 次验证 → 打印控制台地址（含局域网 IP）。

卸载（保留代码与数据）：

```bash
bash deploy/uninstall.sh
```

### 方式二：手动前台运行（调试用）

```bash
cd "/Users/yunxin/Desktop/开发/贾维斯系统"
.venv/bin/python -m uvicorn jarvis.server:app --host 0.0.0.0 --port 8777
```

### 访问

| 入口 | 地址/用法 |
|------|----------|
| 网页控制台 | `http://localhost:8777`（首次访问输入 `.env` 里的 JARVIS_TOKEN） |
| 手机 | iPhone 同一 WiFi 开 `http://<局域网IP>:8777`，Safari 分享 → 添加到主屏幕 |
| CLI | `cli/jarvis "指令"`；`-s <session_id>` 续会话；`--status` 系统状态；`--approvals` 处理待批授权 |
| 健康检查 | `curl http://localhost:8777/healthz`（免认证） |

## .env 配置说明

项目根 `.env`（首次可由 `cp .env.example .env` 或 install.sh 自动生成），全部有默认值：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `JARVIS_HOST` | `0.0.0.0` | 监听地址（0.0.0.0 = 允许局域网访问） |
| `JARVIS_PORT` | `8777` | 服务端口 |
| `JARVIS_TOKEN` | （install.sh 自动生成） | API/控制台访问令牌，`openssl rand -hex 16` |
| `BARK_KEY` | 空 | iPhone Bark App 的 device key；**留空时推送降级为日志，不报错** |
| `BARK_SERVER` | `https://api.day.app` | Bark 服务器 |
| `CODEX_BIN` | `~/.npm-global/bin/codex` | codex CLI 路径 |
| `JARVIS_MODEL` | `gpt-5.5` | 模型 |
| `JARVIS_REASONING` | `high` | 推理力度 |
| `JARVIS_SANDBOX` | `danger-full-access` | codex 沙箱级别 |
| `JARVIS_TASK_TIMEOUT` | `3600` | 单任务超时（秒） |
| `APPROVAL_TIMEOUT` | `1800` | 授权等待超时（秒），超时按 expired=拒绝处理 |

改完 `.env` 后重跑 `bash deploy/install.sh` 生效（会重新渲染 plist 并重载服务）。

## 语音模式（Phase 2）

喊一声 **"Jarvis"** 唤醒 → 说指令 → 贾维斯应声接活 → 办完用贾维斯音色播报结果；待批授权可直接开口说"批准/拒绝"。唤醒、断句、识别、合成全部本地推理，音频不出这台 Mac。

### 组成

| 组件 | 进程 / 端口 | 说明 |
|------|------------|------|
| 语音守护 `voice/daemon.py` | LaunchAgent `com.yunxin.jarvis.voice`（`.venv-voice`） | openwakeword(hey_jarvis) 唤醒 + silero-vad 断句 + faster-whisper(large-v3-turbo) 转写，对接 :8777 主服务；播报中开口说话即可打断（免唤醒） |
| TTS worker `voice/tts_worker.py` | LaunchAgent `com.yunxin.jarvis.tts`，仅绑 `127.0.0.1:8778` | IndexTTS-2 合成贾维斯音色，跑在 index-tts 仓库自带 venv；启动即暖机（模型加载约 20~30s） |
| 服务端语音端点 | `POST /api/voice/transcribe`、`POST /api/voice/tts`（均要 Bearer） | 网页录音转写 / TTS 代理（worker 掉线返回 503） |
| 网页控制台 | 输入框旁 🎤 按钮 + 顶栏"朗读"开关 | 按住说话、松开自动转写发送；开朗读后本会话任务完成自动播报摘要 |

### .env 语音变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `VOICE_ENABLED` | `1` | `0`/`false`/空 = 不装语音服务（install.sh 会顺手卸掉已装的） |
| `TTS_PORT` | `8778` | TTS worker 端口（只监听 127.0.0.1） |
| `INDEX_TTS_DIR` | `/Users/yunxin/Desktop/开发/index-tts` | IndexTTS-2 仓库路径（自带 `.venv` 与 checkpoints） |
| `VOICE_REF` | `workspace/voice/jarvis_ref.wav` | 参考音色；**换音色 = 换这个 wav（22050Hz 单声道）后重跑 install.sh** |
| `ASR_MODEL` | `large-v3-turbo` | faster-whisper 模型（cpu int8） |
| `WAKE_THRESHOLD` | `0.5` | 唤醒置信度阈值：误唤醒多 → 调大；唤不醒 → 调小 |

### 安装与使用

- `bash deploy/install.sh`（VOICE_ENABLED=1 时）自动渲染并装载 `com.yunxin.jarvis.tts` / `com.yunxin.jarvis.voice` 两个 LaunchAgent，并对 `http://127.0.0.1:8778/healthz` 重试最长 2 分钟等模型就绪。
- **首次启动 macOS 会弹麦克风授权窗，必须点允许**（之后在 系统设置 → 隐私与安全性 → 麦克风 可改）。
- 流程：喊 "Jarvis" → 听到应答（如"在 / 大哥请讲"）→ 说指令 → 贾维斯确认接单，办完自动播报；正在忙时会提示稍等。

### 调试与排障

| 操作 | 命令 / 位置 |
|------|------------|
| 自检（不碰麦克风） | `.venv-voice/bin/python voice/daemon.py --selftest` |
| 试音色 | `.venv-voice/bin/python voice/daemon.py --say "大哥好"` |
| 喂 wav 走完整流程 | `.venv-voice/bin/python voice/daemon.py --once-from-wav xx.wav` |
| 日志 | `logs/tts.{out,err}.log`、`logs/voice.{out,err}.log` |
| TTS 健康检查 | `curl http://127.0.0.1:8778/healthz` |
| 跑语音测试 | `.venv-voice/bin/python -m pytest tests/voice/ -v`；服务端语音端点 `.venv/bin/python -m pytest tests/test_voice_api.py -v` |

常见问题：听不见播报 → 先 `curl :8778/healthz` 看 worker 是否在线、再查 `logs/tts.err.log`；唤不醒 → 确认麦克风权限已允许、调小 `WAKE_THRESHOLD`；网页 🎤 置灰 → 浏览器无麦克风权限或非安全上下文（localhost 不受限）。

## 常见故障

| 症状 | 原因 | 处理 |
|------|------|------|
| 任务失败，错误含 `401` / `unauthorized` / `login`（同时会收到 Bark 提醒） | codex 的 ChatGPT 登录态过期 | 终端执行 `codex login` 重新登录，然后重试任务 |
| 收不到 iPhone 推送，日志里出现推送降级记录 | `BARK_KEY` 未配置 | iPhone 装 Bark App，把 device key 填入 `.env` 的 `BARK_KEY`，重跑 install.sh |
| install.sh 报 "未找到 venv" | 虚拟环境没建 | 按提示用 `/opt/homebrew/bin/python3.12 -m venv .venv` 建好并装依赖 |
| `/healthz` 重试 10 次不通过 | 服务起不来（依赖缺失/端口被占/代码错误） | `tail -50 logs/jarvis.err.log` 看根因；端口冲突改 `.env` 的 `JARVIS_PORT` 后重装 |
| 控制台 401 反复弹 token 输入 | token 不对或 `.env` 重新生成过 | 用 `.env` 当前 `JARVIS_TOKEN` 重新登录（浏览器会清掉旧的 localStorage） |
| 改了 `.env` 不生效 | LaunchAgent 还在跑旧配置 | 重跑 `bash deploy/install.sh`（自动 unload + load） |
| 服务被杀后没复活 | LaunchAgent 未装载 | `launchctl list | grep com.yunxin.jarvis` 检查；没有则重跑 install.sh |

## 目录结构

```
jarvis/         后端：server / engine / db / approval / scheduler / push / mcp_server / config
voice/          语音侧：daemon / wake / asr / audio / acks / client / tts_worker
web/            科幻控制台（纯静态，零构建）
cli/jarvis      命令行入口
workspace/      贾维斯工作区：AGENTS.md（人格）、memory.md（长期记忆）、voice/jarvis_ref.wav（音色）
deploy/         LaunchAgent 模板 ×3（主服务/tts/voice）+ install.sh / uninstall.sh
data/           SQLite 数据库（jarvis.db，WAL）、voice_cache/（应答语缓存）
logs/           jarvis / tts / voice 的 .out.log 与 .err.log
tests/          pytest 测试（tests/voice/ 用 .venv-voice 跑）
```

## 开发

```bash
.venv/bin/python -m pytest tests/ -v     # 跑全部测试
```

注意：`.env`（含 token）已被 `.gitignore` 排除，永不入库、永不外传。

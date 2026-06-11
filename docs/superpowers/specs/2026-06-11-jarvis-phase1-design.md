# 木木系统 Phase 1 设计文档

日期：2026-06-11
状态：已获大哥批准

## 1. 背景与目标

打造一个钢铁侠 J.A.R.V.I.S. 风格的个人 AI 管家系统，运行在大哥的 Mac (M4 Max) 上，
帮助处理电脑操作、信息情报、日程事务、公司业务等各类事务（全能型）。

**核心约束**：模型用 GPT-5.5，不走 API 计费，通过 codex CLI（ChatGPT Pro 账号登录態）调用。
环境已确认：codex-cli 0.137.0，ChatGPT 登录模式，默认模型 gpt-5.5，reasoning effort xhigh。

## 2. 需求决策记录

| 决策点 | 结论 |
|--------|------|
| 功能范围 | 全能：电脑操作 / 信息情报 / 日程事务 / 业务相关，啥都能干 |
| 交互形态 | 科幻网页控制台 + 语音 + 手机消息 + CLI（分阶段） |
| 主动性 | 主动管家：常驻后台、定时任务、事件触发、主动汇报 |
| 授权模型 | 分级授权：读查类直接干；高危操作（删除/对外发送/花钱/部署/系统配置）推送确认后执行 |
| 手机通道 | Phase 1 手机网页 + Bark 推送；Phase 3 Telegram / 微信（OpenClaw 腾讯官方插件桥）/ 企微 |
| 总体方案 | 方案C：自研核心 + 科幻控制台，OpenClaw 仅在 Phase 3 作为微信通道桥 |

## 3. 分阶段规划

| 阶段 | 内容 | 本文档范围 |
|------|------|-----------|
| Phase 1 | 核心大脑 + 科幻网页控制台 + Bark 推送 + 定时任务 + 分级授权 + CLI | ✅ 是 |
| Phase 2 | 语音对话（浏览器收音输入 + index-tts 木木音色输出） | 否，另立 spec |
| Phase 3 | Telegram / 微信(OpenClaw 官方插件桥) / 企微 通道适配器 | 否，另立 spec |

Phase 1 架构中预留通道适配器接口（消息进出走统一抽象），保证 Phase 3 可插拔接入。

## 4. 整体架构

```
┌──────────────── Mac M4 Max（LaunchAgent 常驻，开机自启）────────────────┐
│                                                                        │
│  jarvis-server（Python / FastAPI）                                      │
│  ├─ API + WebSocket 层 ── 对话、任务状态实时推送                          │
│  ├─ Agent 引擎 ────────── 子进程跑 codex exec --json（GPT-5.5），        │
│  │                        resume 续会话，JSONL 事件流实时解析             │
│  ├─ 调度器 ────────────── APScheduler，定时任务持久化于 SQLite            │
│  ├─ 授权网关 ──────────── 高危操作拦截 → Bark 推送 → 等确认               │
│  ├─ 推送 ──────────────── Bark API → iPhone                             │
│  └─ 存储 ──────────────── SQLite（会话/任务/授权记录）                    │
│                                                                        │
│  jarvis-mcp（MCP 工具服务器，stdio 方式注册给 codex）                    │
│  └─ request_approval / notify / schedule_task / remember               │
│                                                                        │
│  web/ 科幻控制台（纯 HTML/CSS/JS，无构建链，FastAPI 静态托管）            │
│  cli/ jarvis 命令（调本地 API）                                          │
│  workspace/ 木木工作目录（AGENTS.md = 人格 + 规则 + 高危清单）             │
└────────────────────────────────────────────────────────────────────────┘
        手机：同一网页（局域网访问，PWA 加主屏幕）+ Bark 推送
```

## 5. 组件设计

### 5.1 Agent 引擎（engine.py）
- 以子进程方式运行 `codex exec --json`，新对话开新会话，后续轮次用 `codex exec resume <session-id>`。
- 解析 JSONL 事件流（思考、命令执行、输出、最终消息），逐事件经 WebSocket 推送前端。
- 维护「控制台会话 ↔ codex session id」映射，存 SQLite。
- 超时控制（默认上限可配置）；进程异常退出时任务标记失败并告警。
- codex 配置：工作目录 `-C workspace/`，沿用大哥全局 sandbox 配置；通过 `-c` 注入 jarvis-mcp 的 MCP server 配置，不污染全局 `~/.codex/config.toml`。

### 5.2 jarvis-mcp 工具服务器（mcp_server.py）
赋予 GPT-5.5 主动能力的关键。stdio MCP server，提供 4 个工具：

| 工具 | 行为 |
|------|------|
| `request_approval(action, detail, risk_level)` | 创建授权请求 → Bark 推送 + 控制台红卡 → 阻塞等待批准/拒绝/超时（默认 30 分钟超时=拒绝），返回结果 |
| `notify(title, message)` | 即时 Bark 推送给大哥 |
| `schedule_task(cron, prompt, name)` | 在调度器中创建定时任务（写 SQLite，立即生效） |
| `remember(content)` | 追加写入 workspace/memory.md，跨会话长期记忆 |

### 5.3 调度器（scheduler.py）
- APScheduler + SQLite job store；任务 = cron 表达式 + prompt 模板 + 名称。
- 到点触发：组装 prompt → 走 Agent 引擎执行（独立 codex 会话）→ 结果入库 + Bark 推送摘要。
- 控制台任务面板可增删改查、手动立即触发、查看历史执行记录。

### 5.4 授权网关（approval.py）
- 授权请求状态机：`pending → approved / denied / expired`。
- 渠道：控制台红色授权卡片（批准/拒绝按钮）+ Bark 推送（深链回控制台）。
- 全部请求与决策入库，可审计。
- 高危清单（写入 AGENTS.md，要求模型必须先调 request_approval）：
  删除/覆盖文件、对外发送（邮件/消息）、涉及金钱、部署线上、修改系统配置、
  git push / reset --hard、安装卸载系统级软件。
- 明确边界：Phase 1 的分级授权是**约定式**（模型遵守 AGENTS.md 硬规则），
  sandbox 不做物理强制（与大哥现有 codex danger-full-access 使用习惯一致）；
  如后续需要物理强制，可改用 workspace-write sandbox + 审批升级，另行评估。

### 5.5 科幻控制台（web/）
- 技术：纯 HTML/CSS/JS 单页应用，无构建链；WebSocket 实时通信。
- 视觉：深空黑底 + 全息青蓝光效 + 扫描线/网格背景；中央反应堆呼吸圆环（木木
  思考时加速旋转）；开场上线动画。
- 布局：
  - 中央：对话流（指令 + 回复 + 可展开的实时「执行过程」事件流）
  - 右侧：任务面板（定时任务 / 运行中 / 历史）
  - 顶部：系统状态条（CPU / 内存 / 磁盘 / codex 会话状态）
  - 浮层：授权请求红色高亮卡片
- 手机自适应 + PWA manifest（iPhone 加主屏幕当独立 App 用）。

### 5.6 CLI（cli/jarvis）
- `jarvis "指令"`：调本地 API，流式打印执行过程与结果。
- 与控制台共享同一后端与认证令牌。

### 5.7 人格与规则（workspace/AGENTS.md）
- 称呼大哥、中文汇报、简洁直接、报告用表格。
- 高危清单 + 必须走 request_approval 的硬规则。
- 可用 MCP 工具说明与使用时机（主动汇报用 notify，被要求定期做事用 schedule_task 等）。

## 6. 数据流

**即时指令**：控制台/CLI 输入 → WS/API → 创建任务 → codex exec（resume）
→ JSONL 事件流实时推前端 → 遇高危 agent 调 request_approval → Bark + 红卡
→ 大哥批准 → 继续 → 完成入库 + 展示 +（离线时）Bark 汇报。

**定时任务**：到点 → 调度器组 prompt → 同上 → 摘要 Bark 推送。

## 7. 安全设计

- 服务监听局域网；网页/CLI 统一 Bearer Token 认证（首次输入，localStorage 记住）。
- 不暴露公网；后续远程需求用 Tailscale 解决（不在 Phase 1 范围）。
- 授权决策仅在持有令牌的控制台/带一次性 token 的 Bark 深链中完成。
- 密钥（Bark key、访问令牌）存本地 `.env`，不入 git。
- 授权记录全量入库审计。

## 8. 错误处理

| 故障 | 处理 |
|------|------|
| codex 进程崩溃/超时 | 任务标失败 + Bark 告警，不自动重试超过 1 次 |
| ChatGPT 登录过期 | 识别 auth 类错误，推送「需要重新登录 codex」 |
| 服务自身崩溃 | LaunchAgent KeepAlive 自动拉起；SQLite WAL 防损坏 |
| 同一任务连败 2 次 | 停止重试，报症状等大哥处理（按高危 SOP 精神） |
| 调度任务执行时服务重启 | APScheduler misfire 策略：错过窗口则跳过并记录 |

## 9. 测试策略

- pytest 单测：授权状态机、调度器持久化、JSONL 事件解析（engine 以录制样本 mock）。
- 真实 codex 冒烟测试 1 条（节省 Pro 额度）。
- UI 浏览器实跑验证、API curl 实调（按大哥验证规矩）。

## 10. Phase 1 验收标准

1. 网页控制台对话指挥木木干活，实时看到执行过程。
2. 定时任务到点自动执行，结果 Bark 主动推送 iPhone。
3. 高危操作触发手机/控制台确认，批准后才执行，拒绝则中止。
4. `jarvis "指令"` 命令行可用。
5. 重启电脑后服务自动恢复，定时任务不丢。
6. 手机浏览器（局域网）可正常访问控制台并加入主屏幕。

## 11. 目录结构

```
贾维斯系统/
├── jarvis/              # Python 包
│   ├── server.py        # FastAPI 入口（API + WS + 静态托管）
│   ├── engine.py        # codex exec 子进程管理
│   ├── scheduler.py     # APScheduler 封装
│   ├── approval.py      # 授权网关
│   ├── push.py          # Bark 推送
│   ├── mcp_server.py    # jarvis-mcp 工具
│   └── db.py            # SQLite
├── web/                 # 科幻控制台静态文件
├── cli/jarvis           # CLI 脚本
├── workspace/           # 木木工作目录（AGENTS.md / memory.md）
├── tests/
├── .env.example
└── docs/superpowers/specs/
```

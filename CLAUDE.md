# 贾维斯系统（J.A.R.V.I.S.）项目规则

## 部署坐标（线上真值以实际进程/配置为准）
- 服务：LaunchAgent `com.yunxin.jarvis`（KeepAlive 常驻，开机自启），uvicorn 跑 `jarvis.server:app`
- 端口：8777（监听 0.0.0.0，局域网可访问）；控制台 http://localhost:8777
- 访问令牌：`.env` 的 `JARVIS_TOKEN`（只打印掩码）
- 日志：`logs/jarvis.{out,err}.log`；数据库：`data/jarvis.db`（SQLite WAL）
- 重启：`launchctl unload ~/Library/LaunchAgents/com.yunxin.jarvis.plist && launchctl load 同路径`，或重跑 `bash deploy/install.sh`（幂等）
- 卸载：`bash deploy/uninstall.sh`

## 架构速查
- 引擎：`codex exec --json --ignore-user-config`（GPT-5.5，ChatGPT Pro 订阅，非 API）；resume 续会话
- MCP 工具桥：`jarvis/mcp_server.py`（request_approval / notify / schedule_task / remember）；
  凭据走 `data/.runtime.json`（0600，server 启动时写），**token 严禁进子进程 argv**
- 贾维斯人格：`workspace/AGENTS.md`；长期记忆：`workspace/memory.md`
- 接口契约：`docs/superpowers/plans/2026-06-11-jarvis-phase1.md` 第 1 节（改接口先改契约文档）

## 开发规则
- venv：`.venv`（python3.12）；测试：`.venv/bin/python -m pytest tests/ -v`，改完必须全绿
- WS 认证是首消息 `{"type":"auth","token":...}`，不是 URL query——前端/CLI/测试三处保持一致
- 真实 codex 调用消耗大哥的 Pro 额度，测试用 mock/fixtures，真实冒烟点到为止
- BARK_KEY 为空时推送降级为日志（`logs/jarvis.err.log` 搜 "推送降级"）

## 阶段状态
- Phase 1（控制台+引擎+调度+授权+CLI）已交付验收
- Phase 2 语音、Phase 3 消息通道（Telegram/微信 OpenClaw 桥/企微）：另立 spec，等大哥发话

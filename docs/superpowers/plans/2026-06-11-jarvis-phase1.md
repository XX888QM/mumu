# 贾维斯系统 Phase 1 实施计划

> **For agentic workers:** 本计划由多 agent 团队（Workflow 编排）并行执行。每个 agent 只动「文件所有权表」中分配给自己的文件，严格遵守「接口契约」一节，禁止修改契约。步骤用 checkbox 跟踪。

**Goal:** 在大哥的 Mac 上建成常驻的贾维斯 AI 管家：科幻网页控制台指挥 GPT-5.5（codex CLI）干活，支持实时执行过程展示、定时任务、分级授权（Bark/控制台确认）、CLI。

**Architecture:** Python FastAPI 单进程服务（API+WS+静态托管）+ codex exec 子进程引擎 + APScheduler 调度 + stdio MCP 工具桥（赋予模型申请授权/推送/建任务/记忆四种主动能力）+ 纯 HTML/JS 科幻控制台。

**Tech Stack:** Python 3.12 venv、FastAPI、uvicorn、APScheduler、httpx、mcp(FastMCP)、psutil、pytest；前端无构建链纯静态；Bark 推送；LaunchAgent 常驻。

---

## 0. 已验证事实（不要重新验证，直接信）

- codex-cli 0.137.0，ChatGPT Pro 登录态，模型 gpt-5.5。
- 新会话：`codex exec --json -C <dir> --skip-git-repo-check "<prompt>"`
- 续会话：`codex exec --json -C <dir> --skip-git-repo-check resume <thread_id> "<prompt>"`（全局参数必须在 `resume` 子命令**之前**）
- `--ignore-user-config` 跳过 `~/.codex/config.toml`（避免大哥全局配置里过期的 cloudflare MCP 报错和 notify 弹窗钩子），**auth 不受影响**。需自带 `-m gpt-5.5 -s danger-full-access`。
- JSONL 事件实测样本：

```jsonl
{"type":"thread.started","thread_id":"019eb332-306a-7ca3-b792-434c9f1351d7"}
{"type":"turn.started"}
{"type":"item.started","item":{"id":"item_0","type":"command_execution","command":"/bin/zsh -lc 'echo JARVIS_TEST'","aggregated_output":"","exit_code":null,"status":"in_progress"}}
{"type":"item.completed","item":{"id":"item_0","type":"command_execution","command":"/bin/zsh -lc 'echo JARVIS_TEST'","aggregated_output":"JARVIS_TEST\n","exit_code":0,"status":"completed"}}
{"type":"item.completed","item":{"id":"item_1","type":"agent_message","text":"大哥，..."}}
{"type":"turn.completed","usage":{"input_tokens":95260,"cached_input_tokens":41088,"output_tokens":358,"reasoning_output_tokens":266}}
```

- item.type 还可能有 `reasoning`、`mcp_tool_call` 等，解析器必须容忍未知类型（原样透传）。
- stderr 可能有日志噪音，引擎只解析 stdout 的 JSON 行；非 JSON 行忽略。
- Python：用 `/opt/homebrew/bin/python3.12` 建 venv（系统 3.9 太老）。
- 端口 8777 空闲。Bark key 磁盘上没有，BARK_KEY 留空时推送降级为日志。

## 1. 接口契约（锁定，任何 agent 不得更改）

### 1.1 目录与文件所有权

| Agent | 创建文件 | 职责 |
|-------|---------|------|
| scaffold(编排者) | `.env` `.env.example` `.gitignore` `requirements.txt` `jarvis/__init__.py` `jarvis/config.py` `tests/__init__.py` `tests/fixtures/codex_events.jsonl` | 脚手架 |
| A | `jarvis/db.py` `jarvis/approval.py` `tests/test_db.py` `tests/test_approval.py` | 存储+授权状态机 |
| B | `jarvis/engine.py` `tests/test_engine.py` | codex 子进程引擎 |
| C | `jarvis/scheduler.py` `jarvis/push.py` `tests/test_scheduler.py` `tests/test_push.py` | 调度+推送 |
| D | `jarvis/mcp_server.py` `tests/test_mcp_server.py` | MCP 工具桥 |
| E | `jarvis/server.py` `cli/jarvis` `tests/test_api.py` | API/WS/静态/CLI |
| F | `web/index.html` `web/app.js` `web/style.css` `web/manifest.json` `web/icon.svg` `web/icon-180.png` | 科幻控制台 |
| G | `workspace/AGENTS.md` `deploy/com.yunxin.jarvis.plist` `deploy/install.sh` `deploy/uninstall.sh` `README.md` | 人格+部署 |

集成阶段才允许跨文件修改（由集成 agent 统一处理）。

### 1.2 环境变量（jarvis/config.py 从项目根 .env 加载，全部有默认值）

```
JARVIS_HOST=0.0.0.0            # 监听局域网
JARVIS_PORT=8777
JARVIS_TOKEN=<openssl rand -hex 16 生成>
BARK_KEY=                      # 空=推送降级为日志
BARK_SERVER=https://api.day.app
CODEX_BIN=/Users/yunxin/.npm-global/bin/codex
JARVIS_MODEL=gpt-5.5
JARVIS_REASONING=high
JARVIS_SANDBOX=danger-full-access
JARVIS_TASK_TIMEOUT=3600       # 秒
APPROVAL_TIMEOUT=1800          # 秒，超时=denied(expired)
JARVIS_ROOT=<项目根绝对路径，config.py 自动推导>
# 派生路径(config.py 提供)：WORKSPACE=$JARVIS_ROOT/workspace  DB_PATH=$JARVIS_ROOT/data/jarvis.db
# VENV_PY=$JARVIS_ROOT/.venv/bin/python
```

`jarvis/config.py` 已由脚手架写好，所有模块 `from jarvis.config import settings` 使用，
字段名即上表小写（如 `settings.jarvis_port`、`settings.db_path`）。

### 1.3 SQLite schema（db.py 初始化执行，WAL 模式）

```sql
CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY, codex_thread_id TEXT, title TEXT,
  created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,
  role TEXT CHECK(role IN ('user','jarvis')), content TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS tasks(
  id TEXT PRIMARY KEY, session_id TEXT, source TEXT, prompt TEXT,
  status TEXT CHECK(status IN ('running','done','failed','cancelled')),
  result TEXT, error TEXT, usage_json TEXT, started_at TEXT, finished_at TEXT);
CREATE TABLE IF NOT EXISTS task_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, type TEXT,
  payload TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS approvals(
  id TEXT PRIMARY KEY, task_id TEXT, action TEXT, detail TEXT,
  risk_level TEXT, status TEXT CHECK(status IN ('pending','approved','denied','expired')),
  created_at TEXT, decided_at TEXT, decided_via TEXT);
CREATE TABLE IF NOT EXISTS cron_jobs(
  id TEXT PRIMARY KEY, name TEXT, cron TEXT, prompt TEXT,
  enabled INTEGER DEFAULT 1, created_at TEXT, last_run_at TEXT, last_status TEXT);
```

时间一律 `datetime.now().astimezone().isoformat()`。id 一律 `uuid.uuid4().hex`。

### 1.4 jarvis/db.py 公开接口（同步 sqlite3，check_same_thread=False + 全局锁）

```python
class Database:
    def __init__(self, path: str): ...            # 建目录、连库、执行 schema、WAL
    # sessions
    def create_session(self, title: str) -> dict
    def get_session(self, session_id: str) -> dict | None
    def set_session_thread(self, session_id: str, thread_id: str) -> None
    def list_sessions(self, limit: int = 50) -> list[dict]
    # messages
    def add_message(self, session_id: str, role: str, content: str) -> dict
    def list_messages(self, session_id: str, limit: int = 200) -> list[dict]
    # tasks
    def create_task(self, session_id: str | None, source: str, prompt: str) -> dict   # status=running
    def finish_task(self, task_id: str, status: str, result: str = "", error: str = "", usage_json: str = "") -> None
    def get_task(self, task_id: str) -> dict | None
    def list_tasks(self, limit: int = 50) -> list[dict]
    def add_task_event(self, task_id: str, type: str, payload: str) -> dict
    def list_task_events(self, task_id: str) -> list[dict]
    # approvals
    def create_approval(self, task_id: str | None, action: str, detail: str, risk_level: str) -> dict
    def decide_approval(self, approval_id: str, status: str, via: str) -> dict | None  # 仅 pending 可决，返回更新后行
    def get_approval(self, approval_id: str) -> dict | None
    def list_approvals(self, status: str | None = None, limit: int = 50) -> list[dict]
    # cron
    def create_cron(self, name: str, cron: str, prompt: str) -> dict
    def update_cron(self, job_id: str, **fields) -> dict | None
    def delete_cron(self, job_id: str) -> bool
    def list_cron(self) -> list[dict]
    def get_cron(self, job_id: str) -> dict | None
    def mark_cron_run(self, job_id: str, status: str) -> None
```

所有返回 dict 的方法返回该行的全部列（列名为 key）。

### 1.5 jarvis/approval.py 公开接口

```python
class ApprovalGateway:
    def __init__(self, db: Database, on_request=None, on_resolve=None):
        # on_request(approval: dict)、on_resolve(approval: dict)：异步回调（由 server 注入：广播WS+Bark推送）
    async def request(self, task_id: str | None, action: str, detail: str, risk_level: str) -> dict
        # 创建 pending 记录，触发 on_request，返回 approval dict（不阻塞等待）
    async def decide(self, approval_id: str, decision: str, via: str) -> dict | None
        # decision in ('approved','denied')；幂等：非 pending 返回 None；触发 on_resolve
    async def wait(self, approval_id: str, timeout: float) -> str
        # 轮询db每1s直到非pending或超时；超时则置 expired 并触发 on_resolve；返回最终 status
    def expire_stale(self) -> int   # 启动时清理：把超过 APPROVAL_TIMEOUT 的 pending 置 expired
```

### 1.6 jarvis/engine.py 公开接口

```python
class CodexEngine:
    def __init__(self, on_event=None):
        # on_event(task_id: str, event: dict)：每解析到一行 JSONL 调用（异步回调）
    async def run(self, prompt: str, task_id: str, thread_id: str | None = None,
                  timeout: float | None = None) -> EngineResult
        # 组装命令行（见1.7），spawn 子进程，逐行读 stdout：
        #   - json.loads 成功 → on_event(task_id, event)；记录 thread.started 的 thread_id；
        #     收集最后一条 agent_message 文本为 final_message；记录 turn.completed 的 usage
        #   - 解析失败 → 忽略该行
        # 超时 → terminate，5秒后 kill，抛 EngineTimeout
        # 退出码非0 → 抛 EngineError(stderr尾部500字)
    def cancel(self, task_id: str) -> bool   # terminate 对应子进程

@dataclass
class EngineResult:
    thread_id: str | None
    final_message: str
    usage: dict          # turn.completed 的 usage，可能为空 dict
class EngineError(Exception): ...
class EngineTimeout(EngineError): ...
```

### 1.7 引擎命令行组装（锁定）

```python
cmd = [
  settings.codex_bin, "exec", "--json", "--ignore-user-config",
  "-C", settings.workspace, "--skip-git-repo-check",
  "-m", settings.jarvis_model, "-s", settings.jarvis_sandbox,
  "-c", f'model_reasoning_effort="{settings.jarvis_reasoning}"',
  "-c", f'mcp_servers.jarvis.command="{settings.venv_py}"',
  "-c", f'mcp_servers.jarvis.args=["{settings.jarvis_root}/jarvis/mcp_server.py"]',
  "-c", ('mcp_servers.jarvis.env={ JARVIS_URL = "http://127.0.0.1:%d", JARVIS_TOKEN = "%s", JARVIS_TASK_ID = "%s" }'
         % (settings.jarvis_port, settings.jarvis_token, task_id)),
]
if thread_id:
    cmd += ["resume", thread_id, prompt]
else:
    cmd += [prompt]
```

注意：`-c` 的 value 是 TOML 字面量，引号必须如上。**集成阶段必须真实验证 MCP 注入可用**。

### 1.8 jarvis/push.py 公开接口（Bark）

```python
async def bark_push(title: str, body: str, url: str | None = None, level: str = "active") -> bool
    # BARK_KEY 为空：logging.info 记录并返回 False（降级，不抛错）
    # 否则 POST {BARK_SERVER}/push  json={"device_key":KEY,"title":title,"body":body,
    #   "level":level,"group":"jarvis", **({"url":url} if url else {})}
    # httpx 超时10s，失败 logging.warning 返回 False，不抛错
```

### 1.9 jarvis/scheduler.py 公开接口

```python
class JarvisScheduler:
    def __init__(self, db: Database, run_job):   # run_job: async (job: dict) -> None，由 server 注入
    def start(self) -> None      # AsyncIOScheduler；从 db.list_cron() 恢复 enabled 任务；misfire_grace_time=60, coalesce=True
    def shutdown(self) -> None
    def add(self, name: str, cron: str, prompt: str) -> dict        # 写db+注册；cron为标准5段表达式，CronTrigger.from_crontab
    def update(self, job_id: str, **fields) -> dict | None          # 改db+重注册/暂停
    def remove(self, job_id: str) -> bool
    def trigger_now(self, job_id: str) -> None                      # 立即异步执行一次
```

### 1.10 jarvis/mcp_server.py（独立 stdio 进程，FastMCP，不 import jarvis 包其他模块）

从环境变量读 `JARVIS_URL` `JARVIS_TOKEN` `JARVIS_TASK_ID`。用 httpx 同步客户端调 server，
Header `Authorization: Bearer $JARVIS_TOKEN`。4 个工具（docstring 写中文，模型会读）：

```python
@mcp.tool()
def request_approval(action: str, detail: str, risk_level: str = "high") -> str:
    """高危操作前必须调用此工具申请大哥批准。action=一句话动作名，detail=精确清单(做什么/影响什么)，
    risk_level=high|critical。返回 approved/denied/expired，非 approved 时禁止执行该操作。"""
    # POST /api/internal/approvals {task_id,action,detail,risk_level} -> {approval_id}
    # 每2s GET /api/approvals/{id} 直到 status!=pending 或 APPROVAL_TIMEOUT(默认1800s)；返回最终status

@mcp.tool()
def notify(title: str, body: str) -> str:
    """主动推送消息到大哥的 iPhone（Bark）。任务完成汇报、重要发现、需要大哥注意的事用这个。"""
    # POST /api/internal/notify -> "ok"/"failed"

@mcp.tool()
def schedule_task(name: str, cron: str, prompt: str) -> str:
    """创建定时任务。cron=标准5段表达式(分 时 日 月 周)，prompt=到点时交给贾维斯执行的完整指令。"""
    # POST /api/internal/schedule -> 返回 job id

@mcp.tool()
def remember(content: str) -> str:
    """写入长期记忆(跨会话不忘)。大哥的偏好、重要事实、未完成事项都应记录。"""
    # POST /api/internal/remember -> "ok"
```

### 1.11 REST API（server.py；除 /healthz 外全部要求 `Authorization: Bearer $JARVIS_TOKEN`，错误返回 401 {"detail":"unauthorized"}）

| Method/Path | Body → Response |
|---|---|
| GET /healthz | → {"ok":true} 免认证 |
| POST /api/chat | {message, session_id?} → 202 {task_id, session_id}；该会话已有 running 任务→409 {"detail":"busy"} |
| GET /api/sessions | → [{id,title,codex_thread_id,created_at,updated_at}] |
| GET /api/sessions/{id}/messages | → [{role,content,created_at}] |
| GET /api/tasks?limit=50 | → tasks 列表（新→旧） |
| GET /api/tasks/{id} | → {**task, events:[{type,payload,created_at}]} |
| POST /api/tasks/{id}/cancel | → {"ok":true}；非 running→404 |
| GET /api/approvals?status= | → approvals 列表 |
| POST /api/approvals/{id}/decide | {decision:"approved"\|"denied"} → 决定后的 approval；非 pending→409 |
| GET /api/cron | → cron_jobs 列表 |
| POST /api/cron | {name,cron,prompt} → 新 job dict；cron 非法→422 |
| PATCH /api/cron/{id} | {name?,cron?,prompt?,enabled?} → 更新后 dict |
| DELETE /api/cron/{id} | → {"ok":true} |
| POST /api/cron/{id}/run | → {"ok":true} 立即触发 |
| GET /api/system | → {cpu_percent,mem_percent,disk_percent,uptime_sec,codex_auth:"ok"\|"unknown"\|"error",active_tasks:int} |
| POST /api/internal/approvals | {task_id?,action,detail,risk_level} → {approval_id}（MCP桥用） |
| POST /api/internal/notify | {title,body} → {"ok":bool} |
| POST /api/internal/schedule | {name,cron,prompt} → job dict |
| POST /api/internal/remember | {content} → {"ok":true}（带时间戳追加 workspace/memory.md） |

静态：`/` 挂 `web/`（index.html 为默认页）。

### 1.12 WebSocket 协议（`/ws?token=...`，token 错直接关闭 code 4401）

服务端 → 客户端（均为 JSON 单条）：

```json
{"type":"task_started","task":{...task行...}}
{"type":"task_event","task_id":"..","event":{..codex原始事件..}}
{"type":"task_done","task_id":"..","status":"done|failed|cancelled","result":"最终消息","usage":{..}}
{"type":"approval_request","approval":{...approvals行...}}
{"type":"approval_resolved","approval":{...更新后行...}}
{"type":"cron_changed"}
{"type":"system","data":{..同/api/system..}}        // 每5秒
{"type":"pong"}
```

客户端 → 服务端：`{"type":"ping"}`（保活，其余操作全走 REST）。

### 1.13 chat 任务执行流（server 内 run_chat_task，锁定语义）

1. db.add_message(session,'user',message)；db.create_task(...)；WS 广播 task_started
2. engine.run(prompt=message, task_id, thread_id=会话已存线程id)
   - on_event：db.add_task_event + WS 广播 task_event；遇 thread.started 且会话无线程id → set_session_thread
3. 成功：db.add_message(session,'jarvis',final_message)；finish_task(done)；WS task_done
4. EngineTimeout/EngineError：finish_task(failed,error=..)；WS task_done(status=failed)；bark_push("贾维斯任务失败",...)
5. error 文本含 "401"/"unauthorized"/"login" → bark_push("大哥需要重新登录 codex","codex login")

cron 任务流：prompt = job.prompt + "\n\n(定时任务【{name}】自动执行，完成后用 notify 工具汇报结果摘要)"；无 session（thread_id=None 单次会话）；完成后 mark_cron_run。

### 1.14 前端（web/）要求

- 纯静态三件套，零依赖零构建；fetch + WebSocket 对接上述契约；token 首次访问弹输入层，存 localStorage("jarvis_token")，401/4401 时清除重弹。
- 视觉锁定：深空黑 `#04080f` 底、全息青 `#00e5ff`/蓝 `#1e90ff` 光效、扫描线+网格背景、等宽科技字体（系统字体栈：`"SF Mono", Menlo, monospace`）；中央顶部**反应堆圆环**（CSS/SVG 动画：常态呼吸，存在 running 任务时加速旋转）；开场 boot 动画（"J.A.R.V.I.S. 系统在线"逐字打出）。
- 布局：顶部状态条（CPU/内存/磁盘/codex 状态/活动任务数，来自 system 事件）；左侧会话列表（可新建）；中央对话流；右侧任务面板（Tab：定时任务/运行中/历史，定时任务可增删改、立即运行）；授权请求=全屏置顶红色警示卡（action/detail/risk，批准/拒绝大按钮）。
- 对话流渲染：用户消息右侧蓝框；贾维斯回复左侧青框（marked 不可用，简单处理换行+代码块即可）；执行过程=回复上方可折叠区，实时滚动显示 task_event（command_execution 显示 `$ command` + 输出，agent_message 中间消息，mcp_tool_call 显示工具名，未知类型显示 type）；task_done 后显示 usage tokens。
- 手机自适应（≤768px 侧栏抽屉化）；manifest.json + apple-touch-icon(icon-180.png) + `apple-mobile-web-app-capable` meta，可加 iPhone 主屏。
- icon：青色圆环反应堆风 SVG，icon-180.png 用 Pillow 画同款（脚本内联在 agent 工序里，画完删脚本）。

### 1.15 CLI（cli/jarvis，python脚本，chmod +x，shebang 用 venv python 绝对路径）

```
jarvis "指令"            # POST /api/chat 新会话，然后 WS 收流式事件打印：
                         #   [命令] $ ...  / 输出尾行 / 最终消息（青色 ANSI），task_done 退出
jarvis -s <session_id> "指令"   # 续会话
jarvis --status          # GET /api/system 表格打印
jarvis --approvals       # 列 pending 授权 + 交互式 y/n 决定
```

token/端口从项目根 .env 读取（脚本内定位自身路径→项目根）。

### 1.16 workspace/AGENTS.md（贾维斯人格，G 撰写，要点锁定）

- 身份：贾维斯（J.A.R.V.I.S.），大哥的 AI 管家；永远叫"大哥"，中文回复，简洁直接，报告用表格。
- 主动性：完成任务若大哥不在场（定时/后台任务）必须用 notify 汇报；发现值得长期记住的信息用 remember；被要求"每天/定期"做事用 schedule_task。
- **高危硬规则**（必须先 request_approval 且仅 approved 才执行）：删除/覆盖大量或重要文件、对外发送任何内容（邮件/消息/发布）、涉及金钱、部署线上、修改系统配置/开机项、git push/reset --hard、安装卸载系统级软件。detail 必须列精确清单不抽样。
- 长期记忆在 `memory.md`：复杂任务开始前先读。
- 工作目录即 workspace/，可自由读写其中文件；电脑全盘可读（sandbox full access），写盘外文件谨慎。

### 1.17 deploy/（G）

- `com.yunxin.jarvis.plist`：Label=com.yunxin.jarvis；ProgramArguments=[VENV_PY, -m, uvicorn, jarvis.server:app, --host, $JARVIS_HOST, --port, $JARVIS_PORT]；WorkingDirectory=项目根；RunAtLoad+KeepAlive=true；StandardOut/ErrorPath=项目 logs/jarvis.{out,err}.log。
- `install.sh`：检查 venv 存在 → 生成/校验 .env → mkdir -p data logs → cp plist 到 ~/Library/LaunchAgents/ → launchctl unload(忽略错误)+load → curl /healthz 重试10次验证 → 打印控制台地址（含局域网IP）。幂等可重跑。
- `uninstall.sh`：launchctl unload + rm plist。

## 2. 任务分解（团队并行，TDD）

每个 agent 工序统一：①读本计划相关契约 ②先写测试（pytest，能跑必须先跑红）③实现到全绿 ④`python -m pytest tests/test_<自己的>.py -v` 全过 ⑤汇报文件清单+测试结果（不准 git commit，由编排者统一提交）。
测试公共约定：用 tmp_path 建临时 DB；不碰真实 codex/Bark（B 用 fixtures/codex_events.jsonl 喂 FakeProcess；C 的 bark 用 httpx MockTransport 或 monkeypatch；E 用 FastAPI TestClient + FakeEngine）。

### Task A：db.py + approval.py
- 测试要点：schema 建表成功；CRUD 各方法行为与 1.4 一致；decide_approval 幂等（二次决定返回 None）；wait() approved/denied/超时三路径（timeout 用 0.1s 短超时测）；expire_stale。
### Task B：engine.py
- 测试要点：喂 fixtures 样本逐行 → on_event 次数与内容正确、EngineResult.thread_id/final_message/usage 正确；非 JSON 行忽略；非零退出抛 EngineError 且带 stderr；超时路径（用 sleep 的假进程）抛 EngineTimeout；cancel 杀进程；resume 与新会话命令行组装与 1.7 完全一致（直接断言 cmd 列表）。
### Task C：scheduler.py + push.py
- 测试要点：add/update/remove 持久化与 APScheduler 注册一致；start() 从 db 恢复；非法 cron 抛 ValueError；trigger_now 调 run_job；bark_push 无 key 返回 False 不抛错、有 key 时 POST payload 正确（MockTransport 断言）。
### Task D：mcp_server.py
- 测试要点：4 工具对 httpx 的请求 method/path/headers/body 正确（MockTransport）；request_approval 轮询直到 approved（mock 序列 pending→pending→approved）；环境变量缺失时报清晰错误。FastMCP 实例能 import 不崩。
### Task E：server.py + cli/jarvis
- 测试要点（TestClient + 注入 FakeEngine/FakeScheduler）：401 无 token；chat 创建任务并最终写回 jarvis 消息；busy 409；approvals decide 流（含 409）；cron CRUD + 422 非法 cron；internal 4 端点；/healthz 免认证；WS：错 token 关闭、ping/pong、task 事件广播（TestClient websocket_connect）。
- 模块组装：app = FastAPI()；lifespan 中初始化 db/engine/gateway/scheduler 并互相注入（按 1.13 语义）；全局单例放 app.state。
### Task F：web/
- 无单测；交付后由集成与实测阶段浏览器验证。必须严格按 1.12/1.14 实现；先写 README 注释式的事件→DOM 映射表再写代码。
### Task G：AGENTS.md + deploy + README
- README：架构图、启动方式（install.sh / 手动 uvicorn）、.env 说明、常见故障（codex 登录过期、Bark 未配置）。

## 3. 集成阶段（单 agent，顺序执行）

- [ ] pip install -r requirements.txt 全量装通（fastapi uvicorn[standard] apscheduler httpx "mcp[cli]" psutil python-dotenv pytest pytest-asyncio pillow）
- [ ] `python -m pytest tests/ -v` 全绿（修复跨模块接口错配，以契约为准）
- [ ] 手动起服务 `JARVIS_… uvicorn jarvis.server:app --port 8777`，curl /healthz、401、/api/system
- [ ] **真实 codex 冒烟**：POST /api/chat "运行 echo JARVIS_OK 并告诉我输出"，轮询 task 到 done，校验事件流入库
- [ ] **真实 MCP 注入验证**：POST /api/chat "调用 notify 工具发标题 test 内容 test，然后回复完成"（BARK_KEY 空则看服务日志出现降级记录即算通过）
- [ ] **真实授权流验证**：POST /api/chat "调用 request_approval 工具申请删除测试文件，等待结果并告诉我"，另开 curl decide approved，确认任务继续完成
- [ ] git add -A && git commit

## 4. 审查阶段

- code-reviewer + security-reviewer 并行审全部 diff；高优先级发现 → 修复 agent 修；pytest 回归全绿；commit。

## 5. 实测验收（按 CLAUDE.md 验证规矩）

- [ ] bash deploy/install.sh → launchctl 拉起 → /healthz 通过（LaunchAgent 属高危：清单=新增 ~/Library/LaunchAgents/com.yunxin.jarvis.plist，大哥已在方案中批准开机自启）
- [ ] Chrome 实开 http://localhost:8777：boot 动画、token 登录、发真实指令看流式过程、task 完成渲染
- [ ] 建一条 1 分钟后的 cron 任务，验证自动执行+记录
- [ ] 授权红卡全流程点批准
- [ ] 手机模拟：窄窗口自适应检查
- [ ] `jarvis "现在几点"` CLI 实跑
- [ ] 截图/GIF 留证，重启服务验证 KeepAlive

## 验收对照 spec §10：全部 6 条逐条打勾后才能向大哥汇报。

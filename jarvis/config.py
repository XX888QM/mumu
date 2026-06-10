"""贾维斯全局配置：从项目根 .env 加载，全部有默认值。

所有模块统一 `from jarvis.config import settings` 使用。
字段命名与实施计划 1.2 节锁定的契约一致。
"""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


class Settings:
    def __init__(self) -> None:
        g = os.environ.get
        self.jarvis_root = str(ROOT)
        self.jarvis_host = g("JARVIS_HOST", "0.0.0.0")
        self.jarvis_port = int(g("JARVIS_PORT", "8777"))
        self.jarvis_token = g("JARVIS_TOKEN", "")
        self.bark_key = g("BARK_KEY", "")
        self.bark_server = g("BARK_SERVER", "https://api.day.app")
        self.codex_bin = g("CODEX_BIN", "/Users/yunxin/.npm-global/bin/codex")
        self.jarvis_model = g("JARVIS_MODEL", "gpt-5.5")
        self.jarvis_reasoning = g("JARVIS_REASONING", "high")
        self.jarvis_sandbox = g("JARVIS_SANDBOX", "danger-full-access")
        self.jarvis_task_timeout = float(g("JARVIS_TASK_TIMEOUT", "3600"))
        self.approval_timeout = float(g("APPROVAL_TIMEOUT", "1800"))
        self.workspace = g("JARVIS_WORKSPACE", str(ROOT / "workspace"))
        self.db_path = g("JARVIS_DB", str(ROOT / "data" / "jarvis.db"))
        self.venv_py = str(ROOT / ".venv" / "bin" / "python")
        # MCP 桥运行时凭据文件（0600，server 启动时写入）：token 不进子进程 argv
        self.runtime_file = str(ROOT / "data" / ".runtime.json")


settings = Settings()

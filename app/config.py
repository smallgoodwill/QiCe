"""集中管理路径、约束与运行期配置。

原则：
1. 所有目录都从 PROJECT_ROOT 推导，禁止在业务模块里拼相对路径。
2. .env 只在这里载入一次；业务代码通过 os.environ 读取，不感知文件位置。
3. 静态资源目录（web/）与数据目录（data/）严格分离，
   这也是"密钥文件不下发到浏览器"的前提。
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录（app/ 的上一级）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# .env 使用 override=False：已存在的系统环境变量优先，
# 便于用 QICE_* 环境变量在 CI / 自检脚本里覆盖配置。
load_dotenv(PROJECT_ROOT / ".env", override=False)

# ---- 目录 ----------------------------------------------------------------
WEB_DIR = PROJECT_ROOT / "web"
DATA_ROOT = PROJECT_ROOT / "data"
SAMPLES_DIR = DATA_ROOT / "samples"

# 运行期数据（SQLite、日志）可用 QICE_DATA_DIR 指向临时目录，
# 自检脚本依靠它做到"不污染真实账本"。
_runtime_override = os.environ.get("QICE_DATA_DIR", "").strip()
RUNTIME_DIR = Path(_runtime_override) if _runtime_override else DATA_ROOT / "runtime"
LOG_DIR = RUNTIME_DIR / "logs"

DB_PATH = RUNTIME_DIR / "qice.sqlite3"
SAMPLE_CSV = SAMPLES_DIR / "sample_5m_synthetic.csv"

# ---- 服务 ----------------------------------------------------------------
DEFAULT_HOST = "127.0.0.1"          # 仅回环，禁止 0.0.0.0
DEFAULT_PORT = 8765
MAX_BODY_BYTES = 20 * 1024 * 1024   # 单请求上限 20 MB
MIN_BARS = 30
MAX_BARS = 20_000
MAX_ACTIVE_RUNS = 10

# ---- AI ------------------------------------------------------------------
AI_PROVIDERS = {
    "deepseek": {
        "label": "DeepSeek",
        "endpoint": "https://api.deepseek.com/chat/completions",
        "key_env": "DEEPSEEK_API_KEY",
        "model_env": "DEEPSEEK_MODEL",
        "default_model": "deepseek-flash",
    },
    "glm": {
        "label": "GLM",
        "endpoint": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        "key_env": "GLM_API_KEY",
        "model_env": "GLM_MODEL",
        "default_model": "glm-5",
    },
}


def default_ai_provider() -> str:
    provider = os.environ.get("AI_PROVIDER", "deepseek").strip().lower()
    return provider if provider in AI_PROVIDERS else "deepseek"


def ensure_dirs() -> None:
    """启动时创建运行期目录（SQLite 与日志的父目录必须存在）。"""
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

"""配置中心：.env 为默认值 + SQLite 设置库覆盖（保存即生效，无需重启）。

优先级：环境变量 > .env 文件（默认值） <-- 数据库设置（settings 表）> 用于任务执行。
调用 reload() 可随时重新加载（A4 配置热重载）。
"""
from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR_TASKS = DATA_DIR / "tasks"
DATA_DIR_RAW = DATA_DIR / "raw"
DATA_DIR_REPORTS = DATA_DIR / "reports"
SETTINGS_DB = DATA_DIR / "settings.db"
for _d in (DATA_DIR_TASKS, DATA_DIR_RAW, DATA_DIR_REPORTS, DATA_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# 生产环境的系统环境变量若设置了这些键，将作为最终基线（DB 设置再覆盖）
_ENV_BASELINE = {k: v for k, v in os.environ.items() if k.startswith(("SEARXNG_", "AI_", "DEEPSEEK_", "QWEN_", "HTTP_", "MAX_"))}

# 设置页可管理、入库的键
MANAGED_KEYS = (
    "SEARXNG_URL", "AI_ROUTER_BASE_URL", "AI_ROUTER_API_KEY", "AI_ROUTER_MODEL",
    "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL",
    "QWEN_API_KEY", "QWEN_MODEL", "QWEN_ENABLE_SEARCH",
    "AI_PRIMARY_PROVIDER", "ENABLED_GROUPS",
)
SECRET_KEYS = ("AI_ROUTER_API_KEY", "DEEPSEEK_API_KEY", "QWEN_API_KEY")


def _load_env(force: bool = False):
    """读取 .env 到进程环境（不覆盖已存在的环境变量，除非 force）。"""
    if load_dotenv is not None:
        load_dotenv(ROOT / ".env", override=force)


def _load_db() -> dict:
    """读取 SQLite 设置（返回 dict，覆盖 .env 默认值）。"""
    out: dict = {}
    try:
        import sqlite3
        conn = sqlite3.connect(str(SETTINGS_DB))
        try:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        finally:
            conn.close()
    except Exception:
        return out
    for k, v in rows:
        if k in MANAGED_KEYS:
            out[k] = v
    return out


USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def pick_provider():
    """按优先级选可用 provider：显式指定 > router > deepseek > qwen。"""
    if AI_PRIMARY_PROVIDER:
        return AI_PRIMARY_PROVIDER
    if AI_ROUTER.get("base_url"):
        return "router"
    if DEEPSEEK.get("api_key"):
        return "deepseek"
    if QWEN.get("api_key"):
        return "qwen"
    return "deepseek"


def _read_all():
    """把当前生效值读入模块级常量（每次 reload 后重新执行）。"""
    global SEARXNG_URL, SEARXNG_TIMEOUT, AI_ROUTER, DEEPSEEK, QWEN, \
        AI_PRIMARY_PROVIDER, HTTP_TIMEOUT, MAX_ITEMS_PER_QUERY, ENABLED_GROUPS

    SEARXNG_URL = os.getenv("SEARXNG_URL", "https://searxng.6556888.xyz").rstrip("/")
    SEARXNG_TIMEOUT = int(os.getenv("SEARXNG_TIMEOUT", "25"))
    AI_ROUTER = {
        "base_url": os.getenv("AI_ROUTER_BASE_URL", "").rstrip("/"),
        "api_key": os.getenv("AI_ROUTER_API_KEY", ""),
        "model": os.getenv("AI_ROUTER_MODEL", ""),
    }
    DEEPSEEK = {
        "api_key": os.getenv("DEEPSEEK_API_KEY", ""),
        "model": os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
    }
    QWEN = {
        "api_key": os.getenv("QWEN_API_KEY", ""),
        "model": os.getenv("QWEN_MODEL", "qwen-plus"),
        "enable_search": os.getenv("QWEN_ENABLE_SEARCH", "false").lower() in ("1", "true", "yes"),
    }
    AI_PRIMARY_PROVIDER = os.getenv("AI_PRIMARY_PROVIDER", "")
    HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "25"))
    MAX_ITEMS_PER_QUERY = int(os.getenv("MAX_ITEMS_PER_QUERY", "30"))
    ENABLED_GROUPS = [g.strip() for g in os.getenv("ENABLED_GROUPS", "news,wechat,web,video").split(",") if g.strip()]


def reload():
    """热重载：.env 默认值 + 系统环境变量基线 → 数据库设置覆盖。
    每次任务开始前调用，页面保存设置后无需重启服务。"""
    # 先清掉受管键的旧值，回到 .env 基线
    for k in MANAGED_KEYS:
        os.environ.pop(k, None)
    for k, v in _ENV_BASELINE.items():
        os.environ[k] = v
    _load_env(force=True)          # .env 覆盖（此时环境里已无残留 DB 值）
    db = _load_db()
    for k, v in db.items():
        os.environ[k] = v          # DB 设置最高优先级
    _read_all()


def db_settings() -> dict:
    """供设置页展示：返回 DB 中已存的受管设置（含密钥原文，仅后端使用）。"""
    return _load_db()


# ---- 启动时初始化（默认值 = .env）----
_load_env()
_read_all()
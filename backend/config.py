"""读取 .env 配置（纯 stdlib + python-dotenv，无 C 扩展依赖）。"""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass  # 无 dotenv 时直接读环境变量

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR_TASKS = DATA_DIR / "tasks"
DATA_DIR_RAW = DATA_DIR / "raw"
DATA_DIR_REPORTS = DATA_DIR / "reports"
for _d in (DATA_DIR_TASKS, DATA_DIR_RAW, DATA_DIR_REPORTS):
    _d.mkdir(parents=True, exist_ok=True)

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

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"


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
    return "deepseek"  # 默认指向 deepseek（无 key 时会报清晰错误）
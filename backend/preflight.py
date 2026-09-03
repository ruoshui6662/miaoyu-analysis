"""目标部署机预检：不打印密钥，给出可执行的上线前结论。"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "settings.db"


def run(*, require_docker: bool = False) -> dict:
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    add("docker", bool(shutil.which("docker")),
        "docker 命令可用" if shutil.which("docker") else "未安装 Docker")
    add("compose_files", all((ROOT / name).exists()
                              for name in ("docker-compose.yml",
                                           "docker-compose.cloudflare.yml",
                                           "docker-compose.searxng.yml")),
        "基础 Compose、SearXNG 与 Cloudflare 覆盖文件存在")
    add("env_template", (ROOT / ".env.example").exists(), ".env.example 存在")
    add("data_dir", DATA_DIR.exists(), "data 目录存在")
    if DB_PATH.exists():
        conn = sqlite3.connect(str(DB_PATH))
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            conn.close()
        add("sqlite", integrity == "ok", f"SQLite integrity_check={integrity}")
    else:
        add("sqlite", True, "尚未创建 settings.db，首次启动会初始化")
    failed = [item for item in checks if not item["ok"]]
    if not require_docker:
        failed = [item for item in failed if item["name"] != "docker"]
    return {"ok": not failed, "require_docker": require_docker, "checks": checks}


def main() -> None:
    parser = argparse.ArgumentParser(description="妙舆部署预检")
    parser.add_argument("--require-docker", action="store_true", help="把 Docker 缺失视为失败")
    args = parser.parse_args()
    result = run(require_docker=args.require_docker)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()

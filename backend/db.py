"""SQLite 设置库（纯 stdlib）：settings 表 key/value，设置页读写。

数据文件：data/settings.db（与报告、素材同卷，容器升级不丢）。
"""
from __future__ import annotations

import sqlite3

from config import MANAGED_KEYS, SETTINGS_DB


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(SETTINGS_DB))
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    return conn


def get_all() -> dict:
    """返回全部已保存设置（原文，仅后端使用）。"""
    conn = _conn()
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    finally:
        conn.close()
    return dict(rows)


def save(items: dict) -> int:
    """批量保存（只接受受管键）。返回写入条数。"""
    allowed = {k: str(v).strip() for k, v in items.items() if k in MANAGED_KEYS}
    conn = _conn()
    try:
        conn.executemany(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            list(allowed.items()),
        )
        conn.commit()
    finally:
        conn.close()
    return len(allowed)


def clear_all() -> None:
    """清空设置（回到 .env 默认）。"""
    conn = _conn()
    try:
        conn.execute("DELETE FROM settings")
        conn.commit()
    finally:
        conn.close()
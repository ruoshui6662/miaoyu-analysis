"""SQLite 本地库（纯 stdlib）：settings 表 + sources 表（精细信源）。

settings：key/value 键值配置（设置页）。
sources： 信源目录（名称/域名/类别/等级/采集方式/勾选状态），C1a。
"""
from __future__ import annotations

import json
import sqlite3

from config import MANAGED_KEYS, SETTINGS_DB

SOURCES_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  host TEXT NOT NULL DEFAULT '',
  category TEXT NOT NULL,
  level TEXT NOT NULL DEFAULT 'C',
  stype TEXT NOT NULL DEFAULT 'site',
  extra TEXT DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  manual INTEGER NOT NULL DEFAULT 0,
  sort INTEGER DEFAULT 0
)
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(SETTINGS_DB))
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(SOURCES_SCHEMA)
    return conn


# ---------- settings ----------

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


# ---------- sources ----------

def seed_sources(catalog: list[dict]) -> int:
    """播种内置信源目录：仅当表为空时写入。返回写入条数。"""
    conn = _conn()
    try:
        n = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        if n > 0:
            return 0
        rows = [
            (s["name"], s.get("host", ""), s["category"], s.get("level", "C"),
             s.get("stype", "site"), json.dumps(s.get("extra", {}), ensure_ascii=False),
             1 if s.get("enabled", True) else 0, 0, i)
            for i, s in enumerate(catalog)
        ]
        conn.executemany(
            "INSERT INTO sources(name, host, category, level, stype, extra, enabled, manual, sort) "
            "VALUES(?,?,?,?,?,?,?,?,?)", rows)
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def list_sources() -> list[dict]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, name, host, category, level, stype, extra, enabled, manual, sort "
            "FROM sources ORDER BY sort, id").fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        _id, name, host, cat, level, stype, extra, enabled, manual, sort = r
        out.append({
            "id": _id, "name": name, "host": host, "category": cat, "level": level,
            "stype": stype, "extra": json.loads(extra) if extra else {},
            "enabled": bool(enabled), "manual": bool(manual), "sort": sort,
        })
    return out


def set_enabled(ids: list[int], enabled: bool) -> int:
    conn = _conn()
    try:
        cur = conn.executemany(
            "UPDATE sources SET enabled = ? WHERE id = ?",
            [(1 if enabled else 0, i) for i in ids])
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def add_source(name: str, host: str, category: str, level: str = "C") -> int:
    conn = _conn()
    try:
        max_sort = conn.execute("SELECT COALESCE(MAX(sort),0) FROM sources").fetchone()[0]
        cur = conn.execute(
            "INSERT INTO sources(name, host, category, level, stype, enabled, manual, sort) "
            "VALUES(?,?,?,?,'site',1,1,?)", (name, host, category, level, max_sort + 1))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def delete_source(sid: int) -> bool:
    conn = _conn()
    try:
        cur = conn.execute("DELETE FROM sources WHERE id = ? AND manual = 1", (sid,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def enabled_sites() -> dict[str, str]:
    """返回已启用网站类信源的 host → level 映射（采集过滤/加权用）。"""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT host, level FROM sources WHERE enabled = 1 AND stype = 'site' "
            "AND host != ''").fetchall()
    finally:
        conn.close()
    out = {}
    for host, level in rows:
        for h in host.replace("，", ",").split(","):
            h = h.strip()
            if h:
                out[h] = level
    return out
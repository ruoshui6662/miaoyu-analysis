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

TASKS_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  topic TEXT NOT NULL DEFAULT '',
  provider TEXT DEFAULT '',
  verify INTEGER DEFAULT 0,
  status TEXT DEFAULT 'pending',
  step TEXT DEFAULT '',
  detail TEXT DEFAULT '',
  created_at TEXT DEFAULT '',
  finished_at TEXT DEFAULT '',
  report_file TEXT DEFAULT '',
  report_summary TEXT DEFAULT '',
  error TEXT DEFAULT ''
)
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(SETTINGS_DB))
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(SOURCES_SCHEMA)
    conn.execute(TASKS_SCHEMA)
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


# ---------- tasks（B3 历史持久化）----------

def task_create(tid: str, topic: str, provider: str = "", verify: bool = False,
                created_at: str = "") -> None:
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO tasks(id, topic, provider, verify, status, created_at) "
            "VALUES(?,?,?,?, 'pending', ?)",
            (tid, topic, provider, 1 if verify else 0, created_at))
        conn.commit()
    finally:
        conn.close()


def task_update(tid: str, **fields) -> None:
    """更新任务字段（status/step/detail/report_file/report_summary/error/finished_at）。"""
    allowed = ("status", "step", "detail", "report_file", "report_summary", "error", "finished_at")
    items = {k: v for k, v in fields.items() if k in allowed}
    if not items:
        return
    cols = ", ".join(f"{k} = ?" for k in items)
    conn = _conn()
    try:
        conn.execute(f"UPDATE tasks SET {cols} WHERE id = ?", (*items.values(), tid))
        conn.commit()
    finally:
        conn.close()


def task_list(limit: int = 100) -> list[dict]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, topic, provider, verify, status, step, detail, created_at, "
            "finished_at, report_summary, error FROM tasks "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        _id, topic, provider, verify, status, step, detail, created_at, finished_at, summary, error = r
        out.append({
            "id": _id, "topic": topic, "provider": provider, "verify": bool(verify),
            "status": status, "step": step, "detail": detail,
            "created_at": created_at, "finished_at": finished_at or "",
            "report_summary": json.loads(summary) if summary else None, "error": error or "",
        })
    return out


def task_get(tid: str) -> dict | None:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, topic, provider, verify, status, step, detail, created_at, "
            "finished_at, report_file, report_summary, error FROM tasks WHERE id = ?",
            (tid,)).fetchall()
    finally:
        conn.close()
    if not rows:
        return None
    r = rows[0]
    _id, topic, provider, verify, status, step, detail, created_at, finished_at, report_file, summary, error = r
    return {
        "id": _id, "topic": topic, "provider": provider, "verify": bool(verify),
        "status": status, "step": step, "detail": detail,
        "created_at": created_at, "finished_at": finished_at or "",
        "report_file": report_file or "", "report_summary": json.loads(summary) if summary else None,
        "error": error or "",
    }
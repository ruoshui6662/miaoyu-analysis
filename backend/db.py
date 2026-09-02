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

EVIDENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS scan_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id TEXT NOT NULL,
  started_at TEXT NOT NULL DEFAULT '',
  finished_at TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'running',
  item_count INTEGER NOT NULL DEFAULT 0,
  error_message TEXT DEFAULT ''
)
;
CREATE INDEX IF NOT EXISTS idx_scan_runs_source_started
  ON scan_runs(source_id, started_at DESC)
;
CREATE TABLE IF NOT EXISTS mentions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  content_hash TEXT NOT NULL,
  canonical_url TEXT NOT NULL DEFAULT '',
  source_id TEXT NOT NULL,
  source_type TEXT NOT NULL DEFAULT 'site',
  title TEXT NOT NULL DEFAULT '',
  snippet TEXT DEFAULT '',
  body TEXT DEFAULT '',
  published_at TEXT DEFAULT '',
  captured_at TEXT NOT NULL DEFAULT '',
  topic_id TEXT DEFAULT '',
  credibility TEXT DEFAULT '',
  fetch_status TEXT NOT NULL DEFAULT 'captured',
  raw_path TEXT DEFAULT '',
  engagement_json TEXT DEFAULT '',
  UNIQUE(source_id, content_hash)
)
;
CREATE INDEX IF NOT EXISTS idx_mentions_captured_at
  ON mentions(captured_at DESC)
;
CREATE INDEX IF NOT EXISTS idx_mentions_topic_captured
  ON mentions(topic_id, captured_at DESC)
;
CREATE TABLE IF NOT EXISTS hot_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scan_run_id INTEGER NOT NULL,
  mention_id INTEGER,
  board_id TEXT NOT NULL,
  item_key TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  canonical_url TEXT DEFAULT '',
  rank INTEGER,
  hot_value TEXT DEFAULT '',
  captured_at TEXT NOT NULL DEFAULT '',
  provider TEXT DEFAULT '',
  UNIQUE(scan_run_id, board_id, item_key),
  FOREIGN KEY(scan_run_id) REFERENCES scan_runs(id),
  FOREIGN KEY(mention_id) REFERENCES mentions(id)
)
;
CREATE INDEX IF NOT EXISTS idx_hot_items_board_captured
  ON hot_items(board_id, captured_at DESC)
;
CREATE TABLE IF NOT EXISTS cursors (
  source_id TEXT PRIMARY KEY,
  cursor_value TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT ''
)
;
CREATE TABLE IF NOT EXISTS topics (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  keywords_json TEXT NOT NULL DEFAULT '[]',
  exclude_keywords_json TEXT NOT NULL DEFAULT '[]',
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT ''
)
;
CREATE TABLE IF NOT EXISTS subscriptions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  topic_id TEXT NOT NULL UNIQUE,
  interval_seconds INTEGER NOT NULL DEFAULT 600,
  enabled INTEGER NOT NULL DEFAULT 1,
  last_run_at TEXT DEFAULT '',
  next_run_at TEXT DEFAULT '',
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  cooldown_until TEXT DEFAULT '',
  created_at TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(topic_id) REFERENCES topics(id)
)
;
CREATE TABLE IF NOT EXISTS monitor_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  subscription_id INTEGER NOT NULL,
  topic_id TEXT NOT NULL,
  started_at TEXT NOT NULL DEFAULT '',
  finished_at TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'running',
  item_count INTEGER NOT NULL DEFAULT 0,
  new_count INTEGER NOT NULL DEFAULT 0,
  cursor_before TEXT DEFAULT '',
  cursor_after TEXT DEFAULT '',
  error_message TEXT DEFAULT '',
  FOREIGN KEY(subscription_id) REFERENCES subscriptions(id),
  FOREIGN KEY(topic_id) REFERENCES topics(id)
)
;
CREATE INDEX IF NOT EXISTS idx_monitor_runs_topic_started
  ON monitor_runs(topic_id, started_at DESC)
;
CREATE TABLE IF NOT EXISTS mention_topics (
  mention_id INTEGER NOT NULL,
  topic_id TEXT NOT NULL,
  first_seen_at TEXT NOT NULL DEFAULT '',
  last_seen_at TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(mention_id, topic_id),
  FOREIGN KEY(mention_id) REFERENCES mentions(id),
  FOREIGN KEY(topic_id) REFERENCES topics(id)
)
;
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  topic_id TEXT NOT NULL,
  normalized_key TEXT NOT NULL,
  canonical_title TEXT NOT NULL DEFAULT '',
  grouping_method TEXT NOT NULL DEFAULT 'exact_key',
  first_seen_at TEXT NOT NULL DEFAULT '',
  last_seen_at TEXT NOT NULL DEFAULT '',
  mention_count INTEGER NOT NULL DEFAULT 0,
  platform_count INTEGER NOT NULL DEFAULT 0,
  heat_score REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT '',
  UNIQUE(topic_id, normalized_key),
  FOREIGN KEY(topic_id) REFERENCES topics(id)
)
;
CREATE INDEX IF NOT EXISTS idx_events_topic_last_seen
  ON events(topic_id, last_seen_at DESC)
;
CREATE TABLE IF NOT EXISTS event_mentions (
  event_id INTEGER NOT NULL,
  mention_id INTEGER NOT NULL,
  topic_id TEXT NOT NULL,
  grouping_method TEXT NOT NULL DEFAULT 'exact_key',
  first_seen_at TEXT NOT NULL DEFAULT '',
  last_seen_at TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(event_id, mention_id),
  UNIQUE(topic_id, mention_id),
  FOREIGN KEY(event_id) REFERENCES events(id),
  FOREIGN KEY(mention_id) REFERENCES mentions(id),
  FOREIGN KEY(topic_id) REFERENCES topics(id)
)
;
CREATE INDEX IF NOT EXISTS idx_event_mentions_topic_seen
  ON event_mentions(topic_id, last_seen_at DESC)
;
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  topic_id TEXT NOT NULL,
  event_id INTEGER NOT NULL,
  signal_type TEXT NOT NULL,
  severity TEXT NOT NULL DEFAULT 'medium',
  title TEXT NOT NULL DEFAULT '',
  payload_json TEXT NOT NULL DEFAULT '{}',
  observed_at TEXT NOT NULL DEFAULT '',
  acknowledged INTEGER NOT NULL DEFAULT 0,
  dedupe_key TEXT NOT NULL UNIQUE,
  FOREIGN KEY(topic_id) REFERENCES topics(id),
  FOREIGN KEY(event_id) REFERENCES events(id)
)
;
CREATE INDEX IF NOT EXISTS idx_signals_topic_observed
  ON signals(topic_id, observed_at DESC)
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(SETTINGS_DB), timeout=15)
    conn.execute("PRAGMA busy_timeout = 15000")
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(SOURCES_SCHEMA)
    conn.execute(TASKS_SCHEMA)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(EVIDENCE_SCHEMA)
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


# ---------- evidence / hot snapshots (G1) ----------

def scan_run_create(source_id: str, started_at: str) -> int:
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT INTO scan_runs(source_id, started_at, status) VALUES(?,?, 'running')",
            (source_id, started_at),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def scan_run_finish(run_id: int, *, status: str, item_count: int,
                    finished_at: str, error_message: str = "") -> None:
    conn = _conn()
    try:
        conn.execute(
            "UPDATE scan_runs SET finished_at=?, status=?, item_count=?, error_message=? WHERE id=?",
            (finished_at, status, int(item_count), error_message[:500], run_id),
        )
        conn.commit()
    finally:
        conn.close()


def mention_upsert(mention: dict) -> tuple[int, bool]:
    """按 source_id + content_hash 幂等写入证据，返回 (mention_id, 是否新建)。"""
    conn = _conn()
    try:
        existing = conn.execute(
            "SELECT id FROM mentions WHERE source_id=? AND content_hash=?",
            (mention["source_id"], mention["content_hash"]),
        ).fetchone()
        columns = (
            "content_hash", "canonical_url", "source_id", "source_type", "title",
            "snippet", "body", "published_at", "captured_at", "topic_id",
            "credibility", "fetch_status", "raw_path", "engagement_json",
        )
        values = tuple(str(mention.get(k) or "") for k in columns)
        conn.execute(
            "INSERT INTO mentions(" + ",".join(columns) + ") VALUES(" + ",".join("?" for _ in columns) + ") "
            "ON CONFLICT(source_id, content_hash) DO UPDATE SET "
            "canonical_url=excluded.canonical_url, title=excluded.title, snippet=excluded.snippet, "
            "body=CASE WHEN excluded.body != '' THEN excluded.body ELSE mentions.body END, "
            "published_at=CASE WHEN excluded.published_at != '' THEN excluded.published_at ELSE mentions.published_at END, "
            "captured_at=excluded.captured_at, credibility=excluded.credibility, "
            "fetch_status=excluded.fetch_status, raw_path=excluded.raw_path, "
            "engagement_json=excluded.engagement_json",
            values,
        )
        row = conn.execute(
            "SELECT id FROM mentions WHERE source_id=? AND content_hash=?",
            (mention["source_id"], mention["content_hash"]),
        ).fetchone()
        conn.commit()
        if row is None:
            raise RuntimeError("mention upsert succeeded but row could not be read")
        return int(row[0]), existing is None
    finally:
        conn.close()


def hot_item_insert(item: dict) -> int:
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT INTO hot_items(scan_run_id, mention_id, board_id, item_key, title, canonical_url, "
            "rank, hot_value, captured_at, provider) VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(scan_run_id, board_id, item_key) DO UPDATE SET "
            "mention_id=excluded.mention_id, title=excluded.title, canonical_url=excluded.canonical_url, "
            "rank=excluded.rank, hot_value=excluded.hot_value, provider=excluded.provider",
            (item["scan_run_id"], item.get("mention_id"), item["board_id"], item["item_key"],
             item.get("title", ""), item.get("canonical_url", ""), item.get("rank"),
             item.get("hot_value", ""), item.get("captured_at", ""), item.get("provider", "")),
        )
        conn.commit()
        return int(cur.lastrowid or conn.execute(
            "SELECT id FROM hot_items WHERE scan_run_id=? AND board_id=? AND item_key=?",
            (item["scan_run_id"], item["board_id"], item["item_key"]),
        ).fetchone()[0])
    finally:
        conn.close()


def record_hot_snapshot(board_id: str, rows: list[dict], captured_at: str) -> dict:
    """单事务写入一个榜单快照，避免逐条开连接造成 SQLite 锁竞争。"""
    conn = _conn()
    run_id = 0
    item_count = 0
    mentions_new = 0
    try:
        signature = [
            (str(row["mention"]["content_hash"]), int(row.get("rank") or 0),
             str(row.get("hot_value") or ""))
            for row in rows
        ]
        latest = conn.execute(
            "SELECT id FROM scan_runs WHERE source_id=? AND status='success' "
            "ORDER BY id DESC LIMIT 1",
            (board_id,),
        ).fetchone()
        if latest:
            previous = conn.execute(
                "SELECT item_key, COALESCE(rank, 0), COALESCE(hot_value, '') "
                "FROM hot_items WHERE scan_run_id=? ORDER BY rank, item_key",
                (latest[0],),
            ).fetchall()
            if previous == sorted(signature, key=lambda value: (value[1], value[0])):
                return {"run_id": int(latest[0]), "items": 0, "mentions_new": 0,
                        "skipped": True}
        cur = conn.execute(
            "INSERT INTO scan_runs(source_id, started_at, status) VALUES(?,?, 'running')",
            (board_id, captured_at),
        )
        run_id = int(cur.lastrowid)
        for row in rows:
            mention = row["mention"]
            existing = conn.execute(
                "SELECT id FROM mentions WHERE source_id=? AND content_hash=?",
                (mention["source_id"], mention["content_hash"]),
            ).fetchone()
            columns = (
                "content_hash", "canonical_url", "source_id", "source_type", "title",
                "snippet", "body", "published_at", "captured_at", "topic_id",
                "credibility", "fetch_status", "raw_path", "engagement_json",
            )
            values = tuple(str(mention.get(k) or "") for k in columns)
            conn.execute(
                "INSERT INTO mentions(" + ",".join(columns) + ") VALUES(" + ",".join("?" for _ in columns) + ") "
                "ON CONFLICT(source_id, content_hash) DO UPDATE SET "
                "canonical_url=excluded.canonical_url, title=excluded.title, snippet=excluded.snippet, "
                "body=CASE WHEN excluded.body != '' THEN excluded.body ELSE mentions.body END, "
                "published_at=CASE WHEN excluded.published_at != '' THEN excluded.published_at ELSE mentions.published_at END, "
                "captured_at=excluded.captured_at, credibility=excluded.credibility, "
                "fetch_status=excluded.fetch_status, raw_path=excluded.raw_path, "
                "engagement_json=excluded.engagement_json",
                values,
            )
            mention_id = conn.execute(
                "SELECT id FROM mentions WHERE source_id=? AND content_hash=?",
                (mention["source_id"], mention["content_hash"]),
            ).fetchone()[0]
            mentions_new += int(existing is None)
            conn.execute(
                "INSERT INTO hot_items(scan_run_id, mention_id, board_id, item_key, title, canonical_url, "
                "rank, hot_value, captured_at, provider) VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(scan_run_id, board_id, item_key) DO UPDATE SET "
                "mention_id=excluded.mention_id, title=excluded.title, canonical_url=excluded.canonical_url, "
                "rank=excluded.rank, hot_value=excluded.hot_value, provider=excluded.provider",
                (run_id, mention_id, board_id, mention["content_hash"], mention["title"],
                 mention["canonical_url"], row.get("rank"), row.get("hot_value", ""),
                 captured_at, row.get("provider", "")),
            )
            item_count += 1
        conn.execute(
            "UPDATE scan_runs SET finished_at=?, status=?, item_count=? WHERE id=?",
            (captured_at, "success" if item_count else "empty", item_count, run_id),
        )
        conn.commit()
        return {"run_id": run_id, "items": item_count, "mentions_new": mentions_new,
                "skipped": False}
    except Exception as exc:
        conn.rollback()
        if run_id:
            try:
                conn.execute(
                    "UPDATE scan_runs SET finished_at=?, status='error', item_count=?, error_message=? WHERE id=?",
                    (captured_at, item_count, str(exc)[:500], run_id),
                )
                conn.commit()
            except sqlite3.Error:
                conn.rollback()
        raise
    finally:
        conn.close()


def hot_history(board_id: str, since: str = "", limit: int = 1000) -> list[dict]:
    """读取榜单历史快照，结果按采集时间倒序、排名正序返回。"""
    conn = _conn()
    try:
        query = (
            "SELECT h.id, h.scan_run_id, h.board_id, h.item_key, h.mention_id, h.title, "
            "h.canonical_url, h.rank, h.hot_value, h.captured_at, h.provider "
            "FROM hot_items h JOIN scan_runs r ON r.id=h.scan_run_id "
            "WHERE h.board_id=? AND r.status='success'"
        )
        params: list = [board_id]
        if since:
            query += " AND h.captured_at >= ?"
            params.append(since)
        query += " ORDER BY h.captured_at DESC, COALESCE(h.rank, 999999), h.id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 5000)))
        rows = conn.execute(query, params).fetchall()
        return [{
            "id": row[0], "scan_run_id": row[1], "board_id": row[2], "item_key": row[3],
            "mention_id": row[4], "title": row[5], "url": row[6], "rank": row[7],
            "hot": row[8], "captured_at": row[9], "provider": row[10],
        } for row in rows]
    finally:
        conn.close()


def hot_rank_changes(board_id: str) -> dict:
    """计算最近两个成功快照的排名变化，key 为 item_key。"""
    conn = _conn()
    try:
        runs = conn.execute(
            "SELECT id, started_at FROM scan_runs WHERE source_id=? AND status='success' "
            "ORDER BY id DESC LIMIT 2", (board_id,)
        ).fetchall()
        if not runs:
            return {"current_scan_at": "", "previous_scan_at": "", "items": {}}
        current_id, current_at = runs[0]
        previous_id, previous_at = runs[1] if len(runs) > 1 else (None, "")
        current_rows = conn.execute(
            "SELECT item_key, title, canonical_url, rank, hot_value, provider "
            "FROM hot_items WHERE scan_run_id=? ORDER BY COALESCE(rank, 999999), id",
            (current_id,),
        ).fetchall()
        previous_rows = conn.execute(
            "SELECT item_key, rank FROM hot_items WHERE scan_run_id=?",
            (previous_id,),
        ).fetchall() if previous_id else []
        previous = {str(row[0]): row[1] for row in previous_rows}
        items = {}
        for row in current_rows:
            key = str(row[0])
            rank = row[3]
            old_rank = previous.get(key)
            items[key] = {
                "title": row[1], "url": row[2], "rank": rank, "hot": row[4],
                "provider": row[5], "previous_rank": old_rank,
                "rank_change": (old_rank - rank) if old_rank is not None and rank is not None else None,
                "is_new": old_rank is None,
            }
        return {"current_scan_at": current_at, "previous_scan_at": previous_at, "items": items}
    finally:
        conn.close()


def cursor_upsert(source_id: str, cursor_value: str, updated_at: str) -> None:
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO cursors(source_id, cursor_value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(source_id) DO UPDATE SET cursor_value=excluded.cursor_value, updated_at=excluded.updated_at",
            (source_id, cursor_value, updated_at),
        )
        conn.commit()
    finally:
        conn.close()


# ---------- monitoring subscriptions (G2) ----------

def topic_create(topic_id: str, name: str, keywords: list[str],
                 exclude_keywords: list[str], now: str,
                 enabled: bool = True) -> None:
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO topics(id, name, keywords_json, exclude_keywords_json, enabled, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (topic_id, name, json.dumps(keywords, ensure_ascii=False),
             json.dumps(exclude_keywords, ensure_ascii=False),
             1 if enabled else 0, now, now),
        )
        conn.commit()
    finally:
        conn.close()


def topic_get(topic_id: str) -> dict | None:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT id, name, keywords_json, exclude_keywords_json, enabled, created_at, updated_at "
            "FROM topics WHERE id=?", (topic_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {
        "id": row[0], "name": row[1],
        "keywords": json.loads(row[2] or "[]"),
        "exclude_keywords": json.loads(row[3] or "[]"),
        "enabled": bool(row[4]), "created_at": row[5], "updated_at": row[6],
    }


def topic_list() -> list[dict]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, name, keywords_json, exclude_keywords_json, enabled, created_at, updated_at "
            "FROM topics ORDER BY created_at DESC, id DESC"
        ).fetchall()
    finally:
        conn.close()
    return [{
        "id": row[0], "name": row[1],
        "keywords": json.loads(row[2] or "[]"),
        "exclude_keywords": json.loads(row[3] or "[]"),
        "enabled": bool(row[4]), "created_at": row[5], "updated_at": row[6],
    } for row in rows]


def topic_set_enabled(topic_id: str, enabled: bool, now: str) -> bool:
    conn = _conn()
    try:
        cur = conn.execute("UPDATE topics SET enabled=?, updated_at=? WHERE id=?",
                           (1 if enabled else 0, now, topic_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def subscription_upsert(topic_id: str, interval_seconds: int, enabled: bool,
                        now: str, next_run_at: str = "") -> int:
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO subscriptions(topic_id, interval_seconds, enabled, next_run_at, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(topic_id) DO UPDATE SET "
            "interval_seconds=excluded.interval_seconds, enabled=excluded.enabled, "
            "updated_at=excluded.updated_at",
            (topic_id, max(60, int(interval_seconds)), 1 if enabled else 0,
             next_run_at or now, now, now),
        )
        row = conn.execute("SELECT id FROM subscriptions WHERE topic_id=?", (topic_id,)).fetchone()
        conn.commit()
        return int(row[0])
    finally:
        conn.close()


def subscription_get(subscription_id: int) -> dict | None:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT id, topic_id, interval_seconds, enabled, last_run_at, next_run_at, "
            "consecutive_failures, cooldown_until, created_at, updated_at "
            "FROM subscriptions WHERE id=?", (subscription_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {"id": row[0], "topic_id": row[1], "interval_seconds": row[2],
            "enabled": bool(row[3]), "last_run_at": row[4] or "",
            "next_run_at": row[5] or "", "consecutive_failures": row[6],
            "cooldown_until": row[7] or "", "created_at": row[8], "updated_at": row[9]}


def subscription_get_by_topic(topic_id: str) -> dict | None:
    conn = _conn()
    try:
        row = conn.execute("SELECT id FROM subscriptions WHERE topic_id=?", (topic_id,)).fetchone()
    finally:
        conn.close()
    return subscription_get(int(row[0])) if row else None


def subscription_set_enabled(subscription_id: int, enabled: bool, now: str) -> None:
    conn = _conn()
    try:
        conn.execute("UPDATE subscriptions SET enabled=?, updated_at=? WHERE id=?",
                     (1 if enabled else 0, now, subscription_id))
        conn.commit()
    finally:
        conn.close()


def subscriptions_due(now: str) -> list[dict]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, topic_id, interval_seconds, enabled, last_run_at, next_run_at, "
            "consecutive_failures, cooldown_until, created_at, updated_at "
            "FROM subscriptions WHERE enabled=1 AND "
            "(next_run_at='' OR next_run_at<=?) AND (cooldown_until='' OR cooldown_until<=?) "
            "ORDER BY id", (now, now)
        ).fetchall()
    finally:
        conn.close()
    return [{"id": row[0], "topic_id": row[1], "interval_seconds": row[2],
             "enabled": bool(row[3]), "last_run_at": row[4] or "",
             "next_run_at": row[5] or "", "consecutive_failures": row[6],
             "cooldown_until": row[7] or "", "created_at": row[8], "updated_at": row[9]}
            for row in rows]


def subscription_mark_success(subscription_id: int, now: str, next_run_at: str) -> None:
    conn = _conn()
    try:
        conn.execute(
            "UPDATE subscriptions SET last_run_at=?, next_run_at=?, consecutive_failures=0, "
            "cooldown_until='', updated_at=? WHERE id=?",
            (now, next_run_at, now, subscription_id),
        )
        conn.commit()
    finally:
        conn.close()


def subscription_mark_failure(subscription_id: int, now: str, next_run_at: str,
                              cooldown_until: str) -> None:
    conn = _conn()
    try:
        conn.execute(
            "UPDATE subscriptions SET next_run_at=?, cooldown_until=?, "
            "consecutive_failures=consecutive_failures+1, updated_at=? WHERE id=?",
            (next_run_at, cooldown_until, now, subscription_id),
        )
        conn.commit()
    finally:
        conn.close()


def monitor_run_create(subscription_id: int, topic_id: str, started_at: str,
                       cursor_before: str) -> int:
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT INTO monitor_runs(subscription_id, topic_id, started_at, cursor_before) "
            "VALUES(?,?,?,?)", (subscription_id, topic_id, started_at, cursor_before),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def monitor_run_finish(run_id: int, *, status: str, finished_at: str,
                       item_count: int, new_count: int, cursor_after: str = "",
                       error_message: str = "") -> None:
    conn = _conn()
    try:
        conn.execute(
            "UPDATE monitor_runs SET finished_at=?, status=?, item_count=?, new_count=?, "
            "cursor_after=?, error_message=? WHERE id=?",
            (finished_at, status, int(item_count), int(new_count), cursor_after,
             error_message[:500], run_id),
        )
        conn.commit()
    finally:
        conn.close()


def mention_topic_touch(mention_id: int, topic_id: str, seen_at: str) -> None:
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO mention_topics(mention_id, topic_id, first_seen_at, last_seen_at) "
            "VALUES(?,?,?,?) ON CONFLICT(mention_id, topic_id) DO UPDATE SET last_seen_at=excluded.last_seen_at",
            (mention_id, topic_id, seen_at, seen_at),
        )
        conn.commit()
    finally:
        conn.close()


def monitor_runs(topic_id: str = "", limit: int = 100) -> list[dict]:
    conn = _conn()
    try:
        query = (
            "SELECT id, subscription_id, topic_id, started_at, finished_at, status, item_count, "
            "new_count, cursor_before, cursor_after, error_message FROM monitor_runs"
        )
        params: list = []
        if topic_id:
            query += " WHERE topic_id=?"
            params.append(topic_id)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()
    return [{"id": r[0], "subscription_id": r[1], "topic_id": r[2],
             "started_at": r[3], "finished_at": r[4] or "", "status": r[5],
             "item_count": r[6], "new_count": r[7], "cursor_before": r[8] or "",
             "cursor_after": r[9] or "", "error_message": r[10] or ""} for r in rows]


def monitor_runs_window(topic_id: str, since: str, until: str,
                        limit: int = 500) -> list[dict]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, subscription_id, topic_id, started_at, finished_at, status, item_count, "
            "new_count, cursor_before, cursor_after, error_message FROM monitor_runs "
            "WHERE topic_id=? AND started_at>=? AND started_at<=? "
            "ORDER BY id DESC LIMIT ?",
            (topic_id, since, until, max(1, min(int(limit), 1000))),
        ).fetchall()
    finally:
        conn.close()
    return [{"id": r[0], "subscription_id": r[1], "topic_id": r[2],
             "started_at": r[3], "finished_at": r[4] or "", "status": r[5],
             "item_count": r[6], "new_count": r[7], "cursor_before": r[8] or "",
             "cursor_after": r[9] or "", "error_message": r[10] or ""} for r in rows]


def cursor_get(source_id: str) -> str:
    conn = _conn()
    try:
        row = conn.execute("SELECT cursor_value FROM cursors WHERE source_id=?", (source_id,)).fetchone()
        return row[0] if row else ""
    finally:
        conn.close()


# ---------- event aggregation (G2.5) ----------

def event_candidates(topic_id: str, cutoff_at: str, limit: int = 200) -> list[dict]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, normalized_key, canonical_title, last_seen_at "
            "FROM events WHERE topic_id=? AND last_seen_at>=? "
            "ORDER BY last_seen_at DESC LIMIT ?",
            (topic_id, cutoff_at, max(1, min(int(limit), 1000))),
        ).fetchall()
    finally:
        conn.close()
    return [{"id": r[0], "normalized_key": r[1], "canonical_title": r[2],
             "last_seen_at": r[3]} for r in rows]


def event_create(topic_id: str, normalized_key: str, canonical_title: str,
                 grouping_method: str, seen_at: str) -> int:
    conn = _conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO events(topic_id, normalized_key, canonical_title, "
            "grouping_method, first_seen_at, last_seen_at, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (topic_id, normalized_key, canonical_title, grouping_method,
             seen_at, seen_at, seen_at, seen_at),
        )
        row = conn.execute(
            "SELECT id FROM events WHERE topic_id=? AND normalized_key=?",
            (topic_id, normalized_key),
        ).fetchone()
        conn.commit()
        return int(row[0])
    finally:
        conn.close()


def event_attach(event_id: int, mention_id: int, topic_id: str,
                 grouping_method: str, seen_at: str) -> None:
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO event_mentions(event_id, mention_id, topic_id, grouping_method, "
            "first_seen_at, last_seen_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(event_id, mention_id) DO UPDATE SET last_seen_at=excluded.last_seen_at",
            (event_id, mention_id, topic_id, grouping_method, seen_at, seen_at),
        )
        conn.execute(
            "UPDATE events SET grouping_method=?, last_seen_at=?, updated_at=? WHERE id=?",
            (grouping_method, seen_at, seen_at, event_id),
        )
        conn.commit()
    finally:
        conn.close()


def event_stats(event_id: int) -> dict:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT m.source_id), MIN(em.first_seen_at), "
            "MAX(em.last_seen_at) "
            "FROM event_mentions em JOIN mentions m ON m.id=em.mention_id "
            "WHERE em.event_id=?", (event_id,),
        ).fetchone()
        hot_rows = conn.execute(
            "SELECT m.engagement_json FROM event_mentions em "
            "JOIN mentions m ON m.id=em.mention_id WHERE em.event_id=?",
            (event_id,),
        ).fetchall()
    finally:
        conn.close()
    return {"mention_count": int(row[0]), "platform_count": int(row[1]),
            "first_seen_at": row[2] or "", "last_seen_at": row[3] or "",
            "engagement_jsons": [r[0] or "" for r in hot_rows]}


def event_update_metrics(event_id: int, *, mention_count: int,
                         platform_count: int, first_seen_at: str,
                         last_seen_at: str, heat_score: float,
                         updated_at: str) -> None:
    conn = _conn()
    try:
        conn.execute(
            "UPDATE events SET mention_count=?, platform_count=?, first_seen_at=?, "
            "last_seen_at=?, heat_score=?, updated_at=? WHERE id=?",
            (int(mention_count), int(platform_count), first_seen_at, last_seen_at,
             float(heat_score), updated_at, event_id),
        )
        conn.commit()
    finally:
        conn.close()


def event_list(topic_id: str = "", limit: int = 100) -> list[dict]:
    conn = _conn()
    try:
        query = (
            "SELECT id, topic_id, canonical_title, normalized_key, grouping_method, "
            "first_seen_at, last_seen_at, mention_count, platform_count, heat_score, "
            "created_at, updated_at FROM events"
        )
        params: list = []
        if topic_id:
            query += " WHERE topic_id=?"
            params.append(topic_id)
        query += " ORDER BY heat_score DESC, last_seen_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()
    return [{"id": r[0], "topic_id": r[1], "canonical_title": r[2],
             "normalized_key": r[3], "grouping_method": r[4],
             "first_seen_at": r[5], "last_seen_at": r[6],
             "mention_count": r[7], "platform_count": r[8],
             "heat_score": r[9], "created_at": r[10], "updated_at": r[11]} for r in rows]


def event_list_window(topic_id: str, since: str, until: str,
                      limit: int = 100) -> list[dict]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, topic_id, canonical_title, normalized_key, grouping_method, "
            "first_seen_at, last_seen_at, mention_count, platform_count, heat_score, "
            "created_at, updated_at FROM events WHERE topic_id=? AND last_seen_at>=? "
            "AND last_seen_at<=? ORDER BY heat_score DESC, last_seen_at DESC LIMIT ?",
            (topic_id, since, until, max(1, min(int(limit), 500))),
        ).fetchall()
    finally:
        conn.close()
    return [{"id": r[0], "topic_id": r[1], "canonical_title": r[2],
             "normalized_key": r[3], "grouping_method": r[4],
             "first_seen_at": r[5], "last_seen_at": r[6],
             "mention_count": r[7], "platform_count": r[8],
             "heat_score": r[9], "created_at": r[10], "updated_at": r[11]} for r in rows]


def event_mentions(event_id: int, since: str = "", until: str = "",
                    limit: int = 100) -> list[dict]:
    conn = _conn()
    try:
        query = (
            "SELECT em.mention_id, m.source_id, m.title, m.canonical_url, "
            "m.engagement_json, em.first_seen_at, em.last_seen_at "
            "FROM event_mentions em JOIN mentions m ON m.id=em.mention_id "
            "WHERE em.event_id=?"
        )
        params: list = [event_id]
        if since:
            query += " AND em.last_seen_at>=?"
            params.append(since)
        if until:
            query += " AND em.last_seen_at<=?"
            params.append(until)
        query += " ORDER BY em.last_seen_at DESC, em.mention_id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()
    output = []
    for row in rows:
        try:
            engagement = json.loads(row[4] or "{}")
        except json.JSONDecodeError:
            engagement = {}
        output.append({"mention_id": row[0], "source_id": row[1], "title": row[2],
                       "url": row[3], "engagement": engagement,
                       "first_seen_at": row[5], "last_seen_at": row[6]})
    return output


def signal_upsert(topic_id: str, event_id: int, signal_type: str,
                  severity: str, title: str, payload_json: str,
                  observed_at: str, dedupe_key: str) -> int:
    conn = _conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO signals(topic_id, event_id, signal_type, severity, title, "
            "payload_json, observed_at, dedupe_key) VALUES(?,?,?,?,?,?,?,?)",
            (topic_id, event_id, signal_type, severity, title, payload_json,
             observed_at, dedupe_key),
        )
        row = conn.execute("SELECT id FROM signals WHERE dedupe_key=?", (dedupe_key,)).fetchone()
        conn.commit()
        return int(row[0])
    finally:
        conn.close()


def signal_list(topic_id: str = "", limit: int = 100,
                since: str = "", until: str = "") -> list[dict]:
    conn = _conn()
    try:
        query = (
            "SELECT id, topic_id, event_id, signal_type, severity, title, payload_json, "
            "observed_at, acknowledged, dedupe_key FROM signals"
        )
        params: list = []
        if topic_id:
            query += " WHERE topic_id=?"
            params.append(topic_id)
        if since:
            query += " AND " if " WHERE " in query else " WHERE "
            query += "observed_at>=?"
            params.append(since)
        if until:
            query += " AND " if " WHERE " in query else " WHERE "
            query += "observed_at<=?"
            params.append(until)
        query += " ORDER BY observed_at DESC, id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()
    output = []
    for row in rows:
        try:
            payload = json.loads(row[6] or "{}")
        except json.JSONDecodeError:
            payload = {}
        output.append({"id": row[0], "topic_id": row[1], "event_id": row[2],
                       "signal_type": row[3], "severity": row[4], "title": row[5],
                       "payload": payload, "observed_at": row[7],
                       "acknowledged": bool(row[8]), "dedupe_key": row[9]})
    return output


def metrics_snapshot() -> dict:
    """返回可观测性摘要，不包含设置值、令牌或任何 API Key。"""
    conn = _conn()
    try:
        def grouped(table: str, column: str) -> dict:
            rows = conn.execute(
                f"SELECT {column}, COUNT(*) FROM {table} GROUP BY {column}"
            ).fetchall()
            return {str(row[0] or "unknown"): int(row[1]) for row in rows}
        return {
            "scan_runs": grouped("scan_runs", "status"),
            "monitor_runs": grouped("monitor_runs", "status"),
            "tasks": grouped("tasks", "status"),
            "signals": int(conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]),
            "mentions": int(conn.execute("SELECT COUNT(*) FROM mentions").fetchone()[0]),
            "events": int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]),
        }
    finally:
        conn.close()

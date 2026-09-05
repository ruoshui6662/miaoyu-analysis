"""SQLite 本地库（纯 stdlib）：settings 表 + sources 表（精细信源）。

settings：key/value 键值配置（设置页）。
sources： 信源目录（名称/域名/类别/等级/采集方式/勾选状态），C1a。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

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
CREATE INDEX IF NOT EXISTS idx_hot_items_board_key_captured
  ON hot_items(board_id, item_key, captured_at)
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
  kind TEXT NOT NULL DEFAULT 'monitor',
  source_scope_json TEXT NOT NULL DEFAULT '["L1","L2","L3"]',
  last_read_at TEXT NOT NULL DEFAULT '',
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
  matched_keywords_json TEXT NOT NULL DEFAULT '[]',
  match_location TEXT NOT NULL DEFAULT 'title',
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

RADAR_SCHEMA = """
CREATE TABLE IF NOT EXISTS source_fetch_states (
  source_id TEXT PRIMARY KEY,
  source_name TEXT NOT NULL DEFAULT '',
  source_type TEXT NOT NULL DEFAULT '',
  last_checked_at TEXT NOT NULL DEFAULT '',
  etag TEXT NOT NULL DEFAULT '',
  last_modified TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'unknown',
  item_count INTEGER NOT NULL DEFAULT 0,
  error_message TEXT NOT NULL DEFAULT ''
)
;
CREATE INDEX IF NOT EXISTS idx_source_fetch_states_status
  ON source_fetch_states(status, last_checked_at DESC)
;

CREATE TABLE IF NOT EXISTS source_endpoints (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id INTEGER NOT NULL,
  endpoint_type TEXT NOT NULL,
  url TEXT NOT NULL,
  platform TEXT NOT NULL DEFAULT '',
  account_identifier TEXT NOT NULL DEFAULT '',
  adapter_key TEXT NOT NULL DEFAULT '',
  auth_ref TEXT NOT NULL DEFAULT '',
  poll_interval_seconds INTEGER NOT NULL DEFAULT 900,
  enabled INTEGER NOT NULL DEFAULT 1,
  manual INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT '',
  UNIQUE(endpoint_type, url, account_identifier),
  FOREIGN KEY(source_id) REFERENCES sources(id)
)
;
CREATE INDEX IF NOT EXISTS idx_source_endpoints_enabled
  ON source_endpoints(enabled, endpoint_type, updated_at DESC)
;
CREATE TABLE IF NOT EXISTS radar_topic_endpoints (
  topic_id TEXT NOT NULL,
  endpoint_id INTEGER NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(topic_id, endpoint_id),
  FOREIGN KEY(topic_id) REFERENCES topics(id),
  FOREIGN KEY(endpoint_id) REFERENCES source_endpoints(id)
)
;
CREATE INDEX IF NOT EXISTS idx_radar_topic_endpoints_endpoint
  ON radar_topic_endpoints(endpoint_id, enabled)
;
CREATE TABLE IF NOT EXISTS radar_sync_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  endpoint_id INTEGER NOT NULL,
  started_at TEXT NOT NULL DEFAULT '',
  finished_at TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'running',
  item_count INTEGER NOT NULL DEFAULT 0,
  new_count INTEGER NOT NULL DEFAULT 0,
  http_status INTEGER DEFAULT 0,
  error_code TEXT DEFAULT '',
  error_message TEXT DEFAULT '',
  FOREIGN KEY(endpoint_id) REFERENCES source_endpoints(id)
)
;
CREATE INDEX IF NOT EXISTS idx_radar_sync_runs_endpoint_started
  ON radar_sync_runs(endpoint_id, started_at DESC)
"""


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    """为已经存在的本地 SQLite 库补齐增量字段，升级无需删库。"""
    columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(SETTINGS_DB), timeout=15)
    conn.execute("PRAGMA busy_timeout = 15000")
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(SOURCES_SCHEMA)
    conn.execute(TASKS_SCHEMA)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(EVIDENCE_SCHEMA)
    conn.executescript(RADAR_SCHEMA)
    _ensure_column(conn, "topics", "kind", "TEXT NOT NULL DEFAULT 'monitor'")
    _ensure_column(conn, "topics", "source_scope_json", "TEXT NOT NULL DEFAULT '[\"L1\",\"L2\",\"L3\"]'")
    _ensure_column(conn, "topics", "last_read_at", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "mention_topics", "matched_keywords_json", "TEXT NOT NULL DEFAULT '[]'")
    _ensure_column(conn, "mention_topics", "match_location", "TEXT NOT NULL DEFAULT 'title'")
    _ensure_column(conn, "source_fetch_states", "etag", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "source_fetch_states", "last_modified", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "source_fetch_states", "cursor_value", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "source_fetch_states", "last_success_at", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "source_fetch_states", "next_fetch_at", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "source_fetch_states", "consecutive_failures", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "source_fetch_states", "cooldown_until", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "source_fetch_states", "average_update_seconds", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "source_fetch_states", "last_http_status", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "source_fetch_states", "lease_owner", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "source_fetch_states", "lease_until", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "source_fetch_states", "updated_at", "TEXT NOT NULL DEFAULT ''")
    conn.commit()
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
            "SELECT item_key, title, canonical_url, rank, hot_value, provider, captured_at "
            "FROM hot_items WHERE scan_run_id=? ORDER BY COALESCE(rank, 999999), id",
            (current_id,),
        ).fetchall()
        first_seen_rows = conn.execute(
            "SELECT h.item_key, MIN(h.captured_at) "
            "FROM hot_items h JOIN scan_runs r ON r.id=h.scan_run_id "
            "WHERE h.board_id=? AND r.status='success' "
            "GROUP BY h.item_key",
            (board_id,),
        ).fetchall()
        first_seen = {str(row[0]): row[1] for row in first_seen_rows}
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
                "provider": row[5], "captured_at": row[6],
                "first_seen_at": first_seen.get(key) or row[6],
                "previous_rank": old_rank,
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
                 enabled: bool = True, kind: str = "monitor",
                 source_scope: list[str] | None = None) -> None:
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO topics(id, name, keywords_json, exclude_keywords_json, kind, source_scope_json, "
            "last_read_at, enabled, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (topic_id, name, json.dumps(keywords, ensure_ascii=False),
             json.dumps(exclude_keywords, ensure_ascii=False),
             kind if kind in {"monitor", "radar"} else "monitor",
             json.dumps(source_scope or ["L1", "L2", "L3"], ensure_ascii=False),
             "", 1 if enabled else 0, now, now),
        )
        conn.commit()
    finally:
        conn.close()


def topic_get(topic_id: str) -> dict | None:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT id, name, keywords_json, exclude_keywords_json, kind, source_scope_json, last_read_at, "
            "enabled, created_at, updated_at "
            "FROM topics WHERE id=?", (topic_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {
        "id": row[0], "name": row[1],
        "keywords": json.loads(row[2] or "[]"),
        "exclude_keywords": json.loads(row[3] or "[]"), "kind": row[4] or "monitor",
        "source_scope": json.loads(row[5] or "[]"), "last_read_at": row[6] or "",
        "enabled": bool(row[7]), "created_at": row[8], "updated_at": row[9],
    }


def topic_list() -> list[dict]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, name, keywords_json, exclude_keywords_json, kind, source_scope_json, last_read_at, "
            "enabled, created_at, updated_at "
            "FROM topics ORDER BY created_at DESC, id DESC"
        ).fetchall()
    finally:
        conn.close()
    return [{
        "id": row[0], "name": row[1],
        "keywords": json.loads(row[2] or "[]"),
        "exclude_keywords": json.loads(row[3] or "[]"), "kind": row[4] or "monitor",
        "source_scope": json.loads(row[5] or "[]"), "last_read_at": row[6] or "",
        "enabled": bool(row[7]), "created_at": row[8], "updated_at": row[9],
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


def mention_topic_touch(mention_id: int, topic_id: str, seen_at: str,
                        matched_keywords: list[str] | None = None,
                        match_location: str = "title") -> None:
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO mention_topics(mention_id, topic_id, first_seen_at, last_seen_at, "
            "matched_keywords_json, match_location) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(mention_id, topic_id) DO UPDATE SET last_seen_at=excluded.last_seen_at, "
            "matched_keywords_json=excluded.matched_keywords_json, match_location=excluded.match_location",
            (mention_id, topic_id, seen_at, seen_at,
             json.dumps(matched_keywords or [], ensure_ascii=False), match_location or "title"),
        )
        conn.commit()
    finally:
        conn.close()


def radar_topics() -> list[dict]:
    """只返回雷达主题，避免与旧版分析监测主题混淆。"""
    return [topic for topic in topic_list() if topic.get("kind") == "radar"]


def radar_subscriptions_due(now: str) -> list[dict]:
    """读取到期的雷达订阅；雷达调度与分析监测调度相互隔离。"""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT s.id, s.topic_id, s.interval_seconds, s.enabled, s.last_run_at, s.next_run_at, "
            "s.consecutive_failures, s.cooldown_until, s.created_at, s.updated_at "
            "FROM subscriptions s JOIN topics t ON t.id=s.topic_id "
            "WHERE t.kind='radar' AND t.enabled=1 AND s.enabled=1 AND "
            "(s.next_run_at='' OR s.next_run_at<=?) AND (s.cooldown_until='' OR s.cooldown_until<=?) "
            "ORDER BY s.id", (now, now),
        ).fetchall()
    finally:
        conn.close()
    return [{"id": r[0], "topic_id": r[1], "interval_seconds": r[2], "enabled": bool(r[3]),
             "last_run_at": r[4] or "", "next_run_at": r[5] or "",
             "consecutive_failures": r[6], "cooldown_until": r[7] or "",
             "created_at": r[8], "updated_at": r[9]} for r in rows]


def radar_timeline(topic_id: str, *, limit: int = 100, before: str = "",
                   source_id: str = "") -> list[dict]:
    """按发布时间优先、采集时间兜底读取雷达时间线。before 使用 ISO 时间游标。"""
    conn = _conn()
    try:
        query = (
            "SELECT m.id, m.title, m.snippet, m.canonical_url, m.source_id, m.source_type, "
            "m.published_at, m.captured_at, mt.first_seen_at, mt.last_seen_at, "
            "mt.matched_keywords_json, mt.match_location "
            "FROM mention_topics mt JOIN mentions m ON m.id=mt.mention_id "
            "WHERE mt.topic_id=?"
        )
        params: list = [topic_id]
        if source_id:
            query += " AND m.source_id=?"
            params.append(source_id)
        if before:
            query += " AND COALESCE(NULLIF(m.published_at,''), mt.first_seen_at) < ?"
            params.append(before)
        query += " ORDER BY COALESCE(NULLIF(m.published_at,''), mt.first_seen_at) DESC, m.id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()
    out = []
    for row in rows:
        try:
            matched = json.loads(row[10] or "[]")
        except json.JSONDecodeError:
            matched = []
        out.append({"id": row[0], "title": row[1], "snippet": row[2] or "",
                    "url": row[3] or "", "source_id": row[4], "source_type": row[5],
                    "published_at": row[6] or "", "captured_at": row[7] or "",
                    "first_seen_at": row[8] or "", "last_seen_at": row[9] or "",
                    "matched_keywords": matched, "match_location": row[11] or "title"})
    return out


def radar_stats(topic_id: str) -> dict:
    conn = _conn()
    try:
        topic = conn.execute("SELECT last_read_at FROM topics WHERE id=?", (topic_id,)).fetchone()
        total = conn.execute("SELECT COUNT(*) FROM mention_topics WHERE topic_id=?", (topic_id,)).fetchone()[0]
        unread = conn.execute(
            "SELECT COUNT(*) FROM mention_topics WHERE topic_id=? AND (?='' OR first_seen_at>?)",
            (topic_id, topic[0] if topic else "", topic[0] if topic else ""),
        ).fetchone()[0]
        latest = conn.execute(
            "SELECT MAX(COALESCE(NULLIF(m.published_at,''), mt.first_seen_at)) "
            "FROM mention_topics mt JOIN mentions m ON m.id=mt.mention_id WHERE mt.topic_id=?",
            (topic_id,),
        ).fetchone()[0] or ""
        sources = conn.execute(
            "SELECT COUNT(DISTINCT m.source_id) FROM mention_topics mt JOIN mentions m ON m.id=mt.mention_id "
            "WHERE mt.topic_id=?", (topic_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    return {"total": int(total), "unread": int(unread), "source_count": int(sources),
            "latest_at": latest, "last_read_at": topic[0] if topic else ""}


def radar_mark_read(topic_id: str, read_at: str) -> bool:
    conn = _conn()
    try:
        cur = conn.execute("UPDATE topics SET last_read_at=?, updated_at=? WHERE id=?",
                           (read_at, read_at, topic_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def radar_delete_topic(topic_id: str) -> bool:
    conn = _conn()
    try:
        exists = conn.execute("SELECT 1 FROM topics WHERE id=? AND kind='radar'", (topic_id,)).fetchone()
        if not exists:
            return False
        # 旧版监测可能已经为同一主题写入事件/信号；先清理所有从属关系，
        # 这样升级后的雷达主题仍可安全删除，不会被历史外键卡住。
        conn.execute("DELETE FROM signals WHERE topic_id=?", (topic_id,))
        conn.execute("DELETE FROM event_mentions WHERE topic_id=?", (topic_id,))
        conn.execute("DELETE FROM events WHERE topic_id=?", (topic_id,))
        conn.execute("DELETE FROM monitor_runs WHERE topic_id=?", (topic_id,))
        conn.execute("DELETE FROM mention_topics WHERE topic_id=?", (topic_id,))
        conn.execute("DELETE FROM subscriptions WHERE topic_id=?", (topic_id,))
        cur = conn.execute("DELETE FROM topics WHERE id=? AND kind='radar'", (topic_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def radar_source_state(source_id: str, *, name: str, source_type: str,
                       checked_at: str, status: str, item_count: int = 0,
                       error_message: str = "") -> None:
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO source_fetch_states(source_id, source_name, source_type, last_checked_at, status, "
            "item_count, error_message) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(source_id) DO UPDATE SET source_name=excluded.source_name, "
            "source_type=excluded.source_type, last_checked_at=excluded.last_checked_at, "
            "status=excluded.status, item_count=excluded.item_count, error_message=excluded.error_message",
            (source_id, name, source_type, checked_at, status, int(item_count), error_message[:500]),
        )
        conn.commit()
    finally:
        conn.close()


def radar_source_states() -> list[dict]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT source_id, source_name, source_type, last_checked_at, status, item_count, error_message "
            "FROM source_fetch_states ORDER BY source_name, source_id"
        ).fetchall()
    finally:
        conn.close()
    return [{"source_id": r[0], "source_name": r[1], "source_type": r[2],
             "last_checked_at": r[3], "status": r[4], "item_count": r[5],
             "error_message": r[6]} for r in rows]


# ---------- 雷达信源端点（RADAR-SRC-1） ----------

_RADAR_ENDPOINT_TYPES = {"rss", "atom", "website", "rsshub", "rssbridge", "account", "api", "hotlist"}


def radar_source_identity_get_or_create(name: str, host: str = "",
                                        category: str = "media", level: str = "C") -> int:
    """创建或复用来源身份；同步入口不直接把身份信息写入 endpoint。"""
    clean_name = str(name or "").strip()
    clean_host = str(host or "").strip().lower()
    if not clean_name:
        raise ValueError("来源名称不能为空")
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT id FROM sources WHERE name=? AND host=? ORDER BY id LIMIT 1",
            (clean_name, clean_host),
        ).fetchone()
        if row:
            return int(row[0])
        max_sort = conn.execute("SELECT COALESCE(MAX(sort),0) FROM sources").fetchone()[0]
        cur = conn.execute(
            "INSERT INTO sources(name, host, category, level, stype, extra, enabled, manual, sort) "
            "VALUES(?,?,?,?,?,?,1,1,?)",
            (clean_name, clean_host, category or "media", level or "C", "site", "", max_sort + 1),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def radar_endpoint_create(source_id: int, endpoint_type: str, url: str, *,
                          platform: str = "", account_identifier: str = "",
                          adapter_key: str = "", auth_ref: str = "",
                          poll_interval_seconds: int = 900, enabled: bool = True,
                          manual: bool = True, now: str = "") -> int:
    etype = str(endpoint_type or "").strip().lower()
    clean_url = str(url or "").strip()
    if etype not in _RADAR_ENDPOINT_TYPES:
        raise ValueError("不支持的雷达端点类型")
    if not clean_url:
        raise ValueError("端点地址不能为空")
    now = now or datetime.now(timezone.utc).isoformat()
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT INTO source_endpoints(source_id, endpoint_type, url, platform, account_identifier, "
            "adapter_key, auth_ref, poll_interval_seconds, enabled, manual, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (int(source_id), etype, clean_url, str(platform or "").strip(),
             str(account_identifier or "").strip(), str(adapter_key or "").strip(),
             str(auth_ref or "").strip(), max(300, min(86400, int(poll_interval_seconds))),
             1 if enabled else 0, 1 if manual else 0, now, now),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def radar_endpoint_get(endpoint_id: int) -> dict | None:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT e.id, e.source_id, s.name, s.host, s.category, s.level, e.endpoint_type, e.url, "
            "e.platform, e.account_identifier, e.adapter_key, e.auth_ref, e.poll_interval_seconds, "
            "e.enabled, e.manual, e.created_at, e.updated_at "
            "FROM source_endpoints e JOIN sources s ON s.id=e.source_id WHERE e.id=?", (int(endpoint_id),)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return _radar_endpoint_dict(row)


def _radar_endpoint_dict(row) -> dict:
    return {
        "id": row[0], "source_id": row[1], "source_name": row[2], "host": row[3],
        "category": row[4], "level": row[5], "endpoint_type": row[6], "url": row[7],
        "platform": row[8], "account_identifier": row[9], "adapter_key": row[10],
        "auth_ref": row[11], "poll_interval_seconds": row[12], "enabled": bool(row[13]),
        "manual": bool(row[14]), "created_at": row[15], "updated_at": row[16],
    }


def radar_endpoints(*, enabled_only: bool = False) -> list[dict]:
    conn = _conn()
    try:
        query = (
            "SELECT e.id, e.source_id, s.name, s.host, s.category, s.level, e.endpoint_type, e.url, "
            "e.platform, e.account_identifier, e.adapter_key, e.auth_ref, e.poll_interval_seconds, "
            "e.enabled, e.manual, e.created_at, e.updated_at "
            "FROM source_endpoints e JOIN sources s ON s.id=e.source_id "
        )
        if enabled_only:
            query += "WHERE e.enabled=1 AND s.enabled=1 "
        query += "ORDER BY e.id"
        rows = conn.execute(query).fetchall()
    finally:
        conn.close()
    return [_radar_endpoint_dict(row) for row in rows]


def radar_topic_endpoint_bind(topic_id: str, endpoint_id: int, *, enabled: bool = True,
                              now: str = "") -> bool:
    now = now or datetime.now(timezone.utc).isoformat()
    conn = _conn()
    try:
        valid = conn.execute(
            "SELECT 1 FROM topics WHERE id=? AND kind='radar'", (topic_id,)
        ).fetchone() and conn.execute(
            "SELECT 1 FROM source_endpoints WHERE id=?", (int(endpoint_id),)
        ).fetchone()
        if not valid:
            return False
        conn.execute(
            "INSERT INTO radar_topic_endpoints(topic_id, endpoint_id, enabled, created_at) VALUES(?,?,?,?) "
            "ON CONFLICT(topic_id, endpoint_id) DO UPDATE SET enabled=excluded.enabled",
            (topic_id, int(endpoint_id), 1 if enabled else 0, now),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def radar_topic_endpoint_ids(topic_ids: list[str] | tuple[str, ...]) -> set[int]:
    ids = [str(x).strip() for x in topic_ids if str(x).strip()]
    if not ids:
        return set()
    conn = _conn()
    try:
        marks = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"SELECT endpoint_id FROM radar_topic_endpoints WHERE enabled=1 AND topic_id IN ({marks})",
            ids,
        ).fetchall()
    finally:
        conn.close()
    return {int(row[0]) for row in rows}


def radar_topic_endpoints(topic_id: str) -> list[dict]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT e.id, e.source_id, s.name, s.host, s.category, s.level, e.endpoint_type, e.url, "
            "e.platform, e.account_identifier, e.adapter_key, e.auth_ref, e.poll_interval_seconds, "
            "e.enabled, e.manual, e.created_at, e.updated_at "
            "FROM radar_topic_endpoints te JOIN source_endpoints e ON e.id=te.endpoint_id "
            "JOIN sources s ON s.id=e.source_id WHERE te.topic_id=? AND te.enabled=1 ORDER BY e.id",
            (topic_id,),
        ).fetchall()
    finally:
        conn.close()
    return [_radar_endpoint_dict(row) for row in rows]


def radar_endpoint_state(endpoint_id: int) -> dict:
    key = f"endpoint:{int(endpoint_id)}"
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT source_id, etag, last_modified, cursor_value, last_checked_at, last_success_at, "
            "next_fetch_at, consecutive_failures, cooldown_until, average_update_seconds, "
            "last_http_status, status, item_count, error_message, lease_owner, lease_until, updated_at "
            "FROM source_fetch_states WHERE source_id=?", (key,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return {"endpoint_id": int(endpoint_id), "etag": "", "last_modified": "",
                "cursor_value": "", "last_checked_at": "", "last_success_at": "",
                "next_fetch_at": "", "consecutive_failures": 0, "cooldown_until": "",
                "average_update_seconds": 0, "last_http_status": 0, "status": "unknown",
                "item_count": 0, "error_message": "", "lease_owner": "", "lease_until": "",
                "updated_at": ""}
    return {"endpoint_id": int(endpoint_id), "etag": row[1] or "", "last_modified": row[2] or "",
            "cursor_value": row[3] or "", "last_checked_at": row[4] or "", "last_success_at": row[5] or "",
            "next_fetch_at": row[6] or "", "consecutive_failures": int(row[7] or 0),
            "cooldown_until": row[8] or "", "average_update_seconds": int(row[9] or 0),
            "last_http_status": int(row[10] or 0), "status": row[11] or "unknown",
            "item_count": int(row[12] or 0), "error_message": row[13] or "",
            "lease_owner": row[14] or "", "lease_until": row[15] or "", "updated_at": row[16] or ""}


def radar_endpoint_state_upsert(endpoint_id: int, *, status: str, checked_at: str,
                                etag: str = "", last_modified: str = "", cursor_value: str = "",
                                last_success_at: str = "", next_fetch_at: str = "",
                                consecutive_failures: int = 0, cooldown_until: str = "",
                                average_update_seconds: int = 0, last_http_status: int = 0,
                                item_count: int = 0, error_message: str = "") -> None:
    key = f"endpoint:{int(endpoint_id)}"
    conn = _conn()
    try:
        endpoint_meta = conn.execute(
            "SELECT s.name, e.endpoint_type FROM source_endpoints e "
            "JOIN sources s ON s.id=e.source_id WHERE e.id=?", (int(endpoint_id),)
        ).fetchone()
        display_name = f"{endpoint_meta[0]} · {endpoint_meta[1]}" if endpoint_meta else key
        display_type = endpoint_meta[1] if endpoint_meta else "rss"
        conn.execute(
            "INSERT INTO source_fetch_states(source_id, source_name, source_type, last_checked_at, "
            "status, item_count, error_message, etag, last_modified, cursor_value, last_success_at, "
            "next_fetch_at, consecutive_failures, cooldown_until, average_update_seconds, last_http_status, "
            "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(source_id) DO UPDATE SET last_checked_at=excluded.last_checked_at, "
            "status=excluded.status, item_count=excluded.item_count, error_message=excluded.error_message, "
            "etag=excluded.etag, last_modified=excluded.last_modified, cursor_value=excluded.cursor_value, "
            "last_success_at=excluded.last_success_at, next_fetch_at=excluded.next_fetch_at, "
            "consecutive_failures=excluded.consecutive_failures, cooldown_until=excluded.cooldown_until, "
            "average_update_seconds=excluded.average_update_seconds, last_http_status=excluded.last_http_status, "
            "updated_at=excluded.updated_at",
            (key, display_name, display_type, checked_at, status, int(item_count),
             error_message[:500], etag[:500], last_modified[:500], cursor_value[:500], last_success_at,
             next_fetch_at, int(consecutive_failures), cooldown_until, int(average_update_seconds),
             int(last_http_status), checked_at),
        )
        conn.commit()
    finally:
        conn.close()


def radar_sync_run_create(endpoint_id: int, started_at: str) -> int:
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT INTO radar_sync_runs(endpoint_id, started_at, status) VALUES(?,?, 'running')",
            (int(endpoint_id), started_at),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def radar_sync_run_finish(run_id: int, *, status: str, finished_at: str,
                          item_count: int = 0, new_count: int = 0, http_status: int = 0,
                          error_code: str = "", error_message: str = "") -> None:
    conn = _conn()
    try:
        conn.execute(
            "UPDATE radar_sync_runs SET finished_at=?, status=?, item_count=?, new_count=?, "
            "http_status=?, error_code=?, error_message=? WHERE id=?",
            (finished_at, status, int(item_count), int(new_count), int(http_status),
             error_code[:80], error_message[:500], int(run_id)),
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

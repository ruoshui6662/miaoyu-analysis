"""统一舆情证据模型（G1）。

数据源只负责返回原始/半标准化条目；本模块负责把条目变成可追溯的
Mention，并把热榜排名作为一次快照写入 hot_items。这里不做定时调度、
跨平台事件聚类或 AI 评分，这些属于后续阶段。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import db


_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "spm", "from", "share_source", "share_medium", "share_tag",
}


def normalize_text(value: Any) -> str:
    """压缩空白，保留中文标点，作为指纹生成的稳定输入。"""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def canonicalize_url(value: Any) -> str:
    """清理常见追踪参数；无法解析的内容按原文本保留，避免丢失证据。"""
    raw = normalize_text(value)
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
        if not parts.scheme or not parts.netloc:
            return raw
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                 if k.lower() not in _TRACKING_PARAMS and not k.lower().startswith("utm_")]
        path = parts.path.rstrip("/") or "/"
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path,
                           urlencode(query), ""))
    except ValueError:
        return raw


def content_hash(title: Any, url: Any) -> str:
    """生成来源内稳定指纹；同标题不同链接仍保留为不同证据。"""
    seed = normalize_text(title) + "\n" + canonicalize_url(url)
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_mention(raw: dict, *, source_id: str, source_type: str,
                      captured_at: str | None = None,
                      topic_id: str = "") -> dict:
    """把 NewsNow/聚合源条目转换为统一 Mention 字段。"""
    title = normalize_text(raw.get("title"))
    url = canonicalize_url(raw.get("url") or raw.get("mobileUrl"))
    hot_value = raw.get("hot")
    engagement = {"hot": hot_value} if hot_value not in (None, "") else {}
    sentiment = normalize_text(raw.get("sentiment") or raw.get("sentiment_label") or raw.get("stance"))
    if sentiment in {"positive", "negative", "neutral"}:
        engagement["sentiment"] = sentiment
    return {
        "content_hash": content_hash(title, url),
        "canonical_url": url,
        "source_id": source_id,
        "source_type": source_type,
        "title": title,
        "snippet": normalize_text(raw.get("snippet")),
        "body": normalize_text(raw.get("body")),
        "published_at": normalize_text(raw.get("published") or raw.get("published_at")),
        "captured_at": captured_at or normalize_text(raw.get("captured_at")) or now_iso(),
        "topic_id": topic_id,
        "credibility": normalize_text(raw.get("credibility") or raw.get("level")),
        "fetch_status": normalize_text(raw.get("fetch_status")) or "captured",
        "raw_path": normalize_text(raw.get("raw_path")),
        "engagement_json": json.dumps(engagement, ensure_ascii=False),
    }


class SourceAdapter:
    """G1 数据源适配器约定，具体平台在后续按需实现。"""

    def capabilities(self) -> dict:
        return {}

    def fetch(self, query: str | None = None, cursor: str | None = None) -> list[dict]:
        raise NotImplementedError

    def normalize(self, raw: dict) -> dict:
        return raw

    def healthcheck(self) -> dict:
        return {"ok": True}


def record_hot_boards(boards: list[dict], *, captured_at: str | None = None) -> dict:
    """把一次聚合热榜写入 scan_runs、mentions、hot_items。

    每个榜单独立记录一次 scan_run；单榜写入失败会抛出，由调用层决定是否
    降级，但不会影响已经成功写入的其他榜单。
    """
    captured = captured_at or now_iso()
    result = {"runs": 0, "mentions_new": 0, "items": 0}
    for board in boards or []:
        board_id = normalize_text(board.get("source_id") or board.get("name"))
        if not board_id:
            continue
        rows = []
        for rank, raw in enumerate((board.get("items") or [])[:30], start=1):
            if not isinstance(raw, dict):
                continue
            mention = normalize_mention(
                raw, source_id=board_id, source_type="hotlist", captured_at=captured,
            )
            if mention["title"]:
                rows.append({
                    "mention": mention,
                    "rank": raw.get("rank") or rank,
                    "hot_value": raw.get("hot") or "",
                    "provider": raw.get("provider") or board.get("provider") or "",
                })
        snapshot = db.record_hot_snapshot(board_id, rows, captured)
        result["runs"] += 1
        result["mentions_new"] += snapshot["mentions_new"]
        result["items"] += snapshot["items"]
    return result

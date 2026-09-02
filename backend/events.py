"""可解释的事件聚合（G2.5）。

聚合是确定性的：先做标题规范化，再在同一主题的时间窗口内匹配。
每次匹配保留 grouping_method，便于人工复核，不把 AI 判断混入底层事实层。
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import db


def normalize_event_title(value: Any) -> str:
    """去空白和标点，保留中文、英文和数字的顺序作为稳定事件键。"""
    text = str(value or "").lower()
    return "".join(re.findall(r"[\u4e00-\u9fff]|[a-z0-9]+", text))


def _bigrams(value: str) -> set[str]:
    return {value[i:i + 2] for i in range(len(value) - 1)} if len(value) > 1 else {value}


def title_similarity(left: str, right: str) -> float:
    if left == right:
        return 1.0
    if not left or not right:
        return 0.0
    if left in right or right in left:
        return min(len(left), len(right)) / max(len(left), len(right))
    a, b = _bigrams(left), _bigrams(right)
    return len(a & b) / len(a | b) if a and b else 0.0


def _parse_hot(value: Any) -> float:
    text = str(value or "").strip().lower().replace(",", "")
    if not text:
        return 0.0
    multiplier = 1.0
    if text.endswith(("万", "w")):
        multiplier = 10000.0
        text = text[:-1]
    elif text.endswith("k"):
        multiplier = 1000.0
        text = text[:-1]
    try:
        return max(0.0, float(text) * multiplier)
    except ValueError:
        return 0.0


def heat_score(*, mention_count: int, platform_count: int,
               hot_total: float, last_seen_at: str, now_at: str) -> float:
    """热度公式：覆盖面 + 平台数 + 可选热度值 + 新鲜度，所有项可解释。"""
    try:
        last = datetime.fromisoformat(last_seen_at.replace("Z", "+00:00"))
        now = datetime.fromisoformat(now_at.replace("Z", "+00:00"))
        age_hours = max(0.0, (now - last).total_seconds() / 3600)
    except (AttributeError, TypeError, ValueError):
        age_hours = 168.0
    freshness = max(0.0, 20.0 * (1.0 - age_hours / 168.0))
    return round(
        mention_count * 10.0 + platform_count * 20.0
        + min(50.0, math.log1p(hot_total) * 5.0) + freshness, 2
    )


def assign_mention(topic_id: str, mention_id: int, mention: dict,
                   seen_at: str, *, window_days: int = 7) -> dict:
    """将 Mention 归入事件并刷新事件指标，返回匹配依据和热度。"""
    title = str(mention.get("title") or "").strip()
    key = normalize_event_title(title)
    if not key:
        return {"event_id": None, "method": "ignored", "heat_score": 0.0}
    cutoff = (datetime.fromisoformat(seen_at.replace("Z", "+00:00"))
              - timedelta(days=window_days)).isoformat()
    candidates = db.event_candidates(topic_id, cutoff)
    event_id = None
    method = "exact_key"
    for candidate in candidates:
        if candidate["normalized_key"] == key:
            event_id = candidate["id"]
            break
    if event_id is None and len(key) >= 8:
        best = max(candidates, key=lambda item: title_similarity(key, item["normalized_key"]), default=None)
        if best and title_similarity(key, best["normalized_key"]) >= 0.62:
            event_id = best["id"]
            method = "title_overlap"
    if event_id is None:
        event_id = db.event_create(topic_id, key, title, method, seen_at)
    db.event_attach(event_id, mention_id, topic_id, method, seen_at)
    stats = db.event_stats(event_id)
    hot_total = 0.0
    # engagement_json 是 Mention 的原始可解释字段；累计所有关联 Mention，
    # 无法解析的值按 0 处理，避免一次刷新覆盖事件历史热度。
    for raw_engagement in stats.get("engagement_jsons", []):
        try:
            hot_total += _parse_hot(json.loads(raw_engagement or "{}").get("hot"))
        except (TypeError, ValueError, AttributeError):
            continue
    stats_for_update = {key: value for key, value in stats.items()
                        if key != "engagement_jsons"}
    score = heat_score(
        mention_count=stats["mention_count"], platform_count=stats["platform_count"],
        hot_total=hot_total, last_seen_at=stats["last_seen_at"], now_at=seen_at,
    )
    db.event_update_metrics(event_id, heat_score=score, updated_at=seen_at,
                            **stats_for_update)
    return {"event_id": event_id, "method": method, "heat_score": score,
            "mention_count": stats["mention_count"], "platform_count": stats["platform_count"]}

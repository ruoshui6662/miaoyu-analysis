"""基于证据的确定性告警（G2.6）。"""
from __future__ import annotations

import json

import db


def _sentiment_counts(engagement_jsons: list[str]) -> tuple[int, int]:
    labeled = negative = 0
    for raw in engagement_jsons:
        try:
            value = json.loads(raw or "{}").get("sentiment", "")
        except (TypeError, ValueError, AttributeError):
            value = ""
        if value in {"positive", "negative", "neutral"}:
            labeled += 1
            negative += int(value == "negative")
    return labeled, negative


def evaluate_topic(topic_id: str, observed_at: str) -> list[dict]:
    """根据事件覆盖面、提及数和已标注情感生成去重信号。"""
    created = []
    for event in db.event_list(topic_id, limit=500):
        stats = db.event_stats(event["id"])
        metrics = {
            "mention_count": stats["mention_count"],
            "platform_count": stats["platform_count"],
            "heat_score": event["heat_score"],
        }
        rules: list[tuple[str, str, str, dict]] = []
        if stats["platform_count"] >= 2:
            rules.append(("cross_platform", "medium", "事件已跨平台传播", metrics))
        if stats["mention_count"] >= 3:
            severity = "high" if stats["mention_count"] >= 5 else "medium"
            rules.append(("surge", severity, "事件出现连续新增提及", metrics))
        labeled, negative = _sentiment_counts(stats.get("engagement_jsons", []))
        if labeled >= 3 and negative / labeled >= 0.6:
            payload = {**metrics, "labeled_count": labeled,
                       "negative_count": negative,
                       "negative_ratio": round(negative / labeled, 3)}
            rules.append(("negative_ratio", "high", "事件负面情感占比偏高", payload))
        for signal_type, severity, title, payload in rules:
            dedupe_key = f"{topic_id}:{event['id']}:{signal_type}"
            signal_id = db.signal_upsert(
                topic_id, event["id"], signal_type, severity, title,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                observed_at, dedupe_key,
            )
            created.append({"id": signal_id, "event_id": event["id"],
                            "signal_type": signal_type, "severity": severity,
                            "title": title, "payload": payload})
    return created

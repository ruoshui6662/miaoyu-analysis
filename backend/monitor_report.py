"""监测周期报告编排（G2.6）。

报告只从 SQLite 中已经落库的监测运行、事件、信号和 Mention 取数，
不在报告阶段重新抓取，也不调用 AI，保证报告可以按窗口回放。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import db
from config import DATA_DIR_REPORTS
from pipeline import gen_docx, gen_markdown, safe_filename_component


def _as_datetime(value: str) -> datetime:
    text = str(value or "").strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _window(start: str | None, end: str | None, hours: int) -> tuple[str, str]:
    if start and end:
        left, right = _as_datetime(start), _as_datetime(end)
    elif start:
        left, right = _as_datetime(start), _as_datetime(start) + timedelta(hours=hours)
    elif end:
        right = _as_datetime(end)
        left = right - timedelta(hours=hours)
    else:
        right = datetime.now(timezone.utc)
        left = right - timedelta(hours=hours)
    if left >= right:
        raise ValueError("报告窗口的开始时间必须早于结束时间")
    if right - left > timedelta(days=31):
        raise ValueError("报告窗口不能超过 31 天")
    return left.isoformat(), right.isoformat()


def _event_payload(event: dict, mentions: list[dict]) -> dict:
    return {**event, "mentions": mentions}


def _summary_paragraphs(summary: dict) -> list[dict]:
    return [
        {"lead": "监测运行：", "body":
         f"共 {summary['run_count']} 次，其中成功 {summary['success_runs']} 次、失败 {summary['error_runs']} 次。"},
        {"lead": "证据规模：", "body":
         f"窗口内关联 Mention {summary['mention_count']} 条，归并为 {summary['event_count']} 个事件，覆盖 {summary['platform_count']} 个平台。"},
        {"lead": "风险信号：", "body": f"产生 {summary['signal_count']} 条去重信号。"},
    ]


def generate_periodic_report(topic_id: str, *, start: str | None = None,
                              end: str | None = None, hours: int = 24,
                              output_dir: Path | None = None) -> dict:
    topic = db.topic_get(topic_id)
    if not topic:
        raise ValueError("主题不存在")
    try:
        hours = max(1, min(int(hours), 24 * 31))
    except (TypeError, ValueError):
        raise ValueError("hours 必须是整数") from None
    window_start, window_end = _window(start, end, hours)
    runs = db.monitor_runs_window(topic_id, window_start, window_end)
    events = []
    mention_count = 0
    platforms: set[str] = set()
    for event in db.event_list_window(topic_id, window_start, window_end, limit=100):
        mentions = db.event_mentions(event["id"], window_start, window_end, limit=100)
        mention_count += len(mentions)
        platforms.update(str(item["source_id"]) for item in mentions if item.get("source_id"))
        events.append(_event_payload(event, mentions))
    signals = db.signal_list(topic_id, limit=500, since=window_start, until=window_end)
    success_runs = sum(run["status"] == "success" for run in runs)
    error_runs = sum(run["status"] == "error" for run in runs)
    summary = {
        "run_count": len(runs), "success_runs": success_runs, "error_runs": error_runs,
        "mention_count": mention_count, "event_count": len(events),
        "platform_count": len(platforms), "signal_count": len(signals),
    }

    event_paragraphs: list[dict] = []
    for index, event in enumerate(events, start=1):
        event_paragraphs.append({
            "lead": f"（{index}）{event['canonical_title']}。",
            "body": (f"热度 {event['heat_score']}；窗口证据 {len(event['mentions'])} 条；"
                     f"涉及 {event['platform_count']} 个平台；归并依据 {event['grouping_method']}。"),
        })
        for mention in event["mentions"][:5]:
            engagement = mention.get("engagement") or {}
            extra = f"；情感 {engagement['sentiment']}" if engagement.get("sentiment") else ""
            event_paragraphs.append({
                "lead": f"证据（{mention['source_id']}）：",
                "body": f"{mention['title']}（{mention['url'] or '无链接'}）{extra}",
            })
    if not event_paragraphs:
        event_paragraphs = [{"lead": "暂无事件：", "body": "该窗口内没有已完成并落库的事件证据。"}]

    signal_paragraphs = [
        {"lead": f"{item['severity']} · {item['title']}：",
         "body": f"事件 #{item['event_id']}；指标 {json.dumps(item['payload'], ensure_ascii=False, sort_keys=True)}。"}
        for item in signals
    ] or [{"lead": "暂无信号：", "body": "该窗口内没有生成去重信号。"}]
    run_paragraphs = [
        {"lead": f"#{run['id']} {run['status']}：",
         "body": f"{run['started_at']}，采集 {run['item_count']} 条，新建 {run['new_count']} 条。"
                 + (f" 错误：{run['error_message']}" if run['error_message'] else "")}
        for run in runs
    ] or [{"lead": "暂无运行记录：", "body": "该窗口内没有监测运行记录。"}]

    report: dict = {
        "title": f"“{topic['name']}”监测周期报告",
        "intro": (f"监测窗口：{window_start} 至 {window_end}。"
                   "本报告仅汇总已落库证据，不代表对未采集信息作出判断。"),
        "sections": [
            {"heading": "一、窗口摘要", "paragraphs": _summary_paragraphs(summary)},
            {"heading": "二、事件与证据", "paragraphs": event_paragraphs},
            {"heading": "三、风险信号", "paragraphs": signal_paragraphs},
            {"heading": "四、监测运行记录", "paragraphs": run_paragraphs},
        ],
        "stats": {
            "total_raw": summary["mention_count"],
            "total_after_dedupe": summary["mention_count"],
            "body_fetched": 0,
            "credibility_dist": {},
            "keywords": topic["keywords"],
        },
        "monitor": {"topic": topic, "window_start": window_start,
                    "window_end": window_end, "summary": summary},
        "events": events,
        "signals": signals,
        "runs": runs,
        "ai_ready": False,
    }
    target = Path(output_dir or DATA_DIR_REPORTS)
    target.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = f"{safe_filename_component(topic['name'])}监测周期报告-{stamp}-{uuid.uuid4().hex[:8]}"
    json_path = target / f"{stem}.json"
    md_path = target / f"{stem}.md"
    docx_path = target / f"{stem}.docx"
    report["json"] = str(json_path)
    report["md"] = str(md_path) if gen_markdown(report, str(md_path)) else ""
    report["docx"] = str(docx_path) if gen_docx(report, str(docx_path)) else ""
    json_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return report

"""持续监测服务（G2）：订阅、调度、游标和失败退避。

调度器是单进程实例；采集函数通过构造参数注入，便于离线测试而不访问
真实 SearXNG。监测成功才推进 cursor，失败只记录 run 并进入退避。
"""
from __future__ import annotations

import threading
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from apscheduler.schedulers.background import BackgroundScheduler

import db
from collector import collect_topic
from evidence import normalize_mention
from events import assign_mention
from alerts import evaluate_topic

LOGGER = logging.getLogger("miaoyu.monitor")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _source_id(item: dict) -> str:
    source = str(item.get("source_name") or item.get("source") or "").strip()
    if source:
        return source
    host = urlsplit(str(item.get("url") or "")).netloc.lower()
    return host or "unknown"


class MonitorService:
    def __init__(self, collect_fn=None, tick_seconds: int = 15):
        self.collect_fn = collect_fn or collect_topic
        self.tick_seconds = max(5, int(tick_seconds))
        self.scheduler = BackgroundScheduler(timezone="UTC", daemon=True)
        self._started = False
        self._start_lock = threading.Lock()
        self._run_locks: dict[int, threading.Lock] = {}
        self._run_locks_lock = threading.Lock()

    def start(self) -> None:
        with self._start_lock:
            if self._started:
                return
            self.scheduler.add_job(
                self.tick, "interval", seconds=self.tick_seconds,
                id="monitor-dispatch", max_instances=1, coalesce=True,
                replace_existing=True,
            )
            self.scheduler.start()
            self._started = True

    def stop(self) -> None:
        with self._start_lock:
            if not self._started:
                return
            self.scheduler.shutdown(wait=False)
            self._started = False

    def _lock_for(self, subscription_id: int) -> threading.Lock:
        with self._run_locks_lock:
            return self._run_locks.setdefault(subscription_id, threading.Lock())

    def tick(self) -> list[dict]:
        """派发到期订阅；每个订阅互斥，避免重复扫描。"""
        now = _iso(_now())
        results = []
        for sub in db.subscriptions_due(now):
            topic = db.topic_get(sub["topic_id"])
            if topic and topic.get("kind") == "radar":
                # 雷达由独立调度器处理，不能误进入分析监测链路。
                continue
            lock = self._lock_for(sub["id"])
            if not lock.acquire(blocking=False):
                continue
            try:
                results.append(self.run_subscription(sub["id"], _lock_already_held=True))
            finally:
                lock.release()
        return results

    def run_subscription(self, subscription_id: int, *, _lock_already_held: bool = False) -> dict:
        lock = self._lock_for(subscription_id)
        if not _lock_already_held and not lock.acquire(blocking=False):
            return {"status": "skipped", "reason": "already_running", "subscription_id": subscription_id}
        try:
            return self._run_subscription(subscription_id)
        finally:
            if not _lock_already_held:
                lock.release()

    def _run_subscription(self, subscription_id: int) -> dict:
        started = _now()
        started_at = _iso(started)
        sub = db.subscription_get(subscription_id)
        if not sub:
            raise ValueError(f"订阅不存在: {subscription_id}")
        topic = db.topic_get(sub["topic_id"])
        if not topic:
            raise ValueError(f"主题不存在: {sub['topic_id']}")
        cursor_key = f"topic:{topic['id']}"
        cursor_before = db.cursor_get(cursor_key)
        run_id = db.monitor_run_create(subscription_id, topic["id"], started_at, cursor_before)
        LOGGER.info("monitor_run_started", extra={"scan_run_id": run_id,
                                                   "topic_id": topic["id"]})
        count = 0
        new_count = 0
        try:
            result = self.collect_fn(topic["name"], topic["keywords"])
            items = result.get("items") or []
            if not items and result.get("warning"):
                raise RuntimeError(str(result["warning"]))
            for item in items:
                if not isinstance(item, dict) or not str(item.get("title") or "").strip():
                    continue
                source_id = _source_id(item)
                mention = normalize_mention(
                    item, source_id=source_id, source_type="search",
                    captured_at=started_at, topic_id=topic["id"],
                )
                mention_id, created = db.mention_upsert(mention)
                db.mention_topic_touch(mention_id, topic["id"], started_at)
                assign_mention(topic["id"], mention_id, mention, started_at)
                count += 1
                new_count += int(created)
            evaluate_topic(topic["id"], started_at)
            cursor_after = started_at
            db.cursor_upsert(cursor_key, cursor_after, started_at)
            finished = _now()
            db.monitor_run_finish(
                run_id, status="success", finished_at=_iso(finished),
                item_count=count, new_count=new_count, cursor_after=cursor_after,
            )
            next_run = _iso(finished + timedelta(seconds=sub["interval_seconds"]))
            db.subscription_mark_success(subscription_id, _iso(finished), next_run)
            LOGGER.info("monitor_run_finished", extra={"scan_run_id": run_id,
                                                        "topic_id": topic["id"],
                                                        "status": "success"})
            return {"run_id": run_id, "status": "success", "items": count,
                    "new_count": new_count, "cursor": cursor_after}
        except Exception as exc:
            finished = _now()
            failures = int(sub.get("consecutive_failures") or 0) + 1
            backoff = min(900, 30 * (2 ** min(failures - 1, 4)))
            next_run = _iso(finished + timedelta(seconds=backoff))
            db.monitor_run_finish(
                run_id, status="error", finished_at=_iso(finished),
                item_count=count, new_count=new_count, cursor_after=cursor_before,
                error_message=str(exc),
            )
            db.subscription_mark_failure(subscription_id, _iso(finished), next_run, next_run)
            LOGGER.warning("monitor_run_finished", extra={"scan_run_id": run_id,
                                                            "topic_id": topic["id"],
                                                            "status": "error"})
            return {"run_id": run_id, "status": "error", "items": count,
                    "error": str(exc)[:500], "cursor": cursor_before,
                    "retry_at": next_run}


monitor_service = MonitorService()

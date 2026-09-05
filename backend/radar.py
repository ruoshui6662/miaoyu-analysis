"""雷达轻量采集链路（RADAR-1 ~ RADAR-3）。

雷达的职责只有三件事：拉取已启用的公开聚合信源、按关键词匹配、按时间保存。
它不调用大模型，不做情绪/风险/热度分析，也不创建 events/signals，和旧监测链路
保持边界。一次调度先拉取信源，再把同一批条目分发给多个关键词主题，避免重复请求。
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from apscheduler.schedulers.background import BackgroundScheduler

import db
from evidence import normalize_mention
from hotlists import fetch_for_sources
from radar_sources import RadarFeedError, fetch_feed

LOGGER = logging.getLogger("miaoyu.radar")


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


def match_keywords(item: dict, keywords: list[str], excludes: list[str] | None = None) -> dict | None:
    """大小写不敏感的多关键词匹配；返回命中词和命中位置，便于解释。"""
    title = str(item.get("title") or "").strip()
    snippet = str(item.get("snippet") or item.get("content") or "").strip()
    text = f"{title}\n{snippet}".casefold()
    if any(str(word).strip().casefold() in text for word in (excludes or []) if str(word).strip()):
        return None
    matched = [str(word).strip() for word in keywords if str(word).strip().casefold() in text]
    if not matched:
        return None
    location = "title" if any(word.casefold() in title.casefold() for word in matched) else "snippet"
    return {"matched_keywords": matched, "match_location": location}


class RadarService:
    def __init__(self, tick_seconds: int = 60):
        self.tick_seconds = max(30, int(tick_seconds))
        self.scheduler = BackgroundScheduler(timezone="UTC", daemon=True)
        self._started = False
        self._start_lock = threading.Lock()
        self._run_lock = threading.Lock()

    def start(self) -> None:
        with self._start_lock:
            if self._started:
                return
            self.scheduler.add_job(
                self.tick, "interval", seconds=self.tick_seconds,
                id="radar-dispatch", max_instances=1, coalesce=True,
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

    def tick(self) -> list[dict]:
        if not self._run_lock.acquire(blocking=False):
            return []
        try:
            due = db.radar_subscriptions_due(_iso(_now()))
            if not due:
                return []
            return self._collect_for_subscriptions(due)
        finally:
            self._run_lock.release()

    def run_topic(self, topic_id: str) -> dict:
        """手动刷新一个雷达主题，供页面的“立即刷新”使用。"""
        if not self._run_lock.acquire(blocking=False):
            return {"status": "skipped", "reason": "already_running", "topic_id": topic_id}
        try:
            sub = db.subscription_get_by_topic(topic_id)
            topic = db.topic_get(topic_id)
            if not topic or topic.get("kind") != "radar":
                return {"status": "error", "error": "雷达主题不存在", "topic_id": topic_id}
            if not sub:
                return {"status": "error", "error": "雷达主题没有订阅", "topic_id": topic_id}
            return self._collect_for_subscriptions([sub])[0]
        finally:
            self._run_lock.release()

    def _collect_for_subscriptions(self, subscriptions: list[dict]) -> list[dict]:
        started = _now()
        started_at = _iso(started)
        topic_ids = [str(sub["topic_id"]) for sub in subscriptions]
        rss_items, rss_warnings = self._fetch_rss_endpoints(
            db.radar_topic_endpoint_ids(topic_ids), started, started_at,
        )
        active_sources = [s for s in db.list_sources()
                          if s["enabled"] and s["stype"] in ("hotlist", "feed")]
        source_types = {s["name"]: ("feed" if s["stype"] == "feed" else "hotlist")
                        for s in active_sources}
        source_warnings: list[str] = []
        try:
            raw_items, source_warnings = fetch_for_sources(active_sources) if active_sources else ([], [])
        except Exception as exc:  # noqa: BLE001
            raw_items = []
            source_warnings = [f"雷达信源异常: {str(exc)[:120]}"]
        raw_items.extend(rss_items)
        source_warnings.extend(rss_warnings)
        for source in active_sources:
            warning = next((w for w in source_warnings if source["name"] in w), "")
            db.radar_source_state(
                str(source["id"]), name=source["name"],
                source_type="feed" if source["stype"] == "feed" else "hotlist",
                checked_at=started_at, status="error" if warning else "success",
                item_count=0 if warning else sum(1 for x in raw_items if x.get("source") == source["name"]),
                error_message=warning,
            )

        results = []
        for sub in subscriptions:
            results.append(self._ingest_topic(sub, raw_items, source_types, source_warnings,
                                              started_at, started))
        return results

    def _fetch_rss_endpoints(self, endpoint_ids: set[int], started: datetime,
                             started_at: str) -> tuple[list[dict], list[str]]:
        """每个已绑定 RSS 端点只抓一次；失败不影响热榜和其他端点。"""
        if not endpoint_ids:
            return [], []
        items: list[dict] = []
        warnings: list[str] = []
        for endpoint in db.radar_endpoints(enabled_only=True):
            if int(endpoint["id"]) not in endpoint_ids:
                continue
            if endpoint["endpoint_type"] not in {"rss", "atom"}:
                continue
            endpoint_id = int(endpoint["id"])
            state = db.radar_endpoint_state(endpoint_id)
            run_id = db.radar_sync_run_create(endpoint_id, started_at)
            try:
                result = fetch_feed(endpoint, state)
                finished = _now()
                is_unchanged = result["status"] == "unchanged"
                last_success = state.get("last_success_at", "") or finished.isoformat()
                next_fetch = _iso(finished + timedelta(seconds=endpoint["poll_interval_seconds"]))
                db.radar_endpoint_state_upsert(
                    endpoint_id, status="unchanged" if is_unchanged else "healthy",
                    checked_at=started_at, etag=result.get("etag", state.get("etag", "")),
                    last_modified=result.get("last_modified", state.get("last_modified", "")),
                    cursor_value=result.get("cursor_after", state.get("cursor_value", "")),
                    last_success_at=last_success, next_fetch_at=next_fetch,
                    consecutive_failures=0, cooldown_until="",
                    average_update_seconds=max(0, int((finished - started).total_seconds())),
                    last_http_status=result.get("http_status", 0),
                    item_count=len(result.get("items", [])), error_message="",
                )
                db.radar_sync_run_finish(
                    run_id, status="unchanged" if is_unchanged else "success",
                    finished_at=_iso(finished), item_count=len(result.get("items", [])),
                    http_status=result.get("http_status", 0),
                )
                for item in result.get("items", []):
                    item["source"] = endpoint["source_name"]
                    item["source_endpoint_id"] = endpoint_id
                    item["level"] = endpoint["level"]
                items.extend(result.get("items", []))
            except RadarFeedError as exc:
                self._record_rss_failure(endpoint, state, run_id, started_at, started, exc)
                warnings.append(f"{endpoint['source_name']} RSS 采集失败: {str(exc)[:120]}")
            except Exception as exc:  # noqa: BLE001
                wrapped = RadarFeedError("adapter_error", f"RSS 适配器异常: {str(exc)[:120]}")
                self._record_rss_failure(endpoint, state, run_id, started_at, started, wrapped)
                warnings.append(f"{endpoint['source_name']} RSS 采集失败: {str(exc)[:120]}")
        return items, warnings

    @staticmethod
    def _record_rss_failure(endpoint: dict, state: dict, run_id: int, started_at: str,
                            started: datetime, error: RadarFeedError) -> None:
        finished = _now()
        failures = int(state.get("consecutive_failures", 0)) + 1
        retry_seconds = min(900, 30 * (2 ** min(failures - 1, 5)))
        cooldown = _iso(finished + timedelta(seconds=retry_seconds))
        db.radar_endpoint_state_upsert(
            int(endpoint["id"]), status="cooldown" if failures >= 5 else "degraded",
            checked_at=started_at, etag=state.get("etag", ""),
            last_modified=state.get("last_modified", ""), cursor_value=state.get("cursor_value", ""),
            last_success_at=state.get("last_success_at", ""), next_fetch_at=cooldown,
            consecutive_failures=failures, cooldown_until=cooldown,
            average_update_seconds=max(0, int((finished - started).total_seconds())),
            last_http_status=error.http_status, item_count=0,
            error_message=str(error),
        )
        db.radar_sync_run_finish(
            run_id, status="error", finished_at=_iso(finished), http_status=error.http_status,
            error_code=error.code, error_message=str(error),
        )

    def _ingest_topic(self, sub: dict, raw_items: list[dict], source_types: dict,
                      source_warnings: list[str], started_at: str, started: datetime) -> dict:
        topic = db.topic_get(sub["topic_id"])
        if not topic:
            return {"status": "error", "topic_id": sub["topic_id"], "error": "主题不存在"}
        run_id = db.monitor_run_create(sub["id"], topic["id"], started_at,
                                       db.cursor_get(f"radar:{topic['id']}"))
        count = 0
        new_count = 0
        try:
            for raw in raw_items:
                hit = match_keywords(raw, topic["keywords"], topic.get("exclude_keywords"))
                if not hit:
                    continue
                source_id = _source_id(raw)
                mention = normalize_mention(
                    raw, source_id=source_id, source_type=source_types.get(raw.get("source"), "feed"),
                    captured_at=started_at, topic_id="",
                )
                if not mention["title"]:
                    continue
                mention_id, created = db.mention_upsert(mention)
                db.mention_topic_touch(mention_id, topic["id"], started_at,
                                        hit["matched_keywords"], hit["match_location"])
                count += 1
                new_count += int(created)
            db.cursor_upsert(f"radar:{topic['id']}", started_at, started_at)
            finished = _now()
            db.monitor_run_finish(run_id, status="success", finished_at=_iso(finished),
                                  item_count=count, new_count=new_count, cursor_after=started_at)
            next_run = _iso(finished + timedelta(seconds=sub["interval_seconds"]))
            db.subscription_mark_success(sub["id"], _iso(finished), next_run)
            return {"run_id": run_id, "status": "success", "topic_id": topic["id"],
                    "items": count, "new_count": new_count, "source_warnings": source_warnings[:5]}
        except Exception as exc:  # noqa: BLE001
            finished = _now()
            next_run = _iso(finished + timedelta(seconds=60))
            db.monitor_run_finish(run_id, status="error", finished_at=_iso(finished),
                                  item_count=count, new_count=new_count,
                                  cursor_after=started_at, error_message=str(exc))
            db.subscription_mark_failure(sub["id"], _iso(finished), next_run, next_run)
            LOGGER.warning("radar_run_failed", extra={"topic_id": topic["id"], "error": str(exc)})
            return {"run_id": run_id, "status": "error", "topic_id": topic["id"],
                    "items": count, "error": str(exc)[:500]}


radar_service = RadarService()

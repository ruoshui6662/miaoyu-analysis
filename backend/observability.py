"""轻量结构化日志与运行指标，不引入外部日志服务依赖。"""
from __future__ import annotations

import json
import logging
import time
import uuid
from collections import Counter

from flask import g, request


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("request_id", "scan_run_id", "topic_id", "source_id", "status", "elapsed_ms"):
            value = getattr(record, key, None)
            if value not in (None, ""):
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class Metrics:
    def __init__(self):
        self._counts = Counter()
        self._lock = __import__("threading").Lock()

    def observe_request(self, method: str, path: str, status: int, elapsed_ms: float) -> None:
        with self._lock:
            self._counts[f"http.{method}.{path}.{status}"] += 1

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._counts)


metrics = Metrics()


def request_id() -> str:
    value = request.headers.get("X-Request-ID", "").strip()
    return value[:80] if value else uuid.uuid4().hex[:16]


def begin_request() -> None:
    g.request_id = request_id()
    g.request_started = time.perf_counter()


def finish_request(response):
    elapsed_ms = round((time.perf_counter() - getattr(g, "request_started", time.perf_counter())) * 1000, 1)
    metrics.observe_request(request.method, request.path, response.status_code, elapsed_ms)
    logging.getLogger("miaoyu.http").info(
        "request", extra={"request_id": getattr(g, "request_id", ""),
                           "status": response.status_code, "elapsed_ms": elapsed_ms},
    )
    response.headers["X-Request-ID"] = getattr(g, "request_id", "")
    return response

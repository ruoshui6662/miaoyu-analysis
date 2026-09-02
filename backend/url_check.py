# -*- coding: utf-8 -*-
"""D1 来源 URL 可达性核查：并发 HEAD + 当日缓存，不拖慢主线。

结果分级：ok(2xx/3xx) / gone(4xx/5xx) / unreachable(网络异常/超时)
"""
from __future__ import annotations

import datetime
import threading
from concurrent.futures import ThreadPoolExecutor

import requests

from config import USER_AGENT

# 每个规范 URL 独立缓存，避免同日不同报告复用彼此的完整结果集。
_cache: dict[str, tuple[str, str]] = {}  # url -> (date, status)
_lock = threading.Lock()

_HEADERS = {"User-Agent": USER_AGENT}
_CONNECT, _READ = 3, 5


def _check_one(url: str) -> str:
    try:
        r = requests.head(url, headers=_HEADERS, timeout=(_CONNECT, _READ), allow_redirects=True)
        if r.status_code < 400:
            return "ok"
        if r.status_code < 500:
            return "gone"   # 404/410 等明确失效
        return "gone"
    except requests.exceptions.MissingSchema:
        return "unreachable"
    except requests.RequestException:
        # HEAD 被拒时降级为 GET（range 只取头，减少下载量）
        try:
            r = requests.get(url, headers={**_HEADERS, "Range": "bytes=0-0"},
                             timeout=(_CONNECT, _READ), allow_redirects=True)
            if r.status_code < 400:
                return "ok"
            return "gone"
        except requests.RequestException:
            return "unreachable"


def check_urls(urls: list[str], max_urls: int = 40, workers: int = 12) -> dict[str, str]:
    """并发核查 URL 可达性；同一 URL 当日缓存，互不污染。"""
    today = datetime.date.today().strftime("%Y-%m-%d")
    urls = list(dict.fromkeys(
        u.strip() for u in urls if u and u.strip().startswith(("http://", "https://"))
    ))[:max_urls]
    if not urls:
        return {}
    with _lock:
        result = {url: cached[1] for url in urls
                  if (cached := _cache.get(url)) and cached[0] == today}
    pending = [url for url in urls if url not in result]
    if pending:
        with ThreadPoolExecutor(max_workers=min(workers, len(pending))) as ex:
            checked = dict(zip(pending, ex.map(_check_one, pending)))
        result.update(checked)
        with _lock:
            for url, status in checked.items():
                _cache[url] = (today, status)
    return {url: result[url] for url in urls}


def summarize(check: dict[str, str]) -> dict:
    """统计：{ok, gone, unreachable, total}"""
    out = {"ok": 0, "gone": 0, "unreachable": 0, "total": len(check)}
    for st in check.values():
        if st in out:
            out[st] += 1
    return out

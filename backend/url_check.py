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

_cache: dict[str, tuple[str, dict[str, str]]] = {}  # key -> (date, result)
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
    """并发核查 URL 可达性。当日缓存，重复调用不重复请求。"""
    today = datetime.date.today().strftime("%Y-%m-%d")
    urls = [u.strip() for u in urls if u and u.startswith(("http://", "https://"))][:max_urls]
    if not urls:
        return {}
    with _lock:
        cached = _cache.get("urls")
        if cached and cached[0] == today:
            return cached[1]
    result: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for url, status in zip(urls, ex.map(_check_one, urls)):
            result[url] = status
    with _lock:
        _cache["urls"] = (today, result)
    return result


def summarize(check: dict[str, str]) -> dict:
    """统计：{ok, gone, unreachable, total}"""
    out = {"ok": 0, "gone": 0, "unreachable": 0, "total": len(check)}
    for st in check.values():
        if st in out:
            out[st] += 1
    return out
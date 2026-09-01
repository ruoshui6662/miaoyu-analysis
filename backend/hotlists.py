# -*- coding: utf-8 -*-
"""热榜/聚合信源采集（C1c）：tophub 榜单 + Buzzing JSON Feed。

- tophub：GET 榜页提取 nodeid → POST /node-items-by-date 取当日条目（标题/链接/热度）
- Buzzing：GET feed.json → 按 _site_identifier 过滤
- 全部失败静默降级（返回空 + 告警），不拖垮主线采集；频控：进程内缓存当日结果。
"""
from __future__ import annotations

import datetime
import json
import re
import time

import requests

from config import HTTP_TIMEOUT, USER_AGENT

TH_BASE = "https://tophub.today"
BUZZ_FEED = "https://www.buzzing.cc/feed.json"
_UA_BROWSER = USER_AGENT

_session = requests.Session()
_session.headers.update({"User-Agent": _UA_BROWSER})

_nodeid_cache: dict[str, int] = {}
_cache: dict[str, tuple[str, list[dict]]] = {}  # key -> (date, items)，当日缓存


def _cache_get(key: str) -> list[dict] | None:
    ent = _cache.get(key)
    if ent and ent[0] == datetime.date.today().strftime("%Y-%m-%d"):
        return ent[1]
    return None


def _cache_set(key: str, items: list[dict]) -> None:
    _cache[key] = (datetime.date.today().strftime("%Y-%m-%d"), items)


def _tophub_nodeid(hashid: str) -> int | None:
    if hashid in _nodeid_cache:
        return _nodeid_cache[hashid]
    try:
        r = _session.get(f"{TH_BASE}/n/{hashid}", timeout=HTTP_TIMEOUT)
        m = re.search(r'window\.nodeId\s*=\s*"?(\d+)"?', r.text)
        nodeid = int(m.group(1)) if m else None
    except requests.RequestException:
        return None
    _nodeid_cache[hashid] = nodeid
    return nodeid


def fetch_tophub_board(hashid: str, date: str | None = None) -> list[dict]:
    """抓取 tophub 单榜当日条目：[{title, url, hot, published}]"""
    key = f"th:{hashid}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    nodeid = _tophub_nodeid(hashid)
    if not nodeid:
        return []
    date = date or datetime.date.today().strftime("%Y-%m-%d")
    try:
        r = _session.post(
            f"{TH_BASE}/node-items-by-date",
            data={"p": 1, "date": date, "nodeid": nodeid},
            headers={"Referer": f"{TH_BASE}/n/{hashid}", "X-Requested-With": "XMLHttpRequest"},
            timeout=HTTP_TIMEOUT,
        )
        data = r.json()
        items = (data.get("data") or {}).get("items") or []
    except (requests.RequestException, ValueError):
        return []
    out = []
    for it in items:
        title = (it.get("title") or "").strip()
        url = it.get("url") or ""
        if not title:
            continue
        out.append({
            "title": title,
            "url": url,
            "hot": it.get("extra") or "",
            "published": (it.get("time") or "")[:10],
        })
    _cache_set(key, out)
    return out


def fetch_buzzing(filter_sites: list[str] | None = None) -> list[dict]:
    """抓取 Buzzing feed.json 并按来源平台过滤：[{title, url, hot, published, site, category}]"""
    key = "buzz:" + ",".join(filter_sites or [])
    hit = _cache_get(key)
    if hit is not None:
        return hit
    try:
        r = _session.get(BUZZ_FEED, timeout=HTTP_TIMEOUT + 5)
        feed = r.json()
        items = feed.get("items") or []
    except (requests.RequestException, ValueError):
        return []
    fs = set(filter_sites or [])
    out = []
    for it in items:
        site = it.get("_site_identifier") or ""
        if fs and site not in fs:
            continue
        title = (it.get("title") or "").strip()
        if not title:
            continue
        out.append({
            "title": title,
            "url": it.get("url") or "",
            "hot": it.get("_num_comments") or "",
            "published": (it.get("date_published") or "")[:10],
            "site": site,
            "category": it.get("_category") or "",
        })
    _cache_set(key, out)
    return out


def fetch_for_sources(sources: list[dict]) -> tuple[list[dict], list[str]]:
    """按 sources 表中已启用的 hotlist/feed 条目采集 → (条目列表, 告警列表)。"""
    results: list[dict] = []
    warns: list[str] = []
    for s in sources:
        try:
            if s["stype"] == "hotlist":
                hid = (s.get("extra") or {}).get("tophub_id")
                if not hid:
                    continue
                items = fetch_tophub_board(hid)
                src = s["name"]
            elif s["stype"] == "feed":
                items = fetch_buzzing((s.get("extra") or {}).get("filter_site"))
                src = "Buzzing"
            else:
                continue
            for it in items:
                it["source"] = src
                it["level"] = s["level"]
            results.extend(items)
            time.sleep(0.4)
        except Exception as e:  # noqa: BLE001 任何异常都不拖垮主线
            warns.append(f"{s['name']} 采集失败: {str(e)[:60]}")
    return results, warns


def hotlists_summary() -> list[dict]:
    """C1d 热榜发现页用：返回全部可展示榜单 + 各自条目（未启用也展示，条目可为空）。"""
    from db import list_sources
    active = [s for s in list_sources() if s["stype"] == "hotlist"]
    out = []
    for s in active:
        hid = (s.get("extra") or {}).get("tophub_id")
        items = fetch_tophub_board(hid) if hid else []
        out.append({"name": s["name"], "enabled": s["enabled"], "count": len(items),
                    "hot": items[:5]})
    return out


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("Buzzing 样例:")
    for it in fetch_buzzing(filter_sites=["bloombergnew"])[:3]:
        print(f"  - [{it['site']}/{it['category']}] {it['title'][:40]} | hot={it['hot']}")
    print("\n微博热搜样例:")
    for it in fetch_tophub_board("KqndgxeLl9")[:10]:
        print(f"  - {it['title'][:36]} | hot={it['hot']}")
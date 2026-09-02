# -*- coding: utf-8 -*-
"""热榜/聚合信源采集（provider 化，C1c + 首页态势面板）。

优先级（可轮替可降级）：
  1) tophubdata 官方 API（api.tophubdata.com，Header Authorization 认证，
     每日免费 1000 次 —— SQLite 记账，超过阈值自动降级；.env: TOPHUBDATA_KEY）
  2) tophub.today 后端 HTML 解析（兜底，无配额、稳定性中）
  3) 免费公开 API（V2EX/HN 等）——见 docs/信源精细化规划.md 轮替清单，按需接入

tophub 单榜：官方 /nodes/{hashid} → HTML 兜底
聚合榜（首页）：官方 /hot（一次调用全站）→ HTML 六榜合并兜底
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sqlite3
import time

import requests

from config import HTTP_TIMEOUT, SETTINGS_DB, USER_AGENT

TH_BASE = "https://tophub.today"
BUZZ_FEED = "https://www.buzzing.cc/feed.json"
THD_BASE = "https://api.tophubdata.com"

_UA_BROWSER = USER_AGENT


def _thd_key() -> str:
    # 运行时读取（配合 config.reload 热加载，设置页后续可直接改）
    return os.getenv("TOPHUBDATA_KEY", "").strip()


def _hot_limit() -> int:
    return int(os.getenv("HOT_DAILY_LIMIT", "900") or 900)

_session = requests.Session()
_session.headers.update({"User-Agent": _UA_BROWSER})

_cache: dict[str, tuple[str, list[dict]]] = {}  # key -> (date, items)，当日缓存


def _cache_get(key: str) -> list[dict] | None:
    ent = _cache.get(key)
    if ent and ent[0] == datetime.date.today().strftime("%Y-%m-%d"):
        return ent[1]
    return None


def _cache_set(key: str, items: list[dict]) -> None:
    _cache[key] = (datetime.date.today().strftime("%Y-%m-%d"), items)


# ---------- 配额记账（tophubdata 每日 1000 次） ----------

def _quota_used() -> int:
    try:
        conn = sqlite3.connect(str(SETTINGS_DB))
        row = conn.execute(
            "SELECT n FROM hot_quota WHERE d = ?",
            (datetime.date.today().strftime("%Y-%m-%d"),)).fetchone()
        conn.close()
        return row[0] if row else 0
    except sqlite3.Error:
        return 0


def _quota_inc() -> None:
    try:
        conn = sqlite3.connect(str(SETTINGS_DB))
        conn.execute("CREATE TABLE IF NOT EXISTS hot_quota (d TEXT PRIMARY KEY, n INTEGER)")
        conn.execute("INSERT INTO hot_quota(d, n) VALUES(?, 1) "
                     "ON CONFLICT(d) DO UPDATE SET n = n + 1",
                     (datetime.date.today().strftime("%Y-%m-%d"),))
        conn.commit()
        conn.close()
    except sqlite3.Error:
        pass


def _quota_ok() -> bool:
    return _quota_used() < _hot_limit()


def quota_state() -> dict:
    used = _quota_used()
    return {"used": used, "limit": _hot_limit(),
            "provider": "tophubdata" if (_thd_key() and used < _hot_limit()) else "html"}


# ---------- tophubdata 官方 API ----------

_thd_off: str = ""  # 当日官方接口停用（业务错误如余额不足）


def _thd(path: str, params: dict | None = None) -> dict | None:
    """官方 API 请求。无 key/超配额/出错 → None（降级）。每次调用记账。"""
    global _thd_off
    today = datetime.date.today().strftime("%Y-%m-%d")
    if _thd_off == today:
        return None
    if not _thd_key() or not _quota_ok():
        return None
    try:
        r = requests.get(THD_BASE + path, headers={"Authorization": _thd_key(),
                                                   "User-Agent": _UA_BROWSER},
                         params=params, timeout=15)
        _quota_inc()
        d = r.json()
    except (requests.RequestException, ValueError):
        _quota_inc()
        return None
    if d.get("error") is False and isinstance(d.get("data"), (dict, list)):
        return d["data"]
    # 业务错误（余额不足/配额类，status 100300 等）→ 当日暂停官方，避免后续请求白耗配额
    msg = str(d.get("msg") or "")
    status = d.get("status")
    if status in (100300, 100400) or "余额" in msg or "额度" in msg:
        _thd_off = today
    return None


def _parse_thd_items(container) -> list[dict]:
    """容错解析官方 API 条目（结构以文档 '榜单最新详细' 为准，键做兼容）。"""
    if isinstance(container, dict):
        container = container.get("items") or container.get("list") or []
    if not isinstance(container, list):
        return []
    out = []
    for it in container[:30]:
        if not isinstance(it, dict):
            continue
        title = str(it.get("title") or it.get("name") or "").strip()
        if not title:
            continue
        out.append({
            "title": title,
            "url": it.get("url") or it.get("link") or "",
            "hot": it.get("hot") or it.get("heat") or it.get("read") or "",
            "published": it.get("published") or it.get("time") or "",
        })
    return out


# ---------- tophub.today HTML 兜底 ----------

def _fetch_html_board(hashid: str) -> list[dict]:
    """tophub.today 单榜：直解页面 SSR 表格（条目带 itemid 锚点 + ws 热度单元格）。

    条目本就服务端渲染在页面上，无需旧版"提取 nodeId → POST 二次接口"两步：
    页面 nodeId 已退化为占位值，二次接口随前端 JS 变动（2026-09 实测失效），直解最稳。
    """
    try:
        r = _session.get(f"{TH_BASE}/n/{hashid}", timeout=HTTP_TIMEOUT)
        html = r.text
    except requests.RequestException:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for tr in re.findall(r"<tr>.*?</tr>", html, re.S):
        m = re.search(r'<a[^>]+href="([^"]+)"[^>]+itemid="\d+"[^>]*>([^<]{2,})</a>', tr)
        if not m:
            continue
        url, title = m.group(1), m.group(2).strip()
        if title in seen:
            continue
        seen.add(title)
        mh = (re.search(r'<td[^>]*class="ws"[^>]*>([^<]*)</td>', tr)
              or re.search(r'<div[^>]*class="item-desc"[^>]*>([^<]*)</div>', tr))
        out.append({"title": title, "url": url,
                    "hot": mh.group(1).strip() if mh else "",
                    "published": datetime.date.today().strftime("%Y-%m-%d")})
        if len(out) >= 30:
            break
    return out


def fetch_tophub_board(hashid: str, date: str | None = None) -> list[dict]:
    """单榜当日条目：官方 API 优先 → HTML 兜底。"""
    key = f"th:{hashid}:{date or ''}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    items = _parse_thd_items(_thd(f"/nodes/{hashid}", {"date": date} if date else None))
    if not items:
        items = _fetch_html_board(hashid)
    _cache_set(key, items)
    return items


def fetch_aggregated() -> list[dict]:
    """聚合热点（首页）：官方 /hot 优先 → HTML 六榜合并兜底。"""
    key = "agg"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    data = _thd("/hot")
    boards: list[dict] = []
    if isinstance(data, dict):
        # 兼容多种结构：{data:[{name,items}]} 或 {p:{节点:{items}}}
        pool = data.get("list") or data.get("boards") or []
        for b in pool[:20]:
            if isinstance(b, dict):
                boards.append({
                    "name": str(b.get("name") or b.get("title") or "热榜"),
                    "items": _parse_thd_items(b),
                })
    if not boards:
        boards = legacy_aggregate_boards()
    # 兜底失败时保留现有路径：从 sources 的 hotlist 条目取
    if not boards:
        from db import list_sources
        for s in [x for x in list_sources() if x["stype"] == "hotlist"]:
            hid = (s.get("extra") or {}).get("tophub_id")
            if hid:
                boards.append({"name": s["name"], "items": fetch_tophub_board(hid)})
    _cache_set(key, boards if isinstance(boards, list) else [])
    return boards


def legacy_aggregate_boards() -> list[dict]:
    """HTML 六榜（无配额）聚合，与旧行为一致。"""
    from db import list_sources
    boards = []
    for s in [x for x in list_sources() if x["stype"] == "hotlist"]:
        hid = (s.get("extra") or {}).get("tophub_id")
        if hid:
            boards.append({"name": s["name"], "items": fetch_tophub_board(hid)})
    return boards


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
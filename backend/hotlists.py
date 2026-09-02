# -*- coding: utf-8 -*-
"""热榜/聚合信源采集（provider 化，C1c + 首页态势面板）。

优先级（可轮替可降级）：
  1) NewsNow 公开 JSON（中国重点热榜，无 Key，5 分钟本地缓存）
  2) REBANG / tophub.today 服务端 HTML（缺榜或公开源不可达时兜底）
  3) TopHubData 官方 API（仅当 PAID_APIS_ENABLED=true 且配置 Key 时启用）

tophub 单榜：官方 /nodes/{hashid} → HTML 兜底
聚合榜（首页）：NewsNow 六榜 → REBANG/TopHub HTML → 可选官方 /hot
"""
from __future__ import annotations

import datetime
import html
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
NEWSNOW_BASE = os.getenv("NEWSNOW_BASE_URL", "https://newsnow.busiyi.world").rstrip("/")
REBANG_BASE = "https://top.open2hub.com"

# NewsNow 的公开源 ID 与项目内榜单名称保持显式映射，避免用平台名猜 URL。
NEWSNOW_BOARDS = {
    "weibo": "微博热搜",
    "zhihu": "知乎热榜",
    "bilibili": "B站热门",
    "douyin": "抖音热点",
    "baidu": "百度热搜",
    "toutiao": "今日头条热榜",
}

_UA_BROWSER = USER_AGENT


def _thd_key() -> str:
    # 运行时读取（配合 config.reload 热加载，设置页后续可直接改）
    return os.getenv("TOPHUBDATA_KEY", "").strip()


def _hot_limit() -> int:
    return int(os.getenv("HOT_DAILY_LIMIT", "900") or 900)


def _paid_apis_enabled() -> bool:
    """付费数据源预留开关，默认关闭；当前阶段只走公开源。"""
    return os.getenv("PAID_APIS_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")

_session = requests.Session()
_session.headers.update({"User-Agent": _UA_BROWSER})

_cache: dict[str, tuple[str, list[dict]]] = {}  # key -> (date, items)，当日缓存
_newsnow_cache: dict[str, tuple[float, list[dict]]] = {}
_newsnow_health: dict[str, dict] = {}
_aggregate_cache: tuple[float, list[dict]] | None = None


def _newsnow_enabled() -> bool:
    """公开实例默认开启；显式关闭用于故障隔离或切换到自建实例。"""
    return os.getenv("NEWSNOW_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")


def _newsnow_ttl() -> int:
    try:
        return max(60, int(os.getenv("NEWSNOW_CACHE_SECONDS", "300")))
    except ValueError:
        return 300


def _newsnow_json(source_id: str) -> tuple[dict | None, str]:
    """读取 NewsNow 公开 JSON；显式按 UTF-8 解码，避开 requests 本机编码探测差异。"""
    if not _newsnow_enabled() or source_id not in NEWSNOW_BOARDS:
        return None, "disabled"
    now = time.monotonic()
    hit = _newsnow_cache.get(source_id)
    if hit and now - hit[0] < _newsnow_ttl():
        return {"items": hit[1], "status": "local-cache"}, "cache"
    started = time.monotonic()
    try:
        response = _session.get(
            f"{NEWSNOW_BASE}/api/s",
            params={"id": source_id, "latest": "true"},
            headers={"Accept": "application/json", "Referer": f"{NEWSNOW_BASE}/"},
            timeout=min(max(8, HTTP_TIMEOUT), 20),
        )
        if response.status_code != 200:
            raise requests.HTTPError(f"HTTP {response.status_code}")
        data = json.loads(response.content.decode("utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise ValueError("响应缺少 items[]")
        _newsnow_health[source_id] = {
            "ok": True, "status": data.get("status", "success"),
            "count": len(data["items"]),
            "latency_ms": round((time.monotonic() - started) * 1000),
            "updated_time": data.get("updatedTime"),
        }
        return data, "success"
    except (requests.RequestException, UnicodeError, ValueError) as exc:
        _newsnow_health[source_id] = {
            "ok": False, "error": str(exc)[:120],
            "latency_ms": round((time.monotonic() - started) * 1000),
        }
        return None, "error"


def fetch_newsnow_board(source_id: str) -> list[dict]:
    """NewsNow 单榜标准化：保留原站 URL，热度缺失时不伪造数值。"""
    data, state = _newsnow_json(source_id)
    if data is None:
        return []
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    out: list[dict] = []
    for rank, item in enumerate(data.get("items", [])[:30], start=1):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        out.append({
            "title": title,
            "url": item.get("url") or item.get("mobileUrl") or "",
            "mobileUrl": item.get("mobileUrl") or item.get("url") or "",
            "hot": extra.get("hot") or extra.get("info") or "",
            "published": extra.get("date") or extra.get("time") or "",
            "rank": rank,
            "source_id": source_id,
            "provider": "newsnow",
            "captured_at": now,
        })
    if state == "success":
        _newsnow_cache[source_id] = (time.monotonic(), out)
    return out


def fetch_newsnow_aggregated() -> list[dict]:
    """并行能力的最小实现：逐榜隔离失败，至少一个榜有数据即可作为主链路。"""
    boards: list[dict] = []
    for source_id, name in NEWSNOW_BOARDS.items():
        items = fetch_newsnow_board(source_id)
        boards.append({"name": name, "source_id": source_id,
                       "provider": "newsnow", "items": items})
    return [b for b in boards if b["items"]]


def _fetch_rebang_boards() -> list[dict]:
    """REBANG 公开 HTML 低频兜底；页面结构变化时返回空，不影响主链路。"""
    try:
        response = _session.get(f"{REBANG_BASE}/", timeout=HTTP_TIMEOUT)
        if response.status_code != 200:
            return []
        text = response.content.decode("utf-8", "replace")
    except (requests.RequestException, UnicodeError):
        return []
    out: list[dict] = []
    cards = re.findall(r'<div class="card-rebang">(.*?)(?=<div class="card-rebang">|</main>)', text, re.S)
    for card in cards:
        title_match = re.search(r'class="platform-title">\s*(.*?)\s*</h3>', card, re.S)
        if not title_match:
            continue
        name = re.sub(r"<[^>]+>", "", html.unescape(title_match.group(1))).strip()
        items: list[dict] = []
        for match in re.finditer(r'<a class="list-item-link"[^>]+href="([^"]+)"[^>]*>.*?class="list-text">\s*(.*?)\s*</span>', card, re.S):
            url = html.unescape(match.group(1))
            item_title = re.sub(r"<[^>]+>", "", html.unescape(match.group(2))).strip()
            if item_title:
                items.append({"title": item_title, "url": url,
                              "hot": "", "published": "",
                              "provider": "rebang"})
            if len(items) >= 30:
                break
        if items:
            out.append({"name": name, "provider": "rebang", "items": items})
    return out


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
            "provider": "tophubdata" if (_paid_apis_enabled() and _thd_key() and used < _hot_limit()) else "public"}


def source_health() -> dict:
    """返回本轮公开源健康快照；不暴露任何密钥。"""
    return {"newsnow": {k: dict(v) for k, v in _newsnow_health.items()},
            "paid_apis_enabled": _paid_apis_enabled()}


# ---------- tophubdata 官方 API ----------

_thd_off: str = ""  # 当日官方接口停用（业务错误如余额不足）


def _thd(path: str, params: dict | None = None) -> dict | None:
    """官方 API 请求。无 key/超配额/出错 → None（降级）。每次调用记账。"""
    global _thd_off
    today = datetime.date.today().strftime("%Y-%m-%d")
    if _thd_off == today:
        return None
    if not _paid_apis_enabled() or not _thd_key() or not _quota_ok():
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
    """聚合热点：NewsNow 无 Key 主源 → TopHub/REBANG HTML → 旧路径。"""
    global _aggregate_cache
    now = time.monotonic()
    if _aggregate_cache and now - _aggregate_cache[0] < _newsnow_ttl():
        return _aggregate_cache[1]

    # 中国内地重点榜单优先。每个榜单独立失败，部分成功也可交付。
    boards = fetch_newsnow_aggregated()
    names = {b["name"] for b in boards}
    if len(boards) < len(NEWSNOW_BOARDS):
        for fallback in _fetch_rebang_boards() + legacy_aggregate_boards():
            if fallback.get("name") not in names and fallback.get("items"):
                boards.append(fallback)
                names.add(fallback.get("name"))

    # NewsNow 全部不可用时，保留原有聚合行为；付费 TopHubData 由开关控制。
    if not boards:
        data = _thd("/hot")
        if isinstance(data, dict):
            pool = data.get("list") or data.get("boards") or []
            for b in pool[:20]:
                if isinstance(b, dict):
                    boards.append({
                        "name": str(b.get("name") or b.get("title") or "热榜"),
                        "provider": "tophubdata", "items": _parse_thd_items(b),
                    })
        if not boards:
            boards = legacy_aggregate_boards()

    _aggregate_cache = (now, boards if isinstance(boards, list) else [])
    return _aggregate_cache[1]


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
                extra = s.get("extra") or {}
                newsnow_id = extra.get("newsnow_id") or extra.get("board")
                hid = extra.get("tophub_id")
                if not newsnow_id and not hid:
                    continue
                items = fetch_newsnow_board(newsnow_id) if newsnow_id else []
                if not items and hid:
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
        extra = s.get("extra") or {}
        newsnow_id = extra.get("newsnow_id") or extra.get("board")
        hid = extra.get("tophub_id")
        items = fetch_newsnow_board(newsnow_id) if newsnow_id else []
        if not items and hid:
            items = fetch_tophub_board(hid)
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

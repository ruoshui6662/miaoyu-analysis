"""SearXNG JSON API 客户端：多关键词 × 多引擎 × 时间窗批量搜索。

依赖：requests（纯 Python），不依赖 lxml/pydantic 等被本机策略拦截的 C 扩展。
"""
from __future__ import annotations

import time
from urllib.parse import quote

import requests

from config import SEARXNG_TIMEOUT, SEARXNG_URL, USER_AGENT, MAX_ITEMS_PER_QUERY

# 结果统一规范字段
_KEEP = ("title", "url", "content", "published", "engine", "engines", "positions", "score", "category", "source")


def _norm_result(r: dict) -> dict:
    pub = r.get("publishedDate") or r.get("pubdate") or ""
    return {
        "title": (r.get("title") or "").strip(),
        "url": r.get("url") or "",
        "content": (r.get("content") or "").strip(),
        "published": str(pub)[:19] if pub else "",
        "engine": r.get("engine") or "",
        "engines": r.get("engines") or [],
        "score": r.get("score") or 0,
        "category": r.get("category") or "",
    }


class SearxClient:
    def __init__(self, base_url: str = SEARXNG_URL, timeout: int = SEARXNG_TIMEOUT):
        self.base_url = base_url
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    def search(
        self,
        query: str,
        pageno: int = 1,
        language: str = "zh-CN",
        time_range: str = "",       # 空|day|week|month|year
        categories: str = "general",
        engines: list[str] | None = None,
    ) -> dict:
        """单次查询，返回 {query, results, suggestions, unresponsive}"""
        params = {
            "q": query,
            "format": "json",
            "pageno": pageno,
            "language": language,
            "categories": categories,
        }
        if time_range:
            params["time_range"] = time_range
        if engines:
            params["engines"] = ",".join(engines)
        for attempt in range(2):
            try:
                resp = self.session.get(f"{self.base_url}/search", params=params, timeout=self.timeout)
                if resp.status_code == 403:
                    # 部分实例对无 Referer/频繁请求返回 403，退避一次
                    time.sleep(2)
                    continue
                resp.raise_for_status()
                data = resp.json()
                return {
                    "query": query,
                    "results": [_norm_result(r) for r in data.get("results", [])],
                    "suggestions": data.get("suggestions", []),
                    "unresponsive": data.get("unresponsive_engines", []),
                }
            except requests.RequestException as e:
                if attempt == 1:
                    return {"query": query, "results": [], "suggestions": [], "unresponsive": [], "error": str(e)}
                time.sleep(1)
        return {"query": query, "results": [], "suggestions": [], "unresponsive": []}

    def multi_search(
        self,
        queries: list[str],
        engines: list[str] | None = None,
        time_range: str = "",
        language: str = "zh-CN",
        max_items: int = MAX_ITEMS_PER_QUERY,
    ) -> list[dict]:
        """多关键词批量搜索，按关键词顺序聚合原始结果（去重在清洗层做）。"""
        all_results: list[dict] = []
        for q in queries:
            data = self.search(q, language=language, time_range=time_range, engines=engines)
            for r in data["results"][:max_items]:
                r["query"] = q
                all_results.append(r)
            time.sleep(0.4)  # 礼貌限速，避免触发实例风控
        return all_results


def explore_engines(base_url: str = SEARXNG_URL):
    """探测实例可用的引擎（通过一次查询返回的 engines 字段近似）。"""
    c = SearxClient(base_url)
    d = c.search("热点 舆情", engines=["google cse", "bing", "baidu"])
    return [r["engine"] for r in d["results"]] + d["unresponsive"]


if __name__ == "__main__":
    import json

    c = SearxClient()
    q = "假装上班公司 舆情"
    print(f">>> 查询: {q}  @ {c.base_url}")
    d = c.search(q)
    print(f"结果数: {len(d['results'])}  未响应引擎: {d['unresponsive']}")
    for r in d["results"][:8]:
        print(f"- [{r['engine']}] {r['title'][:40]} | {r['published'][:10]} | {r['url'][:60]}")
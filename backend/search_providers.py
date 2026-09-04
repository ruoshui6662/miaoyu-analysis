"""统一搜索 Provider：保留 SearXNG 主链路，按配置接入 Brave/Tavily。

搜索服务与首页热榜是两条链路。这里仅负责分析任务的网页证据发现，
不改变热榜适配器，也不把不同 Provider 的原生热度值混成一个分数。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests

import config as _cfg
from searx_client import SearxClient


def _published(value: Any) -> str:
    if not value:
        return ""
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return str(value)[:19]


def _empty(query: str, provider: str, error: str = "") -> dict:
    data = {
        "query": query,
        "results": [],
        "suggestions": [],
        "unresponsive": [],
        "provider": provider,
    }
    if error:
        data["error"] = error
    return data


class BraveSearchClient:
    provider_id = "brave"

    def __init__(self, api_key: str | None = None, timeout: int | None = None):
        self.api_key = (api_key if api_key is not None else _cfg.BRAVE_API_KEY).strip()
        self.endpoint = _cfg.BRAVE_SEARCH_ENDPOINT
        self.timeout = timeout if timeout is not None else _cfg.SEARCH_TIMEOUT
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "User-Agent": _cfg.USER_AGENT,
            "X-Subscription-Token": self.api_key,
        })

    def search(self, query: str, pageno: int = 1, language: str = "zh-CN",
               time_range: str = "", categories: str = "general",
               engines: list[str] | None = None) -> dict:
        if not self.api_key:
            return _empty(query, self.provider_id, "Brave API Key 未配置")
        lang = _cfg.BRAVE_SEARCH_LANG or language.split("-")[0]
        params = {
            "q": query,
            "count": min(20, max(1, _cfg.MAX_ITEMS_PER_QUERY)),
            "offset": max(0, pageno - 1),
            "country": _cfg.BRAVE_COUNTRY,
            "search_lang": lang,
            "safesearch": "off",
        }
        freshness = {"day": "pd", "week": "pw", "month": "pm", "year": "py"}.get(time_range)
        if freshness:
            params["freshness"] = freshness
        try:
            response = self.session.get(self.endpoint, params=params, timeout=self.timeout)
            response.raise_for_status()
            payload = response.json()
            results = []
            for item in (payload.get("web") or {}).get("results", []):
                results.append({
                    "title": str(item.get("title") or "").strip(),
                    "url": item.get("url") or "",
                    "content": str(item.get("description") or "").strip(),
                    "published": _published(item.get("page_age") or item.get("age")),
                    "engine": "brave",
                    "engines": ["brave"],
                    "score": 0,
                    "category": categories or "general",
                    "provider": self.provider_id,
                })
            return {**_empty(query, self.provider_id), "results": results,
                    "more_results_available": bool((payload.get("query") or {}).get("more_results_available"))}
        except requests.RequestException as exc:
            return _empty(query, self.provider_id, f"Brave 请求失败：{exc}")
        except (TypeError, ValueError) as exc:
            return _empty(query, self.provider_id, f"Brave 响应解析失败：{exc}")


class TavilySearchClient:
    provider_id = "tavily"

    def __init__(self, api_key: str | None = None, timeout: int | None = None):
        self.api_key = (api_key if api_key is not None else _cfg.TAVILY_API_KEY).strip()
        self.endpoint = _cfg.TAVILY_SEARCH_ENDPOINT
        self.timeout = timeout if timeout is not None else _cfg.SEARCH_TIMEOUT
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": _cfg.USER_AGENT,
            "Authorization": f"Bearer {self.api_key}",
        })

    def search(self, query: str, pageno: int = 1, language: str = "zh-CN",
               time_range: str = "", categories: str = "general",
               engines: list[str] | None = None) -> dict:
        if not self.api_key:
            return _empty(query, self.provider_id, "Tavily API Key 未配置")
        payload = {
            "query": query,
            "search_depth": _cfg.TAVILY_SEARCH_DEPTH,
            "topic": "general",
            "max_results": min(20, max(1, _cfg.MAX_ITEMS_PER_QUERY)),
            "include_answer": False,
            "include_raw_content": False,
            "country": "china",
        }
        if time_range in {"day", "week", "month", "year"}:
            payload["time_range"] = time_range
        try:
            response = self.session.post(self.endpoint, json=payload, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
            results = []
            for item in data.get("results", []):
                results.append({
                    "title": str(item.get("title") or "").strip(),
                    "url": item.get("url") or "",
                    "content": str(item.get("content") or "").strip(),
                    "published": _published(item.get("published_date")),
                    "engine": "tavily",
                    "engines": ["tavily"],
                    "score": item.get("score") or 0,
                    "category": categories or "general",
                    "provider": self.provider_id,
                })
            return {**_empty(query, self.provider_id), "results": results,
                    "usage": data.get("usage") or {}}
        except requests.RequestException as exc:
            return _empty(query, self.provider_id, f"Tavily 请求失败：{exc}")
        except (TypeError, ValueError) as exc:
            return _empty(query, self.provider_id, f"Tavily 响应解析失败：{exc}")


class SearchRouter:
    """按配置路由搜索请求。

    调用优先级固定为 SearXNG → Brave → Tavily；“固定优先级”与“必须可用”
    是两件事。未配置的 Provider 会在路由阶段被跳过，不会阻塞后续服务。

    failover：省额度，遇到空结果/异常才继续下一个 Provider；
    fanout：多源合并，适合重点事件，不做跨 Provider 的绝对排序。
    """

    PROVIDER_ORDER = ("searxng", "brave", "tavily")

    def __init__(self, timeout: int | None = None):
        self.timeout = timeout if timeout is not None else _cfg.SEARCH_TIMEOUT
        self.providers = {
            "searxng": SearxClient(timeout=self.timeout),
            "brave": BraveSearchClient(timeout=self.timeout),
            "tavily": TavilySearchClient(timeout=self.timeout),
        }

    def _enabled_ids(self) -> list[str]:
        """只返回已配置的服务，并始终保持固定优先级。"""
        configured = []
        for pid in self.PROVIDER_ORDER:
            provider = self.providers[pid]
            if pid == "searxng":
                is_configured = bool(getattr(provider, "base_url", "").strip())
            else:
                is_configured = bool(getattr(provider, "api_key", "").strip())
            if is_configured:
                configured.append(pid)
        return configured

    def search(self, query: str, pageno: int = 1, language: str = "zh-CN",
               time_range: str = "", categories: str = "general",
               engines: list[str] | None = None) -> dict:
        ids = self._enabled_ids()
        collected: list[dict] = []
        errors: list[str] = []
        used: list[str] = []
        for pid in ids:
            data = self.providers[pid].search(
                query, pageno=pageno, language=language, time_range=time_range,
                categories=categories, engines=engines,
            )
            used.append(pid)
            if data.get("error"):
                errors.append(data["error"])
            for item in data.get("results") or []:
                item.setdefault("provider", pid)
                collected.append(item)
            if _cfg.SEARCH_PROVIDER_MODE != "fanout" and collected:
                break
        # 仅对 URL 去重，保留同 URL 的第一来源和来源字段，便于报告追溯。
        deduped: list[dict] = []
        seen: set[str] = set()
        for item in collected:
            key = item.get("url") or f"title:{item.get('title', '')}"
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        result = {**_empty(query, used[0] if used else ""),
                  "results": deduped, "providers_used": used}
        if errors and not deduped:
            result["error"] = "；".join(errors[:3])
        elif errors:
            result["provider_errors"] = errors[:3]
        elif not used:
            result["error"] = "没有可用的搜索服务，请至少配置 SearXNG、Brave Search 或 Tavily"
        return result

    def test(self, provider_id: str, query: str = "中国 舆情 新闻") -> dict:
        if provider_id not in self.providers:
            return {"ok": False, "error": "不支持的搜索服务商"}
        if provider_id == "searxng" and not self.providers[provider_id].base_url.strip():
            return {"ok": False, "provider": provider_id, "query": query,
                    "count": 0, "results": [], "error": "SearXNG 搜索地址未配置"}
        data = self.providers[provider_id].search(query, language="zh-CN", categories="general")
        return {
            "ok": not bool(data.get("error")),
            "provider": provider_id,
            "query": query,
            "count": len(data.get("results") or []),
            "results": (data.get("results") or [])[:3],
            "error": data.get("error", ""),
        }

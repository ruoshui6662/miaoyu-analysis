"""雷达 RSS/Atom 端点适配器（RADAR-SRC-1）。

本模块只负责安全请求、RSS/Atom 解析和统一条目输出，不做关键词匹配、正文抓取
或 AI 分析。依赖 requests + Python 标准库，避免为一个结构化 Feed 引入重型运行时。
"""
from __future__ import annotations

import email.utils
import ipaddress
import os
import socket
from datetime import datetime, timezone
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

import requests

from config import USER_AGENT


MAX_FEED_BYTES = 2 * 1024 * 1024
HTTP_TIMEOUT = (5, 20)


class RadarFeedError(RuntimeError):
    def __init__(self, code: str, message: str, *, http_status: int = 0):
        super().__init__(message)
        self.code = code
        self.http_status = int(http_status or 0)


def _private_allowed() -> bool:
    return os.getenv("MIAOYU_RADAR_ALLOW_PRIVATE_SOURCES", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _private_address(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_private or ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_endpoint_url(url: str, *, allow_private: bool | None = None) -> str:
    """只接受 HTTP(S) URL，并拒绝显式凭据和未授权私网地址。"""
    raw = str(url or "").strip()
    parts = urlsplit(raw)
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        raise RadarFeedError("invalid_url", "只支持 http:// 或 https:// 地址")
    if parts.username or parts.password:
        raise RadarFeedError("credentials_in_url", "地址不能包含账号或密码")
    host = parts.hostname or ""
    if not host:
        raise RadarFeedError("invalid_url", "地址缺少主机名")
    if allow_private is None:
        allow_private = _private_allowed()
    if _private_address(host) and not allow_private:
        raise RadarFeedError("private_address_blocked", "默认禁止访问内网或本机地址")
    return raw


def _check_resolved_target(url: str, *, allow_private: bool | None = None) -> None:
    """在发起请求前复核 DNS 结果，降低 DNS rebinding 风险。"""
    raw = validate_endpoint_url(url, allow_private=allow_private)
    host = urlsplit(raw).hostname or ""
    if allow_private is None:
        allow_private = _private_allowed()
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise RadarFeedError("dns_failed", f"无法解析信源地址: {host}") from exc
    if not allow_private:
        for address in addresses:
            try:
                ip = ipaddress.ip_address(address)
            except ValueError:
                continue
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_reserved:
                raise RadarFeedError("private_address_blocked", "信源解析到了未授权内网地址")


def _local_name(tag: str) -> str:
    return str(tag or "").rsplit("}", 1)[-1].lower()


def _text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return " ".join("".join(node.itertext()).split())


def _first_child(node: ET.Element, names: set[str]) -> ET.Element | None:
    for child in list(node):
        if _local_name(child.tag) in names:
            return child
    return None


def _link(node: ET.Element) -> str:
    candidates = []
    for child in list(node):
        if _local_name(child.tag) != "link":
            continue
        href = str(child.attrib.get("href") or "").strip()
        value = href or _text(child)
        if not value:
            continue
        rel = str(child.attrib.get("rel") or "alternate").lower()
        candidates.append((rel == "alternate", value))
    if not candidates:
        return ""
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _date(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = None
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        parsed = None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return raw
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def parse_feed(payload: bytes) -> dict:
    if not payload:
        raise RadarFeedError("empty_feed", "信源返回空内容")
    probe = payload[:4096].upper()
    if b"<!DOCTYPE" in probe or b"<!ENTITY" in probe:
        raise RadarFeedError("unsafe_xml", "拒绝包含外部实体声明的 XML")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise RadarFeedError("invalid_xml", "返回内容不是有效 RSS/Atom XML") from exc
    root_name = _local_name(root.tag)
    if root_name not in {"rss", "feed", "rdf"}:
        raise RadarFeedError("unsupported_feed", "只支持 RSS/Atom Feed")

    container = root
    if root_name == "rss":
        channel = _first_child(root, {"channel"})
        if channel is not None:
            container = channel
    entries = [child for child in list(container) if _local_name(child.tag) in {"item", "entry"}]
    if not entries:
        raise RadarFeedError("empty_feed", "Feed 中没有可用条目")
    feed_title = _text(_first_child(container, {"title"}))
    items = []
    for entry in entries:
        title = _text(_first_child(entry, {"title"}))
        url = _link(entry)
        snippet_node = _first_child(entry, {"description", "summary", "content", "encoded"})
        snippet = _text(snippet_node)
        published = _text(_first_child(entry, {"pubdate", "published", "updated", "date"}))
        external_id = _text(_first_child(entry, {"guid", "id"})) or url or title
        if not title and not url:
            continue
        items.append({
            "title": title,
            "url": url,
            "snippet": snippet[:4000],
            "published": _date(published),
            "external_id": external_id[:500],
        })
    if not items:
        raise RadarFeedError("empty_feed", "Feed 条目缺少标题和链接")
    return {"feed_title": feed_title, "items": items}


def fetch_feed(endpoint: dict, state: dict | None = None, *, preview_limit: int = 50) -> dict:
    """执行一次条件请求，返回 success/unchanged 及规范化 Feed 条目。"""
    url = validate_endpoint_url(endpoint.get("url", ""))
    _check_resolved_target(url)
    state = state or {}
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.1",
    }
    if state.get("etag"):
        headers["If-None-Match"] = str(state["etag"])
    if state.get("last_modified"):
        headers["If-Modified-Since"] = str(state["last_modified"])
    try:
        response = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT, stream=True)
    except requests.RequestException as exc:
        raise RadarFeedError("network_error", f"请求信源失败: {str(exc)[:160]}") from exc
    try:
        if response.status_code == 304:
            return {
                "status": "unchanged", "items": [], "feed_title": "",
                "etag": response.headers.get("ETag", state.get("etag", "")),
                "last_modified": response.headers.get("Last-Modified", state.get("last_modified", "")),
                "http_status": 304,
            }
        if response.status_code < 200 or response.status_code >= 300:
            raise RadarFeedError("http_error", f"信源返回 HTTP {response.status_code}", http_status=response.status_code)
        content_type = str(response.headers.get("Content-Type") or "").lower()
        if content_type and not any(x in content_type for x in ("xml", "rss", "atom", "text/plain", "html")):
            raise RadarFeedError("unsupported_content_type", "信源返回的不是可解析文本/XML", http_status=response.status_code)
        chunks = []
        size = 0
        for chunk in response.iter_content(chunk_size=65536):
            if not chunk:
                continue
            size += len(chunk)
            if size > MAX_FEED_BYTES:
                raise RadarFeedError("feed_too_large", "Feed 超过 2MB 大小限制", http_status=response.status_code)
            chunks.append(chunk)
        parsed = parse_feed(b"".join(chunks))
        items = parsed["items"][:max(1, min(int(preview_limit), 100))]
        cursor = items[0].get("external_id") if items else ""
        return {
            "status": "success", "items": items, "feed_title": parsed["feed_title"],
            "cursor_after": cursor or "", "etag": response.headers.get("ETag", ""),
            "last_modified": response.headers.get("Last-Modified", ""),
            "http_status": response.status_code,
        }
    finally:
        response.close()

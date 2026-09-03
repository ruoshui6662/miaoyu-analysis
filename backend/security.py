"""妙舆安全基线：管理员令牌、请求限流和安全响应策略。"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
import threading
import time
from pathlib import Path

from flask import request

from config import DATA_DIR

LOGGER = logging.getLogger("miaoyu.security")
TOKEN_ENV = "MIAOYU_ADMIN_TOKEN"
TOKEN_FILE = DATA_DIR / "admin_token"
SESSION_COOKIE = "miaoyu_session"


def _valid_token(value: str) -> bool:
    return len(str(value or "").strip()) >= 24


def admin_token() -> str:
    """优先读环境变量；否则生成并持久化一次性管理员令牌。"""
    configured = os.getenv(TOKEN_ENV, "").strip()
    if _valid_token(configured):
        return configured
    try:
        existing = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if _valid_token(existing):
            return existing
    except OSError:
        pass
    generated = secrets.token_urlsafe(32)
    try:
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(generated + "\n", encoding="utf-8")
        try:
            os.chmod(TOKEN_FILE, 0o600)
        except OSError:
            pass
    except OSError as exc:
        LOGGER.error("无法持久化管理员令牌: %s", exc)
    # 只在首次生成时输出，便于容器首次启动完成初始化；绝不输出环境变量令牌。
    LOGGER.warning("首次启动已生成管理员令牌，请从本次启动日志复制并妥善保存；后续启动不再重复显示: %s", generated)
    return generated


def request_token() -> str:
    authorization = request.headers.get("Authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    header = request.headers.get("X-Admin-Token", "").strip()
    if header:
        return header
    return request.cookies.get(SESSION_COOKIE, "").strip()


def authorized() -> bool:
    return verify_admin_token(request_token())


def verify_admin_token(value: str) -> bool:
    """校验用户提交的管理员令牌，不暴露服务端令牌内容。"""
    supplied = str(value or "").strip()
    expected = admin_token()
    if not supplied or not expected:
        return False
    return secrets.compare_digest(supplied, expected)


def attach_session_cookie(response):
    """令牌请求成功后转为 HttpOnly 会话，便于下载链接等非 fetch 请求复用。"""
    supplied = request.headers.get("Authorization", "")
    if supplied.lower().startswith("bearer ") and secrets.compare_digest(
        supplied[7:].strip(), admin_token()
    ):
        response.set_cookie(
            SESSION_COOKIE, supplied[7:].strip(), max_age=86400,
            httponly=True, samesite="Lax", secure=bool(request.is_secure),
        )
    return response


class FixedWindowLimiter:
    def __init__(self, limit: int = 120, window_seconds: int = 60):
        self.limit = max(1, int(limit))
        self.window_seconds = max(1, int(window_seconds))
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[float, int]] = {}

    def allow(self, key: str, *, limit: int | None = None) -> tuple[bool, int]:
        now = time.monotonic()
        max_requests = max(1, int(limit or self.limit))
        with self._lock:
            start, count = self._buckets.get(key, (now, 0))
            if now - start >= self.window_seconds:
                start, count = now, 0
            count += 1
            self._buckets[key] = (start, count)
            # 限制内存增长：只保留当前窗口内的桶。
            if len(self._buckets) > 4096:
                self._buckets = {
                    k: v for k, v in self._buckets.items()
                    if now - v[0] < self.window_seconds
                }
            if count > max_requests:
                retry = max(1, int(self.window_seconds - (now - start)) + 1)
                return False, retry
            return True, 0


def client_key() -> str:
    # 不信任 X-Forwarded-For，除非未来在受信反向代理层显式配置解析规则。
    raw = request.remote_addr or "unknown"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


limiter = FixedWindowLimiter()

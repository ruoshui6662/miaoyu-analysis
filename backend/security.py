"""妙舆安全基线：管理员令牌、请求限流和安全响应策略。"""
from __future__ import annotations

import hashlib
import json
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
ACCOUNT_FILE = DATA_DIR / "admin_account.json"
SESSION_COOKIE = "miaoyu_session"
MIN_TOKEN_LENGTH = 6
MIN_PASSWORD_LENGTH = 8
DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "password"

_ACCOUNT_LOCK = threading.RLock()
_SESSIONS_LOCK = threading.Lock()
_SESSIONS: dict[str, tuple[str, float]] = {}


def _valid_token(value: str) -> bool:
    return len(str(value or "").strip()) >= MIN_TOKEN_LENGTH


def _password_hash(password: str, salt: bytes | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", str(password).encode("utf-8"), salt, 240_000
    )
    return salt.hex(), digest.hex()


def _read_account() -> dict:
    try:
        data = json.loads(ACCOUNT_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("username") == DEFAULT_ADMIN_USERNAME \
                and data.get("salt") and data.get("password_hash"):
            return data
    except (OSError, ValueError, TypeError):
        pass
    return {}


def _write_account(account: dict) -> None:
    ACCOUNT_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = ACCOUNT_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(account, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(temp, 0o600)
    except OSError:
        pass
    temp.replace(ACCOUNT_FILE)


def admin_account() -> dict:
    """读取或初始化站内管理员账户；密码只以 PBKDF2 哈希形式落盘。"""
    with _ACCOUNT_LOCK:
        account = _read_account()
        if account:
            return account
        salt, digest = _password_hash(DEFAULT_ADMIN_PASSWORD)
        account = {
            "username": DEFAULT_ADMIN_USERNAME,
            "salt": salt,
            "password_hash": digest,
            "must_change_password": True,
            "updated_at": int(time.time()),
        }
        try:
            _write_account(account)
        except OSError as exc:
            LOGGER.error("无法持久化管理员账户: %s", exc)
        return account


def verify_admin_password(username: str, password: str) -> bool:
    account = admin_account()
    supplied_user = str(username or "").strip()
    supplied_password = str(password or "")
    if supplied_user != DEFAULT_ADMIN_USERNAME or not supplied_password:
        return False
    try:
        _, digest = _password_hash(supplied_password, bytes.fromhex(account["salt"]))
    except (KeyError, ValueError):
        return False
    return secrets.compare_digest(digest, str(account.get("password_hash") or ""))


def password_must_change() -> bool:
    return bool(admin_account().get("must_change_password", True))


def set_admin_password(current_password: str, new_password: str) -> bool:
    """校验旧密码并更新密码；成功后所有既有会话失效。"""
    if len(str(new_password or "")) < MIN_PASSWORD_LENGTH:
        return False
    if not verify_admin_password(DEFAULT_ADMIN_USERNAME, current_password):
        return False
    with _ACCOUNT_LOCK:
        account = admin_account()
        salt, digest = _password_hash(new_password)
        account.update({
            "salt": salt,
            "password_hash": digest,
            "must_change_password": False,
            "updated_at": int(time.time()),
        })
        try:
            _write_account(account)
        except OSError as exc:
            LOGGER.error("无法保存管理员密码: %s", exc)
            return False
    with _SESSIONS_LOCK:
        _SESSIONS.clear()
    return True


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
    return ""


def _session_user(value: str) -> str:
    sid = str(value or "").strip()
    if not sid:
        return ""
    with _SESSIONS_LOCK:
        session = _SESSIONS.get(sid)
        if not session:
            return ""
        username, expires_at = session
        if expires_at <= time.time():
            _SESSIONS.pop(sid, None)
            return ""
        return username


def create_session(username: str = DEFAULT_ADMIN_USERNAME) -> str:
    sid = secrets.token_urlsafe(32)
    with _SESSIONS_LOCK:
        _SESSIONS[sid] = (username, time.time() + 86400)
        if len(_SESSIONS) > 4096:
            now = time.time()
            _SESSIONS.update({k: v for k, v in _SESSIONS.items() if v[1] > now})
    return sid


def revoke_session(value: str) -> None:
    with _SESSIONS_LOCK:
        _SESSIONS.pop(str(value or "").strip(), None)


def authenticated_username() -> str:
    username = _session_user(request.cookies.get(SESSION_COOKIE, ""))
    if username:
        return username
    # 兼容既有 Bearer/X-Admin-Token 自动化调用；网页会话不会走这里。
    return DEFAULT_ADMIN_USERNAME if verify_admin_token(request_token()) else ""


def authorized() -> bool:
    return bool(authenticated_username())


def verify_admin_token(value: str) -> bool:
    """校验用户提交的管理员令牌，不暴露服务端令牌内容。"""
    supplied = str(value or "").strip()
    expected = admin_token()
    if not supplied or not expected:
        return False
    return secrets.compare_digest(supplied, expected)


def attach_session_cookie(response):
    """兼容令牌请求，并将其转换为随机 HttpOnly 会话。"""
    supplied = request.headers.get("Authorization", "")
    if supplied.lower().startswith("bearer ") and secrets.compare_digest(
        supplied[7:].strip(), admin_token()
    ):
        sid = create_session()
        response.set_cookie(
            SESSION_COOKIE, sid, max_age=86400,
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

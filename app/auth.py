import hmac
import secrets
import threading
import time

from fastapi import Header, HTTPException

from .config import ADMIN_PASSWORD, TOKEN_TTL_SECONDS


_sessions: dict[str, float] = {}
_lock = threading.Lock()
_MAX_SESSIONS = 256


def check_password(pw: str) -> bool:
    return bool(ADMIN_PASSWORD) and hmac.compare_digest(pw.encode(), ADMIN_PASSWORD.encode())


def issue_token() -> str:
    with _lock:
        now = time.monotonic()
        for token, expiry in list(_sessions.items()):
            if expiry <= now:
                _sessions.pop(token)
        if len(_sessions) >= _MAX_SESSIONS:
            _sessions.pop(next(iter(_sessions)))
        token = secrets.token_urlsafe(32)
        _sessions[token] = now + TOKEN_TTL_SECONDS
        return token


def session_remaining(token: str) -> float:
    with _lock:
        remaining = _sessions.get(token, 0) - time.monotonic()
        if remaining <= 0:
            _sessions.pop(token, None)
        return max(0, remaining)


def verify_token(token: str) -> bool:
    return session_remaining(token) > 0


def revoke_token(token: str):
    with _lock:
        _sessions.pop(token, None)


def bearer(authorization: str) -> str:
    scheme, _, token = authorization.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def require_admin(authorization: str = Header(default="")) -> str:
    token = bearer(authorization)
    if not verify_token(token):
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    return token

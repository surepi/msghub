import base64
import hashlib
import hmac
import time

from fastapi import Header, HTTPException

from .config import ADMIN_PASSWORD, SECRET_KEY, TOKEN_TTL_SECONDS


def _sign(payload: str) -> str:
    return hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()


def check_password(pw: str) -> bool:
    return bool(ADMIN_PASSWORD) and hmac.compare_digest(pw.encode(), ADMIN_PASSWORD.encode())


def issue_token() -> str:
    exp = str(int(time.time()) + TOKEN_TTL_SECONDS)
    raw = f"admin.{exp}"
    return base64.urlsafe_b64encode(f"{raw}.{_sign(raw)}".encode()).decode()


def verify_token(token: str) -> bool:
    try:
        sub, exp, sig = base64.urlsafe_b64decode(token.encode()).decode().split(".")
    except Exception:
        return False
    if not hmac.compare_digest(sig, _sign(f"{sub}.{exp}")):
        return False
    return int(exp) > time.time()


def require_admin(authorization: str = Header(default="")):
    token = authorization.removeprefix("Bearer ").strip()
    if not token or not verify_token(token):
        raise HTTPException(status_code=401, detail="未登录或登录已过期")

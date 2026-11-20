"""留言接口与对话接口共用的小工具。"""
import json
import re
import threading
import time
from collections import OrderedDict, deque
from datetime import datetime, timedelta
from urllib.parse import urlsplit

DEFAULT_WELCOME = "您好，请问有什么可以帮您？"
DEFAULT_OFFLINE = "客服当前不在线，您可以先留言，我们上班后会尽快通过您留下的联系方式联系您。"

_PHONE = re.compile(r"^\+?[\d\- ()]{7,20}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


def valid_contact(s: str) -> bool:
    """联系方式：电话（至少 7 位数字）或邮箱。"""
    s = s.strip()
    if _EMAIL.match(s):
        return True
    return bool(_PHONE.match(s)) and sum(c.isdigit() for c in s) >= 7


def parse_work_hours(raw: str) -> dict | None:
    """{"days":[1..7], "start":"09:00", "end":"18:00"}；空或无效 = 不限制（全天）。周一 = 1。"""
    if not raw:
        return None
    try:
        wh = json.loads(raw)
        days = [int(d) for d in wh.get("days", []) if 1 <= int(d) <= 7]
        start, end = str(wh["start"]), str(wh["end"])
        if not (re.match(r"^\d{2}:\d{2}$", start) and re.match(r"^\d{2}:\d{2}$", end)):
            return None
        return {"days": days, "start": start, "end": end}
    except Exception:
        return None


def in_work_hours(raw: str, now: datetime | None = None) -> bool:
    wh = parse_work_hours(raw)
    if not wh:
        return True
    now = now or datetime.now()
    days, start, end = wh["days"], wh["start"], wh["end"]
    hm = now.strftime("%H:%M")
    if start <= end:
        return now.isoweekday() in days and start <= hm < end
    if hm >= start:
        return now.isoweekday() in days
    return hm < end and (now - timedelta(days=1)).isoweekday() in days


class RateLimiter:
    """滑动窗口限流：每个 key 在 window 秒内最多 limit 次。仅单进程内有效。"""

    def __init__(self, limit: int, window: float = 60, max_keys: int = 10000):
        self.limit = limit
        self.window = window
        self.max_keys = max_keys
        self._hits: OrderedDict[str, deque] = OrderedDict()
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        with self._lock:
            now = time.monotonic()
            cutoff = now - self.window
            while self._hits and next(iter(self._hits.values()))[-1] <= cutoff:
                self._hits.popitem(last=False)
            q = self._hits.get(key)
            if q is None:
                if len(self._hits) >= self.max_keys or self.limit <= 0:
                    return False
                q = self._hits[key] = deque()
            while q and q[0] <= cutoff:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            self._hits.move_to_end(key)
            return True


def origin_allowed(site: dict, origin: str, headers=None) -> bool:
    """站点配置了允许来源时，校验请求的 Origin；没配置或请求没带 Origin 则放行。
    MsgHub 自己的页面（如后台的挂件预览页）始终放行：传入 headers 时比较 Origin 与 Host。"""
    allowed = [o.strip().rstrip("/") for o in site["allowed_origins"].split(",") if o.strip()]
    if not allowed or not origin:
        return True
    if origin.rstrip("/") in allowed:
        return True
    if headers is not None:
        host = origin.split("://", 1)[-1].rstrip("/")
        return host == headers.get("host", "")
    return False


def http_origin(url: str) -> tuple[str, str, int] | None:
    if "\\" in url or any(ord(c) <= 32 or ord(c) == 127 for c in url):
        return None
    try:
        parts = urlsplit(url)
        if (parts.scheme in {"http", "https"} and parts.hostname and parts.hostname.isascii()
                and parts.username is None):
            host = parts.hostname
            port = parts.port
            if port != 0:
                return parts.scheme, host, port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        pass
    return None

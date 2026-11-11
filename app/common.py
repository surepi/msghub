"""留言接口与对话接口共用的小工具。"""
import json
import re
import threading
import time
from collections import defaultdict, deque
from datetime import datetime

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
    if now.isoweekday() not in wh["days"]:
        return False
    hm = now.strftime("%H:%M")
    if wh["start"] <= wh["end"]:
        return wh["start"] <= hm < wh["end"]
    return hm >= wh["start"] or hm < wh["end"]   # 跨午夜，如 20:00–02:00


class RateLimiter:
    """滑动窗口限流：每个 key 在 window 秒内最多 limit 次。仅单进程内有效。"""

    def __init__(self, limit: int, window: float = 60):
        self.limit = limit
        self.window = window
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            if len(self._hits) > 10000:  # 防止 key 无限增长
                for k in [k for k, v in self._hits.items() if not v]:
                    del self._hits[k]
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
        return host in {headers.get("host", ""), headers.get("x-forwarded-host", "")}
    return False

"""留言接口与对话接口共用的小工具。"""
import threading
import time
from collections import defaultdict, deque


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


def origin_allowed(site: dict, origin: str) -> bool:
    """站点配置了允许来源时，校验请求的 Origin；没配置或请求没带 Origin 则放行。"""
    allowed = [o.strip().rstrip("/") for o in site["allowed_origins"].split(",") if o.strip()]
    if not allowed or not origin:
        return True
    return origin.rstrip("/") in allowed

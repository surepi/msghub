from collections import Counter

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from .common import RateLimiter
from .media import MAX_IMAGE_BYTES


class IngressLimits:
    def __init__(self, app):
        self.app = app
        self.connections = Counter()
        self.handshakes = RateLimiter(60)
        self.entry_rates = {
            "/api/public/messages": RateLimiter(60),
            "/api/chat/upload": RateLimiter(30),
            "/api/admin/upload": RateLimiter(30),
        }

    async def __call__(self, scope, receive, send):
        peer = scope.get("client")
        ip = peer[0] if peer else ""
        if scope["type"] == "websocket":
            if (self.connections.total() >= 512 or self.connections[ip] >= 16
                    or not self.handshakes.allow(ip)):
                await send({"type": "websocket.close", "code": 1013})
                return
            self.connections[ip] += 1
            try:
                await self.app(scope, receive, send)
            finally:
                self.connections[ip] -= 1
                if not self.connections[ip]:
                    del self.connections[ip]
            return

        if scope["type"] != "http" or scope.get("method") not in {"POST", "PUT", "PATCH"}:
            await self.app(scope, receive, send)
            return

        path = scope["path"].rstrip("/")
        if path in {"/api/chat/upload", "/api/admin/upload"}:
            limit = MAX_IMAGE_BYTES + 64 * 1024
        elif path == "/api/admin/login":
            limit = 4096
        else:
            limit = 256 * 1024

        headers = dict(scope["headers"])
        response_headers = {}
        if (origin := headers.get(b"origin")) and path in {"/api/chat/upload", "/api/public/messages"}:
            response_headers = {"Access-Control-Allow-Origin": origin.decode("latin1"), "Vary": "Origin"}
        if (rate := self.entry_rates.get(path)) and not rate.allow(ip):
            await JSONResponse({"detail": "请求过于频繁，请稍后再试"}, status_code=429,
                               headers={**response_headers, "Retry-After": "60"})(scope, receive, send)
            return
        detail = "请求体超过大小限制"
        if (length := headers.get(b"content-length", b"")).isdigit() and int(length) > limit:
            await JSONResponse({"detail": detail}, status_code=413, headers=response_headers)(scope, receive, send)
            return

        size = 0

        async def bounded_receive():
            nonlocal size
            message = await receive()
            if message["type"] == "http.request":
                size += len(message.get("body", b""))
                if size > limit:
                    raise HTTPException(413, detail, response_headers)
            return message

        await self.app(scope, bounded_receive, send)

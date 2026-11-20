"""图片上传、会话归属和短期下载链接。"""
import asyncio
import hmac
import re
import secrets
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Path as PathParam, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from . import auth
from .common import RateLimiter, origin_allowed
from .config import DATA_DIR, SECRET_KEY
from .db import get_db

UPLOAD_DIR = DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
URL_PREFIX = "/uploads/"
MAX_IMAGE_BYTES = 5 * 1024 * 1024
_IMAGE_NAME = re.compile(r"[0-9a-f]{32}\.(?:png|jpg|gif|webp)")
_LINK_TTL = 600
_PENDING_TTL = 1800
_CLEANUP_AGE = 24 * 3600

router = APIRouter()
_upload_by_ip = RateLimiter(10)

# 文件头 → 扩展名
_MAGIC = [
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
]


def sniff(data: bytes) -> str | None:
    for head, ext in _MAGIC:
        if data.startswith(head):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def save_image(data: bytes) -> str:
    """校验并保存图片，返回站内路径 /uploads/xxx.ext。"""
    if not data:
        raise ValueError("文件为空")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("图片不能超过 5MB")
    ext = sniff(data)
    if not ext:
        raise ValueError("只支持 PNG / JPG / GIF / WebP 图片")
    name = f"{secrets.token_hex(16)}.{ext}"
    (UPLOAD_DIR / name).write_bytes(data)
    return URL_PREFIX + name


def local_path(url: str) -> Path | None:
    """站内图片路径 → 本地文件；不是本站上传的图片返回 None。"""
    if not url.startswith(URL_PREFIX):
        return None
    name = url[len(URL_PREFIX):]
    if not _IMAGE_NAME.fullmatch(name):
        return None
    p = UPLOAD_DIR / name
    return p if not p.is_symlink() and p.is_file() else None


def _signature(message_id: int, expires: int) -> str:
    return hmac.digest(SECRET_KEY.encode(), f"media:{message_id}:{expires}".encode(), "sha256").hex()


def image_url(message_id: int) -> str:
    expires = int(time.time()) + _LINK_TTL
    return f"/api/media/{message_id}?expires={expires}&signature={_signature(message_id, expires)}"


@router.get("/api/media/{message_id}", include_in_schema=False)
def read_image(message_id: int = PathParam(gt=0, le=9223372036854775807), *,
               expires: int, signature: str):
    if (not re.fullmatch(r"[0-9a-f]{64}", signature) or expires <= time.time()
            or not hmac.compare_digest(signature, _signature(message_id, expires))):
        raise HTTPException(404)
    with get_db() as db:
        message = db.execute("SELECT content FROM chat_messages WHERE id=? AND msg_type='image'",
                             (message_id,)).fetchone()
    path = local_path(message["content"]) if message else None
    if path is None:
        raise HTTPException(404)
    return FileResponse(path, headers={
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
    })


def admin_owner(token: str) -> str:
    return "admin:" + hmac.digest(SECRET_KEY.encode(), token.encode(), "sha256").hex()


def claim(db, url: str, owner: str) -> bool:
    return bool(local_path(url) and db.execute(
        "DELETE FROM pending_uploads WHERE path=? AND owner=? AND expires_at>?",
        (url, owner, time.time()),
    ).rowcount)


def stage_image(data: bytes, owner: str) -> str:
    url = save_image(data)
    try:
        with get_db() as db:
            db.execute("INSERT INTO pending_uploads (path,owner,expires_at) VALUES (?,?,?)",
                       (url, owner, time.time() + _PENDING_TTL))
    except Exception:
        (UPLOAD_DIR / url.removeprefix(URL_PREFIX)).unlink(missing_ok=True)
        raise
    return url


def collect_uploads():
    now = time.time()
    with get_db() as db:
        db.execute("DELETE FROM pending_uploads WHERE expires_at<=?", (now,))
        referenced = {row[0] for row in db.execute(
            "SELECT content FROM chat_messages WHERE msg_type='image' UNION SELECT path FROM pending_uploads"
        ).fetchall()}
    for path in UPLOAD_DIR.iterdir():
        if (_IMAGE_NAME.fullmatch(path.name) and not path.is_symlink()
                and URL_PREFIX + path.name not in referenced and path.is_file()
                and path.stat().st_mtime < now - _CLEANUP_AGE):
            path.unlink(missing_ok=True)


async def cleanup_loop():
    while True:
        await asyncio.sleep(3600)
        try:
            await asyncio.to_thread(collect_uploads)
        except Exception as error:
            print(f"[media] 清理失败：{type(error).__name__}", flush=True)


async def _read_limited(file: UploadFile) -> bytes:
    data = await file.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(413, "图片不能超过 5MB")
    return data


@router.post("/api/chat/upload", tags=["对话"])
async def visitor_upload(request: Request, file: UploadFile, site_key: str = Form(...), token: str = Form(...)):
    """访客上传图片（需已有访客身份）。返回 {"url": "/uploads/.."}，再通过 WebSocket 以 msg_type=image 发送。
    挂件在客户网站上跨域调用，所有响应（含错误）都带 CORS 头，否则挂件读不到错误信息。"""
    origin = request.headers.get("origin", "")
    cors = {"Access-Control-Allow-Origin": origin or "*", "Vary": "Origin"}

    def reply(status: int, body: dict):
        return JSONResponse(body, status_code=status, headers=cors)

    ip = request.client.host if request.client else ""
    if not _upload_by_ip.allow(ip):
        return reply(429, {"error": "上传过于频繁，请稍后再试"})
    with get_db() as db:
        site = db.execute("SELECT * FROM sites WHERE site_key = ?", (site_key,)).fetchone()
        visitor = db.execute("SELECT id FROM visitors WHERE token = ? AND site_id = ?",
                             (token, site["id"] if site else None)).fetchone()
    if not site or not site["enabled"] or not origin_allowed(dict(site), origin, request.headers):
        return reply(403, {"error": "站点不存在或来源不被允许"})
    if not visitor:
        return reply(403, {"error": "请先开始对话"})
    data = await file.read(MAX_IMAGE_BYTES + 1)
    try:
        return reply(200, {"url": stage_image(data, f"visitor:{visitor['id']}")})
    except ValueError as e:
        return reply(400, {"error": str(e)})


@router.post("/api/admin/upload", tags=["对话"])
async def admin_upload(file: UploadFile, token: str = Depends(auth.require_admin)):
    try:
        return {"url": stage_image(await _read_limited(file), admin_owner(token))}
    except ValueError as e:
        raise HTTPException(400, str(e))

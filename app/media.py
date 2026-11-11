"""图片上传与存储。文件存在 DATA_DIR/uploads，以随机文件名公开访问（/uploads/<名字>）。"""
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from . import auth
from .common import RateLimiter, origin_allowed
from .config import DATA_DIR
from .db import get_db

UPLOAD_DIR = DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
URL_PREFIX = "/uploads/"
MAX_IMAGE_BYTES = 5 * 1024 * 1024

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
    if "/" in name or ".." in name or not name:
        return None
    p = UPLOAD_DIR / name
    return p if p.is_file() else None


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
        visitor = db.execute("SELECT id FROM visitors WHERE token = ?", (token,)).fetchone()
    if not site or not site["enabled"] or not origin_allowed(dict(site), origin):
        return reply(403, {"error": "站点不存在或来源不被允许"})
    if not visitor:
        return reply(403, {"error": "请先开始对话"})
    data = await file.read(MAX_IMAGE_BYTES + 1)
    try:
        return reply(200, {"url": save_image(data)})
    except ValueError as e:
        return reply(400, {"error": str(e)})


@router.post("/api/admin/upload", tags=["对话"], dependencies=[Depends(auth.require_admin)])
async def admin_upload(file: UploadFile):
    try:
        return {"url": save_image(await _read_limited(file))}
    except ValueError as e:
        raise HTTPException(400, str(e))

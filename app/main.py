import asyncio
import csv
import io
import json
import os
import secrets
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, auto, chat, media, qq, qq_delivery, summaries, triage
from .common import RateLimiter, origin_allowed, parse_work_hours
from .config import MAX_CONTACT, MAX_CONTENT, MAX_NAME, RATE_LIMIT_PER_MIN
from .db import ADMIN_MESSAGE_SQL, STATUSES, get_db, init_db, rows

STATIC = Path(__file__).parent / "static"

app = FastAPI(title="MsgHub 留言汇", version="0.1.0", docs_url="/api/docs", redoc_url=None)


@app.middleware("http")
async def cache_headers(request: Request, call_next):
    """页面和脚本每次都向服务器确认是否更新（未变化时返回 304），避免 CDN / 浏览器在部署后继续用旧版本；
    上传的图片文件名随机且内容不变，长期缓存。"""
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/uploads/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif path in ("/", "/embed.js", "/favicon.ico") or path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.mount("/uploads", StaticFiles(directory=media.UPLOAD_DIR), name="uploads")
app.include_router(chat.router)
app.include_router(media.router)
app.include_router(qq.router)
app.include_router(qq_delivery.router)
app.include_router(auto.router)
app.include_router(summaries.router)
app.include_router(triage.router)


@app.on_event("startup")
async def _startup():
    init_db()
    qq.setup()     # 先注册 QQ 转发，再注册 AI / 转留言
    auto.setup()
    triage.setup()


@app.get("/api/health")
def health():
    return {"ok": True}


_readiness_check: asyncio.Task[bool] | None = None


def _database_reachable() -> bool:
    try:
        with get_db() as db:
            db.execute("SELECT 1").fetchone()
    except Exception:
        return False
    return True


@app.get("/api/ready", include_in_schema=False)
async def readiness():
    global _readiness_check
    if _readiness_check is None or _readiness_check.done():
        _readiness_check = asyncio.create_task(asyncio.to_thread(_database_reachable))
    try:
        # Let a timed-out check finish without starting more database connections.
        ready = await asyncio.wait_for(asyncio.shield(_readiness_check), timeout=2)
    except TimeoutError:
        ready = False
    return JSONResponse({"ok": ready}, status_code=200 if ready else 503,
                        headers={"Cache-Control": "no-store"})


def _file(request: Request, path: Path, media_type: str | None = None) -> Response:
    """带 ETag 的文件响应；浏览器带着相同 ETag 来确认时返回 304，不重复传输内容。"""
    resp = FileResponse(path, media_type=media_type, stat_result=os.stat(path))
    etag = resp.headers.get("etag")
    if etag and request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"etag": etag})
    return resp


@app.get("/", include_in_schema=False)
def index(request: Request):
    return _file(request, STATIC / "index.html")


@app.get("/embed.js", include_in_schema=False)
def embed_js(request: Request):
    return _file(request, STATIC / "embed.js", "application/javascript")


@app.get("/favicon.ico", include_in_schema=False)
def favicon(request: Request):
    return _file(request, STATIC / "favicon.ico", "image/vnd.microsoft.icon")


# ---------------------------------------------------------------------------
# 公开接口：供网站提交留言
# ---------------------------------------------------------------------------

_submit_limiter = RateLimiter(RATE_LIMIT_PER_MIN)


def _client_ip(req: Request) -> str:
    return req.client.host if req.client else ""


def _cors_headers(origin: str) -> dict:
    return {
        "Access-Control-Allow-Origin": origin or "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, X-Site-Key",
        "Access-Control-Max-Age": "86400",
        "Vary": "Origin",
    }


def _push_webhook(url: str, payload: dict):
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "MsgHub-Webhook/0.1"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=8).close()
    except Exception as e:  # noqa: BLE001
        print(f"[webhook] 推送失败 {url}: {e}")


@app.options("/api/public/messages", include_in_schema=False)
def public_preflight(request: Request):
    return Response(status_code=204, headers=_cors_headers(request.headers.get("origin", "")))


@app.post("/api/public/messages", tags=["公开接口"])
async def public_submit(request: Request, bg: BackgroundTasks):
    """提交留言。支持 JSON、表单（urlencoded / multipart）。

    字段：site_key（或请求头 X-Site-Key）、name、contact、content、page_url、
    _redirect（表单提交成功后跳转地址，可选）、_hp（蜜罐字段，必须留空）。
    """
    origin = request.headers.get("origin", "")
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            data = await request.json()
        except Exception:
            data = None
        if not isinstance(data, dict):
            raise HTTPException(400, "JSON 格式错误")
    else:
        data = dict(await request.form())

    def fail(code: int, msg: str):
        return JSONResponse({"ok": False, "error": msg}, status_code=code, headers=_cors_headers(origin))

    def s(key: str) -> str:
        v = data.get(key, "")
        return v.strip() if isinstance(v, str) else ""

    site_key = request.headers.get("x-site-key", "") or s("site_key")
    if not site_key:
        return fail(400, "缺少 site_key")

    with get_db() as db:
        site = db.execute("SELECT * FROM sites WHERE site_key = ?", (site_key,)).fetchone()
    if not site or not site["enabled"]:
        return fail(403, "站点不存在或已停用")
    site = dict(site)
    if not origin_allowed(site, origin, request.headers):
        return fail(403, "来源域名不被允许")

    redirect = s("_redirect")
    if s("_hp"):  # 蜜罐被填写 -> 机器人，静默成功
        return RedirectResponse(redirect, 303) if redirect else JSONResponse({"ok": True}, headers=_cors_headers(origin))

    ip = _client_ip(request)
    if not _submit_limiter.allow(ip):
        return fail(429, "提交过于频繁，请稍后再试")

    content = s("content")
    if not content:
        return fail(400, "留言内容不能为空")
    if len(content) > MAX_CONTENT:
        return fail(400, f"留言内容不能超过 {MAX_CONTENT} 字")

    msg = {
        "site_id": site["id"],
        "name": s("name")[:MAX_NAME],
        "contact": s("contact")[:MAX_CONTACT],
        "content": content,
        "page_url": (s("page_url") or request.headers.get("referer", ""))[:500],
        "ip": ip,
        "user_agent": request.headers.get("user-agent", "")[:300],
    }
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO messages (site_id,name,contact,content,page_url,ip,user_agent) "
            "VALUES (:site_id,:name,:contact,:content,:page_url,:ip,:user_agent)",
            msg,
        )
        msg["id"] = cur.lastrowid

    await chat.hub.to_admins({"type": "inbox_changed", "conversation_ids": []})

    if site["webhook_url"]:
        bg.add_task(_push_webhook, site["webhook_url"], {"event": "message.created", "site": site["name"], **msg})

    if redirect and urlparse(redirect).scheme in ("http", "https"):
        return RedirectResponse(redirect, 303)
    return JSONResponse({"ok": True, "id": msg["id"]}, headers=_cors_headers(origin))


# ---------------------------------------------------------------------------
# 管理接口
# ---------------------------------------------------------------------------

class LoginIn(BaseModel):
    password: str


@app.post("/api/admin/login", tags=["管理"])
def login(body: LoginIn):
    if not auth.check_password(body.password):
        time.sleep(1)
        raise HTTPException(401, "密码错误")
    return {"token": auth.issue_token()}


admin = Depends(auth.require_admin)


@app.get("/api/admin/stats", tags=["管理"], dependencies=[admin])
def stats():
    with get_db() as db:
        by_status = {r["status"]: r["c"] for r in db.execute("SELECT status, COUNT(*) c FROM messages GROUP BY status")}
        today = db.execute("SELECT COUNT(*) FROM messages WHERE date(created_at) = date('now','localtime')").fetchone()[0]
        sites = db.execute("SELECT COUNT(*) FROM sites").fetchone()[0]
        conv_open, conv_waiting, conv_unread = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(status = 'waiting'), 0), COALESCE(SUM(unread > 0), 0) "
            "FROM conversations WHERE status != 'closed'"
        ).fetchone()
        trend = rows(db.execute(
            "SELECT date(created_at) d, COUNT(*) c FROM messages "
            "WHERE created_at >= date('now','localtime','-13 days') GROUP BY d ORDER BY d"
        ))
    return {
        "total": sum(by_status.values()),
        "today": today,
        "sites": sites,
        "conv_open": conv_open,
        "conv_waiting": conv_waiting,
        "conv_unread": conv_unread,
        "by_status": {k: by_status.get(k, 0) for k in STATUSES},
        "trend": trend,
    }


# ---- 站点 ----

class SiteIn(BaseModel):
    name: str
    domain: str = ""
    allowed_origins: str = ""
    webhook_url: str = ""
    enabled: bool = True
    welcome: str = ""
    offline_text: str = ""
    work_hours: dict | None = None     # {"days":[1..7],"start":"09:00","end":"18:00"}；None = 全天
    require_contact: bool = True
    ai_enabled: bool = True
    crawl_urls: str = ""


SITE_FIELDS = ("name", "domain", "allowed_origins", "webhook_url", "enabled", "welcome", "offline_text",
               "work_hours", "require_contact", "ai_enabled", "crawl_urls")


def _site_values(body: SiteIn) -> list:
    if not body.name.strip():
        raise HTTPException(400, "站点名称不能为空")
    wh = ""
    if body.work_hours:
        wh = json.dumps(body.work_hours, ensure_ascii=False)
        if not parse_work_hours(wh) or not parse_work_hours(wh)["days"]:
            raise HTTPException(400, "工作时间格式不正确：至少选一天，时间为 HH:MM")
        wh = json.dumps(parse_work_hours(wh), ensure_ascii=False)
    return [body.name.strip(), body.domain.strip(), body.allowed_origins.strip(), body.webhook_url.strip(),
            int(body.enabled), body.welcome.strip()[:500], body.offline_text.strip()[:500], wh,
            int(body.require_contact), int(body.ai_enabled), body.crawl_urls.strip()[:2000]]


@app.get("/api/admin/sites", tags=["站点"], dependencies=[admin])
def list_sites():
    with get_db() as db:
        return rows(db.execute(
            "SELECT s.*, "
            " (SELECT COUNT(*) FROM messages m WHERE m.site_id = s.id) AS total, "
            " (SELECT COUNT(*) FROM messages m WHERE m.site_id = s.id AND m.status = 'new') AS unread "
            "FROM sites s ORDER BY s.id DESC"
        ))


@app.post("/api/admin/sites", tags=["站点"], dependencies=[admin])
def create_site(body: SiteIn):
    values = _site_values(body)
    key = "sk_" + secrets.token_urlsafe(24)
    with get_db() as db:
        cur = db.execute(
            f"INSERT INTO sites (site_key, {', '.join(SITE_FIELDS)}) VALUES (?{', ?' * len(SITE_FIELDS)})",
            [key, *values],
        )
        return dict(db.execute("SELECT * FROM sites WHERE id = ?", (cur.lastrowid,)).fetchone())


@app.put("/api/admin/sites/{site_id}", tags=["站点"], dependencies=[admin])
async def update_site(site_id: int, body: SiteIn):
    values = _site_values(body)
    with get_db() as db:
        cur = db.execute(
            f"UPDATE sites SET {', '.join(f + '=?' for f in SITE_FIELDS)} WHERE id=?",
            [*values, site_id],
        )
        if not cur.rowcount:
            raise HTTPException(404, "站点不存在")
        site = dict(db.execute("SELECT site_key, enabled, allowed_origins FROM sites WHERE id=?",
                               (site_id,)).fetchone())
    await chat.hub.revoke_site_visitors(site_id, site)
    return {"ok": True}


@app.post("/api/admin/sites/{site_id}/rotate-key", tags=["站点"], dependencies=[admin])
async def rotate_key(site_id: int):
    key = "sk_" + secrets.token_urlsafe(24)
    with get_db() as db:
        if not db.execute("UPDATE sites SET site_key=? WHERE id=?", (key, site_id)).rowcount:
            raise HTTPException(404, "站点不存在")
    await chat.hub.revoke_site_visitors(site_id)
    return {"site_key": key}


@app.delete("/api/admin/sites/{site_id}", tags=["站点"], dependencies=[admin])
async def delete_site(site_id: int):
    with get_db() as db:
        db.execute("DELETE FROM sites WHERE id=?", (site_id,))
    await chat.hub.revoke_site_visitors(site_id)
    return {"ok": True}


# ---- 留言 ----

def _msg_filter(site_id: int | None, status: str | None, q: str | None, *, before_id: int | None = None):
    where, args = [], []
    if site_id:
        where.append("m.site_id = ?"); args.append(site_id)
    if status:
        where.append("m.status = ?"); args.append(status)
    if q:
        where.append("(m.content LIKE ? OR m.name LIKE ? OR m.contact LIKE ?)")
        args += [f"%{q}%"] * 3
    if before_id is not None:
        where.append("m.id < ?")
        args.append(before_id)
    return ("WHERE " + " AND ".join(where)) if where else "", args


@app.get("/api/admin/messages", tags=["留言"], dependencies=[admin])
def list_messages(site_id: int | None = None, status: str | None = None, q: str | None = None,
                  page: int = 1, size: int = 20):
    size = max(1, min(size, 100))
    page = max(1, page)
    where, args = _msg_filter(site_id, status, q)
    with get_db() as db:
        total = db.execute(f"SELECT COUNT(*) FROM messages m {where}", args).fetchone()[0]
        items = rows(db.execute(
            ADMIN_MESSAGE_SQL + f"{where} "
            f"ORDER BY m.id DESC LIMIT ? OFFSET ?",
            [*args, size, (page - 1) * size],
        ))
    return {"total": total, "page": page, "size": size, "items": items}


class MsgPatch(BaseModel):
    status: str | None = None
    note: str | None = None


@app.post("/api/admin/messages/{msg_id}/read", tags=["留言"], dependencies=[admin])
async def mark_message_read(msg_id: int):
    with get_db() as db:
        message = db.execute("SELECT conversation_id FROM messages WHERE id = ?", (msg_id,)).fetchone()
        if not message:
            raise HTTPException(404, "留言不存在")
        changed = db.execute("UPDATE messages SET status = 'read', updated_at = datetime('now','localtime') WHERE id = ? AND status = 'new'", (msg_id,)).rowcount
        current = db.execute("SELECT status FROM messages WHERE id = ?", (msg_id,)).fetchone()[0]
    if changed:
        await chat.hub.to_admins({"type": "inbox_changed", "conversation_ids": [message[0]] if message[0] else []})
    return {"ok": True, "status": current}


@app.patch("/api/admin/messages/{msg_id}", tags=["留言"], dependencies=[admin])
async def patch_message(msg_id: int, body: MsgPatch):
    sets, args = [], []
    if body.status is not None:
        if body.status not in STATUSES:
            raise HTTPException(400, "状态无效")
        sets.append("status=?"); args.append(body.status)
    if body.note is not None:
        sets.append("note=?"); args.append(body.note[:2000])
    if not sets:
        return {"ok": True}
    sets.append("updated_at=datetime('now','localtime')")
    with get_db() as db:
        source = db.execute("SELECT conversation_id FROM messages WHERE id = ?", (msg_id,)).fetchone()
        if not db.execute(f"UPDATE messages SET {', '.join(sets)} WHERE id=?", [*args, msg_id]).rowcount:
            raise HTTPException(404, "留言不存在")
    await chat.hub.to_admins({"type": "inbox_changed", "conversation_ids": [source[0]] if source[0] else []})
    return {"ok": True}


class BatchIn(BaseModel):
    ids: list[int]
    action: str  # read / handled / spam / new / delete


@app.post("/api/admin/messages/batch", tags=["留言"], dependencies=[admin])
async def batch_messages(body: BatchIn):
    if not body.ids:
        return {"ok": True, "affected": 0}
    marks = ",".join("?" * len(body.ids))
    with get_db() as db:
        source_ids = [r[0] for r in db.execute(f"SELECT DISTINCT conversation_id FROM messages WHERE id IN ({marks}) AND conversation_id IS NOT NULL", body.ids)]
        if body.action == "delete":
            n = db.execute(f"DELETE FROM messages WHERE id IN ({marks})", body.ids).rowcount
        elif body.action in STATUSES:
            n = db.execute(
                f"UPDATE messages SET status=?, updated_at=datetime('now','localtime') WHERE id IN ({marks})",
                [body.action, *body.ids],
            ).rowcount
        else:
            raise HTTPException(400, "操作无效")
    await chat.hub.to_admins({"type": "inbox_changed", "conversation_ids": source_ids})
    return {"ok": True, "affected": n}


@app.delete("/api/admin/messages/{msg_id}", tags=["留言"], dependencies=[admin])
async def delete_message(msg_id: int):
    with get_db() as db:
        source = db.execute("SELECT conversation_id FROM messages WHERE id = ?", (msg_id,)).fetchone()
        db.execute("DELETE FROM messages WHERE id=?", (msg_id,))
    await chat.hub.to_admins({"type": "inbox_changed", "conversation_ids": [source[0]] if source and source[0] else []})
    return {"ok": True}


_CSV_BATCH_SIZE = 100


@app.get("/api/admin/messages/export.csv", tags=["留言"], dependencies=[admin])
def export_csv(site_id: int | None = None, status: str | None = None, q: str | None = None):
    def fetch_batch(before_id=None):
        where, args = _msg_filter(site_id, status, q, before_id=before_id)
        with get_db() as db:
            return db.execute(
                "SELECT m.id, s.name AS site, m.name, m.contact, m.content, m.status, m.note, m.page_url, m.ip, m.created_at "
                f"FROM messages m JOIN sites s ON s.id = m.site_id {where} ORDER BY m.id DESC LIMIT ?",
                [*args, _CSV_BATCH_SIZE],
            ).fetchall()

    def stream(batch):
        buf = io.StringIO(newline="")
        buf.write("\ufeff")  # 让 Excel 正确识别 UTF-8
        writer = csv.writer(buf)
        writer.writerow(["ID", "站点", "姓名", "联系方式", "内容", "状态", "备注", "来源页面", "IP", "时间"])
        yield buf.getvalue()
        while batch:
            buf.seek(0)
            buf.truncate()
            for row in batch:
                writer.writerow(_csv_cell(value) for value in row)
            yield buf.getvalue()
            if len(batch) < _CSV_BATCH_SIZE:
                break
            batch = fetch_batch(batch[-1][0])

    return StreamingResponse(stream(fetch_batch()), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=messages.csv"})


def _csv_cell(value):
    if isinstance(value, str) and (value.startswith(("\t", "\r", "\n"))
                                   or value.lstrip().startswith(("=", "+", "-", "@", "＝", "＋", "－", "＠"))):
        return "'" + value
    return value


@app.get("/api/admin/messages/{msg_id}", tags=["留言"], dependencies=[admin])
def get_message(msg_id: int):
    with get_db() as db:
        message = db.execute(ADMIN_MESSAGE_SQL + "WHERE m.id = ?", (msg_id,)).fetchone()
    if not message:
        raise HTTPException(404, "留言不存在")
    return dict(message)

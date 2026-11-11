"""在线对话：访客 WebSocket、客服后台接口、实时推送。

协议（JSON 文本帧）
访客 → 服务端
    {"type":"hello", "site_key":"sk_..", "token":"<可空>", "page_url":".."}   连接后 10 秒内必须发送
    {"type":"profile", "name":"..", "contact":".."}                          填写称呼和联系方式
    {"type":"msg", "content":"..", "msg_type":"text|image", "client_id":"..", "page_url":".."}
        msg_type=image 时 content 为 /api/chat/upload 返回的图片路径
    {"type":"ping"}
服务端 → 访客
    {"type":"init", "site":{welcome, require_contact, online, offline_text},
                    "profile":{name, contact}|null, "conversation":{id,status}|null, "messages":[...]}
    {"type":"token", "token":".."}                  首次创建访客身份时下发，浏览器自行保存
    {"type":"profile_ok", "profile":{...}}
    {"type":"msg", "message":{id, sender, msg_type, content, created_at}, "client_id":".."}
        sender: visitor / agent / ai / system
    {"type":"status", "status":"waiting|active|closed"}
    {"type":"error", "error":".."}  /  {"type":"pong"}

客服后台通过 /api/admin/ws 接收实时事件（先发 {"type":"auth","token":".."}）：
    {"type":"msg", "conversation":{...}, "message":{...}}
    {"type":"conv", "conversation":{...}}

注意：连接表保存在进程内存中，只能以单进程（单 worker）运行。
"""
import asyncio
import secrets
from collections import defaultdict
from typing import Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from . import auth, media
from .common import (DEFAULT_OFFLINE, DEFAULT_WELCOME, RateLimiter, in_work_hours, origin_allowed,
                     valid_contact)
from .config import MAX_CONTACT, MAX_CONTENT, MAX_NAME
from .db import get_db, rows

router = APIRouter()
admin = Depends(auth.require_admin)

HELLO_TIMEOUT = 10
HISTORY_LIMIT = 100
SENDERS = ("visitor", "agent", "ai", "system")

_msg_by_visitor = RateLimiter(20)      # 每个访客每分钟最多 20 条
_msg_by_ip = RateLimiter(60)           # 每个 IP 每分钟最多 60 条
_new_visitor_by_ip = RateLimiter(10)   # 每个 IP 每分钟最多新建 10 个访客身份


# ---------------------------------------------------------------------------
# 实时连接表
# ---------------------------------------------------------------------------

class Hub:
    def __init__(self):
        self.visitors: dict[int, set[WebSocket]] = defaultdict(set)  # 会话 ID -> 访客连接（可能多个标签页）
        self.admins: set[WebSocket] = set()

    @staticmethod
    async def _send(ws: WebSocket, data: dict) -> bool:
        try:
            await ws.send_json(data)
            return True
        except Exception:
            return False

    async def to_visitor(self, conv_id: int, data: dict) -> int:
        """推给该会话的所有访客连接，返回送达的连接数。"""
        sent = 0
        for ws in list(self.visitors.get(conv_id, ())):
            if await self._send(ws, data):
                sent += 1
            else:
                self.visitors[conv_id].discard(ws)
        return sent

    async def to_admins(self, data: dict):
        for ws in list(self.admins):
            if not await self._send(ws, data):
                self.admins.discard(ws)

    def visitor_online(self, conv_id: int) -> bool:
        return bool(self.visitors.get(conv_id))

    def join(self, conv_id: int, ws: WebSocket):
        self.visitors[conv_id].add(ws)

    def leave(self, conv_id: int, ws: WebSocket):
        conns = self.visitors.get(conv_id)
        if conns is not None:
            conns.discard(ws)
            if not conns:
                del self.visitors[conv_id]


hub = Hub()

# 访客发来新消息后依次调用 hook(conversation, message)；hook 抛出的异常只记录，不影响访客。
# 注册方：qq（转发给客服）、auto（AI 应答 / 转留言）。
VisitorHook = Callable[[dict, dict], Awaitable[None]]
visitor_message_hooks: list[VisitorHook] = []

# 客服是否在岗（不考虑工作时间）。接入 QQ 后由 qq 模块替换为"客服未设为忙碌"。
agent_available: Callable[[], bool] = lambda: True


def site_online(site: dict) -> bool:
    """该站点此刻是否有人工客服：在工作时间内，且客服没有设为忙碌。"""
    return in_work_hours(site.get("work_hours", "")) and agent_available()


def site_public_config(site: dict) -> dict:
    return {
        "welcome": site.get("welcome") or DEFAULT_WELCOME,
        "offline_text": site.get("offline_text") or DEFAULT_OFFLINE,
        "require_contact": bool(site.get("require_contact", 1)),
        "online": site_online(site),
    }


# ---------------------------------------------------------------------------
# 数据操作
# ---------------------------------------------------------------------------

CONV_SUMMARY_SQL = (
    "SELECT c.*, s.name AS site_name, v.name AS visitor_name, v.contact AS visitor_contact, v.ip AS visitor_ip "
    "FROM conversations c JOIN sites s ON s.id = c.site_id JOIN visitors v ON v.id = c.visitor_id "
)


def _msg_out(m: dict) -> dict:
    return {"id": m["id"], "sender": m["sender"], "msg_type": m.get("msg_type") or "text",
            "content": m["content"], "created_at": m["created_at"]}


def preview_of(msg_type: str, content: str) -> str:
    return "[图片]" if msg_type == "image" else content.replace("\n", " ")[:80]


def conv_summary(conv_id: int) -> dict | None:
    with get_db() as db:
        r = db.execute(CONV_SUMMARY_SQL + "WHERE c.id = ?", (conv_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["visitor_online"] = hub.visitor_online(conv_id)
    return d


def find_site(site_key: str) -> dict | None:
    with get_db() as db:
        r = db.execute("SELECT * FROM sites WHERE site_key = ?", (site_key,)).fetchone()
    return dict(r) if r else None


def get_site(site_id: int) -> dict | None:
    with get_db() as db:
        r = db.execute("SELECT * FROM sites WHERE id = ?", (site_id,)).fetchone()
    return dict(r) if r else None


def find_visitor(token: str) -> dict | None:
    if not token:
        return None
    with get_db() as db:
        r = db.execute("SELECT * FROM visitors WHERE token = ?", (token,)).fetchone()
        if r:
            db.execute("UPDATE visitors SET last_seen_at = datetime('now','localtime') WHERE id = ?", (r["id"],))
    return dict(r) if r else None


def create_visitor(ip: str, ua: str, page: str) -> dict:
    token = "v_" + secrets.token_urlsafe(24)
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO visitors (token, ip, user_agent, first_page) VALUES (?,?,?,?)",
            (token, ip, ua[:300], page[:500]),
        )
        return dict(db.execute("SELECT * FROM visitors WHERE id = ?", (cur.lastrowid,)).fetchone())


def find_conversation(site_id: int, visitor_id: int) -> dict | None:
    with get_db() as db:
        r = db.execute(
            "SELECT * FROM conversations WHERE site_id = ? AND visitor_id = ?", (site_id, visitor_id)
        ).fetchone()
    return dict(r) if r else None


def get_or_create_conversation(site_id: int, visitor_id: int) -> dict:
    with get_db() as db:
        db.execute(
            "INSERT OR IGNORE INTO conversations (site_id, visitor_id) VALUES (?, ?)", (site_id, visitor_id)
        )
        return dict(db.execute(
            "SELECT * FROM conversations WHERE site_id = ? AND visitor_id = ?", (site_id, visitor_id)
        ).fetchone())


def history(conv_id: int, limit: int = HISTORY_LIMIT) -> list[dict]:
    with get_db() as db:
        items = rows(db.execute(
            "SELECT * FROM (SELECT * FROM chat_messages WHERE conversation_id = ? ORDER BY id DESC LIMIT ?) "
            "ORDER BY id",
            (conv_id, limit),
        ))
    return [_msg_out(m) for m in items]


def add_message(conv_id: int, sender: str, content: str, page_url: str = "", msg_type: str = "text") -> dict:
    """写入一条消息并更新会话状态：
    访客消息 → 未读 +1，已结束的会话重新打开为 waiting；
    客服消息 → 未读清零，会话变为 active；AI / 系统消息不改状态。"""
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO chat_messages (conversation_id, sender, content, msg_type) VALUES (?,?,?,?)",
            (conv_id, sender, content, msg_type),
        )
        msg = dict(db.execute("SELECT * FROM chat_messages WHERE id = ?", (cur.lastrowid,)).fetchone())
        preview = preview_of(msg_type, content)
        if sender == "visitor":
            db.execute(
                "UPDATE conversations SET last_msg_at = ?, last_preview = ?, unread = unread + 1, "
                "status = CASE WHEN status = 'closed' THEN 'waiting' ELSE status END, "
                "closed_at = CASE WHEN status = 'closed' THEN NULL ELSE closed_at END, "
                "page_url = CASE WHEN ? != '' THEN ? ELSE page_url END WHERE id = ?",
                (msg["created_at"], preview, page_url, page_url[:500], conv_id),
            )
        elif sender == "agent":
            db.execute(
                "UPDATE conversations SET last_msg_at = ?, last_preview = ?, unread = 0, status = 'active', "
                "closed_at = NULL WHERE id = ?",
                (msg["created_at"], preview, conv_id),
            )
        elif sender == "ai":
            db.execute("UPDATE conversations SET last_msg_at = ?, last_preview = ? WHERE id = ?",
                       (msg["created_at"], "[AI] " + preview, conv_id))
    return _msg_out(msg)


async def broadcast_message(conv_id: int, msg: dict, client_id: str = ""):
    """把一条新消息推给访客和所有在线的客服后台。"""
    await hub.to_visitor(conv_id, {"type": "msg", "message": msg, "client_id": client_id})
    await hub.to_admins({"type": "msg", "conversation": conv_summary(conv_id), "message": msg})


async def agent_reply(conv_id: int, content: str, msg_type: str = "text") -> tuple[dict, bool]:
    """客服回复（网页后台和 QQ 共用）。返回 (消息, 访客当前是否在线)。"""
    msg = add_message(conv_id, "agent", content, msg_type=msg_type)
    delivered = hub.visitor_online(conv_id)
    await broadcast_message(conv_id, msg)
    await hub.to_visitor(conv_id, {"type": "status", "status": "active"})
    return msg, delivered


async def post_message(conv_id: int, sender: str, content: str) -> dict:
    """AI / 系统消息。"""
    msg = add_message(conv_id, sender, content)
    await broadcast_message(conv_id, msg)
    return msg


async def set_conversation_status(conv_id: int, status: str):
    """结束（closed）或重新打开（waiting）会话，并通知访客与后台。"""
    with get_db() as db:
        if status == "closed":
            db.execute(
                "UPDATE conversations SET status = 'closed', closed_at = datetime('now','localtime') WHERE id = ?",
                (conv_id,),
            )
        else:
            db.execute("UPDATE conversations SET status = 'waiting', closed_at = NULL WHERE id = ?", (conv_id,))
    if status == "closed":
        await post_message(conv_id, "system", "对话已结束。如还有问题，直接发送消息即可重新联系客服。")
    await hub.to_visitor(conv_id, {"type": "status", "status": status})
    await hub.to_admins({"type": "conv", "conversation": conv_summary(conv_id)})


def _profile(visitor: dict | None) -> dict | None:
    if not visitor:
        return None
    return {"name": visitor["name"], "contact": visitor["contact"]}


def _profile_complete(visitor: dict | None) -> bool:
    return bool(visitor and visitor["name"] and visitor["contact"])


# ---------------------------------------------------------------------------
# 访客 WebSocket
# ---------------------------------------------------------------------------

@router.websocket("/api/chat/ws")
async def visitor_ws(ws: WebSocket):
    await ws.accept()

    async def fail(error: str, code: int = 4400):
        await hub._send(ws, {"type": "error", "error": error})
        await ws.close(code)

    try:
        hello = await asyncio.wait_for(ws.receive_json(), HELLO_TIMEOUT)
    except Exception:
        return await fail("握手超时")
    if not isinstance(hello, dict) or hello.get("type") != "hello":
        return await fail("协议错误")

    site = find_site(str(hello.get("site_key", "")))
    if not site or not site["enabled"]:
        return await fail("站点不存在或已停用", 4403)
    if not origin_allowed(site, ws.headers.get("origin", "")):
        return await fail("来源域名不被允许", 4403)

    ip = ws.client.host if ws.client else ""
    ua = ws.headers.get("user-agent", "")
    page_url = str(hello.get("page_url", ""))[:500]

    # 访客身份在填写资料或第一次发消息时才创建，只打开挂件不会产生数据
    visitor = find_visitor(str(hello.get("token", "")))
    conv = find_conversation(site["id"], visitor["id"]) if visitor else None
    if conv:
        hub.join(conv["id"], ws)
        await hub.to_admins({"type": "conv", "conversation": conv_summary(conv["id"])})
    await hub._send(ws, {
        "type": "init",
        "site": site_public_config(site),
        "profile": _profile(visitor),
        "conversation": {"id": conv["id"], "status": conv["status"]} if conv else None,
        "messages": history(conv["id"]) if conv else [],
    })

    async def ensure_visitor(client_id: str = "") -> bool:
        nonlocal visitor
        if visitor:
            return True
        if not _new_visitor_by_ip.allow(ip):
            await hub._send(ws, {"type": "error", "error": "操作过于频繁，请稍后再试", "client_id": client_id})
            return False
        visitor = create_visitor(ip, ua, page_url)
        await hub._send(ws, {"type": "token", "token": visitor["token"]})
        return True

    try:
        while True:
            data = await ws.receive_json()
            if not isinstance(data, dict):
                continue
            kind = data.get("type")
            if kind == "ping":
                await hub._send(ws, {"type": "pong"})
                continue

            if kind == "profile":
                name = str(data.get("name", "")).strip()[:MAX_NAME]
                contact = str(data.get("contact", "")).strip()[:MAX_CONTACT]
                if not name:
                    await hub._send(ws, {"type": "error", "error": "请填写称呼", "field": "name"})
                    continue
                if not valid_contact(contact):
                    await hub._send(ws, {"type": "error", "error": "请填写正确的电话或邮箱", "field": "contact"})
                    continue
                if not await ensure_visitor():
                    continue
                with get_db() as db:
                    db.execute("UPDATE visitors SET name = ?, contact = ? WHERE id = ?", (name, contact, visitor["id"]))
                visitor = {**visitor, "name": name, "contact": contact}
                await hub._send(ws, {"type": "profile_ok", "profile": _profile(visitor)})
                if conv:
                    await hub.to_admins({"type": "conv", "conversation": conv_summary(conv["id"])})
                continue

            if kind != "msg":
                continue

            client_id = str(data.get("client_id", ""))[:64]
            msg_type = "image" if data.get("msg_type") == "image" else "text"
            content = str(data.get("content", "")).strip()
            if not content:
                continue
            if msg_type == "image" and not media.local_path(content):
                await hub._send(ws, {"type": "error", "error": "图片无效，请重新上传", "client_id": client_id})
                continue
            if len(content) > MAX_CONTENT:
                await hub._send(ws, {"type": "error", "error": f"单条消息不能超过 {MAX_CONTENT} 字", "client_id": client_id})
                continue
            site = find_site(site["site_key"]) or site   # 设置可能刚被修改
            if site.get("require_contact", 1) and not _profile_complete(visitor):
                await hub._send(ws, {"type": "error", "error": "请先填写称呼和联系方式", "client_id": client_id,
                                     "need_profile": True})
                continue
            if not _msg_by_ip.allow(ip) or (visitor and not _msg_by_visitor.allow(str(visitor["id"]))):
                await hub._send(ws, {"type": "error", "error": "发送过于频繁，请稍后再试", "client_id": client_id})
                continue
            if not await ensure_visitor(client_id):
                continue
            if not conv:
                conv = get_or_create_conversation(site["id"], visitor["id"])
                hub.join(conv["id"], ws)

            msg_page = str(data.get("page_url", ""))[:500] or page_url
            was_closed = conv_summary(conv["id"])["status"] == "closed"
            msg = add_message(conv["id"], "visitor", content, msg_page, msg_type)
            await broadcast_message(conv["id"], msg, client_id)
            if was_closed:
                await hub.to_visitor(conv["id"], {"type": "status", "status": "waiting"})

            summary = conv_summary(conv["id"])
            for hook in visitor_message_hooks:
                try:
                    await hook(summary, msg)
                except Exception as e:  # noqa: BLE001
                    print(f"[chat] hook 执行失败: {e!r}", flush=True)
    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception as e:  # noqa: BLE001  无效 JSON 等
        print(f"[chat] 访客连接异常: {e!r}", flush=True)
    finally:
        if conv:
            hub.leave(conv["id"], ws)
            await hub.to_admins({"type": "conv", "conversation": conv_summary(conv["id"])})


# ---------------------------------------------------------------------------
# 客服后台：实时通道
# ---------------------------------------------------------------------------

@router.websocket("/api/admin/ws")
async def admin_ws(ws: WebSocket):
    await ws.accept()
    try:
        first = await asyncio.wait_for(ws.receive_json(), HELLO_TIMEOUT)
    except Exception:
        await ws.close(4401)
        return
    if not isinstance(first, dict) or first.get("type") != "auth" or not auth.verify_token(str(first.get("token", ""))):
        await hub._send(ws, {"type": "error", "error": "未登录或登录已过期"})
        await ws.close(4401)
        return
    hub.admins.add(ws)
    await hub._send(ws, {"type": "ready"})
    try:
        while True:
            data = await ws.receive_json()
            if isinstance(data, dict) and data.get("type") == "ping":
                await hub._send(ws, {"type": "pong"})
    except Exception:
        pass
    finally:
        hub.admins.discard(ws)


# ---------------------------------------------------------------------------
# 客服后台：REST 接口
# ---------------------------------------------------------------------------

@router.get("/api/admin/conversations", tags=["对话"], dependencies=[admin])
def list_conversations(status: str | None = None, site_id: int | None = None, q: str | None = None,
                       page: int = 1, size: int = 30):
    size = max(1, min(size, 100))
    page = max(1, page)
    where, args = [], []
    if status == "open":
        where.append("c.status != 'closed'")
    elif status:
        where.append("c.status = ?"); args.append(status)
    if site_id:
        where.append("c.site_id = ?"); args.append(site_id)
    if q:
        q = q.strip().lstrip("#")
        cond = "(v.name LIKE ? OR v.contact LIKE ? OR c.last_preview LIKE ?"
        args += [f"%{q}%"] * 3
        if q.isdigit():
            cond += " OR c.id = ?"; args.append(int(q))
        where.append(cond + ")")
    w = ("WHERE " + " AND ".join(where)) if where else ""
    with get_db() as db:
        total = db.execute(
            f"SELECT COUNT(*) FROM conversations c JOIN visitors v ON v.id = c.visitor_id {w}", args
        ).fetchone()[0]
        items = rows(db.execute(
            CONV_SUMMARY_SQL + f"{w} ORDER BY c.last_msg_at DESC, c.id DESC LIMIT ? OFFSET ?",
            [*args, size, (page - 1) * size],
        ))
    for it in items:
        it["visitor_online"] = hub.visitor_online(it["id"])
    return {"total": total, "page": page, "size": size, "items": items}


def _require_conv(conv_id: int) -> dict:
    conv = conv_summary(conv_id)
    if not conv:
        raise HTTPException(404, "会话不存在")
    return conv


@router.get("/api/admin/conversations/{conv_id}", tags=["对话"], dependencies=[admin])
async def get_conversation(conv_id: int):
    conv = _require_conv(conv_id)
    with get_db() as db:
        visitor = dict(db.execute(
            "SELECT id, name, contact, ip, user_agent, first_page, created_at, last_seen_at FROM visitors WHERE id = ?",
            (conv["visitor_id"],),
        ).fetchone())
        if conv["unread"]:
            db.execute("UPDATE conversations SET unread = 0 WHERE id = ?", (conv_id,))
    if conv["unread"]:
        conv = _require_conv(conv_id)
        await hub.to_admins({"type": "conv", "conversation": conv})
    return {"conversation": conv, "visitor": visitor, "messages": history(conv_id, 500)}


@router.post("/api/admin/conversations/{conv_id}/read", tags=["对话"], dependencies=[admin])
async def mark_read(conv_id: int):
    _require_conv(conv_id)
    with get_db() as db:
        changed = db.execute("UPDATE conversations SET unread = 0 WHERE id = ? AND unread > 0", (conv_id,)).rowcount
    if changed:
        await hub.to_admins({"type": "conv", "conversation": conv_summary(conv_id)})
    return {"ok": True}


class ReplyIn(BaseModel):
    content: str
    msg_type: str = "text"


@router.post("/api/admin/conversations/{conv_id}/messages", tags=["对话"], dependencies=[admin])
async def reply(conv_id: int, body: ReplyIn):
    _require_conv(conv_id)
    content = body.content.strip()
    msg_type = "image" if body.msg_type == "image" else "text"
    if not content:
        raise HTTPException(400, "内容不能为空")
    if msg_type == "image" and not media.local_path(content):
        raise HTTPException(400, "图片无效，请重新上传")
    if len(content) > MAX_CONTENT:
        raise HTTPException(400, f"单条消息不能超过 {MAX_CONTENT} 字")
    msg, delivered = await agent_reply(conv_id, content, msg_type)
    return {"message": msg, "delivered": delivered}


class StatusIn(BaseModel):
    status: str  # closed / waiting


@router.post("/api/admin/conversations/{conv_id}/status", tags=["对话"], dependencies=[admin])
async def set_status(conv_id: int, body: StatusIn):
    conv = _require_conv(conv_id)
    if body.status not in ("closed", "waiting"):
        raise HTTPException(400, "状态无效")
    if conv["status"] != body.status:
        await set_conversation_status(conv_id, body.status)
    return {"ok": True}


@router.delete("/api/admin/conversations/{conv_id}", tags=["对话"], dependencies=[admin])
async def delete_conversation(conv_id: int):
    with get_db() as db:
        db.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
    for ws in list(hub.visitors.pop(conv_id, ())):
        try:
            await ws.close(4410)
        except Exception:
            pass
    await hub.to_admins({"type": "conv_deleted", "id": conv_id})
    return {"ok": True}

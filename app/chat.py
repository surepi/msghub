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
import sqlite3
from collections import defaultdict
from contextlib import nullcontext
from datetime import date
from typing import Awaitable, Callable, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from . import auth, media
from .common import (DEFAULT_OFFLINE, DEFAULT_WELCOME, RateLimiter, in_work_hours, origin_allowed,
                     valid_contact)
from .config import MAX_CONTACT, MAX_CONTENT, MAX_NAME
from .db import ADMIN_MESSAGE_SQL, get_db, rows

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


def _attach_tags(db, conversations: list[dict]):
    """Only used for admin summaries, never for the visitor protocol."""
    if not conversations:
        return
    from .triage import attach
    attach(db, conversations)
    by_id = {c["id"]: c for c in conversations}
    for c in conversations:
        c["tags"] = []
    marks = ",".join("?" for _ in by_id)
    for tag in db.execute(
        "SELECT ct.conversation_id, t.id, t.name FROM conversation_tags ct "
        f"JOIN tags t ON t.id = ct.tag_id WHERE ct.conversation_id IN ({marks}) ORDER BY t.name, t.id",
        list(by_id),
    ):
        by_id[tag["conversation_id"]]["tags"].append({"id": tag["id"], "name": tag["name"]})


def conv_summary(conv_id: int) -> dict | None:
    with get_db() as db:
        r = db.execute(CONV_SUMMARY_SQL + "WHERE c.id = ?", (conv_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        _attach_tags(db, [d])
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


def add_message(conv_id: int, sender: str, content: str, page_url: str = "", msg_type: str = "text", *, db=None) -> dict:
    """写入一条消息并更新会话状态：
    访客消息 → 未读 +1，已结束的会话重新打开为 waiting；
    客服消息 → 未读清零，会话变为 active；AI / 系统消息不改状态。"""
    with (get_db() if db is None else nullcontext(db)) as db:
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
    if not origin_allowed(site, ws.headers.get("origin", ""), ws.headers):
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
def list_conversations(status: str | None = None, site_id: int | None = None,
                       q: str = Query(default="", max_length=200), page: int = 1, size: int = 30,
                       tag_id: int | None = Query(default=None, gt=0),
                       date_from: date | None = None, date_to: date | None = None,
                       category: str | None = None, priority: str | None = None):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(400, "开始日期不能晚于结束日期")
    size = max(1, min(size, 100))
    page = max(1, page)
    q = q.strip()
    # Treat wildcard characters as ordinary search text, and keep SQLite's ASCII case folding.
    pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    msg_where, msg_args = ["m.conversation_id = c.id"], []
    if date_from:
        msg_where.append("m.created_at >= ?")
        msg_args.append(date_from.isoformat() + " 00:00:00")
    if date_to:
        msg_where.append("m.created_at <= ?")
        msg_args.append(date_to.isoformat() + " 23:59:59")
    period_where = " AND ".join(msg_where)
    period_args = list(msg_args)
    if q:
        msg_where.extend(["m.msg_type = 'text'", "m.content LIKE ? ESCAPE '\\'"])
        msg_args.append(pattern)
    match_where = " AND ".join(msg_where)
    where, args = [], []
    if status == "open":
        where.append("c.status != 'closed'")
    elif status:
        where.append("c.status = ?"); args.append(status)
    if site_id:
        where.append("c.site_id = ?"); args.append(site_id)
    if tag_id is not None:
        where.append("EXISTS (SELECT 1 FROM conversation_tags ct WHERE ct.conversation_id = c.id AND ct.tag_id = ?)")
        args.append(tag_id)
    from .triage import CATEGORIES
    if category:
        if category not in CATEGORIES:
            raise HTTPException(400,'分类无效')
        where.append("(SELECT t.category FROM conversation_triage t WHERE t.conversation_id=c.id AND t.status='ready' ORDER BY t.source_upto DESC LIMIT 1)=?")
        args.append(category)
    if priority:
        if priority != 'high':
            raise HTTPException(400,'优先级无效')
        where.append("c.status!='closed' AND EXISTS (SELECT 1 FROM conversation_triage t WHERE t.conversation_id=c.id AND t.status='ready' AND t.priority='high' AND t.dismissed=0 AND t.source_upto=(SELECT MAX(t2.source_upto) FROM conversation_triage t2 WHERE t2.conversation_id=c.id AND t2.status='ready') AND t.source_upto>COALESCE((SELECT MAX(m.id) FROM chat_messages m WHERE m.conversation_id=c.id AND m.sender='agent'),0))")
    if date_from or date_to:
        where.append(f"EXISTS (SELECT 1 FROM chat_messages m WHERE {period_where})")
        args.extend(period_args)
    if q:
        cond = f"(v.name LIKE ? ESCAPE '\\' OR v.contact LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM chat_messages m WHERE {match_where})"
        args.extend([pattern, pattern, *msg_args])
        number = q.removeprefix("#")
        if number.isascii() and number.isdigit() and int(number) <= 9223372036854775807:
            cond += " OR c.id = ?"; args.append(int(number))
        where.append(cond + ")")
    w = ("WHERE " + " AND ".join(where)) if where else ""
    summary_sql, select_args = CONV_SUMMARY_SQL, []
    if q or date_from or date_to:
        summary_sql = summary_sql.replace("SELECT c.*", f"SELECT (SELECT m.id FROM chat_messages m WHERE {match_where} ORDER BY m.id DESC LIMIT 1) AS match_id, c.*", 1)
        select_args = msg_args
    with get_db() as db:
        total = db.execute(
            f"SELECT COUNT(*) FROM conversations c JOIN visitors v ON v.id = c.visitor_id {w}", args
        ).fetchone()[0]
        items = rows(db.execute(
            summary_sql + f"{w} ORDER BY c.last_msg_at DESC, c.id DESC LIMIT ? OFFSET ?",
            [*select_args, *args, size, (page - 1) * size],
        ))
        _attach_tags(db, items)
        match_ids = [it["match_id"] for it in items if it.get("match_id")]
        matches = {}
        if match_ids:
            marks = ",".join("?" for _ in match_ids)
            matches = {m["id"]: dict(m) for m in db.execute(
                f"SELECT id, content, sender, msg_type, created_at FROM chat_messages WHERE id IN ({marks})", match_ids)}
        for it in items:
            match = matches.get(it.pop("match_id", None))
            it["search_match"] = None
            if match:
                text = "[图片]" if match["msg_type"] == "image" else match["content"]
                start = max(0, text.lower().find(q.lower()) - 35) if q else 0
                snippet = ("…" if start else "") + text[start:start + 160] + ("…" if start + 160 < len(text) else "")
                it["search_match"] = {"id": match["id"], "sender": match["sender"],
                                      "created_at": match["created_at"], "snippet": snippet}
    for it in items:
        it["visitor_online"] = hub.visitor_online(it["id"])
    return {"total": total, "page": page, "size": size, "items": items}


def _message_page(db, conv_id: int, *, before_id: int | None = None, after_id: int | None = None,
                  around_id: int | None = None, limit: int = 100) -> dict:
    anchors = [i for i in (before_id, after_id, around_id) if i is not None]
    if len(anchors) > 1:
        raise HTTPException(400, "只能指定一个消息定位参数")
    if anchors and not db.execute("SELECT 1 FROM chat_messages WHERE id = ? AND conversation_id = ?",
                                  (anchors[0], conv_id)).fetchone():
        raise HTTPException(404, "消息不存在或不属于此会话")
    if around_id is not None:
        before = rows(db.execute("SELECT * FROM chat_messages WHERE conversation_id = ? AND id < ? ORDER BY id DESC LIMIT ?",
                                 (conv_id, around_id, limit // 2)))
        after = rows(db.execute("SELECT * FROM chat_messages WHERE conversation_id = ? AND id >= ? ORDER BY id LIMIT ?",
                                (conv_id, around_id, limit - limit // 2)))
        items = list(reversed(before)) + after
    elif after_id is not None:
        items = rows(db.execute("SELECT * FROM chat_messages WHERE conversation_id = ? AND id > ? ORDER BY id LIMIT ?",
                                (conv_id, after_id, limit)))
    else:
        clause = " AND id < ?" if before_id is not None else ""
        args = [conv_id, before_id, limit] if before_id is not None else [conv_id, limit]
        items = list(reversed(rows(db.execute(
            f"SELECT * FROM chat_messages WHERE conversation_id = ?{clause} ORDER BY id DESC LIMIT ?", args))))
    first = items[0]["id"] if items else anchors[0] if anchors else 0
    last = items[-1]["id"] if items else first
    return {"items": [_msg_out(m) for m in items],
            "has_older": bool(db.execute("SELECT 1 FROM chat_messages WHERE conversation_id = ? AND id < ? LIMIT 1", (conv_id, first)).fetchone()),
            "has_newer": bool(db.execute("SELECT 1 FROM chat_messages WHERE conversation_id = ? AND id > ? LIMIT 1", (conv_id, last)).fetchone())}


@router.get("/api/admin/conversations/{conv_id}/messages", tags=["对话"], dependencies=[admin])
def get_admin_messages(conv_id: int, before_id: int | None = Query(default=None, gt=0),
                       after_id: int | None = Query(default=None, gt=0),
                       around_id: int | None = Query(default=None, gt=0),
                       limit: int = Query(default=100, ge=1, le=500)):
    _require_conv(conv_id)
    with get_db() as db:
        return _message_page(db, conv_id, before_id=before_id, after_id=after_id, around_id=around_id, limit=limit)


@router.get("/api/admin/conversations/{conv_id}/inbox-messages", tags=["留言"], dependencies=[admin])
def conversation_inbox_messages(conv_id: int, page: int = Query(default=1, ge=1),
                                size: int = Query(default=10, ge=1, le=100)):
    conv = _require_conv(conv_id)
    with get_db() as db:
        args = (conv_id, conv["site_id"])
        total = db.execute("SELECT COUNT(*) FROM messages WHERE conversation_id = ? AND site_id = ?", args).fetchone()[0]
        items = rows(db.execute(ADMIN_MESSAGE_SQL + "WHERE m.conversation_id = ? AND m.site_id = ? ORDER BY m.id DESC LIMIT ? OFFSET ?",
                                (*args, size, (page - 1) * size)))
    return {"items": items, "total": total, "page": page, "size": size}


def _require_conv(conv_id: int) -> dict:
    conv = conv_summary(conv_id)
    if not conv:
        raise HTTPException(404, "会话不存在")
    return conv


class TagIn(BaseModel):
    name: str


def _tag_name(body: TagIn) -> str:
    name = body.name.strip()
    if not name or len(name) > 30:
        raise HTTPException(400, "标签名称需要 1–30 字")
    return name


@router.get("/api/admin/tags", tags=["标签"], dependencies=[admin])
def list_tags():
    with get_db() as db:
        return rows(db.execute(
            "SELECT t.id, t.name, COUNT(ct.conversation_id) AS conversation_count FROM tags t "
            "LEFT JOIN conversation_tags ct ON ct.tag_id = t.id GROUP BY t.id ORDER BY t.id"
        ))


@router.post("/api/admin/tags", tags=["标签"], dependencies=[admin])
async def create_tag(body: TagIn):
    name = _tag_name(body)
    try:
        with get_db() as db:
            cur = db.execute("INSERT INTO tags (name) VALUES (?)", (name,))
            tag = {"id": cur.lastrowid, "name": name, "conversation_count": 0}
    except sqlite3.IntegrityError:
        raise HTTPException(409, "已存在同名标签")
    await hub.to_admins({"type": "tags_changed"})
    return tag


@router.put("/api/admin/tags/{tag_id}", tags=["标签"], dependencies=[admin])
async def rename_tag(tag_id: int, body: TagIn):
    name = _tag_name(body)
    try:
        with get_db() as db:
            if not db.execute("UPDATE tags SET name = ? WHERE id = ?", (name, tag_id)).rowcount:
                raise HTTPException(404, "标签不存在")
    except sqlite3.IntegrityError:
        raise HTTPException(409, "已存在同名标签")
    await hub.to_admins({"type": "tags_changed"})
    return {"ok": True}


@router.delete("/api/admin/tags/{tag_id}", tags=["标签"], dependencies=[admin])
async def delete_tag(tag_id: int):
    with get_db() as db:
        if not db.execute("DELETE FROM tags WHERE id = ?", (tag_id,)).rowcount:
            raise HTTPException(404, "标签不存在")
    await hub.to_admins({"type": "tags_changed"})
    return {"ok": True}


class TagBatchIn(BaseModel):
    conversation_ids: list[int] = Field(min_length=1, max_length=100)
    tag_id: int = Field(gt=0)
    action: Literal["add", "remove"]


async def _change_tags(conv_ids: list[int], tag_id: int, action: str) -> list[dict]:
    conv_ids = list(dict.fromkeys(conv_ids))
    marks = ",".join("?" for _ in conv_ids)
    with get_db() as db:
        # Validate and mutate in one transaction; invalid batches must change nothing.
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM tags WHERE id = ?", (tag_id,)).fetchone():
            raise HTTPException(404, "标签不存在")
        count = db.execute(f"SELECT COUNT(*) FROM conversations WHERE id IN ({marks})", conv_ids).fetchone()[0]
        if count != len(conv_ids):
            raise HTTPException(404, "部分会话已不存在，请刷新后重试")
        if action == "add":
            db.executemany("DELETE FROM conversation_ai_tag_blocks WHERE conversation_id=? AND tag_id=?", [(cid,tag_id) for cid in conv_ids])
            db.executemany("INSERT OR IGNORE INTO conversation_tags (conversation_id, tag_id) VALUES (?, ?)",
                           [(cid, tag_id) for cid in conv_ids])
        else:
            db.executemany("INSERT OR IGNORE INTO conversation_ai_tag_blocks (conversation_id,tag_id) VALUES (?,?)", [(cid,tag_id) for cid in conv_ids])
            db.executemany("DELETE FROM conversation_tags WHERE conversation_id = ? AND tag_id = ?",
                           [(cid, tag_id) for cid in conv_ids])
        conversations = rows(db.execute(CONV_SUMMARY_SQL + f"WHERE c.id IN ({marks})", conv_ids))
        _attach_tags(db, conversations)
    for c in conversations:
        c["visitor_online"] = hub.visitor_online(c["id"])
        await hub.to_admins({"type": "conv", "conversation": c})
    return conversations


@router.post("/api/admin/conversation-tags/batch", tags=["标签"], dependencies=[admin])
async def batch_tags(body: TagBatchIn):
    conversations = await _change_tags(body.conversation_ids, body.tag_id, body.action)
    return {"ok": True, "affected": len(conversations), "conversations": conversations}


@router.get("/api/admin/conversations/{conv_id}/tags", tags=["标签"], dependencies=[admin])
def get_conversation_tags(conv_id: int):
    return {"tags": _require_conv(conv_id)["tags"]}


@router.put("/api/admin/conversations/{conv_id}/tags/{tag_id}", tags=["标签"], dependencies=[admin])
async def add_tag_to_conversation(conv_id: int, tag_id: int):
    return {"conversation": (await _change_tags([conv_id], tag_id, "add"))[0]}


@router.delete("/api/admin/conversations/{conv_id}/tags/{tag_id}", tags=["标签"], dependencies=[admin])
async def remove_tag_from_conversation(conv_id: int, tag_id: int):
    return {"conversation": (await _change_tags([conv_id], tag_id, "remove"))[0]}


@router.get("/api/admin/conversations/{conv_id}", tags=["对话"], dependencies=[admin])
async def get_conversation(conv_id: int, message_id: int | None = Query(default=None, gt=0)):
    conv = _require_conv(conv_id)
    with get_db() as db:
        window = _message_page(db, conv_id, around_id=message_id, limit=100 if message_id else 500)
        visitor = dict(db.execute(
            "SELECT id, name, contact, ip, user_agent, first_page, created_at, last_seen_at FROM visitors WHERE id = ?",
            (conv["visitor_id"],),
        ).fetchone())
        notes = rows(db.execute(
            "SELECT id, content, created_at FROM conversation_notes WHERE conversation_id = ? ORDER BY id DESC LIMIT 100",
            (conv_id,),
        ))
        if conv["unread"]:
            db.execute("UPDATE conversations SET unread = 0 WHERE id = ?", (conv_id,))
    if conv["unread"]:
        conv = _require_conv(conv_id)
        await hub.to_admins({"type": "conv", "conversation": conv})
    return {"conversation": conv, "visitor": visitor, "messages": window["items"], "notes": notes,
            "has_older": window["has_older"], "has_newer": window["has_newer"]}


class InternalNoteIn(BaseModel):
    content: str


@router.post("/api/admin/conversations/{conv_id}/notes", tags=["对话"], dependencies=[admin])
def add_conversation_note(conv_id: int, body: InternalNoteIn):
    _require_conv(conv_id)
    content = body.content.strip()
    if not content:
        raise HTTPException(400, "备注不能为空")
    if len(content) > 2000:
        raise HTTPException(400, "备注不能超过 2000 字")
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO conversation_notes (conversation_id, content) VALUES (?, ?)",
            (conv_id, content),
        )
        note = dict(db.execute(
            "SELECT id, content, created_at FROM conversation_notes WHERE id = ?",
            (cur.lastrowid,),
        ).fetchone())
    return {"note": note}


class QuickReplyIn(BaseModel):
    title: str
    content: str
    site_id: int | None = Field(default=None, gt=0)


def _quick_reply_values(body: QuickReplyIn) -> tuple[int | None, str, str]:
    title, content = body.title.strip(), body.content.strip()
    if not title:
        raise HTTPException(400, "快捷回复名称不能为空")
    if not content:
        raise HTTPException(400, "快捷回复内容不能为空")
    if len(title) > 80 or len(content) > 2000:
        raise HTTPException(400, "名称最多 80 字，内容最多 2000 字")
    return body.site_id, title, content


@router.get("/api/admin/quick-replies", tags=["对话"], dependencies=[admin])
def list_quick_replies(site_id: int | None = Query(default=None, gt=0)):
    with get_db() as db:
        if site_id is not None:
            return rows(db.execute(
                "SELECT q.*, s.name AS site_name FROM quick_replies q "
                "LEFT JOIN sites s ON s.id = q.site_id WHERE q.site_id IS NULL OR q.site_id = ? "
                "ORDER BY q.site_id IS NOT NULL, q.title COLLATE NOCASE, q.id",
                (site_id,),
            ))
        return rows(db.execute(
            "SELECT q.*, s.name AS site_name FROM quick_replies q "
            "LEFT JOIN sites s ON s.id = q.site_id ORDER BY q.site_id IS NOT NULL, q.title COLLATE NOCASE, q.id"
        ))


@router.post("/api/admin/quick-replies", tags=["对话"], dependencies=[admin])
def create_quick_reply(body: QuickReplyIn):
    site_id, title, content = _quick_reply_values(body)
    with get_db() as db:
        if site_id is not None and not db.execute("SELECT 1 FROM sites WHERE id = ?", (site_id,)).fetchone():
            raise HTTPException(404, "站点不存在")
        cur = db.execute(
            "INSERT INTO quick_replies (site_id, title, content) VALUES (?, ?, ?)",
            (site_id, title, content),
        )
        return dict(db.execute(
            "SELECT q.*, s.name AS site_name FROM quick_replies q "
            "LEFT JOIN sites s ON s.id = q.site_id WHERE q.id = ?",
            (cur.lastrowid,),
        ).fetchone())


@router.put("/api/admin/quick-replies/{reply_id}", tags=["对话"], dependencies=[admin])
def update_quick_reply(reply_id: int, body: QuickReplyIn):
    site_id, title, content = _quick_reply_values(body)
    with get_db() as db:
        if site_id is not None and not db.execute("SELECT 1 FROM sites WHERE id = ?", (site_id,)).fetchone():
            raise HTTPException(404, "站点不存在")
        cur = db.execute(
            "UPDATE quick_replies SET site_id = ?, title = ?, content = ?, updated_at = datetime('now','localtime') "
            "WHERE id = ?",
            (site_id, title, content, reply_id),
        )
        if not cur.rowcount:
            raise HTTPException(404, "快捷回复不存在")
    return {"ok": True}


@router.delete("/api/admin/quick-replies/{reply_id}", tags=["对话"], dependencies=[admin])
def delete_quick_reply(reply_id: int):
    with get_db() as db:
        if not db.execute("DELETE FROM quick_replies WHERE id = ?", (reply_id,)).rowcount:
            raise HTTPException(404, "快捷回复不存在")
    return {"ok": True}


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

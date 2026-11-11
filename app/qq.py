"""QQ 官方机器人对接（二期第 ② 阶段）。

流程
    访客发消息 → 合并 QQ_MERGE_DELAY 秒内的消息 → 发给已绑定的客服（单聊）
    客服在 QQ 回复 → 平台回调 /api/qq/callback → 找到目标会话 → 推给访客网页

发给客服的策略：先发主动消息；失败且客服最近一条消息仍在被动回复窗口内 → 改用被动回复；
都不行 → 消息留在积压队列（chat_messages.qq_state = 'pending'），客服下次发任何消息时补发，
后台每 30 秒也会重试一次。

客服回复定位会话的顺序：引用的消息 → 「#编号 内容」→ 当前默认会话（最近转发过来的会话）。
"""
import asyncio
import json
import re
import secrets
import time
import urllib.error
import urllib.request

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from . import auth, chat
from .config import (QQ_API_BASE, QQ_APP_ID, QQ_APP_SECRET, QQ_MERGE_DELAY, QQ_PASSIVE_MAX, QQ_PASSIVE_WINDOW,
                     QQ_TOKEN_URL)
from .db import get_db, rows

router = APIRouter()
admin = Depends(auth.require_admin)

ENABLED = bool(QQ_APP_ID and QQ_APP_SECRET)
MAX_QQ_TEXT = 1500          # 单条 QQ 消息的长度上限（保守值），超出拆成多条
SWITCH_GUARD_SECONDS = 10   # 默认会话刚被新消息切换时，不接受"直接发内容"，防止回错人
BIND_CODE_TTL = 600

HELP = (
    "MsgHub 客服助手\n"
    "· 直接发文字：回复当前会话\n"
    "· 引用某条转发消息再回复：发给那位访客\n"
    "· #编号 内容：发给指定会话\n"
    "/列表 进行中的会话\n"
    "/切换 #编号 设为当前会话\n"
    "/历史 #编号 最近 10 条消息\n"
    "/结束 #编号 结束会话（不带编号=当前会话）\n"
    "/忙 /在 切换在线状态"
)


def log(text: str):
    print(f"[qq] {text}", flush=True)


# ---------------------------------------------------------------------------
# 签名（Ed25519，私钥种子 = AppSecret 重复到 32 字节后截断）
# ---------------------------------------------------------------------------

def make_key(secret: str) -> Ed25519PrivateKey:
    seed = secret
    while len(seed) < 32:
        seed = seed * 2
    return Ed25519PrivateKey.from_private_bytes(seed[:32].encode())


_key = make_key(QQ_APP_SECRET) if ENABLED else None


def sign_validation(event_ts: str, plain_token: str, key: Ed25519PrivateKey | None = None) -> str:
    """回调地址校验（op 13）：签名内容为 event_ts + plain_token，十六进制输出。"""
    return (key or _key).sign((event_ts + plain_token).encode()).hex()


def verify_event(timestamp: str, body: bytes, sig_hex: str, key: Ed25519PrivateKey | None = None) -> bool:
    """普通事件：校验 X-Signature-Ed25519 是否为 (X-Signature-Timestamp + 原始请求体) 的签名。"""
    if not timestamp or not sig_hex:
        return False
    try:
        sig = bytes.fromhex(sig_hex)
    except ValueError:
        return False
    if len(sig) != 64 or sig[63] & 224:
        return False
    try:
        (key or _key).public_key().verify(sig, timestamp.encode() + body)
        return True
    except InvalidSignature:
        return False


# ---------------------------------------------------------------------------
# OpenAPI 客户端
# ---------------------------------------------------------------------------

class QQApiError(Exception):
    def __init__(self, code, message: str, status: int = 0):
        super().__init__(f"{code} {message}")
        self.code, self.message, self.status = code, message, status


def _http(method: str, url: str, body: dict | None, headers: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(body, ensure_ascii=False).encode() if body is not None else None,
        headers={"Content-Type": "application/json", **headers},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {"message": raw[:200].decode(errors="replace")}
    return status, data if isinstance(data, dict) else {"data": data}


class QQClient:
    def __init__(self):
        self._token = ""
        self._expires = 0.0
        self._lock = asyncio.Lock()

    async def token(self, force: bool = False) -> str:
        async with self._lock:
            if not force and self._token and time.time() < self._expires - 60:
                return self._token
            status, d = await asyncio.to_thread(
                _http, "POST", QQ_TOKEN_URL, {"appId": QQ_APP_ID, "clientSecret": QQ_APP_SECRET}, {})
            if "access_token" not in d:
                raise QQApiError(d.get("code"), d.get("message") or "获取 access_token 失败", status)
            self._token = d["access_token"]
            self._expires = time.time() + int(d.get("expires_in") or 7200)
            return self._token

    async def call(self, method: str, path: str, body: dict | None = None) -> dict:
        for attempt in (1, 2):
            tok = await self.token(force=attempt == 2)
            status, d = await asyncio.to_thread(
                _http, method, QQ_API_BASE + path, body,
                {"Authorization": "QQBot " + tok, "X-Union-Appid": QQ_APP_ID})
            if status == 401 and attempt == 1:
                continue  # 令牌失效，强制刷新后重试一次
            if status >= 400 or ("code" in d and d.get("code") not in (0, None) and "id" not in d):
                raise QQApiError(d.get("code"), d.get("message") or f"HTTP {status}", status)
            return d
        raise QQApiError(None, "鉴权失败", 401)

    async def send_c2c(self, openid: str, content: str, msg_id: str = "", msg_seq: int = 0) -> dict:
        body: dict = {"content": content, "msg_type": 0}
        if msg_id:
            body["msg_id"] = msg_id
            body["msg_seq"] = msg_seq
        return await self.call("POST", f"/v2/users/{openid}/messages", body)


client = QQClient()
_send_lock = asyncio.Lock()   # 串行发送：保证顺序，也避免被动回复序号冲突


# ---------------------------------------------------------------------------
# 客服状态
# ---------------------------------------------------------------------------

def agent() -> dict:
    with get_db() as db:
        return dict(db.execute("SELECT * FROM qq_agent WHERE id = 1").fetchone())


def update_agent(**fields):
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with get_db() as db:
        db.execute(f"UPDATE qq_agent SET {cols} WHERE id = 1", list(fields.values()))


def passive_available(a: dict) -> bool:
    return bool(a["last_msg_id"]) and time.time() - a["last_msg_at"] < QQ_PASSIVE_WINDOW \
        and a["passive_used"] < QQ_PASSIVE_MAX


def _chunks(text: str) -> list[str]:
    return [text[i:i + MAX_QQ_TEXT] for i in range(0, len(text), MAX_QQ_TEXT)] or [""]


async def deliver(text: str, conv_id: int | None = None, openid: str = "") -> tuple[list[str], str]:
    """发给客服。返回 (成功发出的 QQ 消息 ID 列表, 失败原因)。发出的消息记入 qq_forwards 供引用回复定位。"""
    async with _send_lock:
        a = agent()
        openid = openid or a["openid"]
        if not openid:
            return [], "尚未绑定客服"
        ids: list[str] = []
        for part in _chunks(text):
            try:
                d = await client.send_c2c(openid, part)
            except QQApiError as e:
                a = agent()
                if not passive_available(a) or openid != a["openid"]:
                    log(f"主动消息失败且无可用被动窗口：{e}")
                    return ids, f"发送失败：{e.message}（{e.code}）"
                seq = a["passive_used"] + 1
                update_agent(passive_used=seq)
                try:
                    d = await client.send_c2c(openid, part, a["last_msg_id"], seq)
                except QQApiError as e2:
                    log(f"主动消息失败（{e}），被动回复也失败：{e2}")
                    return ids, f"发送失败：{e2.message}（{e2.code}）"
            except Exception as e:  # noqa: BLE001  网络错误
                log(f"发送异常：{e}")
                return ids, f"网络错误：{e}"
            refs = [d.get("id"), (d.get("ext_info") or {}).get("ref_idx")]
            if conv_id:
                with get_db() as db:
                    for ref in filter(None, refs):
                        db.execute("INSERT OR REPLACE INTO qq_forwards (qq_msg_id, conversation_id) VALUES (?, ?)",
                                   (str(ref), conv_id))
            ids.append(str(d.get("id") or ""))
        return ids, ""


async def reply_to(openid: str, msg_id: str, text: str):
    """回复未绑定的用户（例如绑定码错误）：只用被动回复。"""
    try:
        await client.send_c2c(openid, text, msg_id, 1)
    except Exception as e:  # noqa: BLE001
        log(f"回复未绑定用户失败：{e}")


# ---------------------------------------------------------------------------
# 访客 → 客服
# ---------------------------------------------------------------------------

_flush_timers: dict[int, asyncio.TimerHandle] = {}
_tasks: set[asyncio.Task] = set()


def _spawn(coro):
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


async def on_visitor_message(conv: dict, msg: dict):
    """chat 模块的访客消息 hook。"""
    if not agent()["openid"]:
        return
    with get_db() as db:
        db.execute("UPDATE chat_messages SET qq_state = 'pending' WHERE id = ?", (msg["id"],))
    conv_id = conv["id"]
    if conv_id not in _flush_timers:
        loop = asyncio.get_running_loop()
        _flush_timers[conv_id] = loop.call_later(QQ_MERGE_DELAY, lambda: _spawn(flush_conversation(conv_id)))


def compose(conv: dict, msgs: list[dict], first: bool) -> str:
    head = f"【{conv['site_name']}】#{conv['id']} {conv['visitor_name'] or '访客'}"
    body = "\n".join(m["content"] for m in msgs)
    if first:
        return (f"{head}（新会话）\n页面：{conv['page_url'] or '—'}\n——\n{body}\n——\n"
                f"引用本条回复，或发「#{conv['id']} 内容」")
    return f"{head}：\n{body}"


async def flush_conversation(conv_id: int) -> bool:
    """把该会话积压的访客消息合并发给客服。"""
    _flush_timers.pop(conv_id, None)
    conv = chat.conv_summary(conv_id)
    if not conv:
        return True
    with get_db() as db:
        msgs = rows(db.execute(
            "SELECT id, content FROM chat_messages WHERE conversation_id = ? AND qq_state = 'pending' ORDER BY id",
            (conv_id,)))
        first = not db.execute(
            "SELECT 1 FROM chat_messages WHERE conversation_id = ? AND qq_state = 'sent' LIMIT 1", (conv_id,)
        ).fetchone()
    if not msgs:
        return True
    ids, err = await deliver(compose(conv, msgs, first), conv_id)
    if not ids:
        log(f"#{conv_id} 转发失败，{len(msgs)} 条消息进入积压：{err}")
        return False
    with get_db() as db:
        db.executemany("UPDATE chat_messages SET qq_state = 'sent', qq_message_id = ? WHERE id = ?",
                       [(ids[0], m["id"]) for m in msgs])
    a = agent()
    if a["current_conv_id"] != conv_id:
        update_agent(current_conv_id=conv_id, current_set_at=time.time())
    return True


async def flush_backlog():
    with get_db() as db:
        conv_ids = [r[0] for r in db.execute(
            "SELECT DISTINCT conversation_id FROM chat_messages WHERE qq_state = 'pending' ORDER BY id")]
    for cid in conv_ids:
        if cid in _flush_timers:
            continue  # 正在等待合并，交给定时器
        if not await flush_conversation(cid):
            break


async def _backlog_loop():
    while True:
        await asyncio.sleep(30)
        try:
            if agent()["openid"]:
                await flush_backlog()
        except Exception as e:  # noqa: BLE001
            log(f"积压重试异常：{e}")


# ---------------------------------------------------------------------------
# 客服 → 访客
# ---------------------------------------------------------------------------

def _conv_line(c: dict, current: int | None) -> str:
    mark = "→" if c["id"] == current else " "
    status = {"waiting": "待回复", "active": "进行中", "closed": "已结束"}[c["status"]]
    return f"{mark}#{c['id']}【{c['site_name']}】{c['visitor_name'] or '访客'} · {status} · {c['last_preview'][:20]}"


async def handle_command(content: str, a: dict):
    m = re.match(r"^/(\S+?)\s*(?:#?(\d+))?\s*$", content)
    cmd, arg = (m.group(1), int(m.group(2)) if m.group(2) else None) if m else ("", None)
    say = lambda text: deliver(text)  # noqa: E731

    if cmd in ("帮助", "help", "h", "?"):
        return await say(HELP)
    if cmd == "列表":
        with get_db() as db:
            items = rows(db.execute(
                chat.CONV_SUMMARY_SQL + "WHERE c.status != 'closed' ORDER BY c.last_msg_at DESC LIMIT 10"))
        if not items:
            return await say("当前没有进行中的会话")
        return await say("进行中的会话：\n" + "\n".join(_conv_line(c, a["current_conv_id"]) for c in items))
    if cmd in ("忙", "在"):
        update_agent(online=0 if cmd == "忙" else 1)
        return await say("已设为忙碌" if cmd == "忙" else "已设为在线")

    target = arg or a["current_conv_id"]
    conv = chat.conv_summary(target) if target else None
    if cmd in ("切换", "历史", "结束") and not conv:
        return await say(f"会话 #{target} 不存在" if target else "请带上会话编号，例如 /历史 #12")
    if cmd == "切换":
        update_agent(current_conv_id=conv["id"], current_set_at=0)
        return await say("当前会话：" + _conv_line(conv, conv["id"]).lstrip("→"))
    if cmd == "历史":
        msgs = chat.history(conv["id"], 10)
        who = {"visitor": "访客", "agent": "客服", "system": "系统"}
        lines = [f"{m['created_at'][11:16]} {who[m['sender']]}：{m['content']}" for m in msgs]
        return await say(f"#{conv['id']} 最近消息：\n" + ("\n".join(lines) or "（无）"))
    if cmd == "结束":
        if conv["status"] != "closed":
            await chat.set_conversation_status(conv["id"], "closed")
        return await say(f"#{conv['id']} 已结束")
    return await say("未知指令。\n\n" + HELP)


def quoted_refs(d: dict) -> list[str]:
    """取出客服"引用回复"时被引用消息的编号。平台在不同位置给出：
    message_reference.message_id；message_scene.ext 中的 ref_msg_idx=...；msg_elements 中被引用消息的 msg_idx。"""
    refs: list[str] = []
    mr = d.get("message_reference") or {}
    if isinstance(mr, dict) and mr.get("message_id"):
        refs.append(str(mr["message_id"]))
    scene = d.get("message_scene") or {}
    for item in (scene.get("ext") or []) if isinstance(scene, dict) else []:
        if isinstance(item, str) and item.startswith("ref_msg_idx="):
            refs.append(item.split("=", 1)[1])
    for el in d.get("msg_elements") or []:
        if isinstance(el, dict):
            for k in ("msg_idx", "ref_idx", "message_id", "id"):
                if el.get(k):
                    refs.append(str(el[k]))
    return [r for i, r in enumerate(refs) if r and r not in refs[:i]]


def _debug_shape(d: dict) -> str:
    """记录引用相关字段的结构（不含文字内容），用于核对平台格式。"""
    scene = d.get("message_scene")
    els = [{k: (v if k.endswith("idx") or k in ("message_type", "id") else type(v).__name__) for k, v in el.items()}
           for el in (d.get("msg_elements") or []) if isinstance(el, dict)]
    return json.dumps({"message_type": d.get("message_type"), "message_scene": scene, "msg_elements": els},
                      ensure_ascii=False)[:600]


async def handle_c2c(d: dict):
    openid = str((d.get("author") or {}).get("user_openid") or "")
    content = str(d.get("content") or "").strip()
    msg_id = str(d.get("id") or "")
    if not openid:
        return
    a = agent()

    # 绑定：/绑定 123456
    m = re.match(r"^/?绑定\s*(\d{6})$", content)
    if m:
        if a["bind_code"] and secrets.compare_digest(a["bind_code"], m.group(1)) and time.time() < a["bind_expires"]:
            update_agent(openid=openid, bound_at=time.strftime("%Y-%m-%d %H:%M:%S"), bind_code="", bind_expires=0,
                         last_msg_id=msg_id, last_msg_at=time.time(), passive_used=0)
            log("客服绑定成功")
            await deliver("绑定成功！网站访客的消息之后会转发到这里。\n\n" + HELP)
            await flush_backlog()
        else:
            await reply_to(openid, msg_id, "绑定码无效或已过期，请在 MsgHub 后台重新生成。")
        return
    if openid != a["openid"]:
        await reply_to(openid, msg_id, "这是网站客服机器人，仅供已绑定的客服使用。")
        return

    update_agent(last_msg_id=msg_id, last_msg_at=time.time(), passive_used=0)
    a = agent()
    await flush_backlog()  # 客服刚发过消息，被动窗口打开，先补发积压

    if not content:
        if d.get("attachments"):
            await deliver("暂不支持把图片 / 文件转给访客，请发送文字。")
        return
    if content.startswith("/"):
        await handle_command(content, a)
        return

    # 定位目标会话：引用 → #编号 → 当前会话
    target, text, implicit = None, content, False
    refs = quoted_refs(d)
    if refs:
        with get_db() as db:
            marks = ",".join("?" * len(refs))
            r = db.execute(f"SELECT conversation_id FROM qq_forwards WHERE qq_msg_id IN ({marks}) LIMIT 1",
                           refs).fetchone()
        target = r[0] if r else None
        log(f"引用回复 → {'#' + str(target) if target else '未找到对应会话'}（候选 {[x[:24] for x in refs]}）")
    m = re.match(r"^#(\d+)\s+(.+)$", content, re.S)
    if m:
        target, text = int(m.group(1)), m.group(2).strip()
    if not target:
        implicit = True
        target = a["current_conv_id"]
        if not target:
            await deliver("当前没有可回复的会话。发 /列表 查看，或用「#编号 内容」指定。")
            return
        if a["current_set_at"] and time.time() - a["current_set_at"] < SWITCH_GUARD_SECONDS:
            await deliver(f"刚有新会话 #{target} 的消息进来，为避免回错人，这条没有发出。\n"
                          "请引用要回复的消息，或发「#编号 内容」。")
            return
    if not chat.conv_summary(target):
        await deliver(f"会话 #{target} 不存在")
        return

    await chat.agent_reply(target, text)
    if implicit:
        await deliver(f"✓ 已发给 #{target}")
    elif target != a["current_conv_id"]:
        update_agent(current_conv_id=target, current_set_at=0)


# ---------------------------------------------------------------------------
# 平台回调
# ---------------------------------------------------------------------------

_seen_events: dict[str, float] = {}


def _duplicate(event_id: str) -> bool:
    now = time.time()
    if len(_seen_events) > 2000:
        for k in [k for k, t in _seen_events.items() if now - t > 600]:
            del _seen_events[k]
    if event_id in _seen_events:
        return True
    _seen_events[event_id] = now
    return False


async def _run(coro, label: str):
    try:
        await coro
    except Exception as e:  # noqa: BLE001
        log(f"处理 {label} 出错：{e!r}")


@router.post("/api/qq/callback", include_in_schema=False)
async def callback(request: Request):
    if not ENABLED:
        raise HTTPException(404)
    body = await request.body()
    try:
        payload = json.loads(body)
    except ValueError:
        return JSONResponse({"error": "invalid json"}, 400)
    appid = request.headers.get("x-bot-appid")
    if appid and appid != QQ_APP_ID:
        return JSONResponse({"error": "appid mismatch"}, 403)

    op = payload.get("op")
    if op == 13:  # 回调地址校验
        d = payload.get("d") or {}
        plain = str(d.get("plain_token", ""))
        log("收到回调地址校验请求")
        return {"plain_token": plain, "signature": sign_validation(str(d.get("event_ts", "")), plain)}

    if not verify_event(request.headers.get("x-signature-timestamp", ""), body,
                        request.headers.get("x-signature-ed25519", "")):
        log("签名校验失败，已拒绝")
        return JSONResponse({"error": "invalid signature"}, 401)

    update_agent(last_event_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    if op == 0:
        t, d = payload.get("t"), payload.get("d") or {}
        event_id = str(payload.get("id") or d.get("id") or "")
        if event_id and _duplicate(event_id):
            return {"op": 12}
        ref = d.get("message_reference")
        log(f"事件 {t}，字段 {sorted(d.keys())}" + (f"，引用 {json.dumps(ref, ensure_ascii=False)[:120]}" if ref else ""))
        if t == "C2C_MESSAGE_CREATE":
            if d.get("msg_elements") or d.get("message_reference"):
                log(f"引用相关字段：{_debug_shape(d)}")
            _spawn(_run(handle_c2c(d), t))
    return {"op": 12}


# ---------------------------------------------------------------------------
# 后台管理接口
# ---------------------------------------------------------------------------

def _mask(s: str) -> str:
    return s[:4] + "…" + s[-4:] if len(s) > 10 else s


@router.get("/api/admin/qq/status", tags=["QQ"], dependencies=[admin])
async def status():
    if not ENABLED:
        return {"enabled": False}
    a = agent()
    token_ok, token_error = True, ""
    try:
        await client.token()
    except Exception as e:  # noqa: BLE001
        token_ok, token_error = False, str(e)
    with get_db() as db:
        pending = db.execute("SELECT COUNT(*) FROM chat_messages WHERE qq_state = 'pending'").fetchone()[0]
    window = max(0, int(QQ_PASSIVE_WINDOW - (time.time() - a["last_msg_at"]))) if passive_available(a) else 0
    return {
        "enabled": True,
        "app_id": QQ_APP_ID,
        "token_ok": token_ok,
        "token_error": token_error,
        "callback_path": "/api/qq/callback",
        "last_event_at": a["last_event_at"],
        "bound": bool(a["openid"]),
        "openid": _mask(a["openid"]),
        "bound_at": a["bound_at"],
        "online": bool(a["online"]),
        "current_conv_id": a["current_conv_id"],
        "passive_seconds_left": window,
        "pending": pending,
        "bind_code": a["bind_code"] if a["bind_code"] and time.time() < a["bind_expires"] else "",
        "bind_expires_in": max(0, int(a["bind_expires"] - time.time())),
    }


@router.post("/api/admin/qq/bind-code", tags=["QQ"], dependencies=[admin])
def bind_code():
    if not ENABLED:
        raise HTTPException(400, "未配置 QQ 机器人")
    code = f"{secrets.randbelow(1_000_000):06d}"
    update_agent(bind_code=code, bind_expires=time.time() + BIND_CODE_TTL)
    return {"code": code, "expires_in": BIND_CODE_TTL}


@router.post("/api/admin/qq/unbind", tags=["QQ"], dependencies=[admin])
def unbind():
    update_agent(openid="", bound_at=None, last_msg_id="", last_msg_at=0, passive_used=0,
                 current_conv_id=None, current_set_at=0)
    with get_db() as db:
        db.execute("UPDATE chat_messages SET qq_state = '' WHERE qq_state = 'pending'")
    return {"ok": True}


@router.post("/api/admin/qq/test", tags=["QQ"], dependencies=[admin])
async def test_message():
    if not ENABLED:
        raise HTTPException(400, "未配置 QQ 机器人")
    ids, err = await deliver("这是一条来自 MsgHub 后台的测试消息，能看到说明转发正常。")
    if not ids:
        raise HTTPException(502, err)
    return {"ok": True}


# ---------------------------------------------------------------------------

def setup():
    """应用启动时调用：注册访客消息 hook、启动积压重试。"""
    if not ENABLED:
        log("未配置 QQ_APP_ID / QQ_APP_SECRET，QQ 转发未启用")
        return
    chat.visitor_message_hooks.append(on_visitor_message)
    _spawn(_backlog_loop())
    log(f"已启用，AppID {QQ_APP_ID}")

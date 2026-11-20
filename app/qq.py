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
import base64
import json
import re
import secrets
import time
from contextlib import nullcontext
import urllib.error
import urllib.request

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from . import auth, auto, chat, media, qq_inbound
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
    "/总结 #编号 AI 总结、需求和联系方式\n"
    "/分类 #编号 AI 分类、优先级和标签建议\n"
    "/加标签 #编号 应用最新分类的标签建议\n"
    "/普通 #编号 取消高优先级\n"
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


class QQSendUncertain(Exception):
    """A send may have reached QQ; a blind retry could duplicate it."""


def safe_error(exc) -> str:
    text = str(exc)
    for value in (QQ_APP_SECRET, client._token, agent()['openid']):
        if value:
            text = text.replace(value, '[已隐藏]')
    return text[:500]


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
            try:
                status, d = await asyncio.to_thread(
                    _http, "POST", QQ_TOKEN_URL, {"appId": QQ_APP_ID, "clientSecret": QQ_APP_SECRET}, {})
            except Exception as exc:
                raise QQApiError(None, '获取 QQ 凭据时网络错误：' + safe_error(exc)) from exc
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

    async def send_c2c(self, openid: str, content: str = "", msg_id: str = "", msg_seq: int = 0,
                       body: dict | None = None) -> dict:
        """发送单聊消息。body 为空时发文字；传 body 可发富媒体（msg_type=7）。"""
        body = dict(body) if body else {"content": content, "msg_type": 0}
        if msg_id:
            body["msg_id"] = msg_id
            body["msg_seq"] = msg_seq
        return await self.call("POST", f"/v2/users/{openid}/messages", body)

    async def upload_image(self, openid: str, data: bytes) -> str:
        """上传图片（二进制，≤5MB），返回发送消息时用的 file_info。"""
        d = await self.call("POST", f"/v2/users/{openid}/files", {
            "file_type": 1, "file_data": base64.b64encode(data).decode(), "srv_send_msg": False})
        if not d.get("file_info"):
            raise QQApiError(d.get("code"), d.get("message") or "上传图片失败")
        return d["file_info"]


client = QQClient()
_send_lock = asyncio.Lock()   # 串行发送：保证顺序，也避免被动回复序号冲突


# ---------------------------------------------------------------------------
# 客服状态
# ---------------------------------------------------------------------------

def agent() -> dict:
    with get_db() as db:
        return dict(db.execute("SELECT * FROM qq_agent WHERE id = 1").fetchone())


def update_agent(*, db=None, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with (get_db() if db is None else nullcontext(db)) as db:
        db.execute(f"UPDATE qq_agent SET {cols} WHERE id = 1", list(fields.values()))


def passive_available(a: dict) -> bool:
    return bool(a["last_msg_id"]) and time.time() - a["last_msg_at"] < QQ_PASSIVE_WINDOW \
        and a["passive_used"] < QQ_PASSIVE_MAX


def _chunks(text: str) -> list[str]:
    return [text[i:i + MAX_QQ_TEXT] for i in range(0, len(text), MAX_QQ_TEXT)] or [""]


async def _send_one(openid: str, body: dict, conv_id: int | None) -> str:
    """发一条消息给客服：主动消息失败时用被动回复兜底。成功返回消息 ID，失败抛 QQApiError。
    调用方需持有 _send_lock。"""
    try:
        try:
            d = await client.send_c2c(openid, body=body)
        except QQApiError as exc:
            if exc.status >= 500:
                raise QQSendUncertain('QQ 服务异常，发送结果未确认：' + safe_error(exc)) from exc
            a = agent()
            if not passive_available(a) or openid != a["openid"]:
                raise
            seq = a["passive_used"] + 1
            update_agent(passive_used=seq)
            d = await client.send_c2c(openid, body=body, msg_id=a["last_msg_id"], msg_seq=seq)
    except QQApiError as exc:
        if exc.status >= 500:
            raise QQSendUncertain('QQ 服务异常，发送结果未确认：' + safe_error(exc)) from exc
        raise
    except QQSendUncertain:
        raise
    except Exception as exc:
        raise QQSendUncertain('请求未取得明确响应，请先核对 QQ 收件情况：' + safe_error(exc)) from exc
    extra = d.get('ext_info') if isinstance(d, dict) else None
    refs = [d.get('id') if isinstance(d, dict) else None,
            extra.get('ref_idx') if isinstance(extra, dict) else None]
    if not any(refs):
        raise QQSendUncertain('QQ 未返回可确认的消息编号，请先核对收件情况')
    if conv_id:
        try:
            with get_db() as db:
                for ref in filter(None, refs):
                    db.execute("INSERT OR REPLACE INTO qq_forwards (qq_msg_id, conversation_id) VALUES (?, ?)",
                               (str(ref), conv_id))
        except Exception as exc:
            raise QQSendUncertain('QQ 已响应，但本地引用记录保存失败，请先核对收件情况') from exc
    return str(next(ref for ref in refs if ref))


async def deliver(text: str, conv_id: int | None = None, openid: str = "", *, binding_version: int | None = None) -> tuple[list[str], str]:
    """发文字给客服。返回 (成功发出的 QQ 消息 ID 列表, 失败原因)。发出的消息记入 qq_forwards 供引用回复定位。"""
    async with _send_lock:
        owner = agent()
        if binding_version is not None and (owner["openid"] != openid or owner["binding_version"] != binding_version):
            return [], "客服绑定已变更"
        openid = openid or owner["openid"]
        if not openid:
            return [], "尚未绑定客服"
        ids: list[str] = []
        for part in _chunks(text):
            try:
                ids.append(await _send_one(openid, {"content": part, "msg_type": 0}, conv_id))
            except QQApiError as e:
                return ids, '发送失败：' + safe_error(e)
            except Exception as e:  # noqa: BLE001  网络错误
                log('发送异常：' + safe_error(e))
                return ids, '发送异常：' + safe_error(e)
        return ids, ""


async def notify(text: str, conv_id: int | None = None):
    """给客服发提醒（AI 回复、转人工等）；未绑定时忽略。"""
    if agent()["openid"]:
        await deliver(text, conv_id)


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
    """Schedule forwarding for a visitor message already stored with its queue state."""
    if not agent()["openid"]:
        return
    conv_id = conv["id"]
    if conv_id not in _flush_timers:
        loop = asyncio.get_running_loop()
        _flush_timers[conv_id] = loop.call_later(QQ_MERGE_DELAY, lambda: _spawn(flush_conversation(conv_id)))


def compose(conv: dict, msgs: list[dict], first: bool) -> str:
    who = conv["visitor_name"] or "访客"
    if conv.get("visitor_contact"):
        who += f"（{conv['visitor_contact']}）"
    head = f"【{conv['site_name']}】#{conv['id']} {who}"
    body = "\n".join("[图片，见下一条]" if m["msg_type"] == "image" else m["content"] for m in msgs)
    if first:
        return (f"{head}（新会话）\n页面：{conv['page_url'] or '—'}\n——\n{body}\n——\n"
                f"引用本条回复，或发「#{conv['id']} 内容」")
    return f"{head}：\n{body}"


async def flush_conversation(conv_id: int) -> bool:
    from .qq_delivery import flush_conversation as flush
    return await flush(conv_id)


async def flush_backlog():
    from .qq_delivery import flush_backlog as flush
    await flush()


_backlog_wake: asyncio.Event | None = None


def wake_backlog():
    if _backlog_wake is not None:
        _backlog_wake.set()


async def _backlog_loop():
    global _backlog_wake

    _backlog_wake = asyncio.Event()
    try:
        while True:
            _backlog_wake.clear()
            try:
                if agent()["openid"]:
                    await flush_backlog()
            except Exception as exc:
                log(f"积压重试异常：{type(exc).__name__}")
            try:
                async with asyncio.timeout(30):
                    await _backlog_wake.wait()
            except TimeoutError:
                pass
    finally:
        _backlog_wake = None


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
    async def say(text):
        await deliver(text, openid=a["openid"], binding_version=a["binding_version"])

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
    if cmd in ("切换", "历史", "结束", "总结", "分类", "加标签", "普通") and not conv:
        return await say(f"会话 #{target} 不存在" if target else "请带上会话编号，例如 /历史 #12")
    if cmd == "切换":
        update_agent(current_conv_id=conv["id"], current_set_at=0)
        return await say("当前会话：" + _conv_line(conv, conv["id"]).lstrip("→"))
    if cmd in ("分类", "加标签", "普通"):
        from . import triage
        try:
            if cmd == "普通":
                await triage.dismiss(conv['id'])
                return await say(f"#{conv['id']} 已取消高优先级")
            if cmd == "加标签":
                item=triage.detail(conv['id'])['item']
                if not item or item['status']!='ready':
                    return await say('请先用 /分类 #编号 生成有效分类')
                await triage.apply_tags(conv['id'],item['id'])
                return await say(f"#{conv['id']} 已应用仍有效的标签建议")
            result=await triage.generate(conv['id'])
        except HTTPException as exc:
            return await say(str(exc.detail))
        if a.get('openid') and agent()['openid']!=a['openid']:
            return
        item=result['item']
        if item['status']!='ready':
            return await say(item['error'])
        text=(f"#{conv['id']} AI 分类（供核对）：{item['category_name']}\n"
              f"优先级：{'高（已撤销）' if item['dismissed'] else '高' if item['priority']=='high' else '普通'}\n"
              f"标签建议：{'、'.join(t['name'] for t in item['tag_suggestions']) or '无'}")
        for label,key in [('分类原话','category_evidence'),('优先级原话','priority_evidence')]:
            if item[key]:
                text+=f"\n{label}（消息 #{item[key]['message_id']}）：{item[key]['quote'][:200]}"
        text+='\n有新访客消息，请再次 /分类 更新。' if result['stale'] else '\n/加标签 #编号 应用建议；/普通 #编号 取消高优先级。'
        return await say(text)
    if cmd == "总结":
        from . import summaries
        try:
            result = await summaries.generate(conv["id"])
        except HTTPException as exc:
            return await say(str(exc.detail))
        if a.get("openid") and agent()["openid"] != a["openid"]:
            return  # Binding changed while the model was running.
        item = result["item"]
        text = summaries.qq_text(item) or item["error"]
        return await say(f"#{conv['id']} " + text + ("\n生成期间有新消息，可再次发送 /总结 更新。" if result["stale"] else ""))
    if cmd == "历史":
        msgs = chat.history(conv["id"], 10)
        who = {"visitor": "访客", "agent": "客服", "system": "系统", "ai": "AI"}
        lines = [f"{m['created_at'][11:16]} {who[m['sender']]}：{m['content']}" for m in msgs]
        return await say(f"#{conv['id']} 最近消息：\n" + ("\n".join(lines) or "（无）"))
    if cmd == "结束":
        if conv["status"] != "closed":
            await chat.set_conversation_status(conv["id"], "closed")
        return await say(f"#{conv['id']} 已结束")
    return await say("未知指令。\n\n" + HELP)


def _download(url: str) -> bytes:
    """下载客服在 QQ 里发的图片（平台地址会过期，所以存到本地）。"""
    if url.startswith("//"):
        url = "https:" + url
    elif not url.startswith(("http://", "https://")):
        url = "https://" + url
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 MsgHub"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = r.read(media.MAX_IMAGE_BYTES + 1)
    if len(data) > media.MAX_IMAGE_BYTES:
        raise ValueError("图片超过 5MB")
    return data


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


def _resolve_reply(d: dict, content: str, has_images: bool, inbound: dict, owner: dict):
    """Return a conversation, text, whether the reply is implicit and any rejection message."""
    refs = quoted_refs(d)
    targets = set()
    if refs:
        with get_db() as db:
            marks = ",".join("?" for _ in refs)
            targets = {row[0] for row in db.execute(
                f"SELECT DISTINCT conversation_id FROM qq_forwards WHERE qq_msg_id IN ({marks})", refs
            )}
    explicit = re.fullmatch(r"#(\d+)(?:\s+(.*))?", content, re.S)
    text = (explicit.group(2) or "").strip() if explicit else content
    if explicit and not text and not has_images:
        return None, text, False, f"请在 #{explicit.group(1)} 后面写上要回复的内容"
    if explicit and len(explicit.group(1)) > 19:
        return None, text, False, "会话编号无效"
    target = int(explicit.group(1)) if explicit else None
    if target is not None and not 0 < target <= (1 << 63) - 1:
        return None, text, False, "会话编号无效"
    if len(targets) > 1 or (targets and target is not None and target not in targets):
        return None, text, False, "引用消息与回复目标不一致，未发送。请单独用「#编号 内容」回复。"
    if target is not None or targets:
        return target if target is not None else targets.pop(), text, False, ""
    if refs or d.get("message_reference"):
        return None, text, False, "找不到引用消息对应的会话，未发送。请用「#编号 内容」明确指定。"
    target = inbound["default_conv_id"]
    if not target or not owner["current_conv_id"]:
        return None, text, True, "当前没有可回复的会话。发 /列表 查看，或用「#编号 内容」指定。"
    set_at = inbound["default_set_at"]
    if (owner["current_conv_id"] != target or owner["current_set_at"] != set_at
            or (set_at and inbound["received_at"] - set_at < SWITCH_GUARD_SECONDS)):
        return None, text, True, "当前会话刚切换，未发送。请引用原消息，或用「#编号 内容」回复。"
    return target, text, True, ""


async def _bind_from_inbound(openid: str, msg_id: str, code: str, inbound: dict):
    from .qq_delivery import queue_lock, cancel_for_binding_change

    version = None
    async with queue_lock, _send_lock:
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = db.execute("SELECT * FROM qq_agent WHERE id=1").fetchone()
            valid = (owner["binding_version"] == inbound["binding_version"] and owner["bind_code"]
                     and secrets.compare_digest(owner["bind_code"], code) and time.time() < owner["bind_expires"])
            qq_inbound.finish(db, inbound["id"])
            if valid:
                version = owner["binding_version"]
                if owner["openid"] != openid:
                    version += 1
                    cancel_for_binding_change(db=db)
                    update_agent(db=db, current_conv_id=None, current_set_at=0, binding_version=version)
                update_agent(db=db, openid=openid, bound_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                             bind_code="", bind_expires=0, last_msg_id=msg_id,
                             last_msg_at=inbound["received_at"], passive_used=0)
    if version is None:
        await reply_to(openid, msg_id, "绑定码无效或已过期，请在 MsgHub 后台重新生成。")
    else:
        log("客服绑定成功")
        await deliver("绑定成功！网站访客的消息之后会转发到这里。\n\n" + HELP,
                      openid=openid, binding_version=version)
        wake_backlog()


async def handle_c2c(d: dict, *, inbound: dict):
    author = d.get("author")
    openid = str(author.get("user_openid") or "") if isinstance(author, dict) else ""
    content = str(d.get("content") or "").strip()
    msg_id = str(d.get("id") or "")
    if not openid:
        return
    owner = agent()
    version = inbound["binding_version"]
    if owner["binding_version"] != version:
        return
    bind = re.fullmatch(r"/?绑定\s*([0-9]{6})", content)
    if bind:
        await _bind_from_inbound(openid, msg_id, bind.group(1), inbound)
        return
    if openid != owner["openid"]:
        await reply_to(openid, msg_id, "这是网站客服机器人，仅供已绑定的客服使用。")
        return

    received_at = inbound["received_at"]
    if msg_id and received_at > owner["last_msg_at"] and time.time() - received_at < QQ_PASSIVE_WINDOW:
        update_agent(last_msg_id=msg_id, last_msg_at=received_at, passive_used=0)
        wake_backlog()

    async def tell(text):
        await deliver(text, openid=openid, binding_version=version)

    attachments = [x for x in (d.get("attachments") or []) if isinstance(x, dict)]
    images = [x for x in attachments if str(x.get("content_type", "")).startswith("image")]
    warnings = []
    if len(images) < len(attachments):
        warnings.append("暂不支持把文件 / 语音 / 视频转给访客，请发送文字或图片。")
    if not content and not images:
        if warnings:
            await tell(warnings[0])
        return
    if content.startswith("/") and not images:
        # Commands with an omitted conversation number keep the target at acceptance time.
        await handle_command(content, {**owner, "current_conv_id": inbound["default_conv_id"]})
        return

    target, text, implicit, error = _resolve_reply(d, content, bool(images), inbound, owner)
    if inbound["target_conv_id"] is not None:
        target, implicit, error = inbound["target_conv_id"], bool(inbound["implicit"]), ""
    if error:
        await tell(error)
        return
    if not chat.conv_summary(target):
        await tell(f"会话 #{target} 不存在")
        return
    qq_inbound.pin_reply(inbound, target, implicit)

    contents = []
    messages = []
    try:
        for img in images:
            try:
                data = await asyncio.to_thread(_download, str(img.get("url") or ""))
                path = media.save_image(data)
            except (urllib.error.URLError, TimeoutError) as exc:
                retryable = not isinstance(exc, urllib.error.HTTPError) or exc.code in (408, 429) or exc.code >= 500
                if retryable and inbound["attempts"] + 1 < qq_inbound.MAX_ATTEMPTS:
                    raise
                warnings.append(f"图片没能发给 #{target}，请重新发送。")
            except ValueError:
                warnings.append(f"图片没能发给 #{target}，请重新发送。")
            else:
                contents.append(("image", path))
        if text:
            contents.append(("text", text))
        messages, error = qq_inbound.commit_reply(inbound, openid, target, implicit, contents)
    finally:
        if not messages:
            # A cancelled or rolled-back reply has no message referencing these new files.
            for kind, content in contents:
                if kind == "image":
                    path = media.local_path(content)
                    if path:
                        try:
                            path.unlink(missing_ok=True)
                        except OSError:
                            log("未使用的 QQ 图片清理失败")
    for message in messages:
        if not chat.conv_summary(target):
            break
        await chat.broadcast_message(target, message)
    if messages and chat.conv_summary(target):
        await chat.hub.to_visitor(target, {"type": "status", "status": "active"})
    if error:
        warnings.append(error)
    if implicit and messages:
        warnings.append(f"✓ 已发给 #{target}")
    if warnings:
        await tell("\n".join(warnings))


# ---------------------------------------------------------------------------
# 平台回调
# ---------------------------------------------------------------------------

@router.post("/api/qq/callback", include_in_schema=False)
async def callback(request: Request):
    if not ENABLED:
        raise HTTPException(404)
    body = await request.body()
    try:
        payload = json.loads(body)
    except ValueError:
        return JSONResponse({"error": "invalid json"}, 400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "invalid event"}, 400)
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

    if op == 0 and payload.get("t") == "C2C_MESSAGE_CREATE":
        data = payload.get("d")
        if not isinstance(data, dict):
            return JSONResponse({"error": "invalid event"}, 400)
        qq_inbound.accept(QQ_APP_ID, str(payload.get("id") or ""), data)
    else:
        update_agent(last_event_at=time.strftime("%Y-%m-%d %H:%M:%S"))
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
        token_ok, token_error = False, safe_error(e)
    with get_db() as db:
        pending = db.execute("SELECT COUNT(*) FROM chat_messages WHERE qq_state = 'pending'").fetchone()[0]
        inbound_pending, inbound_failed = db.execute(
            "SELECT COUNT(CASE WHEN status='pending' THEN 1 END),COUNT(CASE WHEN status='failed' THEN 1 END) "
            "FROM qq_inbound WHERE status IN ('pending','failed') AND app_id=?", (QQ_APP_ID,)
        ).fetchone()
        priority_pending = db.execute("SELECT COUNT(*) FROM qq_deliveries WHERE kind='priority' AND status NOT IN ('sent','cancelled')").fetchone()[0]
        handoffs_pending = db.execute("SELECT COUNT(*) FROM qq_deliveries WHERE kind='handoff' AND status NOT IN ('sent','cancelled')").fetchone()[0]
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
        "handoffs_pending": handoffs_pending,
        "priority_pending": priority_pending,
        "inbound_pending": inbound_pending,
        "inbound_failed": inbound_failed,
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
async def unbind():
    from .qq_delivery import queue_lock, cancel_for_binding_change
    async with queue_lock, _send_lock:
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("SELECT binding_version FROM qq_agent WHERE id=1").fetchone()[0]
            update_agent(db=db, openid="", bound_at=None, last_msg_id="", last_msg_at=0, passive_used=0,
                         current_conv_id=None, current_set_at=0, binding_version=version + 1, bind_code="", bind_expires=0)
            cancel_for_binding_change(db=db)
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
    from .qq_delivery import recover
    recover()
    if not ENABLED:
        log("未配置 QQ_APP_ID / QQ_APP_SECRET，QQ 转发未启用")
        return
    chat.visitor_message_hooks.append(on_visitor_message)
    # 客服在 QQ 发 /忙 后视为不在岗：访客看到离线提示，消息交给 AI / 转留言
    chat.agent_available = lambda: (lambda a: not a["openid"] or bool(a["online"]))(agent())
    auto.notify_agent = notify
    _spawn(_backlog_loop())
    _spawn(qq_inbound.run())
    log(f"已启用，AppID {QQ_APP_ID}")

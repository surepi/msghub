"""AI first response, human handoff, unattended archiving and site crawling.

Configured sites start AI after a short merge window. Human replies take over the
conversation. Sites without AI still archive unanswered messages on the existing schedule.
"""
import asyncio
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from collections import defaultdict
from dataclasses import dataclass, field
from html.parser import HTMLParser

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from . import auth, chat
from .common import DEFAULT_OFFLINE, in_work_hours
from .db import get_db, get_setting, rows, set_settings

router = APIRouter()
admin = Depends(auth.require_admin)

HANDOFF = "[转人工]"
HANDOFF_TEXT = "已为您转人工，客服可以在这里回复。您也可以留下联系方式，方便后续联系。"
TIMEOUT_TEXT = "客服暂时没能及时回复，已为您记录留言，我们会尽快通过您留下的联系方式联系您。"
MERGE_SECONDS = 2
LIVE_REPLY_SECONDS = 12
LIVE_SUMMARY_SECONDS = 3
NOTICE_COOLDOWN = 30 * 60      # 同一会话的"已转留言"提示 30 分钟内只发一次

CRAWL_MAX_PAGES = 50
CRAWL_MAX_DEPTH = 3
CRAWL_MAX_BYTES = 2 * 1024 * 1024
CRAWL_INTERVAL = 24 * 3600
PAGE_TEXT_LIMIT = 20000
CHUNK = 400
UA = "MsgHub-Crawler/1.0"


def log(text: str):
    print(f"[auto] {text}", flush=True)


# 客服通知（qq 模块启用时替换为发给客服 QQ）
async def _no_notify(text: str, conv_id: int):
    return None

notify_agent = _no_notify


# ---------------------------------------------------------------------------
# 设置
# ---------------------------------------------------------------------------

def ai_settings() -> dict:
    return {
        "enabled": get_setting("ai_enabled", "0") == "1",
        "triage_enabled": get_setting("ai_triage_enabled", "0") == "1",
        "auto_tags": get_setting("ai_auto_tags", "0") == "1",
        "base_url": get_setting("ai_base_url", ""),
        "api_key": get_setting("ai_api_key", ""),
        "model": get_setting("ai_model", ""),
        "prompt": get_setting("ai_prompt", ""),
        "no_reply_minutes": int(get_setting("no_reply_minutes", "3") or 3),
    }


def ai_configured(settings: dict) -> bool:
    return settings["enabled"] and bool(settings["base_url"] and settings["api_key"] and settings["model"])


def ai_ready(site: dict) -> bool:
    return bool(site.get("enabled", 1) and site.get("ai_enabled", 1) and ai_configured(ai_settings()))


# ---------------------------------------------------------------------------
# 检索：问答库 + 抓取页面，按中文二字组 / 英文单词重合度打分
# ---------------------------------------------------------------------------

def _grams(text: str) -> set[str]:
    s = re.sub(r"\s+", "", text.lower())
    return {s[i:i + 2] for i in range(len(s) - 1)} | set(re.findall(r"[a-z0-9]{2,}", s))


def retrieve_sources(site_id: int, query: str, limit_chars: int = 3500) -> tuple[str, list[dict]]:
    q = _grams(query)
    if not q:
        return "", []
    with get_db() as db:
        qa = rows(db.execute("SELECT id,site_id,question,answer FROM qa WHERE site_id IS NULL OR site_id = ? ORDER BY id", (site_id,)))
        pages = rows(db.execute("SELECT url,title,text,fetched_at FROM kb_pages WHERE site_id = ? ORDER BY url", (site_id,)))
    scored = []
    for item in qa:
        s = 2 * len(q & _grams(item["question"])) + len(q & _grams(item["answer"]))
        if s:
            scored.append((s + 3, f"问：{item['question']}\n答：{item['answer']}",
                           {'type':'qa','id':item['id'],'site_id':item['site_id'],'title':item['question'][:200],
                            'url':'','scope':'全局' if item['site_id'] is None else '本站'}))
    for p in pages:
        t = p["text"]
        for i in range(0, len(t), CHUNK):
            chunk = t[max(0, i - 50):i + CHUNK]
            s = len(q & _grams(chunk))
            if s >= 2:
                scored.append((s, f"（网页：{p['title'] or p['url']}）{chunk}",
                               {'type':'page','site_id':site_id,'title':(p['title'] or p['url'])[:200],
                                'url':p['url'],'fetched_at':p['fetched_at'],'offset':max(0,i-50),'scope':'本站'}))
    scored.sort(key=lambda x: -x[0])
    out, sources, used = [], [], 0
    for score, text, source in scored[:8]:
        prefix = f'[资料{len(sources)+1}] '
        remaining = limit_chars - used - (2 if out else 0) - len(prefix)
        if remaining < 20:
            break
        snippet = text[:remaining]
        part = prefix + snippet
        sources.append({**source,'ref':len(sources)+1,'score':score,'snippet':snippet,'truncated':len(snippet)<len(text)})
        used += len(part) + (2 if out else 0)
        out.append(part)
    return '\n\n'.join(out), sources


def retrieve(site_id: int, query: str, limit_chars: int = 3500) -> str:
    return retrieve_sources(site_id, query, limit_chars)[0]


# ---------------------------------------------------------------------------
# 大模型（OpenAI 兼容接口）
# ---------------------------------------------------------------------------

class AIError(Exception):
    evidence = None


REASONS = {
    'answered':'AI 已回答', 'insufficient_sources':'模型判断资料不足',
    'visitor_requested':'模型判断访客要求人工', 'human_required':'模型判断需人工办理',
    'model_handoff':'AI 判断需要人工处理（未提供具体分类）',
    'api_error':'AI 服务暂不可用', 'reply_timeout':'AI 回复超时', 'empty_answer':'AI 未返回有效回答',
    'first_response_expired':'已超过即时接待时间，交由人工跟进',
    'human_pending':'已转人工，等待客服接待', 'human_active':'人工客服已接管该会话',
    'disabled':'AI 自动应答未启用', 'unconfigured':'AI 自动应答配置未完成',
}
HANDOFF_CODES = {'资料不足':'insufficient_sources','访客要求':'visitor_requested','需人工办理':'human_required'}
HANDOFF_PATTERN = re.compile(r'\[转人工(?:\s*[:：]\s*([^\]]*))?\s*\]')


@dataclass
class AIAnswer:
    answer: str
    handoff: bool
    reason_code: str
    evidence: dict = field(default_factory=dict)

    def __iter__(self):
        # Preserve the existing (answer, handoff) caller contract.
        return iter((self.answer, self.handoff))


def parse_answer(text: str, evidence: dict) -> AIAnswer:
    matches = list(HANDOFF_PATTERN.finditer(text))
    code = HANDOFF_CODES.get((matches[0].group(1) or '').strip(), 'model_handoff') if matches else 'answered'
    answer = HANDOFF_PATTERN.sub('', text)
    if '[转人工' in answer:  # A provider may stop in the middle of its marker.
        code = 'model_handoff' if code == 'answered' else code
        answer = answer.split('[转人工',1)[0]
    answer = answer.strip()[:5000]
    if code == 'answered' and not answer:
        code = 'empty_answer'
    return AIAnswer(answer, code != 'answered', code, evidence)


def _completion(base_url: str, api_key: str, model: str, messages: list[dict], timeout: int = 40) -> str:
    url = base_url.rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    body = json.dumps({"model": model, "messages": messages, "temperature": 0.3, "max_tokens": 800}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + api_key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode(errors="replace")
        raise AIError(f"HTTP {e.code}：{detail}")
    except Exception as e:  # noqa: BLE001
        raise AIError(f"请求失败：{e}")
    try:
        return str(data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        raise AIError(f"返回格式无法识别：{str(data)[:200]}")


def build_messages(site: dict, history: list[dict], context: str, extra: str) -> list[dict]:
    system = (
        f"你是「{site['name']}」网站的在线客服助手，正在网页上回答访客的问题。\n"
        "规则：\n"
        "1. 只根据下面【资料】中的内容回答，不要编造资料里没有的价格、政策、时间、联系方式等信息。\n"
        "2. 资料里找不到答案时只回复：[转人工:资料不足]；访客要求人工时只回复：[转人工:访客要求]；"
        "投诉、退款、下单付款等需要人工处理时只回复：[转人工:需人工办理]。不要编造转人工理由或解释思考过程。\n"
        "3. 访客只是打招呼或道谢时，可以简短礼貌地回应。\n"
        "4. 用中文回答，简洁友好，不超过 200 字，不要使用 Markdown 格式，不要在回答中输出资料编号。\n"
        + (f"5. {extra.strip()}\n" if extra.strip() else "")
        + f"\n【资料】\n{context or '（无）'}"
    )
    msgs = [{"role": "system", "content": system}]
    for m in history:
        if m["sender"] == "system":
            continue
        text = "[图片]" if m["msg_type"] == "image" else m["content"]
        role = "user" if m["sender"] == "visitor" else "assistant"
        if msgs[-1]["role"] == role and role == "user":
            msgs[-1]["content"] += "\n" + text
        else:
            msgs.append({"role": role, "content": text})
    return msgs


async def evaluate(site: dict, history: list[dict], question: str, settings: dict | None = None,
                   *, timeout: int = 40) -> AIAnswer:
    s = settings or ai_settings()
    context, sources = retrieve_sources(site['id'], question)
    evidence = {'context':context,'sources':sources,'model':s['model'][:200]}
    msgs = build_messages(site, history, context, s['prompt'])
    try:
        text = await asyncio.to_thread(_completion, s['base_url'], s['api_key'], s['model'], msgs, timeout)
    except AIError as exc:
        exc.evidence = evidence
        raise
    return parse_answer(text, evidence)


async def ai_answer(site: dict, conv_id: int, question: str) -> AIAnswer:
    return await evaluate(site, chat.history(conv_id, 12), question, timeout=LIVE_REPLY_SECONDS + 1)


def save_decision(db, conv_id, upto, trigger, question, answer, reason_code, evidence,
                  *, message_id=None, inbox_id=None, delivery_id=None):
    """Persist only bounded evidence and explicit outcomes, never provider errors/secrets/prompts."""
    outcome = 'answered' if reason_code == 'answered' else 'error' if reason_code in ('api_error','reply_timeout','empty_answer','first_response_expired') else 'disabled' if reason_code in ('disabled','unconfigured') else 'handoff'
    origin = '模型回答' if reason_code == 'answered' else '模型分类' if reason_code in ('insufficient_sources','visitor_requested','human_required','model_handoff') else '系统状态'
    sources = evidence.get('sources', [])
    return db.execute(
        'INSERT INTO ai_decisions (conversation_id,source_upto,message_id,inbox_message_id,qq_delivery_id,outcome,trigger,reason_code,reason,reason_origin,model,question,question_truncated,answer,context,sources,source_count) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (conv_id,upto,message_id,inbox_id,delivery_id,outcome,trigger,reason_code,REASONS[reason_code],origin,
         evidence.get('model','')[:200],question[:6000],int(len(question)>6000),answer[:5000],evidence.get('context',''),json.dumps(sources,ensure_ascii=False),len(sources))).lastrowid


def _decision_conv(db, conv_id):
    if not db.execute('SELECT 1 FROM conversations WHERE id=?',(conv_id,)).fetchone():
        raise HTTPException(404,'会话不存在')


@router.get('/api/admin/conversations/{conv_id}/ai-decisions', tags=['AI'], dependencies=[admin])
def list_decisions(conv_id: int, page: int = Query(default=1,ge=1,le=1000000), size: int = Query(default=20,ge=1,le=100),
                   message_id: int | None = Query(default=None,gt=0), delivery_id: int | None = Query(default=None,gt=0)):
    with get_db() as db:
        _decision_conv(db,conv_id)
        where,args = 'conversation_id=?',[conv_id]
        if message_id:
            if not db.execute("SELECT 1 FROM chat_messages WHERE id=? AND conversation_id=? AND sender='ai'",(message_id,conv_id)).fetchone():
                raise HTTPException(404,'AI 消息不存在或不属于此会话')
            where += ' AND message_id=?'; args.append(message_id)
        if delivery_id:
            if not db.execute('SELECT 1 FROM qq_deliveries WHERE id=? AND conversation_id=?',(delivery_id,conv_id)).fetchone():
                raise HTTPException(404,'投递记录不存在或不属于此会话')
            where += ' AND qq_delivery_id=?'; args.append(delivery_id)
        total = db.execute('SELECT COUNT(*) FROM ai_decisions WHERE '+where,args).fetchone()[0]
        items = rows(db.execute('SELECT id,conversation_id,message_id,qq_delivery_id,inbox_message_id,outcome,trigger,reason_code,reason,reason_origin,model,source_count,created_at '
                                'FROM ai_decisions WHERE '+where+' ORDER BY id DESC LIMIT ? OFFSET ?',(*args,size,(page-1)*size)))
    return {'items':items,'total':total,'page':page,'size':size}


@router.get('/api/admin/conversations/{conv_id}/ai-decisions/{decision_id}', tags=['AI'], dependencies=[admin])
def decision_detail(conv_id: int, decision_id: int):
    with get_db() as db:
        row = db.execute('SELECT * FROM ai_decisions WHERE id=? AND conversation_id=?',(decision_id,conv_id)).fetchone()
    if not row:
        raise HTTPException(404,'AI 处理记录不存在')
    result = dict(row)
    result['sources'] = json.loads(result['sources'])
    return result


# ---------------------------------------------------------------------------
# 无人值守处理
# ---------------------------------------------------------------------------

_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
_timers: dict[int, asyncio.TimerHandle] = {}
_tasks: set[asyncio.Task] = set()
_running: set[int] = set()
_live_slots = asyncio.Semaphore(4)


def _spawn(coro):
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


def _unhandled(conv_id: int) -> list[dict]:
    """客服最后一次回复之后、且尚未被自动处理的访客消息。"""
    with get_db() as db:
        return rows(db.execute(
            "SELECT m.* FROM chat_messages m JOIN conversations c ON c.id = m.conversation_id "
            "WHERE m.conversation_id = ? AND m.sender = 'visitor' AND m.id > c.auto_handled_upto "
            "AND m.id > COALESCE((SELECT MAX(id) FROM chat_messages WHERE conversation_id = ? AND sender = 'agent'), 0) "
            "ORDER BY m.id", (conv_id, conv_id)))


def archive(db, conv: dict, msgs: list[dict], note: str):
    """记为留言（同一会话 24 小时内未处理的留言直接追加，避免刷屏）。"""
    lines = [f"[图片] {m['content']}" if m["msg_type"] == "image" else m["content"] for m in msgs]
    text = "\n".join(lines) + f"\n（{note}，来自对话 #{conv['id']}）"
    v = db.execute("SELECT * FROM visitors WHERE id = ?", (conv["visitor_id"],)).fetchone()
    existing = db.execute(
        "SELECT id, content FROM messages WHERE conversation_id = ? AND status = 'new' "
        "AND created_at >= datetime('now','localtime','-1 day') ORDER BY id DESC LIMIT 1", (conv["id"],)).fetchone()
    if existing:
        db.execute("UPDATE messages SET content = ?, updated_at = datetime('now','localtime') WHERE id = ?",
                   ((existing["content"] + "\n\n" + text)[-5000:], existing["id"]))
        return existing['id']
    return db.execute(
        "INSERT INTO messages (site_id, name, contact, content, page_url, ip, user_agent, conversation_id) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (conv["site_id"], v["name"], v["contact"], text[-5000:], conv["page_url"], v["ip"], v["user_agent"], conv["id"])).lastrowid


def _recent_notice(db, conv_id: int) -> bool:
    return bool(db.execute(
        "SELECT 1 FROM chat_messages WHERE conversation_id=? AND sender='system' "
        "AND created_at >= datetime('now','localtime', ?) "
        "AND id > COALESCE((SELECT MAX(id) FROM chat_messages WHERE conversation_id=? "
        "AND sender='system' AND content=?),0) LIMIT 1",
        (conv_id, f"-{NOTICE_COOLDOWN // 60} minutes", conv_id, chat.CONVERSATION_CLOSED_TEXT)
    ).fetchone())


async def _live_answer(site: dict, conv_id: int, question: str, upto: int, *, show_hint: bool):
    if show_hint:
        await chat.hub.to_visitor(conv_id, {"type": "ai_status", "thinking": True})
    try:
        async with asyncio.timeout(LIVE_REPLY_SECONDS):
            async with _live_slots:
                # The agent may have taken over while this request waited for a model slot.
                conv = chat.conv_summary(conv_id)
                if (not conv or conv["status"] != "waiting" or conv["ai_waiting_for_agent"]
                        or conv["auto_handled_upto"] >= upto):
                    return None
                return await ai_answer(site, conv_id, question)
    finally:
        if show_hint:
            await chat.hub.to_visitor(conv_id, {"type": "ai_status", "thinking": False})


async def handle_unattended(conv_id: int, reason: str):
    """Respond early when AI is in charge, or archive an unanswered visitor batch."""
    async with _locks[conv_id]:
        conv = chat.conv_summary(conv_id)
        if not conv or conv["status"] == "closed" or (conv["status"] == "active" and reason not in ("timeout", "timeout_archive")):
            return
        msgs = _unhandled(conv_id)
        if not msgs:
            return
        site = chat.get_site(conv["site_id"])
        if not site or not site["enabled"]:
            return
        waiting_for_agent = bool(conv["ai_waiting_for_agent"])
        human_active = conv["status"] == "active"
        eligible_ai = not waiting_for_agent and not human_active and ai_ready(site)
        enabled = eligible_ai and reason != "timeout_archive"
        if reason == "ai_first" and not enabled and not waiting_for_agent:
            if chat.site_online(site):
                return
            reason = "offline"
        if reason == "offline":
            if enabled:
                reason = "ai_first"
            elif chat.site_online(site):
                return
        if waiting_for_agent:
            note = "等待人工接待"
        elif reason == "ai_first":
            note = "AI 快速接待"
        elif reason == "offline":
            note = "非工作时间" if not in_work_hours(site.get("work_hours", "")) else "客服忙碌"
        else:
            note = "客服未及时回复"
        answer, handoff = '', True
        reason_code, evidence = "disabled", {}
        if waiting_for_agent:
            reason_code = "human_pending"
        elif human_active:
            reason_code = "human_active"
        elif eligible_ai and reason == "timeout_archive":
            reason_code = "first_response_expired"
        question = "\n".join("[图片]" if m["msg_type"] == "image" else m["content"] for m in msgs)
        if not enabled and reason_code == 'disabled' and ai_settings()['enabled'] and site.get('ai_enabled', 1):
            reason_code = 'unconfigured'
        if enabled:
            try:
                result = await _live_answer(site, conv_id, question, msgs[-1]["id"], show_hint=reason == "ai_first")
                if result is None:
                    return
                answer, handoff = result
                evidence = getattr(result,'evidence',{})
                reason_code = getattr(result,'reason_code','model_handoff' if handoff else 'answered')
                if not answer and not handoff:
                    handoff, reason_code = True, 'empty_answer'
            except TimeoutError:
                reason_code = "reply_timeout"
                evidence = {"model": ai_settings()["model"][:200]}
            except AIError as exc:
                log(f"#{conv_id} AI 调用失败，改为转留言")
                answer, handoff = "", True
                reason_code, evidence = 'api_error', exc.evidence or {}
        starting_status = "active" if human_active else "waiting"
        from . import summaries
        summary = None
        if handoff and enabled and reason_code not in ("api_error", "reply_timeout"):
            try:
                if reason == "ai_first":
                    async with asyncio.timeout(LIVE_SUMMARY_SECONDS):
                        summary = await summaries.prepare(conv_id, msgs[-1]["id"])
                else:
                    summary = await summaries.prepare(conv_id, msgs[-1]["id"])
            except TimeoutError:
                pass  # The original text is sufficient to notify the agent immediately.
            except HTTPException as exc:
                if exc.status_code == 404:
                    return
                raise
        cause = REASONS[reason_code]
        # AI may take seconds; never create a stale handoff after the agent has replied/closed.
        conv = chat.conv_summary(conv_id)
        if not conv:
            return
        from . import qq_delivery
        delivery_id, inbox_id, ai_message, notice = None, None, None, None
        with get_db() as db:
            db.execute('BEGIN IMMEDIATE')
            state = db.execute(
                "SELECT c.status,c.auto_handled_upto,s.enabled,s.ai_enabled FROM conversations c "
                "JOIN sites s ON s.id=c.site_id WHERE c.id=?", (conv_id,)).fetchone()
            last_agent = db.execute("SELECT MAX(id) FROM chat_messages WHERE conversation_id=? AND sender='agent'", (conv_id,)).fetchone()[0] or 0
            upto = msgs[-1]['id']
            if (not state or not state["enabled"] or state["status"] != starting_status
                    or state["status"] == "closed" or state["auto_handled_upto"] >= upto or last_agent >= upto):
                return
            if enabled and not ai_ready({"enabled": state["enabled"], "ai_enabled": state["ai_enabled"]}):
                return
            if handoff:
                summary = summaries.save(db, summary)
                detail = note + '；' + cause
                inbox_id = archive(db, conv, msgs, detail)
                if not waiting_for_agent:
                    delivery_id = qq_delivery.enqueue_handoff(db, conv, msgs, detail, inbox_id, answer, summary=summary)
                if not waiting_for_agent and (not answer or enabled) and not _recent_notice(db, conv_id):
                    text = HANDOFF_TEXT if enabled else ((site.get("offline_text") or DEFAULT_OFFLINE) if reason == "offline" else TIMEOUT_TEXT)
                    notice = chat.add_message(conv_id, "system", text, db=db)
            if answer:
                ai_message = chat.add_message(conv_id,'ai',answer,db=db)
            decision_id = save_decision(db,conv_id,upto,note,question,answer,reason_code,evidence,
                                        message_id=ai_message['id'] if ai_message else None,inbox_id=inbox_id,delivery_id=delivery_id)
            # Archive, notification snapshot and progress commit together, before network I/O.
            db.execute(
                "UPDATE conversations SET auto_handled_upto=?,"
                "ai_waiting_for_agent=CASE WHEN ? THEN 1 ELSE ai_waiting_for_agent END WHERE id=?",
                (upto, handoff and (enabled or reason_code == "first_response_expired"), conv_id),
            )
        if handoff and (enabled or reason_code == "first_response_expired"):
            await chat.hub.to_visitor(conv_id, {"type": "ai_status", "thinking": False, "handoff": True})
        if ai_message:
            await chat.broadcast_message(conv_id, ai_message)
        if notice:
            await chat.broadcast_message(conv_id, notice)
        await chat.hub.to_admins({'type':'ai_decision_changed','conversation_id':conv_id,'decision_id':decision_id})
        if handoff:
            if summary:
                await chat.hub.to_admins({'type':'conversation_summary_changed','conversation_id':conv_id})
            await chat.hub.to_admins({'type': 'inbox_changed', 'conversation_ids': [conv_id]})
            if delivery_id:
                await qq_delivery._changed(delivery_id, conv_id)
                if reason == "ai_first":
                    from . import qq
                    qq.wake_backlog()
                else:
                    await qq_delivery.flush_conversation(conv_id)
        elif answer:
            reminder = notify_agent(f"🤖 AI 已回复 #{conv_id}：{answer}", conv_id)
            if reason == "ai_first":
                _spawn(reminder)
            else:
                await reminder
        log(f"#{conv_id} {note}：{'已记为留言' if handoff else 'AI 已回答'}")


async def _run_scheduled(cid: int, reason: str):
    try:
        await handle_unattended(cid, reason)
    except Exception as exc:
        log(f"#{cid} 自动接待异常：{type(exc).__name__}")
    finally:
        _running.discard(cid)


def _schedule(cid: int, reason: str, delay: float):
    if cid in _timers or cid in _running:
        return

    def start():
        _timers.pop(cid, None)
        _running.add(cid)
        _spawn(_run_scheduled(cid, reason))

    _timers[cid] = asyncio.get_running_loop().call_later(delay, start)


async def on_visitor_message(conv: dict, msg: dict):
    if not conv or conv["status"] != "waiting":
        return
    site = chat.get_site(conv["site_id"])
    if not site or not site["enabled"]:
        return
    if conv["ai_waiting_for_agent"] or ai_ready(site):
        _schedule(conv["id"], "ai_first", MERGE_SECONDS)
    elif not chat.site_online(site):
        _schedule(conv["id"], "offline", MERGE_SECONDS)


# The cursor also covers interruptions after the visitor message was committed,
# including a process restart before the websocket hook could set its timer.
_WAITING_MESSAGES = """
    SELECT c.id, MAX(m.created_at) AS latest_at
    FROM conversations c
    JOIN sites s ON s.id=c.site_id
    JOIN chat_messages m ON m.conversation_id=c.id AND m.sender='visitor'
    WHERE c.status!='closed' AND s.enabled=1 AND m.id>c.auto_handled_upto
      AND m.id>COALESCE((SELECT MAX(a.id) FROM chat_messages a
                         WHERE a.conversation_id=c.id AND a.sender='agent'),0)
"""


async def _recover_ai_loop():
    while True:
        await asyncio.sleep(MERGE_SECONDS)
        try:
            if not ai_configured(ai_settings()):
                continue
            with get_db() as db:
                recent = [row[0] for row in db.execute(
                    _WAITING_MESSAGES + " AND c.status='waiting' AND (s.ai_enabled=1 OR c.ai_waiting_for_agent=1) "
                    "AND m.created_at >= datetime('now','localtime','-2 minutes') "
                    "GROUP BY c.id HAVING latest_at <= datetime('now','localtime', ?) ORDER BY latest_at LIMIT 100",
                    (f"-{MERGE_SECONDS + 1} seconds",))]
            for cid in recent:
                _schedule(cid, "ai_first", 0)
        except Exception as exc:
            log(f"AI 接待恢复检查异常：{type(exc).__name__}")


async def _timeout_loop():
    while True:
        await asyncio.sleep(20)
        try:
            settings = ai_settings()
            with get_db() as db:
                pending = rows(db.execute(
                    _WAITING_MESSAGES + " GROUP BY c.id HAVING MIN(m.created_at)<=datetime('now','localtime', ?) LIMIT 100",
                    (f"-{settings['no_reply_minutes']} minutes",)))
            for row in pending:
                _schedule(row["id"], "timeout_archive", 0)
        except Exception as exc:
            log(f"超时检查异常：{type(exc).__name__}")


# ---------------------------------------------------------------------------
# 网站内容抓取
# ---------------------------------------------------------------------------

class _PageParser(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template", "iframe"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.parts, self.links = "", [], []
        self._skip, self._in_title = 0, False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)
        if tag in ("p", "br", "div", "li", "h1", "h2", "h3", "h4", "tr", "section"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data.strip()
        elif not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        t = re.sub(r"[ \t\r\f\v]+", " ", "".join(self.parts))
        return re.sub(r"\n\s*\n+", "\n", t).strip()[:PAGE_TEXT_LIMIT]


_SKIP_EXT = re.compile(r"\.(pdf|jpe?g|png|gif|webp|svg|zip|rar|7z|gz|mp4|mp3|avi|mov|exe|dmg|apk|doc|docx|xls|xlsx|ppt|pptx|css|js|ico|woff2?)$", re.I)


def _fetch(url: str) -> tuple[str, str, bytes]:
    """返回 (最终 URL, content-type, 内容)。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*;q=0.5"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.geturl(), r.headers.get("Content-Type", ""), r.read(CRAWL_MAX_BYTES)


def _decode(ctype: str, raw: bytes) -> str:
    m = re.search(r"charset=([\w-]+)", ctype, re.I) or re.search(rb'charset=["\']?([\w-]+)', raw[:2048], re.I)
    enc = m.group(1) if m else "utf-8"
    enc = enc.decode() if isinstance(enc, bytes) else enc
    try:
        return raw.decode(enc, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def crawl_starts(site: dict) -> list[str]:
    starts = [u.strip() for u in (site.get("crawl_urls") or "").splitlines() if u.strip()]
    if not starts and site.get("domain"):
        d = site["domain"].strip()
        starts = [d if d.startswith("http") else f"https://{d}/"]
    return [u for u in starts if urllib.parse.urlparse(u).scheme in ("http", "https")]


def crawl(site: dict) -> dict:
    """同步执行（在线程中调用）。只抓起始地址所在的域名，遵守 robots.txt。"""
    starts = crawl_starts(site)
    if not starts:
        return {"ok": False, "error": "没有可抓取的地址：请在站点设置中填写域名或抓取起始地址"}
    hosts = {urllib.parse.urlparse(u).netloc for u in starts}
    robots: dict[str, urllib.robotparser.RobotFileParser] = {}

    def allowed(url: str) -> bool:
        p = urllib.parse.urlparse(url)
        rp = robots.get(p.netloc)
        if rp is None:
            rp = urllib.robotparser.RobotFileParser()
            try:
                _, _, raw = _fetch(f"{p.scheme}://{p.netloc}/robots.txt")
                rp.parse(raw.decode(errors="replace").splitlines())
            except Exception:
                rp.parse([])
            robots[p.netloc] = rp
        return rp.can_fetch(UA, url)

    queue, seen, pages, errors = [(u, 0) for u in starts], set(starts), [], 0
    while queue and len(pages) < CRAWL_MAX_PAGES:
        url, depth = queue.pop(0)
        if not allowed(url):
            continue
        try:
            final, ctype, raw = _fetch(url)
        except Exception:
            errors += 1
            continue
        if "html" not in ctype.lower():
            continue
        parser = _PageParser()
        try:
            parser.feed(_decode(ctype, raw))
        except Exception:
            continue
        text = parser.text()
        if len(text) >= 10:
            pages.append({"url": final, "title": parser.title[:200], "text": text})
        if depth < CRAWL_MAX_DEPTH:
            for href in parser.links:
                link = urllib.parse.urljoin(final, href).split("#")[0]
                p = urllib.parse.urlparse(link)
                if p.scheme in ("http", "https") and p.netloc in hosts and link not in seen and not _SKIP_EXT.search(p.path):
                    seen.add(link)
                    queue.append((link, depth + 1))
        time.sleep(0.3)
    if not pages:
        return {"ok": False, "error": f"没有抓到有效页面（失败 {errors} 次），已保留原有资料"}
    with get_db() as db:
        db.execute("DELETE FROM kb_pages WHERE site_id = ?", (site["id"],))
        db.executemany("INSERT OR REPLACE INTO kb_pages (site_id, url, title, text) VALUES (?,?,?,?)",
                       [(site["id"], p["url"], p["title"], p["text"]) for p in pages])
    set_settings({f"crawl_at_{site['id']}": str(int(time.time()))})
    return {"ok": True, "pages": len(pages), "errors": errors}


crawl_state: dict[int, dict] = {}


async def run_crawl(site: dict):
    sid = site["id"]
    if crawl_state.get(sid, {}).get("running"):
        return
    crawl_state[sid] = {"running": True}
    try:
        result = await asyncio.to_thread(crawl, site)
    except Exception as e:  # noqa: BLE001
        result = {"ok": False, "error": str(e)}
    crawl_state[sid] = {"running": False, **result, "finished_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    log(f"站点 {sid} 抓取完成：{result}")


async def _crawl_loop():
    await asyncio.sleep(60)
    while True:
        try:
            if ai_settings()["enabled"]:
                with get_db() as db:
                    sites = rows(db.execute("SELECT * FROM sites WHERE enabled = 1 AND ai_enabled = 1"))
                for site in sites:
                    last = int(get_setting(f"crawl_at_{site['id']}", "0") or 0)
                    if crawl_starts(site) and time.time() - last > CRAWL_INTERVAL:
                        await run_crawl(site)
        except Exception as e:  # noqa: BLE001
            log(f"定时抓取异常：{e!r}")
        await asyncio.sleep(3600)


# ---------------------------------------------------------------------------
# 后台接口
# ---------------------------------------------------------------------------

class AISettingsIn(BaseModel):
    enabled: bool
    triage_enabled: bool | None = None
    auto_tags: bool | None = None
    base_url: str = ""
    model: str = ""
    prompt: str = ""
    no_reply_minutes: int = 3
    api_key: str | None = None   # None = 不修改；"" = 清除


@router.get("/api/admin/ai/settings", tags=["AI"], dependencies=[admin])
def get_ai_settings():
    s = ai_settings()
    key = s.pop("api_key")
    return {**s, "has_key": bool(key), "key_hint": ("…" + key[-4:]) if len(key) > 8 else ("已设置" if key else "")}


@router.put("/api/admin/ai/settings", tags=["AI"], dependencies=[admin])
def put_ai_settings(body: AISettingsIn):
    if body.base_url and urllib.parse.urlparse(body.base_url).scheme not in ("http", "https"):
        raise HTTPException(400, "接口地址需以 http:// 或 https:// 开头")
    values = {
        "ai_enabled": "1" if body.enabled else "0",
        "ai_base_url": body.base_url.strip(),
        "ai_model": body.model.strip(),
        "ai_prompt": body.prompt.strip()[:2000],
        "no_reply_minutes": str(max(1, min(body.no_reply_minutes, 120))),
    }
    if body.triage_enabled is not None:
        values["ai_triage_enabled"] = "1" if body.triage_enabled else "0"
    if body.auto_tags is not None:
        values["ai_auto_tags"] = "1" if body.auto_tags else "0"
    if body.api_key is not None:
        values["ai_api_key"] = body.api_key.strip()
    set_settings(values)
    return {"ok": True}


class AITestIn(BaseModel):
    question: str = Field(min_length=1,max_length=5000)
    site_id: int | None = Field(default=None,gt=0)


@router.post("/api/admin/ai/test", tags=["AI"], dependencies=[admin])
async def test_ai(body: AITestIn):
    s = ai_settings()
    if not (s["base_url"] and s["api_key"] and s["model"]):
        raise HTTPException(400, "请先填写接口地址、API Key 和模型名称")
    site = chat.get_site(body.site_id) if body.site_id else None
    if body.site_id and not site:
        raise HTTPException(404,'站点不存在')
    site = site or {"id": 0, "name": "测试站点"}
    try:
        result = await evaluate(site,[{'sender':'visitor','msg_type':'text','content':body.question}],body.question,s)
    except AIError:
        raise HTTPException(502,'AI 服务暂不可用，请检查接口配置和服务状态')
    return {'answer':result.answer,'handoff':result.handoff,'reason_code':result.reason_code,
            'reason':REASONS[result.reason_code],**result.evidence}


class QAIn(BaseModel):
    question: str
    answer: str
    site_id: int | None = None


@router.get("/api/admin/qa", tags=["AI"], dependencies=[admin])
def list_qa(site_id: int | None = None):
    with get_db() as db:
        if site_id:
            return rows(db.execute("SELECT * FROM qa WHERE site_id = ? ORDER BY id DESC", (site_id,)))
        return rows(db.execute("SELECT * FROM qa ORDER BY id DESC"))


def _check_qa(body: QAIn):
    if not body.question.strip() or not body.answer.strip():
        raise HTTPException(400, "问题和答案都不能为空")


@router.post("/api/admin/qa", tags=["AI"], dependencies=[admin])
def create_qa(body: QAIn):
    _check_qa(body)
    with get_db() as db:
        cur = db.execute("INSERT INTO qa (site_id, question, answer) VALUES (?,?,?)",
                         (body.site_id or None, body.question.strip()[:500], body.answer.strip()[:3000]))
    return {"id": cur.lastrowid}


@router.put("/api/admin/qa/{qa_id}", tags=["AI"], dependencies=[admin])
def update_qa(qa_id: int, body: QAIn):
    _check_qa(body)
    with get_db() as db:
        db.execute("UPDATE qa SET site_id = ?, question = ?, answer = ? WHERE id = ?",
                   (body.site_id or None, body.question.strip()[:500], body.answer.strip()[:3000], qa_id))
    return {"ok": True}


@router.delete("/api/admin/qa/{qa_id}", tags=["AI"], dependencies=[admin])
def delete_qa(qa_id: int):
    with get_db() as db:
        db.execute("DELETE FROM qa WHERE id = ?", (qa_id,))
    return {"ok": True}


@router.get("/api/admin/kb/{site_id}", tags=["AI"], dependencies=[admin])
def kb_status(site_id: int):
    site = chat.get_site(site_id)
    if not site:
        raise HTTPException(404, "站点不存在")
    with get_db() as db:
        pages = rows(db.execute(
            "SELECT url, title, LENGTH(text) AS chars, fetched_at FROM kb_pages WHERE site_id = ? ORDER BY url", (site_id,)))
    return {"starts": crawl_starts(site), "pages": pages, "state": crawl_state.get(site_id, {})}


@router.post("/api/admin/kb/{site_id}/crawl", tags=["AI"], dependencies=[admin])
async def kb_crawl(site_id: int):
    site = chat.get_site(site_id)
    if not site:
        raise HTTPException(404, "站点不存在")
    if not crawl_starts(site):
        raise HTTPException(400, "请先在站点设置中填写域名或抓取起始地址")
    _spawn(run_crawl(site))
    return {"ok": True}


@router.delete("/api/admin/kb/{site_id}", tags=["AI"], dependencies=[admin])
def kb_clear(site_id: int):
    with get_db() as db:
        db.execute("DELETE FROM kb_pages WHERE site_id = ?", (site_id,))
    return {"ok": True}


def setup():
    chat.visitor_message_hooks.append(on_visitor_message)
    _spawn(_recover_ai_loop())
    _spawn(_timeout_loop())
    _spawn(_crawl_loop())

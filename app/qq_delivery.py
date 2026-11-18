"""Durable visitor-to-QQ deliveries. Single worker, serialized queue and per-part checkpoints."""
import asyncio
import re
import time
from contextlib import nullcontext

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from . import auth, chat, media
from .db import get_db, rows

router = APIRouter()
admin = Depends(auth.require_admin)
queue_lock = asyncio.Lock()
STATES = ('pending', 'sending', 'failed', 'uncertain', 'sent', 'cancelled')
RETRY_COOLDOWN = 10
MAX_BATCH_MESSAGES = 10
SELECT = (
    "SELECT d.id, d.conversation_id, d.kind, d.reason, d.summary, d.inbox_message_id, d.summary_id, d.triage_id, d.status, d.attempts, d.last_error, d.warning, "
    "d.last_attempt_at, d.next_attempt_at, d.created_at, d.updated_at, d.sent_at, "
    "s.id AS site_id, s.name AS site_name, "
    "((SELECT COUNT(*) FROM qq_delivery_messages dm WHERE dm.delivery_id=d.id) + "
    "(SELECT COUNT(*) FROM qq_notification_sources ns WHERE ns.delivery_id=d.id)) AS message_count, "
    "(SELECT COUNT(*) FROM qq_delivery_parts p WHERE p.delivery_id=d.id) AS part_count, "
    "(SELECT COUNT(*) FROM qq_delivery_parts p WHERE p.delivery_id=d.id AND p.status='sent') AS sent_parts, "
    "(SELECT COUNT(*) FROM qq_delivery_parts p WHERE p.delivery_id=d.id AND p.status='skipped') AS skipped_parts "
    "FROM qq_deliveries d JOIN conversations c ON c.id=d.conversation_id JOIN sites s ON s.id=c.site_id "
)


class ImageUnavailable(Exception):
    pass


def _finish(db, delivery_id):
    db.execute("UPDATE qq_deliveries SET status='sent', last_error='', next_attempt_at=0, "
               "sent_at=datetime('now','localtime'), updated_at=datetime('now','localtime') WHERE id=?", (delivery_id,))
    first = db.execute("SELECT qq_message_id FROM qq_delivery_parts WHERE delivery_id=? AND status='sent' AND qq_message_id!='' ORDER BY sequence LIMIT 1", (delivery_id,)).fetchone()
    db.execute("UPDATE chat_messages SET qq_state='sent', qq_message_id=? WHERE id IN "
               "(SELECT message_id FROM qq_delivery_messages WHERE delivery_id=?)", (first[0] if first else '', delivery_id))


def recover():
    """An interrupted request might have reached QQ. Never silently resend it on startup."""
    with get_db() as db:
        if db.execute("SELECT 1 FROM qq_deliveries WHERE status='sending' LIMIT 1").fetchone():
            db.execute('UPDATE qq_agent SET current_conv_id=NULL, current_set_at=0 WHERE id=1')
        db.execute("UPDATE qq_delivery_parts SET status='uncertain', last_error='服务在发送期间重启，投递结果未确认' WHERE status='sending'")
        db.execute("UPDATE qq_deliveries SET status='uncertain', last_error='服务在发送期间重启，请先核对 QQ 收件情况', next_attempt_at=0, updated_at=datetime('now','localtime') "
                   "WHERE status='sending' AND EXISTS (SELECT 1 FROM qq_delivery_parts p WHERE p.delivery_id=qq_deliveries.id AND p.status='uncertain')")
        for r in db.execute("SELECT id FROM qq_deliveries WHERE status='sending'").fetchall():
            if db.execute("SELECT 1 FROM qq_delivery_parts WHERE delivery_id=? AND status NOT IN ('sent','skipped')", (r[0],)).fetchone():
                db.execute("UPDATE qq_deliveries SET status='pending' WHERE id=?", (r[0],))
            else:
                _finish(db, r[0])


def cancel_for_binding_change(*, db=None):
    """Caller holds queue_lock; pending content must not move to a different QQ account."""
    with (get_db() if db is None else nullcontext(db)) as db:
        db.execute("UPDATE qq_deliveries SET status='cancelled', last_error='客服绑定已变更，停止此投递', next_attempt_at=0, updated_at=datetime('now','localtime') WHERE status NOT IN ('sent','cancelled')")
        db.execute("UPDATE chat_messages SET qq_state='' WHERE qq_state='pending'")
        db.execute("DELETE FROM conversation_triage_jobs")
        db.execute("UPDATE qq_inbound SET status='cancelled',payload=NULL WHERE status='pending'")


async def _changed(delivery_id, conv_id):
    await chat.hub.to_admins({'type': 'qq_delivery_changed', 'delivery_id': delivery_id, 'conversation_id': conv_id})


def _create(conv, recipient):
    from . import qq
    with get_db() as db:
        msgs = rows(db.execute(
            "SELECT id, content, msg_type FROM chat_messages m WHERE conversation_id=? AND qq_state='pending' "
            "AND NOT EXISTS (SELECT 1 FROM qq_delivery_messages dm WHERE dm.message_id=m.id) ORDER BY id LIMIT ?",
            (conv['id'], MAX_BATCH_MESSAGES)))
        if not msgs:
            return None
        first = not db.execute("SELECT 1 FROM chat_messages WHERE conversation_id=? AND qq_state='sent' LIMIT 1", (conv['id'],)).fetchone()
        delivery_id = db.execute("INSERT INTO qq_deliveries (conversation_id,recipient) VALUES (?,?)", (conv['id'], recipient)).lastrowid
        db.executemany("INSERT INTO qq_delivery_messages (delivery_id,message_id) VALUES (?,?)", [(delivery_id, m['id']) for m in msgs])
        parts = [('text', text) for text in qq._chunks(qq.compose(conv, msgs, first))]
        parts += [('image', m['content']) for m in msgs if m['msg_type'] == 'image']
        db.executemany("INSERT INTO qq_delivery_parts (delivery_id,sequence,kind,content) VALUES (?,?,?,?)",
                       [(delivery_id, seq, kind, content) for seq, (kind, content) in enumerate(parts)])
    return delivery_id


def enqueue_handoff(db, conv, msgs, reason, inbox_message_id, ai_answer='', *, summary=None):
    """Write the outbox in the caller's archive/progress transaction; no network I/O."""
    from . import qq
    recipient = db.execute('SELECT openid FROM qq_agent WHERE id=1').fetchone()[0]
    if not qq.ENABLED or not recipient:
        return None
    key = f"handoff:{conv['id']}:{msgs[-1]['id']}"
    existing = db.execute('SELECT id FROM qq_deliveries WHERE event_key=?', (key,)).fetchone()
    if existing:
        return existing[0]
    def compact(value, limit):
        text = re.sub(r'\s+', ' ', str(value or '')).strip()
        return text if len(text) <= limit else text[:limit-1] + '…'
    from .summaries import qq_text
    ai_summary = qq_text(summary)
    summary_id = summary.get('id') if summary and summary['status'] == 'ready' else None
    selected = msgs if len(msgs) <= 5 else msgs[:2] + msgs[-3:]
    lines = [compact('[图片]' if m['msg_type'] == 'image' else m['content'], 110) for m in selected]
    if len(msgs) > 5:
        lines.insert(2, f"（省略 {len(msgs)-5} 条，完整内容见来源会话）")
    summary = '\n'.join('· ' + line for line in lines)
    text = (f"⚠ 需要人工处理【{compact(conv['site_name'], 80)}】#{conv['id']}\n"
            f"原因：{reason}\n"
            f"访客：{compact(conv.get('visitor_name') or '访客', 100)}\n"
            f"联系方式：{compact(conv.get('visitor_contact') or '未留联系方式', 200)}\n"
            f"来源：对话 #{conv['id']} · 留言 #{inbox_message_id}\n"
            f"页面：{compact(conv.get('page_url') or '未记录', 240)}\n"
            f"访客原话摘录（共 {len(msgs)} 条，非 AI 总结）：\n{summary}\n"
            + (f"AI 刚才的回复：{compact(ai_answer, 300)}\n" if ai_answer else '') +
            (ai_summary + '\n' if ai_summary else '') +
            f"已记为留言；引用本条回复，或发「#{conv['id']} 内容」。")
    jid = db.execute('INSERT INTO qq_deliveries (conversation_id,recipient,kind,event_key,reason,summary,inbox_message_id,summary_id) '
                     "VALUES (?,?,'handoff',?,?,?,?,?)", (conv['id'], recipient, key, reason, summary, inbox_message_id, summary_id)).lastrowid
    db.executemany('INSERT INTO qq_notification_sources (delivery_id,message_id) VALUES (?,?)', [(jid,m['id']) for m in msgs])
    db.executemany("INSERT INTO qq_delivery_parts (delivery_id,sequence,kind,content) VALUES (?,?,'text',?)",
                   [(jid,i,part) for i,part in enumerate(qq._chunks(text))])
    return jid


async def _send_part(job, part):
    from . import qq
    async with qq._send_lock:
        if part['kind'] == 'image':
            local = media.local_path(part['content'])
            if not local or not local.is_file():
                raise ImageUnavailable('图片不存在')
            if local.suffix.lower() not in ('.png', '.jpg'):
                raise ImageUnavailable(f'QQ 不支持 {local.suffix[1:].upper()} 格式')
            file_info = await qq.client.upload_image(job['recipient'], local.read_bytes())
            body = {'msg_type': 7, 'media': {'file_info': file_info}}
        else:
            body = {'content': part['content'], 'msg_type': 0}
        return await qq._send_one(job['recipient'], body, job['conversation_id'])


def _cancel_obsolete_handoff(db, job):
    if job['kind'] == 'priority':
        from . import triage, auto
        conv = chat.conv_summary(job['conversation_id'])
        with_state = db.execute('SELECT dismissed,source_upto FROM conversation_triage WHERE id=?', (job['triage_id'],)).fetchone()
        latest_agent = db.execute("SELECT COALESCE(MAX(id),0) FROM chat_messages WHERE conversation_id=? AND sender='agent'", (job['conversation_id'],)).fetchone()[0]
        if (with_state and latest_agent >= with_state['source_upto']) or not triage.enabled() or not conv or not auto.ai_ready(chat.get_site(conv['site_id']) or {}) or not conv.get('triage') or not conv['triage']['high'] or not with_state or with_state['dismissed']:
            db.execute("UPDATE qq_deliveries SET status='cancelled',next_attempt_at=0,last_error='优先级已撤销、会话已接管/结束或自动分类已关闭' WHERE id=?",(job['id'],))
            return True
        return False
    if job['kind'] != 'handoff':
        return False
    conv = db.execute('SELECT status FROM conversations WHERE id=?', (job['conversation_id'],)).fetchone()
    inbox = db.execute('SELECT status FROM messages WHERE id=?', (job['inbox_message_id'],)).fetchone()
    source_upto = db.execute('SELECT MAX(message_id) FROM qq_notification_sources WHERE delivery_id=?', (job['id'],)).fetchone()[0] or 0
    latest_agent = db.execute("SELECT MAX(id) FROM chat_messages WHERE conversation_id=? AND sender='agent'", (job['conversation_id'],)).fetchone()[0] or 0
    if not conv or conv['status'] == 'closed' or not inbox or inbox['status'] in ('handled', 'spam') or latest_agent > source_upto:
        db.execute("UPDATE qq_deliveries SET status='cancelled', last_error='会话已接管/结束或来源留言已处理/删除，停止补发转人工通知', "
                   "next_attempt_at=0, updated_at=datetime('now','localtime') WHERE id=?", (job['id'],))
        return True
    return False


async def _run(delivery_id, *, manual=False):
    """Caller holds queue_lock from snapshot through checkpoint; never restart sent parts."""
    from . import qq
    with get_db() as db:
        row = db.execute("SELECT * FROM qq_deliveries WHERE id=?", (delivery_id,)).fetchone()
    if not row:
        return True
    job = dict(row)
    if job['status'] == 'sending':
        # A previous worker may have failed between the external send and checkpoint.
        # queue_lock excludes a live sender here; apply the same recovery as a restart.
        recover()
        with get_db() as db:
            job = dict(db.execute('SELECT * FROM qq_deliveries WHERE id=?', (delivery_id,)).fetchone())
    if job['status'] in ('sent', 'cancelled'):
        return True
    with get_db() as db:
        obsolete = _cancel_obsolete_handoff(db, job)
    if obsolete:
        await _changed(delivery_id, job['conversation_id'])
        return True
    if job['status'] == 'uncertain' or (not manual and job['next_attempt_at'] > time.time()):
        return False
    recipient = qq.agent()['openid']
    if not recipient or recipient != job['recipient']:
        with get_db() as db:
            db.execute("UPDATE qq_deliveries SET status='cancelled', last_error='客服绑定已变更，停止此投递', next_attempt_at=0 WHERE id=?", (delivery_id,))
            db.execute("UPDATE chat_messages SET qq_state='' WHERE id IN (SELECT message_id FROM qq_delivery_messages WHERE delivery_id=?)", (delivery_id,))
        await _changed(delivery_id, job['conversation_id'])
        return True
    with get_db() as db:
        db.execute("UPDATE qq_deliveries SET status='sending', attempts=attempts+1, last_attempt_at=?, updated_at=datetime('now','localtime') WHERE id=?", (time.time(), delivery_id))
    await _changed(delivery_id, job['conversation_id'])
    try:
        while True:
            with get_db() as db:
                row = db.execute("SELECT * FROM qq_delivery_parts WHERE delivery_id=? AND status NOT IN ('sent','skipped') ORDER BY sequence LIMIT 1", (delivery_id,)).fetchone()
                if not row:
                    _finish(db, delivery_id)
                else:
                    if _cancel_obsolete_handoff(db, job):
                        return True
                    part = dict(row)
                    db.execute("UPDATE qq_delivery_parts SET status='sending', attempts=attempts+1 WHERE id=?", (part['id'],))
            if not row:
                a = qq.agent()
                if a['current_conv_id'] != job['conversation_id']:
                    qq.update_agent(current_conv_id=job['conversation_id'], current_set_at=time.time())
                return True
            try:
                message_id = await _send_part(job, part)
            except ImageUnavailable as exc:
                reason = str(exc)
                with get_db() as db:
                    db.execute("UPDATE qq_delivery_parts SET status='skipped', last_error=? WHERE id=?", (reason, part['id']))
                    db.execute("UPDATE qq_deliveries SET warning=? WHERE id=?", (reason + '，已改发文字提示', delivery_id))
                    seq = db.execute("SELECT MAX(sequence)+1 FROM qq_delivery_parts WHERE delivery_id=?", (delivery_id,)).fetchone()[0]
                    db.execute("INSERT INTO qq_delivery_parts (delivery_id,sequence,kind,content) VALUES (?,?,'fallback',?)",
                               (delivery_id, seq, f"#{job['conversation_id']} 的图片无法转发（{reason}），请在网页后台查看"))
                continue
            except BaseException as exc:
                uncertain = isinstance(exc, (qq.QQSendUncertain, asyncio.CancelledError))
                state = 'uncertain' if uncertain else 'failed'
                error = '发送被中断，请先核对 QQ 收件情况' if isinstance(exc, asyncio.CancelledError) else qq.safe_error(exc)
                delay = min(900, 30 * 2 ** min(job['attempts'], 5))
                with get_db() as db:
                    db.execute("UPDATE qq_delivery_parts SET status=?, last_error=? WHERE id=?", (state, error, part['id']))
                    db.execute("UPDATE qq_deliveries SET status=?, last_error=?, next_attempt_at=?, updated_at=datetime('now','localtime') WHERE id=?",
                               (state, error, 0 if uncertain else time.time() + delay, delivery_id))
                if uncertain:
                    # The agent may see a new conversation header despite the missing receipt.
                    # Require explicit targeting rather than leave an older default reply target.
                    qq.update_agent(current_conv_id=None, current_set_at=0)
                if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                    raise
                return False
            with get_db() as db:
                db.execute("UPDATE qq_delivery_parts SET status='sent', qq_message_id=?, last_error='', sent_at=datetime('now','localtime') WHERE id=?", (message_id, part['id']))
            if qq.agent()['current_conv_id'] != job['conversation_id']:
                qq.update_agent(current_conv_id=job['conversation_id'], current_set_at=time.time())
    finally:
        await _changed(delivery_id, job['conversation_id'])


async def flush_conversation(conv_id):
    from . import qq
    async with queue_lock:
        timer = qq._flush_timers.pop(conv_id, None)
        if timer:
            timer.cancel()
        conv = chat.conv_summary(conv_id)
        if not conv or not qq.agent()['openid']:
            return True
        with get_db() as db:
            jobs = db.execute("SELECT id,kind FROM qq_deliveries WHERE conversation_id=? AND status NOT IN ('sent','cancelled') "
                              "ORDER BY CASE WHEN kind='priority' THEN 0 WHEN kind='handoff' THEN 1 ELSE 2 END,id", (conv_id,)).fetchall()
        blocked = set()
        for job in jobs:
            if job['kind'] not in blocked and not await _run(job['id']):
                blocked.add(job['kind'])
        # An urgent handoff and visitor forwarding have separate FIFO streams.
        # A failure/uncertain result in one must not hide the other.
        if 'visitor' not in blocked:
            delivery_id = _create(conv, qq.agent()['openid'])
            if delivery_id and not await _run(delivery_id):
                blocked.add('visitor')
        return not blocked


async def flush_backlog():
    from . import qq
    with get_db() as db:
        conv_ids = [r[0] for r in db.execute("SELECT conversation_id FROM qq_deliveries WHERE status NOT IN ('sent','cancelled') "
                                           "UNION SELECT conversation_id FROM chat_messages WHERE qq_state='pending' ORDER BY conversation_id")]
    for conv_id in conv_ids:
        if conv_id not in qq._flush_timers:
            await flush_conversation(conv_id)  # One failed conversation must not starve the others.


@router.get('/api/admin/qq/deliveries', tags=['QQ'], dependencies=[admin])
def list_deliveries(status: str | None = None, conversation_id: int | None = Query(default=None, gt=0),
                    page: int = Query(default=1, ge=1), size: int = Query(default=20, ge=1, le=100), kind: str | None = None):
    where, args = [], []
    if status:
        if status not in STATES:
            raise HTTPException(400, '投递状态无效')
        where.append('d.status=?'); args.append(status)
    if conversation_id:
        where.append('d.conversation_id=?'); args.append(conversation_id)
    if kind:
        if kind not in ('visitor', 'handoff', 'priority'):
            raise HTTPException(400, '投递类型无效')
        where.append('d.kind=?'); args.append(kind)
    clause = 'WHERE ' + ' AND '.join(where) if where else ''
    with get_db() as db:
        total = db.execute(f'SELECT COUNT(*) FROM qq_deliveries d {clause}', args).fetchone()[0]
        items = rows(db.execute(SELECT + clause + ' ORDER BY d.id DESC LIMIT ? OFFSET ?', (*args, size, (page - 1) * size)))
    return {'items': items, 'total': total, 'page': page, 'size': size}


@router.get('/api/admin/qq/deliveries/{delivery_id}', tags=['QQ'], dependencies=[admin])
def delivery_detail(delivery_id: int):
    with get_db() as db:
        job = db.execute(SELECT + 'WHERE d.id=?', (delivery_id,)).fetchone()
        if not job:
            raise HTTPException(404, '投递记录不存在')
        parts = rows(db.execute('SELECT id,sequence,kind,content,status,attempts,last_error,qq_message_id,manually_confirmed,sent_at FROM qq_delivery_parts WHERE delivery_id=? ORDER BY sequence', (delivery_id,)))
        messages = rows(db.execute('SELECT * FROM (SELECT m.id,m.content,m.msg_type,m.created_at FROM chat_messages m WHERE m.id IN '
                                  '(SELECT message_id FROM qq_delivery_messages WHERE delivery_id=? UNION '
                                  'SELECT message_id FROM qq_notification_sources WHERE delivery_id=?) ORDER BY m.id DESC LIMIT 100) ORDER BY id', (delivery_id,delivery_id)))
    return {'delivery': dict(job), 'parts': parts, 'messages': messages, 'messages_truncated': job['message_count'] > len(messages)}


class RetryIn(BaseModel):
    confirm_uncertain: bool = False


@router.post('/api/admin/qq/deliveries/{delivery_id}/retry', tags=['QQ'], dependencies=[admin])
async def retry_delivery(delivery_id: int, body: RetryIn):
    from . import qq
    async with queue_lock:
        with get_db() as db:
            row = db.execute('SELECT * FROM qq_deliveries WHERE id=?', (delivery_id,)).fetchone()
        if not row:
            raise HTTPException(404, '投递记录不存在')
        job = dict(row)
        if job['status'] == 'sent':
            return {'ok': True, 'status': 'sent'}
        if job['status'] == 'cancelled':
            raise HTTPException(409, '此投递已取消')
        with get_db() as db:
            earlier = db.execute("SELECT 1 FROM qq_deliveries WHERE conversation_id=? AND kind=? AND id<? AND status NOT IN ('sent','cancelled') LIMIT 1", (job['conversation_id'], job['kind'], delivery_id)).fetchone()
        if earlier:
            raise HTTPException(409, '请先处理此会话更早的同类积压投递')
        if not qq.ENABLED or not qq.agent()['openid']:
            raise HTTPException(409, 'QQ 机器人未配置或客服未绑定')
        if qq.agent()['openid'] != job['recipient']:
            raise HTTPException(409, '客服绑定已变更，不能补发给其他账号')
        if time.time() - job['last_attempt_at'] < RETRY_COOLDOWN:
            raise HTTPException(429, '请在上次尝试 10 秒后再补发')
        if job['status'] == 'uncertain' and not body.confirm_uncertain:
            raise HTTPException(409, 'QQ 可能已收到消息，请先核对；确认补发可能重复的部分后再重试')
        if job['status'] == 'uncertain':
            with get_db() as db:
                db.execute("UPDATE qq_delivery_parts SET status='pending' WHERE delivery_id=? AND status='uncertain'", (delivery_id,))
                db.execute("UPDATE qq_deliveries SET status='pending' WHERE id=?", (delivery_id,))
        await _run(delivery_id, manual=True)
    return {'ok': True, 'status': delivery_detail(delivery_id)['delivery']['status']}


@router.post('/api/admin/qq/deliveries/{delivery_id}/confirm', tags=['QQ'], dependencies=[admin])
async def confirm_received(delivery_id: int):
    async with queue_lock:
        with get_db() as db:
            job = db.execute('SELECT * FROM qq_deliveries WHERE id=?', (delivery_id,)).fetchone()
            if not job:
                raise HTTPException(404, '投递记录不存在')
            if job['status'] != 'uncertain':
                raise HTTPException(409, '只有投递结果未确认的记录可以人工确认')
            db.execute("UPDATE qq_delivery_parts SET status='sent', manually_confirmed=1, last_error='', sent_at=datetime('now','localtime') WHERE delivery_id=? AND status='uncertain'", (delivery_id,))
            remaining = db.execute("SELECT 1 FROM qq_delivery_parts WHERE delivery_id=? AND status NOT IN ('sent','skipped')", (delivery_id,)).fetchone()
            if remaining:
                db.execute("UPDATE qq_deliveries SET status='pending', last_error='', next_attempt_at=0, updated_at=datetime('now','localtime') WHERE id=?", (delivery_id,))
            else:
                _finish(db, delivery_id)
        await _changed(delivery_id, job['conversation_id'])
    return {'ok': True}

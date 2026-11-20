"""QQ forwarding regression with a fake client. No production data or QQ API calls."""
import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError


class FakeQQ:
    _token = 'fake-token'

    def __init__(self):
        self.calls = []
        self.uploads = []
        self.fail_on = set()
        self.timeout_on = set()
        self.no_id_on = set()
        self.delay = 0

    async def send_c2c(self, recipient, content='', msg_id='', msg_seq=0, body=None):
        from app.qq import QQApiError
        self.calls.append({'recipient':recipient, 'body':body, 'msg_id':msg_id, 'msg_seq':msg_seq})
        number = len(self.calls)
        if self.delay:
            await asyncio.sleep(self.delay)
        if number in self.fail_on:
            raise QQApiError(429, 'Fake rate limit', 429)
        if number in self.timeout_on:
            raise TimeoutError('Fake response timeout')
        if number in self.no_id_on:
            return {}
        return {'id':f'fake-{number}', 'ext_info':{'ref_idx':f'ref-{number}'}}

    async def upload_image(self, recipient, data):
        self.uploads.append((recipient,data))
        return 'fake-image-info'


class QQDeliveryTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='msghub-qq-tests-')
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.env = patch.dict(os.environ, {'DATA_DIR':cls.tmp.name, 'ADMIN_PASSWORD':'qq-test-only',
                                         'SECRET_KEY':'qq-test-only', 'QQ_APP_ID':'qq-fake-id', 'QQ_APP_SECRET':'qq-fake-secret'})
        cls.env.start(); cls.addClassCleanup(cls.env.stop)
        from app import db, qq, qq_delivery
        cls.db, cls.qq, cls.delivery = db, qq, qq_delivery
        db.init_db()

    def setUp(self):
        self.qq._send_lock = asyncio.Lock()
        self.delivery.queue_lock = asyncio.Lock()
        self.qq._flush_timers = {}
        self.fake = FakeQQ()
        self.old_client = self.qq.client
        self.qq.client = self.fake
        self.addCleanup(setattr,self.qq,'client',self.old_client)
        with self.db.get_db() as db:
            db.execute('DELETE FROM qq_inbound')
            db.execute('DELETE FROM settings')
            db.execute('DELETE FROM qa')
            db.execute('DELETE FROM sites')
            db.execute('DELETE FROM visitors')
            self.site = db.execute("INSERT INTO sites (name,site_key) VALUES ('QQ test','qq-test-key')").lastrowid
        self.qq.update_agent(openid='test-agent',last_msg_id='',last_msg_at=0,passive_used=0,current_conv_id=None)

    def conversation(self, text='hello', kind='text'):
        with self.db.get_db() as db:
            vid=db.execute('INSERT INTO visitors (token) VALUES (?)',(os.urandom(12).hex(),)).lastrowid
            cid=db.execute('INSERT INTO conversations (site_id,visitor_id) VALUES (?,?)',(self.site,vid)).lastrowid
            mid=db.execute("INSERT INTO chat_messages (conversation_id,sender,content,msg_type,qq_state) VALUES (?,'visitor',?,?,'pending')",(cid,text,kind)).lastrowid
        return cid,mid

    def job(self,cid):
        return self.delivery.list_deliveries(status=None,conversation_id=cid,page=1,size=20)['items'][0]

    def make_due(self,jid):
        with self.db.get_db() as db:
            db.execute('UPDATE qq_deliveries SET last_attempt_at=0,next_attempt_at=0 WHERE id=?',(jid,))

    async def test_partial_chunks_retry_and_idempotence(self):
        cid,mid=self.conversation('X'*4000)
        self.fake.fail_on={2}
        self.assertFalse(await self.delivery.flush_conversation(cid))
        job=self.job(cid); detail=self.delivery.delivery_detail(job['id'])
        self.assertEqual((job['status'],job['sent_parts'],job['attempts']),('failed',1,1))
        self.assertEqual(self.qq.agent()['current_conv_id'],cid)
        self.assertGreater(self.qq.agent()['current_set_at'],0)
        self.assertTrue(29 <= job['next_attempt_at']-__import__('time').time() <= 31)
        await self.delivery.flush_backlog()
        self.assertEqual(len(self.fake.calls),2)  # Backoff blocks an early automatic resend.
        with self.db.get_db() as db:
            self.assertEqual(db.execute('SELECT qq_state FROM chat_messages WHERE id=?',(mid,)).fetchone()[0],'pending')
        first_body=self.fake.calls[0]['body']
        self.make_due(job['id'])
        result,_=await asyncio.gather(self.delivery.retry_delivery(job['id'],self.delivery.RetryIn()),
                                    self.delivery.flush_conversation(cid))
        self.assertEqual(result['status'],'sent')
        self.assertNotIn(first_body,[c['body'] for c in self.fake.calls[2:]])
        count=len(self.fake.calls)
        await self.delivery.flush_conversation(cid)
        await self.delivery.retry_delivery(job['id'],self.delivery.RetryIn())
        self.assertEqual(len(self.fake.calls),count)
        self.assertEqual(self.job(cid)['attempts'],2)
        with self.db.get_db() as db:
            self.assertEqual(db.execute('SELECT qq_state FROM chat_messages WHERE id=?',(mid,)).fetchone()[0],'sent')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM qq_forwards WHERE conversation_id=?',(cid,)).fetchone()[0],6)

    async def test_concurrent_flush_and_new_arrival(self):
        cid,mid=self.conversation('one')
        self.fake.delay=.03
        await asyncio.gather(self.delivery.flush_conversation(cid),self.delivery.flush_conversation(cid),self.delivery.flush_backlog())
        self.assertEqual(len(self.fake.calls),1)
        with self.db.get_db() as db:
            db.execute("INSERT INTO chat_messages (conversation_id,sender,content,msg_type,qq_state) VALUES (?,'visitor','two','text','pending')",(cid,))
        await self.delivery.flush_conversation(cid)
        self.assertEqual(len(self.fake.calls),2)
        self.assertEqual(self.delivery.list_deliveries(status=None,conversation_id=cid,page=1,size=20)['total'],2)

    async def test_uncertain_timeout_confirm_and_restart(self):
        from fastapi import HTTPException
        cid,_=self.conversation('X'*3000)
        previous,previous_mid=self.conversation('previous reply target')
        with self.db.get_db() as db:
            db.execute("UPDATE chat_messages SET qq_state='sent' WHERE id=?", (previous_mid,))
        self.qq.update_agent(current_conv_id=previous)
        self.fake.timeout_on={1}
        await self.delivery.flush_conversation(cid)
        job=self.job(cid)
        self.assertEqual(job['status'],'uncertain')
        self.assertIsNone(self.qq.agent()['current_conv_id'])
        await self.delivery.flush_backlog()
        self.assertEqual(len(self.fake.calls),1)
        self.make_due(job['id'])
        with self.assertRaises(HTTPException) as ctx:
            await self.delivery.retry_delivery(job['id'],self.delivery.RetryIn())
        self.assertEqual(ctx.exception.status_code,409)
        await self.delivery.confirm_received(job['id'])
        await self.delivery.flush_backlog()
        self.assertEqual(self.job(cid)['status'],'sent')
        self.assertEqual(self.delivery.delivery_detail(job['id'])['parts'][0]['manually_confirmed'],1)
        # A process stopped mid-request must recover to uncertain, never automatic resend.
        other,_=self.conversation('interrupted')
        jid=self.delivery._create(__import__('app.chat',fromlist=['conv_summary']).conv_summary(other),'test-agent')
        with self.db.get_db() as db:
            db.execute("UPDATE qq_deliveries SET status='sending' WHERE id=?",(jid,))
            db.execute("UPDATE qq_delivery_parts SET status='sending' WHERE delivery_id=?",(jid,))
        self.delivery.recover(); self.delivery.recover()
        self.assertEqual(self.job(other)['status'],'uncertain')
        count=len(self.fake.calls); await self.delivery.flush_backlog()
        self.assertEqual(len(self.fake.calls),count)
        self.make_due(jid)
        await self.delivery.retry_delivery(jid,self.delivery.RetryIn(confirm_uncertain=True))
        self.assertEqual(self.job(other)['status'],'sent')

    async def test_missing_receipt_passive_fallback_and_cooldown(self):
        from fastapi import HTTPException
        cid,_=self.conversation('missing receipt')
        self.fake.no_id_on={1}
        await self.delivery.flush_conversation(cid)
        job=self.job(cid)
        self.assertEqual(job['status'],'uncertain')
        with self.assertRaises(HTTPException) as ctx:
            await self.delivery.retry_delivery(job['id'],self.delivery.RetryIn(confirm_uncertain=True))
        self.assertEqual(ctx.exception.status_code,429)
        other,_=self.conversation('passive fallback')
        self.qq.update_agent(last_msg_id='incoming',last_msg_at=__import__('time').time(),passive_used=0)
        self.fake.fail_on={2}
        await self.delivery.flush_conversation(other)
        self.assertEqual(self.job(other)['status'],'sent')
        self.assertEqual(self.fake.calls[-1]['msg_id'],'incoming')
        self.assertEqual(self.fake.calls[-1]['msg_seq'],1)

    async def test_image_progress_and_unsupported_fallback(self):
        from app import media
        png=media.save_image(b'\x89PNG\r\n\x1a\n' + b'fake')
        cid,_=self.conversation(png,'image')
        self.fake.fail_on={2}
        await self.delivery.flush_conversation(cid)
        job=self.job(cid)
        self.assertEqual((job['status'],job['sent_parts']),('failed',1))
        self.make_due(job['id'])
        await self.delivery.retry_delivery(job['id'],self.delivery.RetryIn())
        self.assertEqual(self.job(cid)['status'],'sent')
        self.assertEqual(sum(c['body']['msg_type']==0 for c in self.fake.calls),1)
        gif=media.save_image(b'GIF89a' + b'fake')
        other,_=self.conversation(gif,'image')
        await self.delivery.flush_conversation(other)
        d=self.delivery.delivery_detail(self.job(other)['id'])
        self.assertEqual(d['delivery']['status'],'sent')
        self.assertIn('GIF',d['delivery']['warning'])
        self.assertEqual([p['status'] for p in d['parts']],['sent','skipped','sent'])

    async def test_binding_change_cancel_and_backlog_fairness(self):
        from fastapi import HTTPException
        cid,_=self.conversation('blocked')
        self.fake.fail_on={1}
        await self.delivery.flush_conversation(cid)
        other,_=self.conversation('must still deliver')
        await self.delivery.flush_backlog()
        self.assertEqual(self.job(other)['status'],'sent')
        self.assertEqual(self.job(cid)['status'],'failed')
        job=self.job(cid); self.make_due(job['id'])
        self.qq.update_agent(openid='different-agent')
        with self.assertRaises(HTTPException) as ctx:
            await self.delivery.retry_delivery(job['id'],self.delivery.RetryIn())
        self.assertEqual(ctx.exception.status_code,409)
        count=len(self.fake.calls)
        await self.delivery.flush_backlog()
        self.assertEqual(self.job(cid)['status'],'cancelled')
        self.assertEqual(len(self.fake.calls),count)
        with self.assertRaises(HTTPException): self.delivery.delivery_detail(999999)
        self.assertNotIn('recipient',self.job(cid))
        await self.qq.unbind()

    async def test_ambiguous_server_error_local_checkpoint_and_redaction(self):
        from unittest.mock import AsyncMock
        import time
        cid,_ = self.conversation('ambiguous server response')
        self.qq.update_agent(last_msg_id='incoming',last_msg_at=time.time(),passive_used=0)
        self.fake.send_c2c = AsyncMock(side_effect=self.qq.QQApiError(500, 'gateway error', 502))
        await self.delivery.flush_conversation(cid)
        self.assertEqual(self.job(cid)['status'], 'uncertain')
        self.assertEqual(self.fake.send_c2c.await_count, 1)  # No passive resend after ambiguous response.
        self.fake.send_c2c = AsyncMock(return_value={'ext_info':{'ref_idx':'only-ref'}})
        self.assertEqual(await self.qq._send_one('test-agent', {'content':'ok'}, cid), 'only-ref')
        # QQ accepted the request; failure saving its quote reference must not cause blind resend.
        with patch('app.qq.get_db', side_effect=RuntimeError('local checkpoint failed')):
            with self.assertRaises(self.qq.QQSendUncertain):
                await self.qq._send_one('test-agent', {'content':'ok'}, cid)
        redacted = self.qq.safe_error('qq-fake-secret fake-token test-agent')
        self.assertEqual(redacted, '[已隐藏] [已隐藏] [已隐藏]')
        self.fake.send_c2c = AsyncMock(side_effect=self.qq.QQApiError(400, 'qq-fake-secret fake-token test-agent', 400))
        self.qq.update_agent(last_msg_id='')
        ids, error = await self.qq.deliver('command response')
        self.assertEqual(ids, [])
        self.assertNotIn('qq-fake-secret', error)
        # Recover an interrupted checkpoint without waiting for a service restart.
        other,_ = self.conversation('checkpoint recovery')
        from app.chat import conv_summary
        jid = self.delivery._create(conv_summary(other), 'test-agent')
        with self.db.get_db() as db:
            db.execute("UPDATE qq_deliveries SET status='sending' WHERE id=?", (jid,))
            db.execute("UPDATE qq_delivery_parts SET status='sending' WHERE delivery_id=?", (jid,))
        count = self.fake.send_c2c.await_count
        await self.delivery.flush_conversation(other)
        self.assertEqual(self.job(other)['status'], 'uncertain')
        self.assertEqual(self.fake.send_c2c.await_count, count)

    def enqueue_reply(self, msg_id, content, **fields):
        from app import qq_inbound
        data = {"id": msg_id, "author":{"user_openid":"test-agent"}, "content":content, **fields}
        qq_inbound.accept(self.qq.QQ_APP_ID, msg_id, data)
        return data

    def inbound_rows(self):
        with self.db.get_db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM qq_inbound ORDER BY id")]

    async def test_concurrent_binding_consumes_code_once_and_cancels_old_jobs(self):
        from unittest.mock import AsyncMock
        from app import qq_inbound
        import time
        cid, mid = self.conversation("pending for original agent")
        self.fake.fail_on = {1}
        await self.delivery.flush_conversation(cid)
        jid = self.job(cid)["id"]
        self.qq.update_agent(bind_code="123456", bind_expires=time.time()+60, current_conv_id=cid)
        version = self.qq.agent()["binding_version"]
        self.enqueue_reply("new-binding", "/绑定 123456", author={"user_openid":"new-agent"})
        self.enqueue_reply("competing-binding", "/绑定 123456", author={"user_openid":"other-agent"})
        self.enqueue_reply("reply-in-old-binding", f"#{cid} old reply")
        with patch.object(self.qq, "deliver", new_callable=AsyncMock) as ack:
            self.assertTrue(await qq_inbound.process_one())
            self.assertFalse(await qq_inbound.process_one())
            self.assertEqual(ack.await_count, 1)
        self.assertEqual([r["status"] for r in self.inbound_rows()], ["done","cancelled","cancelled"])
        self.assertEqual(self.qq.agent()["openid"], "new-agent")
        self.assertEqual(self.qq.agent()["bind_code"], "")
        self.assertEqual(self.qq.agent()["binding_version"], version + 1)
        self.assertIsNone(self.qq.agent()["current_conv_id"])
        self.assertEqual(self.delivery.delivery_detail(jid)["delivery"]["status"], "cancelled")
        with self.db.get_db() as db:
            self.assertEqual(db.execute("SELECT qq_state FROM chat_messages WHERE id=?", (mid,)).fetchone()[0], "")

    async def test_reply_uses_guarded_target_after_backlog(self):
        from unittest.mock import AsyncMock
        from app import chat, qq_inbound
        import time
        cid, _ = self.conversation("original target")
        other, _ = self.conversation("new pending header")
        self.qq.update_agent(current_conv_id=cid, current_set_at=0)
        self.enqueue_reply("incoming-1", "must not reach old visitor")
        self.qq.update_agent(current_conv_id=None, current_set_at=0)
        with patch.object(self.qq, "deliver", new_callable=AsyncMock) as ack:
            await qq_inbound.process_one()
            self.assertIn("当前没有可回复的会话", ack.call_args.args[0])
            self.qq.update_agent(current_conv_id=cid, current_set_at=0)
            self.enqueue_reply("incoming-2", "must respect new guard")
            self.qq.update_agent(current_conv_id=other, current_set_at=time.time())
            await qq_inbound.process_one()
            self.assertIn("当前会话刚切换", ack.call_args.args[0])
        self.assertEqual(len(chat.history(cid)), 1)
        self.assertEqual(len(chat.history(other)), 1)

    async def test_inbound_quotes_fail_closed_and_explicit_target_is_supported(self):
        from unittest.mock import AsyncMock
        from app import chat, qq_inbound
        target, _ = self.conversation("intended target")
        current, _ = self.conversation("current target")
        self.qq.update_agent(current_conv_id=current,current_set_at=0)
        with self.db.get_db() as db:
            db.execute("INSERT INTO qq_forwards (qq_msg_id,conversation_id) VALUES ('known',?)", (target,))
        self.enqueue_reply("unknown-reference", "no target", message_reference={"message_id":"unknown"})
        self.enqueue_reply("conflicting-reference", f"#{current} conflicting target", message_reference={"message_id":"known"})
        self.enqueue_reply("explicit-after-unmapped", f"#{target} explicit text", message_reference={"message_id":"unknown"})
        self.enqueue_reply("known-reference", "quoted text", message_reference={"message_id":"known"})
        wide_id = str(target).translate(str.maketrans("0123456789", "０１２３４５６７８９"))
        self.enqueue_reply("wide-id", f"#{wide_id} wide number")
        with patch.object(self.qq, "deliver", new_callable=AsyncMock) as ack:
            for _ in range(5):
                self.assertTrue(await qq_inbound.process_one())
        self.assertEqual(len(chat.history(current)), 1)
        self.assertEqual([m["content"] for m in chat.history(target) if m["sender"]=="agent"], ["explicit text","quoted text","wide number"])
        self.assertIn("找不到引用消息", ack.await_args_list[0].args[0])
        self.assertIn("回复目标不一致", ack.await_args_list[1].args[0])

    async def test_qq_callback_commits_before_ack_and_duplicate_after_restart_is_ignored(self):
        import json
        import time
        from unittest.mock import AsyncMock
        from fastapi import HTTPException
        from starlette.requests import Request
        from app import chat, qq_inbound
        callback_key = "test-callback-key-for-qq-regression"
        self.enterContext(patch.object(self.qq, "QQ_CALLBACK_KEY", callback_key))
        challenge = {
            "op": 13, "d": {"event_ts": str(int(time.time())), "plain_token": "platform-challenge"},
        }
        receive = AsyncMock(return_value={"type": "http.request", "body": json.dumps(challenge).encode()})
        with self.assertRaises(HTTPException) as rejected:
            await self.qq.callback(Request({"type": "http", "headers": []}, receive), "unknown-key")
        self.assertEqual(rejected.exception.status_code, 404)
        result = await self.qq.callback(Request({"type": "http", "headers": []}, receive), callback_key)
        self.assertEqual(result["plain_token"], "platform-challenge")
        cid, _ = self.conversation("test visitor")
        data = {"id":"platform-message", "author":{"user_openid":"test-agent"}, "content":f"#{cid} preserved reply"}
        async def callback(envelope_id, *, age=0):
            body = json.dumps({"op":0,"id":envelope_id,"t":"C2C_MESSAGE_CREATE","d":data}).encode()
            timestamp = str(int(time.time()) - age)
            signature = self.qq._key.sign(timestamp.encode()+body).hex()
            receive = AsyncMock(return_value={"type":"http.request","body":body})
            return await self.qq.callback(Request({"type":"http","headers":[
                (b"x-signature-timestamp",timestamp.encode()),
                (b"x-signature-ed25519",signature.encode())]},receive), callback_key)
        self.assertEqual((await callback("expired-event", age=3600)).status_code, 401)
        self.assertEqual(await callback("envelope-1"),{"op":12})
        self.assertEqual(self.inbound_rows()[0]["status"],"pending")
        self.assertEqual(len(chat.history(cid)),1)
        self.db.init_db()  # No in-memory state from accepting the callback is needed.
        with patch.object(chat, "broadcast_message", new_callable=AsyncMock, side_effect=RuntimeError("broadcast interrupted")):
            self.assertTrue(await qq_inbound.process_one())
        self.assertEqual(self.inbound_rows()[0]["status"],"done")
        self.assertIsNone(self.inbound_rows()[0]["payload"])
        self.assertEqual(await callback("envelope-2"),{"op":12})
        self.assertFalse(await qq_inbound.process_one())
        self.assertEqual([m["content"] for m in chat.history(cid) if m["sender"]=="agent"],["preserved reply"])
        self.assertEqual(len(self.inbound_rows()),1)

    async def test_inbound_batch_rolls_back_and_keeps_target_on_retry(self):
        from app import chat, qq_inbound
        cid, _ = self.conversation("image target")
        other, _ = self.conversation("unrelated")
        with self.db.get_db() as db:
            db.execute("INSERT INTO qq_forwards (qq_msg_id,conversation_id) VALUES ('old-map',?)",(cid,))
        self.enqueue_reply("image-text", "image caption", message_reference={"message_id":"old-map"},
                           attachments=[{"content_type":"image/png","url":"https://example.invalid/image"}])
        real_add = chat.add_message
        def fail_after_image(*args, **kwargs):
            if kwargs["msg_type"]=="text":
                raise RuntimeError("interrupted after writing the image in this transaction")
            return real_add(*args,**kwargs)
        with patch.object(self.qq, "_download", return_value=b"fake image"), \
                patch("app.media.save_image", return_value="/uploads/fixture.png"), \
                patch.object(chat,"add_message",side_effect=fail_after_image):
            await qq_inbound.process_one()
        self.assertEqual(len(chat.history(cid)),1)
        self.assertEqual(self.inbound_rows()[0]["status"],"pending")
        self.assertEqual(self.inbound_rows()[0]["target_conv_id"],cid)
        with self.db.get_db() as db:
            db.execute("UPDATE qq_inbound SET next_attempt_at=0")
            db.execute("UPDATE qq_forwards SET conversation_id=? WHERE qq_msg_id='old-map'",(other,))
        self.db.init_db()
        with patch.object(self.qq, "_download", return_value=b"fake image"), \
                patch("app.media.save_image", return_value="/uploads/fixture.png"):
            await qq_inbound.process_one()
        self.assertEqual([m["msg_type"] for m in chat.history(cid) if m["sender"]=="agent"],["image","text"])
        self.assertEqual(len(chat.history(other)),1)

    async def test_visitor_message_is_discoverable_before_any_broadcast_or_hook(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from fastapi import WebSocketDisconnect
        from app import chat
        class FixtureSocket(SimpleNamespace):
            __hash__ = object.__hash__

        websocket = FixtureSocket(headers={},client=SimpleNamespace(host="isolated-fixture"),
                                  accept=AsyncMock(),close=AsyncMock(),send_json=AsyncMock())
        with self.db.get_db() as db:
            db.execute("UPDATE sites SET require_contact=0 WHERE id=?", (self.site,))
        websocket.receive_json = AsyncMock(side_effect=[
            {"type":"hello","site_key":"qq-test-key"},
            {"type":"msg","client_id":"isolated","content":"message survives restart"},
            WebSocketDisconnect(),
        ])
        async def after_commit(conv_id, message, client_id):
            with self.db.get_db() as db:
                self.assertEqual(db.execute("SELECT qq_state FROM chat_messages WHERE id=?",(message["id"],)).fetchone()[0],"pending")
            await self.delivery.flush_backlog()
            self.assertEqual(self.job(conv_id)["status"],"sent")
            raise WebSocketDisconnect()
        with patch.object(chat,"broadcast_message",side_effect=after_commit) as broadcast:
            await chat.visitor_ws(websocket)
            broadcast.assert_awaited_once()
        self.assertEqual(sum("message survives restart" in c["body"].get("content","") for c in self.fake.calls),1)
        await self.delivery.flush_backlog()
        self.assertEqual(len(self.fake.calls),1)

    async def test_site_revocation_skips_sockets_in_a_pending_broadcast(self):
        from unittest.mock import AsyncMock, Mock
        from app.chat import Hub

        hub = Hub()
        site = {"id": 1, "site_key": "old-key"}
        sockets = [Mock(spec=["send_json", "close"]) for _ in range(2)]
        for ws in sockets:
            ws.send_json = AsyncMock()
            ws.close = AsyncMock()
            hub.track_visitor(ws, site)
            hub.join(1, ws)
        first = next(iter(hub.visitors[1]))
        started, release = asyncio.Event(), asyncio.Event()

        async def blocked_send(_):
            started.set()
            await release.wait()

        first.send_json.side_effect = blocked_send
        pending = asyncio.create_task(hub.to_visitor(1, {"type": "msg"}))
        try:
            await asyncio.wait_for(started.wait(), 1)
            await hub.revoke_site_visitors(1)
            self.assertFalse(hub.visitor_online(1))
            for ws in sockets:
                ws.close.assert_awaited_once_with(4403)
                if ws is not first:
                    ws.send_json.assert_not_awaited()
        finally:
            release.set()
        self.assertEqual(await asyncio.wait_for(pending, 1), 0)
        for ws in sockets:
            self.assertEqual(ws.send_json.await_count, int(ws is first))

    async def test_handoff_snapshot_dedup_archive_merge_and_quote(self):
        from unittest.mock import AsyncMock
        from app import auto
        cid,mid = self.conversation('想咨询套餐并找人工客服')
        with self.db.get_db() as db:
            db.execute("UPDATE chat_messages SET qq_state='' WHERE id=?", (mid,))
            db.execute("UPDATE visitors SET name='验收访客',contact='customer@example.com' WHERE id=(SELECT visitor_id FROM conversations WHERE id=?)", (cid,))
            db.execute("UPDATE conversations SET page_url='https://example.com/pricing' WHERE id=?", (cid,))
            db.execute("INSERT INTO conversation_notes (conversation_id,content) VALUES (?,'内部备注绝不外发')", (cid,))
        with patch.object(auto,'ai_ready',return_value=True), \
                patch.object(auto,'ai_answer',new_callable=AsyncMock,return_value=('',True)), \
                patch.object(auto,'notify_agent',new_callable=AsyncMock) as old_notify:
            await auto.handle_unattended(cid,'timeout')
            await auto.handle_unattended(cid,'timeout')
            old_notify.assert_not_awaited()
        job = self.job(cid)
        self.assertEqual((job['kind'],job['status'],job['message_count']), ('handoff','sent',1))
        self.assertIn('AI 判断需要人工处理',job['reason'])
        sent='\n'.join(c['body']['content'] for c in self.fake.calls)
        for fragment in ['验收访客','customer@example.com','https://example.com/pricing','想咨询套餐',f'#{cid}','非 AI 总结']:
            self.assertIn(fragment,sent)
        self.assertNotIn('内部备注绝不外发',sent)
        detail=self.delivery.delivery_detail(job['id'])
        self.assertEqual([m['id'] for m in detail['messages']],[mid])
        with self.db.get_db() as db:
            self.assertEqual(db.execute('SELECT qq_state FROM chat_messages WHERE id=?',(mid,)).fetchone()[0],'')
            self.assertEqual(db.execute('SELECT conversation_id FROM qq_forwards WHERE qq_msg_id=?',(detail['parts'][0]['qq_message_id'],)).fetchone()[0],cid)
            db.execute("INSERT INTO chat_messages (conversation_id,sender,content) VALUES (?,'visitor','补充第二项需求')",(cid,))
        with patch.object(auto,'ai_ready',return_value=False):
            await auto.handle_unattended(cid,'timeout')
        self.assertEqual(self.job(cid)["id"], job["id"])  # Follow-ups don't send another handoff.
        with self.db.get_db() as db:
            archived = db.execute("SELECT id,content FROM messages WHERE conversation_id=?", (cid,)).fetchall()
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0]["id"], job["inbox_message_id"])
        self.assertIn("补充第二项需求", archived[0]["content"])

    async def test_handoff_failures_retry_without_pending_visitor_and_binding_cancel(self):
        from app import auto
        from unittest.mock import AsyncMock
        cid,mid=self.conversation('notification only')
        with self.db.get_db() as db:
            db.execute("UPDATE chat_messages SET qq_state='' WHERE id=?",(mid,))
        self.fake.fail_on={1}
        with patch.object(auto,'ai_ready',return_value=False):
            await auto.handle_unattended(cid,'timeout')
        job=self.job(cid)
        self.assertEqual(job['status'],'failed')
        self.make_due(job['id'])
        self.delivery.recover()
        await self.delivery.flush_backlog()  # No chat qq_state=pending; outbox alone drives retry.
        self.assertEqual(self.job(cid)['status'],'sent')
        self.assertEqual(self.job(cid)['attempts'],2)
        other,mid=self.conversation('uncertain handoff')
        with self.db.get_db() as db:
            db.execute("UPDATE chat_messages SET qq_state='' WHERE id=?",(mid,))
        self.fake.timeout_on={len(self.fake.calls)+1}
        with patch.object(auto,'ai_ready',return_value=True), \
                patch.object(auto,'ai_answer',new_callable=AsyncMock,side_effect=auto.AIError('fake error')):
            await auto.handle_unattended(other,'timeout')
        job=self.job(other)
        self.assertEqual(job['status'],'uncertain')
        self.assertIn('AI 服务暂不可用',job['reason'])
        count=len(self.fake.calls); await self.delivery.flush_backlog()
        self.assertEqual(len(self.fake.calls),count)
        await self.delivery.confirm_received(job['id'])
        self.assertEqual(self.job(other)['status'],'sent')
        third,mid=self.conversation('cancel on unbind')
        with self.db.get_db() as db:
            db.execute("UPDATE chat_messages SET qq_state='' WHERE id=?",(mid,))
        self.fake.fail_on.add(len(self.fake.calls)+1)
        with patch.object(auto,'ai_ready',return_value=False):
            await auto.handle_unattended(third,'timeout')
        await self.qq.unbind()
        self.assertEqual(self.job(third)['status'],'cancelled')

    async def test_handoff_and_visitor_failures_do_not_hide_each_other(self):
        from app import auto
        cid,_=self.conversation('original pending visitor')
        self.fake.timeout_on={1}
        await self.delivery.flush_conversation(cid)
        visitor_job=self.job(cid)
        with patch.object(auto,'ai_ready',return_value=False):
            await auto.handle_unattended(cid,'timeout')
        self.assertEqual(self.job(cid)['kind'],'handoff')
        self.assertEqual(self.job(cid)['status'],'sent')
        self.assertEqual(self.delivery.delivery_detail(visitor_job['id'])['delivery']['status'],'uncertain')
        other,_=self.conversation('visitor still needs forwarding')
        self.fake.fail_on={len(self.fake.calls)+1}
        with patch.object(auto,'ai_ready',return_value=False):
            await auto.handle_unattended(other,'timeout')
        jobs=self.delivery.list_deliveries(status=None,conversation_id=other,page=1,size=20)['items']
        self.assertEqual({d['kind']:d['status'] for d in jobs},{'handoff':'failed','visitor':'sent'})
        handoff=next(d for d in jobs if d['kind']=='handoff');self.make_due(handoff['id'])
        await self.delivery.retry_delivery(handoff['id'],self.delivery.RetryIn())
        self.assertEqual(self.delivery.delivery_detail(handoff['id'])['delivery']['status'],'sent')

    async def test_handoff_atomic_commit_and_agent_takeover_during_ai(self):
        from app import auto,chat
        from unittest.mock import AsyncMock
        cid,_=self.conversation('transaction must roll back')
        with patch.object(auto,'ai_ready',return_value=False), \
                patch.object(self.delivery,'enqueue_handoff',side_effect=RuntimeError('simulate outbox failure')):
            with self.assertRaises(RuntimeError): await auto.handle_unattended(cid,'timeout')
        with self.db.get_db() as db:
            self.assertEqual(db.execute('SELECT auto_handled_upto FROM conversations WHERE id=?',(cid,)).fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM messages WHERE conversation_id=?',(cid,)).fetchone()[0],0)
        async def agent_replies(*args):
            chat.add_message(cid,'agent','人工已经接管')
            return '',True
        with patch.object(auto,'ai_ready',return_value=True),patch.object(auto,'ai_answer',side_effect=agent_replies):
            await auto.handle_unattended(cid,'timeout')
        self.assertEqual(self.delivery.list_deliveries(status=None,conversation_id=cid,page=1,size=20)['total'],0)
        other,_=self.conversation('new offline messages')
        with patch.object(auto,'ai_ready',return_value=False),patch.object(chat,'agent_available',return_value=False):
            await asyncio.gather(auto.handle_unattended(other,'offline'),auto.handle_unattended(other,'offline'))
        self.assertEqual(self.delivery.list_deliveries(status=None,conversation_id=other,page=1,size=20,kind='handoff')['total'],1)
        self.assertIn('客服忙碌',next(d for d in self.delivery.list_deliveries(status=None,conversation_id=other,page=1,size=20)['items'] if d['kind']=='handoff')['reason'])

    async def test_handoff_no_binding_disabled_ai_success_and_summary_limits(self):
        from app import auto,chat
        from unittest.mock import AsyncMock
        cid,mid=self.conversation('offline while unbound')
        self.qq.update_agent(openid='')
        with patch.object(auto,'ai_ready',return_value=False): await auto.handle_unattended(cid,'timeout')
        self.assertEqual(self.delivery.list_deliveries(status=None,conversation_id=cid,page=1,size=20)['total'],0)
        with self.db.get_db() as db:
            self.assertEqual(db.execute('SELECT auto_handled_upto FROM conversations WHERE id=?',(cid,)).fetchone()[0],mid)
        self.qq.update_agent(openid='test-agent')
        other,mid=self.conversation('AI solves this')
        with patch.object(auto,'ai_ready',return_value=True), \
                patch.object(auto,'ai_answer',new_callable=AsyncMock,return_value=('正常回答',False)), \
                patch.object(auto,'notify_agent',new_callable=AsyncMock) as old_notify:
            await auto.handle_unattended(other,'timeout')
            old_notify.assert_awaited_once()
        self.assertEqual(self.delivery.list_deliveries(status=None,conversation_id=other,page=1,size=20)['total'],0)
        third,mid=self.conversation('first '+ 'x'*4000)
        with self.db.get_db() as db:
            for i in range(120):
                db.execute("INSERT INTO chat_messages (conversation_id,sender,content,msg_type) VALUES (?,'visitor',?,?)",(third,'/uploads/fake.png' if i==119 else 'question '+str(i)+'\n'+'Y'*4000,'image' if i==119 else 'text'))
            msgs=self.db.rows(db.execute("SELECT * FROM chat_messages WHERE conversation_id=? AND sender='visitor' ORDER BY id",(third,)))
            conv=chat.conv_summary(third)
            inbox=auto.archive(db,conv,msgs,'summary fixture')
            jid=self.delivery.enqueue_handoff(db,conv,msgs,'测试原因',inbox)
            self.assertEqual(self.delivery.enqueue_handoff(db,conv,msgs,'测试原因',inbox),jid)
        detail=self.delivery.delivery_detail(jid)
        self.assertLess(len(detail['delivery']['summary']),650)
        self.assertIn('[图片]',detail['delivery']['summary'])
        self.assertIn('省略 116 条',detail['delivery']['summary'])
        self.assertEqual(len(detail['messages']),100)
        self.assertEqual(detail['delivery']['message_count'],121)
        self.assertTrue(detail['messages_truncated'])

    async def test_handoff_migrates_existing_delivery_records(self):
        from app import db
        with tempfile.TemporaryDirectory(prefix='msghub-legacy-') as folder, patch.object(db,'DB_PATH',Path(folder)/'legacy.db'):
            legacy_schema=db.SCHEMA
            for line in ["    kind TEXT NOT NULL DEFAULT 'visitor',\n",'    event_key TEXT,\n',"    reason TEXT NOT NULL DEFAULT '',\n", "    summary TEXT NOT NULL DEFAULT '',\n",'    inbox_message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,\n']:
                legacy_schema=legacy_schema.replace(line,'')
            with db.get_db() as conn:
                conn.executescript(legacy_schema)
                conn.execute('DROP TABLE ai_decisions')
                sid=conn.execute("INSERT INTO sites (name,site_key) VALUES ('legacy','legacy')").lastrowid
                vid=conn.execute("INSERT INTO visitors (token) VALUES ('legacy')").lastrowid
                cid=conn.execute('INSERT INTO conversations (site_id,visitor_id) VALUES (?,?)',(sid,vid)).lastrowid
                jid=conn.execute("INSERT INTO qq_deliveries (conversation_id,recipient,status,attempts,last_error) VALUES (?,'old-agent','failed',3,'old failure')",(cid,)).lastrowid
            db.init_db();db.init_db()
            with db.get_db() as conn:
                row=conn.execute('SELECT * FROM qq_deliveries WHERE id=?',(jid,)).fetchone()
                self.assertEqual((row['kind'],row['status'],row['attempts'],row['last_error']),('visitor','failed',3,'old failure'))
                self.assertIsNone(row['event_key'])
                self.assertIsNone(row['inbox_message_id'])
                self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(),[])
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM ai_decisions').fetchone()[0],0)

    async def test_handled_or_taken_over_handoffs_stop_retrying(self):
        from app import auto,chat
        for action in ('reply','closed','handled','spam','deleted'):
            cid,mid=self.conversation('waiting '+action)
            with self.db.get_db() as db:
                db.execute("UPDATE chat_messages SET qq_state='' WHERE id=?",(mid,))
            self.fake.fail_on.add(len(self.fake.calls)+1)
            with patch.object(auto,'ai_ready',return_value=False): await auto.handle_unattended(cid,'timeout')
            job=self.job(cid);self.assertEqual(job['status'],'failed')
            self.make_due(job['id'])
            if action=='reply': chat.add_message(cid,'agent','人工已经回复')
            else:
                with self.db.get_db() as db:
                    if action=='closed': db.execute("UPDATE conversations SET status='closed' WHERE id=?",(cid,))
                    elif action=='deleted': db.execute('DELETE FROM messages WHERE id=?',(job['inbox_message_id'],))
                    else: db.execute('UPDATE messages SET status=? WHERE id=?',(action,job['inbox_message_id']))
            count=len(self.fake.calls)
            await self.delivery.flush_backlog()
            self.assertEqual(self.job(cid)['status'],'cancelled')
            self.assertEqual(len(self.fake.calls),count)

    async def test_handoff_empty_ai_answer_ai_excerpt_and_offline_causes(self):
        from app import auto,chat
        from unittest.mock import AsyncMock
        for response,cause in [(('',False),'AI 未返回有效回答'),(('先为您解释，再转人工',True),'AI 判断需要人工处理')]:
            cid,mid=self.conversation('需要进一步处理')
            with self.db.get_db() as db: db.execute("UPDATE chat_messages SET qq_state='' WHERE id=?",(mid,))
            with patch.object(auto,'ai_ready',return_value=True), \
                    patch.object(auto,'ai_answer',new_callable=AsyncMock,return_value=response), \
                    patch.object(auto,'notify_agent',new_callable=AsyncMock) as old_notify:
                await auto.handle_unattended(cid,'timeout')
                old_notify.assert_not_awaited()
            self.assertIn(cause,self.job(cid)['reason'])
            if response[0]:
                content=''.join(p['content'] for p in self.delivery.delivery_detail(self.job(cid)['id'])['parts'])
                self.assertIn('AI 刚才的回复：'+response[0],content)
        cid,mid=self.conversation('offline work hours')
        with self.db.get_db() as db:
            db.execute("UPDATE chat_messages SET qq_state='' WHERE id=?",(mid,))
            db.execute("UPDATE sites SET work_hours=? WHERE id=?",('{"days":[1,2,3,4,5,6,7],"start":"00:00","end":"00:00"}',self.site))
        with patch.object(auto,'ai_ready',return_value=False),patch.object(auto,'ai_settings',return_value={'enabled':True}):
            await auto.handle_unattended(cid,'offline')
        self.assertEqual(self.job(cid)['reason'],'非工作时间；AI 自动应答配置未完成')
        other,_=self.conversation('availability recovered during merge')
        with patch.object(chat,'site_online',return_value=True): await auto.handle_unattended(other,'offline')
        self.assertEqual(self.delivery.list_deliveries(status=None,conversation_id=other,page=1,size=20)['total'],0)

    async def test_ai_first_timeout_transfers_and_stops_ai_until_new_round(self):
        from app import auto, chat
        from unittest.mock import AsyncMock

        self.enable_fake_ai()
        cid, mid = self.conversation("等一个不会及时返回的模型")
        with self.db.get_db() as db:
            db.execute("UPDATE chat_messages SET qq_state='' WHERE id=?", (mid,))
        async def slow(*args):
            await asyncio.Event().wait()

        with patch.object(auto, "LIVE_REPLY_SECONDS", .04), patch.object(auto, "ai_answer", side_effect=slow) as model:
            await asyncio.wait_for(auto.handle_unattended(cid, "ai_first"), 1)
            chat.add_message(cid, "visitor", "补充给人工的信息")
            await auto.handle_unattended(cid, "ai_first")
            model.assert_awaited_once()
        with self.db.get_db() as db:
            results = [row["reason_code"] for row in db.execute(
                "SELECT reason_code FROM ai_decisions WHERE conversation_id=? ORDER BY id", (cid,))]
        self.assertEqual(results, ["reply_timeout", "human_pending"])
        self.assertEqual(self.delivery.list_deliveries(
            status=None, conversation_id=cid, kind="handoff", page=1, size=10)["total"], 1)
        self.assertTrue(chat.conv_summary(cid)["ai_waiting_for_agent"])
        self.assertIn(auto.HANDOFF_TEXT, [message["content"] for message in chat.history(cid)])
        await chat.set_conversation_status(cid, "closed")
        chat.add_message(cid, "visitor", "重新开始咨询")
        with patch.object(auto, "ai_answer", new_callable=AsyncMock, return_value=("新一轮 AI 回复", False)) as model:
            await auto.handle_unattended(cid, "ai_first")
            await chat.agent_reply(cid, "我来处理")
            chat.add_message(cid, "visitor", "给已接管客服的新消息")
            await auto.handle_unattended(cid, "ai_first")
            await auto.handle_unattended(cid, "timeout_archive")
            model.assert_awaited_once()
        self.assertFalse(chat.conv_summary(cid)["ai_waiting_for_agent"])
        self.assertEqual([x["content"] for x in chat.history(cid) if x["sender"]=="ai"], ["新一轮 AI 回复"])

    async def test_ai_first_handoff_does_not_wait_for_slow_summary(self):
        from app import auto, chat, summaries
        from unittest.mock import AsyncMock

        self.enable_fake_ai()
        cid, mid = self.conversation("请转人工")
        with self.db.get_db() as db:
            db.execute("UPDATE chat_messages SET qq_state='' WHERE id=?", (mid,))
        async def slow(*args):
            await asyncio.Event().wait()

        with patch.object(auto, "LIVE_SUMMARY_SECONDS", .04), patch.object(summaries, "prepare", side_effect=slow), \
                patch.object(auto, "ai_answer", new_callable=AsyncMock, return_value=("", True)):
            await asyncio.wait_for(auto.handle_unattended(cid, "ai_first"), 1)
        self.assertTrue(chat.conv_summary(cid)["ai_waiting_for_agent"])
        self.assertEqual(self.job(cid)["kind"], "handoff")
        self.assertEqual(self.job(cid)["status"], "pending")
        self.assertIn(auto.HANDOFF_TEXT, [m["content"] for m in chat.history(cid)])

    async def test_old_unanswered_batch_is_archived_without_a_late_ai_reply(self):
        from app import auto, chat
        from unittest.mock import AsyncMock

        self.enable_fake_ai()
        cid, mid = self.conversation("旧的来访消息")
        with patch.object(auto, "ai_answer", new_callable=AsyncMock) as model:
            await auto.handle_unattended(cid, "timeout_archive")
            model.assert_not_awaited()
        self.assertTrue(chat.conv_summary(cid)["ai_waiting_for_agent"])
        with self.db.get_db() as db:
            self.assertEqual(db.execute("SELECT reason_code FROM ai_decisions WHERE conversation_id=?", (cid,)).fetchone()[0],
                             "first_response_expired")

    def enable_fake_ai(self):
        self.db.set_settings({'ai_enabled':'1','ai_base_url':'http://fake-provider.invalid/v1',
                              'ai_api_key':'ai-fake-secret-never-expose','ai_model':'test-model','ai_prompt':'Private system instruction'})

    def test_partial_crawl_keeps_unavailable_and_unvisited_knowledge(self):
        from app import auto

        root = "https://docs.example.com/"
        help_url, removed_url, unvisited_url = (root + name for name in ("help", "removed", "unvisited"))
        site = {"id": self.site, "crawl_urls": root}
        with self.db.get_db() as db:
            other = db.execute("INSERT INTO sites (name,site_key) VALUES ('Other site','other-site')").lastrowid
            db.executemany("INSERT INTO kb_pages (site_id,url,text) VALUES (?,?,?)", [
                (self.site, root, "Old homepage"),
                (self.site, help_url, "旧版退款帮助中心"),
                (self.site, removed_url, "Old removed article"),
                (self.site, unvisited_url, "Old unvisited article"),
                (other, removed_url, "Other site's article"),
            ])

        responses = {
            root + "robots.txt": b"User-agent: *\nDisallow:",
            root: b'Updated homepage <a href="/help">help</a> <a href="/removed">old article</a>',
            help_url: 503,
            removed_url: 410,
        }

        def fetch(url, _hosts):
            result = responses[url]
            if isinstance(result, int):
                raise HTTPError(url, result, "Fixture response", {}, None)
            return url, "text/html; charset=utf-8", result

        def saved_pages(site_id):
            with self.db.get_db() as db:
                return dict(db.execute("SELECT url,text FROM kb_pages WHERE site_id=?", (site_id,)).fetchall())

        with patch.object(auto, "_fetch", side_effect=fetch), patch.object(auto.time, "sleep"):
            self.assertEqual(auto.crawl(site), {"ok": True, "pages": 1, "errors": 2})
            self.assertEqual(saved_pages(self.site), {
                root: "Updated homepage help old article",
                help_url: "旧版退款帮助中心",
                unvisited_url: "Old unvisited article",
            })
            self.assertIn("旧版退款帮助中心", auto.retrieve_sources(self.site, "退款帮助")[0])
            self.assertEqual(saved_pages(other), {removed_url: "Other site's article"})

            responses[help_url] = "新版退款帮助中心内容已更新".encode()
            self.assertEqual(auto.crawl(site), {"ok": True, "pages": 2, "errors": 1})
            self.assertEqual(saved_pages(self.site)[help_url], "新版退款帮助中心内容已更新")

            responses[root] = 404
            self.assertFalse(auto.crawl(site)["ok"])
            self.assertEqual(set(saved_pages(self.site)), {help_url, unvisited_url})

    async def test_ai_retrieval_scope_snapshots_and_budget(self):
        from app import auto
        with self.db.get_db() as db:
            other=db.execute("INSERT INTO sites (name,site_key) VALUES ('other','other')").lastrowid
            db.execute("INSERT INTO qa (question,answer) VALUES ('套餐 pricing','Global answer')")
            db.execute("INSERT INTO qa (site_id,question,answer) VALUES (?,'套餐 pricing','Site answer')",(self.site,))
            db.execute("INSERT INTO qa (site_id,question,answer) VALUES (?,'套餐 pricing','Other site private answer')",(other,))
            db.execute("INSERT INTO kb_pages (site_id,url,title,text) VALUES (?,'https://example.com/pricing','套餐页面','套餐 pricing 页面资料')",(self.site,))
            db.execute("INSERT INTO kb_pages (site_id,url,title,text) VALUES (?,'https://other.example/pricing','其他站点','套餐 pricing 私有网页')",(other,))
        context,sources=auto.retrieve_sources(self.site,'套餐 pricing')
        self.assertEqual(len(sources),3)
        self.assertEqual(context,'\n\n'.join(f"[资料{s['ref']}] {s['snippet']}" for s in sources))
        self.assertTrue({'全局','本站'}<={s['scope'] for s in sources})
        self.assertTrue({'qa','page'}=={s['type'] for s in sources})
        self.assertNotIn('Other site',context);self.assertNotIn('其他站点',context)
        with self.db.get_db() as db:
            db.execute('DELETE FROM qa')
            db.execute("INSERT INTO qa (question,answer) VALUES (?,?)",('套餐 pricing '*100,'Large answer '*1000))
        context,sources=auto.retrieve_sources(self.site,'套餐 pricing',limit_chars=150)
        self.assertLessEqual(len(context),150)
        self.assertTrue(sources[0]['truncated'])
        self.assertIn('问：',context)
        self.assertEqual(auto.retrieve_sources(self.site,''),('',[]))

    async def test_ai_answer_atomic_audit_immutable_and_private(self):
        from app import auto,chat
        from unittest.mock import AsyncMock
        self.enable_fake_ai()
        cid,_=self.conversation('套餐多少钱 pricing')
        with self.db.get_db() as db:
            qid=db.execute("INSERT INTO qa (site_id,question,answer) VALUES (?,'套餐 pricing','套餐每月 100 元')",(self.site,)).lastrowid
            db.execute("INSERT INTO conversation_notes (conversation_id,content) VALUES (?,'内部备注不能进入模型')",(cid,))
        with patch.object(auto,'_completion',return_value='套餐每月 100 元。') as completion, \
                patch.object(auto,'notify_agent',new_callable=AsyncMock), \
                patch.object(chat,'broadcast_message',new_callable=AsyncMock) as broadcast:
            await auto.handle_unattended(cid,'timeout')
            await auto.handle_unattended(cid,'timeout')
            self.assertEqual(completion.call_count,1)
            prompt=completion.call_args.args[3][0]['content']
            self.assertNotIn('内部备注不能进入模型',str(completion.call_args))
            self.assertEqual(broadcast.await_count,1)
        with self.db.get_db() as db:
            row=db.execute('SELECT * FROM ai_decisions WHERE conversation_id=?',(cid,)).fetchone()
            self.assertEqual(row['outcome'],'answered')
            self.assertEqual(row['source_count'],1)
            self.assertEqual(row['model'],'test-model')
            self.assertIn(row['context'],prompt)
            self.assertNotIn('ai-fake-secret-never-expose',str(dict(row)))
            self.assertNotIn('Private system instruction',str(dict(row)))
            self.assertEqual(db.execute('SELECT sender,content FROM chat_messages WHERE id=?',(row['message_id'],)).fetchone()[:],('ai','套餐每月 100 元。'))
            db.execute('DELETE FROM qa WHERE id=?',(qid,))
        detail=auto.decision_detail(cid,row['id'])
        self.assertIn('100 元',detail['sources'][0]['snippet'])
        public=chat.history(cid)
        self.assertTrue(all(set(m)=={'id','sender','msg_type','content','created_at'} for m in public))
        self.assertNotIn('sources',str(public))
        with self.db.get_db() as db:
            db.execute('DELETE FROM conversations WHERE id=?',(cid,))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM ai_decisions WHERE id=?',(row['id'],)).fetchone()[0],0)

    async def test_ai_handoff_reason_markers_and_record_delivery_link(self):
        from app import auto
        self.enable_fake_ai()
        for text,code in [('[转人工:资料不足]','insufficient_sources'),('[转人工： 访客要求]','visitor_requested'),
                          ('[转人工:需人工办理]','human_required'),('[转人工]','model_handoff'),
                          ('[转人工:未识别分类]','model_handoff'),('[转人工:资料不足','model_handoff'),
                          ('[转人工:'+ 'X'*100 +']','model_handoff'),('  ','empty_answer')]:
            cid,mid=self.conversation('请处理本问题')
            with self.db.get_db() as db: db.execute("UPDATE chat_messages SET qq_state='' WHERE id=?",(mid,))
            with patch.object(auto,'_completion',return_value=text): await auto.handle_unattended(cid,'timeout')
            with self.db.get_db() as db:
                row=db.execute('SELECT * FROM ai_decisions WHERE conversation_id=?',(cid,)).fetchone()
                self.assertEqual(row['reason_code'],code)
                self.assertEqual(row['source_count'],0)
                self.assertTrue(row['inbox_message_id'])
                self.assertEqual(row['qq_delivery_id'],self.job(cid)['id'])
                self.assertNotIn('[转人工',row['answer'])
            self.assertIn(auto.REASONS[code],self.job(cid)['reason'])

    async def test_ai_error_evidence_redaction_and_transaction_rollback(self):
        from app import auto
        self.enable_fake_ai()
        cid,mid=self.conversation('退款 policy')
        with self.db.get_db() as db:
            db.execute("UPDATE chat_messages SET qq_state='' WHERE id=?",(mid,))
            db.execute("INSERT INTO qa (question,answer) VALUES ('退款 policy','仅限人工处理')")
        with patch.object(auto,'_completion',side_effect=auto.AIError('provider leaked ai-fake-secret-never-expose')):
            await auto.handle_unattended(cid,'timeout')
        with self.db.get_db() as db:
            row=db.execute('SELECT * FROM ai_decisions WHERE conversation_id=?',(cid,)).fetchone()
            self.assertEqual(row['reason_code'],'api_error')
            self.assertEqual(row['source_count'],1)
            self.assertNotIn('provider leaked',str(dict(row)))
            self.assertNotIn('ai-fake-secret',str(dict(row)))
        other,_=self.conversation('atomic answer')
        with patch.object(auto,'_completion',return_value='Answer for atomic test'), \
                patch.object(auto,'save_decision',side_effect=RuntimeError('local write failed')):
            with self.assertRaises(RuntimeError): await auto.handle_unattended(other,'timeout')
        with self.db.get_db() as db:
            self.assertEqual(db.execute('SELECT auto_handled_upto FROM conversations WHERE id=?',(other,)).fetchone()[0],0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM chat_messages WHERE conversation_id=? AND sender='ai'",(other,)).fetchone()[0],0)
        self.qq.update_agent(openid='')
        third,_=self.conversation('no bot still records disabled decision '*200)
        with patch.object(auto,'ai_ready',return_value=False): await auto.handle_unattended(third,'timeout')
        with self.db.get_db() as db:
            row=db.execute('SELECT * FROM ai_decisions WHERE conversation_id=?',(third,)).fetchone()
            self.assertEqual(row['reason_code'],'unconfigured')
            self.assertIsNone(row['qq_delivery_id'])
            self.assertEqual(len(row['question']),6000)
            self.assertEqual(row['question_truncated'],1)

    async def test_summary_extraction_grounding_bounds_and_private_sources(self):
        import json
        from app import auto,chat,summaries
        self.enable_fake_ai()
        cid,mid=self.conversation('需要两个套餐，QQ 12345678 <img src=x>')
        agent=chat.add_message(cid,'agent','客服邮箱 agent@example.com')['id']
        with self.db.get_db() as db:
            db.execute("INSERT INTO conversation_notes (conversation_id,content) VALUES (?,'private-note-for-summary')",(cid,))
        output={'summary':'客户咨询套餐，等待报价。','needs':[
            {'value':'两个套餐','message_id':mid,'quote':'需要两个套餐'},
            {'value':'捏造','message_id':mid,'quote':'没有这段原话'},
            {'value':'跨会话','message_id':99999,'quote':'需要两个套餐'}],
            'contacts':[
                {'value':'12345678','message_id':mid,'quote':'QQ 12345678'},
                {'value':'87654321','message_id':mid,'quote':'QQ 12345678'},
                {'value':'agent@example.com','message_id':agent,'quote':'客服邮箱 agent@example.com'}]}
        with patch.object(auto,'_completion',return_value=json.dumps(output)) as call:
            result=await summaries.generate(cid)
            item=result['item']
            self.assertEqual(len(item['needs']),1);self.assertEqual(len(item['contacts']),1)
            self.assertEqual(item['contacts'][0]['message_id'],mid)
            self.assertNotIn('private-note-for-summary',str(call.call_args))
            self.assertNotIn('Private system instruction',str(call.call_args))
            await summaries.generate(cid)
            self.assertEqual(call.call_count,1)
        self.assertFalse(result['stale'])
        with self.db.get_db() as db:
            self.assertEqual(db.execute('SELECT contact FROM visitors WHERE id=(SELECT visitor_id FROM conversations WHERE id=?)',(cid,)).fetchone()[0],'')
            db.execute("UPDATE chat_messages SET content='后来修改' WHERE id=?",(mid,))
        self.assertIn('QQ 12345678',summaries.latest(cid)['item']['sources'][0]['content'])
        chat.add_message(cid,'visitor','新增问题')
        self.assertTrue(summaries.latest(cid)['stale'])
        self.assertNotIn('needs',str(chat.history(cid)))
        with self.db.get_db() as db:
            db.execute('DELETE FROM sites WHERE id=?',(self.site,))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM conversation_summaries WHERE conversation_id=?',(cid,)).fetchone()[0],0)

    async def test_summary_handoff_atomicity_snapshot_and_retry(self):
        import json
        from app import auto,summaries
        from unittest.mock import AsyncMock
        self.enable_fake_ai()
        cid,mid=self.conversation('我要退款，QQ 12345678')
        with self.db.get_db() as db: db.execute("UPDATE chat_messages SET qq_state='' WHERE id=?",(mid,))
        output=json.dumps({'summary':'客户申请退款，等待人工处理。','needs':[{'value':'退款','message_id':mid,'quote':'我要退款'}],
                           'contacts':[{'value':'12345678','message_id':mid,'quote':'QQ 12345678'}]})
        with patch.object(auto,'ai_answer',new_callable=AsyncMock,return_value=('',True)), \
                patch.object(auto,'_completion',return_value=output), \
                patch.object(self.delivery,'enqueue_handoff',side_effect=RuntimeError('atomic failure')):
            with self.assertRaises(RuntimeError): await auto.handle_unattended(cid,'timeout')
        self.assertIsNone(summaries.latest(cid)['item'])
        self.fake.fail_on={1}
        with patch.object(auto,'ai_answer',new_callable=AsyncMock,return_value=('',True)),patch.object(auto,'_completion',return_value=output):
            await auto.handle_unattended(cid,'timeout')
        item=summaries.latest(cid)['item'];self.assertEqual(item['status'],'ready')
        job=self.job(cid);detail=self.delivery.delivery_detail(job['id'])
        text=''.join(p['content'] for p in detail['parts'])
        self.assertIn('AI 会话总结（供核对）',text);self.assertIn('12345678',text)
        self.assertEqual(detail['delivery']['summary_id'],item['id'])
        self.assertIn('原话摘录',text)
        with patch.object(auto,'_completion',side_effect=AssertionError('retry must not regenerate')):
            self.make_due(job['id']);await self.delivery.retry_delivery(job['id'],self.delivery.RetryIn())
        self.assertEqual(text,''.join(p['content'] for p in self.delivery.delivery_detail(job['id'])['parts']))

    async def test_summary_errors_do_not_prevent_handoff_and_manual_retry(self):
        import json
        from app import auto,summaries
        from unittest.mock import AsyncMock
        self.enable_fake_ai()
        cid,mid=self.conversation('请转人工')
        with self.db.get_db() as db: db.execute("UPDATE chat_messages SET qq_state='' WHERE id=?",(mid,))
        with patch.object(auto,'ai_answer',new_callable=AsyncMock,return_value=('',True)), \
                patch.object(auto,'_completion',side_effect=auto.AIError('private-provider-secret')):
            await auto.handle_unattended(cid,'timeout')
        item=summaries.latest(cid)['item']
        self.assertEqual(item['error_code'],'api_error');self.assertNotIn('private-provider-secret',str(item))
        self.assertEqual(self.job(cid)['status'],'sent')
        self.assertIsNone(self.job(cid)['summary_id'])
        for value in ['invalid json','{}','[]','{"summary": 123}']:
            with patch.object(auto,'_completion',return_value=value):
                self.assertEqual((await summaries.generate(cid))['item']['error_code'],'invalid_output')
        with patch.object(auto,'_completion',return_value=json.dumps({'summary':'等待人工。'})):
            ready=(await summaries.generate(cid))['item']
        self.assertEqual(ready['id'],item['id']);self.assertEqual(ready['status'],'ready')

    async def test_summary_concurrency_new_messages_and_takeover(self):
        import json,threading
        from app import auto,chat,summaries
        from unittest.mock import AsyncMock
        self.enable_fake_ai()
        cid,mid=self.conversation('并发总结')
        with patch.object(auto,'_completion',return_value=json.dumps({'summary':'并发总结'})) as call:
            results=await asyncio.gather(summaries.generate(cid),summaries.generate(cid))
            self.assertEqual(call.call_count,1);self.assertEqual(results[0]['item']['id'],results[1]['item']['id'])
        chat.add_message(cid,'visitor','追加消息')
        started,release=threading.Event(),threading.Event()
        def delayed(*args):
            started.set();release.wait(5);return json.dumps({'summary':'等待人工'})
        with patch.object(auto,'_completion',side_effect=delayed):
            task=asyncio.create_task(summaries.generate(cid))
            await asyncio.to_thread(started.wait,3)
            chat.add_message(cid,'visitor','生成期间又一条消息');release.set()
            self.assertTrue((await task)['stale'])
        other,mid=self.conversation('人工接管时不要发送过期摘要')
        started.clear();release.clear()
        with patch.object(auto,'ai_answer',new_callable=AsyncMock,return_value=('',True)),patch.object(auto,'_completion',side_effect=delayed):
            task=asyncio.create_task(auto.handle_unattended(other,'timeout'))
            await asyncio.to_thread(started.wait,3)
            chat.add_message(other,'agent','我来处理');release.set();await task
        self.assertIsNone(summaries.latest(other)['item'])
        self.assertEqual(self.delivery.list_deliveries(status=None,conversation_id=other,page=1,size=20)['total'],0)

    async def test_summary_large_history_and_scoping(self):
        from app import chat,summaries
        cid,_=self.conversation('第一条')
        other,_=self.conversation('其他站点/会话的私有内容')
        for _ in range(65): chat.add_message(cid,'visitor','A'*1000)
        chat.add_message(cid,'visitor','/uploads/private-image.png',msg_type='image')
        snap=summaries.snapshot(cid)
        self.assertTrue(snap['truncated']);self.assertLessEqual(len(snap['sources']),60)
        self.assertEqual(sum(len(m['content']) for m in snap['sources']),12000)
        self.assertNotIn('/uploads/private-image.png',str(snap));self.assertNotIn('其他站点/会话',str(snap))
        self.assertIn('[图片，未识别内容]',str(snap))
        self.db.init_db();self.db.init_db()

    async def test_summary_qq_command_is_private_and_uses_target(self):
        import json
        from app import auto,chat
        from unittest.mock import AsyncMock
        self.enable_fake_ai()
        cid,_=self.conversation('QQ 总结命令')
        before=chat.history(cid)
        with patch.object(auto,'_completion',return_value=json.dumps({'summary':'QQ 私有总结'})), \
                patch.object(self.qq,'deliver',new_callable=AsyncMock) as send:
            await self.qq.handle_command('/总结 #'+str(cid),self.qq.agent())
            self.assertIn('#'+str(cid),send.call_args.args[0]);self.assertIn('QQ 私有总结',send.call_args.args[0])
            await self.qq.handle_command('/总结 #999999',self.qq.agent())
            self.assertIn('不存在',send.call_args.args[0])
        self.assertEqual(before,chat.history(cid))

    def triage_output(self,mid,tag_id=None,quote='紧急故障需要处理',priority='high'):
        import json
        return json.dumps({'category':{'code':'support','message_id':mid,'quote':quote},
                           'priority':{'level':priority,'message_id':mid,'quote':quote},
                           'tags':[{'id':tag_id,'name':'售后','message_id':mid,'quote':quote}] if tag_id else []})

    def enable_triage(self):
        self.enable_fake_ai()
        self.db.set_settings({'ai_triage_enabled':'1','ai_auto_tags':'1'})

    async def test_triage_grounded_enums_and_catalog_validation(self):
        import json
        from app import triage,chat
        cid,mid=self.conversation('紧急故障需要处理')
        agent=chat.add_message(cid,'agent','虚构原话')['id']
        sources=__import__('app.summaries',fromlist=['snapshot']).snapshot(cid)['sources']
        tag={'id':7,'name':'售后'}
        text=json.loads(self.triage_output(mid,7))
        text['tags'] += [{'id':999,'name':'自建标签','message_id':mid,'quote':'紧急故障'},
                         {'id':7,'name':'被改名','message_id':mid,'quote':'紧急故障'}]
        parsed=triage.parse(json.dumps(text),sources,[tag],0)
        self.assertEqual(parsed['category'],'support');self.assertEqual(parsed['priority'],'high')
        self.assertEqual(len(parsed['tag_suggestions']),1)
        self.assertEqual(triage.parse(json.dumps(text),sources,[tag],agent)['priority'],'normal')
        text['category']['code']=['invalid'];text['priority']['message_id']=agent
        text['tags'][0]['quote']='不在原话中'
        parsed=triage.parse(json.dumps(text),sources,[tag],0)
        self.assertEqual(parsed['category'],'other');self.assertEqual(parsed['priority'],'normal')
        self.assertEqual(parsed['tag_suggestions'],[])
        with self.assertRaises(ValueError): triage.parse('{}',sources,[],0)

    async def test_triage_atomic_tags_alert_dedup_cooldown_and_retry(self):
        from app import triage,auto,chat
        self.enable_triage()
        cid,mid=self.conversation('紧急故障需要处理')
        with self.db.get_db() as db:
            tag=db.execute("SELECT id FROM tags WHERE name='售后'").fetchone()[0]
            db.execute("UPDATE chat_messages SET qq_state='' WHERE id=?",(mid,))
            db.execute("INSERT INTO conversation_notes (conversation_id,content) VALUES (?,'triage-private-note')",(cid,))
        with patch.object(auto,'_completion',return_value=self.triage_output(mid,tag)) as call, \
                patch.object(triage,'enqueue_priority',side_effect=RuntimeError('outbox failed')):
            with self.assertRaises(RuntimeError): await triage.generate(cid,automatic=True)
            self.assertNotIn('triage-private-note',str(call.call_args))
            self.assertNotIn('Private system instruction',str(call.call_args))
        self.assertIsNone(triage.detail(cid)['item']);self.assertEqual(chat.conv_summary(cid)['tags'],[])
        self.fake.fail_on={1}
        with patch.object(auto,'_completion',return_value=self.triage_output(mid,tag)) as call:
            result=await triage.generate(cid,automatic=True)
            await triage.generate(cid,automatic=True)
            self.assertEqual(call.call_count,1)
        job=self.job(cid);self.assertEqual(job['kind'],'priority');self.assertEqual(job['status'],'failed')
        self.assertTrue(chat.conv_summary(cid)['triage']['high']);self.assertEqual(chat.conv_summary(cid)['tags'][0]['id'],tag)
        text=''.join(p['content'] for p in self.delivery.delivery_detail(job['id'])['parts'])
        self.assertIn('高优先级',text);self.assertIn('紧急故障',text)
        self.make_due(job['id']);await self.delivery.retry_delivery(job['id'],self.delivery.RetryIn())
        self.assertEqual(self.job(cid)['status'],'sent')
        new=chat.add_message(cid,'visitor','紧急故障需要处理')['id']
        with patch.object(auto,'_completion',return_value=self.triage_output(new,tag)):
            await triage.generate(cid,automatic=True)
        self.assertEqual(self.delivery.list_deliveries(status=None,conversation_id=cid,page=1,size=20,kind='priority')['total'],1)
        self.assertEqual(triage.detail(cid)['item']['source_upto'],new)
        self.assertNotIn('category_evidence',str(chat.history(cid)))

    async def test_triage_manual_tag_removal_and_catalog_changes(self):
        from app import triage,auto,chat
        self.enable_triage();self.qq.update_agent(openid='')
        cid,mid=self.conversation('紧急故障需要处理')
        with self.db.get_db() as db: tag=db.execute("SELECT id FROM tags WHERE name='售后'").fetchone()[0]
        with patch.object(auto,'_completion',return_value=self.triage_output(mid,tag)):
            result=await triage.generate(cid,automatic=True)
        await chat._change_tags([cid],tag,'remove')
        await triage.generate(cid,automatic=True)
        self.assertEqual(chat.conv_summary(cid)['tags'],[])
        await triage.apply_tags(cid,result['item']['id'])
        self.assertEqual(chat.conv_summary(cid)['tags'][0]['id'],tag)
        await chat._change_tags([cid],tag,'remove')
        with self.db.get_db() as db: db.execute("UPDATE tags SET name='改名' WHERE id=?",(tag,))
        def restore_tag():
            with self.db.get_db() as db: db.execute("UPDATE tags SET name='售后' WHERE id=?",(tag,))
        self.addCleanup(restore_tag)
        await triage.apply_tags(cid,result['item']['id'])
        self.assertEqual(chat.conv_summary(cid)['tags'],[])
        chat.add_message(cid,'visitor','新消息')
        with self.assertRaises(__import__('fastapi').HTTPException) as exc: await triage.apply_tags(cid,result['item']['id'])
        self.assertEqual(exc.exception.status_code,409)

    async def test_triage_stale_takeover_binding_change_and_dismiss_during_model(self):
        import threading
        from app import triage,auto,chat
        self.enable_triage()
        for change in ['agent','closed','new_visitor','dismiss','binding']:
            cid,mid=self.conversation('紧急故障需要处理')
            with self.db.get_db() as db: db.execute("UPDATE chat_messages SET qq_state='' WHERE id=?",(mid,))
            started,release=threading.Event(),threading.Event()
            def completion(*args):
                started.set();release.wait(5);return self.triage_output(mid)
            with patch.object(auto,'_completion',side_effect=completion):
                task=asyncio.create_task(triage.generate(cid,automatic=True))
                await asyncio.to_thread(started.wait,3)
                if change=='agent': chat.add_message(cid,'agent','我来处理')
                if change=='closed':
                    with self.db.get_db() as db: db.execute("UPDATE conversations SET status='closed' WHERE id=?",(cid,))
                if change=='new_visitor': chat.add_message(cid,'visitor','新的需求')
                if change=='dismiss': await triage.dismiss(cid)
                if change=='binding': self.qq.update_agent(openid='new-agent')
                release.set();result=await task
            self.assertEqual(self.delivery.list_deliveries(status=None,conversation_id=cid,page=1,size=20)['total'],0,change)
            if change=='dismiss': self.assertTrue(result['item']['dismissed'])

    async def test_triage_obsolete_alert_and_stream_independence(self):
        from app import triage,auto,chat
        self.enable_triage()
        cid,mid=self.conversation('紧急故障需要处理')
        self.fake.timeout_on={1}
        with patch.object(auto,'_completion',return_value=self.triage_output(mid)):
            await triage.generate(cid,automatic=True)
        jobs=self.delivery.list_deliveries(status=None,conversation_id=cid,page=1,size=20)['items']
        self.assertEqual({j['kind']:j['status'] for j in jobs},{'priority':'uncertain','visitor':'sent'})
        priority=next(j for j in jobs if j['kind']=='priority')
        await triage.dismiss(cid)
        self.assertEqual(self.delivery.delivery_detail(priority['id'])['delivery']['status'],'cancelled')
        self.assertFalse(chat.conv_summary(cid)['triage']['high'])
        other,mid=self.conversation('紧急故障需要处理')
        self.fake.fail_on={len(self.fake.calls)+1}
        with patch.object(auto,'_completion',return_value=self.triage_output(mid)):
            await triage.generate(other,automatic=True)
        jid=next(j['id'] for j in self.delivery.list_deliveries(status=None,conversation_id=other,page=1,size=20)['items'] if j['kind']=='priority')
        chat.add_message(other,'agent','已接管')
        new=chat.add_message(other,'visitor','紧急故障需要处理')['id']
        with patch.object(auto,'_completion',return_value=self.triage_output(new)):
            await triage.generate(other,automatic=True)
        self.assertEqual(self.delivery.delivery_detail(jid)['delivery']['status'],'cancelled')
        self.assertEqual(next(j['status'] for j in self.delivery.list_deliveries(status=None,conversation_id=other,page=1,size=20)['items'] if j['kind']=='priority' and j['id']!=jid),'sent')

    async def test_triage_durable_jobs_coalesce_retry_and_disabled_privacy(self):
        from app import triage,auto,chat
        self.enable_triage();self.qq.update_agent(openid='')
        cid,mid=self.conversation('紧急故障需要处理')
        await triage.on_visitor_message({'id':cid,'site_id':self.site},{'id':mid})
        new=chat.add_message(cid,'visitor','紧急故障需要处理')['id']
        await triage.on_visitor_message({'id':cid,'site_id':self.site},{'id':new})
        with self.db.get_db() as db:
            self.assertEqual(db.execute('SELECT source_upto FROM conversation_triage_jobs WHERE conversation_id=?',(cid,)).fetchone()[0],new)
        with patch.object(auto,'_completion',side_effect=auto.AIError('triage-provider-secret')):
            await triage._work(cid)
        self.assertNotIn('triage-provider-secret',str(triage.detail(cid)))
        with patch.object(auto,'_completion',return_value=self.triage_output(new)):
            await triage._work(cid)
        with self.db.get_db() as db:
            job=db.execute('SELECT * FROM conversation_triage_jobs WHERE conversation_id=?',(cid,)).fetchone()
            self.assertEqual(job['source_upto'],0)
        third=chat.add_message(cid,'visitor','紧急故障需要处理')['id']
        await triage.on_visitor_message({'id':cid,'site_id':self.site},{'id':third})
        chat.add_message(cid,'agent','人工已回复')
        await triage._work(cid)
        with self.db.get_db() as db:
            self.assertEqual(db.execute('SELECT source_upto FROM conversation_triage_jobs WHERE conversation_id=?',(cid,)).fetchone()[0],0)
        self.db.set_settings({'ai_triage_enabled':'0'})
        await triage.on_visitor_message({'id':cid,'site_id':self.site},{'id':third})
        with self.db.get_db() as db:
            self.assertEqual(db.execute('SELECT source_upto FROM conversation_triage_jobs WHERE conversation_id=?',(cid,)).fetchone()[0],0)
        # A queued worker waiting for a manual analysis must not revive a binding-cancelled job.
        fourth,mid=self.conversation('紧急故障需要处理')
        self.db.set_settings({'ai_triage_enabled':'1'})
        self.qq.update_agent(openid='old-agent')
        await triage.on_visitor_message({'id':fourth,'site_id':self.site},{'id':mid})
        async with triage._locks[fourth]:
            task=asyncio.create_task(triage._work(fourth))
            await asyncio.sleep(.01)
            self.delivery.cancel_for_binding_change()
            self.qq.update_agent(openid='new-agent')
        with patch.object(auto,'_completion',side_effect=AssertionError('Cancelled job must not call the provider')) as provider:
            await task
            provider.assert_not_called()
        self.assertIsNone(triage.detail(fourth)['item'])
        self.db.init_db();self.db.init_db()

    async def test_triage_qq_commands_and_delete_cascade(self):
        from app import triage,auto,chat
        from unittest.mock import AsyncMock
        self.enable_fake_ai()
        cid,mid=self.conversation('紧急故障需要处理')
        before=chat.history(cid)
        with patch.object(auto,'_completion',return_value=self.triage_output(mid)),patch.object(self.qq,'deliver',new_callable=AsyncMock) as say:
            await self.qq.handle_command('/分类 #'+str(cid),self.qq.agent())
            self.assertIn('技术支持',say.call_args.args[0]);self.assertIn('紧急故障',say.call_args.args[0])
            await self.qq.handle_command('/普通 #'+str(cid),self.qq.agent())
            self.assertIn('已取消',say.call_args.args[0])
        self.assertEqual(before,chat.history(cid))
        with self.db.get_db() as db:
            db.execute('DELETE FROM sites WHERE id=?',(self.site,))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM conversation_triage WHERE conversation_id=?',(cid,)).fetchone()[0],0)


if __name__=='__main__':
    unittest.main()

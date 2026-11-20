"""Run with the application dependencies: python -m unittest discover -s tests -v.

Each run uses a temporary database and a separate Uvicorn process.
No production configuration, QQ credentials or existing records are used.
"""
import csv
import http.client
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from contextlib import ExitStack
from datetime import datetime
from urllib.parse import urlencode

from websockets.sync.client import connect


class WorkHoursTests(unittest.TestCase):
    def test_overnight_hours_follow_the_day_the_shift_started(self):
        from app.common import in_work_hours

        cases = [
            ([1], "20:00", "02:00", datetime(2026, 11, 23, 1), False),
            ([1], "20:00", "02:00", datetime(2026, 11, 23, 20), True),
            ([1], "20:00", "02:00", datetime(2026, 11, 24, 1), True),
            ([1], "20:00", "02:00", datetime(2026, 11, 24, 2), False),
            ([7], "20:00", "02:00", datetime(2026, 11, 23, 1), True),
            ([1], "09:00", "18:00", datetime(2026, 11, 23, 9), True),
            ([1], "09:00", "18:00", datetime(2026, 11, 23, 18), False),
            ([1], "00:00", "00:00", datetime(2026, 11, 23), False),
        ]
        for days, start, end, now, expected in cases:
            with self.subTest(days=days, start=start, end=end, now=now):
                schedule = json.dumps({"days": days, "start": start, "end": end})
                self.assertEqual(in_work_hours(schedule, now), expected)


class CustomerToolsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="msghub-tests-")
        cls.addClassCleanup(cls.tmp.cleanup)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        cls.base = f"http://127.0.0.1:{port}"
        cls.ws_base = f"ws://127.0.0.1:{port}"
        env = dict(os.environ, DATA_DIR=cls.tmp.name, ADMIN_PASSWORD="test-only", AI_ALLOW_LOOPBACK_HTTP="1",
                   SECRET_KEY="test-only", QQ_APP_ID="", QQ_APP_SECRET="")
        cls.env = env
        cls.server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
             "--port", str(port), "--log-level", "error"],
            cwd=Path(__file__).resolve().parents[1], env=env,
        )
        cls.addClassCleanup(cls.stop_server)
        # Cold container imports can exceed five seconds while Chromium is also running.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                cls.request("GET", "/api/health", authenticated=False)
                break
            except (OSError, urllib.error.URLError):
                if cls.server.poll() is not None:
                    raise RuntimeError("Test server failed to start")
                time.sleep(.05)
        else:
            raise RuntimeError("Timed out waiting for test server")
        cls.token = cls.request("POST", "/api/admin/login", {"password": "test-only"},
                                authenticated=False)[1]["token"]
        # Startup migrations must also be safe to run again on an existing database.
        subprocess.run([sys.executable, "-c", "from app.db import init_db; init_db(); init_db()"],
                       cwd=Path(__file__).resolve().parents[1], env=env, check=True)

    @classmethod
    def stop_server(cls):
        cls.server.terminate()
        try:
            cls.server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.server.kill()
            cls.server.wait()

    @classmethod
    def request(cls, method, path, body=None, authenticated=True):
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers["Authorization"] = "Bearer " + cls.token
        req = urllib.request.Request(
            cls.base + path, method=method, headers=headers,
            data=json.dumps(body).encode() if body is not None else None,
        )
        try:
            response = urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, json.loads(response.read())

    def setUp(self):
        self.sites = []
        for name in ("Site A", "Site B"):
            status, site = self.request("POST", "/api/admin/sites", {
                "name": name, "require_contact": False, "ai_enabled": False,
            })
            self.assertEqual(status, 200)
            self.sites.append(site)
            self.addCleanup(self.request, "DELETE", f"/api/admin/sites/{site['id']}")

    def create_reply(self, title, site_id=None):
        status, reply = self.request("POST", "/api/admin/quick-replies",
                                     {"title": title, "content": "Content", "site_id": site_id})
        self.assertEqual(status, 200)
        self.addCleanup(self.request, "DELETE", f"/api/admin/quick-replies/{reply['id']}")
        return reply

    def create_tag(self, name):
        status, tag = self.request("POST", "/api/admin/tags", {"name": name})
        self.assertEqual(status, 200)
        self.addCleanup(self.request, "DELETE", f"/api/admin/tags/{tag['id']}")
        return tag

    def create_conversation(self, site, text="Tag visitor"):
        with connect(self.ws_base + "/api/chat/ws") as ws:
            ws.send(json.dumps({"type": "hello", "site_key": site["site_key"]}))
            self.assertEqual(json.loads(ws.recv())["type"], "init")
            ws.send(json.dumps({"type": "msg", "content": text}))
            while json.loads(ws.recv())["type"] != "msg":
                pass
            return self.request("GET", f"/api/admin/conversations?site_id={site['id']}")[1]["items"][0]

    def test_logout_revokes_admin_api_and_live_connection(self):
        from websockets.exceptions import ConnectionClosed

        _, body = self.request("POST", "/api/admin/login", {"password": "test-only"}, authenticated=False)
        token = body["token"]
        headers = {"Authorization": "Bearer " + token}
        with connect(self.ws_base + "/api/admin/ws") as ws:
            ws.send(json.dumps({"type": "auth", "token": token}))
            self.assertEqual(json.loads(ws.recv(timeout=3))["type"], "ready")
            req = urllib.request.Request(self.base + "/api/admin/logout", headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=5) as response:
                self.assertEqual(response.status, 204)
            with self.assertRaises(ConnectionClosed):
                ws.recv(timeout=3)
        req = urllib.request.Request(self.base + "/api/admin/stats", headers=headers)
        with self.assertRaises(urllib.error.HTTPError) as rejected:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(rejected.exception.code, 401)
        rejected.exception.close()

    def test_visitor_token_and_attachment_stay_with_their_owner(self):
        site, other = self.sites
        ws_url = self.ws_base + "/api/chat/ws"
        image = b"\x89PNG\r\n\x1a\n" + b"image fixture"

        def upload(key, token):
            fields = {"site_key": key, "token": token}
            body = b"".join(
                f'--fixture\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
                for name, value in fields.items()
            )
            body += (b'--fixture\r\nContent-Disposition: form-data; name="file"; filename="photo.png"\r\n'
                     b"Content-Type: image/png\r\n\r\n" + image + b"\r\n--fixture--\r\n")
            req = urllib.request.Request(self.base + "/api/chat/upload", data=body,
                                         headers={"Content-Type": "multipart/form-data; boundary=fixture"})
            try:
                response = urllib.request.urlopen(req, timeout=5)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                return response.status, json.load(response)

        with connect(ws_url) as original, connect(ws_url) as unrelated:
            original.send(json.dumps({"type": "hello", "site_key": site["site_key"]}))
            self.assertEqual(json.loads(original.recv(timeout=3))["type"], "init")
            original.send(json.dumps({"type": "profile", "name": "Visitor", "contact": "contact@example.com"}))
            token = json.loads(original.recv(timeout=3))["token"]
            self.assertEqual(json.loads(original.recv(timeout=3))["type"], "profile_ok")
            unrelated.send(json.dumps({"type": "hello", "site_key": other["site_key"], "token": token}))
            self.assertIsNone(json.loads(unrelated.recv(timeout=3))["profile"])
            self.assertEqual(upload(other["site_key"], token)[0], 403)
            status, staged = upload(site["site_key"], token)
            self.assertEqual(status, 200)
            with self.assertRaises(urllib.error.HTTPError) as hidden:
                urllib.request.urlopen(self.base + staged["url"], timeout=5)
            self.assertEqual(hidden.exception.code, 404)
            hidden.exception.close()
            original.send(json.dumps({"type": "msg", "msg_type": "image", "content": staged["url"]}))
            event = json.loads(original.recv(timeout=3))
            self.assertEqual(event["type"], "msg")
            url = self.base + event["message"]["image_url"]
            with urllib.request.urlopen(url, timeout=5) as response:
                self.assertEqual(response.read(), image)
                self.assertIn("no-store", response.headers["Cache-Control"])
            original.send(json.dumps({"type": "msg", "msg_type": "image", "content": staged["url"]}))
            self.assertEqual(json.loads(original.recv(timeout=3))["type"], "error")
        with connect(ws_url) as restored:
            restored.send(json.dumps({"type": "hello", "site_key": site["site_key"], "token": token}))
            init = json.loads(restored.recv(timeout=3))
            self.assertEqual(init["profile"]["name"], "Visitor")
            conv_id = init["conversation"]["id"]
        self.request("DELETE", f"/api/admin/conversations/{conv_id}")
        with self.assertRaises(urllib.error.HTTPError) as removed:
            urllib.request.urlopen(url, timeout=5)
        self.assertEqual(removed.exception.code, 404)
        removed.exception.close()

    def test_site_changes_revoke_only_affected_visitor_connections(self):
        from websockets.exceptions import ConnectionClosed

        site, other = self.sites
        path = f"/api/admin/sites/{site['id']}"
        allowed, removed = "https://allowed.example", "https://removed.example"

        def configure(origins, enabled=True):
            self.assertEqual(self.request("PUT", path, {
                "name": site["name"], "require_contact": False, "ai_enabled": False,
                "enabled": enabled, "allowed_origins": origins,
            })[0], 200)

        def connect_visitor(sockets, key, origin, token=""):
            ws = sockets.enter_context(connect(self.ws_base + "/api/chat/ws", origin=origin))
            ws.send(json.dumps({"type": "hello", "site_key": key, "token": token}))
            return ws, json.loads(ws.recv(timeout=3))

        def assert_revoked(ws):
            with self.assertRaises(ConnectionClosed) as closed:
                ws.recv(timeout=3)
            self.assertEqual(closed.exception.rcvd.code, 4403)

        def assert_online(ws):
            ws.send('{"type":"ping"}')
            self.assertEqual(json.loads(ws.recv(timeout=3))["type"], "pong")

        message = "hello from the original connection"
        cid, _ = self.seed_search_conversation(site, messages=[(message, datetime.now().isoformat(sep=" "), "text")])
        with sqlite3.connect(Path(self.tmp.name) / "msghub.db") as db:
            token = db.execute(
                "UPDATE visitors SET site_id=? WHERE id=(SELECT visitor_id FROM conversations WHERE id=?) RETURNING token",
                (site["id"], cid),
            ).fetchone()[0]
        configure(f"{allowed},{removed}")
        with ExitStack() as sockets:
            original, init = connect_visitor(sockets, site["site_key"], allowed, token)
            self.assertEqual(init["conversation"]["id"], cid)
            rejected, _ = connect_visitor(sockets, site["site_key"], removed)
            unaffected, _ = connect_visitor(sockets, other["site_key"], removed)

            configure(allowed)
            assert_revoked(rejected)
            assert_online(original)
            assert_online(unaffected)

            idle, init = connect_visitor(sockets, site["site_key"], allowed)
            self.assertIsNone(init["conversation"])
            status, rotated = self.request("POST", path + "/rotate-key")
            self.assertEqual(status, 200)
            assert_revoked(original)
            assert_revoked(idle)
            stale, error = connect_visitor(sockets, site["site_key"], allowed, token)
            self.assertEqual(error["type"], "error")
            assert_revoked(stale)

            restored, init = connect_visitor(sockets, rotated["site_key"], allowed, token)
            self.assertEqual(init["messages"][0]["content"], message)
            self.assertEqual(init["conversation"]["id"], cid)
            status, reply = self.request("POST", f"/api/admin/conversations/{cid}/messages", {"content": "Current reply"})
            self.assertEqual(status, 200)
            self.assertTrue(reply["delivered"])
            self.assertEqual(json.loads(restored.recv(timeout=3))["message"]["content"], "Current reply")
            self.assertEqual(json.loads(restored.recv(timeout=3))["type"], "status")

            configure(allowed, enabled=False)
            assert_revoked(restored)
            disabled, error = connect_visitor(sockets, rotated["site_key"], allowed)
            self.assertEqual(error["type"], "error")
            assert_revoked(disabled)
            assert_online(unaffected)

    def test_submission_redirect_is_scoped_and_export_keeps_cells_as_text(self):
        site_id = self.sites[0]["id"]
        self.request("PUT", f"/api/admin/sites/{site_id}", {
            "name": "Redirect tests", "allowed_origins": "https://shop.example.com",
        })
        data = {"site_key": self.sites[0]["site_key"], "_hp": "fixture", "content": "test"}
        connection = http.client.HTTPConnection(self.base.removeprefix("http://"), timeout=5)
        self.addCleanup(connection.close)
        for target, expected in (("https://shop.example.com/thanks", 303),
                                 ("https://other.example.com/thanks", 200)):
            connection.request("POST", "/api/public/messages", json.dumps({**data, "_redirect": target}),
                               {"Content-Type": "application/json"})
            with connection.getresponse() as response:
                self.assertEqual(response.status, expected)
                self.assertEqual(response.headers.get("Location"), target if expected == 303 else None)
                response.read()
        connection.request("POST", "/api/public/messages", iter([b" " * (128 * 1024)] * 3),
                           {"Content-Type": "application/json"}, encode_chunked=True)
        with connection.getresponse() as response:
            self.assertEqual(response.status, 413)
            response.read()
        note = "备注，中文\n带换行"
        self.seed_inbox_message(site_id, content="=1+1", note=note)
        headers = {"Authorization": "Bearer " + self.token}
        req = urllib.request.Request(self.base + f"/api/admin/messages/export.csv?site_id={site_id}", headers=headers)
        with urllib.request.urlopen(req, timeout=5) as response:
            row = next(csv.DictReader(io.StringIO(response.read().decode("utf-8-sig"))))
        self.assertEqual(row["内容"], "'=1+1")
        self.assertEqual(row["备注"], note)

    def test_database_readiness_is_not_cached(self):
        with urllib.request.urlopen(self.base + "/api/ready", timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(json.load(response), {"ok": True})

    def test_export_keeps_filters_and_order_across_batches(self):
        site_id = self.sites[0]["id"]
        excluded_site = self.sites[1]["id"]
        selected = [
            self.seed_inbox_message(site_id, content=f"批次回归 {i}", status="handled")
            for i in range(205)
        ]
        self.seed_inbox_message(excluded_site, content="批次回归 其他站点", status="handled")
        self.seed_inbox_message(site_id, content="批次回归 未处理")
        self.seed_inbox_message(site_id, content="关键词不匹配", status="handled")

        headers = {"Authorization": "Bearer " + self.token}
        path = self.base + "/api/admin/messages/export.csv?"
        params = {"site_id": site_id, "status": "handled", "q": "批次回归"}
        with urllib.request.urlopen(urllib.request.Request(path + urlencode(params), headers=headers), timeout=5) as response:
            self.assertEqual(response.headers["Content-Disposition"], "attachment; filename=messages.csv")
            body = response.read()
            self.assertTrue(body.startswith(b"\xef\xbb\xbf"))
        exported = csv.DictReader(io.StringIO(body.decode("utf-8-sig"), newline=""))
        self.assertEqual([int(row["ID"]) for row in exported], list(reversed(selected)))

        params["q"] = "没有匹配的留言"
        with urllib.request.urlopen(urllib.request.Request(path + urlencode(params), headers=headers), timeout=5) as response:
            empty = csv.DictReader(io.StringIO(response.read().decode("utf-8-sig"), newline=""))
        self.assertEqual(list(empty), [])
        self.assertEqual(empty.fieldnames, exported.fieldnames)

    def test_tag_catalog_validation_and_restart(self):
        catalog = self.request("GET", "/api/admin/tags")[1]
        self.assertTrue({"待报价", "售后", "投诉", "重点客户", "已转化"}.issubset(
            {t["name"] for t in catalog}))
        tag = self.create_tag(" TrimTag ")
        self.assertEqual(tag["name"], "TrimTag")
        self.assertEqual(self.request("POST", "/api/admin/tags", {"name": "trimtag"})[0], 409)
        other = self.create_tag("OtherTag")
        self.assertEqual(self.request("PUT", f"/api/admin/tags/{other['id']}",
                                      {"name": "TRIMTAG"})[0], 409)
        for method, path in (("POST", "/api/admin/tags"), ("PUT", f"/api/admin/tags/{tag['id']}")):
            for name in (" ", "x" * 31):
                self.assertEqual(self.request(method, path, {"name": name})[0], 400)
        for method in ("PUT", "DELETE"):
            self.assertEqual(self.request(method, "/api/admin/tags/999999", {"name": "missing"})[0], 404)
        # Removing a default is permanent; restarting must not recreate it.
        default = next(t for t in catalog if t["name"] == "待报价")
        self.request("DELETE", f"/api/admin/tags/{default['id']}")
        try:
            subprocess.run([sys.executable, "-c", "from app.db import init_db; init_db(); init_db()"],
                           cwd=Path(__file__).resolve().parents[1], env=self.env, check=True)
            self.assertNotIn("待报价", {t["name"] for t in self.request("GET", "/api/admin/tags")[1]})
        finally:
            self.request("POST", "/api/admin/tags", {"name": "待报价"})

    def seed_search_conversation(self, site, name="Search visitor", contact="test@example.com", messages=None):
        with sqlite3.connect(Path(self.tmp.name) / "msghub.db") as db:
            visitor = db.execute("INSERT INTO visitors (token,name,contact) VALUES (?,?,?)",
                                 (os.urandom(16).hex(), name, contact)).lastrowid
            cid = db.execute("INSERT INTO conversations (site_id,visitor_id,last_preview,unread) VALUES (?,?,?,7)",
                             (site["id"], visitor, "Latest without keyword")).lastrowid
            ids = []
            for text, timestamp, kind in messages or [("old uniqueNeedle", "2026-01-01 00:00:00", "text"),
                                                       ("Latest without keyword", "2026-02-01 12:00:00", "text")]:
                ids.append(db.execute("INSERT INTO chat_messages (conversation_id,sender,content,created_at,msg_type) VALUES (?,'visitor',?,?,?)",
                                      (cid, text, timestamp, kind)).lastrowid)
            return cid, ids

    def test_full_history_search_dates_and_literal_text(self):
        a, ids = self.seed_search_conversation(self.sites[0])
        b, _ = self.seed_search_conversation(self.sites[1], messages=[("other", "2026-01-01 23:59:59", "text")])
        tag = self.create_tag("搜索组合")
        self.request("PUT", f"/api/admin/conversations/{a}/tags/{tag['id']}")
        def search(**params):
            status, result = self.request("GET", "/api/admin/conversations?" + urlencode(params))
            self.assertEqual(status, 200)
            return result
        result = search(q="UNIQUENEEDLE", date_from="2026-01-01", date_to="2026-01-01",
                        site_id=self.sites[0]["id"], tag_id=tag["id"], status="open")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["search_match"]["id"], ids[0])
        self.assertIn("uniqueNeedle", result["items"][0]["search_match"]["snippet"])
        for query in ("#"+str(a), "Search visitor", "test@example.com"):
            self.assertIn(a, [c["id"] for c in search(q=query)["items"]])
        self.assertEqual(search(q="uniqueNeedle", date_from="2026-02-01")["total"], 0)
        self.assertEqual(search(date_to="2026-01-01")["total"], 2)
        self.assertEqual(search(date_from="2026-01-02", date_to="2026-01-31")["total"], 0)
        self.assertEqual(search(site_id=self.sites[1]["id"], q="uniqueNeedle")["total"], 0)
        for q in ("%", "_", "\\", "#", "9" * 200):
            self.assertEqual(search(q=q)["total"], 0)
        literal, _ = self.seed_search_conversation(self.sites[0], messages=[("100%_\\ literal", "2026-01-01 12:00:00", "text")])
        self.assertEqual(search(q="%_\\")["items"][0]["id"], literal)
        image, _ = self.seed_search_conversation(self.sites[0], messages=[("/uploads/uniquepicture.png", "2026-01-01 12:00:00", "image")])
        self.assertEqual(search(q="uniquepicture")["total"], 0)
        self.request("POST", f"/api/admin/conversations/{a}/notes", {"content": "privatenoteneedle"})
        self.assertEqual(search(q="privatenoteneedle")["total"], 0)
        pages = [search(size=1, page=i) for i in range(1, 6)]
        self.assertEqual([p["total"] for p in pages], [4] * 5)
        self.assertEqual(len({p["items"][0]["id"] for p in pages[:4]}), 4)
        self.assertEqual(pages[-1]["items"], [])
        for params, code in (({"date_from":"bad"}, 422), ({"date_from":"2026-02-01","date_to":"2026-01-01"}, 400), ({"q":"x"*201}, 422)):
            self.assertEqual(self.request("GET", "/api/admin/conversations?" + urlencode(params))[0], code)
        with sqlite3.connect(Path(self.tmp.name) / "msghub.db") as db:
            self.assertEqual(db.execute("SELECT unread,status FROM conversations WHERE id=?", (a,)).fetchone(), (7,"waiting"))

    def test_message_context_and_cursor_pagination(self):
        cid, ids = self.seed_search_conversation(self.sites[0], messages=[
            ("Context " + str(i), "2026-01-01 12:00:00", "text") for i in range(620)])
        other, other_ids = self.seed_search_conversation(self.sites[1])
        path = f"/api/admin/conversations/{cid}"
        def window(**params):
            status, result = self.request("GET", path + "/messages?" + urlencode(params))
            self.assertEqual(status, 200)
            return result
        r = window(around_id=ids[110], limit=20)
        self.assertEqual([m["id"] for m in r["items"]], ids[100:120])
        self.assertTrue(r["has_older"] and r["has_newer"])
        first = window(after_id=ids[0], limit=100)
        second = window(after_id=first["items"][-1]["id"], limit=100)
        self.assertEqual([m["id"] for m in first["items"] + second["items"]], ids[1:201])
        self.assertEqual(window(before_id=ids[0])["items"], [])
        self.assertFalse(window(after_id=ids[-1])["has_newer"])
        with sqlite3.connect(Path(self.tmp.name) / "msghub.db") as db:
            self.assertEqual(db.execute("SELECT unread FROM conversations WHERE id=?", (cid,)).fetchone()[0], 7)
        detail = self.request("GET", path + "?message_id=" + str(ids[10]))[1]
        self.assertIn(ids[10], [m["id"] for m in detail["messages"]])
        self.assertTrue(detail["has_newer"])
        self.assertEqual(detail["conversation"]["unread"], 7)
        latest = self.request("GET", path)[1]
        self.assertEqual([m["id"] for m in latest["messages"]], ids[-500:])
        self.assertTrue(latest["has_older"])
        self.assertFalse(latest["has_newer"])
        for route, code in ((path+"/messages?around_id="+str(other_ids[0]),404),
                            (path+"?message_id="+str(other_ids[0]),404),
                            (path+"/messages?before_id=0",422),
                            (path+"/messages?limit=501",422),
                            (path+"/messages?around_id=1&after_id=2",400),
                            ("/api/admin/conversations/999999/messages",404)):
            self.assertEqual(self.request("GET", route)[0], code)
        self.assertEqual(self.request("GET", path+"/messages", authenticated=False)[0],401)

    def test_read_cursor_keeps_later_messages_unread(self):
        site = self.sites[0]
        now = datetime.now().isoformat(sep=" ", timespec="seconds")
        cid, ids = self.seed_search_conversation(site, messages=[(f"Visitor message {i}", now, "text") for i in range(3)])
        _, other_ids = self.seed_search_conversation(self.sites[1])
        path = f"/api/admin/conversations/{cid}"
        with sqlite3.connect(Path(self.tmp.name) / "msghub.db") as db:
            db.execute("UPDATE conversations SET unread=3 WHERE id=?", (cid,))
            visitor = db.execute("SELECT visitor_id FROM conversations WHERE id=?", (cid,)).fetchone()[0]
            db.execute("UPDATE visitors SET site_id=? WHERE id=?", (site["id"], visitor))
            token = db.execute("SELECT token FROM visitors WHERE id=?", (visitor,)).fetchone()[0]

        def state():
            conv = self.request("GET", path)[1]["conversation"]
            return conv["read_upto"], conv["unread"]

        self.assertEqual(state(), (0, 3))
        self.assertEqual(self.request("POST", path + "/read", {"upto_id": other_ids[0]})[0], 404)
        self.assertEqual(self.request("POST", path + "/read")[0], 422)
        with connect(self.ws_base + "/api/chat/ws") as ws:
            ws.send(json.dumps({"type": "hello", "site_key": site["site_key"], "token": token}))
            self.assertEqual(json.loads(ws.recv(timeout=3))["type"], "init")
            ws.send('{"type":"msg","content":"Arrived after the displayed page"}')
            later = json.loads(ws.recv(timeout=3))["message"]["id"]
            self.assertEqual(state(), (0, 4))
            for upto in (ids[-1], ids[0], ids[-1]):
                self.assertEqual(self.request("POST", path + "/read", {"upto_id": upto})[0], 200)
                self.assertEqual(state(), (ids[-1], 1))
            reply = self.request("POST", path + "/messages", {"content": "Answered"})[1]["message"]
            self.assertEqual(state(), (reply["id"], 0))
            self.assertEqual(self.request("POST", path + "/read", {"upto_id": later})[0], 200)
            self.assertEqual(state(), (reply["id"], 0))

    def test_read_cursor_migrates_legacy_counts_and_can_be_run_again(self):
        with tempfile.TemporaryDirectory() as folder:
            env = {**self.env, "DATA_DIR": folder}
            migrate = [sys.executable, "-m", "app.db"]
            subprocess.run(migrate, env=env, check=True)
            with sqlite3.connect(Path(folder) / "msghub.db") as db:
                db.execute("UPDATE settings SET value='1' WHERE key='schema_version'")
                db.execute("ALTER TABLE conversations DROP COLUMN read_upto")
                site = db.execute("INSERT INTO sites (name,site_key) VALUES ('Migration','migration')").lastrowid
                expected = []
                for unread in (0, 2, 5):
                    visitor = db.execute("INSERT INTO visitors (token,site_id) VALUES (?,?)", (str(unread), site)).lastrowid
                    cid = db.execute("INSERT INTO conversations (site_id,visitor_id,unread) VALUES (?,?,?)",
                                     (site, visitor, unread)).lastrowid
                    visitors = []
                    for sender in ("visitor", "agent", "visitor", "ai", "visitor"):
                        mid = db.execute("INSERT INTO chat_messages (conversation_id,sender,content) VALUES (?,?,'old')",
                                         (cid, sender)).lastrowid
                        if sender == "visitor":
                            visitors.append(mid)
                    already_read = visitors[:max(0, len(visitors) - unread)]
                    expected.append((min(unread, len(visitors)), already_read[-1] if already_read else 0))
            for _ in range(2):
                subprocess.run(migrate, env=env, check=True)
                with sqlite3.connect(Path(folder) / "msghub.db") as db:
                    self.assertEqual(db.execute("SELECT unread,read_upto FROM conversations ORDER BY id").fetchall(), expected)
                    self.assertEqual(db.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone(), ("2",))

    def seed_inbox_message(self, site_id, conv_id=None, content="Inbox entry", status="new", note="Private inbox note"):
        with sqlite3.connect(Path(self.tmp.name) / "msghub.db") as db:
            return db.execute("INSERT INTO messages (site_id,conversation_id,content,status,note) VALUES (?,?,?,?,?)",
                              (site_id, conv_id, content, status, note)).lastrowid

    def test_inbox_conversation_links_lifecycle_and_scope(self):
        cid, _ = self.seed_search_conversation(self.sites[0])
        other, _ = self.seed_search_conversation(self.sites[1])
        mids = [self.seed_inbox_message(self.sites[0]["id"], cid, content=f"Linked {i}") for i in range(12)]
        self.seed_inbox_message(self.sites[1]["id"], cid, content="Wrong-site legacy reference")
        independent = self.seed_inbox_message(self.sites[0]["id"])
        path = f"/api/admin/conversations/{cid}/inbox-messages"
        status, first = self.request("GET", path)
        self.assertEqual(status,200)
        self.assertEqual((first["total"],len(first["items"])),(12,10))
        second = self.request("GET",path+"?page=2")[1]
        self.assertEqual([m["id"] for m in first["items"]+second["items"]],list(reversed(mids)))
        detail = self.request("GET",f"/api/admin/messages/{mids[0]}")[1]
        self.assertEqual((detail["linked_conversation_id"],detail["conversation_status"],detail["status"]),(cid,"waiting","new"))
        self.assertEqual(self.request("GET",f"/api/admin/messages/{independent}")[1]["linked_conversation_id"],None)
        wrong = self.request("GET",f"/api/admin/messages?site_id={self.sites[1]['id']}")[1]["items"][0]
        self.assertIsNone(wrong["linked_conversation_id"])
        self.assertEqual(self.request("GET",f"/api/admin/conversations/{other}/inbox-messages")[1]["total"],0)
        with sqlite3.connect(Path(self.tmp.name) / "msghub.db") as db:
            self.assertEqual(db.execute("SELECT unread FROM conversations WHERE id=?",(cid,)).fetchone()[0],7)
        self.request("PATCH",f"/api/admin/messages/{mids[0]}",{"status":"handled","note":"Handled privately"})
        self.assertEqual(self.request("POST",f"/api/admin/messages/{mids[0]}/read")[1]["status"],"handled")
        self.assertEqual(self.request("POST",f"/api/admin/messages/{mids[1]}/read")[1]["status"],"read")
        self.assertEqual(self.request("GET",path+"?size=100")[1]["items"][-1]["status"],"handled")
        with sqlite3.connect(Path(self.tmp.name) / "msghub.db") as db:
            self.assertEqual(db.execute("SELECT status,unread FROM conversations WHERE id=?",(cid,)).fetchone(),("waiting",7))
        self.request("DELETE",f"/api/admin/conversations/{cid}")
        orphan = self.request("GET",f"/api/admin/messages/{mids[0]}")[1]
        self.assertEqual(orphan["conversation_id"],cid)
        self.assertIsNone(orphan["linked_conversation_id"])
        self.assertEqual(orphan["note"],"Handled privately")
        self.assertEqual(self.request("GET",path)[0],404)
        self.assertEqual(self.request("POST","/api/admin/messages/999999/read")[0],404)
        self.assertEqual(self.request("POST",f"/api/admin/messages/{mids[0]}/read",authenticated=False)[0],401)
        for route, code in ((f"/api/admin/messages/{mids[0]}",401),
                            (f"/api/admin/conversations/{other}/inbox-messages",401)):
            self.assertEqual(self.request("GET",route,authenticated=False)[0],code)
        for route, code in (("/api/admin/messages/999999",404),(path+"?size=0",422),
                            (path+"?page=0",422),(path+"?size=101",422)):
            self.assertEqual(self.request("GET",route)[0],code)
        self.request("DELETE",f"/api/admin/sites/{self.sites[0]['id']}")
        self.assertEqual(self.request("GET",f"/api/admin/messages/{mids[0]}")[0],404)

    def test_auto_archive_links_events_and_public_privacy(self):
        site = self.sites[0]
        self.assertEqual(self.request("PUT",f"/api/admin/sites/{site['id']}",{
            "name":site["name"],"require_contact":False,"ai_enabled":False,
            "work_hours":{"days":[1,2,3,4,5,6,7],"start":"00:00","end":"00:00"}})[0],200)
        with connect(self.ws_base+"/api/admin/ws") as admin_ws, connect(self.ws_base+"/api/chat/ws") as visitor:
            admin_ws.send(json.dumps({"type":"auth","token":self.token}))
            self.assertEqual(json.loads(admin_ws.recv())["type"],"ready")
            visitor.send(json.dumps({"type":"hello","site_key":site["site_key"]})); visitor.recv()
            questions = ["甲" * 5000, "乙" * 5000, "丙" * 5000]
            visitor.send(json.dumps({"type":"msg","content":questions[0]}))
            visitor_token = None
            while True:
                event=json.loads(visitor.recv(timeout=5))
                if event["type"]=="token": visitor_token=event["token"]
                if event["type"]=="msg" and event["message"]["sender"]=="system": break
            conv = self.request("GET",f"/api/admin/conversations?site_id={site['id']}")[1]["items"][0]
            def wait_inbox():
                for _ in range(30):
                    event=json.loads(admin_ws.recv(timeout=5))
                    if event["type"]=="inbox_changed": return event
                self.fail("No admin inbox notification")
            self.assertEqual(wait_inbox()["conversation_ids"],[conv["id"]])
            path=f"/api/admin/conversations/{conv['id']}/inbox-messages"
            first=self.request("GET",path)[1]
            self.assertEqual(first["total"],1)
            self.assertIn(questions[0],first["items"][0]["content"])
            self.assertEqual(first["items"][0]["linked_conversation_id"],conv["id"])
            for question in questions[1:]:
                visitor.send(json.dumps({"type":"msg","content":question}))
                while json.loads(visitor.recv(timeout=5)).get("type")!="msg": pass
                wait_inbox()
            merged=self.request("GET",path)[1]
            self.assertEqual(merged["total"],1)
            mid=merged["items"][0]["id"]
            detail = self.request("GET", f"/api/admin/messages/{mid}")[1]
            self.assertEqual(detail["content"], merged["items"][0]["content"])
            for question in questions:
                self.assertIn(question, detail["content"])
            export = urllib.request.Request(
                f"{self.base}/api/admin/messages/export.csv?site_id={site['id']}",
                headers={"Authorization": "Bearer " + self.token},
            )
            with urllib.request.urlopen(export, timeout=5) as response:
                exported = list(csv.DictReader(io.StringIO(response.read().decode("utf-8-sig"))))
            self.assertEqual([row["内容"] for row in exported], [detail["content"]])
            self.request("PATCH",f"/api/admin/messages/{mid}",{"note":"Secret inbox handling","status":"handled"})
            self.assertEqual(wait_inbox()["conversation_ids"],[conv["id"]])
            with self.assertRaises(TimeoutError): visitor.recv(timeout=.2)
            self.request("POST","/api/admin/messages/batch",{"ids":[mid],"action":"new"})
            self.assertEqual(wait_inbox()["conversation_ids"],[conv["id"]])
            # Public forms cannot attach themselves to an arbitrary conversation.
            status, direct=self.request("POST","/api/public/messages",{"site_key":site["site_key"],"content":"Direct form", "conversation_id":conv["id"]},authenticated=False)
            self.assertEqual(status,200)
            self.assertEqual(wait_inbox()["conversation_ids"],[])
            self.assertIsNone(self.request("GET",f"/api/admin/messages/{direct['id']}")[1]["conversation_id"])
            self.request("DELETE",f"/api/admin/messages/{mid}")
            self.assertEqual(wait_inbox()["conversation_ids"],[conv["id"]])
            self.assertEqual(self.request("GET",path)[1]["total"],0)
        with connect(self.ws_base+"/api/chat/ws") as visitor:
            visitor.send(json.dumps({"type":"hello","site_key":site["site_key"],"token":visitor_token}))
            init=json.loads(visitor.recv())
            self.assertNotIn("Secret inbox handling",json.dumps(init))
            self.assertNotIn("inbox",init)

    def test_tags_filter_batch_atomicity_and_cascade(self):
        a = self.create_conversation(self.sites[0], "Need quote")
        b = self.create_conversation(self.sites[0], "Need help")
        c = self.create_conversation(self.sites[1], "Need quote")
        tag, other = self.create_tag("报价过滤"), self.create_tag("保留标签")
        path = f"/api/admin/conversations/{a['id']}"
        batch = "/api/admin/conversation-tags/batch"
        def change(ids, action="add", tag_id=tag["id"]):
            return self.request("POST", batch, {"conversation_ids": ids, "tag_id": tag_id, "action": action})
        for _ in range(2):
            status, result = self.request("PUT", path + f"/tags/{other['id']}")
            self.assertEqual(status, 200)
            self.assertEqual(len(result["conversation"]["tags"]), 1)
        status, result = change([a["id"], b["id"], c["id"], a["id"]])
        self.assertEqual((status, result["affected"]), (200, 3))
        self.assertEqual(next(t for t in self.request("GET", "/api/admin/tags")[1]
                              if t["id"] == tag["id"])["conversation_count"], 3)
        query = f"/api/admin/conversations?tag_id={tag['id']}&size=1"
        pages = [self.request("GET", query + f"&page={i}")[1] for i in (1, 2, 3, 4)]
        self.assertEqual([p["total"] for p in pages], [3] * 4)
        self.assertEqual(len({p["items"][0]["id"] for p in pages[:3]}), 3)
        self.assertEqual(pages[3]["items"], [])
        filtered = self.request("GET", query + f"&site_id={self.sites[0]['id']}&q=quote&status=open")[1]
        self.assertEqual((filtered["total"], filtered["items"][0]["id"]), (1, a["id"]))
        self.request("POST", path + "/status", {"status": "closed"})
        self.assertEqual(self.request("GET", query + "&status=closed")[1]["total"], 1)
        self.assertEqual(self.request("GET", "/api/admin/conversations?tag_id=0")[0], 422)
        # Invalid batches cannot remove the valid conversation's labels.
        self.assertEqual(change([a["id"], 999999], "remove")[0], 404)
        self.assertEqual(change([a["id"]], tag_id=999999)[0], 404)
        self.assertEqual(len(self.request("GET", path + "/tags")[1]["tags"]), 2)
        for ids, action in (([], "add"), ([a["id"]] * 101, "add"), ([a["id"]], "replace")):
            self.assertEqual(change(ids, action)[0], 422)
        self.assertEqual(change([a["id"], b["id"]], "remove")[0], 200)
        self.assertEqual(self.request("GET", path + "/tags")[1]["tags"],
                         [{"id": other["id"], "name": other["name"]}])
        self.assertEqual(self.request("DELETE", path + f"/tags/{tag['id']}")[0], 200)
        self.request("PUT", f"/api/admin/tags/{other['id']}", {"name": "重命名"})
        before = self.request("GET", path)[1]
        self.assertEqual(before["conversation"]["tags"][0]["name"], "重命名")
        listing = self.request("GET", f"/api/admin/conversations?q=%23{a['id']}")[1]
        self.assertEqual(listing["items"][0]["tags"], before["conversation"]["tags"])
        self.request("DELETE", f"/api/admin/tags/{other['id']}")
        after = self.request("GET", path)[1]
        self.assertEqual(after["conversation"]["tags"], [])
        self.assertEqual(before["messages"], after["messages"])
        self.request("DELETE", f"/api/admin/sites/{self.sites[1]['id']}")
        self.assertEqual(next(t for t in self.request("GET", "/api/admin/tags")[1]
                              if t["id"] == tag["id"])["conversation_count"], 0)

    def test_tags_admin_realtime_and_visitor_privacy(self):
        with connect(self.ws_base + "/api/chat/ws") as visitor:
            visitor.send(json.dumps({"type": "hello", "site_key": self.sites[0]["site_key"]}))
            visitor.recv()
            visitor.send(json.dumps({"type": "msg", "content": "Private tag test"}))
            visitor_token = None
            while True:
                event = json.loads(visitor.recv())
                if event["type"] == "token":
                    visitor_token = event["token"]
                if event["type"] == "msg":
                    break
            conv = self.request("GET", f"/api/admin/conversations?site_id={self.sites[0]['id']}")[1]["items"][0]
            path = f"/api/admin/conversations/{conv['id']}"
            with connect(self.ws_base + "/api/admin/ws") as admin_ws:
                admin_ws.send(json.dumps({"type": "auth", "token": self.token}))
                self.assertEqual(json.loads(admin_ws.recv())["type"], "ready")
                tag = self.create_tag("客服私有信息")
                self.assertEqual(json.loads(admin_ws.recv(timeout=2))["type"], "tags_changed")
                self.request("PUT", path + f"/tags/{tag['id']}")
                updated = json.loads(admin_ws.recv(timeout=2))
                self.assertEqual(updated["type"], "conv")
                self.assertEqual(updated["conversation"]["tags"], [{"id": tag["id"], "name": tag["name"]}])
                for key in ("unread", "status", "last_msg_at", "last_preview"):
                    self.assertEqual(updated["conversation"][key], conv[key])
                self.request("PUT", f"/api/admin/tags/{tag['id']}", {"name": "仅客服"})
                self.assertEqual(json.loads(admin_ws.recv(timeout=2))["type"], "tags_changed")
                self.request("DELETE", f"/api/admin/tags/{tag['id']}")
                self.assertEqual(json.loads(admin_ws.recv(timeout=2))["type"], "tags_changed")
                self.request("PUT", path + f"/tags/{self.create_tag('重连隐私')['id']}")
            with self.assertRaises(TimeoutError):
                visitor.recv(timeout=.2)
        self.assertIsNotNone(visitor_token)
        with connect(self.ws_base + "/api/chat/ws") as visitor:
            visitor.send(json.dumps({"type": "hello", "site_key": self.sites[0]["site_key"], "token": visitor_token}))
            init = json.loads(visitor.recv())
            self.assertEqual(set(init["conversation"]), {"id", "status"})
            self.assertNotIn("重连隐私", json.dumps(init, ensure_ascii=False))
        for method, route, body in (
            ("GET", "/api/admin/tags", None),
            ("POST", "/api/admin/tags", {"name": "x"}),
            ("PUT", "/api/admin/tags/1", {"name": "x"}),
            ("DELETE", "/api/admin/tags/1", None),
            ("GET", path + "/tags", None),
            ("PUT", path + "/tags/1", None),
            ("DELETE", path + "/tags/1", None),
            ("POST", "/api/admin/conversation-tags/batch", {"conversation_ids": [conv["id"]], "tag_id": 1, "action": "add"}),
        ):
            self.assertEqual(self.request(method, route, body, authenticated=False)[0], 401)

    def test_reply_crud_scope_and_validation(self):
        global_reply = self.create_reply(" Global ")
        a = self.create_reply("A", self.sites[0]["id"])
        b = self.create_reply("B", self.sites[1]["id"])
        status, applicable = self.request(
            "GET", f"/api/admin/quick-replies?site_id={self.sites[0]['id']}")
        self.assertEqual(status, 200)
        self.assertEqual({r["id"] for r in applicable}, {global_reply["id"], a["id"]})
        self.assertEqual(global_reply["title"], "Global")
        status, _ = self.request("PUT", f"/api/admin/quick-replies/{a['id']}",
                                 {"title": "Updated", "content": " New content ", "site_id": None})
        self.assertEqual(status, 200)
        updated = next(r for r in self.request("GET", "/api/admin/quick-replies")[1] if r["id"] == a["id"])
        self.assertEqual((updated["content"], updated["site_id"]), ("New content", None))
        for site_id in (0, -1, 999999):
            status, _ = self.request("POST", "/api/admin/quick-replies",
                                     {"title": "Bad", "content": "Bad", "site_id": site_id})
            self.assertIn(status, (404, 422))
        for title, content in ((" ", "ok"), ("ok", " "), ("x" * 81, "ok"), ("ok", "x" * 2001)):
            self.assertEqual(self.request("POST", "/api/admin/quick-replies",
                                          {"title": title, "content": content})[0], 400)
        self.assertEqual(self.request("GET", "/api/admin/quick-replies?site_id=0")[0], 422)
        self.assertEqual(self.request("DELETE", f"/api/admin/quick-replies/{b['id']}")[0], 200)
        self.assertEqual(self.request("DELETE", f"/api/admin/quick-replies/{b['id']}")[0], 404)

    def test_notes_are_private_and_persist_on_reconnect(self):
        visitor_token = None
        with connect(self.ws_base + "/api/chat/ws") as ws:
            ws.send(json.dumps({"type": "hello", "site_key": self.sites[0]["site_key"]}))
            self.assertEqual(json.loads(ws.recv())["type"], "init")
            ws.send(json.dumps({"type": "msg", "content": "Visitor message"}))
            while True:
                event = json.loads(ws.recv())
                if event["type"] == "token":
                    visitor_token = event["token"]
                if event["type"] == "msg":
                    break
            conv = self.request("GET", "/api/admin/conversations")[1]["items"][0]
            path = f"/api/admin/conversations/{conv['id']}"
            before = self.request("GET", path)[1]
            for text in ("Private <script>note</script>", "Follow-up"):
                status, result = self.request("POST", path + "/notes", {"content": text})
                self.assertEqual(status, 200)
                self.assertEqual(result["note"]["content"], text)
            after = self.request("GET", path)[1]
            self.assertEqual([n["content"] for n in after["notes"]],
                             ["Follow-up", "Private <script>note</script>"])
            self.assertEqual(after["messages"], before["messages"])
            for key in ("last_msg_at", "last_preview", "status", "unread"):
                self.assertEqual(after["conversation"][key], before["conversation"][key])
            with self.assertRaises(TimeoutError):
                ws.recv(timeout=.2)  # Adding a note must not send a visitor event.
            for content in (" ", "x" * 2001):
                self.assertEqual(self.request("POST", path + "/notes", {"content": content})[0], 400)
            self.assertEqual(self.request("POST", path + "/notes", {"content": "bad"},
                                          authenticated=False)[0], 401)
        self.assertIsNotNone(visitor_token)
        with connect(self.ws_base + "/api/chat/ws") as ws:
            ws.send(json.dumps({"type": "hello", "site_key": self.sites[0]["site_key"],
                                "token": visitor_token}))
            init = json.loads(ws.recv())
            self.assertNotIn("notes", init)
            self.assertNotIn("Private", json.dumps(init))
        self.assertEqual(len(self.request("GET", path)[1]["notes"]), 2)
        self.assertEqual(self.request("POST", "/api/admin/conversations/999999/notes",
                                      {"content": "missing"})[0], 404)

    def test_qq_delivery_api_validation_privacy_and_confirmation(self):
        path = '/api/admin/qq/deliveries'
        for method, url, body in [('GET', path, None), ('GET', path+'/1', None),
                                  ('POST', path+'/1/retry', {}), ('POST', path+'/1/confirm', {})]:
            self.assertEqual(self.request(method, url, body, authenticated=False)[0], 401)
        for query, expected in [('?status=invalid', 400), ('?page=0', 422), ('?size=101', 422),
                                ('?conversation_id=0', 422)]:
            self.assertEqual(self.request('GET', path+query)[0], expected)
        for method, suffix, body in [('GET', '', None), ('POST', '/retry', {}), ('POST', '/confirm', {})]:
            self.assertEqual(self.request(method, path+'/999999'+suffix, body)[0], 404)
        cid, mids = self.seed_search_conversation(self.sites[0])
        with sqlite3.connect(Path(self.tmp.name) / 'msghub.db') as db:
            jid = db.execute("INSERT INTO qq_deliveries (conversation_id,recipient,status) VALUES (?,'private-openid','uncertain')", (cid,)).lastrowid
            db.execute('INSERT INTO qq_delivery_messages (delivery_id,message_id) VALUES (?,?)', (jid,mids[0]))
            db.execute("UPDATE chat_messages SET qq_state='pending' WHERE id=?", (mids[0],))
            db.execute("INSERT INTO qq_delivery_parts (delivery_id,sequence,kind,content,status) VALUES (?,0,'text','snapshot','uncertain')", (jid,))
        listing = self.request('GET', path+f'?conversation_id={cid}&status=uncertain')[1]
        self.assertEqual((listing['total'], listing['items'][0]['id']), (1,jid))
        detail = self.request('GET', path+f'/{jid}')[1]
        self.assertNotIn('private-openid', json.dumps([listing,detail]))
        self.assertNotIn('recipient', detail['delivery'])
        self.assertEqual(detail['parts'][0]['content'], 'snapshot')
        self.assertEqual(self.request('POST', path+f'/{jid}/retry', {})[0], 409)  # QQ disabled
        self.assertEqual(self.request('POST', path+f'/{jid}/retry', {'confirm_uncertain': []})[0], 422)
        self.assertEqual(self.request('POST', path+f'/{jid}/confirm', {})[0], 200)
        detail = self.request('GET', path+f'/{jid}')[1]
        self.assertEqual(detail['delivery']['status'], 'sent')
        self.assertEqual(detail['parts'][0]['manually_confirmed'], 1)
        self.assertEqual(detail['parts'][0]['qq_message_id'], '')
        self.assertEqual(self.request('POST', path+f'/{jid}/confirm', {})[0], 409)
        self.assertEqual(self.request('POST', path+f'/{jid}/retry', {})[1]['status'], 'sent')
        self.assertEqual(self.request('GET',path+'?kind=invalid')[0],400)
        with sqlite3.connect(Path(self.tmp.name) / 'msghub.db') as db:
            inbox=db.execute('INSERT INTO messages (site_id,conversation_id,content) VALUES (?,?,?)',(self.sites[0]['id'],cid,'handoff inbox')).lastrowid
            hid=db.execute("INSERT INTO qq_deliveries (conversation_id,recipient,kind,event_key,reason,summary,inbox_message_id,status) VALUES (?,'private-openid','handoff','private-event','客服忙碌','访客原话',?,'uncertain')",(cid,inbox)).lastrowid
            db.execute('INSERT INTO qq_notification_sources (delivery_id,message_id) VALUES (?,?)',(hid,mids[0]))
            db.execute("INSERT INTO qq_delivery_parts (delivery_id,sequence,kind,content,status) VALUES (?,0,'text','handoff snapshot','uncertain')",(hid,))
        result=self.request('GET',path+f'?kind=handoff&conversation_id={cid}&status=uncertain')[1]
        self.assertEqual((result['total'],result['items'][0]['id']),(1,hid))
        detail=self.request('GET',path+f'/{hid}')[1]
        self.assertEqual(detail['delivery']['message_count'],1)
        self.assertEqual(detail['delivery']['reason'],'客服忙碌')
        self.assertEqual(detail['delivery']['inbox_message_id'],inbox)
        self.assertEqual([m['id'] for m in detail['messages']],[mids[0]])
        self.assertNotIn('private-event',json.dumps(detail))
        self.assertNotIn('private-openid',json.dumps(detail))
        self.request('DELETE',f'/api/admin/messages/{inbox}')
        self.assertIsNone(self.request('GET',path+f'/{hid}')[1]['delivery']['inbox_message_id'])
        self.assertEqual(self.request('POST',path+f'/{hid}/confirm',{})[0],200)
        self.request('DELETE', f'/api/admin/conversations/{cid}')
        self.assertEqual(self.request('GET', path+f'/{jid}')[0], 404)
        self.assertEqual(self.request('GET', path+f'/{hid}')[0], 404)

    def test_admin_auth_and_site_cascade(self):
        for method, path, body in (
            ("GET", "/api/admin/quick-replies", None),
            ("POST", "/api/admin/quick-replies", {"title": "x", "content": "x"}),
            ("PUT", "/api/admin/quick-replies/1", {"title": "x", "content": "x"}),
            ("DELETE", "/api/admin/quick-replies/1", None),
        ):
            self.assertEqual(self.request(method, path, body, authenticated=False)[0], 401)
        scoped = self.create_reply("Scoped", self.sites[0]["id"])
        global_reply = self.create_reply("Global")
        self.request("DELETE", f"/api/admin/sites/{self.sites[0]['id']}")
        ids = {r["id"] for r in self.request("GET", "/api/admin/quick-replies")[1]}
        self.assertNotIn(scoped["id"], ids)
        self.assertIn(global_reply["id"], ids)

    def test_ai_decisions_auth_scope_privacy_pagination_and_legacy(self):
        cid,mids=self.seed_search_conversation(self.sites[0])
        other,_=self.seed_search_conversation(self.sites[1])
        path=f'/api/admin/conversations/{cid}/ai-decisions'
        with sqlite3.connect(Path(self.tmp.name)/'msghub.db') as db:
            ai_mid=db.execute("INSERT INTO chat_messages (conversation_id,sender,content) VALUES (?,'ai','reply')",(cid,)).lastrowid
            legacy=db.execute("INSERT INTO chat_messages (conversation_id,sender,content) VALUES (?,'ai','old reply')",(cid,)).lastrowid
            aid=db.execute("INSERT INTO ai_decisions (conversation_id,source_upto,message_id,outcome,trigger,reason_code,reason,reason_origin,question,answer,context,sources,source_count) VALUES (?, ?, ?, 'answered','timeout','answered','AI 已回答','模型回答','question','reply','snapshot','[]',0)",(cid,mids[0],ai_mid)).lastrowid
            db.execute("INSERT INTO ai_decisions (conversation_id,source_upto,outcome,trigger,reason_code,reason,reason_origin,question) VALUES (?,?,'handoff','timeout','model_handoff','模型转人工','模型分类','another')",(cid,mids[1]))
        for url in [path,path+f'/{aid}']:
            self.assertEqual(self.request('GET',url,authenticated=False)[0],401)
        self.assertEqual(self.request('GET',path+'?size=1')[1]['total'],2)
        self.assertEqual(len(self.request('GET',path+'?size=1&page=2')[1]['items']),1)
        self.assertEqual(self.request('GET',path+'?page=0')[0],422)
        self.assertEqual(self.request('GET',path+'?size=101')[0],422)
        self.assertEqual(self.request('GET',path+'?message_id=0')[0],422)
        self.assertEqual(self.request('GET',path+'?delivery_id=999999')[0],404)
        self.assertEqual(self.request('GET',path+f'?message_id={legacy}')[1]['total'],0)
        listing=self.request('GET',path+f'?message_id={ai_mid}')[1]
        self.assertEqual((listing['total'],listing['items'][0]['id']),(1,aid))
        self.assertEqual(self.request('GET',f'/api/admin/conversations/{other}/ai-decisions?message_id={ai_mid}')[0],404)
        self.assertEqual(self.request('GET',f'/api/admin/conversations/{other}/ai-decisions/{aid}')[0],404)
        self.assertEqual(self.request('GET',path+'?message_id='+str(mids[0]))[0],404)
        self.assertEqual(self.request('GET','/api/admin/conversations/999999/ai-decisions')[0],404)
        detail=self.request('GET',path+f'/{aid}')[1]
        self.assertEqual(detail['context'],'snapshot')
        self.assertEqual(detail['sources'],[])
        # Admin message history and the public visitor protocol never carry evidence blobs.
        history=self.request('GET',f'/api/admin/conversations/{cid}/messages')[1]
        self.assertNotIn('snapshot',json.dumps(history))
        self.request('DELETE',f'/api/admin/conversations/{cid}')
        self.assertEqual(self.request('GET',path+f'/{aid}')[0],404)

    def test_ai_test_uses_same_sources_and_redacts_provider_errors(self):
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        import threading
        requests=[]
        class Handler(BaseHTTPRequestHandler):
            answer='[转人工:资料不足]'
            fail=False
            def log_message(self,*args): pass
            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(502 if self.fail else 200)
                self.send_header('Content-Type','application/json');self.end_headers()
                self.wfile.write(json.dumps({'error':'provider-private-secret'} if self.fail else {'choices':[{'message':{'content':self.answer}}]}).encode())
        stub=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.addCleanup(stub.server_close);self.addCleanup(stub.shutdown)
        threading.Thread(target=stub.serve_forever,daemon=True).start()
        self.request('PUT','/api/admin/ai/settings',{'enabled':False,'base_url':f'http://127.0.0.1:{stub.server_port}/v1','api_key':'provider-private-secret','model':'fake-model','prompt':'private-prompt'})
        self.addCleanup(self.request,'PUT','/api/admin/ai/settings',{'enabled':False,'base_url':'','api_key':'','model':'','prompt':'','no_reply_minutes':3})
        self.request('POST','/api/admin/qa',{'site_id':self.sites[0]['id'],'question':'套餐 pricing','answer':'每月 100 元'})
        code,result=self.request('POST','/api/admin/ai/test',{'site_id':self.sites[0]['id'],'question':'套餐 pricing'})
        self.assertEqual(code,200)
        self.assertEqual(result['reason_code'],'insufficient_sources')
        self.assertTrue(result['handoff']);self.assertEqual(result['answer'],'')
        self.assertEqual(result['sources'][0]['type'],'qa')
        self.assertIn(result['context'],requests[0]['messages'][0]['content'])
        self.assertNotIn('provider-private-secret',json.dumps(result))
        self.assertNotIn('private-prompt',json.dumps(result))
        for question in ['', 'X'*5001]: self.assertEqual(self.request('POST','/api/admin/ai/test',{'question':question})[0],422)
        self.assertEqual(self.request('POST','/api/admin/ai/test',{'question':'test','site_id':999999})[0],404)
        self.assertEqual(self.request('POST','/api/admin/ai/test',{'question':'test'},authenticated=False)[0],401)
        Handler.fail=True
        code,result=self.request('POST','/api/admin/ai/test',{'question':'test'})
        self.assertEqual(code,502);self.assertNotIn('provider-private-secret',json.dumps(result))
        with sqlite3.connect(Path(self.tmp.name)/'msghub.db') as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM ai_decisions WHERE conversation_id IN (SELECT id FROM conversations WHERE site_id=?)',(self.sites[0]['id'],)).fetchone()[0],0)
        Handler.fail=False;Handler.answer='套餐每月 100 元。'
        self.request('PUT','/api/admin/ai/settings',{'enabled':True,'base_url':f'http://127.0.0.1:{stub.server_port}/v1','model':'fake-model','prompt':'private-prompt'})
        site=self.sites[0]
        self.request('PUT',f"/api/admin/sites/{site['id']}",{'name':site['name'],'require_contact':False,'ai_enabled':True,'work_hours':None})
        with connect(self.ws_base+'/api/admin/ws') as admin_ws,connect(self.ws_base+'/api/chat/ws') as visitor:
            admin_ws.send(json.dumps({'type':'auth','token':self.token}));self.assertEqual(json.loads(admin_ws.recv())['type'],'ready')
            visitor.send(json.dumps({'type':'hello','site_key':site['site_key']}))
            public_site=json.loads(visitor.recv())['site']
            self.assertTrue(public_site['online'])
            self.assertTrue(public_site['ai_available'])
            sent_at=time.monotonic()
            visitor.send(json.dumps({'type':'msg','content':'套餐 pricing'}))
            visitor.send(json.dumps({'type':'msg','content':'请问月费'}))
            visitor_token=None;events=[]
            while True:
                event=json.loads(visitor.recv(timeout=6));events.append(event)
                if event['type']=='token': visitor_token=event['token']
                if event['type']=='msg' and event['message']['sender']=='ai': break
            self.assertLess(time.monotonic()-sent_at,8)
            self.assertTrue(any(e['type']=='ai_status' and e['thinking'] for e in events))
            self.assertTrue(any(e['type']=='ai_status' and not e['thinking'] for e in events))
            self.assertIn('套餐 pricing\n请问月费',requests[-1]['messages'][-1]['content'])
            self.assertNotIn('ai_decision_changed',str(events))
            self.assertNotIn('sources',str(events));self.assertNotIn('private-prompt',str(events))
            for _ in range(20):
                event=json.loads(admin_ws.recv(timeout=3))
                if event['type']=='ai_decision_changed': break
            else: self.fail('No admin-only AI event')
            cid,did=event['conversation_id'],event['decision_id']
            detail=self.request('GET',f'/api/admin/conversations/{cid}/ai-decisions/{did}')[1]
            self.assertEqual(detail['outcome'],'answered');self.assertEqual(detail['source_count'],1)
            self.assertEqual(detail['trigger'],'AI 快速接待')
            self.assertEqual(detail['question'],'套餐 pricing\n请问月费')
            with self.assertRaises(TimeoutError): visitor.recv(timeout=.2)
        with connect(self.ws_base+'/api/chat/ws') as visitor:
            visitor.send(json.dumps({'type':'hello','site_key':site['site_key'],'token':visitor_token}))
            init=json.loads(visitor.recv())
            self.assertNotIn('sources',str(init));self.assertNotIn('reason_origin',str(init))
            self.assertNotIn('private-prompt',str(init));self.assertNotIn('provider-private-secret',str(init))

    def test_conversation_summary_api_model_contract_and_visitor_privacy(self):
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        import threading
        requests=[]
        class Handler(BaseHTTPRequestHandler):
            fail=False
            def log_message(self,*args): pass
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append(body)
                sources=json.loads(body['messages'][1]['content'])
                mid=next(m['id'] for m in sources if m['sender']=='visitor')
                text=json.dumps({'summary':'客户需要报价。','needs':[{'value':'报价','message_id':mid,'quote':'需要报价'}],
                                 'contacts':[{'value':'12345678','message_id':mid,'quote':'QQ 12345678'}]})
                self.send_response(502 if self.fail else 200);self.send_header('Content-Type','application/json');self.end_headers()
                self.wfile.write(json.dumps({'error':'summary-provider-secret'} if self.fail else {'choices':[{'message':{'content':text}}]}).encode())
        stub=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.addCleanup(stub.server_close);self.addCleanup(stub.shutdown)
        threading.Thread(target=stub.serve_forever,daemon=True).start()
        self.addCleanup(self.request,'PUT','/api/admin/ai/settings',{'enabled':False,'base_url':'','api_key':'','model':'','prompt':''})
        self.request('PUT','/api/admin/ai/settings',{'enabled':True,'base_url':f'http://127.0.0.1:{stub.server_port}/v1','api_key':'summary-provider-secret','model':'fake-summary','prompt':'private-summary-prompt'})
        site=self.sites[0]
        with connect(self.ws_base+'/api/chat/ws') as visitor,connect(self.ws_base+'/api/admin/ws') as admin_ws:
            admin_ws.send(json.dumps({'type':'auth','token':self.token}));admin_ws.recv()
            visitor.send(json.dumps({'type':'hello','site_key':site['site_key']}));visitor.recv()
            visitor.send(json.dumps({'type':'msg','content':'需要报价，QQ 12345678'}))
            visitor_token=None
            while True:
                e=json.loads(visitor.recv(timeout=3))
                if e['type']=='token': visitor_token=e['token']
                if e['type']=='msg': break
            cid=self.request('GET',f"/api/admin/conversations?site_id={site['id']}")[1]['items'][0]['id']
            # These API fixtures belong to a human-managed conversation.
            with sqlite3.connect(Path(self.tmp.name)/"msghub.db") as db:
                db.execute("UPDATE conversations SET status='active' WHERE id=?", (cid,))
            self.request('PUT',f"/api/admin/sites/{site['id']}",{'name':site['name'],'require_contact':False,'ai_enabled':True,'work_hours':None})
            path=f'/api/admin/conversations/{cid}/summary'
            self.request('POST',f'/api/admin/conversations/{cid}/notes',{'content':'summary-internal-note'})
            for method in ['GET','POST']: self.assertEqual(self.request(method,path,authenticated=False)[0],401)
            self.assertIsNone(self.request('GET',path)[1]['item'])
            before=self.request('GET',f'/api/admin/conversations/{cid}/messages')[1]
            status,result=self.request('POST',path)
            self.assertEqual(status,200);self.assertEqual(result['item']['status'],'ready')
            self.assertEqual(result['item']['contacts'][0]['value'],'12345678')
            self.assertNotIn('private-summary-prompt',str(requests));self.assertNotIn('summary-internal-note',str(requests))
            self.assertNotIn('summary-provider-secret',str(result))
            self.assertEqual(self.request('GET',f'/api/admin/conversations/{cid}/messages')[1],before)
            for _ in range(20):
                event=json.loads(admin_ws.recv(timeout=3))
                if event['type']=='conversation_summary_changed': break
            else: self.fail('No private summary event')
            with self.assertRaises(TimeoutError): visitor.recv(timeout=.2)
            self.request('POST',path);self.assertEqual(len(requests),1)
            other,_=self.seed_search_conversation(self.sites[1])
            self.assertEqual(self.request('GET',f"/api/admin/conversations/{other}/summary?summary_id={result['item']['id']}")[0],404)
            self.assertEqual(self.request('GET',path+'?summary_id=0')[0],422)
            visitor.send(json.dumps({'type':'msg','content':'又有新问题'}))
            while json.loads(visitor.recv(timeout=3))['type']!='msg': pass
            self.assertTrue(self.request('GET',path)[1]['stale'])
            Handler.fail=True
            error=self.request('POST',path)[1]['item']
            self.assertEqual(error['status'],'error');self.assertNotIn('summary-provider-secret',str(error))
        with connect(self.ws_base+'/api/chat/ws') as visitor:
            visitor.send(json.dumps({'type':'hello','site_key':site['site_key'],'token':visitor_token}))
            init=json.loads(visitor.recv(timeout=3))
            self.assertNotIn('客户需要报价',str(init));self.assertNotIn('contacts',str(init));self.assertNotIn('summary-internal-note',str(init))
        self.request('DELETE',f'/api/admin/conversations/{cid}')
        self.assertEqual(self.request('GET',path)[0],404)

    def test_triage_api_filters_settings_worker_and_visitor_privacy(self):
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        import threading
        requests=[]
        tag=self.create_tag('故障标签')
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append(body)
                payload=json.loads(body['messages'][1]['content'])
                vm=next(m for m in reversed(payload['sources']) if m['sender']=='visitor')
                answer=json.dumps({'category':{'code':'support','message_id':vm['id'],'quote':vm['content']},
                                   'priority':{'level':'high','message_id':vm['id'],'quote':vm['content']},
                                   'tags':[{'id':tag['id'],'name':tag['name'],'message_id':vm['id'],'quote':vm['content']}]})
                self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers()
                self.wfile.write(json.dumps({'choices':[{'message':{'content':answer}}]}).encode())
        stub=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.addCleanup(stub.server_close);self.addCleanup(stub.shutdown)
        threading.Thread(target=stub.serve_forever,daemon=True).start()
        self.addCleanup(self.request,'PUT','/api/admin/ai/settings',{'enabled':False,'triage_enabled':False,'auto_tags':False,'base_url':'','api_key':'','model':'','prompt':''})
        settings={'enabled':True,'triage_enabled':False,'auto_tags':False,'base_url':f'http://127.0.0.1:{stub.server_port}/v1','api_key':'triage-http-secret','model':'fake-classifier','prompt':'private-triage-prompt'}
        self.request('PUT','/api/admin/ai/settings',settings)
        site=self.sites[0]
        with connect(self.ws_base+'/api/chat/ws') as visitor,connect(self.ws_base+'/api/admin/ws') as admin_ws:
            admin_ws.send(json.dumps({'type':'auth','token':self.token}));admin_ws.recv()
            visitor.send(json.dumps({'type':'hello','site_key':site['site_key']}));visitor.recv()
            visitor.send(json.dumps({'type':'msg','content':'紧急故障需要处理'}))
            visitor_token=None
            while True:
                e=json.loads(visitor.recv(timeout=3))
                if e['type']=='token': visitor_token=e['token']
                if e['type']=='msg': break
            cid=self.request('GET',f"/api/admin/conversations?site_id={site['id']}")[1]['items'][0]['id']
            # These API fixtures belong to a human-managed conversation.
            with sqlite3.connect(Path(self.tmp.name)/"msghub.db") as db:
                db.execute("UPDATE conversations SET status='active' WHERE id=?", (cid,))
            self.request('PUT',f"/api/admin/sites/{site['id']}",{'name':site['name'],'require_contact':False,'ai_enabled':True,'work_hours':None})
            path=f'/api/admin/conversations/{cid}/triage'
            self.assertIsNone(self.request('GET',path)[1]['item'])
            for method in ('GET','POST'): self.assertEqual(self.request(method,path,authenticated=False)[0],401)
            self.request('POST',f'/api/admin/conversations/{cid}/notes',{'content':'triage-private-note'})
            status,result=self.request('POST',path)
            self.assertEqual(status,200);item=result['item'];aid=item['id']
            self.assertEqual(item['category'],'support');self.assertEqual(item['priority'],'high')
            self.assertEqual(result['tags'],[])
            for _ in range(20):
                event=json.loads(admin_ws.recv(timeout=3))
                if event['type']=='triage_changed': break
            else: self.fail('No admin-only classification event')
            with self.assertRaises(TimeoutError): visitor.recv(timeout=.2)
            self.assertNotIn('triage-private-note',str(requests));self.assertNotIn('private-triage-prompt',str(requests))
            self.assertNotIn('triage-http-secret',str(result))
            self.request('POST',path);self.assertEqual(len(requests),1)
            self.assertEqual(self.request('GET',f'/api/admin/conversations?category=support&priority=high&site_id={site["id"]}')[1]['total'],1)
            self.assertEqual(self.request('GET','/api/admin/conversations?category=bad')[0],400)
            self.assertEqual(self.request('GET','/api/admin/conversations?priority=bad')[0],400)
            self.assertEqual(self.request('GET',path+'?analysis_id=0')[0],422)
            other,_=self.seed_search_conversation(self.sites[1])
            self.assertEqual(self.request('GET',f'/api/admin/conversations/{other}/triage?analysis_id={aid}')[0],404)
            for suffix in (f'/{aid}/apply-tags','/dismiss'):
                self.assertEqual(self.request('POST',path+suffix,authenticated=False)[0],401)
            applied=self.request('POST',path+f'/{aid}/apply-tags')[1]
            self.assertEqual(applied['tags'][0]['id'],tag['id'])
            self.request('DELETE',f'/api/admin/conversations/{cid}/tags/{tag["id"]}')
            self.request('POST',path+'/dismiss')
            self.assertEqual(self.request('GET',f'/api/admin/conversations?priority=high&site_id={site["id"]}')[1]['total'],0)
            settings.update(triage_enabled=True,auto_tags=True)
            self.request('PUT','/api/admin/ai/settings',settings)
            self.assertTrue(self.request('GET','/api/admin/ai/settings')[1]['auto_tags'])
            visitor.send(json.dumps({'type':'msg','content':'又有紧急故障需要处理'}))
            while json.loads(visitor.recv(timeout=3))['type']!='msg': pass
            self.assertEqual(self.request('POST',path+f'/{aid}/apply-tags')[0],409)
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                result=self.request('GET',path)[1]
                if result['item']['id']!=aid: break
                time.sleep(.1)
            else: self.fail('Durable classifier worker did not process new visitor message')
            self.assertEqual(result['tags'],[])  # Explicit manual removal blocks auto-add.
            self.assertEqual(self.request('GET','/api/admin/qq/deliveries?kind=priority')[0],200)
            self.assertEqual(self.request('GET','/api/admin/qq/deliveries?kind=bad')[0],400)
            with self.assertRaises(TimeoutError): visitor.recv(timeout=.2)
        with connect(self.ws_base+'/api/chat/ws') as visitor:
            visitor.send(json.dumps({'type':'hello','site_key':site['site_key'],'token':visitor_token}))
            init=json.loads(visitor.recv(timeout=3))
            self.assertNotIn('triage',str(init));self.assertNotIn('category_evidence',str(init));self.assertNotIn('故障标签',str(init))
        self.request('DELETE',f'/api/admin/conversations/{cid}')
        self.assertEqual(self.request('GET',path)[0],404)


if __name__ == "__main__":
    unittest.main()

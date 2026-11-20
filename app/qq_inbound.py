"""Save verified QQ callbacks before acknowledging them and resume unfinished work."""

import asyncio
import hashlib
import json
import time

from . import chat
from .db import NOW_SQL, begin_write, get_db, insert_once

MAX_ATTEMPTS = 5
_wake: asyncio.Event | None = None


def accept(app_id: str, event_id: str, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    identity = str(payload.get("id") or event_id) or body
    key = hashlib.sha256(f"{app_id}\0{identity}".encode()).hexdigest()
    with get_db() as db:
        insert_once(
            db,
            "INSERT INTO qq_inbound "
            "(event_key,app_id,payload,received_at,binding_version,default_conv_id,default_set_at) "
            "SELECT ?,?,?,?,binding_version,CASE WHEN EXISTS (SELECT 1 FROM qq_deliveries WHERE status='sending') "
            "THEN NULL ELSE current_conv_id END,current_set_at FROM qq_agent WHERE id=1",
            (key, app_id, body, time.time()),
        )
        db.execute(f"UPDATE qq_agent SET last_event_at={NOW_SQL} WHERE id=1")
    if _wake is not None:
        _wake.set()


def pin_reply(event: dict, target: int, implicit: bool) -> None:
    with get_db() as db:
        db.execute(
            "UPDATE qq_inbound SET target_conv_id=?,`implicit`=? WHERE id=? AND status='pending'",
            (target, implicit, event["id"]),
        )


def commit_reply(event: dict, openid: str, target: int, implicit: bool,
                 contents: list[tuple[str, str]]) -> tuple[list[dict], str]:
    """Commit the full reply and its receipt together before broadcasting."""
    with get_db() as db:
        begin_write(db)
        receipt = db.execute("SELECT status FROM qq_inbound WHERE id=?", (event["id"],)).fetchone()
        if not receipt or receipt["status"] != "pending":
            return [], ""
        owner = db.execute("SELECT openid,binding_version,current_conv_id,current_set_at FROM qq_agent WHERE id=1").fetchone()
        if owner["openid"] != openid or owner["binding_version"] != event["binding_version"]:
            finish(db, event["id"])
            return [], ""
        if not db.execute("SELECT 1 FROM conversations WHERE id=?", (target,)).fetchone():
            finish(db, event["id"])
            return [], f"会话 #{target} 不存在"
        messages = [chat.add_message(target, "agent", content, msg_type=kind, db=db)
                    for kind, content in contents]
        same_default = (owner["current_conv_id"] == event["default_conv_id"]
                        and owner["current_set_at"] == event["default_set_at"])
        if messages and not implicit and not event["attempts"] and same_default and owner["current_conv_id"] != target:
            db.execute("UPDATE qq_agent SET current_conv_id=?,current_set_at=0 WHERE id=1", (target,))
        finish(db, event["id"])
    return messages, ""


def finish(db, event_id: int) -> None:
    db.execute("UPDATE qq_inbound SET status='done',payload=NULL,next_attempt_at=0 WHERE id=? AND status='pending'",
               (event_id,))


async def process_one() -> bool:
    """One worker processes accepted events. A crash before commit leaves the row pending."""
    from . import qq

    with get_db() as db:
        row = db.execute(
            "SELECT * FROM qq_inbound WHERE status='pending' AND next_attempt_at<=? ORDER BY id LIMIT 1",
            (time.time(),),
        ).fetchone()
    if row is None:
        return False
    if row["app_id"] != qq.QQ_APP_ID:
        with get_db() as db:
            db.execute("UPDATE qq_inbound SET status='cancelled',payload=NULL WHERE id=?", (row["id"],))
        return True
    try:
        await qq.handle_c2c(json.loads(row["payload"]), inbound=dict(row))
        with get_db() as db:
            finish(db, row["id"])
    except Exception as exc:
        attempts = row["attempts"] + 1
        status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
        with get_db() as db:
            updated = db.execute(
                "UPDATE qq_inbound SET status=?,attempts=?,next_attempt_at=?,"
                "payload=CASE WHEN ?='failed' THEN NULL ELSE payload END WHERE id=? AND status='pending'",
                (status, attempts, time.time() + min(60, 2 ** attempts), status, row["id"]),
            ).rowcount
        if updated:
            qq.log(f"接收任务 #{row['id']} {status}，第 {attempts} 次处理异常：{type(exc).__name__}")
        else:
            qq.log(f"接收任务 #{row['id']} 已结束，后续通知异常：{type(exc).__name__}")
    return True


async def run() -> None:
    from . import qq

    global _wake
    _wake = asyncio.Event()
    try:
        while True:
            try:
                if await process_one():
                    await asyncio.sleep(0)
                    continue
            except Exception as exc:
                qq.log(f"读取待处理 QQ 回调失败：{type(exc).__name__}")
            try:
                async with asyncio.timeout(2):
                    await _wake.wait()
            except TimeoutError:
                pass
            _wake.clear()
    finally:
        _wake = None

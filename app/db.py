import sqlite3
from contextlib import contextmanager

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    domain          TEXT    NOT NULL DEFAULT '',
    site_key        TEXT    NOT NULL UNIQUE,
    allowed_origins TEXT    NOT NULL DEFAULT '',   -- 逗号分隔，空 = 不限制
    webhook_url     TEXT    NOT NULL DEFAULT '',   -- 新留言推送地址，可选
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id     INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    name        TEXT    NOT NULL DEFAULT '',
    contact     TEXT    NOT NULL DEFAULT '',
    content     TEXT    NOT NULL,
    page_url    TEXT    NOT NULL DEFAULT '',
    ip          TEXT    NOT NULL DEFAULT '',
    user_agent  TEXT    NOT NULL DEFAULT '',
    status      TEXT    NOT NULL DEFAULT 'new',    -- new / read / handled / spam
    note        TEXT    NOT NULL DEFAULT '',       -- 内部备注
    created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_messages_site    ON messages(site_id);
CREATE INDEX IF NOT EXISTS idx_messages_status  ON messages(status);
CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);

-- ---------- 二期：在线对话 ----------

CREATE TABLE IF NOT EXISTS visitors (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    token         TEXT    NOT NULL UNIQUE,          -- 存在访客浏览器里的匿名身份
    name          TEXT    NOT NULL DEFAULT '',
    contact       TEXT    NOT NULL DEFAULT '',
    ip            TEXT    NOT NULL DEFAULT '',
    user_agent    TEXT    NOT NULL DEFAULT '',
    first_page    TEXT    NOT NULL DEFAULT '',
    created_at    TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    last_seen_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS conversations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,  -- 即会话编号 #id
    site_id       INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    visitor_id    INTEGER NOT NULL REFERENCES visitors(id) ON DELETE CASCADE,
    status        TEXT    NOT NULL DEFAULT 'waiting', -- waiting / active / closed
    page_url      TEXT    NOT NULL DEFAULT '',        -- 最近一次发消息时所在页面
    last_msg_at   TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    last_preview  TEXT    NOT NULL DEFAULT '',
    unread        INTEGER NOT NULL DEFAULT 0,         -- 客服未读的访客消息数
    created_at    TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    closed_at     TEXT,
    UNIQUE (site_id, visitor_id)
);
CREATE INDEX IF NOT EXISTS idx_conv_last ON conversations(last_msg_at);

CREATE TABLE IF NOT EXISTS chat_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    sender          TEXT    NOT NULL,                 -- visitor / agent / system
    content         TEXT    NOT NULL,
    qq_message_id   TEXT    NOT NULL DEFAULT '',      -- 二期第 ② 阶段：转发到 QQ 后的消息 ID
    created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_chat_conv ON chat_messages(conversation_id, id);

-- ---------- 二期 ②：QQ 机器人 ----------

-- 客服（只有 1 位），固定 id = 1
CREATE TABLE IF NOT EXISTS qq_agent (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    openid          TEXT    NOT NULL DEFAULT '',   -- 客服在本机器人下的 user_openid
    bound_at        TEXT,
    online          INTEGER NOT NULL DEFAULT 1,    -- /在 /忙
    current_conv_id INTEGER,                       -- 直接发内容时的默认会话
    current_set_at  REAL    NOT NULL DEFAULT 0,    -- 默认会话最近一次变化的时间（epoch）
    last_msg_id     TEXT    NOT NULL DEFAULT '',   -- 客服最近一条消息 ID，用于被动回复
    last_msg_at     REAL    NOT NULL DEFAULT 0,
    passive_used    INTEGER NOT NULL DEFAULT 0,
    bind_code       TEXT    NOT NULL DEFAULT '',
    bind_expires    REAL    NOT NULL DEFAULT 0,
    last_event_at   TEXT                           -- 最近一次收到平台回调的时间
);
INSERT OR IGNORE INTO qq_agent (id) VALUES (1);

-- 机器人发给客服的每条消息 → 对应会话，客服"引用回复"时据此定位访客
CREATE TABLE IF NOT EXISTS qq_forwards (
    qq_msg_id       TEXT    PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
"""

STATUSES = ("new", "read", "handled", "spam")
CONV_STATUSES = ("waiting", "active", "closed")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def get_db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.executescript(SCHEMA)
        # 老库迁移：留言关联来源会话（离线会话归档为留言时使用）
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
        if "conversation_id" not in cols:
            conn.execute("ALTER TABLE messages ADD COLUMN conversation_id INTEGER")
        # 访客消息转发到 QQ 的状态：'' 未涉及 / pending 待发 / sent 已发
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(chat_messages)")}
        if "qq_state" not in cols:
            conn.execute("ALTER TABLE chat_messages ADD COLUMN qq_state TEXT NOT NULL DEFAULT ''")


def rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]

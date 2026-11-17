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
CREATE INDEX IF NOT EXISTS idx_chat_conv_created ON chat_messages(conversation_id, created_at, id);

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

CREATE TABLE IF NOT EXISTS qq_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    recipient TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'visitor',
    summary_id INTEGER REFERENCES conversation_summaries(id) ON DELETE SET NULL,
    triage_id INTEGER REFERENCES conversation_triage(id) ON DELETE SET NULL,
    event_key TEXT,
    reason TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    inbox_message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    warning TEXT NOT NULL DEFAULT '',
    last_attempt_at REAL NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    sent_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_qq_deliveries_status ON qq_deliveries(status, next_attempt_at, id);
CREATE INDEX IF NOT EXISTS idx_qq_deliveries_conv ON qq_deliveries(conversation_id, id);
CREATE TABLE IF NOT EXISTS qq_delivery_messages (
    delivery_id INTEGER NOT NULL REFERENCES qq_deliveries(id) ON DELETE CASCADE,
    message_id INTEGER NOT NULL UNIQUE REFERENCES chat_messages(id) ON DELETE CASCADE,
    PRIMARY KEY (delivery_id, message_id)
);
CREATE TABLE IF NOT EXISTS qq_delivery_parts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id INTEGER NOT NULL REFERENCES qq_deliveries(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    qq_message_id TEXT NOT NULL DEFAULT '',
    manually_confirmed INTEGER NOT NULL DEFAULT 0,
    sent_at TEXT,
    UNIQUE (delivery_id, sequence)
);
CREATE TABLE IF NOT EXISTS qq_notification_sources (
    delivery_id INTEGER NOT NULL REFERENCES qq_deliveries(id) ON DELETE CASCADE,
    message_id INTEGER NOT NULL REFERENCES chat_messages(id) ON DELETE CASCADE,
    PRIMARY KEY (delivery_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_qq_notification_message ON qq_notification_sources(message_id, delivery_id);

CREATE TABLE IF NOT EXISTS ai_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    source_upto INTEGER NOT NULL,
    message_id INTEGER REFERENCES chat_messages(id) ON DELETE SET NULL,
    inbox_message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    qq_delivery_id INTEGER REFERENCES qq_deliveries(id) ON DELETE SET NULL,
    outcome TEXT NOT NULL,
    trigger TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    reason TEXT NOT NULL,
    reason_origin TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    question TEXT NOT NULL,
    question_truncated INTEGER NOT NULL DEFAULT 0,
    answer TEXT NOT NULL DEFAULT '',
    context TEXT NOT NULL DEFAULT '',
    sources TEXT NOT NULL DEFAULT '[]',
    source_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE (conversation_id, source_upto)
);
CREATE INDEX IF NOT EXISTS idx_ai_decisions_message ON ai_decisions(message_id);
CREATE INDEX IF NOT EXISTS idx_ai_decisions_conv ON ai_decisions(conversation_id, id);
CREATE INDEX IF NOT EXISTS idx_ai_decisions_delivery ON ai_decisions(qq_delivery_id);

CREATE TABLE IF NOT EXISTS conversation_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    source_upto INTEGER NOT NULL,
    status TEXT NOT NULL,
    error_code TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    needs TEXT NOT NULL DEFAULT '[]',
    contacts TEXT NOT NULL DEFAULT '[]',
    sources TEXT NOT NULL DEFAULT '[]',
    source_count INTEGER NOT NULL DEFAULT 0,
    truncated INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE (conversation_id, source_upto)
);
CREATE INDEX IF NOT EXISTS idx_conversation_summaries_conv ON conversation_summaries(conversation_id, source_upto);

CREATE TABLE IF NOT EXISTS conversation_triage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    source_upto INTEGER NOT NULL,
    status TEXT NOT NULL,
    error_code TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT 'other',
    category_evidence TEXT NOT NULL DEFAULT '{}',
    priority TEXT NOT NULL DEFAULT 'normal',
    priority_evidence TEXT NOT NULL DEFAULT '{}',
    tag_suggestions TEXT NOT NULL DEFAULT '[]',
    sources TEXT NOT NULL DEFAULT '[]',
    source_count INTEGER NOT NULL DEFAULT 0,
    truncated INTEGER NOT NULL DEFAULT 0,
    dismissed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE (conversation_id, source_upto)
);
CREATE INDEX IF NOT EXISTS idx_conversation_triage_conv ON conversation_triage(conversation_id, source_upto);
CREATE TABLE IF NOT EXISTS conversation_triage_jobs (
    conversation_id INTEGER PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
    source_upto INTEGER NOT NULL,
    due_at REAL NOT NULL,
    last_run_at REAL NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS conversation_ai_tag_blocks (
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (conversation_id, tag_id)
);

-- ---------- 二期 ③：AI 与站点设置 ----------

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

-- 问答库：site_id 为空表示所有站点通用
CREATE TABLE IF NOT EXISTS qa (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id     INTEGER REFERENCES sites(id) ON DELETE CASCADE,
    question    TEXT    NOT NULL,
    answer      TEXT    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 抓取的网站页面（AI 资料补充）
CREATE TABLE IF NOT EXISTS kb_pages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id     INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    url         TEXT    NOT NULL,
    title       TEXT    NOT NULL DEFAULT '',
    text        TEXT    NOT NULL DEFAULT '',
    fetched_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE (site_id, url)
);

-- ---------- 客服效率工具 ----------

CREATE TABLE IF NOT EXISTS quick_replies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id     INTEGER REFERENCES sites(id) ON DELETE CASCADE,
    title       TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_quick_replies_site ON quick_replies(site_id);

CREATE TABLE IF NOT EXISTS conversation_notes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    content         TEXT    NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_conversation_notes ON conversation_notes(conversation_id, id);

CREATE TABLE IF NOT EXISTS tags (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL COLLATE NOCASE UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS conversation_tags (
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    tag_id          INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (conversation_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_conversation_tags_tag ON conversation_tags(tag_id, conversation_id);
"""

# 老库迁移：表 → [(列名, 定义)]
MIGRATIONS = {
    "qq_deliveries": [
        ("triage_id", "INTEGER REFERENCES conversation_triage(id) ON DELETE SET NULL"),
        ("summary_id", "INTEGER REFERENCES conversation_summaries(id) ON DELETE SET NULL"),
        ("kind", "TEXT NOT NULL DEFAULT 'visitor'"),
        ("event_key", "TEXT"),
        ("reason", "TEXT NOT NULL DEFAULT ''"),
        ("summary", "TEXT NOT NULL DEFAULT ''"),
        ("inbox_message_id", "INTEGER REFERENCES messages(id) ON DELETE SET NULL"),
    ],
    "messages": [("conversation_id", "INTEGER")],                       # 留言关联来源会话
    "chat_messages": [
        ("qq_state", "TEXT NOT NULL DEFAULT ''"),                       # '' / pending / sent
        ("msg_type", "TEXT NOT NULL DEFAULT 'text'"),                   # text / image（content 为图片路径）
    ],
    "sites": [
        ("welcome", "TEXT NOT NULL DEFAULT ''"),                        # 欢迎语，空 = 默认
        ("offline_text", "TEXT NOT NULL DEFAULT ''"),                   # 离线提示语，空 = 默认
        ("work_hours", "TEXT NOT NULL DEFAULT ''"),                     # JSON {"days":[1..7],"start":"09:00","end":"18:00"}，空 = 全天
        ("require_contact", "INTEGER NOT NULL DEFAULT 1"),              # 对话前必须留称呼和联系方式
        ("ai_enabled", "INTEGER NOT NULL DEFAULT 1"),                   # 该站点是否使用 AI 应答（需全局开启）
        ("crawl_urls", "TEXT NOT NULL DEFAULT ''"),                     # 抓取起始地址，每行一个
    ],
    "conversations": [("ai_priority_dismissed_upto", "INTEGER NOT NULL DEFAULT 0"), ("auto_handled_upto", "INTEGER NOT NULL DEFAULT 0")],  # 已由 AI / 转留言处理到的访客消息 ID
}

STATUSES = ("new", "read", "handled", "spam")
CONV_STATUSES = ("waiting", "active", "closed")

ADMIN_MESSAGE_SQL = (
    "SELECT m.*, s.name AS site_name, c.id AS linked_conversation_id, c.status AS conversation_status "
    "FROM messages m JOIN sites s ON s.id = m.site_id "
    "LEFT JOIN conversations c ON c.id = m.conversation_id AND c.site_id = m.site_id "
)


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
        new_tags = not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tags'"
        ).fetchone()
        conn.executescript(SCHEMA)
        if new_tags:
            conn.executemany("INSERT INTO tags (name) VALUES (?)",
                             [(name,) for name in ("待报价", "售后", "投诉", "重点客户", "已转化")])
        for table, columns in MIGRATIONS.items():
            have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, ddl in columns:
                if name not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        # The legacy messages.conversation_id column must exist before creating this index.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, site_id, id)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_qq_delivery_event ON qq_deliveries(event_key) WHERE event_key IS NOT NULL")


def get_setting(key: str, default: str = "") -> str:
    with get_db() as db:
        r = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return r[0] if r else default


def set_settings(values: dict[str, str]):
    with get_db() as db:
        db.executemany("INSERT INTO settings (key, value) VALUES (?, ?) "
                       "ON CONFLICT(key) DO UPDATE SET value = excluded.value", list(values.items()))


def rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]

import os
import ssl
from datetime import datetime
from pathlib import Path

import pymysql
from pymysql.constants import CLIENT, FIELD_TYPE
from pymysql.converters import conversions
from pymysql.cursors import Cursor


class DuplicateKeyError(Exception):
    pass


class Row(tuple):
    def __new__(cls, values, columns):
        row = super().__new__(cls, values)
        row.columns = columns
        return row

    def keys(self):
        return self.columns

    def __getitem__(self, key):
        if isinstance(key, str):
            key = self.columns.index(key)
        return super().__getitem__(key)


class MySQLCursor(Cursor):
    def _do_get_result(self):
        super()._do_get_result()
        if self.description:
            columns = tuple(column[0] for column in self.description)
            self._rows = tuple(Row(values, columns) for values in self._rows)


class MySQLConnection(pymysql.Connection):
    _write_locked = False

    def begin_write(self):
        if not self._write_locked:
            # SQLite has one writer. Keep write transactions ordered the same way on InnoDB.
            with self.cursor() as cursor:
                cursor.execute("SELECT id FROM schema_state WHERE id = 1 FOR UPDATE")
            self._write_locked = True

    def execute(self, sql, params=None):
        if sql.lstrip().split(None, 1)[0].upper() in {"INSERT", "UPDATE", "DELETE", "REPLACE"}:
            self.begin_write()
        cursor = self.cursor()
        try:
            if params is not None:
                sql = sql.replace("%", "%%").replace("?", "%s")
            cursor.execute(sql, params)
        except pymysql.IntegrityError as exc:
            if exc.args[0] == 1062:
                raise DuplicateKeyError from exc
            raise
        return cursor

    def executemany(self, sql, params):
        self.begin_write()
        cursor = self.cursor()
        cursor.executemany(sql.replace("%", "%%").replace("?", "%s"), params)
        return cursor


def connect():
    host = os.environ.get("DB_HOST", "").strip()
    socket = os.environ.get("DB_SOCKET", "" if host else "/tmp/mysql.sock").strip()
    user = os.environ.get("DB_USER", "msghub")
    database = os.environ.get("DB_NAME", "msghub")
    password = os.environ.get("DB_PASSWORD")
    if not (user and database and password):
        raise ValueError("MySQL 需要有效的 DB_USER、DB_NAME、DB_PASSWORD")
    if socket:
        if not Path(socket).is_absolute():
            raise ValueError("DB_SOCKET 必须为绝对路径")
        endpoint = {"unix_socket": socket}
    else:
        if not host:
            raise ValueError("MySQL TCP 连接需要 DB_HOST")
        # The same-host Docker database uses a generated TLS certificate.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        endpoint = {"host": host, "port": int(os.environ.get("DB_PORT", "3306")), "ssl": context}

    # Match the app's local time and keep the existing API's timestamp strings.
    offset = datetime.now().astimezone().strftime("%z")
    time_zone = f"{offset[:3]}:{offset[3:]}"
    decoders = {**conversions, FIELD_TYPE.DATE: str, FIELD_TYPE.DATETIME: str}
    conn = MySQLConnection(
        **endpoint, user=user, database=database, password=password,
        charset="utf8mb4", cursorclass=MySQLCursor, conv=decoders,
        client_flag=CLIENT.FOUND_ROWS, autocommit=False,
        connect_timeout=5, read_timeout=10, write_timeout=10,
    )
    try:
        if not socket:
            cipher = conn.execute("SHOW SESSION STATUS LIKE 'Ssl_cipher'").fetchone()
            if not cipher or not cipher[1]:
                raise RuntimeError("MySQL TCP 连接未协商 TLS")
        conn.execute("SET SESSION time_zone = ?", (time_zone,))
        conn.execute("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
    except Exception:
        conn.close()
        raise
    return conn


def init_db(conn, default_tags):
    schema = Path(__file__).with_name("mysql_schema.sql").read_text(encoding="utf-8")
    for statement in schema.strip().rstrip(";").split(";"):
        conn.execute(statement)
    if not conn.execute("SHOW COLUMNS FROM visitors LIKE 'site_id'").fetchone():
        conn.execute("ALTER TABLE visitors ADD COLUMN site_id BIGINT, "
                     "ADD FOREIGN KEY (site_id) REFERENCES sites(id) ON DELETE CASCADE")
    # The upsert claims the initialization row through the commit that seeds the tables.
    with conn.cursor() as cursor:
        cursor.execute("INSERT INTO schema_state (id) VALUES (1) ON DUPLICATE KEY UPDATE id = id")
    conn.begin_write()
    seeded = conn.execute("SELECT seeded FROM schema_state WHERE id = 1").fetchone()[0]
    if not seeded:
        conn.execute("INSERT INTO qq_agent (id) VALUES (1)")
        conn.executemany("INSERT INTO tags (name) VALUES (?)", [(name,) for name in default_tags])
        conn.execute("UPDATE schema_state SET seeded = 1 WHERE id = 1")

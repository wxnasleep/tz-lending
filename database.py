"""Small SQLite-compatible adapter for the PostgreSQL connection on Vercel.

The local app keeps using sqlite3. On Vercel, DATABASE_URL selects this
adapter so all lead and Telegram records survive function restarts.
"""
from __future__ import annotations

import re
import psycopg


class Cursor:
    def __init__(self, cursor, lastrowid=None):
        self._cursor = cursor
        self.lastrowid = lastrowid
        self.rowcount = cursor.rowcount

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()


class Connection:
    def __init__(self, url: str):
        self._connection = psycopg.connect(url, connect_timeout=8)

    def execute(self, statement: str, parameters=()):
        sql = statement.strip()
        if sql.upper() == "BEGIN IMMEDIATE":
            cursor = self._connection.execute("SELECT pg_advisory_xact_lock(731904221)")
            return Cursor(cursor)
        if sql.upper() == "PRAGMA TABLE_INFO(LEADS)":
            cursor = self._connection.execute(
                "SELECT ordinal_position - 1, column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'leads' ORDER BY ordinal_position"
            )
            return Cursor(cursor)

        ignored_insert = bool(re.match(r"INSERT\s+OR\s+IGNORE\s+INTO\s+", sql, re.I))
        if ignored_insert:
            sql = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO\s+", "INSERT INTO ", sql, count=1, flags=re.I)
            sql += " ON CONFLICT DO NOTHING"
        sql = re.sub(r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT", "BIGSERIAL PRIMARY KEY", sql, flags=re.I)
        sql = re.sub(r"(ALTER\s+TABLE\s+\w+\s+ADD\s+COLUMN)(\s+)", r"\1 IF NOT EXISTS\2", sql, flags=re.I)
        if re.match(r"INSERT\s+INTO\s+leads\s*\(", sql, re.I) and "RETURNING" not in sql.upper():
            sql += " RETURNING id"
        sql = sql.replace("?", "%s")
        cursor = self._connection.execute(sql, parameters)
        lastrowid = None
        if re.match(r"INSERT\s+INTO\s+leads\s*\(", statement.strip(), re.I):
            inserted = cursor.fetchone()
            lastrowid = inserted[0] if inserted else None
        return Cursor(cursor, lastrowid)

    def commit(self):
        self._connection.commit()

    def rollback(self):
        self._connection.rollback()

    def close(self):
        self._connection.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if exc_type is None:
                self.commit()
            else:
                self.rollback()
        finally:
            self.close()


def connect(database=None, timeout=8, **_kwargs):
    url = __import__("os").environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DATABASE_URL is required for the PostgreSQL adapter")
    return Connection(url)

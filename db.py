#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Слой хранения данных.
- Если задан DATABASE_URL (PostgreSQL / Supabase) — данные в облаке.
- Иначе — локальный SQLite users.db.
Хранит: настройки приложения, пользователей, избранное, скрытые заказы.
"""
import os
import json
import sqlite3
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_PG = DATABASE_URL.startswith("postgres")

_lock = threading.RLock()


def _p(sql: str) -> str:
    return sql.replace("?", "%s") if USE_PG else sql


def get_conn():
    if USE_PG:
        import psycopg2
        if "sslmode=" in DATABASE_URL:
            return psycopg2.connect(DATABASE_URL)
        return psycopg2.connect(DATABASE_URL, sslmode="require")
    conn = sqlite3.connect(os.path.join(BASE_DIR, "users.db"), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _lock, get_conn() as conn:
        cur = conn.cursor()
        if USE_PG:
            cur.execute("""CREATE TABLE IF NOT EXISTS app_settings(
                key TEXT PRIMARY KEY, value TEXT)""")
            cur.execute("""CREATE TABLE IF NOT EXISTS users(
                uid TEXT PRIMARY KEY, created_at TIMESTAMPTZ DEFAULT now())""")
            cur.execute("""CREATE TABLE IF NOT EXISTS favorites(
                uid TEXT NOT NULL, order_id TEXT NOT NULL, PRIMARY KEY(uid, order_id))""")
            cur.execute("""CREATE TABLE IF NOT EXISTS hidden(
                uid TEXT NOT NULL, order_id TEXT NOT NULL, PRIMARY KEY(uid, order_id))""")
        else:
            cur.execute("""CREATE TABLE IF NOT EXISTS app_settings(
                key TEXT PRIMARY KEY, value TEXT)""")
            cur.execute("""CREATE TABLE IF NOT EXISTS users(
                uid TEXT PRIMARY KEY, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
            cur.execute("""CREATE TABLE IF NOT EXISTS favorites(
                uid TEXT NOT NULL, order_id TEXT NOT NULL, PRIMARY KEY(uid, order_id))""")
            cur.execute("""CREATE TABLE IF NOT EXISTS hidden(
                uid TEXT NOT NULL, order_id TEXT NOT NULL, PRIMARY KEY(uid, order_id))""")
        conn.commit()


# ----------------------------- settings -----------------------------
def get_setting(key, default=None):
    with _lock, get_conn() as conn:
        cur = conn.cursor()
        cur.execute(_p("SELECT value FROM app_settings WHERE key=?"), (key,))
        row = cur.fetchone()
    if not row:
        return default
    try:
        return json.loads(row[0])
    except Exception:
        return default


def set_setting(key, value):
    payload = json.dumps(value, ensure_ascii=False)
    with _lock, get_conn() as conn:
        cur = conn.cursor()
        if USE_PG:
            cur.execute(
                "INSERT INTO app_settings(key, value) VALUES(%s, %s) "
                "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                (key, payload))
        else:
            cur.execute(
                "INSERT OR REPLACE INTO app_settings(key, value) VALUES(?, ?)",
                (key, payload))
        conn.commit()


# ----------------------------- users -----------------------------
def ensure_user(uid):
    with _lock, get_conn() as conn:
        cur = conn.cursor()
        cur.execute(_p("INSERT INTO users(uid) VALUES(?)"), (uid,))
        conn.commit()


# ----------------------------- favorites / hidden -----------------------------
def get_uid_list(uid, table):
    if table not in ("favorites", "hidden"):
        return []
    with _lock, get_conn() as conn:
        cur = conn.cursor()
        cur.execute(_p(f"SELECT order_id FROM {table} WHERE uid=?"), (uid,))
        return [r[0] for r in cur.fetchall()]


def set_uid_item(uid, table, order_id, value):
    if table not in ("favorites", "hidden"):
        return
    with _lock, get_conn() as conn:
        cur = conn.cursor()
        if value:
            cur.execute(_p(f"INSERT INTO {table}(uid, order_id) VALUES(?, ?)"), (uid, order_id))
        else:
            cur.execute(_p(f"DELETE FROM {table} WHERE uid=? AND order_id=?"), (uid, order_id))
        conn.commit()
"""API key management with SQLite storage."""

import hashlib
import secrets
import sqlite3
import time
from pathlib import Path

DB_DIR = Path(__file__).parent / "data"
DB_PATH = DB_DIR / "qpa.db"

KEY_PREFIX = "sk-qpa-"


def _ensure_db():
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS api_keys (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            key_hash     TEXT NOT NULL UNIQUE,
            key_prefix   TEXT NOT NULL,
            name         TEXT NOT NULL,
            note         TEXT DEFAULT '',
            created_at   INTEGER NOT NULL,
            last_used_at INTEGER DEFAULT 0,
            is_active    INTEGER DEFAULT 1
        )
    """)
    conn.commit()
    conn.close()


_initialized = False


def _init_once():
    global _initialized
    if not _initialized:
        _ensure_db()
        _initialized = True


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


def generate_key() -> str:
    return KEY_PREFIX + secrets.token_hex(16)


def create_key(name: str, note: str = "") -> dict:
    _init_once()
    raw_key = generate_key()
    key_hash = _hash_key(raw_key)
    key_prefix = raw_key[:12] + "..."
    now = int(time.time())
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        "INSERT INTO api_keys (key_hash, key_prefix, name, note, created_at) VALUES (?, ?, ?, ?, ?)",
        (key_hash, key_prefix, name, note, now)
    )
    conn.commit()
    key_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return {"id": key_id, "key": raw_key, "key_prefix": key_prefix, "name": name, "note": note, "created_at": now}


def list_keys() -> list:
    _init_once()
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute(
        "SELECT id, key_prefix, name, note, created_at, last_used_at, is_active FROM api_keys ORDER BY id DESC"
    ).fetchall()
    conn.close()
    return [
        {"id": r[0], "key_prefix": r[1], "name": r[2], "note": r[3],
         "created_at": r[4], "last_used_at": r[5], "is_active": bool(r[6])}
        for r in rows
    ]


def revoke_key(key_id: int) -> bool:
    _init_once()
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.execute("UPDATE api_keys SET is_active = 0 WHERE id = ?", (key_id,))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def validate_key(raw_key: str) -> bool:
    _init_once()
    key_hash = _hash_key(raw_key)
    conn = sqlite3.connect(str(DB_PATH))
    row = conn.execute("SELECT id FROM api_keys WHERE key_hash = ? AND is_active = 1", (key_hash,)).fetchone()
    if row:
        conn.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (int(time.time()), row[0]))
        conn.commit()
        conn.close()
        return True
    conn.close()
    return False


def has_any_keys() -> bool:
    _init_once()
    conn = sqlite3.connect(str(DB_PATH))
    row = conn.execute("SELECT COUNT(*) FROM api_keys WHERE is_active = 1").fetchone()
    conn.close()
    return row[0] > 0

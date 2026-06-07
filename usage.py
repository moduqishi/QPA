"""Usage statistics collection and querying with SQLite."""

import asyncio
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

DB_DIR = Path(__file__).parent / "data"
DB_PATH = DB_DIR / "qpa.db"

_lock = asyncio.Lock()


def _ensure_db():
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_logs (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp         INTEGER NOT NULL,
            date_key          TEXT NOT NULL,
            account           TEXT NOT NULL,
            model             TEXT NOT NULL,
            prompt_tokens     INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            total_tokens      INTEGER DEFAULT 0,
            latency_ms        INTEGER DEFAULT 0,
            stream            INTEGER DEFAULT 0,
            finish_reason     TEXT DEFAULT ''
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_date ON usage_logs(date_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_account_date ON usage_logs(account, date_key)")
    conn.commit()
    conn.close()


_initialized = False


def _init_once():
    global _initialized
    if not _initialized:
        _ensure_db()
        _initialized = True


def _today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def record_usage(account: str, model: str, prompt_tokens: int, completion_tokens: int,
                       total_tokens: int, latency_ms: int, stream: bool, finish_reason: str):
    """Record a usage log entry asynchronously."""
    _init_once()
    ts = int(time.time())
    dk = _today_key()
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute(
            "INSERT INTO usage_logs (timestamp, date_key, account, model, prompt_tokens, "
            "completion_tokens, total_tokens, latency_ms, stream, finish_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, dk, account, model, prompt_tokens, completion_tokens, total_tokens,
             latency_ms, 1 if stream else 0, finish_reason)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[usage] record failed: {e}")


def get_today_stats() -> dict:
    _init_once()
    dk = _today_key()
    conn = sqlite3.connect(str(DB_PATH))
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(total_tokens),0), COALESCE(AVG(latency_ms),0), "
        "COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(completion_tokens),0) "
        "FROM usage_logs WHERE date_key = ?", (dk,)
    ).fetchone()
    # Yesterday for comparison
    from datetime import timedelta
    yk = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    yrow = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(total_tokens),0), COALESCE(AVG(latency_ms),0) "
        "FROM usage_logs WHERE date_key = ?", (yk,)
    ).fetchone()
    conn.close()

    def pct_change(today, yesterday):
        if yesterday == 0:
            return 100 if today > 0 else 0
        return round((today - yesterday) / yesterday * 100)

    return {
        "requests": row[0],
        "total_tokens": row[1],
        "avg_latency_ms": round(row[2]),
        "prompt_tokens": row[3],
        "completion_tokens": row[4],
        "yesterday_requests": yrow[0],
        "yesterday_tokens": yrow[1],
        "yesterday_latency": round(yrow[2]),
        "requests_change": pct_change(row[0], yrow[0]),
        "tokens_change": pct_change(row[1], yrow[1]),
        "latency_change": pct_change(round(row[2]), round(yrow[2])),
    }


def get_summary(days: int = 7) -> dict:
    _init_once()
    conn = sqlite3.connect(str(DB_PATH))
    cutoff = int(time.time()) - days * 86400

    # By model
    model_rows = conn.execute(
        "SELECT model, COUNT(*), SUM(prompt_tokens), SUM(completion_tokens), "
        "SUM(total_tokens), AVG(latency_ms) FROM usage_logs WHERE timestamp >= ? "
        "GROUP BY model ORDER BY COUNT(*) DESC", (cutoff,)
    ).fetchall()

    # By account
    account_rows = conn.execute(
        "SELECT account, COUNT(*), SUM(total_tokens), AVG(latency_ms) "
        "FROM usage_logs WHERE timestamp >= ? "
        "GROUP BY account ORDER BY COUNT(*) DESC", (cutoff,)
    ).fetchall()

    conn.close()

    return {
        "by_model": [
            {"model": r[0], "requests": r[1], "prompt_tokens": r[2],
             "completion_tokens": r[3], "total_tokens": r[4], "avg_latency_ms": round(r[5])}
            for r in model_rows
        ],
        "by_account": [
            {"account": r[0], "requests": r[1], "total_tokens": r[2], "avg_latency_ms": round(r[3])}
            for r in account_rows
        ],
    }


def get_trend(days: int = 7) -> list:
    _init_once()
    conn = sqlite3.connect(str(DB_PATH))
    cutoff = int(time.time()) - days * 86400
    rows = conn.execute(
        "SELECT date_key, COUNT(*), SUM(total_tokens), SUM(prompt_tokens), "
        "SUM(completion_tokens), AVG(latency_ms) FROM usage_logs WHERE timestamp >= ? "
        "GROUP BY date_key ORDER BY date_key", (cutoff,)
    ).fetchall()
    conn.close()
    return [
        {"date": r[0], "requests": r[1], "total_tokens": r[2],
         "prompt_tokens": r[3], "completion_tokens": r[4], "avg_latency_ms": round(r[5])}
        for r in rows
    ]


def clear_history():
    _init_once()
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("DELETE FROM usage_logs")
    conn.commit()
    conn.close()

"""SQLite-backed watchlist CRUD."""

import sqlite3
from datetime import datetime, timezone
from typing import List, Optional

from config import DB_PATH
from models import WatchEntry


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS watchlist (
            url         TEXT PRIMARY KEY,
            plugin_name TEXT NOT NULL,
            event_name  TEXT NOT NULL DEFAULT '',
            added_at    TEXT NOT NULL,
            last_state  TEXT NOT NULL DEFAULT 'UNKNOWN',
            last_check  TEXT NOT NULL DEFAULT '',
            consecutive_failures INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.commit()
    return conn


def add(url: str, plugin_name: str, event_name: str = "") -> None:
    """Add a URL to the watchlist (or update plugin if it already exists)."""
    conn = _connect()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO watchlist (url, plugin_name, event_name, added_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET plugin_name=excluded.plugin_name, event_name=excluded.event_name
        """,
        (url, plugin_name, event_name, now),
    )
    conn.commit()
    conn.close()


def remove(url: str) -> bool:
    """Remove a URL from the watchlist. Returns True if it existed."""
    conn = _connect()
    cur = conn.execute("DELETE FROM watchlist WHERE url = ?", (url,))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def list_all() -> List[WatchEntry]:
    """Return all watched URLs."""
    conn = _connect()
    rows = conn.execute(
        "SELECT url, plugin_name, event_name, added_at, last_state, last_check, consecutive_failures FROM watchlist ORDER BY added_at"
    ).fetchall()
    conn.close()
    return [
        WatchEntry(
            url=r[0],
            plugin_name=r[1],
            event_name=r[2],
            added_at=r[3],
            last_state=r[4],
            last_check=r[5],
            consecutive_failures=r[6],
        )
        for r in rows
    ]


def update_state(url: str, state: str, reset_failures: bool = True) -> None:
    """Update the last known state and check time for a URL."""
    conn = _connect()
    now = datetime.now(timezone.utc).isoformat()
    if reset_failures:
        conn.execute(
            "UPDATE watchlist SET last_state = ?, last_check = ?, consecutive_failures = 0 WHERE url = ?",
            (state, now, url),
        )
    else:
        conn.execute(
            "UPDATE watchlist SET last_state = ?, last_check = ? WHERE url = ?",
            (state, now, url),
        )
    conn.commit()
    conn.close()


def increment_failures(url: str) -> int:
    """Increment the consecutive failure count. Returns new count."""
    conn = _connect()
    conn.execute(
        "UPDATE watchlist SET consecutive_failures = consecutive_failures + 1 WHERE url = ?",
        (url,),
    )
    row = conn.execute("SELECT consecutive_failures FROM watchlist WHERE url = ?", (url,)).fetchone()
    conn.commit()
    conn.close()
    return row[0] if row else 0


def get(url: str) -> Optional[WatchEntry]:
    """Get a single watchlist entry by URL."""
    conn = _connect()
    row = conn.execute(
        "SELECT url, plugin_name, event_name, added_at, last_state, last_check, consecutive_failures FROM watchlist WHERE url = ?",
        (url,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return WatchEntry(
        url=row[0],
        plugin_name=row[1],
        event_name=row[2],
        added_at=row[3],
        last_state=row[4],
        last_check=row[5],
        consecutive_failures=row[6],
    )

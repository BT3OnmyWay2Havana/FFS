"""SQLite storage for the squawk feed, proposals, positions and the paper account."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    desk TEXT NOT NULL,
    symbol TEXT,
    text TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'info'
);
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    symbol TEXT NOT NULL,
    setup TEXT NOT NULL,
    entry REAL NOT NULL,
    stop REAL NOT NULL,
    target REAL NOT NULL,
    qty REAL NOT NULL,
    conviction INTEGER NOT NULL,
    thesis TEXT NOT NULL,
    risks TEXT NOT NULL,
    status TEXT NOT NULL,
    status_note TEXT NOT NULL DEFAULT '',
    decided_ts REAL,
    tg_message_id INTEGER
);
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id INTEGER,
    symbol TEXT NOT NULL,
    qty REAL NOT NULL,
    entry REAL NOT NULL,
    stop REAL NOT NULL,
    target REAL NOT NULL,
    entry_fee REAL NOT NULL DEFAULT 0,
    opened_ts REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    exit REAL,
    exit_fee REAL,
    pnl REAL,
    closed_ts REAL,
    close_reason TEXT,
    mode TEXT NOT NULL DEFAULT 'paper'
);
CREATE TABLE IF NOT EXISTS equity (
    ts REAL NOT NULL,
    value REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    # -- helpers -------------------------------------------------------------
    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur

    def _all(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def _one(self, sql: str, args: tuple = ()) -> dict[str, Any] | None:
        rows = self._all(sql, args)
        return rows[0] if rows else None

    # -- meta ----------------------------------------------------------------
    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self._one("SELECT value FROM meta WHERE key = ?", (key,))
        return json.loads(row["value"]) if row else default

    def set_meta(self, key: str, value: Any) -> None:
        self._exec(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )

    # -- squawk notes --------------------------------------------------------
    def note(self, desk: str, text: str, symbol: str | None = None, level: str = "info") -> None:
        self._exec(
            "INSERT INTO notes(ts, desk, symbol, text, level) VALUES(?, ?, ?, ?, ?)",
            (time.time(), desk, symbol, text, level),
        )

    def recent_notes(self, limit: int = 60) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM notes ORDER BY id DESC LIMIT ?", (limit,))

    def latest_note_per_desk(self) -> dict[str, dict[str, Any]]:
        rows = self._all(
            "SELECT n.* FROM notes n JOIN (SELECT desk, MAX(id) AS id FROM notes GROUP BY desk) m "
            "ON n.id = m.id"
        )
        return {r["desk"]: r for r in rows}

    # -- proposals -----------------------------------------------------------
    def add_proposal(self, **p: Any) -> int:
        cur = self._exec(
            "INSERT INTO proposals(ts, symbol, setup, entry, stop, target, qty, conviction, "
            "thesis, risks, status, status_note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                time.time(), p["symbol"], p["setup"], p["entry"], p["stop"], p["target"],
                p["qty"], p["conviction"], p["thesis"], p["risks"], p["status"],
                p.get("status_note", ""),
            ),
        )
        return int(cur.lastrowid)

    def get_proposal(self, pid: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM proposals WHERE id = ?", (pid,))

    def set_proposal_status(self, pid: int, status: str, note: str = "") -> None:
        self._exec(
            "UPDATE proposals SET status = ?, status_note = ?, decided_ts = ? WHERE id = ?",
            (status, note, time.time(), pid),
        )

    def claim_pending(self, pid: int, new_status: str) -> bool:
        """Atomically move a pending proposal on, so a double tap can't fill twice."""
        cur = self._exec(
            "UPDATE proposals SET status = ?, decided_ts = ? WHERE id = ? AND status = 'pending'",
            (new_status, time.time(), pid),
        )
        return cur.rowcount == 1

    def set_proposal_message(self, pid: int, message_id: int) -> None:
        self._exec("UPDATE proposals SET tg_message_id = ? WHERE id = ?", (message_id, pid))

    def pending_proposals(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM proposals WHERE status = 'pending' ORDER BY id")

    def recent_proposals(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM proposals ORDER BY id DESC LIMIT ?", (limit,))

    def last_proposal_ts(self, symbol: str) -> float | None:
        row = self._one(
            "SELECT MAX(ts) AS ts FROM proposals WHERE symbol = ?",
            (symbol,),
        )
        return row["ts"] if row and row["ts"] is not None else None

    # -- positions -----------------------------------------------------------
    def open_position(self, **p: Any) -> int:
        cur = self._exec(
            "INSERT INTO positions(proposal_id, symbol, qty, entry, stop, target, entry_fee, "
            "opened_ts, mode) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                p.get("proposal_id"), p["symbol"], p["qty"], p["entry"], p["stop"],
                p["target"], p.get("entry_fee", 0.0), time.time(), p.get("mode", "paper"),
            ),
        )
        return int(cur.lastrowid)

    def close_position(self, pos_id: int, exit_price: float, exit_fee: float, pnl: float, reason: str) -> None:
        self._exec(
            "UPDATE positions SET status = 'closed', exit = ?, exit_fee = ?, pnl = ?, "
            "closed_ts = ?, close_reason = ? WHERE id = ? AND status = 'open'",
            (exit_price, exit_fee, pnl, time.time(), reason, pos_id),
        )

    def open_positions(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM positions WHERE status = 'open' ORDER BY id")

    def closed_positions(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM positions WHERE status = 'closed' ORDER BY closed_ts DESC LIMIT ?",
            (limit,),
        )

    def realised_pnl_since(self, ts: float) -> float:
        row = self._one(
            "SELECT COALESCE(SUM(pnl), 0) AS pnl FROM positions WHERE status = 'closed' AND closed_ts >= ?",
            (ts,),
        )
        return float(row["pnl"]) if row else 0.0

    # -- equity --------------------------------------------------------------
    def record_equity(self, value: float) -> None:
        self._exec("INSERT INTO equity(ts, value) VALUES(?, ?)", (time.time(), value))

    def equity_history(self, since: float) -> list[dict[str, Any]]:
        return self._all("SELECT ts, value FROM equity WHERE ts >= ? ORDER BY ts", (since,))

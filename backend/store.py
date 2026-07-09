"""SQLite persistence for the portfolio: cash, positions, trades, equity curve.

All money amounts are integer cents.
"""

import datetime as dt
import os
import sqlite3
import threading

from .config import DATA_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    qty INTEGER NOT NULL,
    avg_price_cents REAL NOT NULL,
    city TEXT,
    title TEXT,
    event_date TEXT,
    opened_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',   -- open | settled
    result TEXT,                           -- yes | no (when settled)
    pnl_cents INTEGER                      -- realized (when settled)
);
CREATE INDEX IF NOT EXISTS idx_positions_open ON positions (status, ticker, side);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    action TEXT NOT NULL,                  -- buy | settle
    qty INTEGER NOT NULL,
    price_cents INTEGER NOT NULL,
    fee_cents INTEGER NOT NULL DEFAULT 0,
    mode TEXT NOT NULL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS equity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    cash_cents INTEGER NOT NULL,
    positions_cents INTEGER NOT NULL,
    total_cents INTEGER NOT NULL
);
"""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, mode: str, starting_bankroll_cents: int):
        self.path = os.path.join(DATA_DIR, f"portfolio_{mode}.db")
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        with self._lock, self._conn:
            row = self._conn.execute("SELECT value FROM meta WHERE key='cash_cents'").fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO meta VALUES ('cash_cents', ?)", (str(starting_bankroll_cents),)
                )
                self._conn.execute(
                    "INSERT INTO meta VALUES ('starting_cents', ?)", (str(starting_bankroll_cents),)
                )

    # ---- cash ----

    def cash_cents(self) -> int:
        row = self._conn.execute("SELECT value FROM meta WHERE key='cash_cents'").fetchone()
        return int(row["value"])

    def starting_cents(self) -> int:
        row = self._conn.execute("SELECT value FROM meta WHERE key='starting_cents'").fetchone()
        return int(row["value"])

    def _set_cash(self, cents: int):
        self._conn.execute("UPDATE meta SET value=? WHERE key='cash_cents'", (str(cents),))

    # ---- trading ----

    def record_buy(self, sig, mode: str):
        """Debit cash and add/extend a position for a filled buy."""
        cost = sig.stake_cents + sig.fee_cents
        with self._lock, self._conn:
            cash = self.cash_cents()
            if cost > cash:
                raise ValueError("insufficient cash")
            self._set_cash(cash - cost)
            row = self._conn.execute(
                "SELECT id, qty, avg_price_cents FROM positions"
                " WHERE ticker=? AND side=? AND status='open'",
                (sig.ticker, sig.side),
            ).fetchone()
            if row:
                qty = row["qty"] + sig.contracts
                avg = (row["qty"] * row["avg_price_cents"] + sig.stake_cents) / qty
                self._conn.execute(
                    "UPDATE positions SET qty=?, avg_price_cents=? WHERE id=?",
                    (qty, avg, row["id"]),
                )
            else:
                self._conn.execute(
                    "INSERT INTO positions (ticker, side, qty, avg_price_cents, city, title,"
                    " event_date, opened_at) VALUES (?,?,?,?,?,?,?,?)",
                    (sig.ticker, sig.side, sig.contracts, float(sig.price_cents), sig.city,
                     sig.title, sig.event_date, _now()),
                )
            self._conn.execute(
                "INSERT INTO trades (at, ticker, side, action, qty, price_cents, fee_cents, mode, note)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (_now(), sig.ticker, sig.side, "buy", sig.contracts, sig.price_cents,
                 sig.fee_cents, mode,
                 f"model p={sig.model_prob:.2f} edge={sig.edge:.2f} fcst={sig.forecast_high:.0f}F"),
            )

    def settle_position(self, ticker: str, side: str, result: str, mode: str):
        """Pay out $1/contract if the side won, close the position."""
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM positions WHERE ticker=? AND side=? AND status='open'",
                (ticker, side),
            ).fetchone()
            if not row:
                return
            won = side == result
            payout = row["qty"] * 100 if won else 0
            pnl = payout - round(row["qty"] * row["avg_price_cents"])
            self._set_cash(self.cash_cents() + payout)
            self._conn.execute(
                "UPDATE positions SET status='settled', result=?, pnl_cents=? WHERE id=?",
                (result, pnl, row["id"]),
            )
            self._conn.execute(
                "INSERT INTO trades (at, ticker, side, action, qty, price_cents, fee_cents, mode, note)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (_now(), ticker, side, "settle", row["qty"], 100 if won else 0, 0, mode,
                 f"settled {result.upper()}, pnl {pnl / 100:+.2f}"),
            )

    # ---- reads ----

    def open_positions(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM positions WHERE status='open' ORDER BY event_date, ticker"
        ).fetchall()
        return [dict(r) for r in rows]

    def settled_positions(self, limit: int = 50) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM positions WHERE status='settled' ORDER BY event_date DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def trades(self, limit: int = 100) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def record_equity(self, positions_value_cents: int):
        with self._lock, self._conn:
            cash = self.cash_cents()
            self._conn.execute(
                "INSERT INTO equity (at, cash_cents, positions_cents, total_cents)"
                " VALUES (?,?,?,?)",
                (_now(), cash, positions_value_cents, cash + positions_value_cents),
            )

    def equity_curve(self, limit: int = 500) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM (SELECT * FROM equity ORDER BY id DESC LIMIT ?) ORDER BY id",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def traded_keys(self) -> set:
        """Every (ticker, side) ever traded — open or settled."""
        rows = self._conn.execute("SELECT DISTINCT ticker, side FROM positions").fetchall()
        return {(r["ticker"], r["side"]) for r in rows}

    def reset(self, starting_bankroll_cents: int):
        with self._lock, self._conn:
            for table in ("positions", "trades", "equity"):
                self._conn.execute(f"DELETE FROM {table}")
            self._conn.execute(
                "UPDATE meta SET value=? WHERE key='cash_cents'", (str(starting_bankroll_cents),)
            )
            self._conn.execute(
                "UPDATE meta SET value=? WHERE key='starting_cents'", (str(starting_bankroll_cents),)
            )

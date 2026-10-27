"""SQLite persistence. Every row carries mode; PAPER and LIVE never share
balances or positions. decisions and events are append-only, enforced by
triggers."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .models import EngineState, Mode, OrderState, PositionView, base_of, iso, parse_iso, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL,
    created_at TEXT NOT NULL,
    ended_at TEXT,
    starting_capital REAL NOT NULL,
    target_roi REAL NOT NULL,
    target_equity REAL NOT NULL,
    pairs TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'STOPPED',
    state_reason TEXT,
    peak_equity REAL NOT NULL,
    day_start TEXT,
    day_start_equity REAL,
    running_seconds REAL NOT NULL DEFAULT 0,
    start_prices TEXT NOT NULL DEFAULT '{}',
    last_decision_candle TEXT,
    consecutive_invalid INTEGER NOT NULL DEFAULT 0,
    last_summary_day TEXT
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL,
    run_id INTEGER,
    ts TEXT NOT NULL,
    candle_ts TEXT,
    snapshot_hash TEXT,
    snapshot TEXT,
    prompt_version TEXT,
    model TEXT,
    model_called INTEGER NOT NULL DEFAULT 0,
    cache_hit INTEGER NOT NULL DEFAULT 0,
    raw_output TEXT,
    valid INTEGER NOT NULL,
    validation_errors TEXT,
    actions TEXT,
    usage TEXT
);
CREATE TABLE IF NOT EXISTS risk_verdicts (
    id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL,
    run_id INTEGER,
    decision_id INTEGER NOT NULL,
    ts TEXT NOT NULL,
    action_index INTEGER NOT NULL,
    action TEXT NOT NULL,
    pair TEXT,
    verdict TEXT NOT NULL,
    reasons TEXT NOT NULL,
    size_requested REAL,
    size_final REAL,
    base_amount REAL,
    confidence REAL,
    rationale TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL,
    run_id INTEGER,
    decision_id INTEGER,
    position_id INTEGER,
    client_id TEXT NOT NULL UNIQUE,
    exchange_id TEXT,
    pair TEXT NOT NULL,
    side TEXT NOT NULL,
    type TEXT NOT NULL,
    purpose TEXT NOT NULL,
    amount REAL NOT NULL,
    price REAL,
    status TEXT NOT NULL,
    filled REAL NOT NULL DEFAULT 0,
    average REAL,
    fee REAL NOT NULL DEFAULT 0,
    error TEXT,
    meta TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL,
    run_id INTEGER,
    order_id INTEGER NOT NULL,
    pair TEXT NOT NULL,
    side TEXT NOT NULL,
    amount REAL NOT NULL,
    price REAL NOT NULL,
    cost REAL NOT NULL,
    fee REAL NOT NULL,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL,
    run_id INTEGER,
    pair TEXT NOT NULL,
    status TEXT NOT NULL,
    amount REAL NOT NULL,
    amount_open REAL NOT NULL,
    entry_price REAL NOT NULL,
    cost_eur REAL NOT NULL,
    entry_fee REAL NOT NULL,
    stop_loss REAL NOT NULL,
    take_profit REAL NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    exit_price REAL,
    proceeds_eur REAL,
    exit_fee REAL,
    realised_pnl REAL,
    exit_reason TEXT,
    decision_id INTEGER,
    prompt_version TEXT,
    model TEXT
);
CREATE TABLE IF NOT EXISTS equity_snapshots (
    id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL,
    run_id INTEGER,
    ts TEXT NOT NULL,
    equity REAL NOT NULL,
    cash REAL NOT NULL,
    positions_value REAL NOT NULL,
    peak REAL NOT NULL,
    drawdown_pct REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL,
    run_id INTEGER,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    data TEXT
);
CREATE TABLE IF NOT EXISTS settings_versions (
    id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL,
    ts TEXT NOT NULL,
    reason TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    model TEXT NOT NULL,
    config TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS model_cache (
    key TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    ts TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    model TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    output TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_decisions_mode ON decisions(mode, id);
CREATE INDEX IF NOT EXISTS ix_positions_mode ON positions(mode, run_id, status);
CREATE INDEX IF NOT EXISTS ix_fills_run ON fills(mode, run_id);
CREATE INDEX IF NOT EXISTS ix_events_mode ON events(mode, id);
CREATE TRIGGER IF NOT EXISTS decisions_no_update BEFORE UPDATE ON decisions
    BEGIN SELECT RAISE(ABORT, 'decisions is append-only'); END;
CREATE TRIGGER IF NOT EXISTS decisions_no_delete BEFORE DELETE ON decisions
    BEGIN SELECT RAISE(ABORT, 'decisions is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
    BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
    BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;
"""


def _j(x: Any) -> str:
    return json.dumps(x, sort_keys=True, default=str)


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # --- low level --------------------------------------------------------------

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.db.execute(sql, args)

    def _insert(self, table: str, row: dict) -> int:
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        return self._exec(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(row.values())).lastrowid

    def _update(self, table: str, id_: int, fields: dict) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._exec(f"UPDATE {table} SET {sets} WHERE id = ?", (*fields.values(), id_))

    def rows(self, sql: str, args: tuple = ()) -> list[dict]:
        return [dict(r) for r in self._exec(sql, args).fetchall()]

    def one(self, sql: str, args: tuple = ()) -> dict | None:
        r = self._exec(sql, args).fetchone()
        return dict(r) if r else None

    # --- runs -------------------------------------------------------------------

    def create_run(self, mode: Mode, capital: float, target_roi: float, pairs: list[str],
                   timeframe: str, start_prices: dict[str, float], now: datetime) -> int:
        with self._lock:
            self._exec("UPDATE runs SET ended_at = ? WHERE mode = ? AND ended_at IS NULL", (iso(now), mode.value))
            return self._insert("runs", {
                "mode": mode.value, "created_at": iso(now), "starting_capital": capital,
                "target_roi": target_roi, "target_equity": capital * (1 + target_roi),
                "pairs": _j(pairs), "timeframe": timeframe, "state": EngineState.STOPPED.value,
                "peak_equity": capital, "start_prices": _j(start_prices),
            })

    def current_run(self, mode: Mode) -> dict | None:
        r = self.one("SELECT * FROM runs WHERE mode = ? AND ended_at IS NULL ORDER BY id DESC LIMIT 1", (mode.value,))
        if r:
            r["pairs"] = json.loads(r["pairs"])
            r["start_prices"] = json.loads(r["start_prices"])
        return r

    def update_run(self, run_id: int, **fields) -> None:
        self._update("runs", run_id, fields)

    def add_running_seconds(self, run_id: int, seconds: float) -> None:
        self._exec("UPDATE runs SET running_seconds = running_seconds + ? WHERE id = ?", (seconds, run_id))

    def total_running_seconds(self, mode: Mode) -> float:
        r = self.one("SELECT COALESCE(SUM(running_seconds), 0) AS s FROM runs WHERE mode = ?", (mode.value,))
        return float(r["s"])

    # --- events -----------------------------------------------------------------

    def event(self, mode: Mode, run_id: int | None, kind: str, message: str, data: Any = None,
              now: datetime | None = None) -> int:
        return self._insert("events", {
            "mode": mode.value, "run_id": run_id, "ts": iso(now or utcnow()),
            "kind": kind, "message": message, "data": _j(data) if data is not None else None,
        })

    # --- decisions --------------------------------------------------------------

    def insert_decision(self, mode: Mode, run_id: int, **row) -> int:
        for k in ("snapshot", "validation_errors", "actions", "usage"):
            if k in row and not isinstance(row[k], str) and row[k] is not None:
                row[k] = _j(row[k])
        return self._insert("decisions", {"mode": mode.value, "run_id": run_id, **row})

    def insert_verdict(self, mode: Mode, run_id: int, decision_id: int, now: datetime, v) -> int:
        a = v.action
        return self._insert("risk_verdicts", {
            "mode": mode.value, "run_id": run_id, "decision_id": decision_id, "ts": iso(now),
            "action_index": v.index, "action": a.action, "pair": a.pair, "verdict": v.verdict,
            "reasons": _j(v.reasons), "size_requested": a.size_eur, "size_final": v.size_eur,
            "base_amount": v.base_amount, "confidence": a.confidence, "rationale": a.rationale,
        })

    def model_calls_since(self, mode: Mode, since: datetime) -> int:
        r = self.one("SELECT COUNT(*) AS n FROM decisions WHERE mode = ? AND model_called = 1 AND ts >= ?",
                     (mode.value, iso(since)))
        return int(r["n"])

    # --- model cache ------------------------------------------------------------

    def cache_get(self, key: str) -> str | None:
        r = self.one("SELECT output FROM model_cache WHERE key = ?", (key,))
        return r["output"] if r else None

    def cache_put(self, key: str, mode: Mode, prompt_version: str, model: str, snapshot_hash: str, output: str) -> None:
        self._exec(
            "INSERT OR REPLACE INTO model_cache (key, mode, ts, prompt_version, model, snapshot_hash, output) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (key, mode.value, iso(utcnow()), prompt_version, model, snapshot_hash, output),
        )

    # --- orders and fills -------------------------------------------------------

    def insert_order(self, mode: Mode, run_id: int, *, client_id: str, pair: str, side: str, type_: str,
                     purpose: str, amount: float, price: float | None, decision_id: int | None,
                     position_id: int | None, now: datetime, meta: dict | None = None) -> int:
        return self._insert("orders", {
            "mode": mode.value, "run_id": run_id, "decision_id": decision_id, "position_id": position_id,
            "client_id": client_id, "pair": pair, "side": side, "type": type_, "purpose": purpose,
            "amount": amount, "price": price, "status": "new", "meta": _j(meta or {}),
            "created_at": iso(now), "updated_at": iso(now),
        })

    def order_row(self, order_row_id: int) -> dict:
        return self.one("SELECT * FROM orders WHERE id = ?", (order_row_id,))

    def mark_order(self, order_row_id: int, status: str, error: str | None, now: datetime) -> None:
        self._update("orders", order_row_id, {"status": status, "error": error, "updated_at": iso(now)})

    def update_order(self, order_row_id: int, o: OrderState, now: datetime) -> None:
        self._update("orders", order_row_id, {
            "exchange_id": o.id, "status": o.status, "filled": o.filled, "average": o.average,
            "fee": o.fee_eur, "error": o.error, "updated_at": iso(now),
        })

    def unsettled_orders(self, mode: Mode, run_id: int) -> list[dict]:
        return self.rows("SELECT * FROM orders WHERE mode = ? AND run_id = ? AND status IN ('new', 'open')",
                         (mode.value, run_id))

    def insert_fill(self, mode: Mode, run_id: int, order_row_id: int, o: OrderState, now: datetime) -> int:
        return self._insert("fills", {
            "mode": mode.value, "run_id": run_id, "order_id": order_row_id, "pair": o.pair, "side": o.side,
            "amount": o.filled, "price": o.average, "cost": o.filled * (o.average or 0), "fee": o.fee_eur,
            "ts": iso(now),
        })

    def ledger(self, mode: Mode, run_id: int, starting_capital: float) -> tuple[float, dict[str, float]]:
        """EUR cash and per-pair base holdings the app owns in this run, from fills."""
        cash = starting_capital
        holdings: dict[str, float] = {}
        for f in self.rows("SELECT side, pair, amount, cost, fee FROM fills WHERE mode = ? AND run_id = ?",
                           (mode.value, run_id)):
            sign = 1 if f["side"] == "buy" else -1
            cash -= sign * f["cost"] + f["fee"]
            holdings[f["pair"]] = holdings.get(f["pair"], 0.0) + sign * f["amount"]
        return cash, {p: a for p, a in holdings.items() if a > 1e-12}

    def total_fees(self, mode: Mode, run_id: int) -> float:
        r = self.one("SELECT COALESCE(SUM(fee), 0) AS f FROM fills WHERE mode = ? AND run_id = ?", (mode.value, run_id))
        return float(r["f"])

    # --- positions --------------------------------------------------------------

    def open_position(self, mode: Mode, run_id: int, *, pair: str, amount: float, entry_price: float,
                      cost_eur: float, entry_fee: float, stop_loss: float, take_profit: float,
                      decision_id: int | None, prompt_version: str | None, model: str | None,
                      now: datetime) -> int:
        return self._insert("positions", {
            "mode": mode.value, "run_id": run_id, "pair": pair, "status": "open", "amount": amount,
            "amount_open": amount, "entry_price": entry_price, "cost_eur": cost_eur, "entry_fee": entry_fee,
            "stop_loss": stop_loss, "take_profit": take_profit, "opened_at": iso(now),
            "decision_id": decision_id, "prompt_version": prompt_version, "model": model,
        })

    def position(self, position_id: int) -> dict | None:
        return self.one("SELECT * FROM positions WHERE id = ?", (position_id,))

    def open_positions(self, mode: Mode, run_id: int) -> list[PositionView]:
        out = []
        for r in self.rows("SELECT * FROM positions WHERE mode = ? AND run_id = ? AND status = 'open' ORDER BY id",
                           (mode.value, run_id)):
            share = r["amount_open"] / r["amount"] if r["amount"] else 0.0
            out.append(PositionView(r["id"], r["pair"], r["amount_open"], r["entry_price"], r["cost_eur"] * share,
                                    r["stop_loss"], r["take_profit"], parse_iso(r["opened_at"]), r))
        return out

    def update_stop(self, position_id: int, new_stop: float) -> None:
        self._update("positions", position_id, {"stop_loss": new_stop})

    def reduce_position(self, position_id: int, sold: float) -> float:
        self._exec("UPDATE positions SET amount_open = MAX(amount_open - ?, 0) WHERE id = ?", (sold, position_id))
        return float(self.position(position_id)["amount_open"])

    def close_position(self, position_id: int, *, reason: str, now: datetime) -> None:
        """Close using every exit fill recorded against the position. Unsellable
        dust left behind counts as lost in realised P&L."""
        p = self.position(position_id)
        f = self.one(
            "SELECT COALESCE(SUM(f.amount), 0) AS amt, COALESCE(SUM(f.cost), 0) AS cost, "
            "COALESCE(SUM(f.fee), 0) AS fee FROM fills f JOIN orders o ON o.id = f.order_id "
            "WHERE o.position_id = ? AND f.side = 'sell'", (position_id,))
        proceeds = f["cost"] - f["fee"]
        self._update("positions", position_id, {
            "status": "closed", "closed_at": iso(now),
            "exit_price": f["cost"] / f["amt"] if f["amt"] else None,
            "proceeds_eur": proceeds, "exit_fee": f["fee"], "realised_pnl": proceeds - p["cost_eur"],
            "exit_reason": reason,
        })

    def entries_since(self, mode: Mode, run_id: int, since: datetime) -> int:
        r = self.one("SELECT COUNT(*) AS n FROM positions WHERE mode = ? AND run_id = ? AND opened_at >= ?",
                     (mode.value, run_id, iso(since)))
        return int(r["n"])

    def cooldowns(self, mode: Mode, run_id: int, now: datetime, minutes: int) -> dict[str, datetime]:
        since = now - timedelta(minutes=minutes)
        out: dict[str, datetime] = {}
        for r in self.rows("SELECT pair, closed_at FROM positions WHERE mode = ? AND run_id = ? "
                           "AND exit_reason = 'stop_loss' AND closed_at >= ?", (mode.value, run_id, iso(since))):
            until = parse_iso(r["closed_at"]) + timedelta(minutes=minutes)
            out[r["pair"]] = max(out.get(r["pair"], until), until)
        return out

    def closed_trades(self, mode: Mode, run_id: int | None = None) -> list[dict]:
        if run_id is None:
            return self.rows("SELECT * FROM positions WHERE mode = ? AND status = 'closed' ORDER BY closed_at",
                             (mode.value,))
        return self.rows("SELECT * FROM positions WHERE mode = ? AND run_id = ? AND status = 'closed' "
                         "ORDER BY closed_at", (mode.value, run_id))

    # --- equity -----------------------------------------------------------------

    def snapshot_equity(self, mode: Mode, run_id: int, now: datetime, equity: float, cash: float,
                        peak: float) -> None:
        dd = 0.0 if peak <= 0 else max(0.0, (peak - equity) / peak * 100)
        self._insert("equity_snapshots", {
            "mode": mode.value, "run_id": run_id, "ts": iso(now), "equity": equity, "cash": cash,
            "positions_value": equity - cash, "peak": peak, "drawdown_pct": dd,
        })

    # --- settings ---------------------------------------------------------------

    def record_settings(self, mode: Mode, cfg_dump: dict, reason: str, now: datetime | None = None) -> None:
        last = self.one("SELECT config FROM settings_versions ORDER BY id DESC LIMIT 1")
        body = _j(cfg_dump)
        if last and last["config"] == body and reason == "startup":
            return
        self._insert("settings_versions", {
            "mode": mode.value, "ts": iso(now or utcnow()), "reason": reason,
            "prompt_version": cfg_dump.get("prompt_version", ""), "model": cfg_dump.get("model", ""),
            "config": body,
        })

    # --- paper reset ------------------------------------------------------------

    def reset_paper(self) -> None:
        """Clear PAPER balances, positions, orders, fills, equity and runs.
        Decisions, risk verdicts and events are audit and are kept."""
        with self._lock:
            for t in ("fills", "orders", "positions", "equity_snapshots", "runs"):
                self._exec(f"DELETE FROM {t} WHERE mode = ?", (Mode.PAPER.value,))


def holdings_by_asset(holdings: dict[str, float]) -> dict[str, float]:
    out: dict[str, float] = {}
    for pair, amt in holdings.items():
        out[base_of(pair)] = out.get(base_of(pair), 0.0) + amt
    return out

"""Live gate. LIVE is enabled only when every condition holds. The gate is set
in config with floors in code; the UI cannot skip it."""

from __future__ import annotations

from dataclasses import dataclass

from .config import LIVE_CONFIRMATION_PHRASE, Config
from .models import Mode
from .state import Store


def paper_stats(store: Store) -> dict:
    trades = store.closed_trades(Mode.PAPER)
    seconds = store.total_running_seconds(Mode.PAPER)
    return {
        "paper_days": seconds / 86400,
        "paper_trades": len(trades),
        "paper_net_pnl_eur": round(sum(t["realised_pnl"] or 0 for t in trades), 2),
        "paper_fees_eur": round(sum((t["entry_fee"] or 0) + (t["exit_fee"] or 0) for t in trades), 2),
    }


@dataclass
class GateRequest:
    phrase: str = ""
    trade_only_key_confirmed: bool = False
    acknowledged_paper_pnl_eur: float | None = None  # the figure the screen showed


def gate_conditions(cfg: Config, store: Store, req: GateRequest | None = None,
                    balance_ok: bool | None = None, balance_error: str | None = None) -> dict:
    s = paper_stats(store)
    req = req or GateRequest()
    shown = req.acknowledged_paper_pnl_eur
    conds = [
        {"key": "paper_days", "ok": s["paper_days"] >= cfg.live_gate.paper_days,
         "detail": f"{s['paper_days']:.2f} of {cfg.live_gate.paper_days:g} days of PAPER run time"},
        {"key": "paper_trades", "ok": s["paper_trades"] >= cfg.live_gate.paper_trades,
         "detail": f"{s['paper_trades']} of {cfg.live_gate.paper_trades} closed PAPER trades"},
        {"key": "paper_pnl_shown", "ok": shown is not None and abs(shown - s["paper_net_pnl_eur"]) < 0.005,
         "detail": f"paper net P&L after fees: {s['paper_net_pnl_eur']:+.2f} EUR (SIMULATED)"},
        {"key": "phrase", "ok": req.phrase == LIVE_CONFIRMATION_PHRASE,
         "detail": f"type exactly: {LIVE_CONFIRMATION_PHRASE}"},
        {"key": "balance_query", "ok": bool(balance_ok),
         "detail": "live balance query succeeded" if balance_ok else (balance_error or "live balance query not run")},
        {"key": "trade_only_key", "ok": req.trade_only_key_confirmed,
         "detail": "API key confirmed as trade-only (no fund-movement permission)"},
    ]
    return {"ok": all(c["ok"] for c in conds), "conditions": conds, **s}

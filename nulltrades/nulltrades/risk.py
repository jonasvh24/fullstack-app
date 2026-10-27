"""Risk engine. Pure functions, no I/O.

Every model action comes back approved, clamped (with reason) or rejected (with
reason). Actions are processed in order; each approved open reduces cash and
raises the open-position and entry counts before the next action is checked.
Exits are never blocked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from .config import RiskConfig
from .models import MarketInfo, PositionView, Quote, ceil_to_step, floor_to_step

APPROVED, CLAMPED, REJECTED = "approved", "clamped", "rejected"
EPS = 1e-9


@dataclass(frozen=True)
class Action:
    """A schema-validated model action. Built by decision.validate_output."""

    action: Literal["open_long", "close_position", "update_stop", "hold"]
    pair: str | None = None
    size_eur: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    new_stop: float | None = None
    confidence: float | None = None  # logged only, never used for sizing
    rationale: str = ""


@dataclass
class RiskContext:
    now: datetime
    engine_running: bool
    kill_active: bool
    whitelist: frozenset[str]
    markets: dict[str, MarketInfo]
    quotes: dict[str, Quote]
    equity: float
    starting_capital: float
    available_cash: float
    open_positions: dict[str, PositionView]
    entries_today: int
    loss_today: float
    cooldown_until: dict[str, datetime]
    limits: RiskConfig


@dataclass
class Verdict:
    index: int
    action: Action
    verdict: str
    reasons: list[str] = field(default_factory=list)
    size_eur: float | None = None  # final notional at the reference price
    base_amount: float | None = None  # rounded down to market precision
    limit_price: float | None = None
    new_stop: float | None = None
    target_size_eur: float | None = None  # size before precision rounding

    @property
    def executable(self) -> bool:
        return self.verdict in (APPROVED, CLAMPED) and self.action.action != "hold"


def entry_cost_factor(limits: RiskConfig) -> float:
    """EUR spent per EUR of notional, worst case: limit slippage plus taker fee."""
    return (1 + limits.max_slippage_bps / 10_000) * (1 + limits.fee_taker_pct / 100)


def evaluate_actions(actions: list[Action], ctx: RiskContext) -> list[Verdict]:
    cash = ctx.available_cash
    open_count = len(ctx.open_positions)
    entries = ctx.entries_today
    opened: set[str] = set()
    closing: set[str] = set()
    out: list[Verdict] = []

    for i, a in enumerate(actions):
        if a.action == "hold":
            out.append(Verdict(i, a, APPROVED))
        elif a.action == "close_position":
            if a.pair in ctx.open_positions and a.pair not in closing:
                closing.add(a.pair)
                out.append(Verdict(i, a, APPROVED, ["exit always allowed"]))
            else:
                out.append(Verdict(i, a, REJECTED, ["no open position on pair"]))
        elif a.action == "update_stop":
            if a.pair in closing:
                out.append(Verdict(i, a, REJECTED, ["position is being closed"]))
            else:
                out.append(evaluate_update_stop(i, a, ctx))
        else:
            v = evaluate_open_long(i, a, ctx, cash, open_count, entries, opened)
            if v.verdict != REJECTED:
                cash -= (v.size_eur or 0) * entry_cost_factor(ctx.limits)
                open_count += 1
                entries += 1
                opened.add(a.pair)
            out.append(v)
    return out


def evaluate_open_long(
    i: int, a: Action, ctx: RiskContext, cash: float, open_count: int, entries: int, opened: set[str]
) -> Verdict:
    L = ctx.limits
    reasons: list[str] = []
    pair = a.pair or ""
    m = ctx.markets.get(pair)
    q = ctx.quotes.get(pair)

    # 13. engine state
    if not ctx.engine_running or ctx.kill_active:
        reasons.append("engine not running")
    # 1. whitelist and active market
    if pair not in ctx.whitelist:
        reasons.append("pair not on whitelist")
    elif m is None or not m.active:
        reasons.append("market not active")
    if q is None:
        reasons.append("no current quote")
    if pair in ctx.open_positions or pair in opened:
        reasons.append("position already open on pair")
    # 3. open positions
    if open_count >= L.max_open_positions:
        reasons.append(f"max open positions reached ({open_count}/{L.max_open_positions})")
    # 4. entries today
    if entries >= L.max_trades_per_day:
        reasons.append(f"max trades per day reached ({entries}/{L.max_trades_per_day})")
    # 11. cooldown
    until = ctx.cooldown_until.get(pair)
    if until is not None and ctx.now < until:
        reasons.append(f"pair in cooldown until {until.isoformat()}")
    # 12. daily loss budget
    budget = L.max_daily_loss_pct / 100 * ctx.starting_capital
    if budget - ctx.loss_today <= EPS:
        reasons.append("daily loss budget exhausted")
    # 2. cash, no leverage
    if cash <= EPS:
        reasons.append("no cash available")

    if q is None or m is None or a.stop_loss is None or a.take_profit is None or a.size_eur is None:
        if a.size_eur is None or a.stop_loss is None or a.take_profit is None:
            reasons.append("missing size, stop or take-profit")
        return Verdict(i, a, REJECTED, reasons)

    price = q.ask  # what a buy pays now
    stop_dist = (price - a.stop_loss) / price
    tp_dist = (a.take_profit - price) / price

    # 5. stop distance
    if a.stop_loss >= price:
        reasons.append("stop not below current price")
    elif stop_dist < L.min_stop_pct / 100 - EPS:
        reasons.append(f"stop distance {stop_dist * 100:.2f}% below minimum {L.min_stop_pct:.2f}%")
    elif stop_dist > L.max_stop_pct / 100 + EPS:
        reasons.append(f"stop distance {stop_dist * 100:.2f}% above maximum {L.max_stop_pct:.2f}%")
    # 6. take-profit distance
    if a.take_profit <= price:
        reasons.append("take-profit not above current price")
    elif tp_dist > L.max_take_profit_pct / 100 + EPS:
        reasons.append(f"take-profit distance {tp_dist * 100:.2f}% above maximum {L.max_take_profit_pct:.2f}%")
    # 7. reward to risk
    if stop_dist > 0 and tp_dist > 0:
        rr = tp_dist / stop_dist
        if rr < L.min_reward_risk - EPS:
            reasons.append(f"reward-to-risk {rr:.2f} below minimum {L.min_reward_risk:.2f}")
    # 8. take-profit must clear costs
    round_trip = 2 * L.fee_taker_pct / 100
    floor_tp = L.cost_multiple * round_trip
    if tp_dist > 0 and tp_dist < floor_tp - EPS:
        reasons.append(f"take-profit distance {tp_dist * 100:.2f}% below {L.cost_multiple:g}x round-trip fees ({floor_tp * 100:.2f}%)")

    if reasons:
        return Verdict(i, a, REJECTED, reasons)

    # 9. sizing: smallest of risk-based, position cap, cash, and what was asked.
    risk_size = ctx.equity * L.risk_per_trade_pct / 100 / stop_dist
    cap_size = ctx.equity * L.max_position_pct / 100
    cash_size = cash / entry_cost_factor(L)
    candidates = {
        "requested": a.size_eur,
        "risk-based size": risk_size,
        f"{L.max_position_pct:g}% position cap": cap_size,
        "available cash": cash_size,
    }
    binding = min(candidates, key=lambda k: candidates[k])
    size = candidates[binding]
    clamps: list[str] = []
    if size < a.size_eur - EPS:
        clamps.append(f"size clamped from {a.size_eur:.2f} to {size:.2f} EUR by {binding}")

    # 10. precision and exchange minimum. Round down, never up.
    limit_price = floor_to_step(price * (1 + L.max_slippage_bps / 10_000), m.price_step)
    if limit_price < price:  # step coarser than the slippage band
        limit_price = ceil_to_step(price, m.price_step)
    base = floor_to_step(size / limit_price, m.amount_step)
    if base <= 0 or base < m.min_amount - EPS or base * price < m.min_cost - EPS:
        detail = f"{base:g} {pair.split('/')[0]} (~{base * price:.2f} EUR) vs minimum {m.min_amount:g} / {m.min_cost:g} EUR"
        return Verdict(i, a, REJECTED, clamps + [f"below minimum: {detail}"])

    # Rounding the amount down to precision is not a clamp of the request.
    return Verdict(
        i, a, CLAMPED if clamps else APPROVED, clamps,
        size_eur=base * price, base_amount=base, limit_price=limit_price, target_size_eur=size,
    )


def evaluate_update_stop(i: int, a: Action, ctx: RiskContext) -> Verdict:
    pos = ctx.open_positions.get(a.pair or "")
    if pos is None:
        return Verdict(i, a, REJECTED, ["no open position on pair"])
    if a.new_stop is None:
        return Verdict(i, a, REJECTED, ["missing new_stop"])
    q = ctx.quotes.get(pos.pair)
    if q is None:
        return Verdict(i, a, REJECTED, ["no current quote"])
    m = ctx.markets.get(pos.pair)
    new_stop = floor_to_step(a.new_stop, m.price_step) if m else a.new_stop
    reasons = []
    if new_stop <= pos.stop_loss + EPS:
        reasons.append(f"stop may only move up (current {pos.stop_loss:g}, proposed {new_stop:g})")
    if new_stop >= q.last - EPS:
        reasons.append(f"stop {new_stop:g} not below last price {q.last:g}")
    if reasons:
        return Verdict(i, a, REJECTED, reasons)
    return Verdict(i, a, APPROVED, [], new_stop=new_stop)


def exit_reason(pos: PositionView, bid: float, now: datetime, limits: RiskConfig) -> str | None:
    """Engine-side exits, checked on every poll without a model call."""
    if bid <= pos.stop_loss:
        return "stop_loss"
    if bid >= pos.take_profit:
        return "take_profit"
    if pos.age_hours(now) >= limits.max_hold_hours:
        return "max_hold"
    return None

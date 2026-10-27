from __future__ import annotations

import pytest

from nulltrades.config import RiskConfig
from nulltrades.risk import (
    APPROVED, CLAMPED, REJECTED, Action, evaluate_actions, exit_reason,
)

from .conftest import NOW, ctx, later, market, open_long, position, quote


def one(action, **over):
    [v] = evaluate_actions([action], ctx(**over))
    return v


# --- worked example (brief section 6) ----------------------------------------

def test_worked_example_size_is_8():
    # equity 20, risk 2% (0.40), stop 5% below entry: risk-based size 8, cap 10.
    v = one(open_long(size=8.0, stop=95.0, tp=110.0))
    assert v.verdict == APPROVED
    assert v.target_size_eur == pytest.approx(8.0)
    assert v.size_eur <= 8.0
    assert v.base_amount == pytest.approx(0.07992008)


def test_worked_example_larger_request_is_clamped_to_8():
    v = one(open_long(size=15.0))
    assert v.verdict == CLAMPED
    assert v.target_size_eur == pytest.approx(8.0)
    assert "risk-based size" in v.reasons[0]


# --- every open_long rule: table-driven rejections ---------------------------

REJECTIONS = [
    ("1 not on whitelist", open_long(pair="XRP/EUR"), {}, "not on whitelist"),
    ("1 market inactive", open_long(), {"markets": {"BTC/EUR": market(active=False)}}, "market not active"),
    ("2 no cash", open_long(), {"available_cash": 0.0}, "no cash"),
    ("3 max open positions", open_long(), {"open_positions": {
        "ETH/EUR": position("ETH/EUR"), "SOL/EUR": position("SOL/EUR", id=2)}}, "max open positions"),
    ("4 max trades per day", open_long(), {"entries_today": 6}, "max trades per day"),
    ("5 stop too tight", open_long(stop=99.5, tp=110), {}, "below minimum 1.00%"),
    ("5 stop too wide", open_long(stop=90.0, tp=110), {}, "above maximum 5.00%"),
    ("5 stop above price", open_long(stop=101.0, tp=110), {}, "stop not below"),
    ("6 take-profit too far", open_long(stop=95, tp=111), {}, "above maximum 10.00%"),
    ("6 take-profit below price", open_long(stop=95, tp=99), {}, "take-profit not above"),
    ("7 reward-to-risk", open_long(stop=95, tp=107), {}, "reward-to-risk 1.40"),
    ("8 cost multiple", open_long(stop=99, tp=102), {}, "round-trip fees"),
    ("11 cooldown", open_long(), {"cooldown_until": {"BTC/EUR": later(minutes=30)}}, "cooldown"),
    ("12 daily loss budget", open_long(), {"loss_today": 2.0}, "daily loss budget"),
    ("13 engine stopped", open_long(), {"engine_running": False}, "engine not running"),
    ("13 kill active", open_long(), {"kill_active": True}, "engine not running"),
    ("one position per pair", open_long(), {"open_positions": {"BTC/EUR": position()}}, "already open"),
]


@pytest.mark.parametrize("name,action,over,reason", REJECTIONS, ids=[r[0] for r in REJECTIONS])
def test_open_long_rejections(name, action, over, reason):
    v = one(action, **over)
    assert v.verdict == REJECTED
    assert any(reason in r for r in v.reasons), v.reasons
    assert v.base_amount is None


def test_cooldown_expired_allows_entry():
    v = one(open_long(), cooldown_until={"BTC/EUR": NOW})
    assert v.verdict == APPROVED


def test_round_trip_fee_floor_default_is_2_4_pct():
    # 3 x (2 x 0.40%) = 2.40%. 2.5% passes, 2.3% fails.
    assert one(open_long(stop=98.5, tp=102.5)).verdict == APPROVED
    assert one(open_long(stop=98.5, tp=102.3)).verdict == REJECTED


# --- clamping -----------------------------------------------------------------

@pytest.mark.parametrize("over,size,binding,expected", [
    ({}, 15.0, "risk-based size", 8.0),
    ({}, 50.0, "50% position cap", 10.0),  # stop 1%: risk size 40, cap 10
    ({"available_cash": 5.0}, 8.0, "available cash", 5.0 / (1.0015 * 1.004)),
])
def test_clamps_down_only(over, size, binding, expected):
    stop = 99.0 if binding == "50% position cap" else 95.0
    v = one(open_long(size=size, stop=stop, tp=103.0 if stop == 99.0 else 110.0), **over)
    assert v.verdict == CLAMPED
    assert binding in v.reasons[0]
    assert v.target_size_eur == pytest.approx(expected)


def test_smaller_request_is_honoured_not_raised():
    v = one(open_long(size=3.0))
    assert v.verdict == APPROVED
    assert v.target_size_eur == pytest.approx(3.0)


def test_confidence_never_changes_size():
    a = one(open_long(size=8.0, confidence=0.01))
    b = one(open_long(size=8.0, confidence=0.99))
    assert a.base_amount == b.base_amount


# --- precision and minimums ---------------------------------------------------

def test_below_minimum_rejected_without_round_up():
    # min 0.1 BTC (~10 EUR) but the risk-sized order is ~0.08 BTC.
    v = one(open_long(size=8.0), markets={"BTC/EUR": market(min_amount=0.1)})
    assert v.verdict == REJECTED
    assert any(r.startswith("below minimum") for r in v.reasons)
    assert v.base_amount is None


def test_below_min_cost_rejected():
    v = one(open_long(size=8.0), markets={"BTC/EUR": market(min_cost=9.0)})
    assert v.verdict == REJECTED
    assert any("below minimum" in r for r in v.reasons)


def test_amount_rounds_down_to_precision():
    v = one(open_long(size=8.0), markets={"BTC/EUR": market(amount_step=0.01)})
    assert v.base_amount == pytest.approx(0.07)
    assert v.size_eur == pytest.approx(7.0)


# --- sequential processing ----------------------------------------------------

def test_approved_open_reduces_cash_for_next_action():
    actions = [open_long("BTC/EUR", size=8.0), open_long("ETH/EUR", size=8.0)]
    v1, v2 = evaluate_actions(actions, ctx(available_cash=10.0))
    assert v1.verdict == APPROVED
    assert v2.verdict == CLAMPED and "available cash" in v2.reasons[0]
    assert v2.target_size_eur < 2.0


def test_approved_open_counts_toward_position_limit():
    actions = [open_long("BTC/EUR"), open_long("ETH/EUR")]
    v1, v2 = evaluate_actions(actions, ctx(open_positions={"SOL/EUR": position("SOL/EUR")}))
    assert v1.verdict == APPROVED
    assert v2.verdict == REJECTED and any("max open positions" in r for r in v2.reasons)


def test_approved_open_counts_toward_daily_entries():
    actions = [open_long("BTC/EUR", size=4.0), open_long("ETH/EUR", size=4.0)]
    v1, v2 = evaluate_actions(actions, ctx(entries_today=5))
    assert v1.verdict == APPROVED
    assert v2.verdict == REJECTED and any("max trades per day" in r for r in v2.reasons)


# --- exits are never blocked --------------------------------------------------

@pytest.mark.parametrize("over", [
    {"engine_running": False},
    {"kill_active": True},
    {"loss_today": 50.0},
    {"entries_today": 99},
    {"available_cash": 0.0},
    {"cooldown_until": {"BTC/EUR": later(hours=5)}},
])
def test_close_position_always_allowed(over):
    v = one(Action("close_position", "BTC/EUR"), open_positions={"BTC/EUR": position()}, **over)
    assert v.verdict == APPROVED


def test_close_without_position_rejected():
    assert one(Action("close_position", "BTC/EUR")).verdict == REJECTED


def test_hold_is_approved_noop():
    v = one(Action("hold"))
    assert v.verdict == APPROVED and not v.executable


# --- update_stop --------------------------------------------------------------

@pytest.mark.parametrize("new_stop,verdict,reason", [
    (97.0, APPROVED, None),
    (95.0, REJECTED, "only move up"),
    (93.0, REJECTED, "only move up"),
    (100.0, REJECTED, "not below last price"),
    (101.0, REJECTED, "not below last price"),
])
def test_update_stop(new_stop, verdict, reason):
    v = one(Action("update_stop", "BTC/EUR", new_stop=new_stop),
            open_positions={"BTC/EUR": position(stop=95.0)},
            quotes={"BTC/EUR": quote(bid=100.0, ask=100.1, last=100.0)})
    assert v.verdict == verdict
    if reason:
        assert any(reason in r for r in v.reasons)
    else:
        assert v.new_stop == pytest.approx(97.0)


def test_update_stop_allowed_when_killed():
    v = one(Action("update_stop", "BTC/EUR", new_stop=97.0), kill_active=True,
            open_positions={"BTC/EUR": position(stop=95.0)})
    assert v.verdict == APPROVED


def test_update_stop_without_position_rejected():
    assert one(Action("update_stop", "BTC/EUR", new_stop=97.0)).verdict == REJECTED


# --- engine-side exits --------------------------------------------------------

def test_exit_reasons():
    L = RiskConfig()
    p = position(stop=95.0, tp=110.0)
    assert exit_reason(p, 95.0, NOW, L) == "stop_loss"
    assert exit_reason(p, 110.0, NOW, L) == "take_profit"
    assert exit_reason(p, 100.0, later(hours=72), L) == "max_hold"
    assert exit_reason(p, 100.0, later(hours=71), L) is None

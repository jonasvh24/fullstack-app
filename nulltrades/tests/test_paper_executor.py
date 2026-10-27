from __future__ import annotations

import asyncio

import pytest

from nulltrades.config import RiskConfig
from nulltrades.execution.paper import PaperExecutor
from nulltrades.risk import exit_reason

from .conftest import NOW, position, quote


def run(coro):
    return asyncio.run(coro)


def make(cash=20.0, holdings=None, bid=100.0, ask=100.2):
    box = {"q": quote(bid=bid, ask=ask)}
    ex = PaperExecutor(lambda p: box["q"], cash, holdings, fee_taker_pct=0.40, slippage_bps=10)
    return ex, box


def test_buy_fills_at_ask_plus_slippage_with_taker_fee():
    ex, _ = make()
    o = run(ex.buy_limit("BTC/EUR", 0.05, 101.0, "c1"))
    assert o.status == "closed"
    assert o.average == pytest.approx(100.2 * 1.001)
    cost = 0.05 * 100.2 * 1.001
    assert o.fee_eur == pytest.approx(cost * 0.004)
    bal = run(ex.balances())
    assert bal["EUR"] == pytest.approx(20 - cost * 1.004)
    assert bal["BTC"] == pytest.approx(0.05)


def test_sell_fills_at_bid_minus_slippage_with_taker_fee():
    ex, _ = make(cash=0.0, holdings={"BTC": 0.05})
    o = run(ex.market_sell("BTC/EUR", 0.05, "c1"))
    assert o.average == pytest.approx(100.0 * 0.999)
    proceeds = 0.05 * 100.0 * 0.999
    assert run(ex.balances())["EUR"] == pytest.approx(proceeds * 0.996)


def test_round_trip_costs_fees_and_slippage():
    ex, _ = make(bid=100.0, ask=100.0)
    run(ex.buy_limit("BTC/EUR", 0.1, 101.0, "a"))
    run(ex.market_sell("BTC/EUR", 0.1, "b"))
    eur = run(ex.balances())["EUR"]
    # 0.1% slippage and 0.4% fee each way: about 1% of the 10 EUR notional.
    loss = 20 - eur
    assert loss == pytest.approx(10 * 1.001 * 1.004 - 10 * 0.999 * 0.996, rel=1e-6)
    assert 0.09 < loss < 0.11


def test_limit_not_reached_does_not_fill():
    ex, _ = make()
    o = run(ex.buy_limit("BTC/EUR", 0.05, 100.0, "c1"))  # modelled fill 100.3
    assert o.status == "canceled" and o.filled == 0
    assert run(ex.balances())["EUR"] == 20.0


def test_cash_never_negative():
    ex, _ = make(cash=5.0)
    o = run(ex.buy_limit("BTC/EUR", 0.05, 101.0, "c1"))  # ~5.03 with fee
    assert o.status == "rejected"
    assert run(ex.balances())["EUR"] == 5.0
    # Spend down to near zero, then try again.
    o = run(ex.buy_limit("BTC/EUR", 0.0495, 101.0, "c2"))
    assert o.status == "closed"
    assert run(ex.balances())["EUR"] >= 0
    o = run(ex.buy_limit("BTC/EUR", 0.001, 101.0, "c3"))
    assert o.status == "rejected"
    assert run(ex.balances())["EUR"] >= 0


def test_cannot_sell_more_than_held():
    ex, _ = make(holdings={"BTC": 0.01})
    o = run(ex.market_sell("BTC/EUR", 0.02, "c1"))
    assert o.status == "rejected"
    assert run(ex.balances())["BTC"] == pytest.approx(0.01)


@pytest.mark.parametrize("bids,expected,at", [
    ([100, 99, 97, 95.5, 94.9, 96], "stop_loss", 4),
    ([100, 103, 107, 110.0, 108], "take_profit", 3),
    ([100, 101, 99, 100], None, None),
])
def test_stop_and_take_profit_trigger_on_bid_sequence(bids, expected, at):
    ex, box = make(cash=0.0, holdings={"BTC": 0.05})
    pos = position(stop=95.0, tp=110.0)
    hit = None
    for i, b in enumerate(bids):
        box["q"] = quote(bid=b, ask=b + 0.1)
        reason = exit_reason(pos, b, NOW, RiskConfig())
        if reason:
            hit = (reason, i)
            o = run(ex.market_sell("BTC/EUR", pos.amount, "x"))
            assert o.average == pytest.approx(b * 0.999)
            break
    assert (hit or (None, None)) == (expected, at)

"""Each automatic kill trigger cancels open orders, flattens, stops the loop
and blocks new entries. KILLED persists until reset."""

from __future__ import annotations

import asyncio
import time

import pytest

from nulltrades.engine import Engine, EngineError
from nulltrades.decision import DecisionSource
from nulltrades.models import EngineState, Mode
from nulltrades.notify import Notifier
from nulltrades.risk import evaluate_actions

from .helpers import FakeDecider, MockExchange, make_engine, open_long, tick


def run(coro):
    return asyncio.run(coro)


def mock_factory(**kw):
    return lambda eng: MockExchange(lambda p: eng.quotes.get(p), eng.run["starting_capital"] if eng.run else 20.0,
                                    mode=Mode.PAPER, **kw)


async def opened(tmp_path, outputs=None):
    eng, clock, market, decider = await make_engine(
        tmp_path, outputs=outputs or [open_long()], executor_factory=mock_factory())
    await eng.start(run_loop=False)
    await tick(eng)
    assert len(eng.positions()) == 1
    # A resting order the app placed and knows about, so the kill has something to cancel.
    ex = eng.executor
    ex.rest_limits = True
    o = await ex.buy_limit("ETH/EUR", 0.01, 90.0, "ntresting00000001")
    row = eng.store.insert_order(eng.mode, eng.run_id, client_id=o.client_id, pair="ETH/EUR", side="buy",
                                 type_="limit", purpose="entry", amount=0.01, price=90.0, decision_id=None,
                                 position_id=None, now=clock(), meta={"stop_loss": 85, "take_profit": 99})
    eng.store.update_order(row, o, clock())
    ex.rest_limits = False
    return eng, clock, market, decider


def assert_killed(eng, needle):
    assert eng.state == EngineState.KILLED
    assert needle in eng.state_reason
    assert eng.executor.cancels, "open orders were not cancelled"
    assert eng.positions() == [], "positions were not flattened"
    assert not eng._should_loop()
    assert any("KILLED" in m for m in eng.notifier.sent)
    # New entries are blocked.
    from nulltrades.risk import Action
    [v] = evaluate_actions([Action("open_long", "ETH/EUR", 5.0, 97.0, 105.0)], eng.risk_context(eng.clock()))
    assert v.verdict == "rejected" and "engine not running" in v.reasons


def test_kill_on_daily_loss(tmp_path):
    async def go():
        eng, clock, market, _ = await opened(tmp_path)
        market.set("BTC/EUR", 70.0)  # 8 EUR position loses ~2.4 > 2.0 budget
        clock.advance(seconds=10)
        await tick(eng)
        assert_killed(eng, "daily loss budget exceeded")
    run(go())


def test_kill_on_drawdown(tmp_path):
    async def go():
        eng, clock, market, _ = await opened(tmp_path)
        eng.run["peak_equity"] = 30.0  # earlier gains; today's loss is zero
        eng.run["day_start_equity"] = 19.0
        clock.advance(seconds=10)
        await tick(eng)
        assert_killed(eng, "drawdown")
    run(go())


def test_kill_on_three_consecutive_errors(tmp_path):
    async def go():
        eng, clock, market, _ = await opened(tmp_path)
        market.fail = True
        for i in range(3):
            clock.advance(seconds=10)
            await tick(eng)
            if i < 2:
                assert eng.state == EngineState.RUNNING
        market.fail = False  # flattening needs a price
        assert eng.state == EngineState.KILLED
        assert "3 consecutive exchange or data errors" in eng.state_reason
        assert eng.executor.cancels
    run(go())


def test_kill_on_stale_data(tmp_path):
    async def go():
        eng, clock, market, _ = await opened(tmp_path)
        market.lag_seconds = 121
        clock.advance(seconds=10)
        await tick(eng)
        assert_killed(eng, "older than 120 seconds")
    run(go())


def test_kill_on_reconciliation_mismatch(tmp_path):
    async def go():
        eng, clock, market, _ = await opened(tmp_path)
        eng.executor._bal["EUR"] -= 1.0  # 1 EUR missing, far above 0.5% of equity
        eng._last_reconcile_at = None
        clock.advance(seconds=10)
        await tick(eng)
        assert_killed(eng, "reconciliation mismatch")
    run(go())


def test_kill_on_three_invalid_outputs(tmp_path):
    async def go():
        bad = '{"actions": ['
        eng, clock, market, _ = await opened(tmp_path, outputs=[open_long(), bad, bad, bad])
        for _ in range(3):
            clock.advance(hours=1)
            await tick(eng)
        assert_killed(eng, "3 consecutive invalid model outputs")
    run(go())


def test_valid_output_resets_invalid_streak(tmp_path):
    async def go():
        bad = '{"actions": ['
        hold = {"actions": []}
        eng, clock, market, _ = await opened(tmp_path, outputs=[open_long(), bad, bad, hold, bad, bad])
        for _ in range(5):
            clock.advance(hours=1)
            await tick(eng)
        assert eng.state == EngineState.RUNNING
    run(go())


def test_manual_kill_flattens_within_10_seconds(tmp_path):
    async def go():
        eng, clock, market, _ = await opened(tmp_path)
        t0 = time.monotonic()
        await eng.kill("kill button", manual=True)
        assert time.monotonic() - t0 < 10
        assert_killed(eng, "manual")
    run(go())


def test_killed_persists_across_restart_until_reset(tmp_path):
    async def go():
        eng, clock, market, decider = await opened(tmp_path)
        await eng.kill("test", manual=True)
        eng2 = Engine(eng.cfg, eng.store, market, DecisionSource(eng.store, FakeDecider()), Notifier(),
                      clock=clock)
        assert eng2.state == EngineState.KILLED
        with pytest.raises(EngineError):
            await eng2.start(run_loop=False)
        await eng2.reset()
        assert eng2.state == EngineState.STOPPED
        await eng2.start(run_loop=False)
        assert eng2.state == EngineState.RUNNING
    run(go())


def test_no_flatten_when_disabled(tmp_path):
    async def go():
        eng, clock, market, _ = await opened(tmp_path)
        eng.cfg.flatten_on_kill = False
        await eng.kill("test", manual=True)
        assert len(eng.positions()) == 1
        assert eng.executor.cancels
    run(go())

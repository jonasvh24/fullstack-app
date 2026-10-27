from __future__ import annotations

import asyncio

from nulltrades.models import EngineState, Mode

from .helpers import MockExchange, make_engine, open_long, tick


def run(coro):
    return asyncio.run(coro)


def live_mock(extra_eur=0.0):
    return lambda eng: MockExchange(lambda p: eng.quotes.get(p), 20.0, mode=Mode.LIVE, extra_eur=extra_eur)


def test_live_start_reconciles_before_any_order(tmp_path):
    async def go():
        eng, clock, market, decider = await make_engine(
            tmp_path, mode=Mode.LIVE, outputs=[open_long()], executor_factory=live_mock())
        eng.executor._bal["EUR"] = 10.0  # someone moved 10 EUR out of the account
        await eng.start(run_loop=False)
        assert eng.state == EngineState.KILLED
        assert "reconciliation mismatch" in eng.state_reason
        assert eng.executor.submitted == []
        assert decider.calls == 0
    run(go())


def test_mismatch_mid_run_kills(tmp_path):
    async def go():
        eng, clock, market, decider = await make_engine(
            tmp_path, mode=Mode.LIVE, outputs=[open_long()], executor_factory=live_mock())
        await eng.start(run_loop=False)
        await tick(eng)
        assert len(eng.positions()) == 1
        eng.executor._bal["BTC"] = 0.0  # position vanished on the exchange
        clock.advance(minutes=5)
        await tick(eng)
        assert eng.state == EngineState.KILLED
        assert "BTC" in eng.last_reconcile["issues"][0]
    run(go())


def test_extra_funds_outside_app_are_not_a_mismatch(tmp_path):
    async def go():
        eng, clock, market, decider = await make_engine(
            tmp_path, mode=Mode.LIVE, executor_factory=live_mock(extra_eur=500.0))
        eng.executor._bal["BTC"] = 1.0  # the user's own coins
        await eng.start(run_loop=False)
        await tick(eng)
        assert eng.state == EngineState.RUNNING
        assert eng.last_reconcile["ok"]
    run(go())


def test_small_mismatch_within_tolerance(tmp_path):
    async def go():
        eng, clock, market, decider = await make_engine(tmp_path, mode=Mode.LIVE, executor_factory=live_mock())
        eng.executor._bal["EUR"] -= 0.05  # 0.25% of 20 EUR
        await eng.start(run_loop=False)
        assert eng.state == EngineState.RUNNING
        assert eng.last_reconcile["issues"]
    run(go())


def test_downtime_triggers_reconcile(tmp_path):
    async def go():
        eng, clock, market, decider = await make_engine(tmp_path, mode=Mode.LIVE, executor_factory=live_mock())
        await eng.start(run_loop=False)
        await tick(eng)
        first = eng.last_reconcile["ts"]
        clock.advance(seconds=60)  # gap far above the 10 s poll
        await tick(eng)
        assert eng.last_reconcile["ts"] != first
    run(go())


def test_live_never_deploys_above_cap(tmp_path):
    async def go():
        eng, clock, market, decider = await make_engine(
            tmp_path, mode=Mode.LIVE, executor_factory=live_mock(extra_eur=1000.0))
        await eng.start(run_loop=False)
        await tick(eng)
        eng.cfg.live_capital_cap_eur = 5.0
        assert eng.risk_context(clock()).available_cash <= 5.0
    run(go())

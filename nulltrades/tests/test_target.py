"""Target flow against a mocked exchange: close, convert, verify, notify, stop."""

from __future__ import annotations

import asyncio

from nulltrades.models import EngineState, Mode

from .helpers import MockExchange, make_engine, open_long, tick


def run(coro):
    return asyncio.run(coro)


def live_mock(**kw):
    return lambda eng: MockExchange(lambda p: eng.quotes.get(p), 20.0, mode=Mode.LIVE, **kw)


async def live_with_position(tmp_path, **mock_kw):
    eng, clock, market, decider = await make_engine(
        tmp_path, mode=Mode.LIVE, outputs=[open_long()], executor_factory=live_mock(**mock_kw))
    await eng.start(run_loop=False)
    await tick(eng)
    assert len(eng.positions()) == 1
    return eng, clock, market


def test_target_closes_converts_notifies_and_stops(tmp_path):
    async def go():
        eng, clock, market = await live_with_position(tmp_path)
        ex = eng.executor
        ex.rest_limits = True  # exit limits rest unfilled -> cancel after timeout -> market
        # Holdings the app owns outside a position (e.g. a partial fill) must be converted too.
        ex._bal["ETH"] = 0.05
        row = eng.store.insert_order(eng.mode, eng.run_id, client_id="ntmanual000000001", pair="ETH/EUR",
                                     side="buy", type_="limit", purpose="test", amount=0.05, price=100.0,
                                     decision_id=None, position_id=None, now=clock())
        from nulltrades.models import OrderState
        o = OrderState("X1", "ntmanual000000001", "ETH/EUR", "buy", "limit", 0.05, 100.0, "closed", 0.05, 100.0, 0.0)
        eng.store.update_order(row, o, clock())
        eng.store.insert_fill(eng.mode, eng.run_id, row, o, clock())
        ex._bal["EUR"] -= 5.0

        market.set("BTC/EUR", 400.0)  # equity well above the 40 EUR target
        clock.advance(seconds=10)
        await tick(eng)

        assert eng.state == EngineState.TARGET_REACHED
        assert eng.positions() == []
        assert ex.cancels, "resting exit limit was not cancelled after timeout"
        purposes = [r["purpose"] for r in eng.store.rows("SELECT purpose FROM orders WHERE side = 'sell'")]
        assert "exit_target" in purposes and "exit_target_market" in purposes and "convert" in purposes
        assert eng.ledger()[1] == {}
        bal = await ex.balances()
        assert set(bal) == {"EUR"}
        ev = eng.store.rows("SELECT * FROM events WHERE kind = 'target_conversion'")
        assert ev and "EUR balance" in ev[0]["message"]
        msg = eng.notifier.sent[-1]
        assert "Target complete" in msg and "fees paid" in msg and "trades" in msg
        assert msg.startswith("[LIVE")
        assert not eng._should_loop()
        # Latched: another tick does nothing and start is refused.
        await tick(eng)
        assert eng.state == EngineState.TARGET_REACHED
    run(go())


def test_conversion_failure_halts_without_looping(tmp_path):
    async def go():
        eng, clock, market = await live_with_position(tmp_path)
        ex = eng.executor
        ex.rest_limits = True
        ex.fail_market = True
        market.set("BTC/EUR", 400.0)
        clock.advance(seconds=10)
        await tick(eng)
        assert eng.state == EngineState.HALTED
        assert any("HALTED" in m for m in eng.notifier.sent)
        submitted = len(ex.submitted)
        await tick(eng)
        await tick(eng)
        assert len(ex.submitted) == submitted, "HALTED must not keep retrying"
    run(go())


def test_target_equity_formula(tmp_path):
    async def go():
        eng, *_ = await make_engine(tmp_path)
        assert eng.run["target_equity"] == 40.0  # 20 x (1 + 1.0)
    run(go())

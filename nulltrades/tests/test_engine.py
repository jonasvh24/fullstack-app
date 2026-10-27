from __future__ import annotations

import asyncio
import json

import pytest

from nulltrades.models import EngineState

from .helpers import open_long, make_engine, tick


def run(coro):
    return asyncio.run(coro)


def decisions(eng):
    return eng.store.rows("SELECT * FROM decisions ORDER BY id")


def verdicts(eng):
    return eng.store.rows("SELECT * FROM risk_verdicts ORDER BY id")


def test_engine_comes_up_stopped(tmp_path):
    async def go():
        eng, *_ = await make_engine(tmp_path)
        assert eng.state == EngineState.STOPPED
    run(go())


def test_decision_opens_position_and_logs_everything(tmp_path):
    async def go():
        eng, clock, market, decider = await make_engine(tmp_path, outputs=[open_long(size=8.0)])
        await eng.start(run_loop=False)
        await tick(eng)
        assert decider.calls == 1
        [d] = decisions(eng)
        assert d["valid"] == 1 and d["prompt_version"] == "v1" and d["model"] == "claude-sonnet-5-5"
        assert d["snapshot_hash"] and json.loads(d["snapshot"])["mode"] == "PAPER"
        [v] = verdicts(eng)
        assert v["verdict"] == "approved"
        [pos] = eng.positions()
        assert pos.pair == "BTC/EUR" and pos.stop_loss == 95.0
        order = eng.store.one("SELECT * FROM orders")
        assert order["status"] == "closed" and order["decision_id"] == d["id"]
        equity, cash, _ = eng.equity()
        assert cash == pytest.approx(20 - 8.0 * 1.004, abs=0.05)
    run(go())


def test_one_model_call_per_closed_candle(tmp_path):
    async def go():
        eng, clock, market, decider = await make_engine(tmp_path)
        await eng.start(run_loop=False)
        await tick(eng)
        clock.advance(seconds=10)
        await tick(eng)
        clock.advance(minutes=20)
        await tick(eng)
        assert decider.calls == 1
        clock.advance(hours=1)
        await tick(eng)
        assert decider.calls == 2
    run(go())


@pytest.mark.parametrize("price,reason", [(94.0, "stop_loss"), (111.0, "take_profit")])
def test_engine_exits_on_stop_and_take_profit(tmp_path, price, reason):
    async def go():
        eng, clock, market, decider = await make_engine(tmp_path, outputs=[open_long()])
        await eng.start(run_loop=False)
        await tick(eng)
        assert len(eng.positions()) == 1
        for px in (99.0, 97.0, price):
            clock.advance(seconds=10)
            market.set("BTC/EUR", px)
            await tick(eng)
        assert eng.positions() == []
        [t] = eng.store.closed_trades(eng.mode, eng.run_id)
        assert t["exit_reason"] == reason
        sells = eng.store.rows("SELECT * FROM fills WHERE side = 'sell'")
        assert sells[0]["price"] == pytest.approx(price * 0.999)  # bid minus paper slippage
    run(go())


def test_stop_out_starts_cooldown(tmp_path):
    async def go():
        eng, clock, market, decider = await make_engine(
            tmp_path, outputs=[open_long(), open_long(stop=89.3, tp=103.0)])
        await eng.start(run_loop=False)
        await tick(eng)
        market.set("BTC/EUR", 94.0)
        clock.advance(minutes=30)  # stopped out at 10:30
        await tick(eng)
        market.set("BTC/EUR", 95.0)
        clock.advance(minutes=30)  # 11:00 candle, inside the 60 min cooldown
        await tick(eng)
        assert len(verdicts(eng)) == 2
        v = verdicts(eng)[-1]
        assert v["verdict"] == "rejected" and "cooldown" in v["reasons"]
    run(go())


def test_max_hold_closes_without_model_call(tmp_path):
    async def go():
        eng, clock, market, decider = await make_engine(tmp_path, outputs=[open_long()])
        await eng.start(run_loop=False)
        await tick(eng)
        await eng.stop()  # no more model calls
        calls = decider.calls
        clock.advance(hours=72)
        await tick(eng)
        assert eng.positions() == []
        assert eng.store.closed_trades(eng.mode, eng.run_id)[0]["exit_reason"] == "max_hold"
        assert decider.calls == calls
    run(go())


def test_update_stop_moves_up_only(tmp_path):
    async def go():
        up = {"actions": [{"action": "update_stop", "pair": "BTC/EUR", "new_stop": 97.0}]}
        down = {"actions": [{"action": "update_stop", "pair": "BTC/EUR", "new_stop": 96.0}]}
        eng, clock, market, decider = await make_engine(tmp_path, outputs=[open_long(), up, down])
        await eng.start(run_loop=False)
        for _ in range(3):
            await tick(eng)
            clock.advance(hours=1)
        assert eng.positions()[0].stop_loss == 97.0
        assert verdicts(eng)[-1]["verdict"] == "rejected"
    run(go())


def test_model_close_position(tmp_path):
    async def go():
        close = {"actions": [{"action": "close_position", "pair": "BTC/EUR"}]}
        eng, clock, market, decider = await make_engine(tmp_path, outputs=[open_long(), close])
        await eng.start(run_loop=False)
        await tick(eng)
        clock.advance(hours=1)
        await tick(eng)
        assert eng.positions() == []
        assert eng.store.closed_trades(eng.mode, eng.run_id)[0]["exit_reason"] == "model_close"
    run(go())


@pytest.mark.parametrize("bad,needle", [
    ('{"actions": [', "malformed JSON"),
    (open_long(stop=None), "requires stop_loss"),
    (open_long(pair="DOGE/EUR"), "unknown pair"),
    ('{"actions": [{"action": "open_long", "pair": "BTC/EUR", "size_eur": NaN, "stop_loss": 95, '
     '"take_profit": 110}]}', "size_eur"),
    (open_long(size=-8.0), "positive"),
])
def test_invalid_outputs_are_rejected_and_logged(tmp_path, bad, needle):
    async def go():
        eng, clock, market, decider = await make_engine(tmp_path, outputs=[bad])
        await eng.start(run_loop=False)
        await tick(eng)
        [d] = decisions(eng)
        assert d["valid"] == 0
        assert needle in d["validation_errors"]
        assert d["raw_output"]
        assert eng.positions() == []
        ev = eng.store.rows("SELECT * FROM events WHERE kind = 'invalid_output'")
        assert len(ev) == 1
    run(go())


def test_daily_model_call_cap(tmp_path):
    async def go():
        from .helpers import config
        cfg = config()
        cfg.max_model_calls_per_day = 2
        eng, clock, market, decider = await make_engine(tmp_path, cfg=cfg)
        await eng.start(run_loop=False)
        for _ in range(4):
            await tick(eng)
            clock.advance(hours=1)
        assert decider.calls == 2
        assert eng.store.rows("SELECT * FROM events WHERE kind = 'model_cap'")
    run(go())


def test_decisions_and_events_are_append_only(tmp_path):
    async def go():
        eng, *_ = await make_engine(tmp_path)
        await eng.start(run_loop=False)
        await tick(eng)
        import sqlite3
        with pytest.raises(sqlite3.DatabaseError):
            eng.store.db.execute("UPDATE decisions SET valid = 1")
        with pytest.raises(sqlite3.DatabaseError):
            eng.store.db.execute("DELETE FROM events")
    run(go())


def test_running_seconds_accumulate_only_while_running(tmp_path):
    async def go():
        eng, clock, *_ = await make_engine(tmp_path)
        await eng.start(run_loop=False)
        for _ in range(3):
            await tick(eng)
            clock.advance(seconds=10)
        assert eng.store.total_running_seconds(eng.mode) == pytest.approx(20)
    run(go())

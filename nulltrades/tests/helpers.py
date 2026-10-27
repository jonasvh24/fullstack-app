"""Fakes for engine tests: a controllable clock, market, model and exchange."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from nulltrades.config import Config, RiskConfig
from nulltrades.decision import DecisionSource, ModelReply
from nulltrades.engine import Engine
from nulltrades.execution.paper import PaperExecutor
from nulltrades.models import Candle, MarketInfo, Mode, OrderState, Quote, base_of
from nulltrades.notify import Notifier
from nulltrades.state import Store

T0 = datetime(2026, 10, 27, 10, 0, 30, tzinfo=timezone.utc)


class Clock:
    def __init__(self, t: datetime = T0) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw) -> None:
        self.t += timedelta(**kw)


class FakeMarket:
    def __init__(self, clock: Clock, prices: dict[str, float], spread: float = 0.0) -> None:
        self.clock = clock
        self.prices = dict(prices)
        self.spread = spread
        self.fail = False
        self.lag_seconds = 0.0
        self.infos = {p: MarketInfo(p, True, 0.0001, 0.5, 1e-8, 0.01) for p in prices}

    def set(self, pair: str, price: float) -> None:
        self.prices[pair] = price

    async def markets(self):
        return dict(self.infos)

    async def quotes(self, pairs):
        if self.fail:
            raise RuntimeError("exchange unavailable")
        ts = self.clock() - timedelta(seconds=self.lag_seconds)
        return {p: Quote(p, self.prices[p], self.prices[p] + self.spread, self.prices[p], ts)
                for p in pairs if p in self.prices}

    async def candles(self, pair, timeframe, limit):
        now = self.clock().replace(minute=0, second=0, microsecond=0)
        px = self.prices[pair]
        return [Candle(now - timedelta(hours=limit - i), px, px * 1.01, px * 0.99, px, 10.0)
                for i in range(limit + 1)]


class FakeDecider:
    """Returns scripted outputs in order, then holds. A str is passed through raw."""

    def __init__(self, outputs=None) -> None:
        self.outputs = list(outputs or [])
        self.calls = 0

    async def decide(self, *, model, system_prompt, snapshot_json):
        self.calls += 1
        out = self.outputs.pop(0) if self.outputs else {"actions": [{"action": "hold"}]}
        if isinstance(out, ModelReply):
            return out
        raw = out if isinstance(out, str) else json.dumps(out)
        return ModelReply(raw, raw, None, {"input_tokens": 1}, "claude-sonnet-5-5")


def open_long(pair="BTC/EUR", size=8.0, stop=95.0, tp=110.0):
    return {"actions": [{"action": "open_long", "pair": pair, "size_eur": size, "stop_loss": stop,
                         "take_profit": tp, "new_stop": None, "confidence": 0.5, "rationale": "test"}]}


class MockExchange(PaperExecutor):
    """A paper-style ledger with optional resting limit orders and failures,
    standing in for Kraken in LIVE-mode tests."""

    def __init__(self, quote_fn, cash, mode=Mode.LIVE, rest_limits=False, fail_market=False,
                 extra_eur=0.0, fee_taker_pct=0.40, slippage_bps=10) -> None:
        super().__init__(quote_fn, cash + extra_eur, None, fee_taker_pct, slippage_bps)
        self.mode = mode
        self.rest_limits = rest_limits
        self.fail_market = fail_market
        self.cancels: list[str] = []
        self.submitted: list[str] = []

    async def buy_limit(self, pair, amount, price, client_id):
        self.submitted.append(client_id)
        if self.rest_limits:
            return self._new(pair, "buy", "limit", amount, price, client_id)
        return await super().buy_limit(pair, amount, price, client_id)

    async def sell_limit(self, pair, amount, price, client_id):
        self.submitted.append(client_id)
        if self.rest_limits:
            return self._new(pair, "sell", "limit", amount, price, client_id)
        return await super().sell_limit(pair, amount, price, client_id)

    async def market_sell(self, pair, amount, client_id):
        self.submitted.append(client_id)
        if self.fail_market:
            raise RuntimeError("market sell failed")
        return await super().market_sell(pair, amount, client_id)

    async def cancel(self, order_id, pair):
        self.cancels.append(order_id)
        await super().cancel(order_id, pair)


def config(**risk_over) -> Config:
    """Default config with a short order timeout so tests do not wait 120 s."""
    cfg = Config(pairs=["BTC/EUR", "ETH/EUR"])
    cfg.risk = RiskConfig.model_construct(**{**RiskConfig().model_dump(), "order_timeout_seconds": 0.05, **risk_over})
    return cfg


async def no_sleep(_s):
    return None


async def make_engine(tmp_path, *, prices=None, outputs=None, mode=Mode.PAPER, executor_factory=None,
                      cfg=None, capital=20.0, pairs=("BTC/EUR", "ETH/EUR")):
    clock = Clock()
    market = FakeMarket(clock, prices or {"BTC/EUR": 100.0, "ETH/EUR": 100.0})
    store = Store(tmp_path / "t.db")
    decider = FakeDecider(outputs)
    eng = Engine(cfg or config(), store, market, DecisionSource(store, decider), Notifier(),
                 mode=mode, clock=clock, sleep=no_sleep, order_poll_seconds=0)
    if executor_factory is not None:
        eng.executor = executor_factory(eng)
    await eng.create_run(capital, 1.0, list(pairs))
    if executor_factory is not None:
        eng.executor = executor_factory(eng)
    return eng, clock, market, decider


async def tick(eng):
    await eng.tick()
    await eng.drain()

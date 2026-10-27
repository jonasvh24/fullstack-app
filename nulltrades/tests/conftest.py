from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nulltrades.config import RiskConfig
from nulltrades.models import MarketInfo, PositionView, Quote
from nulltrades.risk import Action, RiskContext

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


def market(pair="BTC/EUR", min_amount=0.0001, min_cost=0.5, amount_step=1e-8, price_step=0.1, active=True):
    return MarketInfo(pair, active, min_amount, min_cost, amount_step, price_step)


def quote(pair="BTC/EUR", bid=100.0, ask=100.0, last=None, ts=NOW):
    return Quote(pair, bid, ask, last if last is not None else bid, ts)


def position(pair="BTC/EUR", amount=0.05, entry=100.0, stop=95.0, tp=110.0, opened=NOW, id=1):
    return PositionView(id, pair, amount, entry, amount * entry, stop, tp, opened)


def ctx(**over) -> RiskContext:
    base = dict(
        now=NOW,
        engine_running=True,
        kill_active=False,
        whitelist=frozenset({"BTC/EUR", "ETH/EUR"}),
        markets={"BTC/EUR": market(), "ETH/EUR": market("ETH/EUR", min_amount=0.002, price_step=0.01)},
        quotes={"BTC/EUR": quote(), "ETH/EUR": quote("ETH/EUR", 100.0, 100.0)},
        equity=20.0,
        starting_capital=20.0,
        available_cash=20.0,
        open_positions={},
        entries_today=0,
        loss_today=0.0,
        cooldown_until={},
        limits=RiskConfig(),
    )
    base.update(over)
    return RiskContext(**base)


def open_long(pair="BTC/EUR", size=8.0, stop=95.0, tp=110.0, **kw):
    return Action("open_long", pair, size_eur=size, stop_loss=stop, take_profit=tp, **kw)


@pytest.fixture
def now():
    return NOW


def later(**kw):
    return NOW + timedelta(**kw)

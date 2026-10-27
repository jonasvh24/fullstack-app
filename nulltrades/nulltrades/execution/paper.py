"""PaperExecutor: simulated fills against live public prices.

Buys fill at best ask plus paper_slippage_bps, sells at best bid minus the
same, with the taker fee on every fill. Fills are immediate at the modelled
price, which is optimistic on thin books. A limit that the modelled price would
not satisfy does not fill; paper orders never rest on a book.
"""

from __future__ import annotations

import itertools
from typing import Callable

from ..models import Mode, OrderState, Quote, base_of
from .base import ExecutionError

EPS = 1e-12


class PaperExecutor:
    mode = Mode.PAPER

    def __init__(
        self,
        quote_fn: Callable[[str], Quote | None],
        cash_eur: float,
        holdings: dict[str, float] | None = None,
        fee_taker_pct: float = 0.40,
        slippage_bps: float = 10,
    ) -> None:
        self._quote = quote_fn
        self._bal: dict[str, float] = {"EUR": float(cash_eur)}
        for asset, amt in (holdings or {}).items():
            self._bal[asset] = float(amt)
        self.fee_rate = fee_taker_pct / 100
        self.slip = slippage_bps / 10_000
        self._orders: dict[str, OrderState] = {}
        self._ids = itertools.count(1)

    # --- interface ------------------------------------------------------------

    async def buy_limit(self, pair: str, amount: float, price: float, client_id: str) -> OrderState:
        o = self._new(pair, "buy", "limit", amount, price, client_id)
        q = self._quote(pair)
        if q is None:
            return self._reject(o, "no quote")
        fill = q.ask * (1 + self.slip)
        if fill > price + EPS:
            o.status, o.error = "canceled", f"modelled fill {fill:g} above limit {price:g}"
            return o
        cost = amount * fill
        fee = cost * self.fee_rate
        if cost + fee > self._bal["EUR"] + EPS:
            return self._reject(o, "insufficient EUR")
        self._bal["EUR"] -= cost + fee
        self._bal["EUR"] = max(self._bal["EUR"], 0.0)  # float dust, never negative
        asset = base_of(pair)
        self._bal[asset] = self._bal.get(asset, 0.0) + amount
        return self._fill(o, fill, fee)

    async def sell_limit(self, pair: str, amount: float, price: float, client_id: str) -> OrderState:
        o = self._new(pair, "sell", "limit", amount, price, client_id)
        q = self._quote(pair)
        if q is None:
            return self._reject(o, "no quote")
        fill = q.bid * (1 - self.slip)
        if fill < price - EPS:
            o.status, o.error = "canceled", f"modelled fill {fill:g} below limit {price:g}"
            return o
        return self._sell(o, fill)

    async def market_sell(self, pair: str, amount: float, client_id: str) -> OrderState:
        o = self._new(pair, "sell", "market", amount, None, client_id)
        q = self._quote(pair)
        if q is None:
            return self._reject(o, "no quote")
        return self._sell(o, q.bid * (1 - self.slip))

    async def cancel(self, order_id: str, pair: str) -> None:
        o = self._orders.get(order_id)
        if o and not o.is_final:
            o.status = "canceled"

    async def open_orders(self) -> list[OrderState]:
        return [o for o in self._orders.values() if not o.is_final]

    async def balances(self) -> dict[str, float]:
        return {k: v for k, v in self._bal.items() if v > EPS or k == "EUR"}

    async def fetch_order(self, order_id: str, pair: str) -> OrderState:
        if order_id not in self._orders:
            raise ExecutionError(f"unknown paper order {order_id}")
        return self._orders[order_id]

    # --- internals ------------------------------------------------------------

    def _sell(self, o: OrderState, fill: float) -> OrderState:
        asset = base_of(o.pair)
        held = self._bal.get(asset, 0.0)
        if o.amount > held + EPS:
            return self._reject(o, f"insufficient {asset}")
        proceeds = o.amount * fill
        fee = proceeds * self.fee_rate
        self._bal[asset] = max(held - o.amount, 0.0)
        self._bal["EUR"] += proceeds - fee
        return self._fill(o, fill, fee)

    def _new(self, pair, side, type_, amount, price, client_id) -> OrderState:
        o = OrderState(f"P{next(self._ids)}", client_id, pair, side, type_, float(amount), price, "open")
        self._orders[o.id] = o
        return o

    def _fill(self, o: OrderState, price: float, fee: float) -> OrderState:
        o.status, o.filled, o.average, o.fee_eur = "closed", o.amount, price, fee
        return o

    def _reject(self, o: OrderState, why: str) -> OrderState:
        o.status, o.error = "rejected", why
        return o

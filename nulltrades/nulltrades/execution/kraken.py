"""KrakenExecutor: live spot orders through ccxt.

The ccxt client sits behind _KrakenTradingClient, which exposes only balance,
order query, order creation and cancellation. Withdrawals, deposits and
transfers have no path through this module.

Fees are requested in the quote currency (oflags=fciq), so every fill pays its
fee in EUR and the base amount bought is the base amount held.
"""

from __future__ import annotations

import asyncio
import logging

from ..models import Mode, OrderState
from .base import CLIENT_ID_PREFIX, ExecutionError

log = logging.getLogger("nulltrades.kraken")


class _KrakenTradingClient:
    """Narrow facade over ccxt.kraken. Only these calls exist."""

    def __init__(self, api_key: str, api_secret: str, exchange=None) -> None:
        if exchange is None:
            import ccxt

            exchange = ccxt.kraken({
                "apiKey": api_key, "secret": api_secret,
                "enableRateLimit": True, "timeout": 20000,
            })
        self.__ex = exchange

    @property
    def network_errors(self) -> tuple[type[BaseException], ...]:
        import ccxt

        return (ccxt.NetworkError,)

    def load_markets(self):
        return self.__ex.load_markets()

    def fetch_balance(self) -> dict:
        return self.__ex.fetch_balance()

    def fetch_open_orders(self) -> list[dict]:
        return self.__ex.fetch_open_orders()

    def fetch_closed_orders(self, limit: int = 50) -> list[dict]:
        return self.__ex.fetch_closed_orders(None, None, limit)

    def fetch_order(self, order_id: str, symbol: str) -> dict:
        return self.__ex.fetch_order(order_id, symbol)

    def create_order(self, symbol: str, type_: str, side: str, amount: float, price: float | None,
                     client_id: str) -> dict:
        if type_ == "market" and side != "sell":
            raise ExecutionError("market orders are for exits only")
        amount = float(self.__ex.amount_to_precision(symbol, amount))
        if price is not None:
            price = float(self.__ex.price_to_precision(symbol, price))
        params = {"clientOrderId": client_id, "oflags": "fciq"}
        return self.__ex.create_order(symbol, type_, side, amount, price, params)

    def cancel_order(self, order_id: str, symbol: str) -> dict:
        return self.__ex.cancel_order(order_id, symbol)


def _to_state(o: dict, fallback_client_id: str = "") -> OrderState:
    fee_eur = 0.0
    for f in (o.get("fees") or ([o["fee"]] if o.get("fee") else [])):
        if f and f.get("cost") is not None and (f.get("currency") in (None, "EUR", "ZEUR")):
            fee_eur += float(f["cost"])
    status = {"open": "open", "closed": "closed", "canceled": "canceled", "expired": "canceled",
              "rejected": "rejected"}.get(o.get("status") or "open", "open")
    return OrderState(
        id=str(o.get("id")),
        client_id=str(o.get("clientOrderId") or fallback_client_id),
        pair=o.get("symbol") or "",
        side=o.get("side") or "",
        type=o.get("type") or "",
        amount=float(o.get("amount") or 0),
        price=float(o["price"]) if o.get("price") is not None else None,
        status=status,
        filled=float(o.get("filled") or 0),
        average=float(o["average"]) if o.get("average") is not None else None,
        fee_eur=fee_eur,
    )


class KrakenExecutor:
    mode = Mode.LIVE

    def __init__(self, api_key: str, api_secret: str, client: _KrakenTradingClient | None = None) -> None:
        self._c = client or _KrakenTradingClient(api_key, api_secret)

    async def _call(self, fn, *args):
        return await asyncio.to_thread(fn, *args)

    async def _find_by_client_id(self, client_id: str) -> OrderState | None:
        for o in await self._call(self._c.fetch_open_orders):
            if o.get("clientOrderId") == client_id:
                return _to_state(o)
        for o in await self._call(self._c.fetch_closed_orders, 50):
            if o.get("clientOrderId") == client_id:
                return _to_state(o)
        return None

    async def _submit(self, pair: str, type_: str, side: str, amount: float, price: float | None,
                      client_id: str) -> OrderState:
        """Submit once. On a network error, look the order up by client ID
        before any retry, so a request that reached Kraken is never doubled."""
        for attempt in (1, 2):
            try:
                o = await self._call(self._c.create_order, pair, type_, side, amount, price, client_id)
                return _to_state(o, client_id)
            except self._c.network_errors as e:
                log.warning("order %s network error on attempt %d: %s", client_id, attempt, type(e).__name__)
                found = await self._find_by_client_id(client_id)
                if found is not None:
                    return found
        raise ExecutionError(f"order {client_id} failed after retry")

    async def buy_limit(self, pair: str, amount: float, price: float, client_id: str) -> OrderState:
        return await self._submit(pair, "limit", "buy", amount, price, client_id)

    async def sell_limit(self, pair: str, amount: float, price: float, client_id: str) -> OrderState:
        return await self._submit(pair, "limit", "sell", amount, price, client_id)

    async def market_sell(self, pair: str, amount: float, client_id: str) -> OrderState:
        return await self._submit(pair, "market", "sell", amount, None, client_id)

    async def cancel(self, order_id: str, pair: str) -> None:
        await self._call(self._c.cancel_order, order_id, pair)

    async def open_orders(self) -> list[OrderState]:
        """Open orders this app placed. Orders the user placed by hand are ignored."""
        rows = await self._call(self._c.fetch_open_orders)
        return [_to_state(o) for o in rows if str(o.get("clientOrderId") or "").startswith(CLIENT_ID_PREFIX)]

    async def balances(self) -> dict[str, float]:
        b = await self._call(self._c.fetch_balance)
        total = b.get("total") or {}
        return {k: float(v) for k, v in total.items() if v}

    async def fetch_order(self, order_id: str, pair: str) -> OrderState:
        return _to_state(await self._call(self._c.fetch_order, order_id, pair))

    async def markets(self) -> dict:
        return await self._call(self._c.load_markets)

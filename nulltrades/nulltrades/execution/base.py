"""Execution interface shared by PAPER and LIVE. Only the adapter differs.

Broker adapters implement this protocol. Kraken is the only live
implementation in v1. The interface deliberately has no withdrawal, deposit or
transfer method, and tests/test_no_withdrawal.py fails if one appears.
"""

from __future__ import annotations

import secrets
from typing import Protocol

from ..models import Mode, OrderState

CLIENT_ID_PREFIX = "nt"


def new_client_id() -> str:
    # Kraken accepts an alphanumeric cl_ord_id of up to 18 characters.
    return CLIENT_ID_PREFIX + secrets.token_hex(8)


class Executor(Protocol):
    mode: Mode

    async def buy_limit(self, pair: str, amount: float, price: float, client_id: str) -> OrderState: ...
    async def sell_limit(self, pair: str, amount: float, price: float, client_id: str) -> OrderState: ...
    async def market_sell(self, pair: str, amount: float, client_id: str) -> OrderState: ...
    async def cancel(self, order_id: str, pair: str) -> None: ...
    async def open_orders(self) -> list[OrderState]: ...
    async def balances(self) -> dict[str, float]: ...
    # Not in the brief's method list: needed to follow a resting live order to
    # its final state without blocking (DECISIONS.md D-09).
    async def fetch_order(self, order_id: str, pair: str) -> OrderState: ...


class ExecutionError(Exception):
    pass

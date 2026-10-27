"""Plain data types shared across modules, plus time and rounding helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import Enum
from zoneinfo import ZoneInfo

BRUSSELS = ZoneInfo("Europe/Brussels")


class Mode(str, Enum):
    PAPER = "PAPER"
    LIVE = "LIVE"


class EngineState(str, Enum):
    STOPPED = "STOPPED"
    RUNNING = "RUNNING"
    KILLED = "KILLED"
    TARGET_REACHED = "TARGET_REACHED"
    HALTED = "HALTED"


# These survive restarts until the user resets them.
LATCHED_STATES = {EngineState.KILLED, EngineState.TARGET_REACHED, EngineState.HALTED}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def brussels_day_start(now: datetime) -> datetime:
    """UTC instant of the most recent 00:00 Europe/Brussels."""
    local = now.astimezone(BRUSSELS)
    midnight = datetime.combine(local.date(), time(0, 0), tzinfo=BRUSSELS)
    return midnight.astimezone(timezone.utc)


def floor_to_step(value: float, step: float) -> float:
    """Round down to a multiple of step. Order amounts are never rounded up."""
    if step <= 0:
        return value
    d, s = Decimal(str(value)), Decimal(str(step))
    return float((d / s).to_integral_value(rounding=ROUND_FLOOR) * s)


def ceil_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    d, s = Decimal(str(value)), Decimal(str(step))
    return float((d / s).to_integral_value(rounding=ROUND_CEILING) * s)


def base_of(pair: str) -> str:
    return pair.split("/")[0]


@dataclass(frozen=True)
class Quote:
    pair: str
    bid: float
    ask: float
    last: float
    ts: datetime  # when we received it


@dataclass(frozen=True)
class Candle:
    ts: datetime  # open time, UTC
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class MarketInfo:
    pair: str
    active: bool
    min_amount: float
    min_cost: float
    amount_step: float
    price_step: float


@dataclass
class OrderState:
    id: str
    client_id: str
    pair: str
    side: str  # buy | sell
    type: str  # limit | market
    amount: float
    price: float | None
    status: str  # open | closed | canceled | rejected
    filled: float = 0.0
    average: float | None = None
    fee_eur: float = 0.0
    error: str | None = None

    @property
    def remaining(self) -> float:
        return max(0.0, self.amount - self.filled)

    @property
    def is_final(self) -> bool:
        return self.status in ("closed", "canceled", "rejected")


@dataclass
class PositionView:
    id: int
    pair: str
    amount: float
    entry_price: float
    cost_eur: float
    stop_loss: float
    take_profit: float
    opened_at: datetime
    extra: dict = field(default_factory=dict)

    def age_hours(self, now: datetime) -> float:
        return (now - self.opened_at) / timedelta(hours=1)

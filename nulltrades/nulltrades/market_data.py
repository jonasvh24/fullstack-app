"""Public market data, indicators, staleness and CSV replay input.

No keys are used here. Kraken's OHLC endpoint only returns the most recent 720
candles, so replays read history from CSV files the user places in data/.
"""

from __future__ import annotations

import asyncio
import csv
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from .models import Candle, MarketInfo, Quote, utcnow


# --- indicators ---------------------------------------------------------------

def ema(values: list[float], period: int) -> list[float | None]:
    """EMA seeded with the SMA of the first `period` values."""
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(closes: list[float], period: int = 14) -> list[float | None]:
    """Wilder's RSI."""
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    avg_g, avg_l = gains / period, losses / period
    out[period] = _rsi_value(avg_g, avg_l)
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (period - 1) + max(d, 0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0)) / period
        out[i] = _rsi_value(avg_g, avg_l)
    return out


def _rsi_value(g: float, l: float) -> float:
    if l == 0:
        return 100.0 if g > 0 else 50.0
    return 100 - 100 / (1 + g / l)


def atr(candles: list[Candle], period: int = 14) -> list[float | None]:
    """Wilder's ATR."""
    out: list[float | None] = [None] * len(candles)
    if len(candles) <= period:
        return out
    trs = [candles[0].high - candles[0].low]
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1].close
        trs.append(max(c.high - c.low, abs(c.high - p), abs(c.low - p)))
    prev = sum(trs[1:period + 1]) / period
    out[period] = prev
    for i in range(period + 1, len(candles)):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out


def indicator_summary(candles: list[Candle]) -> dict[str, float | None]:
    closes = [c.close for c in candles]

    def last(xs: list[float | None]) -> float | None:
        v = xs[-1] if xs else None
        return None if v is None or math.isnan(v) else round(v, 10)

    return {
        "ema20": last(ema(closes, 20)),
        "ema50": last(ema(closes, 50)),
        "rsi14": last(rsi(closes, 14)),
        "atr14": last(atr(candles, 14)),
    }


def closed_candles(candles: list[Candle], timeframe_seconds: int, now: datetime) -> list[Candle]:
    """Drop the candle still forming at `now`."""
    tf = timedelta(seconds=timeframe_seconds)
    return [c for c in candles if c.ts + tf <= now]


def last_closed_boundary(now: datetime, timeframe_seconds: int) -> datetime:
    """Close time of the most recent fully closed candle."""
    epoch = int(now.timestamp())
    return datetime.fromtimestamp(epoch - epoch % timeframe_seconds, tz=timezone.utc)


def is_stale(quote: Quote | None, now: datetime, max_age_seconds: float) -> bool:
    return quote is None or (now - quote.ts).total_seconds() > max_age_seconds


# --- sources ------------------------------------------------------------------

class MarketDataSource(Protocol):
    async def markets(self) -> dict[str, MarketInfo]: ...
    async def quotes(self, pairs: list[str]) -> dict[str, Quote]: ...
    async def candles(self, pair: str, timeframe: str, limit: int) -> list[Candle]: ...


def market_info_from_ccxt(pair: str, m: dict) -> MarketInfo:
    limits = m.get("limits") or {}
    precision = m.get("precision") or {}
    return MarketInfo(
        pair=pair,
        active=bool(m.get("active", True)) and bool(m.get("spot", True)),
        min_amount=float((limits.get("amount") or {}).get("min") or 0.0),
        min_cost=float((limits.get("cost") or {}).get("min") or 0.0),
        # ccxt's Kraken driver uses TICK_SIZE precision mode, so these are steps.
        amount_step=float(precision.get("amount") or 1e-8),
        price_step=float(precision.get("price") or 1e-8),
    )


class KrakenPublicData:
    """Kraken public endpoints via ccxt. The sync client runs in a thread so the
    event loop keeps polling stops while a request is in flight."""

    def __init__(self, exchange=None) -> None:
        if exchange is None:
            import ccxt

            exchange = ccxt.kraken({"enableRateLimit": True, "timeout": 15000})
        self._ex = exchange
        self._markets: dict[str, MarketInfo] | None = None
        self._markets_at: datetime | None = None

    async def markets(self) -> dict[str, MarketInfo]:
        now = utcnow()
        if self._markets is None or self._markets_at is None or now - self._markets_at > timedelta(hours=6):
            raw = await asyncio.to_thread(self._ex.load_markets, True)
            self._markets = {
                sym: market_info_from_ccxt(sym, m)
                for sym, m in raw.items()
                if m.get("spot") and m.get("quote") == "EUR" and "/" in sym and ":" not in sym
            }
            self._markets_at = now
        return self._markets

    async def quotes(self, pairs: list[str]) -> dict[str, Quote]:
        if not pairs:
            return {}
        raw = await asyncio.to_thread(self._ex.fetch_tickers, pairs)
        now = utcnow()
        out = {}
        for p in pairs:
            t = raw.get(p)
            if not t or t.get("bid") is None or t.get("ask") is None:
                continue
            out[p] = Quote(p, float(t["bid"]), float(t["ask"]), float(t.get("last") or t["bid"]), now)
        missing = [p for p in pairs if p not in out]
        if missing:
            raise RuntimeError(f"no quote for {', '.join(missing)}")
        return out

    async def candles(self, pair: str, timeframe: str, limit: int) -> list[Candle]:
        raw = await asyncio.to_thread(self._ex.fetch_ohlcv, pair, timeframe, None, limit)
        return [
            Candle(datetime.fromtimestamp(r[0] / 1000, tz=timezone.utc), *map(float, r[1:6]))
            for r in raw
        ]


# --- CSV replay input ---------------------------------------------------------

def csv_path(data_dir: Path, pair: str, timeframe: str) -> Path:
    return data_dir / f"{pair.replace('/', '-')}_{timeframe}.csv"


def load_csv_candles(path: Path) -> list[Candle]:
    """CSV with header timestamp,open,high,low,close,volume. timestamp is the
    candle open time, as UTC ISO 8601 or Unix epoch (seconds or milliseconds)."""
    out: list[Candle] = []
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        need = {"timestamp", "open", "high", "low", "close", "volume"}
        if not reader.fieldnames or not need.issubset({h.strip().lower() for h in reader.fieldnames}):
            raise ValueError(f"{path.name}: header must contain {', '.join(sorted(need))}")
        for row in reader:
            row = {k.strip().lower(): v.strip() for k, v in row.items() if k}
            out.append(Candle(
                _parse_ts(row["timestamp"]),
                float(row["open"]), float(row["high"]), float(row["low"]),
                float(row["close"]), float(row["volume"]),
            ))
    out.sort(key=lambda c: c.ts)
    return out


def _parse_ts(s: str) -> datetime:
    try:
        n = float(s)
    except ValueError:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    if n > 1e11:  # milliseconds
        n /= 1000
    return datetime.fromtimestamp(n, tz=timezone.utc)

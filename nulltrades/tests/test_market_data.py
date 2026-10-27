from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nulltrades.market_data import (
    atr, closed_candles, ema, is_stale, last_closed_boundary, load_csv_candles, rsi,
)
from nulltrades.models import Candle

from .conftest import NOW, quote


def test_ema_seeds_with_sma():
    out = ema([1, 2, 3, 4, 5], 3)
    assert out[:2] == [None, None]
    assert out[2] == pytest.approx(2.0)
    assert out[3] == pytest.approx(3.0)  # 4*0.5 + 2*0.5
    assert out[4] == pytest.approx(4.0)


def test_rsi_bounds():
    assert rsi(list(range(1, 30)))[-1] == 100.0
    assert rsi(list(range(30, 1, -1)))[-1] == pytest.approx(0.0)
    flat = rsi([10.0] * 20)
    assert flat[-1] == 50.0


def test_atr_constant_range():
    cs = [Candle(NOW + timedelta(hours=i), 10, 11, 9, 10, 1) for i in range(30)]
    assert atr(cs, 14)[-1] == pytest.approx(2.0)
    assert atr(cs, 14)[13] is None


def test_closed_candles_drops_forming_candle():
    cs = [Candle(NOW - timedelta(hours=2), 1, 1, 1, 1, 1), Candle(NOW - timedelta(minutes=30), 1, 1, 1, 1, 1)]
    assert len(closed_candles(cs, 3600, NOW)) == 1


def test_last_closed_boundary():
    t = datetime(2026, 1, 1, 13, 42, 7, tzinfo=timezone.utc)
    assert last_closed_boundary(t, 3600) == datetime(2026, 1, 1, 13, 0, tzinfo=timezone.utc)


def test_staleness():
    q = quote(ts=NOW)
    assert not is_stale(q, NOW + timedelta(seconds=120), 120)
    assert is_stale(q, NOW + timedelta(seconds=121), 120)
    assert is_stale(None, NOW, 120)


def test_csv_formats(tmp_path):
    p = tmp_path / "BTC-EUR_1h.csv"
    p.write_text(
        "timestamp,open,high,low,close,volume\n"
        "2026-01-01T01:00:00Z,2,3,1,2.5,10\n"
        "1767225600,1,2,0.5,1.5,5\n"
        "1767232800000,3,4,2,3.5,7\n"
    )
    cs = load_csv_candles(p)
    assert [c.close for c in cs] == [1.5, 2.5, 3.5]
    assert cs[0].ts == datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_csv_bad_header(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text("time,o,h,l,c\n")
    with pytest.raises(ValueError):
        load_csv_candles(p)

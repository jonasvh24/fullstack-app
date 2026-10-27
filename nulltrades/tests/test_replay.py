from __future__ import annotations

import asyncio
import json
import math
from datetime import datetime, timedelta, timezone

from nulltrades.config import Config
from nulltrades.decision import ModelReply
from nulltrades.replay import replay

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def write_csv(path, n=220):
    lines = ["timestamp,open,high,low,close,volume"]
    px = 100.0
    for i in range(n):
        # A gentle uptrend with a wave, so both stops and take-profits get touched.
        nxt = 100 * (1 + 0.002 * i) * (1 + 0.04 * math.sin(i / 6))
        hi, lo = max(px, nxt) * 1.004, min(px, nxt) * 0.996
        lines.append(f"{(T0 + timedelta(hours=i)).isoformat()},{px:.4f},{hi:.4f},{lo:.4f},{nxt:.4f},10")
        px = nxt
    path.write_text("\n".join(lines) + "\n")


class PriceAwareDecider:
    """Opens BTC with a 3% stop and 6% take-profit whenever flat."""

    def __init__(self):
        self.calls = 0

    async def decide(self, *, model, system_prompt, snapshot_json):
        self.calls += 1
        snap = json.loads(snapshot_json)
        if snap["portfolio"]["open_positions"]:
            out = {"actions": []}
        else:
            px = snap["pairs"]["BTC/EUR"]["last_price"]
            out = {"actions": [{"action": "open_long", "pair": "BTC/EUR", "size_eur": 8.0,
                                "stop_loss": round(px * 0.97, 4), "take_profit": round(px * 1.06, 4),
                                "new_stop": None, "confidence": 0.5, "rationale": "test"}]}
        raw = json.dumps(out)
        return ModelReply(raw, raw, None, None, "claude-sonnet-5-5")


def run_replay(tmp_path, allow_api, decider=None, out="r.db"):
    cfg = Config(pairs=["BTC/EUR"])
    return asyncio.run(replay(cfg, ["BTC/EUR"], 20.0, 1.0, tmp_path, None, None, allow_api,
                              tmp_path / out, tmp_path / "cache.db", decider=decider))


def test_replay_without_cache_never_calls_api_and_holds(tmp_path):
    write_csv(tmp_path / "BTC-EUR_1h.csv")
    d = PriceAwareDecider()
    r = run_replay(tmp_path, allow_api=False, decider=d)
    assert d.calls == 0
    assert r["model_calls"] == 0 and r["trades"] == 0
    assert r["final_state"] == "RUNNING"
    assert r["label"].startswith("SIMULATED")


def test_replay_trades_and_is_deterministic_from_cache(tmp_path):
    write_csv(tmp_path / "BTC-EUR_1h.csv")
    d = PriceAwareDecider()
    first = run_replay(tmp_path, allow_api=True, decider=d, out="a.db")
    assert first["trades"] > 0 and d.calls == first["model_calls"] > 0
    second = run_replay(tmp_path, allow_api=False, decider=PriceAwareDecider(), out="b.db")
    assert second["model_calls"] == 0 and second["cache_hits"] == first["decisions"]
    for k in ("trades", "net_pnl", "final_equity", "fees"):
        assert second[k] == first[k]


def test_replay_stop_uses_candle_low(tmp_path):
    write_csv(tmp_path / "BTC-EUR_1h.csv")
    run_replay(tmp_path, allow_api=True, decider=PriceAwareDecider(), out="c.db")
    from nulltrades.state import Store
    from nulltrades.models import Mode
    trades = Store(tmp_path / "c.db").closed_trades(Mode.PAPER)
    reasons = {t["exit_reason"] for t in trades}
    assert reasons & {"stop_loss", "take_profit"}

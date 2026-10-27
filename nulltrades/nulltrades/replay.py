"""Replay: run the PAPER engine over historical candles from data/*.csv.

Results are SIMULATED and go to their own database under data/, never to the
main one, so a replay cannot count toward the live gate. Stops and
take-profits are checked against each candle's low and high; when both are
touched in one candle the stop is assumed to have hit first.

Model calls: by default a replay only uses cached decisions (data/replay-cache.db)
and never calls the API; a cache miss is logged as an invalid output. Pass
--allow-api to fill the cache on the first run (this costs money); re-running
the same replay with the same prompt version and model is then free and
deterministic.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config as C
from .config import Config, Secrets
from .decision import AnthropicDecider, DecisionSource
from .engine import Engine
from .market_data import csv_path, load_csv_candles
from .models import Candle, EngineState, MarketInfo, Mode, Quote, iso
from .notify import Notifier
from .risk import exit_reason
from .state import Store

WARMUP = 100

# Kraken limits are not in the CSVs. These are permissive placeholders; edit
# data/markets.csv (pair,min_amount,min_cost,amount_step,price_step) to match.
DEFAULT_MARKET = dict(min_amount=0.0, min_cost=0.5, amount_step=1e-8, price_step=1e-5)


class ReplayClock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


class ReplayMarket:
    def __init__(self, clock: ReplayClock, candles: dict[str, list[Candle]], tf_seconds: int,
                 infos: dict[str, MarketInfo]) -> None:
        self.clock = clock
        self.series = candles
        self.tf = timedelta(seconds=tf_seconds)
        self.infos = infos
        self.override: dict[str, float] = {}

    def _closed(self, pair: str) -> list[Candle]:
        now = self.clock()
        return [c for c in self.series[pair] if c.ts + self.tf <= now]

    async def markets(self):
        return dict(self.infos)

    async def quotes(self, pairs):
        out = {}
        for p in pairs:
            closed = self._closed(p)
            if not closed:
                continue
            px = self.override.get(p, closed[-1].close)
            out[p] = Quote(p, px, px, px, self.clock())
        return out

    async def candles(self, pair, timeframe, limit):
        return self._closed(pair)[-limit:]


def load_market_overrides(data_dir: Path) -> dict[str, dict]:
    path = data_dir / "markets.csv"
    if not path.exists():
        return {}
    with path.open(newline="") as f:
        return {r["pair"].strip(): {k: float(v) for k, v in r.items() if k != "pair"} for r in csv.DictReader(f)}


async def replay(cfg: Config, pairs: list[str], capital: float, target_roi: float, data_dir: Path,
                 start: datetime | None, end: datetime | None, allow_api: bool, out_db: Path,
                 cache_db: Path, prompt_dir: Path = C.PROMPT_DIR, decider=None) -> dict:
    series = {}
    for p in pairs:
        path = csv_path(data_dir, p, cfg.timeframe)
        if not path.exists():
            raise SystemExit(f"missing {path}; see README 'Replay data'")
        series[p] = load_csv_candles(path)
    overrides = load_market_overrides(data_dir)
    infos = {p: MarketInfo(p, True, **{**DEFAULT_MARKET, **overrides.get(p, {})}) for p in pairs}

    tf = timedelta(seconds=cfg.timeframe_seconds)
    closes = sorted({c.ts + tf for p in pairs for c in series[p][WARMUP:]})
    if start:
        closes = [t for t in closes if t >= start]
    if end:
        closes = [t for t in closes if t <= end]
    if not closes:
        raise SystemExit(f"not enough candles: need more than {WARMUP} per pair in the chosen window")

    clock = ReplayClock(closes[0])
    market = ReplayMarket(clock, series, cfg.timeframe_seconds, infos)
    store = Store(out_db)
    cache_store = Store(cache_db)
    if decider is None and allow_api:
        key = Secrets().anthropic_key
        if not key:
            raise SystemExit("--allow-api needs ANTHROPIC_API_KEY")
        decider = AnthropicDecider(key)
    source = DecisionSource(cache_store, decider, allow_api=allow_api)
    notifier = Notifier()

    async def no_sleep(_s):
        return None

    eng = Engine(cfg, store, market, source, notifier, mode=Mode.PAPER, clock=clock, sleep=no_sleep,
                 order_poll_seconds=0, prompt_dir=prompt_dir)
    await eng.create_run(capital, target_roi, pairs)
    await eng.start(run_loop=False)
    by_close = {p: {c.ts + tf: c for c in series[p]} for p in pairs}

    for t in closes:
        clock.t = t
        if eng.state != EngineState.RUNNING and not (eng.state == EngineState.STOPPED and eng.positions()):
            break
        # Intra-candle stops and take-profits against the candle's range.
        for pos in eng.positions():
            c = by_close[pos.pair].get(t)
            if c is None:
                continue
            fill = None
            if c.low <= pos.stop_loss:
                fill, reason = min(pos.stop_loss, c.open), "stop_loss"
            elif c.high >= pos.take_profit:
                fill, reason = max(pos.take_profit, c.open), "take_profit"
            if fill is not None:
                market.override[pos.pair] = fill
                eng.quotes[pos.pair] = Quote(pos.pair, fill, fill, fill, t)
                await eng._exit(pos, reason)
                market.override.pop(pos.pair, None)
        await eng.tick()
        await eng.drain()

    eq, cash, _ = eng.equity()
    trades = store.closed_trades(Mode.PAPER, eng.run_id)
    pnl = [x["realised_pnl"] or 0 for x in trades]
    decisions = store.rows("SELECT valid, cache_hit, model_called FROM decisions")
    snaps = store.rows("SELECT drawdown_pct FROM equity_snapshots")
    last_close = {p: [c for c in series[p] if c.ts + tf <= clock.t][-1].close for p in pairs}
    first_close = eng.run["start_prices"]
    bh = sum(capital / len(pairs) * last_close[p] / first_close[p] for p in pairs)
    return {
        "label": "SIMULATED (PAPER replay)",
        "window": [iso(closes[0]), iso(clock.t)],
        "pairs": pairs,
        "prompt_version": cfg.prompt_version,
        "model": cfg.model,
        "final_state": eng.state.value,
        "final_equity": round(eq, 4),
        "net_pnl": round(sum(pnl), 4),
        "trades": len(trades),
        "win_rate_pct": round(sum(1 for x in pnl if x > 0) / len(pnl) * 100, 1) if pnl else None,
        "fees": round(store.total_fees(Mode.PAPER, eng.run_id), 4),
        "max_drawdown_pct": round(max((s["drawdown_pct"] for s in snaps), default=0.0), 2),
        "buy_and_hold_equity": round(bh, 4),
        "decisions": len(decisions),
        "cache_hits": sum(d["cache_hit"] for d in decisions),
        "model_calls": sum(d["model_called"] for d in decisions),
        "invalid": sum(1 for d in decisions if not d["valid"]),
        "db": str(out_db),
    }


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="nulltrades replay", description="Replay the PAPER engine over CSV candles.")
    ap.add_argument("--pairs", required=True, help="comma-separated, e.g. BTC/EUR,ETH/EUR")
    ap.add_argument("--capital", type=float, default=None)
    ap.add_argument("--target-roi", type=float, default=None)
    ap.add_argument("--from", dest="start", help="ISO date/time, UTC")
    ap.add_argument("--to", dest="end", help="ISO date/time, UTC")
    ap.add_argument("--allow-api", action="store_true",
                    help="call the model on cache misses (costs money; fills data/replay-cache.db)")
    ap.add_argument("--config", type=Path, default=None)
    args = ap.parse_args(argv)

    cfg = C.load_config(args.config)
    pairs = [p.strip().upper() for p in args.pairs.split(",") if p.strip()]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_db = C.DATA_DIR / f"replay-{stamp}.db"
    result = asyncio.run(replay(
        cfg, pairs, args.capital or cfg.starting_capital_eur, args.target_roi or cfg.target_roi, C.DATA_DIR,
        _parse_dt(args.start), _parse_dt(args.end), args.allow_api, out_db, C.DATA_DIR / "replay-cache.db",
    ))
    print("nulltrades replay — SIMULATED, not real money")
    for k, v in result.items():
        print(f"  {k:<20} {v}")
    csv_out = C.DATA_DIR / f"replay-{stamp}-SIMULATED-trades.csv"
    store = Store(out_db)
    rows = store.closed_trades(Mode.PAPER)
    with csv_out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["# nulltrades replay: SIMULATED (PAPER) — not real money"])
        cols = ["pair", "opened_at", "closed_at", "entry_price", "exit_price", "amount", "realised_pnl",
                "exit_reason"]
        w.writerow(["label", *cols])
        for r in rows:
            w.writerow(["SIMULATED", *[r[c] for c in cols]])
    print(f"  {'trades_csv':<20} {csv_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

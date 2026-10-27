# nulltrades

A personal spot trading app for one Kraken account. It runs on this machine only: a Python backend and a React UI on `127.0.0.1`. An AI model proposes trades; deterministic code validates them, applies hard risk limits and places the orders.

- **PAPER** is simulated and is the default on every launch.
- **LIVE** trades real money and sits behind a gate (14 days and 30 closed trades of PAPER, a typed confirmation, a successful balance query, and confirmation that the API key cannot withdraw).

Scope for v1: Kraken spot, long only, EUR pairs from your whitelist. No margin, leverage, shorting, derivatives, memecoins or DEX trading. No code path withdraws, deposits or transfers funds, and a test fails if one appears.

## Quick start

Requirements: Python 3.12, Node 18+ and npm.

```bash
cp .env.example .env      # fill in keys (see below); never commit .env
./start.sh                # first run: venv, pip install, UI build; then serves
```

Open http://127.0.0.1:8765. Then:

1. **Settings**: add EUR pairs (for example `BTC/EUR`, `ETH/EUR`). Each pair shows its Kraken minimum order and whether it fits the allowed position size.
2. **Start**: choose capital, target ROI (1.0 = +100%) and pairs, and create the run.
3. **Dashboard**: press **Start**. The engine makes one model call per closed candle (default 1h) and checks stops every 10 seconds.

Port and every other setting live in `config.yaml`. Without `ANTHROPIC_API_KEY` the app runs, but the engine refuses to start.

## Secrets

Secrets come only from environment variables. They are never written to config, the database or the logs. `start.sh` exports `.env` if present.

| Variable | Needed for |
|---|---|
| `ANTHROPIC_API_KEY` | the decision model (PAPER and LIVE) |
| `KRAKEN_API_KEY`, `KRAKEN_API_SECRET` | LIVE only; PAPER uses public data |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | alerts; without them alerts go to the log |

## Kraken API key permissions

Create a key with these permissions and nothing else:

- Query Funds
- Query Open Orders & Trades
- Query Closed Orders & Trades
- Create & Modify Orders
- Cancel/Close Orders

**Never** enable Withdraw Funds, Deposit, or any transfer or staking permission. The live gate asks you to confirm this.

Fees: the defaults are 0.25% maker and 0.40% taker. Check them against your Kraken fee tier before going live and set them in Settings.

## How a decision becomes an order

1. Code builds a snapshot: last 100 closed candles per pair, EMA 20/50, RSI 14, ATR 14, quotes, exchange minimums, portfolio, today's remaining entries and loss budget, fees and limits.
2. The model (default `claude-sonnet-5-5`, prompt `prompt/v1.md`) replies through the `submit_decisions` tool. The static system prompt is marked for prompt caching. Replies are cached by (prompt version, model, snapshot hash).
3. pydantic validates the output: malformed JSON, NaN, negative numbers, unknown pairs, missing stops, and closing a position that doesn't exist are all rejected and logged. Three invalid outputs in a row kill the engine.
4. The risk engine (`nulltrades/risk.py`, pure functions) returns each action approved, clamped or rejected, with reasons. Sizing is the smallest of the risk-based size, 50% of equity, available cash and what the model asked for. It clamps down only, and amounts round down to exchange precision, never up. Model confidence is logged, never used for sizing.
5. The executor places the order: a limit within 15 bps of the top of book, cancelled after 120 s. Exits fall back to a market sell, because stops must execute.

Every step is logged: snapshot hash, prompt version, model, raw output, validation result, risk verdict and order result. See the Decisions screen, with a filter for rejections.

## Kill switch, target and alerts

- **Manual kill**: the KILL button. It cancels open orders, market-sells all positions (`flatten_on_kill`), stops the loop and alerts.
- **Automatic kill**: daily loss budget exceeded; drawdown ≥ 25% from peak; 3 consecutive exchange or data errors; price data older than 120 s; reconciliation mismatch above 0.5% of equity; 3 consecutive invalid model outputs.
- **Target**: when equity reaches `capital × (1 + target_roi)`, the engine cancels orders, closes everything (limit, then market), converts what it holds to EUR, verifies the EUR balance, alerts with final equity, fees and trade count, and stops. If conversion fails after retries it enters HALTED and alerts once.
- KILLED, TARGET_REACHED and HALTED survive restarts until you press Reset.
- **Telegram**: started, stopped, killed (with reason), target reached, halted, and a daily summary at 22:00 Europe/Brussels. Every message is prefixed `[PAPER · SIMULATED]` or `[LIVE · REAL MONEY]`.

## Paper trading: what it does and doesn't model

- Uses live public Kraken prices. No keys needed.
- Buys fill at best ask + 10 bps and sells at best bid − 10 bps, with the taker fee on every fill.
- **Fills are immediate at the modelled price. This is optimistic on thin books**: real orders can fill worse, partially or not at all. Expect LIVE to underperform PAPER.
- Stops and take-profits are checked against the best bid every 10 seconds, so a fast move can gap through a stop.
- Every paper figure is labelled PAPER or SIMULATED on screen, in exports and in notifications.

## Stops are enforced by the app

In v1 there are no exchange-side stop orders. **If the app or this machine is down, no stop runs.** The dashboard always shows this warning. After downtime the engine reconciles with Kraken before placing any order. The app always starts in PAPER: if LIVE positions are open at startup you get a warning and an alert, and their stops resume only when you re-enable LIVE.

## Reset

- Paper only: Settings → Reset PAPER state (type `RESET PAPER`), or `.venv/bin/python -m nulltrades reset-paper`. This clears paper runs, positions, orders, fills and equity history, including live-gate progress. Decisions and events are kept as an audit log. LIVE state is never touched.
- Latched engine state (KILLED, TARGET_REACHED, HALTED): the Reset button on the dashboard.
- Everything: stop the app and delete `nulltrades.db`.

## Replay

Kraken's API only returns recent candles, so replays read history from CSV files you place in `data/`:

```
data/BTC-EUR_1h.csv
timestamp,open,high,low,close,volume
2026-01-01T00:00:00Z,85000.1,85210.0,84890.5,85102.3,12.84
```

- File name: `<BASE>-EUR_<timeframe>.csv`, using the timeframe from `config.yaml`.
- `timestamp` is the candle **open** time in UTC: ISO 8601, or Unix epoch in seconds or milliseconds.
- At least 101 rows per pair: the first 100 are warm-up for the snapshot.
- Optional `data/markets.csv` with `pair,min_amount,min_cost,amount_step,price_step` sets exchange limits. The defaults are permissive.

```bash
.venv/bin/python -m nulltrades replay --pairs BTC/EUR,ETH/EUR --from 2026-01-01 --to 2026-03-01
```

Replays never call the model by default: they use cached decisions from `data/replay-cache.db`, and a cache miss is a hold. Add `--allow-api` to call the model on cache misses. This costs money: roughly one call per candle. After the first run, re-running the same replay with the same prompt and model is free and deterministic. Stops and take-profits are checked against each candle's low and high. Results go to their own `data/replay-*.db` and a `*-SIMULATED-trades.csv`, never to the main database, so a replay cannot count toward the live gate.

## Improving the model

The model does not learn while trading. Review the Decisions log weekly, write `prompt/v2.md`, select it in Settings, and run it in PAPER. `settings_versions` and each decision row record which prompt version and model were live at the time.

## Development

```bash
.venv/bin/python -m pytest -q          # 160 tests, no network, no API calls
cd ui && npm run dev                    # UI dev server, proxies /api to 127.0.0.1:8765
```

Layout:

| Path | Role |
|---|---|
| `nulltrades/market_data.py` | ccxt public data, indicators, staleness, CSV loader |
| `nulltrades/decision.py` | snapshot, model call, schema validation, cache |
| `nulltrades/risk.py` | risk engine, pure functions |
| `nulltrades/execution/` | `Executor` interface, `PaperExecutor`, `KrakenExecutor` |
| `nulltrades/engine.py` | loop, state machine, kill switch, target flow, reconciliation |
| `nulltrades/state.py` | SQLite; every row carries `mode` |
| `nulltrades/gate.py` | live gate |
| `nulltrades/notify.py` | Telegram |
| `nulltrades/api.py` | FastAPI; serves `ui/dist` |
| `nulltrades/replay.py` | replay command |
| `prompt/v1.md` | system prompt |
| `DECISIONS.md` | choices the brief left open |

Other brokers: implement the `Executor` protocol in `nulltrades/execution/base.py` and a `MarketDataSource` in `market_data.py`. Kraken is the only implementation in v1.

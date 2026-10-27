# Decisions

Choices the brief left open, or where it conflicted with the environment. Each has a reason. Items marked **[ASK]** go beyond what the brief allows me to decide alone. They are built the conservative way and need your confirmation.

## Conflicts and items to confirm

**D-01 [ASK] Two dependencies not on the list: `uvicorn` and `PyYAML`.**
FastAPI cannot serve without an ASGI server, and `config.yaml` cannot be read without a YAML parser, so both follow from choices the brief already made. The frontend uses only React, React DOM, TypeScript and Vite (plus `@vitejs/plugin-react` and the React type packages), with no router or chart library.

**D-03 [ASK] Forced tool call is not available on `claude-sonnet-5-5`.**
The brief asks for a forced single call to `submit_decisions`. `claude-sonnet-5-5` returns HTTP 400 for `tool_choice` `{"type": "tool"}` or `{"type": "any"}`. The build uses `tool_choice: auto` with `disable_parallel_tool_use: true`, a `strict: true` tool schema (all fields required, `null` where not applicable), and a prompt instruction to call the tool once. A reply with no tool call, a refusal, or a truncated reply counts as an invalid output, so the 3-strikes rule still applies. The intent holds: the tool is only an output format, and nothing executes without validation and the risk engine. To get a real forced call you would need a model that still accepts forced tool choice; I have not changed the default model.

**D-03b Refusal fallbacks are off.**
The API can re-run a declined request on another model. That would make a different model's output drive trades while the log names the configured model, so it is off. `decisions.model` records the model that actually answered (`response.model`).

**D-25 [ASK] Replays and the model cache.**
The brief says replays use the cache and never call the API. Snapshots built from CSV history never match live snapshots, so a cache-only replay holds on every candle. Default behaviour follows the brief: no API calls, and a cache miss is logged as a hold. It does not count as an invalid output. `--allow-api` is an explicit opt-in that fills `data/replay-cache.db` on the first run; after that, re-running the same replay costs nothing and is deterministic. Remove the flag if you want the brief applied literally.

## Risk and sizing

- **D-08** The reference price for entries is the best ask, since that is what a buy pays. The stop and take-profit distances, reward-to-risk and sizing in the risk engine use the ask. Schema validation ("stop below / take-profit above the current price") uses the last price.
- **D-10** One position per pair. An `open_long` on a pair that already has a position is rejected. Use `update_stop` to manage it.
- **D-11** Hard bounds equal the shipped defaults, so config and the UI can only tighten a limit. Fee rates (0–2%) and order timeout (1–600 s) are ranges, because they describe the account rather than risk appetite. `max_model_calls_per_day` is capped at 48. Live-gate floors are 14 days and 30 trades: config can raise them, never lower them.
- **D-13** Daily loss is the drop in equity since 00:00 Europe/Brussels, unrealised included and floored at zero. Check 12 needs budget left above zero. The kill fires when the loss exceeds the budget.
- **D-14** Cash available for sizing is divided by (1 + limit slippage)(1 + taker fee), so a filled order plus its fee always fits the cash.
- **D-05** One invalid action rejects the whole model output, including any valid actions in it. Simplest and fail-closed; engine-side stops keep running regardless.
- **D-06** After 3 invalid outputs in a row the engine is KILLED. Section 5 says "halt new entries" and section 9 says "kill"; a kill also halts new entries.
- **D-07** A failed model API call (network, auth, 5xx) is logged as a decision and counts as an invalid output (fail closed). An Anthropic outage of three candles therefore kills and flattens. Stops never depend on the model.
- **D-29** The daily call cap counts calls that reached the API. Cache hits are free.

## Execution

- **D-09** The executor interface adds `fetch_order` to the brief's list. Following a resting live order to its final state needs it; the alternative is inferring fills from balances.
- **D-17** Kraken orders carry `cl_ord_id` (`nt` + 16 hex characters, within Kraken's 18-character limit) and `oflags=fciq`, so fees are always charged in EUR and the base amount bought equals the base amount held. Before any retry after a network error, the executor looks the order up by client ID in open and recent closed orders.
- **D-30** Paper orders never rest. A limit the modelled price would not satisfy is cancelled unfilled. Entry limits are best ask + 15 bps, exit limits best bid − 15 bps, and the paper fill (± 10 bps) always falls inside them.
- **D-32** Partial fills are tracked with `positions.amount_open`. A position closes when the remainder is below the exchange minimum; that dust stays in holdings and counts as lost in realised P&L.
- **D-15** LIVE equity and cash are the app's own ledger, built from its fills, not the whole Kraken account. Available cash = min(ledger cash, EUR balance at the last reconciliation, `live_capital_cap_eur` − cost of open positions).
- **D-16** Reconciliation flags shortfalls only: the exchange holding less base, or less EUR, than the app thinks it owns, plus any open order with the `nt` prefix that the app does not know about. Coins or EUR the user holds outside the app are not a mismatch. PAPER compares exactly.
- **D-18** Target conversion sells only holdings the app owns. Dust below the exchange minimum cannot be sold; it is logged and does not count as a failed conversion.

## Engine and modes

- **D-12** Stop pauses model calls and entries. While positions are open the poll loop keeps running, so stops, take-profits, max-hold and kill checks stay active. A stopped engine that silently abandons its stops would be worse.
- **D-19** The app always starts in PAPER, and `mode: live` in config is ignored with a warning. If LIVE positions exist at startup, the dashboard and Telegram warn that their stops are not running until LIVE is re-enabled through the gate. Leaving LIVE with open positions is refused.
- **D-20** Saving settings in LIVE needs a confirmation tick. After saving, Start and new runs are refused until the process restarts.
- **D-21** Settings are written back to `config.yaml` (comments replaced by a standard header), and every change is recorded in `settings_versions`. Together with `decisions.prompt_version` this records which prompt version was live at each point in time.
- **D-22** The Start form creates a run (capital, target ROI, pairs). A new run needs the engine stopped and no open positions. Start and Stop on the dashboard act on the current run.
- **D-28** "The app won't start with no pairs" is read as: the engine won't start. The web app still runs so you can add pairs.
- **D-24** Paper run time for the live gate is time spent RUNNING in PAPER, not calendar time.
- **D-23** A paper reset clears PAPER runs, positions, orders, fills and equity snapshots, and with them the gate progress. Decisions, risk verdicts and events are audit records and stay (decisions and events are append-only, enforced by SQLite triggers).
- **D-26** "Paper net P&L shown on screen" is enforced: the live-gate screen sends back the figure it displayed, and the server checks it matches.
- **D-33** The gate's confirmation field is named `trade_only_key_confirmed` so the no-withdrawal scan can stay strict over every identifier and string in the package.
- **D-27** The snapshot includes a `limits` block (stop range, take-profit cap, fee floor and so on). This goes slightly beyond the brief's snapshot list. It is information for the model only; code enforces the limits either way.

## Other

- **D-02** Built as `nulltrades/` inside this repository, next to the existing unrelated projects, so it shares no code with them.
- **D-34** Time zones use `zoneinfo`, which reads the system tz database. That works on Linux and macOS; Windows would need the `tzdata` package.
- **D-35** The server binds to 127.0.0.1 only. It rejects requests with a foreign `Host` header (DNS rebinding) and any write that is not JSON, so a web page open in the same browser cannot drive it.
- **D-36** Replay market limits are not in the CSVs. Defaults are permissive (min cost €0.50); `data/markets.csv` overrides them per pair. When one candle touches both stop and take-profit, the stop is assumed to have hit first.
- **D-04** Model effort and thinking use the model's defaults. `max_tokens` is 16,000.

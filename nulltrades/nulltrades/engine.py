"""Engine: main loop, state machine, kill switch, target flow, reconciliation.

PAPER and LIVE share this engine. Only the executor differs.

Loop, every POLL_SECONDS while RUNNING (or while STOPPED with positions open,
so stops keep working; see DECISIONS.md D-12):
  quotes -> staleness/error kill checks -> reconciliation -> equity, peak, day
  -> loss/drawdown kill checks -> target -> stop/take-profit/max-hold exits
  -> one decision cycle per closed candle (RUNNING only).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Callable

from . import config as C
from .config import Config
from .decision import DecisionSource, ValidationResult, build_snapshot, load_prompt, snapshot_hash, validate_output
from .execution.base import Executor, new_client_id
from .execution.paper import PaperExecutor
from .market_data import (
    MarketDataSource, closed_candles, indicator_summary, is_stale, last_closed_boundary,
)
from .models import (
    BRUSSELS, LATCHED_STATES, EngineState, MarketInfo, Mode, OrderState, PositionView, Quote,
    base_of, brussels_day_start, ceil_to_step, floor_to_step, iso, parse_iso, utcnow,
)
from .notify import Notifier
from .risk import RiskContext, Verdict, evaluate_actions, exit_reason
from .state import Store, holdings_by_asset

log = logging.getLogger("nulltrades.engine")

CANDLES_IN_SNAPSHOT = 100


class EngineError(Exception):
    pass


class Engine:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        market: MarketDataSource,
        decisions: DecisionSource,
        notifier: Notifier,
        *,
        mode: Mode = Mode.PAPER,
        executor: Executor | None = None,
        clock: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], object] = asyncio.sleep,
        order_poll_seconds: float = 2.0,
        prompt_dir=C.PROMPT_DIR,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.market = market
        self.decisions = decisions
        self.notifier = notifier
        self.mode = mode
        self.clock = clock
        self._sleep = sleep
        self.order_poll_seconds = order_poll_seconds
        self.prompt_dir = prompt_dir

        self.run: dict | None = store.current_run(mode)
        self.state = EngineState.STOPPED
        self.state_reason: str | None = None
        if self.run and EngineState(self.run["state"]) in LATCHED_STATES:
            self.state = EngineState(self.run["state"])
            self.state_reason = self.run["state_reason"]
        elif self.run and self.run["state"] != EngineState.STOPPED.value:
            # The engine always comes up STOPPED.
            self.store.update_run(self.run["id"], state=EngineState.STOPPED.value, state_reason="restart")

        self._external_executor = executor
        self.executor: Executor | None = executor
        if self.executor is None and mode == Mode.PAPER and self.run:
            self.executor = self._make_paper_executor()

        self.quotes: dict[str, Quote] = {}
        self.markets: dict[str, MarketInfo] = {}
        self.consecutive_errors = 0
        self.last_reconcile: dict | None = None
        self.last_error: str | None = None
        self.restart_required = False
        self._needs_reconcile = mode == Mode.LIVE
        self._last_tick: datetime | None = None
        self._last_reconcile_at: datetime | None = None
        self._last_snapshot_at: datetime | None = None
        self._loop_task: asyncio.Task | None = None
        self._decision_task: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self._exiting: set[int] = set()
        self._live_eur_balance: float | None = None

    # ------------------------------------------------------------------ helpers

    @property
    def run_id(self) -> int:
        if not self.run:
            raise EngineError("no run; start one first")
        return self.run["id"]

    @property
    def pairs(self) -> list[str]:
        return list(self.run["pairs"]) if self.run else []

    def _make_paper_executor(self) -> PaperExecutor:
        cash, holdings = self.store.ledger(Mode.PAPER, self.run["id"], self.run["starting_capital"])
        return PaperExecutor(
            lambda p: self.quotes.get(p), cash, holdings_by_asset(holdings),
            self.cfg.risk.fee_taker_pct, self.cfg.risk.paper_slippage_bps,
        )

    def _event(self, kind: str, message: str, data=None) -> None:
        run_id = self.run["id"] if self.run else None
        self.store.event(self.mode, run_id, kind, message, data, now=self.clock())
        log.info("[%s] %s: %s", self.mode.value, kind, message)

    async def _notify(self, text: str) -> None:
        try:
            await self.notifier.send(self.mode, text)
        except Exception:  # alerts must never break the engine
            log.exception("notify failed")

    def _set_state(self, state: EngineState, reason: str | None) -> None:
        self.state = state
        self.state_reason = reason
        if self.run:
            self.store.update_run(self.run["id"], state=state.value, state_reason=reason)
            self.run["state"], self.run["state_reason"] = state.value, reason
        self._event("state", f"{state.value}" + (f": {reason}" if reason else ""))

    def positions(self) -> list[PositionView]:
        return self.store.open_positions(self.mode, self.run_id) if self.run else []

    def ledger(self) -> tuple[float, dict[str, float]]:
        return self.store.ledger(self.mode, self.run_id, self.run["starting_capital"])

    def equity(self) -> tuple[float, float, float]:
        """(equity, cash, positions value). Holdings valued at best bid."""
        cash, holdings = self.ledger()
        value = 0.0
        for pair, amt in holdings.items():
            q = self.quotes.get(pair)
            if q is not None:
                value += amt * q.bid
            else:
                value += sum(p.cost_eur for p in self.positions() if p.pair == pair)
        return cash + value, cash, value

    def available_cash(self, cash: float) -> float:
        if self.mode == Mode.PAPER:
            return max(cash, 0.0)
        deployed = sum(p.cost_eur for p in self.positions())
        room = self.cfg.live_capital_cap_eur - deployed
        eur = self._live_eur_balance if self._live_eur_balance is not None else 0.0
        return max(0.0, min(cash, eur, room))

    def _loss_today(self, equity: float) -> float:
        start = self.run.get("day_start_equity")
        return max(0.0, (start if start is not None else equity) - equity)

    async def refresh_markets(self) -> dict[str, MarketInfo]:
        self.markets = await self.market.markets()
        return self.markets

    # --------------------------------------------------------------- lifecycle

    async def create_run(self, capital: float, target_roi: float, pairs: list[str]) -> dict:
        if self.state == EngineState.RUNNING:
            raise EngineError("stop the engine before starting a new run")
        if self.state in LATCHED_STATES:
            raise EngineError(f"engine is {self.state.value}; reset it first")
        if not pairs:
            raise EngineError("choose at least one pair")
        if self.run and self.positions():
            raise EngineError("close open positions before starting a new run")
        if capital <= 0 or target_roi <= 0:
            raise EngineError("capital and target ROI must be positive")
        if self.mode == Mode.LIVE and capital > self.cfg.live_capital_cap_eur:
            raise EngineError(f"LIVE capital is capped at {self.cfg.live_capital_cap_eur:g} EUR")
        markets = await self.refresh_markets()
        unknown = [p for p in pairs if p not in markets or not markets[p].active]
        if unknown:
            raise EngineError(f"not an active Kraken EUR spot market: {', '.join(unknown)}")
        if self.mode == Mode.LIVE:
            bal = await self.executor.balances()
            if bal.get("EUR", 0.0) < capital:
                raise EngineError(f"LIVE EUR balance {bal.get('EUR', 0.0):.2f} is below capital {capital:.2f}")
        quotes = await self.market.quotes(pairs)
        self.quotes.update(quotes)
        await self._cancel_background()
        now = self.clock()
        run_id = self.store.create_run(self.mode, capital, target_roi, pairs, self.cfg.timeframe,
                                       {p: q.last for p, q in quotes.items()}, now)
        self.run = self.store.current_run(self.mode)
        self.store.update_run(run_id, day_start=iso(brussels_day_start(now)), day_start_equity=capital)
        self.run = self.store.current_run(self.mode)
        if self.mode == Mode.PAPER:
            self.executor = self._make_paper_executor()
        self.state, self.state_reason = EngineState.STOPPED, None
        self._event("run_created", f"run {run_id}: {capital:g} EUR, target ROI {target_roi:g}, "
                                   f"pairs {', '.join(pairs)}")
        return self.run

    async def start(self, run_loop: bool = True) -> None:
        if self.restart_required:
            raise EngineError("settings changed in LIVE: restart the app first")
        if not self.run:
            raise EngineError("no run; use the start form")
        if self.state == EngineState.RUNNING:
            return
        if self.state in LATCHED_STATES:
            raise EngineError(f"engine is {self.state.value}; reset it first")
        if not self.pairs:
            raise EngineError("no pairs configured")
        if self.decisions.decider is None and self.decisions.allow_api:
            raise EngineError("ANTHROPIC_API_KEY is not set")
        await self.refresh_markets()
        if self.mode == Mode.LIVE:
            # After downtime, reconcile before placing any order.
            if not await self.reconcile(self.clock()):
                return
        self._last_tick = None
        self._set_state(EngineState.RUNNING, None)
        await self._notify(f"Engine started. Run {self.run_id}, pairs {', '.join(self.pairs)}, "
                           f"model {self.cfg.model}, prompt {self.cfg.prompt_version}.")
        if run_loop:
            self.ensure_loop()

    async def stop(self) -> None:
        if self.state != EngineState.RUNNING:
            return
        self._set_state(EngineState.STOPPED, "stopped by user")
        if self._decision_task and not self._decision_task.done():
            self._decision_task.cancel()
        await self._notify("Engine stopped by user. Open positions keep their stops while the app runs.")

    async def reset(self) -> None:
        if self.state not in LATCHED_STATES:
            return
        prev = self.state
        self._set_state(EngineState.STOPPED, f"reset from {prev.value}")
        if self.run:
            self.store.update_run(self.run_id, consecutive_invalid=0)
            self.run["consecutive_invalid"] = 0
        self.consecutive_errors = 0

    def ensure_loop(self) -> None:
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.create_task(self._loop())

    def _should_loop(self) -> bool:
        if not self.run:
            return False
        if self.state == EngineState.RUNNING:
            return True
        return self.state == EngineState.STOPPED and bool(self.positions())

    async def _loop(self) -> None:
        while self._should_loop():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # never let the loop die silently
                log.exception("tick failed")
                await self._count_error(f"tick failed: {e}")
            await self._sleep(C.POLL_SECONDS)

    async def shutdown(self) -> None:
        await self._cancel_background(include_loop=True)

    async def _cancel_background(self, include_loop: bool = False) -> None:
        current = asyncio.current_task()
        tasks = set(self._tasks)
        if self._decision_task:
            tasks.add(self._decision_task)
        if include_loop and self._loop_task:
            tasks.add(self._loop_task)
        for t in tasks:
            if t is not current and not t.done():
                t.cancel()
        for t in tasks:
            if t is not current:
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

    def _spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._task_done)
        return t

    def _task_done(self, t: asyncio.Task) -> None:
        self._tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            e = t.exception()
            log.error("background task failed", exc_info=e)
            self.last_error = f"task failed: {e}"
            try:
                self._event("error", f"background task failed: {e}")
            except Exception:
                pass

    async def drain(self) -> None:
        """Wait for in-flight decision and order tasks. Used by tests and replay."""
        while True:
            pending = [t for t in (self._tasks | ({self._decision_task} if self._decision_task else set()))
                       if not t.done()]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)

    # -------------------------------------------------------------------- tick

    async def _count_error(self, msg: str) -> bool:
        """Count an exchange or data error. Returns True if it killed the engine."""
        self.consecutive_errors += 1
        self.last_error = msg
        self._event("error", msg, {"consecutive": self.consecutive_errors})
        if self.consecutive_errors >= C.MAX_CONSECUTIVE_ERRORS and self.state not in LATCHED_STATES:
            await self.kill(f"{C.MAX_CONSECUTIVE_ERRORS} consecutive exchange or data errors (last: {msg})")
            return True
        return False

    async def tick(self) -> None:
        if not self.run or self.state in LATCHED_STATES:
            return
        now = self.clock()
        if self._last_tick is not None:
            gap = (now - self._last_tick).total_seconds()
            if gap > 3 * C.POLL_SECONDS:
                self._needs_reconcile = True
            if self.state == EngineState.RUNNING:
                self.store.add_running_seconds(self.run_id, min(gap, 3 * C.POLL_SECONDS))
        self._last_tick = now

        positions = self.positions()
        pairs = sorted(set(self.pairs) | {p.pair for p in positions})
        try:
            if not self.markets:
                await self.refresh_markets()
            self.quotes.update(await self.market.quotes(pairs))
            self.consecutive_errors = 0
        except Exception as e:
            await self._count_error(f"quotes: {e}")
            return

        stale = [p for p in pairs if is_stale(self.quotes.get(p), now, C.STALE_AFTER_SECONDS)]
        if stale:
            await self.kill(f"price data older than {C.STALE_AFTER_SECONDS} seconds for {', '.join(stale)}")
            return

        due = self._last_reconcile_at is None or (now - self._last_reconcile_at).total_seconds() >= C.RECONCILE_EVERY_SECONDS
        if self._needs_reconcile or due:
            if not await self.reconcile(now):
                return

        equity, cash, _ = self.equity()
        self._track_equity(now, equity, cash)

        L = self.cfg.risk
        budget = L.max_daily_loss_pct / 100 * self.run["starting_capital"]
        loss = self._loss_today(equity)
        if loss > budget:
            await self.kill(f"daily loss budget exceeded ({loss:.2f} > {budget:.2f} EUR)")
            return
        peak = self.run["peak_equity"]
        dd = (peak - equity) / peak * 100 if peak > 0 else 0.0
        if dd >= L.max_drawdown_pct:
            await self.kill(f"drawdown {dd:.1f}% from peak {peak:.2f} reached {L.max_drawdown_pct:g}%")
            return

        if equity >= self.run["target_equity"]:
            await self.target_flow(equity)
            return

        for p in positions:
            q = self.quotes.get(p.pair)
            if q is None or p.id in self._exiting:
                continue
            reason = exit_reason(p, q.bid, now, L)
            if reason:
                self._spawn(self._exit(p, reason))

        if self.state == EngineState.RUNNING:
            boundary = last_closed_boundary(now, self.cfg.timeframe_seconds)
            last = parse_iso(self.run.get("last_decision_candle"))
            busy = self._decision_task is not None and not self._decision_task.done()
            if (last is None or boundary > last) and not busy:
                self.run["last_decision_candle"] = iso(boundary)
                self.store.update_run(self.run_id, last_decision_candle=iso(boundary))
                self._decision_task = asyncio.create_task(self.decision_cycle(boundary))
                self._decision_task.add_done_callback(self._task_done)

    def _track_equity(self, now: datetime, equity: float, cash: float) -> None:
        run = self.run
        updates = {}
        if equity > run["peak_equity"]:
            run["peak_equity"] = equity
            updates["peak_equity"] = equity
        day = iso(brussels_day_start(now))
        if run.get("day_start") != day:
            run["day_start"], run["day_start_equity"] = day, equity
            updates.update(day_start=day, day_start_equity=equity)
        if updates:
            self.store.update_run(run["id"], **updates)
        if self._last_snapshot_at is None or (now - self._last_snapshot_at).total_seconds() >= C.EQUITY_SNAPSHOT_EVERY_SECONDS:
            self.store.snapshot_equity(self.mode, run["id"], now, equity, cash, run["peak_equity"])
            self._last_snapshot_at = now

    # ----------------------------------------------------------- reconciliation

    async def reconcile(self, now: datetime) -> bool:
        """Compare exchange balances and open orders with local state.
        Returns False if the engine was killed."""
        if not self.run or self.executor is None:
            return True
        try:
            await self._settle_unsettled()
            bal = await self.executor.balances()
            exch_open = await self.executor.open_orders()
        except Exception as e:
            await self._count_error(f"reconciliation query failed: {e}")
            return self.state not in LATCHED_STATES
        self._needs_reconcile = False
        self._last_reconcile_at = now
        if self.mode == Mode.LIVE:
            self._live_eur_balance = bal.get("EUR", 0.0)

        cash, holdings = self.ledger()
        equity = cash + sum(a * (self.quotes[p].bid if p in self.quotes else 0.0) for p, a in holdings.items())
        issues: list[str] = []
        value = 0.0
        for asset, amt in holdings_by_asset(holdings).items():
            pair = f"{asset}/EUR"
            actual = bal.get(asset, 0.0)
            step = self.markets[pair].amount_step if pair in self.markets else 1e-8
            short = amt - actual
            if short > step:
                px = self.quotes[pair].bid if pair in self.quotes else 0.0
                value += short * px
                issues.append(f"{asset}: local {amt:g}, exchange {actual:g}")
        eur = bal.get("EUR", 0.0)
        if self.mode == Mode.PAPER:
            diff = abs(eur - cash)
        else:
            diff = max(0.0, cash - eur)  # the account may hold more EUR than the app manages
        if diff > 0.005:
            value += diff
            issues.append(f"EUR: local {cash:.4f}, exchange {eur:.4f}")
        known = {r["exchange_id"] for r in self.store.unsettled_orders(self.mode, self.run_id)}
        for o in exch_open:
            if o.id not in known:
                value += o.remaining * (o.price or 0.0)
                issues.append(f"unknown open order {o.id} {o.side} {o.amount:g} {o.pair}")

        limit = C.RECONCILE_TOLERANCE_PCT / 100 * max(equity, 1e-9)
        self.last_reconcile = {"ts": iso(now), "ok": value <= limit, "mismatch_eur": round(value, 6),
                               "limit_eur": round(limit, 6), "issues": issues}
        if value > limit:
            await self.kill(f"reconciliation mismatch {value:.4f} EUR > {limit:.4f} EUR: " + "; ".join(issues))
            return False
        if issues:
            self._event("reconcile_minor", "; ".join(issues), self.last_reconcile)
        return True

    # ---------------------------------------------------------------- decisions

    def risk_context(self, now: datetime) -> RiskContext:
        positions = self.positions()
        equity, cash, _ = self.equity()
        day0 = brussels_day_start(now)
        L = self.cfg.risk
        return RiskContext(
            now=now,
            engine_running=self.state == EngineState.RUNNING,
            kill_active=self.state == EngineState.KILLED,
            whitelist=frozenset(self.pairs),
            markets=self.markets,
            quotes=dict(self.quotes),
            equity=equity,
            starting_capital=self.run["starting_capital"],
            available_cash=self.available_cash(cash),
            open_positions={p.pair: p for p in positions},
            entries_today=self.store.entries_since(self.mode, self.run_id, day0),
            loss_today=self._loss_today(equity),
            cooldown_until=self.store.cooldowns(self.mode, self.run_id, now, L.cooldown_minutes),
            limits=L,
        )

    async def decision_cycle(self, candle_close: datetime) -> int | None:
        """One model call covering all pairs. Returns the decision row id."""
        now = self.clock()
        cfg, L = self.cfg, self.cfg.risk
        calls = self.store.model_calls_since(self.mode, brussels_day_start(now))
        if calls >= cfg.max_model_calls_per_day:
            self._event("model_cap", f"daily model call cap {cfg.max_model_calls_per_day} reached; no new entries")
            return None
        try:
            fetched = await asyncio.gather(*[
                self.market.candles(p, cfg.timeframe, CANDLES_IN_SNAPSHOT + 1) for p in self.pairs
            ])
        except Exception as e:
            await self._count_error(f"candles: {e}")
            return None
        candles = {
            p: closed_candles(cs, cfg.timeframe_seconds, now)[-CANDLES_IN_SNAPSHOT:]
            for p, cs in zip(self.pairs, fetched)
        }
        positions = self.positions()
        ctx = self.risk_context(now)
        budget = L.max_daily_loss_pct / 100 * self.run["starting_capital"]
        snapshot = build_snapshot(
            ts=now, mode=self.mode, timeframe=cfg.timeframe,
            starting_capital=self.run["starting_capital"], target_equity=self.run["target_equity"],
            equity=ctx.equity, peak_equity=self.run["peak_equity"], candles=candles,
            indicators={p: indicator_summary(cs) for p, cs in candles.items()},
            quotes=self.quotes, markets=self.markets, cash=ctx.available_cash, positions=positions,
            entries_left=max(0, L.max_trades_per_day - ctx.entries_today),
            loss_budget_left=max(0.0, budget - ctx.loss_today),
            fee_maker_pct=L.fee_maker_pct, fee_taker_pct=L.fee_taker_pct,
            limits=self._limits_for_model(),
        )
        try:
            system_prompt = load_prompt(cfg.prompt_version, self.prompt_dir)
        except FileNotFoundError as e:
            await self.kill(str(e))
            return None
        reply, cache_hit, called = await self.decisions.get(
            mode=self.mode, prompt_version=cfg.prompt_version, model=cfg.model,
            system_prompt=system_prompt, snapshot=snapshot,
        )
        if reply.error:
            result = ValidationResult(False, [], [reply.error])
        else:
            result = validate_output(reply.output, frozenset(self.pairs), self.quotes, {p.pair for p in positions})
        decision_id = self.store.insert_decision(
            self.mode, self.run_id, ts=iso(now), candle_ts=iso(candle_close),
            snapshot_hash=snapshot_hash(snapshot), snapshot=snapshot, prompt_version=cfg.prompt_version,
            model=reply.served_model or cfg.model, model_called=int(called), cache_hit=int(cache_hit),
            raw_output=reply.raw or reply.output, valid=int(result.ok), validation_errors=result.errors,
            actions=[asdict(a) for a in result.actions], usage=reply.usage,
        )
        if reply.cache_miss:
            # No decision exists for this snapshot and the API is off (replay):
            # nothing to judge, so it does not count as an invalid output.
            self._event("cache_miss", f"decision {decision_id}: no cached decision; holding")
            return decision_id
        if not result.ok:
            n = int(self.run.get("consecutive_invalid") or 0) + 1
            self.run["consecutive_invalid"] = n
            self.store.update_run(self.run_id, consecutive_invalid=n)
            self._event("invalid_output", f"decision {decision_id} rejected: {'; '.join(result.errors)[:500]}",
                        {"decision_id": decision_id, "consecutive": n})
            if n >= C.MAX_CONSECUTIVE_INVALID:
                await self.kill(f"{C.MAX_CONSECUTIVE_INVALID} consecutive invalid model outputs")
            return decision_id
        if self.run.get("consecutive_invalid"):
            self.run["consecutive_invalid"] = 0
            self.store.update_run(self.run_id, consecutive_invalid=0)

        verdicts = evaluate_actions(result.actions, self.risk_context(self.clock()))
        for v in verdicts:
            self.store.insert_verdict(self.mode, self.run_id, decision_id, self.clock(), v)
            if v.executable:
                await self._execute(v, decision_id)
        return decision_id

    def _limits_for_model(self) -> dict:
        L = self.cfg.risk
        return {
            "min_stop_pct": L.min_stop_pct, "max_stop_pct": L.max_stop_pct,
            "max_take_profit_pct": L.max_take_profit_pct, "min_reward_risk": L.min_reward_risk,
            "min_take_profit_pct_for_fees": round(L.cost_multiple * 2 * L.fee_taker_pct, 4),
            "risk_per_trade_pct": L.risk_per_trade_pct, "max_position_pct": L.max_position_pct,
            "max_open_positions": L.max_open_positions, "max_hold_hours": L.max_hold_hours,
            "cooldown_minutes_after_stop": L.cooldown_minutes,
        }

    async def _execute(self, v: Verdict, decision_id: int) -> None:
        a = v.action
        if a.action == "open_long":
            self._spawn(self._enter(a.pair, v.base_amount, v.limit_price, a.stop_loss, a.take_profit, decision_id))
        elif a.action == "close_position":
            pos = next((p for p in self.positions() if p.pair == a.pair), None)
            if pos and pos.id not in self._exiting:
                self._spawn(self._exit(pos, "model_close", decision_id=decision_id))
        elif a.action == "update_stop":
            pos = next((p for p in self.positions() if p.pair == a.pair), None)
            if pos:
                self.store.update_stop(pos.id, v.new_stop)
                self._event("stop_updated", f"{a.pair} stop {pos.stop_loss:g} -> {v.new_stop:g}",
                            {"decision_id": decision_id, "position_id": pos.id})

    # ------------------------------------------------------------------ orders

    async def _await_final(self, o: OrderState, row_id: int) -> OrderState:
        """Follow an order until it is final. Cancel if still open after timeout."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.cfg.risk.order_timeout_seconds
        while not o.is_final:
            if loop.time() >= deadline:
                try:
                    await self.executor.cancel(o.id, o.pair)
                except Exception as e:
                    self._event("cancel_failed", f"{o.id}: {e}")
                o = await self.executor.fetch_order(o.id, o.pair)
                if not o.is_final:
                    o.status = "canceled"
                break
            await self._sleep(self.order_poll_seconds)
            o = await self.executor.fetch_order(o.id, o.pair)
        return o

    async def _place(self, kind: str, pair: str, amount: float, price: float | None, purpose: str, *,
                     decision_id: int | None = None, position_id: int | None = None,
                     meta: dict | None = None) -> OrderState | None:
        """Submit, follow to a final state, then finalise. Returns the final order."""
        cid = new_client_id()
        now = self.clock()
        side = "buy" if kind == "buy_limit" else "sell"
        type_ = "market" if kind == "market_sell" else "limit"
        row = self.store.insert_order(
            self.mode, self.run_id, client_id=cid, pair=pair, side=side, type_=type_, purpose=purpose,
            amount=amount, price=price, decision_id=decision_id, position_id=position_id, now=now, meta=meta,
        )
        try:
            if kind == "buy_limit":
                o = await self.executor.buy_limit(pair, amount, price, cid)
            elif kind == "sell_limit":
                o = await self.executor.sell_limit(pair, amount, price, cid)
            else:
                o = await self.executor.market_sell(pair, amount, cid)
        except Exception as e:
            self.store.mark_order(row, "rejected", str(e)[:500], self.clock())
            await self._count_error(f"{kind} {pair}: {e}")
            return None
        self.store.update_order(row, o, self.clock())
        if not o.is_final:
            o = await self._await_final(o, row)
        self._finalise(row, o)
        return o

    def _finalise(self, row_id: int, o: OrderState) -> None:
        """Record the fill and apply its effect on positions. Idempotent per order."""
        row = self.store.order_row(row_id)
        now = self.clock()
        already = self.store.one("SELECT 1 FROM fills WHERE order_id = ?", (row_id,))
        self.store.update_order(row_id, o, now)
        if already or o.filled <= 0:
            if o.error:
                self._event("order_unfilled", f"{o.side} {o.pair}: {o.error}", {"order": row_id})
            return
        self.store.insert_fill(self.mode, self.run_id, row_id, o, now)
        meta = json.loads(row["meta"] or "{}")
        if row["purpose"] == "entry":
            pid = self.store.open_position(
                self.mode, self.run_id, pair=o.pair, amount=o.filled, entry_price=o.average,
                cost_eur=o.filled * o.average + o.fee_eur, entry_fee=o.fee_eur,
                stop_loss=meta["stop_loss"], take_profit=meta["take_profit"],
                decision_id=row["decision_id"], prompt_version=self.cfg.prompt_version,
                model=self.cfg.model, now=now,
            )
            self._event("position_opened", f"{o.pair} {o.filled:g} @ {o.average:g}, stop {meta['stop_loss']:g}, "
                                           f"take-profit {meta['take_profit']:g}", {"position_id": pid})
        elif row["position_id"] is not None:
            left = self.store.reduce_position(row["position_id"], o.filled)
            m = self.markets.get(o.pair)
            if left <= (m.amount_step if m else 1e-8) or (m and left < m.min_amount):
                self.store.close_position(row["position_id"], reason=meta.get("reason", row["purpose"]), now=now)
                p = self.store.position(row["position_id"])
                self._event("position_closed", f"{o.pair} {meta.get('reason', row['purpose'])}: "
                                               f"realised {p['realised_pnl']:+.4f} EUR", {"position_id": p["id"]})

    async def _enter(self, pair: str, amount: float, limit_price: float, stop: float, tp: float,
                     decision_id: int) -> None:
        await self._place("buy_limit", pair, amount, limit_price, "entry", decision_id=decision_id,
                          meta={"stop_loss": stop, "take_profit": tp})

    def _sellable(self, pair: str, amount: float) -> float:
        _, holdings = self.ledger()
        m = self.markets.get(pair)
        amt = min(amount, holdings.get(pair, 0.0))
        amt = floor_to_step(amt, m.amount_step) if m else amt
        if m and (amt < m.min_amount or amt * (self.quotes[pair].bid if pair in self.quotes else 0) < m.min_cost):
            return 0.0
        return max(amt, 0.0)

    async def _exit(self, pos: PositionView, reason: str, *, market_only: bool = False,
                    decision_id: int | None = None) -> bool:
        """Close a position: limit within max slippage first, market sell for
        whatever is left after the order timeout. Returns True when closed."""
        if pos.id in self._exiting:
            return False
        self._exiting.add(pos.id)
        try:
            meta = {"reason": reason}
            amount = self._sellable(pos.pair, pos.amount)
            if amount <= 0:
                self.store.close_position(pos.id, reason=f"{reason} (dust below minimum)", now=self.clock())
                self._event("exit_dust", f"{pos.pair}: remaining amount below exchange minimum; closed as dust")
                return True
            if not market_only and pos.pair in self.quotes:
                m = self.markets.get(pos.pair)
                px = self.quotes[pos.pair].bid * (1 - self.cfg.risk.max_slippage_bps / 10_000)
                px = ceil_to_step(px, m.price_step) if m else px
                await self._place("sell_limit", pos.pair, amount, px, f"exit_{reason}",
                                  decision_id=decision_id, position_id=pos.id, meta=meta)
            p = self.store.position(pos.id)
            if p["status"] == "open":
                rest = self._sellable(pos.pair, p["amount_open"])
                if rest > 0:
                    await self._place("market_sell", pos.pair, rest, None, f"exit_{reason}_market",
                                      decision_id=decision_id, position_id=pos.id, meta=meta)
                p = self.store.position(pos.id)
                if p["status"] == "open" and self._sellable(pos.pair, p["amount_open"]) <= 0:
                    self.store.close_position(pos.id, reason=reason, now=self.clock())
                    p = self.store.position(pos.id)
            if p["status"] == "closed":
                await self._notify(f"Closed {pos.pair} ({reason}): realised {p['realised_pnl']:+.2f} EUR.")
                return True
            return False
        finally:
            self._exiting.discard(pos.id)

    async def _settle_unsettled(self) -> None:
        """Bring orders left open by a crash or a cancelled task to their final state."""
        for row in self.store.unsettled_orders(self.mode, self.run_id):
            if not row["exchange_id"]:
                self.store.mark_order(row["id"], "rejected", "never acknowledged", self.clock())
                continue
            o = await self.executor.fetch_order(row["exchange_id"], row["pair"])
            if o.is_final:
                self._finalise(row["id"], o)

    async def _cancel_open_orders(self) -> list[str]:
        problems = []
        try:
            for o in await self.executor.open_orders():
                try:
                    await self.executor.cancel(o.id, o.pair)
                except Exception as e:
                    problems.append(f"cancel {o.id}: {e}")
            await self._settle_unsettled()
        except Exception as e:
            problems.append(f"open orders: {e}")
        return problems

    # -------------------------------------------------------------------- kill

    async def kill(self, reason: str, manual: bool = False) -> None:
        if self.state == EngineState.KILLED:
            return
        if not self.run:
            raise EngineError("no run")
        self._set_state(EngineState.KILLED, ("manual: " if manual else "") + reason)
        await self._cancel_background()
        problems = await self._cancel_open_orders()
        flattened = []
        if self.cfg.flatten_on_kill:
            for p in self.positions():
                try:
                    if await self._exit(p, "kill", market_only=True):
                        flattened.append(p.pair)
                except Exception as e:
                    problems.append(f"flatten {p.pair}: {e}")
        self._event("killed", reason, {"flattened": flattened, "problems": problems})
        msg = f"KILLED: {reason}."
        if flattened:
            msg += f" Flattened: {', '.join(flattened)}."
        if problems:
            msg += f" Problems: {'; '.join(problems)}."
        await self._notify(msg + " Reset required before trading resumes.")

    # ------------------------------------------------------------------ target

    async def target_flow(self, equity: float) -> None:
        self._set_state(EngineState.TARGET_REACHED, f"equity {equity:.2f} reached target {self.run['target_equity']:.2f}")
        await self._notify(f"Target reached: equity {equity:.2f} EUR. Closing all positions.")
        await self._cancel_background()
        problems = await self._cancel_open_orders()
        for _ in range(3):
            positions = self.positions()
            if not positions:
                break
            for p in positions:
                try:
                    await self._exit(p, "target")
                except Exception as e:
                    problems.append(f"close {p.pair}: {e}")
        # Convert remaining holdings the app owns to EUR. Holdings the user
        # keeps outside the app are never touched.
        for _ in range(3):
            left = {p: a for p, a in self.ledger()[1].items() if self._sellable(p, a) > 0}
            if not left:
                break
            for pair, amt in left.items():
                await self._place("market_sell", pair, self._sellable(pair, amt), None, "convert")
        left = {p: a for p, a in self.ledger()[1].items() if self._sellable(p, a) > 0}
        try:
            bal = await self.executor.balances()
            eur = bal.get("EUR", 0.0)
        except Exception as e:
            eur = None
            problems.append(f"balance check: {e}")
        final_equity, cash, _ = self.equity()
        fees = self.store.total_fees(self.mode, self.run_id)
        trades = len(self.store.closed_trades(self.mode, self.run_id))
        self._event("target_conversion", f"EUR balance {eur}", {
            "eur_balance": eur, "ledger_cash": cash, "unconverted": left, "problems": problems})
        if left or eur is None or self.positions():
            self._set_state(EngineState.HALTED, "conversion to EUR failed after retries")
            await self._notify(f"HALTED: conversion to EUR failed. Unconverted: {left or 'unknown'}. "
                               f"Problems: {'; '.join(problems) or 'none'}. Manual check needed.")
            return
        await self._notify(
            f"Target complete. Final equity {final_equity:.2f} EUR, EUR balance {eur:.2f}, "
            f"fees paid {fees:.2f} EUR, {trades} trades. Engine stopped."
        )

    # ---------------------------------------------------------- daily summary

    async def maybe_daily_summary(self) -> None:
        if not self.run or not self.cfg.notifications.daily_summary:
            return
        now = self.clock()
        local = now.astimezone(BRUSSELS)
        day = local.date().isoformat()
        if local.hour < C.DAILY_SUMMARY_HOUR or self.run.get("last_summary_day") == day:
            return
        self.run["last_summary_day"] = day
        self.store.update_run(self.run_id, last_summary_day=day)
        equity, _, _ = self.equity()
        day0 = brussels_day_start(now)
        closed = [t for t in self.store.closed_trades(self.mode, self.run_id)
                  if parse_iso(t["closed_at"]) >= day0]
        pnl = sum(t["realised_pnl"] or 0 for t in closed)
        await self._notify(
            f"Daily summary {day}: state {self.state.value}, equity {equity:.2f} EUR "
            f"(target {self.run['target_equity']:.2f}), {len(closed)} closed today, realised {pnl:+.2f} EUR, "
            f"{len(self.positions())} open."
        )

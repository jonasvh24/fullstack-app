"""HTTP API and static UI. Bound to 127.0.0.1 only.

Requests with a foreign Host header are refused (DNS rebinding), and every
POST/PUT must be JSON (a plain cross-site form cannot send that), so a web
page open in the same browser cannot drive the app.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field, ValidationError

from . import config as C
from .config import Config, Secrets, save_config
from .decision import AnthropicDecider, DecisionSource
from .engine import Engine, EngineError
from .execution.kraken import KrakenExecutor
from .gate import GateRequest, gate_conditions, paper_stats
from .market_data import KrakenPublicData, MarketDataSource
from .models import EngineState, Mode, iso, utcnow
from .notify import Notifier, make_notifier
from .state import Store

log = logging.getLogger("nulltrades.api")

UI_DIST = C.PROJECT_ROOT / "ui" / "dist"
STOPS_WARNING = ("Stops and take-profits are enforced by this app. If the app or this machine is down, "
                 "no stop runs.")


class AppState:
    """Holds the one active engine. The app always starts in PAPER."""

    def __init__(self, cfg: Config, store: Store, secrets: Secrets, market: MarketDataSource,
                 notifier: Notifier, decider=None, kraken_factory=None, config_path: Path | None = None,
                 prompt_dir: Path = C.PROMPT_DIR) -> None:
        self.cfg = cfg
        self.store = store
        self.secrets = secrets
        self.market = market
        self.notifier = notifier
        self.decider = decider
        self.kraken_factory = kraken_factory or (
            lambda: KrakenExecutor(secrets.kraken_key, secrets.kraken_secret))
        self.config_path = config_path
        self.prompt_dir = prompt_dir
        self.restart_required = False
        self.startup_warnings: list[str] = []
        if cfg.mode == "live":
            self.startup_warnings.append("config.yaml says mode: live; the app always starts in PAPER.")
        self.engine = self._engine(Mode.PAPER)
        self._bg: asyncio.Task | None = None

    def _engine(self, mode: Mode, executor=None) -> Engine:
        return Engine(self.cfg, self.store, self.market, DecisionSource(self.store, self.decider),
                      self.notifier, mode=mode, executor=executor, prompt_dir=self.prompt_dir)

    @property
    def mode(self) -> Mode:
        return self.engine.mode

    async def startup(self) -> None:
        self.store.record_settings(Mode.PAPER, self.cfg.model_dump(), "startup")
        self.store.event(Mode.PAPER, None, "app_start", "app started in PAPER")
        try:
            await self.engine.refresh_markets()
        except Exception as e:
            self.startup_warnings.append(f"could not load Kraken markets: {e}")
        if self.engine.run and self.engine.positions():
            self.engine.ensure_loop()  # keep paper stops working
        live = self.store.current_run(Mode.LIVE)
        if live and self.store.open_positions(Mode.LIVE, live["id"]):
            msg = ("LIVE positions are open but the app started in PAPER: their stops are not running. "
                   "Switch to LIVE to resume protection.")
            self.startup_warnings.append(msg)
            await self.notifier.send(Mode.LIVE, msg)
        self._bg = asyncio.create_task(self._background())

    async def shutdown(self) -> None:
        if self._bg:
            self._bg.cancel()
        await self.engine.shutdown()

    async def _background(self) -> None:
        while True:
            try:
                await self.engine.maybe_daily_summary()
            except Exception:
                log.exception("daily summary failed")
            await asyncio.sleep(60)

    # --- mode -----------------------------------------------------------------

    async def check_gate(self, req: GateRequest, query_balance: bool) -> tuple[dict, Any]:
        executor, ok, err = None, None, None
        if query_balance:
            if not (self.secrets.kraken_key and self.secrets.kraken_secret):
                ok, err = False, "KRAKEN_API_KEY / KRAKEN_API_SECRET not set"
            else:
                try:
                    executor = self.kraken_factory()
                    await executor.balances()
                    ok = True
                except Exception as e:
                    ok, err = False, f"balance query failed: {type(e).__name__}"
        return gate_conditions(self.cfg, self.store, req, ok, err), executor

    async def switch_mode(self, target: Mode, req: GateRequest) -> dict:
        if target == self.mode:
            return {"mode": target.value}
        if self.engine.state == EngineState.RUNNING:
            raise EngineError("stop the engine before switching mode")
        if target == Mode.PAPER:
            if self.engine.run and self.engine.positions():
                raise EngineError("LIVE positions are open: close them (or kill with flatten) before leaving LIVE")
            await self.engine.shutdown()
            self.engine = self._engine(Mode.PAPER)
            self.store.event(Mode.PAPER, None, "mode", "switched to PAPER")
            return {"mode": "PAPER"}
        gate, executor = await self.check_gate(req, query_balance=True)
        if not gate["ok"]:
            raise GateRefused(gate)
        await self.engine.shutdown()
        self.engine = self._engine(Mode.LIVE, executor)
        await self.engine.refresh_markets()
        self.store.event(Mode.LIVE, None, "mode", "switched to LIVE; gate passed", gate)
        self.store.record_settings(Mode.LIVE, self.cfg.model_dump(), "live enabled")
        if self.engine.run and self.engine.positions():
            if await self.engine.reconcile(utcnow()):
                self.engine.ensure_loop()
        await self.notifier.send(Mode.LIVE, "LIVE mode enabled. Real money.")
        return {"mode": "LIVE"}


class GateRefused(Exception):
    def __init__(self, gate: dict) -> None:
        super().__init__("live gate not passed")
        self.gate = gate


# --- request bodies ----------------------------------------------------------

class RunIn(BaseModel):
    capital: float = Field(gt=0)
    target_roi: float = Field(gt=0)
    pairs: list[str]


class ModeIn(BaseModel):
    mode: str
    phrase: str = ""
    trade_only_key_confirmed: bool = False
    acknowledged_paper_pnl_eur: float | None = None


class ConfirmIn(BaseModel):
    confirm: str = ""


class SettingsIn(BaseModel):
    settings: dict
    confirm_restart: bool = False


# --- helpers -----------------------------------------------------------------

def _label(mode: Mode) -> str:
    return "PAPER (SIMULATED)" if mode == Mode.PAPER else "LIVE"


def _num(x: float | None, nd: int = 6) -> float | None:
    return None if x is None else round(x, nd)


def status_payload(s: AppState) -> dict:
    e = s.engine
    out: dict[str, Any] = {
        "mode": e.mode.value,
        "label": _label(e.mode),
        "state": e.state.value,
        "state_reason": e.state_reason,
        "restart_required": s.restart_required or e.restart_required,
        "timezone": "Europe/Brussels",
        "warnings": [STOPS_WARNING, *s.startup_warnings],
        "secrets": s.secrets.presence(),
        "model": s.cfg.model,
        "prompt_version": s.cfg.prompt_version,
        "timeframe": s.cfg.timeframe,
        "last_error": e.last_error,
        "last_reconcile": e.last_reconcile,
        "run": None,
        "positions": [],
        "last_decision": None,
    }
    if not s.secrets.anthropic_key:
        out["warnings"].append("ANTHROPIC_API_KEY is not set: the engine cannot start.")
    if not (s.secrets.telegram_token and s.secrets.telegram_chat):
        out["warnings"].append("Telegram is not configured: alerts go to the log only.")
    if e.state == EngineState.STOPPED and e.run and e.positions():
        out["warnings"].append("Engine is stopped with open positions: stops still run, no new entries.")
    if not e.run:
        return out
    equity, cash, value = e.equity()
    run = e.run
    peak = max(run["peak_equity"], equity)
    out["run"] = {
        "id": run["id"], "created_at": run["created_at"], "pairs": run["pairs"],
        "starting_capital": run["starting_capital"], "target_roi": run["target_roi"],
        "target_equity": run["target_equity"], "equity": _num(equity, 4), "cash": _num(cash, 4),
        "positions_value": _num(value, 4), "peak_equity": _num(peak, 4),
        "drawdown_pct": _num((peak - equity) / peak * 100 if peak > 0 else 0, 2),
        "progress_pct": _num((equity - run["starting_capital"]) / (run["target_equity"] - run["starting_capital"]) * 100, 1),
        "fees_eur": _num(s.store.total_fees(e.mode, run["id"]), 4),
    }
    for p in e.positions():
        q = e.quotes.get(p.pair)
        bid = q.bid if q else None
        out["positions"].append({
            "id": p.id, "pair": p.pair, "amount": p.amount, "entry_price": p.entry_price,
            "stop_loss": p.stop_loss, "take_profit": p.take_profit, "opened_at": iso(p.opened_at),
            "bid": bid, "unrealised_pnl": _num(p.amount * bid - p.cost_eur, 4) if bid else None,
            "age_hours": round(p.age_hours(utcnow()), 2),
        })
    d = s.store.one("SELECT * FROM decisions WHERE mode = ? ORDER BY id DESC LIMIT 1", (e.mode.value,))
    if d:
        out["last_decision"] = decision_row(s.store, d)
    return out


def decision_row(store: Store, d: dict) -> dict:
    verdicts = store.rows("SELECT * FROM risk_verdicts WHERE decision_id = ? ORDER BY action_index", (d["id"],))
    orders = store.rows("SELECT id, pair, side, type, purpose, amount, price, status, filled, average, fee, error "
                        "FROM orders WHERE decision_id = ? ORDER BY id", (d["id"],))
    actions = json.loads(d["actions"] or "[]")
    return {
        "id": d["id"], "mode": d["mode"], "ts": d["ts"], "candle_ts": d["candle_ts"],
        "prompt_version": d["prompt_version"], "model": d["model"], "cache_hit": bool(d["cache_hit"]),
        "snapshot_hash": d["snapshot_hash"], "valid": bool(d["valid"]),
        "validation_errors": json.loads(d["validation_errors"] or "[]"),
        "actions": actions,
        "pairs": sorted({a.get("pair") for a in actions if a.get("pair")}),
        "verdicts": [{**v, "reasons": json.loads(v["reasons"])} for v in verdicts],
        "orders": orders,
        "has_rejection": (not d["valid"]) or any(v["verdict"] == "rejected" for v in verdicts),
    }


def model_output_text(d: dict) -> str:
    """The submit_decisions input for display, extracted from the raw reply."""
    raw = d.get("raw_output") or ""
    try:
        obj = json.loads(raw)
    except ValueError:
        return raw
    if isinstance(obj, dict) and "content" in obj:
        for b in obj.get("content", []):
            if b.get("type") == "tool_use":
                return json.dumps(b.get("input"), indent=1)
    return json.dumps(obj, indent=1)


def performance(store: Store, mode: Mode, run: dict | None, quotes: dict) -> dict:
    if not run:
        return {"mode": mode.value, "label": _label(mode), "run": None}
    eq = store.rows("SELECT ts, equity, peak, drawdown_pct FROM equity_snapshots WHERE mode = ? AND run_id = ? "
                    "ORDER BY id", (mode.value, run["id"]))
    trades = store.closed_trades(mode, run["id"])
    pnl = [t["realised_pnl"] or 0.0 for t in trades]
    wins = [x for x in pnl if x > 0]
    losses = [x for x in pnl if x <= 0]
    capital = run["starting_capital"]
    bh = None
    starts = run["start_prices"]
    if starts and all(p in quotes for p in starts):
        share = capital / len(starts)
        bh = sum(share * quotes[p].last / px for p, px in starts.items() if px)
    return {
        "mode": mode.value, "label": _label(mode), "run": run["id"],
        "equity_curve": eq,
        "max_drawdown_pct": max((r["drawdown_pct"] for r in eq), default=0.0),
        "trades": len(trades),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1) if trades else None,
        "avg_win_eur": round(sum(wins) / len(wins), 4) if wins else None,
        "avg_loss_eur": round(sum(losses) / len(losses), 4) if losses else None,
        "expectancy_eur": round(sum(pnl) / len(pnl), 4) if pnl else None,
        "net_pnl_eur": round(sum(pnl), 4),
        "total_fees_eur": round(store.total_fees(mode, run["id"]), 4),
        "buy_and_hold_equity": round(bh, 4) if bh is not None else None,
        "buy_and_hold_note": "Equal weight across the run's pairs from run start, before fees.",
        "starting_capital": capital,
    }


def csv_response(rows: list[dict], cols: list[str], mode: Mode, name: str) -> Response:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([f"# nulltrades export: {_label(mode)}" + (" — not real money" if mode == Mode.PAPER else "")])
    w.writerow(["label", *cols])
    for r in rows:
        w.writerow([_label(mode), *[r.get(c) for c in cols]])
    tag = "PAPER-SIMULATED" if mode == Mode.PAPER else "LIVE"
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="nulltrades-{tag}-{name}.csv"'})


# --- app ---------------------------------------------------------------------

def create_app(state: AppState, serve_ui: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await state.startup()
        try:
            yield
        finally:
            await state.shutdown()

    app = FastAPI(title="nulltrades", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.s = state
    allowed_hosts = {f"127.0.0.1:{state.cfg.port}", f"localhost:{state.cfg.port}", "testserver"}

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = request.headers.get("host", "")
        if host not in allowed_hosts:
            return JSONResponse({"error": "forbidden host"}, status_code=403)
        if request.method in ("POST", "PUT", "DELETE"):
            if not request.headers.get("content-type", "").startswith("application/json"):
                return JSONResponse({"error": "JSON body required"}, status_code=415)
        return await call_next(request)

    def eng() -> Engine:
        return state.engine

    async def guarded(coro):
        try:
            return await coro
        except EngineError as e:
            raise HTTPException(409, str(e))

    @app.get("/api/status")
    async def status():
        return status_payload(state)

    @app.post("/api/engine/start")
    async def start(_: dict | None = None):
        await guarded(eng().start())
        return status_payload(state)

    @app.post("/api/engine/stop")
    async def stop(_: dict | None = None):
        await guarded(eng().stop())
        return status_payload(state)

    @app.post("/api/engine/kill")
    async def kill(_: dict | None = None):
        if not eng().run:
            raise HTTPException(409, "no run to kill")
        await guarded(eng().kill("kill button", manual=True))
        return status_payload(state)

    @app.post("/api/engine/reset")
    async def reset(_: dict | None = None):
        await guarded(eng().reset())
        return status_payload(state)

    @app.post("/api/runs")
    async def create_run(body: RunIn):
        if state.restart_required:
            raise HTTPException(409, "settings changed in LIVE: restart the app first")
        pairs = [p.strip().upper() for p in body.pairs]
        bad = [p for p in pairs if not C.PAIR_RE.match(p)]
        if bad:
            raise HTTPException(422, f"not EUR spot pairs: {', '.join(bad)}")
        await guarded(eng().create_run(body.capital, body.target_roi, pairs))
        return status_payload(state)

    @app.get("/api/markets")
    async def markets(pairs: str = ""):
        try:
            ms = await eng().refresh_markets()
        except Exception as e:
            raise HTTPException(503, f"could not load Kraken markets: {e}")
        wanted = [p for p in pairs.split(",") if p] or state.cfg.pairs
        quotes = {}
        try:
            quotes = await state.market.quotes([p for p in wanted if p in ms])
        except Exception:
            pass
        L = state.cfg.risk
        capital = eng().run["starting_capital"] if eng().run else state.cfg.starting_capital_eur
        max_size = min(capital * L.max_position_pct / 100, capital * L.risk_per_trade_pct / L.min_stop_pct)
        typical = min(capital * L.max_position_pct / 100, capital * L.risk_per_trade_pct / L.max_stop_pct)
        rows = []
        for p in wanted:
            m = ms.get(p)
            q = quotes.get(p)
            min_eur = None
            if m and q:
                min_eur = max(m.min_cost, m.min_amount * q.ask)
            rows.append({
                "pair": p, "known": m is not None, "active": bool(m and m.active),
                "min_amount": m.min_amount if m else None, "min_cost_eur": m.min_cost if m else None,
                "price": q.ask if q else None, "min_order_eur": _num(min_eur, 4),
                "max_allowed_size_eur": round(max_size, 2), "size_at_widest_stop_eur": round(typical, 2),
                "fits": None if min_eur is None else min_eur <= typical,
            })
        return {"pairs": rows, "all": sorted(ms)}

    @app.get("/api/settings")
    async def get_settings():
        return {
            "settings": state.cfg.model_dump(),
            "bounds": C.RISK_BOUNDS,
            "prompt_versions": sorted(p.stem for p in state.prompt_dir.glob("v*.md")),
            "timeframes": list(C.TIMEFRAMES),
            "mode": state.mode.value,
            "live_requires_restart": state.mode == Mode.LIVE,
        }

    @app.put("/api/settings")
    async def put_settings(body: SettingsIn):
        if eng().state == EngineState.RUNNING:
            raise HTTPException(409, "stop the engine before changing settings")
        if state.mode == Mode.LIVE and not body.confirm_restart:
            raise HTTPException(409, "changing settings in LIVE needs confirmation and an app restart")
        merged = state.cfg.model_dump()
        for k, v in body.settings.items():
            if k in ("risk", "live_gate", "notifications") and isinstance(v, dict):
                merged[k] = {**merged[k], **v}
            elif k in ("mode", "live_capital_cap_eur", "live_gate", "port"):
                raise HTTPException(422, f"{k} is set in config.yaml only")
            else:
                merged[k] = v
        try:
            new = Config.model_validate(merged)
        except ValidationError as e:
            raise HTTPException(422, "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()))
        if not (state.prompt_dir / f"{new.prompt_version}.md").exists():
            raise HTTPException(422, f"prompt {new.prompt_version} not found")
        save_config(new, state.config_path)
        state.cfg = new
        eng().cfg = new
        reason = "settings changed in LIVE (restart required)" if state.mode == Mode.LIVE else "settings changed"
        state.store.record_settings(state.mode, new.model_dump(), reason)
        state.store.event(state.mode, eng().run["id"] if eng().run else None, "settings", reason,
                          {"changed": body.settings})
        if state.mode == Mode.LIVE:
            state.restart_required = True
            eng().restart_required = True
        return await get_settings()

    @app.get("/api/decisions")
    async def decisions(rejections: int = 0, limit: int = 200):
        rows = state.store.rows("SELECT * FROM decisions WHERE mode = ? ORDER BY id DESC LIMIT ?",
                                (state.mode.value, min(max(limit, 1), 1000) * (4 if rejections else 1)))
        out = []
        for d in rows:
            r = decision_row(state.store, d)
            if rejections and not r["has_rejection"]:
                continue
            r["model_output"] = model_output_text(d)
            out.append(r)
        return {"mode": state.mode.value, "label": _label(state.mode), "decisions": out[:limit]}

    @app.get("/api/trades")
    async def trades():
        m = state.mode
        fills = state.store.rows(
            "SELECT f.*, o.purpose FROM fills f JOIN orders o ON o.id = f.order_id WHERE f.mode = ? "
            "ORDER BY f.id DESC LIMIT 500", (m.value,))
        closed = state.store.rows("SELECT * FROM positions WHERE mode = ? AND status = 'closed' "
                                  "ORDER BY closed_at DESC LIMIT 500", (m.value,))
        return {"mode": m.value, "label": _label(m), "fills": fills, "closed": closed}

    @app.get("/api/performance")
    async def perf():
        return performance(state.store, state.mode, eng().run, eng().quotes)

    @app.get("/api/events")
    async def events(limit: int = 100):
        return {"events": state.store.rows("SELECT * FROM events WHERE mode = ? ORDER BY id DESC LIMIT ?",
                                           (state.mode.value, min(limit, 1000)))}

    @app.get("/api/live-gate")
    async def live_gate():
        # The screen that calls this displays the paper P&L, so that condition is met by
        # display; POST /api/mode re-checks it against the figure the UI sends back.
        pnl = paper_stats(state.store)["paper_net_pnl_eur"]
        gate, _ = await state.check_gate(GateRequest(acknowledged_paper_pnl_eur=pnl), query_balance=False)
        return {**gate, "phrase": C.LIVE_CONFIRMATION_PHRASE, "mode": state.mode.value,
                "live_capital_cap_eur": state.cfg.live_capital_cap_eur,
                "key_permissions": ["Query funds", "Query open orders and trades", "Query closed orders and trades",
                                    "Create and modify orders", "Cancel and close orders"],
                "label": "PAPER (SIMULATED)"}

    @app.post("/api/mode")
    async def set_mode(body: ModeIn):
        try:
            target = Mode(body.mode.upper())
        except ValueError:
            raise HTTPException(422, "mode must be PAPER or LIVE")
        req = GateRequest(body.phrase, body.trade_only_key_confirmed, body.acknowledged_paper_pnl_eur)
        try:
            await state.switch_mode(target, req)
        except GateRefused as g:
            return JSONResponse({"error": "live gate not passed", "gate": g.gate}, status_code=403)
        except EngineError as e:
            raise HTTPException(409, str(e))
        return status_payload(state)

    @app.post("/api/paper/reset")
    async def reset_paper(body: ConfirmIn):
        if body.confirm != "RESET PAPER":
            raise HTTPException(422, 'type "RESET PAPER" to confirm')
        if state.mode != Mode.PAPER:
            raise HTTPException(409, "switch to PAPER first")
        await state.engine.shutdown()
        state.store.reset_paper()
        state.store.event(Mode.PAPER, None, "paper_reset", "paper state cleared by user")
        state.engine = state._engine(Mode.PAPER)
        return status_payload(state)

    @app.get("/api/export/decisions.csv")
    async def export_decisions():
        rows = state.store.rows("SELECT * FROM decisions WHERE mode = ? ORDER BY id", (state.mode.value,))
        for r in rows:
            r["model_output"] = model_output_text(r)
            v = state.store.rows("SELECT verdict, reasons FROM risk_verdicts WHERE decision_id = ?", (r["id"],))
            r["verdicts"] = json.dumps([{"verdict": x["verdict"], "reasons": json.loads(x["reasons"])} for x in v])
        cols = ["id", "ts", "candle_ts", "prompt_version", "model", "cache_hit", "snapshot_hash", "valid",
                "validation_errors", "model_output", "verdicts"]
        return csv_response(rows, cols, state.mode, "decisions")

    @app.get("/api/export/trades.csv")
    async def export_trades():
        rows = state.store.closed_trades(state.mode)
        cols = ["id", "run_id", "pair", "opened_at", "closed_at", "amount", "entry_price", "exit_price",
                "cost_eur", "proceeds_eur", "entry_fee", "exit_fee", "realised_pnl", "exit_reason",
                "prompt_version", "model"]
        return csv_response(rows, cols, state.mode, "trades")

    if serve_ui:
        @app.get("/{path:path}", include_in_schema=False)
        async def ui(path: str):
            if path.startswith("api/"):
                raise HTTPException(404)
            target = (UI_DIST / path).resolve()
            if path and target.is_file() and UI_DIST.resolve() in target.parents:
                return FileResponse(target)
            index = UI_DIST / "index.html"
            if index.exists():
                return FileResponse(index)
            return HTMLResponse("<h1>nulltrades</h1><p>UI not built. Run <code>./start.sh</code> "
                                "or <code>cd ui && npm install && npm run build</code>.</p>")

    return app


def build_state(config_path: Path | None = None, db_path: Path | None = None) -> AppState:
    cfg = C.load_config(config_path)
    secrets = Secrets()
    store = Store(db_path or C.DEFAULT_DB_PATH)
    decider = AnthropicDecider(secrets.anthropic_key) if secrets.anthropic_key else None
    return AppState(cfg, store, secrets, KrakenPublicData(), make_notifier(secrets, cfg.notifications.telegram),
                    decider, config_path=config_path or C.DEFAULT_CONFIG_PATH)

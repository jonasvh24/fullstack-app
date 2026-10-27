from __future__ import annotations

import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from nulltrades.api import AppState, create_app
from nulltrades.config import Config, LiveGateConfig, Secrets
from nulltrades.models import Mode, iso, utcnow
from nulltrades.notify import Notifier
from nulltrades.state import Store

from .helpers import FakeDecider, FakeMarket, config, open_long

PHRASE = "I UNDERSTAND THIS USES REAL MONEY"


class FakeKraken:
    mode = Mode.LIVE

    def __init__(self, fail=False):
        self.fail = fail

    async def balances(self):
        if self.fail:
            raise RuntimeError("EAPI:Invalid key")
        return {"EUR": 50.0}

    async def open_orders(self):
        return []


class Clock:
    def __call__(self):
        return utcnow()


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    def _make(cfg: Config | None = None, kraken_fail=False, with_keys=True, outputs=None):
        for k in ("KRAKEN_API_KEY", "KRAKEN_API_SECRET", "ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN",
                  "TELEGRAM_CHAT_ID"):
            monkeypatch.delenv(k, raising=False)
        if with_keys:
            monkeypatch.setenv("KRAKEN_API_KEY", "k")
            monkeypatch.setenv("KRAKEN_API_SECRET", "s")
            monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
        cfg = cfg or config()
        cfg.port = 8765
        store = Store(tmp_path / "api.db")
        market = FakeMarket(Clock(), {"BTC/EUR": 100.0, "ETH/EUR": 100.0})
        state = AppState(cfg, store, Secrets(), market, Notifier(), decider=FakeDecider(outputs),
                         kraken_factory=lambda: FakeKraken(kraken_fail), config_path=tmp_path / "config.yaml")
        client = TestClient(create_app(state, serve_ui=False), base_url="http://127.0.0.1:8765")
        return client, state
    return _make


def post(c, path, body=None):
    return c.post(path, json=body or {})


def seed_paper_history(store: Store, days=15, trades=31, pnl=0.5):
    run = store.create_run(Mode.PAPER, 20, 1, ["BTC/EUR"], "1h", {"BTC/EUR": 100}, utcnow())
    store.update_run(run, running_seconds=days * 86400)
    for i in range(trades):
        pid = store.open_position(Mode.PAPER, run, pair="BTC/EUR", amount=0.01, entry_price=100, cost_eur=1.0,
                                  entry_fee=0.004, stop_loss=95, take_profit=110, decision_id=None,
                                  prompt_version="v1", model="m", now=utcnow() - timedelta(hours=2))
        store._update("positions", pid, {"status": "closed", "closed_at": iso(utcnow()), "realised_pnl": pnl / trades,
                                         "exit_fee": 0.004, "proceeds_eur": 1.0 + pnl / trades,
                                         "exit_reason": "take_profit"})
    store.update_run(run, ended_at=iso(utcnow()))


# --- basics ------------------------------------------------------------------

def test_starts_in_paper_even_if_config_says_live(make_client):
    cfg = config()
    cfg.mode = "live"
    c, state = make_client(cfg)
    with c:
        s = c.get("/api/status").json()
        assert s["mode"] == "PAPER" and s["label"] == "PAPER (SIMULATED)"
        assert s["state"] == "STOPPED"
        assert any("always starts in PAPER" in w for w in s["warnings"])
        assert any("enforced by this app" in w for w in s["warnings"])


def test_foreign_host_refused(make_client):
    c, _ = make_client()
    with c:
        r = c.get("/api/status", headers={"host": "evil.example:8765"})
        assert r.status_code == 403


def test_non_json_post_refused(make_client):
    c, _ = make_client()
    with c:
        r = c.post("/api/engine/kill", content="x=1", headers={"content-type": "application/x-www-form-urlencoded"})
        assert r.status_code == 415


def test_engine_will_not_start_without_pairs_or_run(make_client):
    c, _ = make_client()
    with c:
        assert post(c, "/api/engine/start").status_code == 409
        r = post(c, "/api/runs", {"capital": 20, "target_roi": 1.0, "pairs": []})
        assert r.status_code == 409 and "pair" in r.json()["detail"]


def test_run_start_and_kill_flattens_within_10s(make_client):
    c, state = make_client(outputs=[open_long()])
    with c:
        r = post(c, "/api/runs", {"capital": 20, "target_roi": 1.0, "pairs": ["BTC/EUR"]})
        assert r.status_code == 200, r.text
        assert post(c, "/api/engine/start").json()["state"] == "RUNNING"
        deadline = time.time() + 5
        while time.time() < deadline and not state.engine.positions():
            time.sleep(0.05)
        assert state.engine.positions(), "first decision cycle did not open a position"
        t0 = time.time()
        s = post(c, "/api/engine/kill").json()
        assert time.time() - t0 < 10
        assert s["state"] == "KILLED" and s["positions"] == []
        assert post(c, "/api/engine/start").status_code == 409
        assert post(c, "/api/engine/reset").json()["state"] == "STOPPED"


def test_exports_are_labelled_paper(make_client):
    c, _ = make_client()
    with c:
        for path in ("/api/export/decisions.csv", "/api/export/trades.csv"):
            r = c.get(path)
            assert "PAPER-SIMULATED" in r.headers["content-disposition"]
            assert "PAPER (SIMULATED)" in r.text.splitlines()[0]
        assert c.get("/api/performance").json()["label"] == "PAPER (SIMULATED)"
        assert c.get("/api/decisions").json()["label"] == "PAPER (SIMULATED)"
        assert c.get("/api/trades").json()["label"] == "PAPER (SIMULATED)"


def test_paper_reset_requires_confirmation(make_client):
    c, state = make_client()
    with c:
        post(c, "/api/runs", {"capital": 20, "target_roi": 1.0, "pairs": ["BTC/EUR"]})
        assert post(c, "/api/paper/reset", {"confirm": "yes"}).status_code == 422
        assert post(c, "/api/paper/reset", {"confirm": "RESET PAPER"}).json()["run"] is None
        assert state.store.rows("SELECT * FROM events WHERE kind = 'paper_reset'")


def test_settings_cannot_loosen_limits(make_client):
    c, _ = make_client(Config(pairs=["BTC/EUR"]))
    with c:
        r = c.put("/api/settings", json={"settings": {"risk": {"risk_per_trade_pct": 5}}})
        assert r.status_code == 422 and "hard ceiling" in r.json()["detail"]
        r = c.put("/api/settings", json={"settings": {"risk": {"risk_per_trade_pct": 1}}})
        assert r.status_code == 200
        assert r.json()["settings"]["risk"]["risk_per_trade_pct"] == 1
        r = c.put("/api/settings", json={"settings": {"live_capital_cap_eur": 1000}})
        assert r.status_code == 422


def test_markets_report_minimum_fit(make_client):
    c, _ = make_client()
    with c:
        rows = c.get("/api/markets?pairs=BTC/EUR").json()["pairs"]
        assert rows[0]["pair"] == "BTC/EUR" and rows[0]["fits"] is True


# --- live gate ---------------------------------------------------------------

def full_request(pnl):
    return {"mode": "LIVE", "phrase": PHRASE, "trade_only_key_confirmed": True,
            "acknowledged_paper_pnl_eur": pnl}


def test_live_refused_on_fresh_install(make_client):
    c, _ = make_client()
    with c:
        r = post(c, "/api/mode", full_request(0.0))
        assert r.status_code == 403
        failed = {x["key"] for x in r.json()["gate"]["conditions"] if not x["ok"]}
        assert {"paper_days", "paper_trades"} <= failed
        assert c.get("/api/status").json()["mode"] == "PAPER"


@pytest.mark.parametrize("break_it,key", [
    (lambda r: r.update(phrase="i understand this uses real money"), "phrase"),
    (lambda r: r.update(trade_only_key_confirmed=False), "trade_only_key"),
    (lambda r: r.update(acknowledged_paper_pnl_eur=None), "paper_pnl_shown"),
    (lambda r: r.update(acknowledged_paper_pnl_eur=99.0), "paper_pnl_shown"),
])
def test_live_refused_unless_every_condition_holds(make_client, break_it, key):
    c, state = make_client()
    seed_paper_history(state.store)
    with c:
        req = full_request(0.5)
        break_it(req)
        r = post(c, "/api/mode", req)
        assert r.status_code == 403
        assert [x["key"] for x in r.json()["gate"]["conditions"] if not x["ok"]] == [key]


def test_live_refused_when_balance_query_fails(make_client):
    c, state = make_client(kraken_fail=True)
    seed_paper_history(state.store)
    with c:
        r = post(c, "/api/mode", full_request(0.5))
        assert r.status_code == 403
        assert [x["key"] for x in r.json()["gate"]["conditions"] if not x["ok"]] == ["balance_query"]


def test_live_refused_without_keys(make_client):
    c, state = make_client(with_keys=False)
    seed_paper_history(state.store)
    with c:
        assert post(c, "/api/mode", full_request(0.5)).status_code == 403


@pytest.mark.parametrize("days,trades", [(13.9, 31), (15, 29)])
def test_live_refused_below_paper_thresholds(make_client, days, trades):
    c, state = make_client()
    seed_paper_history(state.store, days=days, trades=trades)
    with c:
        assert post(c, "/api/mode", full_request(0.5)).status_code == 403


def test_live_enabled_when_every_condition_holds(make_client):
    c, state = make_client()
    seed_paper_history(state.store)
    with c:
        gate = c.get("/api/live-gate").json()
        assert gate["paper_net_pnl_eur"] == 0.5 and gate["label"] == "PAPER (SIMULATED)"
        r = post(c, "/api/mode", full_request(gate["paper_net_pnl_eur"]))
        assert r.status_code == 200, r.text
        assert r.json()["mode"] == "LIVE" and r.json()["label"] == "LIVE"
        # Back to PAPER needs no gate.
        assert post(c, "/api/mode", {"mode": "PAPER"}).json()["mode"] == "PAPER"


def test_gate_floors_cannot_be_lowered_in_config():
    with pytest.raises(ValueError):
        LiveGateConfig(paper_days=1)
    with pytest.raises(ValueError):
        LiveGateConfig(paper_trades=5)

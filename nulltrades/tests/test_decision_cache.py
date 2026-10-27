from __future__ import annotations

import asyncio

from nulltrades.decision import DecisionSource, cache_key, snapshot_hash
from nulltrades.models import Mode
from nulltrades.state import Store

from .helpers import FakeDecider

SNAP = {"timestamp": "2026-10-27T10:00:00+00:00", "pairs": {"BTC/EUR": {"last_price": 100.0}}}


def get(src, snap=SNAP, prompt="v1", model="claude-sonnet-5-5"):
    return asyncio.run(src.get(mode=Mode.PAPER, prompt_version=prompt, model=model,
                               system_prompt="sys", snapshot=snap))


def test_cache_hit_skips_api(tmp_path):
    d = FakeDecider()
    src = DecisionSource(Store(tmp_path / "c.db"), d)
    r1, hit1, called1 = get(src)
    r2, hit2, called2 = get(src)
    assert (hit1, called1, hit2, called2) == (False, True, True, False)
    assert d.calls == 1
    assert r1.output == r2.output


def test_cache_key_covers_prompt_model_and_snapshot(tmp_path):
    d = FakeDecider()
    src = DecisionSource(Store(tmp_path / "c.db"), d)
    get(src)
    get(src, prompt="v2")
    get(src, model="claude-opus-5-5")
    get(src, snap={**SNAP, "timestamp": "2026-10-27T11:00:00+00:00"})
    assert d.calls == 4
    h = snapshot_hash(SNAP)
    assert cache_key("v1", "m", h) != cache_key("v2", "m", h)


def test_snapshot_hash_ignores_key_order():
    assert snapshot_hash({"a": 1, "b": 2}) == snapshot_hash({"b": 2, "a": 1})


def test_api_disabled_never_calls_model(tmp_path):
    d = FakeDecider()
    src = DecisionSource(Store(tmp_path / "c.db"), d, allow_api=False)
    reply, hit, called = get(src)
    assert d.calls == 0 and not hit and not called
    assert reply.output is None and "cache miss" in reply.error


def test_missing_api_key_is_an_error_not_a_call(tmp_path):
    src = DecisionSource(Store(tmp_path / "c.db"), None)
    reply, hit, called = get(src)
    assert not called and "ANTHROPIC_API_KEY" in reply.error

from __future__ import annotations

import json

import pytest

from nulltrades.decision import SUBMIT_TOOL, validate_output

from .conftest import quote

WL = frozenset({"BTC/EUR", "ETH/EUR"})
Q = {"BTC/EUR": quote(bid=100, ask=100.1, last=100)}


def act(**kw):
    base = {"action": "open_long", "pair": "BTC/EUR", "size_eur": 8.0, "stop_loss": 95.0,
            "take_profit": 110.0, "new_stop": None, "confidence": 0.6, "rationale": "breakout"}
    base.update(kw)
    return base


def check(raw, open_pairs=frozenset()):
    return validate_output(raw, WL, Q, set(open_pairs))


def test_valid_open_long():
    r = check({"actions": [act()]})
    assert r.ok and r.actions[0].size_eur == 8.0


def test_empty_actions_is_hold():
    assert check({"actions": []}).ok


@pytest.mark.parametrize("name,raw,needle", [
    ("malformed JSON", '{"actions": [', "malformed JSON"),
    ("not an object", "[1, 2]", "valid dictionary"),
    ("missing stop", {"actions": [act(stop_loss=None)]}, "requires stop_loss"),
    ("missing take-profit", {"actions": [act(take_profit=None)]}, "requires take_profit"),
    ("unknown pair", {"actions": [act(pair="DOGE/EUR")]}, "unknown pair"),
    ("NaN size", '{"actions": [{"action": "open_long", "pair": "BTC/EUR", "size_eur": NaN, '
                 '"stop_loss": 95, "take_profit": 110}]}', "size_eur"),
    ("Infinity stop", '{"actions": [{"action": "open_long", "pair": "BTC/EUR", "size_eur": 8, '
                      '"stop_loss": Infinity, "take_profit": 110}]}', "stop_loss"),
    ("negative size", {"actions": [act(size_eur=-5.0)]}, "positive"),
    ("zero size", {"actions": [act(size_eur=0)]}, "positive"),
    ("negative stop", {"actions": [act(stop_loss=-1.0)]}, "positive"),
    ("string number", {"actions": [act(size_eur="8")]}, "size_eur"),
    ("bool number", {"actions": [act(size_eur=True)]}, "size_eur"),
    ("stop above price", {"actions": [act(stop_loss=101.0)]}, "not below current price"),
    ("tp below price", {"actions": [act(take_profit=99.0)]}, "not above current price"),
    ("unknown action", {"actions": [act(action="open_short")]}, "action"),
    ("extra field", {"actions": [act(leverage=5)]}, "leverage"),
    ("confidence > 1", {"actions": [act(confidence=1.5)]}, "confidence"),
    ("rationale too long", {"actions": [act(rationale="x" * 281)]}, "rationale"),
    ("close without position", {"actions": [{"action": "close_position", "pair": "BTC/EUR"}]}, "no open position"),
    ("update_stop missing new_stop", {"actions": [{"action": "update_stop", "pair": "BTC/EUR"}]}, "requires new_stop"),
    ("none", None, "no output"),
])
def test_rejections(name, raw, needle):
    r = check(raw)
    assert not r.ok and r.actions == []
    assert any(needle in e for e in r.errors), r.errors


def test_one_bad_action_rejects_whole_output():
    r = check({"actions": [{"action": "hold"}, act(pair="DOGE/EUR")]})
    assert not r.ok and r.actions == []


def test_close_with_position_is_valid():
    r = check({"actions": [{"action": "close_position", "pair": "BTC/EUR"}]}, {"BTC/EUR"})
    assert r.ok


def test_strict_tool_schema_shape():
    item = SUBMIT_TOOL["input_schema"]["properties"]["actions"]["items"]
    assert SUBMIT_TOOL["strict"] is True
    assert item["additionalProperties"] is False
    assert set(item["required"]) == set(item["properties"])
    # Strict schemas cannot carry numeric bounds; those are enforced in code.
    assert "minimum" not in json.dumps(SUBMIT_TOOL)

"""The codebase has no withdrawal, deposit or transfer path. This test fails if
one appears: as a definition, a call, an attribute access, a keyword or a
string literal anywhere in the package. Docstrings are prose and are skipped."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import nulltrades
from nulltrades.execution.base import Executor
from nulltrades.execution.kraken import KrakenExecutor, _KrakenTradingClient
from nulltrades.execution.paper import PaperExecutor

PKG = Path(nulltrades.__file__).parent
FORBIDDEN = re.compile(r"withdraw|deposit|transfer|walletTransfer|WithdrawFunds|DepositAddresses", re.I)


def _sources():
    return sorted(PKG.rglob("*.py"))


def test_no_forbidden_names_in_code():
    hits = []
    for path in _sources():
        tree = ast.parse(path.read_text(), filename=str(path))
        docstrings = {
            id(n.body[0].value) for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)
        }
        for node in ast.walk(tree):
            if id(node) in docstrings:  # prose explaining the absence is fine
                continue
            name = None
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = node.name
            elif isinstance(node, ast.Attribute):
                name = node.attr
            elif isinstance(node, ast.Name):
                name = node.id
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                name = node.value
            elif isinstance(node, ast.keyword) and node.arg:
                name = node.arg
            if name and FORBIDDEN.search(name):
                hits.append(f"{path.relative_to(PKG.parent)}:{node.lineno}: {name[:60]!r}")
    assert not hits, "withdrawal/deposit/transfer reference found:\n" + "\n".join(hits)


def test_no_dynamic_attribute_access_on_exchange():
    # getattr(exchange, "with" + "draw") would dodge the scan above.
    for path in (PKG / "execution").glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr":
                raise AssertionError(f"getattr in {path.name}:{node.lineno}")


def test_executor_surfaces_have_no_such_methods():
    for cls in (Executor, PaperExecutor, KrakenExecutor, _KrakenTradingClient):
        bad = [m for m in dir(cls) if FORBIDDEN.search(m)]
        assert not bad, f"{cls.__name__} exposes {bad}"


def test_trading_client_only_exposes_allowed_calls():
    public = {m for m in dir(_KrakenTradingClient) if not m.startswith("_")}
    assert public == {
        "load_markets", "fetch_balance", "fetch_open_orders", "fetch_closed_orders", "fetch_order",
        "create_order", "cancel_order", "network_errors",
    }


def test_scanner_catches_a_planted_withdrawal(tmp_path):
    src = "class X:\n    def withdraw(self):\n        pass\n"
    tree = ast.parse(src)
    names = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    assert any(FORBIDDEN.search(n) for n in names)

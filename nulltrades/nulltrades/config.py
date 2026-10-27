"""Settings from config.yaml, secrets from the environment.

Limits live in code and config. The model never sees the config object and has
no path to change it. Hard bounds are code: config.yaml and the Settings screen
can tighten a risk limit, never loosen it past the shipped default.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"
DEFAULT_DB_PATH = PROJECT_ROOT / "nulltrades.db"
PROMPT_DIR = PROJECT_ROOT / "prompt"
DATA_DIR = PROJECT_ROOT / "data"

TIMEFRAMES: dict[str, int] = {"15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}

# Fixed by the brief, so not configurable.
POLL_SECONDS = 10
STALE_AFTER_SECONDS = 120
RECONCILE_EVERY_SECONDS = 300
EQUITY_SNAPSHOT_EVERY_SECONDS = 300
MAX_CONSECUTIVE_ERRORS = 3
MAX_CONSECUTIVE_INVALID = 3
RECONCILE_TOLERANCE_PCT = 0.5
DAILY_SUMMARY_HOUR = 22
LIVE_CONFIRMATION_PHRASE = "I UNDERSTAND THIS USES REAL MONEY"

PAIR_RE = re.compile(r"^[A-Z0-9]{2,10}/EUR$")

# Hard bounds per risk field: ("max", b) means value <= b, ("min", b) means
# value >= b, ("range", lo, hi) for rates that describe the account rather than
# appetite. Max/min bounds equal the shipped defaults, so only tightening works.
RISK_BOUNDS: dict[str, tuple] = {
    "risk_per_trade_pct": ("max", 2.0),
    "max_position_pct": ("max", 50.0),
    "max_open_positions": ("max", 2),
    "max_trades_per_day": ("max", 6),
    "min_stop_pct": ("min", 1.0),
    "max_stop_pct": ("max", 5.0),
    "max_take_profit_pct": ("max", 10.0),
    "min_reward_risk": ("min", 1.5),
    "cost_multiple": ("min", 3.0),
    "fee_maker_pct": ("range", 0.0, 2.0),
    "fee_taker_pct": ("range", 0.0, 2.0),
    "cooldown_minutes": ("min", 60),
    "max_daily_loss_pct": ("max", 10.0),
    "max_drawdown_pct": ("max", 25.0),
    "max_hold_hours": ("max", 72.0),
    "max_slippage_bps": ("max", 15.0),
    "paper_slippage_bps": ("min", 10.0),
    "order_timeout_seconds": ("range", 1.0, 600.0),
}


class RiskConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    risk_per_trade_pct: float = 2
    max_position_pct: float = 50
    max_open_positions: int = 2
    max_trades_per_day: int = 6
    min_stop_pct: float = 1.0
    max_stop_pct: float = 5.0
    max_take_profit_pct: float = 10.0
    min_reward_risk: float = 1.5
    cost_multiple: float = 3
    fee_maker_pct: float = 0.25
    fee_taker_pct: float = 0.40
    cooldown_minutes: int = 60
    max_daily_loss_pct: float = 10
    max_drawdown_pct: float = 25
    max_hold_hours: float = 72
    max_slippage_bps: float = 15
    paper_slippage_bps: float = 10
    order_timeout_seconds: float = 120

    @model_validator(mode="after")
    def _within_hard_bounds(self) -> "RiskConfig":
        errors = bound_violations(self.model_dump())
        if self.min_stop_pct >= self.max_stop_pct:
            errors.append("min_stop_pct must be below max_stop_pct")
        for name in ("risk_per_trade_pct", "max_position_pct", "max_open_positions",
                     "max_trades_per_day", "max_take_profit_pct", "max_daily_loss_pct",
                     "max_drawdown_pct", "max_hold_hours"):
            if getattr(self, name) <= 0:
                errors.append(f"{name} must be positive")
        if errors:
            raise ValueError("; ".join(errors))
        return self


def bound_violations(values: dict[str, Any]) -> list[str]:
    out = []
    for name, bound in RISK_BOUNDS.items():
        if name not in values:
            continue
        v = values[name]
        if bound[0] == "max" and v > bound[1]:
            out.append(f"{name}={v} exceeds hard ceiling {bound[1]}")
        elif bound[0] == "min" and v < bound[1]:
            out.append(f"{name}={v} is below hard floor {bound[1]}")
        elif bound[0] == "range" and not (bound[1] <= v <= bound[2]):
            out.append(f"{name}={v} outside {bound[1]}–{bound[2]}")
    return out


class LiveGateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Floors in code: config can make the gate stricter, never skip it.
    paper_days: float = Field(14, ge=14)
    paper_trades: int = Field(30, ge=30)


class NotificationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    telegram: bool = True
    daily_summary: bool = True


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Read but never honoured at launch: the app always starts in PAPER.
    mode: Literal["paper", "live"] = "paper"
    starting_capital_eur: float = Field(20, gt=0)
    live_capital_cap_eur: float = Field(20, gt=0)
    target_roi: float = Field(1.0, gt=0)
    pairs: list[str] = Field(default_factory=list)
    timeframe: str = "1h"
    model: str = "claude-sonnet-5-5"
    prompt_version: str = "v1"
    max_model_calls_per_day: int = Field(48, ge=1, le=48)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    live_gate: LiveGateConfig = Field(default_factory=LiveGateConfig)
    flatten_on_kill: bool = True
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)
    port: int = Field(8765, ge=1024, le=65535)

    @field_validator("pairs")
    @classmethod
    def _pairs_shape(cls, v: list[str]) -> list[str]:
        clean = []
        for p in v:
            p = p.strip().upper()
            if not PAIR_RE.match(p):
                raise ValueError(f"{p!r} is not a EUR spot pair like BTC/EUR")
            if p not in clean:
                clean.append(p)
        return clean

    @field_validator("timeframe")
    @classmethod
    def _tf(cls, v: str) -> str:
        if v not in TIMEFRAMES:
            raise ValueError(f"timeframe must be one of {sorted(TIMEFRAMES)}")
        return v

    @field_validator("prompt_version")
    @classmethod
    def _pv(cls, v: str) -> str:
        if not re.fullmatch(r"v\d+", v):
            raise ValueError("prompt_version must look like v1, v2, ...")
        return v

    @property
    def timeframe_seconds(self) -> int:
        return TIMEFRAMES[self.timeframe]

    @property
    def target_equity(self) -> float:
        return self.starting_capital_eur * (1 + self.target_roi)


def load_config(path: Path | None = None) -> Config:
    path = path or DEFAULT_CONFIG_PATH
    if not path.exists():
        return Config()
    raw = yaml.safe_load(path.read_text()) or {}
    return Config.model_validate(raw)


CONFIG_HEADER = """\
# nulltrades configuration. Percentages are whole numbers (2 means 2%).
# bps means basis points (10 bps is 0.10%). Secrets never go here: use the
# environment (see README). Risk limits can be tightened, never loosened past
# the hard bounds in nulltrades/config.py. The app always starts in PAPER.
# Check fee rates against your Kraken fee tier before going live.
"""


def save_config(cfg: Config, path: Path | None = None) -> None:
    path = path or DEFAULT_CONFIG_PATH
    body = yaml.safe_dump(cfg.model_dump(), sort_keys=False)
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(CONFIG_HEADER + body)
    tmp.replace(path)


class Secrets:
    """Secrets from environment variables only. Never logged, never repr'd."""

    __slots__ = ("kraken_key", "kraken_secret", "anthropic_key", "telegram_token", "telegram_chat")

    def __init__(self) -> None:
        self.kraken_key = os.environ.get("KRAKEN_API_KEY") or None
        self.kraken_secret = os.environ.get("KRAKEN_API_SECRET") or None
        self.anthropic_key = os.environ.get("ANTHROPIC_API_KEY") or None
        self.telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN") or None
        self.telegram_chat = os.environ.get("TELEGRAM_CHAT_ID") or None

    def __repr__(self) -> str:
        return "Secrets(<redacted>)"

    __str__ = __repr__

    def presence(self) -> dict[str, bool]:
        return {
            "KRAKEN_API_KEY": bool(self.kraken_key),
            "KRAKEN_API_SECRET": bool(self.kraken_secret),
            "ANTHROPIC_API_KEY": bool(self.anthropic_key),
            "TELEGRAM_BOT_TOKEN": bool(self.telegram_token),
            "TELEGRAM_CHAT_ID": bool(self.telegram_chat),
        }

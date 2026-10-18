"""Style Profiles: style as a parameter vector the compiler enforces (plan §5).

Shipped profiles live beside this file as JSON. Phase 2 adds profiles
measured from reference videos; they use the same schema, so the compiler
cannot tell a shipped profile from an extracted one.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

HERE = Path(__file__).parent
ROLES = ("hook", "setup", "body", "payoff", "cta")
REQUIRED = ("id", "name", "description", "pacing", "zoom", "captions", "broll", "music",
            "sfx", "colour", "transitions", "hook", "beats")


def _validate(style: dict, source: str) -> dict:
    missing = [k for k in REQUIRED if k not in style]
    if missing:
        raise ValueError(f"{source}: style missing {', '.join(missing)}")
    roles = [b["role"] for b in style["beats"]]
    if any(r not in ROLES for r in roles):
        raise ValueError(f"{source}: unknown beat role in {roles}")
    total = sum(b["share"] for b in style["beats"])
    if abs(total - 1.0) > 0.01:
        raise ValueError(f"{source}: beat shares sum to {total:.2f}, not 1")
    return style


def all_styles() -> dict[str, dict]:
    out = {}
    for path in sorted(HERE.glob("*.json")):
        style = _validate(json.loads(path.read_text()), path.name)
        out[style["id"]] = style
    return out


def get(style_id: str) -> dict:
    styles = all_styles()
    if style_id not in styles:
        raise KeyError(f"unknown style {style_id!r}; have {', '.join(styles)}")
    return deepcopy(styles[style_id])


def apply_extras(style: dict, extras: dict | None) -> dict:
    """Brief sliders scale the profile: 1.0 is the style as shipped, 0 switches it off."""
    s = deepcopy(style)
    if not extras:
        return s
    zoom = float(extras.get("zoom", 1.0))
    broll = float(extras.get("broll", 1.0))
    s["zoom"]["per_minute"] = s["zoom"]["per_minute"] * zoom
    if zoom > 1.0:
        s["pacing"]["max_static_s"] = s["pacing"]["max_static_s"] / zoom
    s["broll"]["density"] = min(0.8, s["broll"]["density"] * broll)
    if "hook_required" in extras:
        s["hook"]["required"] = bool(extras["hook_required"])
    return s

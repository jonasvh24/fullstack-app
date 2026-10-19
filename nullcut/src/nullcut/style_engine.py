"""Style Profiles from reference videos, and the match score that proves an edit hits one
(plan §5).

Extraction measures 3-10 references and takes the median of each parameter.
What a finished video cannot reveal (beat template, hook cap, zoom easing,
SFX on zooms) comes from the nearest shipped style, and the profile says so
in `inherited`. The match score measures a render with the same code and
compares it parameter by parameter; anything under 80 % carries the rule that
missed, so "edit like X" stays falsifiable.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Callable

import numpy as np

from . import compiler, config, measure, styles

PASS = 0.8
INHERITED = ["beats", "hook", "zoom.hold_s", "broll.min_s", "broll.max_s",
             "sfx.zoom_share", "music.energy", "music.crossfade_s", "transitions"]
VIDEO_EXTENSIONS = config.VIDEO_EXTENSIONS

Log = Callable[[str], None]


def user_styles_dir() -> Path:
    return config.nullcut_home() / "styles"


def _median(values: list, default=None):
    vals = [v for v in values if v is not None]
    return float(np.median(vals)) if vals else default


def _nearest_shipped(m: dict) -> dict:
    """The shipped style closest on the axes a viewer notices first."""
    def vec(cuts, zooms, broll, upper, music):
        return np.array([cuts / 30, zooms / 10, broll / 0.5, upper, music])

    target = vec(m["cuts_per_min"], m["zoom_per_min"], m["broll"] or 0.0, m["upper"],
                 1.0 if m["music"] else 0.0)
    best, best_d = None, 1e9
    for st in styles.all_styles().values():
        if st.get("kind") == "extracted":
            continue
        cap = st["captions"]
        v = vec(st["pacing"]["cuts_per_min"], st["zoom"]["per_minute"], st["broll"]["density"],
                1.0 if cap["case"] == "upper" else 0.0, 1.0 if st["music"]["presence"] else 0.0)
        d = float(np.linalg.norm(v - target))
        if d < best_d:
            best, best_d = st, d
    return best


def profile_from_measurements(name: str, ms: list[dict]) -> dict:
    caps = [m["captions"] for m in ms if m.get("captions", {}).get("present")]
    agg = {
        "cuts_per_min": _median([m["pacing"]["cuts_per_min"] for m in ms], 10.0),
        "silence_ms": _median([m["silence_ms"] for m in ms], None),
        "zoom_per_min": _median([m["zoom"]["per_minute"] for m in ms], 0.0),
        "zoom_scale": _median([m["zoom"]["scale_median"] for m in ms], None),
        "zoom_rise": _median([m["zoom"]["rise_s"] for m in ms], None),
        "max_static": _median([m["max_static_s"] for m in ms], 6.0),
        "broll": _median([m["broll_ratio"] for m in ms], None),
        "captions": len(caps) * 2 >= len(ms),
        "words_per_block": _median([c.get("words_per_block") for c in caps], None),
        "words_p90": _median([c.get("words_p90") for c in caps], None),
        "upper": _median([c.get("upper_ratio") for c in caps], 0.0),
        "highlights": [c["highlight"] for c in caps if c.get("highlight")],
        "music": sum(bool(m["music"].get("presence")) for m in ms) * 2 >= len(ms),
        "under_db": _median([m["music"].get("under_speech_db") for m in ms], None),
        "gap_db": _median([m["music"].get("gap_db") for m in ms], None),
        "on_beat": _median([m["music"].get("cuts_on_beat") for m in ms], 0.0),
        "sfx": _median([m["sfx_cut_share"] for m in ms], 0.0),
        "colour": {k: _median([m["colour"].get(k) for m in ms if m.get("colour")], None)
                   for k in ("saturation", "contrast", "warmth", "lift")},
    }
    base = _nearest_shipped(agg)
    p = deepcopy(base)
    p.update({"id": re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "extracted",
              "name": name, "kind": "extracted", "sources": [m["file"] for m in ms],
              "inherited_from": base["id"], "inherited": INHERITED})
    p["description"] = (f"Measured from {len(ms)} reference video{'s' if len(ms) > 1 else ''}. "
                        f"Closest shipped style: {base['name']}.")

    sil = int(np.clip(agg["silence_ms"] or base["pacing"]["silence_ms"], 100, 800))
    p["pacing"] = {"silence_ms": sil, "hook_silence_ms": int(sil * 0.75),
                   "max_static_s": round(float(np.clip(agg["max_static"], 1.5, 20)), 1),
                   "cuts_per_min": round(agg["cuts_per_min"], 1)}

    z = p["zoom"]
    z["per_minute"] = round(agg["zoom_per_min"], 1)
    if agg["zoom_scale"]:
        z["scale_max"] = round(float(np.clip(agg["zoom_scale"], 1.03, 1.35)), 3)
        # A zoom nobody can see is not a zoom: 5 % is the floor for both scales.
        z["scale_max"] = max(z["scale_max"], 1.05)
        z["scale_min"] = round(min(z["scale_max"], max(1.05, z["scale_max"] - 0.08)), 3)
    z["drift"] = bool(agg["zoom_rise"] and agg["zoom_rise"] > 1.5)
    if agg["zoom_rise"]:
        # Easing is measurable (the rise time); only the hold is inherited, and a punch
        # never holds longer than the style's static limit allows.
        z["ease_s"] = round(float(np.clip(agg["zoom_rise"], 0.1, 3.0)), 2)
        if not z["drift"]:
            cap = max(1.0, 0.5 * p["pacing"]["max_static_s"])
            z["hold_s"] = round(float(min(z["hold_s"], cap)), 1)

    c = p["captions"]
    if not agg["captions"]:
        c["mode"] = "off"
    else:
        # Word-by-word captions never grow past about three words; clean blocks break at
        # sentence ends too, so their median is short but their p90 is not.
        wpb = agg["words_p90"] or agg["words_per_block"] or c["max_words"]
        c["mode"] = "word" if wpb <= 3.5 else "clean"
        c["max_words"] = int(np.clip(round(wpb), 2, 8))
        c["case"] = "upper" if agg["upper"] > 0.7 else "sentence"
        if agg["highlights"]:
            rgb = np.median([[int(h[i:i + 2], 16) for i in (1, 3, 5)] for h in agg["highlights"]],
                            axis=0)
            c["highlight"] = "#{:02X}{:02X}{:02X}".format(*map(int, rgb))
            c["emphasis"] = True
        else:
            c["emphasis"] = False

    if agg["broll"] is not None:
        p["broll"]["density"] = round(float(np.clip(agg["broll"], 0.0, 0.8)), 2)

    mu = p["music"]
    mu["presence"] = agg["music"]
    under = agg["under_db"] if agg["under_db"] is not None else \
        (agg["gap_db"] - 10 if agg["gap_db"] is not None else mu["under_speech_db"])
    mu["under_speech_db"] = round(float(np.clip(under, -35, -6)), 1)
    gap = agg["gap_db"] if agg["gap_db"] is not None else under + 10
    mu["gap_db"] = round(float(np.clip(gap, mu["under_speech_db"], 0)), 1)
    # Chance alone puts about a quarter of cuts within 60 ms of a beat at typical tempos.
    mu["beat_snap"] = bool(agg["music"] and agg["on_beat"] >= 0.4)

    p["sfx"]["cut_share"] = round(float(np.clip(agg["sfx"], 0, 1)), 2)
    if all(v is not None for v in agg["colour"].values()):
        # Absolute look, not a relative tweak: the LUT is fitted per project from
        # the footage's own statistics towards this target.
        p["colour"] = {"target": {k: round(v, 3) for k, v in agg["colour"].items()}}
    p["measured"] = ms
    return p


def extract(name: str, folder: Path, log: Log = print,
            progress: Callable[[float, str], None] | None = None) -> dict:
    progress = progress or (lambda f, m: None)
    files = sorted(f for f in folder.iterdir()
                   if f.suffix.lower() in VIDEO_EXTENSIONS and not f.name.startswith("."))
    if not 1 <= len(files) <= 10:
        raise ValueError(f"need 1 to 10 reference videos (3 or more recommended), found "
                         f"{len(files)} in {folder}")
    ms = []
    for n, f in enumerate(files):
        progress(n / len(files), f"{f.name}: measuring")
        ms.append(measure.measure(f, log=log))
    profile = profile_from_measurements(name, ms)
    if profile["id"] in {s["id"] for s in styles.all_styles().values()
                         if s.get("kind") != "extracted"}:
        profile["id"] += "-ref"
    out = user_styles_dir() / f"{profile['id']}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(profile, indent=2))
    progress(1.0, f"style {profile['name']} saved")
    return profile


# --- match score ------------------------------------------------------------------

def _closeness(measured: float | None, wanted: float, scale: float) -> float:
    if measured is None:
        return 0.0
    return float(np.clip(1.0 - abs(measured - wanted) / scale, 0.0, 1.0))


def _hex_distance(a: str, b: str) -> float:
    ca = np.array([int(a[i:i + 2], 16) for i in (1, 3, 5)], float)
    cb = np.array([int(b[i:i + 2], 16) for i in (1, 3, 5)], float)
    return float(np.linalg.norm(ca - cb) / 441.7)


def expected_colour(style_colour: dict, source: dict | None) -> dict | None:
    """What the output's colour statistics should be after this style's grade."""
    if "target" in style_colour:
        return style_colour["target"]
    if not source:
        return None
    return {"saturation": source["saturation"] * style_colour.get("saturation", 1.0),
            "contrast": source["contrast"] * style_colour.get("contrast", 1.0),
            # write_cube adds w to red and takes w from blue: R-B moves by 2w.
            "warmth": source["warmth"] + 2 * style_colour.get("warmth", 0.0),
            "lift": style_colour.get("lift", 0.0)
            + source["lift"] * (1 - style_colour.get("lift", 0.0))}


def match(m: dict, style: dict, music_wanted: bool = True, captions_mode: str | None = None,
          source_colour: dict | None = None,
          expected: dict | None = None) -> dict[str, dict]:
    """Per-parameter match of a measured render against a profile, with the rule that
    missed for anything under 80 %."""
    out: dict[str, dict] = {}

    def put(key: str, score: float, measured, wanted, rule: str) -> None:
        score = round(float(score), 2)
        out[key] = {"score": score, "measured": measured, "wanted": wanted,
                    "note": "" if score >= PASS else rule}

    w = style["pacing"]["cuts_per_min"]
    got = m["pacing"]["cuts_per_min"]
    put("pacing", _closeness(got, w, max(w, 4.0)), got, w,
        f"rule 1: {got} cuts/min against {w}; the footage had "
        f"{'fewer' if got < w else 'more'} pauses to cut than the style expects")

    eff = max(style["pacing"]["silence_ms"], round(compiler.min_cut_gap_s() * 1000))
    got = m.get("silence_ms")
    put("silence", _closeness(got, eff, eff), got, eff,
        f"rule 1: pauses up to {got} ms survive; the style cuts at {eff} ms")

    w = style["zoom"]["per_minute"]
    got = m["zoom"]["per_minute"]
    put("zoom", _closeness(got, w, max(w, 2.0)), got, w,
        f"rule 4: {got} zooms/min against {w}; too few emphasis words or items too short "
        f"to hold a punch")

    w = style["pacing"]["max_static_s"]
    got = m["max_static_s"]
    put("static", 1.0 if got <= w * 1.15 else w / got, got, w,
        f"rule 3: stretches of {got} s without a cut, zoom or B-roll; the style allows {w} s")

    mode = captions_mode or style["captions"]["mode"]
    cm = m.get("captions") or {}
    if mode == "off":
        put("captions", 0.0 if cm.get("present") else 1.0, cm.get("present"), False,
            "rule 5: captions found where the brief asked for none")
    else:
        parts = [1.0 if cm.get("present") else 0.0]
        maxw = 3 if mode == "word" else style["captions"]["max_words"]
        wpb = cm.get("words_per_block")
        parts.append(1.0 if wpb is not None and wpb <= maxw + 0.5 else
                     _closeness(wpb, maxw, maxw))
        upper = style["captions"]["case"] == "upper" or mode == "word"
        parts.append(1.0 if cm.get("upper_ratio") is not None and
                     (cm["upper_ratio"] > 0.7) == upper else 0.0)
        if style["captions"].get("emphasis") and style["captions"].get("highlight"):
            hl = cm.get("highlight")
            parts.append(1.0 - min(1.0, 2 * _hex_distance(hl, style["captions"]["highlight"]))
                         if hl else 0.0)
        put("captions", float(np.mean(parts)), cm.get("words_per_block"), maxw,
            "rule 5: caption blocks, case or highlight colour do not read as the profile's")

    w = style["broll"]["density"]
    got = m.get("broll_ratio")
    if got is not None:
        put("broll", _closeness(got, w, max(w, 0.15)), got, w,
            f"rule 6: B-roll covers {got:.0%} of speech against {w:.0%}; not enough B-roll "
            f"footage or shots too short")

    if style["music"]["presence"] and music_wanted:
        mu = m.get("music", {})
        if mu.get("presence") is None:
            pass  # no pause long enough to hear the bed: unmeasurable, not a miss
        elif not mu.get("presence"):
            put("music", 0.0, None, style["music"]["under_speech_db"],
                "rule 7: no music bed found; add tracks to the library or pick one")
        else:
            got = mu.get("under_speech_db")
            w = style["music"]["under_speech_db"]
            put("music", _closeness(got, w, 10.0) if got is not None else 0.7, got, w,
                f"rule 7: music sits at {got} dB under speech against {w} dB")

    w = style["sfx"]["cut_share"]
    got = m.get("sfx_cut_share")
    if got is not None:
        put("sfx", 1.0 if w == 0 and got <= 0.1 else _closeness(got, w, max(w, 0.25)), got, w,
            f"rule 7: SFX on {got:.0%} of cuts against {w:.0%}")

    want = expected or expected_colour(style["colour"], source_colour)
    if want and m.get("colour"):
        got = m["colour"]
        d = np.mean([abs(got["saturation"] - want["saturation"]) / 0.08,
                     abs(got["contrast"] - want["contrast"]) / 0.04,
                     # Warmth follows content too: a zoom into a face warms the frame.
                     abs(got["warmth"] - want["warmth"]) / 0.08])
        put("colour", float(np.clip(1 - d / 2, 0, 1)), round(got["saturation"], 3),
            round(want["saturation"], 3),
            "LUT: saturation, contrast or warmth off the profile's look")
    return out


def overall(detail: dict[str, dict]) -> float:
    return round(float(np.mean([d["score"] for d in detail.values()])), 2) if detail else 0.0


COMPARE = [
    ("pacing.cuts_per_min", "Cut rate", "/min"),
    ("pacing.silence_ms", "Silence cut", "ms"),
    ("pacing.max_static_s", "Max static", "s"),
    ("zoom.per_minute", "Zooms", "/min"),
    ("zoom.scale_max", "Zoom scale", "x"),
    ("captions.max_words", "Caption words", "words"),
    ("broll.density", "B-roll density", "share"),
    ("music.under_speech_db", "Music under speech", "dB"),
    ("music.gap_db", "Music in gaps", "dB"),
    ("sfx.cut_share", "SFX on cuts", "share"),
    ("hook.max_s", "Hook cap", "s"),
]


def _get(style: dict, dotted: str):
    v = style
    for part in dotted.split("."):
        if not isinstance(v, dict) or part not in v:
            return None
        v = v[part]
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def compare(ids: list[str]) -> list[dict]:
    """Rows for the comparison view: every style on the same axis per parameter. Axes span
    all known styles, so a marker's position means the same thing whatever is selected."""
    known = styles.all_styles()
    missing = [i for i in ids if i not in known]
    if missing:
        raise KeyError(f"unknown style {', '.join(missing)}")
    if not 2 <= len(ids) <= 4:
        raise ValueError("compare two to four styles")
    rows = []
    for key, label, unit in COMPARE:
        everyone = [v for st in known.values() if (v := _get(st, key)) is not None]
        lo, hi = (min(everyone), max(everyone)) if everyone else (0.0, 1.0)
        pad = (hi - lo) * 0.08 or 1.0
        rows.append({"key": key, "label": label, "unit": unit,
                     "min": round(lo - pad, 3), "max": round(hi + pad, 3),
                     "values": {i: _get(known[i], key) for i in ids}})
    return rows

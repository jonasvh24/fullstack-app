"""Chat refinement (plan §2 step 6, §6 "refinement fast path").

Every instruction is classified first.

  tweak       "less zoom", "music quieter", "warmer". Resolves to absolute values
              in the brief's style overrides and goes straight to the compiler:
              no LLM, no replan.
  structural  "drop the bit about pricing", "open on the sunset instead". Becomes
              an RFC 6902 JSON Patch against the current Edit Plan, so locked beats
              and earlier fixes stay where they are.
  undo        restores the brief and plan from before the last step.

A rules interpreter covers the common phrasings in English and Dutch. What it
cannot map goes to the LLM when one is reachable, under a schema that only
allows the same tweak parameters and patch operations, validated the same way.
Every sentence the creator deletes or restores here is a labelled example for
the learned highlight weights.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from typing import Callable

from . import learn, llm, music, plan, styles
from .project import Project, read_json, write_json

Log = Callable[[str], None]

# Every parameter a tweak may touch: section.key -> (kind, lo, hi) or allowed values.
PARAMS: dict[str, tuple] = {
    "pacing.silence_ms": ("num", 80, 2000),
    "pacing.hook_silence_ms": ("num", 60, 2000),
    "pacing.max_static_s": ("num", 1.5, 30),
    "zoom.per_minute": ("num", 0, 30),
    "zoom.scale_min": ("num", 1.0, 1.4),
    "zoom.scale_max": ("num", 1.0, 1.5),
    "zoom.ease_s": ("num", 0.06, 3.0),
    "zoom.hold_s": ("num", 0.3, 8.0),
    "captions.max_words": ("int", 1, 12),
    "captions.case": ("enum", "upper", "sentence"),
    "captions.highlight": ("hex",),
    "captions.scale": ("num", 0.6, 1.8),
    "broll.density": ("num", 0, 0.8),
    "music.under_speech_db": ("num", -40, -6),
    "music.gap_db": ("num", -30, 0),
    "sfx.cut_share": ("num", 0, 1),
    "sfx.zoom_share": ("num", 0, 1),
    "hook.max_s": ("num", 2, 40),
    "colour_nudge.saturation": ("num", 0, 2),
    "colour_nudge.contrast": ("num", 0.6, 1.6),
    "colour_nudge.warmth": ("num", -0.25, 0.25),
    "colour_nudge.lift": ("num", -0.08, 0.1),
    "brief.captions.mode": ("enum", "off", "clean", "word"),
    "brief.music.mode": ("enum", *plan.MUSIC_MODES),
    "brief.music.energy": ("enum", "low", "mid", "high"),
    "brief.music.mood": ("str",),
    "brief.target_s": ("num", 15, 1800),
    "brief.format": ("enum", "16:9", "9:16", "both"),
    "brief.style": ("style",),
}
# Parameters a profile may leave out, at the value the compiler assumes.
DEFAULTS = {"captions.scale": 1.0}
# Changes to these re-run the duration solver (sentence lengths or the target move).
RESOLVE = {"pacing.silence_ms", "pacing.hook_silence_ms", "hook.max_s", "brief.target_s",
           "brief.style"}

COLOURS = {"red": "#FF3B30", "orange": "#FF9500", "yellow": "#FFD60A", "amber": "#FFC400",
           "green": "#34C759", "lime": "#A8E10C", "cyan": "#00E5FF", "teal": "#30B0C7",
           "blue": "#0A84FF", "purple": "#BF5AF2", "pink": "#FF2D92", "white": "#FFFFFF",
           "rood": "#FF3B30", "geel": "#FFD60A", "groen": "#34C759", "blauw": "#0A84FF",
           "oranje": "#FF9500", "roze": "#FF2D92", "paars": "#BF5AF2", "wit": "#FFFFFF"}

LESS = (r"(?:less|fewer|lower|reduce|tone down|cut back on|not so many|too many|too much|"
        r"minder|weniger)")
MORE = r"(?:more|extra|increase|bump up|add more|not enough|meer|vaker)"
NONE = (r"(?:no|without|remove all|remove the|remove|turn off|disable|kill|drop all|drop the|"
        r"lose the|geen|zonder|haal de)")

T_ZOOM = r"(?:zoom\w*|punch[- ]?ins?|punches|inzoom\w*)"
T_BROLL = r"(?:b[- ]?roll|cutaways?|cut-aways?|overlays?|stock shots?)"
T_CAPS = r"(?:captions?|subtitles?|subs|ondertitel\w*|text on screen)"
T_MUSIC = r"(?:music|soundtrack|song|track|bed|muziek)"
T_SFX = r"(?:sfx|sound effects?|whoosh\w*|pops?|swooshes|geluidseffect\w*)"

PUNCHY = (r"(?:punch(?:y|ier)|tight(?:er|en)|snapp(?:y|ier)|fast(?:er)?|quick(?:er)?|pace it up|"
          r"speed (?:it )?up|cut (?:the |out )?(?:pauses|silences|gaps|dead air)|"
          r"less (?:dead air|pauses)|strakker|sneller|vlotter|kortere pauzes)")
SLOW = (r"(?:slow(?:er)?(?: it)? down|slower|breath(?:e|ing room)|less rushed|more room|"
        r"calm(?:er)? pac(?:e|ing)|let it breathe|rustiger|trager|langzamer)")

STOPWORDS = plan.STOP | {
    "bit", "part", "piece", "section", "sentence", "line", "moment", "stuff", "segment", "clip",
    "where", "about", "when", "which", "who", "my", "me", "talk", "talks", "talking", "say",
    "says", "said", "mention", "mentions", "explain", "explains", "thing", "one", "please",
    "instead", "there", "here", "he", "she", "they", "his", "her", "our", "stukje", "stuk",
    "deel", "zin", "waar", "over", "wanneer", "die", "mijn", "praat", "zeg", "zegt"}

ROLE_WORDS = {
    "hook": "hook", "intro": "hook", "opening": "hook", "opener": "hook", "start": "hook",
    "setup": "setup", "set-up": "setup", "context": "setup",
    "body": "body", "middle": "body", "main part": "body", "main section": "body",
    "payoff": "payoff", "conclusion": "payoff", "punchline": "payoff",
    "cta": "cta", "outro": "cta", "ending": "cta", "end": "cta", "call to action": "cta",
    "slot": "cta", "afsluiting": "cta",
}
ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
            "seventh": 7, "eighth": 8, "last": -1, "eerste": 1, "tweede": 2, "derde": 3,
            "laatste": -1}


class RefineError(ValueError):
    """An instruction that cannot be applied as asked; the message says why."""


# --- context ------------------------------------------------------------------------

class Ctx:
    """What an interpreter may read: the current plan, brief, effective style, footage."""

    def __init__(self, project: Project):
        self.project = project
        self.doc = plan.latest_plan(project, with_beats=True)
        if self.doc is None:
            raise RefineError("plan the edit first: there is nothing to refine yet")
        self.brief = plan.load_brief(project)
        self.style = styles.for_brief(self.doc["style"], self.brief)
        self.index = project.clip_index()
        self.pool = plan.segment_pool(self.index)
        order = list(self.pool)
        self.story = {sid: n for n, sid in enumerate(order)}

    def kept(self) -> list[str]:
        return [s for b in self.doc["beats"] for s in b["segment_ids"]]

    def dropped(self) -> list[str]:
        return [s for b in self.doc["beats"] for s in b.get("dropped_ids", [])]

    def unused(self) -> list[str]:
        inplan = set(self.kept()) | set(self.dropped())
        return [s for s in self.pool if s not in inplan]

    def get(self, param: str):
        if param.startswith("brief."):
            node = self.brief
            for k in param.split(".")[1:]:
                node = node[k]
            return node
        if param.startswith("colour_nudge."):
            return self.brief.get("overrides", {}).get("colour_nudge", {}).get(
                param.split(".")[1], 1.0 if param.endswith(("saturation", "contrast")) else 0.0)
        section, key = param.split(".")
        return self.style[section].get(key, DEFAULTS.get(param))


# --- tweaks ---------------------------------------------------------------------------

def _clamp(param: str, value):
    spec = PARAMS[param]
    kind = spec[0]
    if kind in ("num", "int"):
        v = min(max(float(value), spec[1]), spec[2])
        return int(round(v)) if kind == "int" else round(v, 4)
    if kind == "enum":
        if value not in spec[1:]:
            raise RefineError(f"{param} must be one of {', '.join(spec[1:])}")
        return value
    if kind == "hex":
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", str(value)):
            raise RefineError(f"{param} must be a colour like #FFC400")
        return str(value).upper()
    if kind == "style":
        if value not in styles.all_styles():
            raise RefineError(f"unknown style {value}")
        return value
    return str(value)[:60]


def _change(ctx: Ctx, param: str, op: str, value) -> dict:
    """One resolved tweak: from the current effective value to an absolute new one."""
    if param not in PARAMS:
        raise RefineError(f"{param} is not a parameter a refinement may change")
    cur = ctx.get(param)
    if op == "scale":
        new = (cur or 0) * float(value)
    elif op == "add":
        new = (cur or 0) + float(value)
    else:
        new = value
    new = _clamp(param, new)
    return {"param": param, "from": cur, "to": new}


def _label(c: dict) -> str:
    names = {"pacing.silence_ms": "pauses cut above", "pacing.hook_silence_ms":
             "hook pauses cut above", "pacing.max_static_s": "longest static stretch",
             "zoom.per_minute": "zooms per minute", "broll.density": "B-roll share",
             "music.under_speech_db": "music under speech", "music.gap_db": "music in pauses",
             "sfx.cut_share": "SFX on cuts", "sfx.zoom_share": "SFX on zooms",
             "brief.captions.mode": "captions", "brief.music.mode": "music",
             "brief.target_s": "target length", "brief.format": "format",
             "brief.style": "style", "hook.max_s": "hook cap",
             "colour_nudge.warmth": "warmth", "colour_nudge.saturation": "saturation",
             "colour_nudge.contrast": "contrast", "colour_nudge.lift": "shadow lift",
             "captions.scale": "caption size", "captions.highlight": "caption highlight",
             "captions.case": "caption case", "captions.max_words": "words per caption",
             "zoom.scale_min": "smallest zoom", "zoom.scale_max": "largest zoom",
             "zoom.ease_s": "zoom ease", "brief.music.energy": "music energy",
             "brief.music.mood": "music mood", "brief.music.track_id": "music track"}
    unit = {"pacing.silence_ms": " ms", "pacing.hook_silence_ms": " ms", "hook.max_s": " s",
            "pacing.max_static_s": " s", "music.under_speech_db": " dB", "music.gap_db": " dB",
            "brief.target_s": " s"}.get(c["param"], "")

    def fmt(v):
        if isinstance(v, float):
            return f"{v:.0%}" if c["param"] in ("broll.density", "sfx.cut_share",
                                                 "sfx.zoom_share", "captions.scale",
                                                 "colour_nudge.saturation",
                                                 "colour_nudge.contrast") else f"{v:g}"
        return str(v)
    return f"{names.get(c['param'], c['param'])} {fmt(c['from'])} → {fmt(c['to'])}{unit}"


def _scope(text: str) -> tuple[str | None, float | None]:
    m = re.search(r"\b(?:first|opening|eerste)\s+(\d+)\s*(?:s|secs?|seconds?|seconden)\b", text)
    if m:
        return "hook", float(m.group(1))
    if re.search(r"\b(?:intro|opening|hook|the start|beginning|begin)\b", text):
        return "hook", None
    return None, None


def _direction(text: str, topic: str) -> str | None:
    if re.search(rf"\b{NONE}\s+(?:\w+\s+)?{topic}", text) or \
            re.search(rf"{topic}\s+(?:off|out|uit|weg)\b", text):
        return "none"
    if re.search(rf"\b{LESS}\b.*{topic}|{topic}.*\b(?:less|fewer|minder)\b", text):
        return "less"
    if re.search(rf"\b{MORE}\b.*{topic}|{topic}.*\b(?:more|meer)\b", text) or \
            re.search(rf"\b(?:add|with|put in|turn on|enable|voeg)\b.*{topic}", text):
        return "more"
    return None


def _tweak_zoom(ctx: Ctx, t: str) -> list[dict] | None:
    if not re.search(T_ZOOM, t):
        return None
    if re.search(r"\b(?:subtler|smaller|gentler|softer|less aggressive|kleinere?)\b", t):
        return [_change(ctx, p, "set", round(1 + (ctx.get(p) - 1) * 0.6, 3))
                for p in ("zoom.scale_min", "zoom.scale_max")]
    if re.search(r"\b(?:bigger|stronger|harder|more aggressive|tighter|grotere?)\b", t):
        return [_change(ctx, p, "set", round(1 + (ctx.get(p) - 1) * 1.4, 3))
                for p in ("zoom.scale_min", "zoom.scale_max")]
    if re.search(r"\b(?:slower|smoother|gradual|langzamere?)\b", t):
        return [_change(ctx, "zoom.ease_s", "scale", 1.6)]
    if re.search(r"\b(?:faster|snappier|quicker|sharper|snellere?)\b", t):
        return [_change(ctx, "zoom.ease_s", "scale", 0.6)]
    d = _direction(t, T_ZOOM)
    cur = ctx.get("zoom.per_minute")
    if d == "none":
        return [_change(ctx, "zoom.per_minute", "set", 0)]
    if d == "less":
        return [_change(ctx, "zoom.per_minute", "set", round(cur * 0.5, 1))]
    if d == "more":
        return [_change(ctx, "zoom.per_minute", "set", round(cur * 1.5, 1) if cur else 2.0)]
    return None


def _tweak_broll(ctx: Ctx, t: str) -> list[dict] | None:
    if not re.search(T_BROLL, t):
        return None
    d = _direction(t, T_BROLL)
    cur = ctx.get("broll.density")
    value = {"none": 0.0, "less": cur * 0.5, "more": cur * 1.5 if cur else 0.15}.get(d)
    return None if value is None else [_change(ctx, "broll.density", "set", round(value, 3))]


def _tweak_captions(ctx: Ctx, t: str) -> list[dict] | None:
    if not re.search(T_CAPS + r"|\b(?:upper ?case|all caps|capitals|hoofdletters|highlight\w*)\b",
                     t):
        return None
    if re.search(r"word[- ]by[- ]word|animated|karaoke|one word at a time|woord voor woord", t):
        return [_change(ctx, "brief.captions.mode", "set", "word")]
    if re.search(r"\bclean\b|full sentences?|static|sentence captions|hele zinnen", t):
        return [_change(ctx, "brief.captions.mode", "set", "clean")]
    if re.search(r"\b(?:upper ?case|all caps|capitals|hoofdletters)\b", t):
        return [_change(ctx, "captions.case", "set", "upper")]
    if re.search(r"\b(?:lower ?case|sentence case|normal case|kleine letters)\b", t):
        return [_change(ctx, "captions.case", "set", "sentence")]
    m = re.search(r"#[0-9a-f]{6}\b", t) or re.search(r"\b(" + "|".join(COLOURS) + r")\b", t)
    if m and re.search(r"highlight|emphasis|accent|kleur|colou?r", t):
        value = m.group(0) if m.group(0).startswith("#") else COLOURS[m.group(1)]
        return [_change(ctx, "captions.highlight", "set", value.upper())]
    if re.search(r"\b(?:bigger|larger|huge|groter)\b", t):
        return [_change(ctx, "captions.scale", "scale", 1.2)]
    if re.search(r"\b(?:smaller|tiny|kleiner)\b", t):
        return [_change(ctx, "captions.scale", "scale", 1 / 1.2)]
    if re.search(r"fewer words|shorter captions|less text|minder woorden", t):
        return [_change(ctx, "captions.max_words", "set",
                        max(1, ctx.get("captions.max_words") - 2))]
    if re.search(r"more words|longer captions|meer woorden", t):
        return [_change(ctx, "captions.max_words", "add", 2)]
    d = _direction(t, T_CAPS)
    if d == "none":
        return [_change(ctx, "brief.captions.mode", "set", "off")]
    if d == "more" and ctx.brief["captions"]["mode"] == "off":
        mode = ctx.style["captions"].get("mode", "clean")
        return [_change(ctx, "brief.captions.mode", "set", mode if mode != "off" else "clean")]
    return None


MOODS = ("cinematic", "lofi", "lo-fi", "ambient", "epic", "corporate", "acoustic", "electronic",
         "jazzy", "jazz", "dark", "happy", "uplifting", "chill", "piano", "orchestral", "funky")


def _tweak_music(ctx: Ctx, t: str) -> list[dict] | None:
    if not re.search(T_MUSIC, t):
        return None
    if re.search(r"\b(?:generate|generated|compose|composed|original|genereer)\b", t):
        return [_change(ctx, "brief.music.mode", "set", "generate")]
    d = _direction(t, T_MUSIC)
    if d == "none":
        return [_change(ctx, "brief.music.mode", "set", "none")]
    if re.search(r"\b(?:quieter|softer|lower|down|duck(?:ed)? more|less loud|too loud|zachter)\b",
                 t):
        return [_change(ctx, "music.under_speech_db", "add", -4),
                _change(ctx, "music.gap_db", "add", -4)]
    if d == "less":
        return [_change(ctx, "music.under_speech_db", "add", -4),
                _change(ctx, "music.gap_db", "add", -4)]
    if re.search(r"\b(?:louder|up|more present|too quiet|harder|luider)\b", t):
        return [_change(ctx, "music.under_speech_db", "add", 4),
                _change(ctx, "music.gap_db", "add", 4)]
    if re.search(r"\b(?:energetic|upbeat|hype|faster|driving|more energy|energieker)\b", t):
        return [_change(ctx, "brief.music.energy", "set", "high")]
    if re.search(r"\b(?:calm(?:er)?|chill(?:er)?|mellow|relaxed|gentle|rustige?r?)\b", t):
        return [_change(ctx, "brief.music.energy", "set", "low")]
    if re.search(r"\b(?:different|another|other|new|swap|change|andere?)\b", t):
        library = music.load_library()
        current = ctx.brief["music"].get("track_id")
        if len(library) > 1:
            last = _current_track(ctx.project)
            others = [x for x in library if x["id"] not in (current, last)]
            if others:
                return [_change(ctx, "brief.music.mode", "set", "track"),
                        {"param": "brief.music.track_id", "from": current,
                         "to": others[0]["id"]}]
        return [_change(ctx, "brief.music.mode", "set", "generate")]
    mood = next((m for m in MOODS if re.search(rf"\b{m}\b", t)), None)
    if mood:
        mode = ctx.brief["music"]["mode"]
        out = [_change(ctx, "brief.music.mood", "set", mood.replace("-", ""))]
        if mode in ("none", "track"):
            out.append(_change(ctx, "brief.music.mode", "set",
                               "library" if music.load_library() else "generate"))
        return out
    if d == "more" and ctx.brief["music"]["mode"] == "none":
        return [_change(ctx, "brief.music.mode", "set",
                        "library" if music.load_library() else "generate")]
    return None


def _current_track(project: Project) -> str | None:
    from . import timeline
    latest = project.latest("timelines", "timeline", ".otio")
    if not latest:
        return None
    meta, _ = timeline.load(latest[1])
    return (meta.get("music") or {}).get("id")


def _tweak_sfx(ctx: Ctx, t: str) -> list[dict] | None:
    if not re.search(T_SFX, t):
        return None
    d = _direction(t, T_SFX)
    out = []
    for p in ("sfx.cut_share", "sfx.zoom_share"):
        cur = ctx.get(p)
        value = {"none": 0.0, "less": cur * 0.5, "more": min(1.0, cur * 1.5) if cur else 0.3}.get(d)
        if value is None:
            return None
        out.append(_change(ctx, p, "set", round(value, 3)))
    return out


def _tweak_colour(ctx: Ctx, t: str) -> list[dict] | None:
    rules = [
        (r"\bwarm(?:er)?\b|warmer", [("warmth", "add", 0.04)]),
        (r"\b(?:cool(?:er)?|cold(?:er)?|koeler|kouder)\b", [("warmth", "add", -0.04)]),
        (r"black and white|monochrome|zwart[- ]wit|greyscale|grayscale",
         [("saturation", "set", 0.0)]),
        (r"\b(?:desaturat\w*|muted|washed out|less saturat\w*|less colou?r\w*|minder kleur)\b",
         [("saturation", "scale", 0.88)]),
        (r"\b(?:more saturat\w*|more colou?r\w*|colou?rful|vivid|vibrant|pop\w*|meer kleur)\b",
         [("saturation", "scale", 1.12)]),
        (r"\b(?:more contrast|contrastier|punchier contrast|meer contrast)\b",
         [("contrast", "scale", 1.08)]),
        (r"\b(?:less contrast|flatter|softer contrast|minder contrast)\b",
         [("contrast", "scale", 0.93)]),
        (r"\b(?:brighter|lift the shadows|lichter|helderder)\b", [("lift", "add", 0.02)]),
        (r"\b(?:darker|moodier|crush\w*|donkerder)\b", [("lift", "add", -0.02),
                                                         ("contrast", "scale", 1.04)]),
    ]
    if not re.search(r"colou?r|grade|look|warm|cool|cold|saturat|contrast|bright|dark|moody|"
                     r"black and white|monochrome|vivid|muted|kleur|zwart", t):
        return None
    for pattern, ops in rules:
        if re.search(pattern, t):
            return [_change(ctx, f"colour_nudge.{k}", op, v) for k, op, v in ops]
    return None


def _tweak_pacing(ctx: Ctx, t: str) -> list[dict] | None:
    punchy, slow = re.search(PUNCHY, t), re.search(SLOW, t)
    if not punchy and not slow:
        return None
    scope, secs = _scope(t)
    factor = 0.7 if punchy else 1.4
    out = []
    if scope == "hook":
        out.append(_change(ctx, "pacing.hook_silence_ms", "scale", 0.6 if punchy else 1.4))
        if secs and secs < ctx.get("hook.max_s"):
            out.append(_change(ctx, "hook.max_s", "set", secs))
        return out
    out.append(_change(ctx, "pacing.silence_ms", "scale", factor))
    out.append(_change(ctx, "pacing.hook_silence_ms", "scale", factor))
    out.append(_change(ctx, "pacing.max_static_s", "scale", 0.8 if punchy else 1.25))
    return out


def _duration(text: str) -> float | None:
    m = re.search(r"\b(\d+):(\d{2})\b", text)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:min|mins|minutes?|minuten|m)\b", text)
    if m:
        return float(m.group(1).replace(",", ".")) * 60
    m = re.search(r"(\d+)\s*(?:s|secs?|seconds?|seconden)\b", text)
    if m:
        return float(m.group(1))
    return None


def _tweak_length(ctx: Ctx, t: str) -> list[dict] | None:
    kept = ctx.doc.get("solver", {}).get("kept_s") or sum(
        b.get("duration_s", 0.0) for b in ctx.doc["beats"])
    tol = ctx.brief["tolerance"]
    verb = re.search(r"\b(?:make it|cut (?:it )?(?:down )?to|trim (?:it )?to|shorten (?:it )?to|"
                     r"target|length|keep it (?:under|below)|under|at most|max(?:imum)?|"
                     r"maak het|inkorten tot)\b", t)
    secs = _duration(t)
    if verb and secs and not re.search(r"\b(?:first|opening|eerste)\s+\d", t):
        if re.search(r"\b(?:under|below|at most|max(?:imum)?)\b", t):
            secs = secs / (1 + tol)
        return [_change(ctx, "brief.target_s", "set", round(secs, 1))]
    m = re.search(r"(\d+)\s*(?:s|secs?|seconds?|seconden)\s+(shorter|longer|korter|langer)", t)
    if m:
        delta = float(m.group(1)) * (-1 if m.group(2) in ("shorter", "korter") else 1)
        return [_change(ctx, "brief.target_s", "set", round(kept + delta, 1))]
    if re.fullmatch(r"(?:make it |a bit |much |way )*(?:shorter|korter)(?: please)?", t):
        return [_change(ctx, "brief.target_s", "set",
                        round(kept * (0.7 if "much" in t or "way" in t else 0.85), 1))]
    if re.fullmatch(r"(?:make it |a bit |much )*(?:longer|langer)(?: please)?", t):
        return [_change(ctx, "brief.target_s", "set", round(kept * 1.15, 1))]
    return None


def _tweak_format(ctx: Ctx, t: str) -> list[dict] | None:
    v = re.search(r"\b(?:vertical|portrait|9:16|shorts?|tiktok|reels?|verticaal)\b", t)
    h = re.search(r"\b(?:landscape|horizontal|16:9|widescreen|liggend)\b", t)
    both = re.search(r"\b(?:also|as well|too|both|version|plus|ook)\b", t)
    if not v and not h:
        return None
    fmt = "both" if (v and h) or both else ("9:16" if v else "16:9")
    return [_change(ctx, "brief.format", "set", fmt)]


def _tweak_style(ctx: Ctx, t: str) -> list[dict] | None:
    if not re.search(r"\b(?:style|stijl|switch to|change to|make it (?:a|an|like)|use the|"
                     r"in the style)\b", t):
        return None

    def norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()

    text = norm(t)
    hits = [s for s in styles.all_styles().values()
            if norm(s["name"]) in text or norm(s["id"]) in text]
    if not hits:
        return None
    best = max(hits, key=lambda s: len(norm(s["name"])))
    if best["id"] == ctx.doc["style"]:
        raise RefineError(f"the edit already uses {best['name']}")
    return [_change(ctx, "brief.style", "set", best["id"])]


TWEAKS = (_tweak_style, _tweak_zoom, _tweak_broll, _tweak_captions, _tweak_music, _tweak_sfx,
          _tweak_colour, _tweak_length, _tweak_format, _tweak_pacing)


def _clauses(text: str) -> list[str]:
    text = re.sub(r"black and white|zwart en wit", "monochrome", text)
    parts = re.split(r"\s*(?:,|;|\band also\b|\band\b|\bplus\b|\ben\b|\bthen\b)\s*", text)
    return [p for p in (x.strip(" .!") for x in parts) if p]


# --- structural ------------------------------------------------------------------------

def _tokens(text: str) -> list[str]:
    return [w for w in re.findall(r"[\w']+", text.lower()) if w not in STOPWORDS and len(w) > 1]


def search(ctx: Ctx, query: str, among: list[str]) -> list[tuple[float, str]]:
    """Rank sentences for a creator's description of them: idf-weighted word overlap,
    with a bonus for a quoted or verbatim phrase."""
    quoted = re.findall(r"[\"“'‘]([^\"”'’]{3,})[\"”'’]", query)
    q = _tokens(" ".join(quoted) if quoted else query)
    if not q:
        return []
    docs = {sid: _tokens(ctx.pool[sid]["text"] + " " + ctx.pool[sid]["caption"])
            for sid in ctx.pool}
    df = Counter(t for d in docs.values() for t in set(d))
    idf = {t: math.log(1 + len(docs) / n) for t, n in df.items()}
    phrase = (quoted[0] if quoted else query).lower().strip()
    out = []
    for sid in among:
        d = docs.get(sid, [])
        hits = set(q) & set(d)
        if not hits:
            continue
        score = sum(idf[t] for t in hits) / (1 + 0.02 * len(d))
        if len(phrase) > 6 and phrase in ctx.pool[sid]["text"].lower():
            score *= 2
        out.append((round(score, 3), sid))
    out.sort(key=lambda x: (-x[0], ctx.story[x[1]]))
    return out


def _pick(ctx: Ctx, query: str, among: list[str], what: str) -> str:
    hits = search(ctx, query, among)
    if not hits:
        hint = (" Non-speech sounds (a cough, a laugh) are not in the transcript; quote "
                "the words around it, or remove it on the storyboard.") \
            if re.search(r"cough|sneeze|laugh|hoest|lach|um+\b|uh+\b", query) else ""
        raise RefineError(f"no {what} sentence matches \"{query}\".{hint}")
    if len(hits) > 1 and hits[1][0] >= hits[0][0] * 0.95:
        options = "; ".join(f"\"{_short(ctx.pool[s]['text'])}\"" for _, s in hits[:3])
        raise RefineError(f"\"{query}\" matches more than one sentence: {options}. "
                          "Quote a few of its words.")
    return hits[0][1]


def _short(text: str, n: int = 9) -> str:
    words = text.split()
    return " ".join(words[:n]) + (" …" if len(words) > n else "")


def _beat_ref(ctx: Ctx, ref: str) -> int | None:
    """Index into the plan's beats for "beat 3", "the third beat", "the setup", "the outro"."""
    ref = ref.strip().lower()
    beats = ctx.doc["beats"]
    m = re.fullmatch(r"(?:the\s+)?(?:beat\s*|b)(\d+)", ref)
    if m:
        n = int(m.group(1))
        return n - 1 if 1 <= n <= len(beats) else None
    m = re.fullmatch(r"(?:the\s+)?(\w+)\s+beat", ref)
    if m and m.group(1) in ORDINALS:
        n = ORDINALS[m.group(1)]
        return (n - 1 if n > 0 else len(beats) - 1) if abs(n) <= len(beats) else None
    m = re.fullmatch(r"(?:the\s+)?(?:whole\s+|entire\s+)?([\w -]+?)(?:\s+(?:beat|section|part))?",
                     ref)
    if m and m.group(1) in ROLE_WORDS:
        role = ROLE_WORDS[m.group(1)]
        idx = [i for i, b in enumerate(beats) if b["role"] == role]
        return idx[0] if role in ("hook", "setup") or len(idx) == 1 else (idx[-1] if idx else None)
    return None


def _where(ctx: Ctx, sid: str) -> tuple[int, str, int] | None:
    for i, b in enumerate(ctx.doc["beats"]):
        for key in ("segment_ids", "dropped_ids"):
            ids = b.get(key, [])
            if sid in ids:
                return i, key, ids.index(sid)
    return None


def _guard(ctx: Ctx, i: int) -> None:
    b = ctx.doc["beats"][i]
    if b.get("locked"):
        raise RefineError(f"beat \"{b['title']}\" is locked; unlock it on the storyboard first")


def _story_slot(ctx: Ctx, sid: str, skip: set[int]) -> tuple[int, int]:
    """Where a sentence goes back in story order: the unlocked beat whose sentences
    surround it in the footage, at the position that keeps the beat in spoken order."""
    key = ctx.story[sid]
    best, best_d = None, None
    for i, b in enumerate(ctx.doc["beats"]):
        if i in skip or b.get("locked") or b["role"] == "hook":
            continue
        keys = [ctx.story[s] for s in b["segment_ids"]]
        if not keys:
            continue
        d = 0 if min(keys) <= key <= max(keys) else min(abs(key - min(keys)), abs(key - max(keys)))
        if best_d is None or d < best_d:
            best, best_d = i, d
    if best is None:
        raise RefineError("no unlocked beat to put that sentence back into")
    ids = ctx.doc["beats"][best]["segment_ids"]
    pos = sum(1 for s in ids if ctx.story[s] < key)
    return best, pos


def _new_beat_id(ctx: Ctx) -> str:
    taken = {b["id"] for b in ctx.doc["beats"]}
    n = 1
    while f"b{n}" in taken:
        n += 1
    return f"b{n}"


def _remove_ops(ctx: Ctx, sid: str) -> list[dict]:
    loc = _where(ctx, sid)
    if loc is None:
        return []
    i, key, j = loc
    _guard(ctx, i)
    return [{"op": "remove", "path": f"/beats/{i}/{key}/{j}"}]


def _structural(ctx: Ctx, text: str) -> dict | None:
    t = text.strip().rstrip(".!")
    tl = t.lower()

    m = re.match(r"^(?:please\s+)?(?:open|start|begin|lead|kick (?:it )?off)\s+(?:on|with)\s+"
                 r"(?P<q>.+?)(?:\s+instead)?$", tl) or \
        re.match(r"^(?:make|use)\s+(?P<q>.+?)\s+(?:the|as the|as)\s+(?:hook|opener|opening)$", tl)
    if m:
        return _open_on(ctx, m.group("q"))

    m = re.match(r"^(?:please\s+)?(?:end|finish|close|wrap(?: it)? up)\s+(?:on|with)\s+"
                 r"(?P<q>.+?)(?:\s+instead)?$", tl)
    if m:
        return _end_on(ctx, m.group("q"))

    m = re.match(r"^(?:please\s+)?move\s+(?P<q>.+?)\s+(?:to\s+the\s+(?P<where>start|beginning|"
                 r"front|top|end|close)|(?P<rel>before|after)\s+(?P<r>.+))$", tl)
    if m:
        if m.group("where") in ("start", "beginning", "front", "top"):
            return _open_on(ctx, m.group("q"))
        if m.group("where"):
            return _end_on(ctx, m.group("q"))
        return _move_relative(ctx, m.group("q"), m.group("rel"), m.group("r"))

    m = re.match(r"^(?:please\s+)?swap\s+(?P<a>.+?)\s+(?:and|with)\s+(?P<b>.+)$", tl)
    if m:
        a, b = _beat_ref(ctx, m.group("a")), _beat_ref(ctx, m.group("b"))
        if a is None or b is None:
            raise RefineError("name two beats to swap: \"swap beats 2 and 3\" or "
                              "\"swap the setup and the body\"")
        _guard(ctx, a)
        _guard(ctx, b)
        lo, hi = sorted((a, b))
        ops = [{"op": "move", "from": f"/beats/{hi}", "path": f"/beats/{lo}"},
               {"op": "move", "from": f"/beats/{lo + 1}", "path": f"/beats/{hi}"}]
        names = [ctx.doc["beats"][x]["title"] for x in (lo, hi)]
        return {"summary": f"swapped \"{names[0]}\" and \"{names[1]}\"", "patch": ops}

    m = re.match(r"^(?:please\s+)?(?:shorten|trim)\s+(?P<b>.+)$", tl)
    if m and _beat_ref(ctx, m.group("b")) is not None:
        i = _beat_ref(ctx, m.group("b"))
        _guard(ctx, i)
        ids = ctx.doc["beats"][i]["segment_ids"]
        if len(ids) < 2:
            raise RefineError("that beat is down to one sentence; remove the beat instead")
        worst = min(ids, key=lambda s: (ctx.pool[s]["score"], -ctx.story[s]))
        return {"summary": f"dropped the weakest sentence of \"{ctx.doc['beats'][i]['title']}\": "
                           f"\"{_short(ctx.pool[worst]['text'])}\"",
                "patch": _remove_ops(ctx, worst)}

    m = re.match(r"^(?:please\s+)?(?:drop|remove|cut|delete|lose|kill|skip|take out|get rid of|"
                 r"haal|verwijder|schrap)\s+(?:out\s+)?(?P<q>.+?)(?:\s+(?:out|eruit|weg))?$", tl)
    if m:
        q = m.group("q")
        topics = rf"{T_ZOOM}|{T_BROLL}|{T_CAPS}|{T_MUSIC}|{T_SFX}|pauses|silences|gaps|dead air"
        if re.match(rf"(?:the\s+|all\s+(?:the\s+)?|some\s+(?:of\s+)?(?:the\s+)?)?(?:{topics})\b",
                    q) or re.match(r"(?:it\s+)?(?:down\s+)?to\s+\d", q):
            return None  # "remove the music", "cut it to 2:30": tweaks, not sentences
        i = _beat_ref(ctx, q)
        if i is not None:
            _guard(ctx, i)
            return {"summary": f"removed the beat \"{ctx.doc['beats'][i]['title']}\"",
                    "patch": [{"op": "remove", "path": f"/beats/{i}"}]}
        n = re.fullmatch(r"(?:the\s+)?(\w+)\s+sentence(?:\s+(?:of|in)\s+(?P<b>.+))?", q)
        if n and n.group(1) in ORDINALS:
            scope = _beat_ref(ctx, n.group("b")) if n.group("b") else None
            ids = ctx.doc["beats"][scope]["segment_ids"] if scope is not None else ctx.kept()
            k = ORDINALS[n.group(1)]
            if not ids or abs(k) > len(ids):
                raise RefineError("there is no such sentence")
            sid = ids[k - 1 if k > 0 else k]
        else:
            sid = _pick(ctx, q, ctx.kept(), "kept")
        return {"summary": f"removed \"{_short(ctx.pool[sid]['text'])}\"",
                "patch": _remove_ops(ctx, sid)}

    m = re.match(r"^(?:please\s+)?(?:bring back|restore|put back|add back|re-?add|reinstate|"
                 r"keep(?!\s+it\b)|zet terug|breng terug)\s+(?P<q>.+)$", tl)
    if m:
        candidates = ctx.dropped() + ctx.unused()
        sid = _pick(ctx, m.group("q"), candidates, "dropped or unused")
        ops = []
        loc = _where(ctx, sid)
        skip: set[int] = set()
        if loc is not None:
            i, key, j = loc
            if ctx.doc["beats"][i].get("locked"):
                skip.add(i)
            else:
                # Back into its own beat, at its spoken position.
                ids = ctx.doc["beats"][i]["segment_ids"]
                pos = sum(1 for s in ids if ctx.story[s] < ctx.story[sid])
                ops = [{"op": "remove", "path": f"/beats/{i}/{key}/{j}"},
                       {"op": "add", "path": f"/beats/{i}/segment_ids/{pos}", "value": sid}]
        if not ops:
            i, pos = _story_slot(ctx, sid, skip)
            ops = [{"op": "add", "path": f"/beats/{i}/segment_ids/{pos}", "value": sid}]
        return {"summary": f"restored \"{_short(ctx.pool[sid]['text'])}\"", "patch": ops}
    return None


def _open_on(ctx: Ctx, q: str) -> dict:
    sid = _pick(ctx, q, list(ctx.pool), "")
    beats = ctx.doc["beats"]
    hook = 0 if beats and beats[0]["role"] == "hook" else None
    if hook is not None:
        _guard(ctx, hook)
        if beats[hook]["segment_ids"] == [sid]:
            raise RefineError("the edit already opens on that sentence")
    ops: list[dict] = []
    # 1. Take the sentence out of wherever it is now.
    loc = _where(ctx, sid)
    work = deepcopy(ctx.doc)
    if loc is not None:
        i, key, j = loc
        if i != hook:
            _guard(ctx, i)
        ops.append({"op": "remove", "path": f"/beats/{i}/{key}/{j}"})
        work["beats"][i][key].pop(j)
    shadow = Ctx.__new__(Ctx)
    shadow.__dict__.update({**ctx.__dict__, "doc": work})
    text = ctx.pool[sid]["text"]
    if hook is not None:
        # 2. The old hook's sentences go back to their place in the story.
        for old in [s for s in work["beats"][hook]["segment_ids"] if s != sid]:
            i, pos = _story_slot(shadow, old, {hook})
            ops.append({"op": "remove",
                        "path": f"/beats/{hook}/segment_ids/"
                                f"{work['beats'][hook]['segment_ids'].index(old)}"})
            work["beats"][hook]["segment_ids"].remove(old)
            ops.append({"op": "add", "path": f"/beats/{i}/segment_ids/{pos}", "value": old})
            work["beats"][i]["segment_ids"].insert(pos, old)
        ops += [{"op": "add", "path": f"/beats/{hook}/segment_ids/0", "value": sid},
                {"op": "replace", "path": f"/beats/{hook}/title", "value": plan._title(text)},
                {"op": "replace", "path": f"/beats/{hook}/emphasis",
                 "value": plan._emphasis(text)}]
    else:
        ops.append({"op": "add", "path": "/beats/0", "value": {
            "id": _new_beat_id(ctx), "role": "hook", "title": plan._title(text),
            "intent": "open on the creator's pick", "segment_ids": [sid], "dropped_ids": [],
            "emphasis": plan._emphasis(text), "music_energy": "high", "locked": False}})
    return {"summary": f"opens on \"{_short(text)}\"", "patch": ops, "hook_segment": sid}


def _end_on(ctx: Ctx, q: str) -> dict:
    sid = _pick(ctx, q, list(ctx.pool), "")
    beats = ctx.doc["beats"]
    target = next((i for i in range(len(beats) - 1, -1, -1)
                   if beats[i]["role"] != "cta" and not beats[i].get("locked")), None)
    if target is None:
        raise RefineError("every closing beat is locked")
    ops = []
    loc = _where(ctx, sid)
    n_target = len(beats[target]["segment_ids"])
    if loc is not None:
        i, key, j = loc
        _guard(ctx, i)
        ops.append({"op": "remove", "path": f"/beats/{i}/{key}/{j}"})
        if i == target and key == "segment_ids":
            n_target -= 1
    ops.append({"op": "add", "path": f"/beats/{target}/segment_ids/{n_target}", "value": sid})
    return {"summary": f"ends on \"{_short(ctx.pool[sid]['text'])}\"", "patch": ops}


def _move_relative(ctx: Ctx, q: str, rel: str, r: str) -> dict:
    sid = _pick(ctx, q, list(ctx.pool), "")
    anchor = _pick(ctx, r, [s for s in ctx.kept() if s != sid], "kept")
    work = deepcopy(ctx.doc)
    ops = []
    loc = _where(ctx, sid)
    if loc is not None:
        i, key, j = loc
        _guard(ctx, i)
        ops.append({"op": "remove", "path": f"/beats/{i}/{key}/{j}"})
        work["beats"][i][key].pop(j)
    for i, b in enumerate(work["beats"]):
        if anchor in b["segment_ids"]:
            _guard(ctx, i)
            pos = b["segment_ids"].index(anchor) + (1 if rel == "after" else 0)
            ops.append({"op": "add", "path": f"/beats/{i}/segment_ids/{pos}", "value": sid})
            break
    return {"summary": f"moved \"{_short(ctx.pool[sid]['text'])}\" {rel} "
                       f"\"{_short(ctx.pool[anchor]['text'], 6)}\"", "patch": ops}


# --- JSON Patch (RFC 6902, the subset a plan needs) -----------------------------------

def _resolve(doc, path: str):
    if not path.startswith("/"):
        raise RefineError(f"bad patch path {path!r}")
    parts = [p.replace("~1", "/").replace("~0", "~") for p in path[1:].split("/")]
    node = doc
    for p in parts[:-1]:
        node = node[int(p)] if isinstance(node, list) else node[p]
    return node, parts[-1]


def apply_patch(doc: dict, ops: list[dict]) -> dict:
    out = deepcopy(doc)
    for op in ops:
        kind = op.get("op")
        try:
            if kind == "test":
                parent, key = _resolve(out, op["path"])
                got = parent[int(key)] if isinstance(parent, list) else parent[key]
                if got != op["value"]:
                    raise RefineError(f"patch test failed at {op['path']}")
                continue
            if kind in ("remove", "replace", "move", "copy"):
                src = op["from"] if kind in ("move", "copy") else op["path"]
                parent, key = _resolve(out, src)
                if isinstance(parent, list):
                    value = parent[int(key)]
                    if kind in ("remove", "move"):
                        parent.pop(int(key))
                else:
                    value = parent[key]
                    if kind in ("remove", "move"):
                        del parent[key]
                if kind == "remove":
                    continue
                if kind == "replace":
                    value = op["value"]
                    if isinstance(parent, list):
                        parent.insert(int(key), value)
                    else:
                        parent[key] = value
                    continue
                value = deepcopy(value)
            elif kind == "add":
                value = deepcopy(op["value"])
            else:
                raise RefineError(f"unsupported patch op {kind!r}")
            parent, key = _resolve(out, op["path"])
            if isinstance(parent, list):
                idx = len(parent) if key == "-" else int(key)
                if not 0 <= idx <= len(parent):
                    raise RefineError(f"patch index out of range at {op['path']}")
                parent.insert(idx, value)
            else:
                parent[key] = value
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            if isinstance(exc, RefineError):
                raise
            raise RefineError(f"patch does not fit the plan at {op.get('path')}: {exc}") from exc
    return out


def check_patched(ctx: Ctx, beats: list[dict]) -> list[str]:
    errors = []
    for b in beats:
        for k in ("id", "role", "title", "segment_ids"):
            if k not in b:
                errors.append(f"a beat is missing {k}")
    if errors:
        return errors
    ids = [b["id"] for b in beats]
    if len(set(ids)) != len(ids):
        errors.append("two beats share an id")
    errors += plan.validate(beats, ctx.pool, [b for b in ctx.doc["beats"] if b.get("locked")])
    # A locked beat keeps its place in the order as well as its content.
    locked = [b["id"] for b in ctx.doc["beats"] if b.get("locked")]
    if [i for i in ids if i in locked] != locked:
        errors.append("locked beats moved")
    return errors


# --- LLM path --------------------------------------------------------------------------

REFINE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["kind", "summary", "tweaks", "patch", "question"],
    "properties": {
        "kind": {"type": "string", "enum": ["tweak", "structural", "unclear"]},
        "summary": {"type": "string"},
        "tweaks": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["param", "op", "value"],
            "properties": {"param": {"type": "string", "enum": list(PARAMS)},
                           "op": {"type": "string", "enum": ["set", "scale", "add"]},
                           "value": {"type": ["number", "string"]}}}},
        "patch": {"type": "array", "items": {
            "type": "object", "required": ["op", "path"],
            "properties": {"op": {"type": "string",
                                  "enum": ["add", "remove", "replace", "move", "test"]},
                           "path": {"type": "string"}, "from": {"type": "string"},
                           "value": {}}}},
        "question": {"type": "string"},
    },
}

REFINE_PROMPT = """You turn a creator's instruction into a change to their video edit.

Two kinds of change exist. A TWEAK changes style parameters and never the story: use the \
"tweaks" list with params from the schema; "scale" multiplies the current value, "add" adds \
to it, "set" replaces it. A STRUCTURAL change edits which sentences play and in what order: \
use "patch", an RFC 6902 JSON Patch against the plan document below (paths like \
/beats/2/segment_ids/0). Sentence ids look like c1:s004. Locked beats must not change or move. \
Prefer the smallest patch that does what was asked. If the instruction is ambiguous, set kind \
"unclear" and ask one short question.

Current style parameters:
{params}

Plan document:
{plan}

Sentences not in the edit (dropped by the length solver, or never used):
{available}"""


def _llm_view(ctx: Ctx, aliases: plan.Aliases) -> tuple[str, str, str]:
    params = {p: ctx.get(p) for p in PARAMS if not p.startswith("brief.")}
    params.update({p: ctx.get(p) for p in PARAMS if p.startswith("brief.")})
    view = {"beats": [{"id": b["id"], "role": b["role"], "title": b["title"],
                       "locked": b.get("locked", False),
                       "segment_ids": [aliases.short(s) for s in b["segment_ids"]],
                       "text": [ctx.pool[s]["text"] for s in b["segment_ids"]]}
                      for b in ctx.doc["beats"]]}
    avail = [f"{aliases.short(s)}: {ctx.pool[s]['text']}"
             for s in sorted(ctx.dropped() + ctx.unused(), key=lambda s: ctx.story[s])][:200]
    return (json.dumps(params, ensure_ascii=False), json.dumps(view, ensure_ascii=False),
            "\n".join(avail) or "(none)")


def _unalias(value, aliases: plan.Aliases):
    if isinstance(value, str) and re.fullmatch(r"c\d+:\S+", value):
        return aliases.long(value)
    if isinstance(value, list):
        return [_unalias(v, aliases) for v in value]
    if isinstance(value, dict):
        return {k: _unalias(v, aliases) for k, v in value.items()}
    return value


def interpret_llm(ctx: Ctx, instruction: str, ep: llm.Endpoint, log: Log = print) -> dict:
    aliases = plan.Aliases(ctx.index)
    params, view, avail = _llm_view(ctx, aliases)
    # The model sees the patchable document with short ids and text alongside; the patch
    # it returns is applied to the same document without the text.
    messages = [{"role": "system", "content": REFINE_PROMPT.format(params=params, plan=view,
                                                                   available=avail)},
                {"role": "user", "content": instruction}]
    for _ in range(2):
        reply = llm.chat(ep, messages, schema=REFINE_SCHEMA, schema_name="refinement",
                         max_tokens=2048, temperature=0.1)
        try:
            raw = llm.parse_json(reply.get("content"))
            out = _from_llm(ctx, raw, aliases)
            out["by"] = f"llm:{ep.model}"
            return out
        except (RefineError, ValueError, KeyError, TypeError) as exc:
            log(f"refinement rejected: {exc}; asking for a repair")
            messages += [{"role": "assistant", "content": reply.get("content") or ""},
                         {"role": "user", "content": f"That cannot be applied: {exc}. "
                                                     "Reply with a corrected JSON object."}]
    raise RefineError("the model's change could not be applied after a repair round")


def _from_llm(ctx: Ctx, raw: dict, aliases: plan.Aliases) -> dict:
    kind = raw.get("kind")
    if kind == "unclear":
        return {"kind": "unclear", "summary": "", "message": raw.get("question") or
                "Can you say that another way?"}
    if kind == "tweak":
        changes = [_change(ctx, t["param"], t["op"], t["value"]) for t in raw.get("tweaks", [])]
        if not changes:
            raise RefineError("a tweak with no parameters")
        return {"kind": "tweak", "summary": raw.get("summary", ""), "changes": changes}
    if kind == "structural":
        patch = [{**op, **({"value": _unalias(op["value"], aliases)} if "value" in op else {})}
                 for op in raw.get("patch", [])]
        if not patch:
            raise RefineError("a structural change with an empty patch")
        doc = {"beats": [{k: b[k] for k in b} for b in ctx.doc["beats"]]}
        beats = apply_patch(doc, patch)["beats"]
        errors = check_patched(ctx, beats)
        if errors:
            raise RefineError("; ".join(errors[:5]))
        return {"kind": "structural", "summary": raw.get("summary", ""), "patch": patch}
    raise RefineError(f"unknown kind {kind!r}")


# --- interpret ---------------------------------------------------------------------------

def interpret(project: Project, instruction: str, use_llm: bool = True,
              log: Log = print) -> dict:
    """Classify and resolve one instruction without changing anything."""
    text = " ".join(instruction.split())
    if not text:
        raise RefineError("say what to change")
    if re.match(r"^(?:undo|revert|go back|take that back|ongedaan|maak ongedaan)\b", text.lower()):
        return {"kind": "undo", "summary": "undo the last change", "by": "rules"}
    ctx = Ctx(project)
    s = _structural(ctx, text)
    if s is not None:
        return {"kind": "structural", "by": "rules", **s}

    changes, unmapped = [], []
    for clause in _clauses(text.lower()):
        got = None
        for rule in TWEAKS:
            got = rule(ctx, clause)
            if got:
                break
        if got:
            changes += got
        else:
            unmapped.append(clause)
    if changes and not unmapped:
        return {"kind": "tweak", "by": "rules", "changes": _merge(changes),
                "summary": "; ".join(_label(c) for c in _merge(changes))}

    ep = llm.planner_endpoint() if use_llm else None
    if ep is not None and llm.ping(ep):
        return interpret_llm(ctx, text, ep, log)
    if changes:
        merged = _merge(changes)
        return {"kind": "tweak", "by": "rules", "changes": merged,
                "summary": "; ".join(_label(c) for c in merged),
                "unmapped": unmapped}
    return {"kind": "unclear", "by": "rules", "summary": "",
            "message": "I could not map that to a change, and no LLM is reachable to read it. "
                       "Try: \"less zoom\", \"music quieter\", \"punchier first 15 seconds\", "
                       "\"drop the part about pricing\", \"open on <words> instead\", "
                       "\"make it 2:30\", \"warmer\", \"undo\"."}


def _merge(changes: list[dict]) -> list[dict]:
    out: dict[str, dict] = {}
    for c in changes:
        if c["param"] in out:
            out[c["param"]] = {**c, "from": out[c["param"]]["from"]}
        else:
            out[c["param"]] = c
    return list(out.values())


# --- apply, history, undo ----------------------------------------------------------------

def history_path(project: Project):
    return project.path("plans", "refinements.json")


def history(project: Project) -> list[dict]:
    path = history_path(project)
    return read_json(path)["steps"] if path.exists() else []


def _save_history(project: Project, steps: list[dict]) -> None:
    write_json(history_path(project), {"steps": steps})


def _brief_with(brief: dict, changes: list[dict]) -> tuple[dict, bool]:
    """The brief after a tweak, and whether the plan must be re-solved."""
    new = deepcopy(brief)
    new.setdefault("overrides", {})
    resolve = False
    for c in changes:
        p, v = c["param"], c["to"]
        resolve |= p in RESOLVE
        if p.startswith("brief."):
            node = new
            keys = p.split(".")[1:]
            for k in keys[:-1]:
                node = node.setdefault(k, {})
            node[keys[-1]] = v
            if p == "brief.style":
                new["overrides"] = {}  # tweaks were relative to the old style
        else:
            section, key = p.split(".")
            new["overrides"].setdefault(section, {})[key] = v
    return new, resolve


def apply(project: Project, interp: dict, instruction: str, log: Log = print) -> dict:
    """Write the change: brief overrides and/or a new plan version. Returns the step."""
    if interp["kind"] == "undo":
        return undo(project, log)
    if interp["kind"] == "unclear":
        raise RefineError(interp.get("message") or "unclear instruction")
    ctx = Ctx(project)
    brief_before = deepcopy(ctx.brief)
    plan_before = ctx.doc["version"]
    doc = deepcopy(ctx.doc)
    labelled = 0
    if interp["kind"] == "tweak":
        new_brief, resolve = _brief_with(ctx.brief, interp["changes"])
        brief = plan.save_brief(project, new_brief)
        if resolve:
            doc["style"] = brief["style"]
            doc["brief"] = brief
            style = styles.for_brief(doc["style"], brief)
            doc = plan._save(project, doc, style, plan.segment_lengths(ctx.index, style), log)
    else:
        patched = apply_patch({"beats": doc["beats"]}, interp["patch"])["beats"]
        errors = check_patched(ctx, patched)
        if errors:
            raise RefineError("; ".join(errors[:5]))
        kept_now = {s for b in patched for s in b["segment_ids"]}
        for b in patched:  # a sentence placed by hand is no longer the solver's drop
            b["dropped_ids"] = [s for s in b.get("dropped_ids", []) if s not in kept_now]
            b.setdefault("dropped_ids", [])
            b.setdefault("emphasis", [])
            b.setdefault("music_energy", "mid")
            b.setdefault("locked", False)
            b.setdefault("intent", "")
        before_all = {s for b in doc["beats"] for s in b["segment_ids"] + b.get("dropped_ids", [])}
        after_all = kept_now | {s for b in patched for s in b["dropped_ids"]}
        kept_before = {s for b in doc["beats"] for s in b["segment_ids"]}
        removed = (kept_before | before_all) - after_all
        added = kept_now - kept_before
        new = {**doc, "beats": patched, "questions": [], "brief": ctx.brief,
               "excluded_ids": sorted((set(doc.get("excluded_ids", [])) | removed) - added),
               "pinned_ids": sorted((set(doc.get("pinned_ids", [])) | added) - removed)}
        if interp.get("hook_segment"):
            new["pinned_ids"] = sorted(set(new["pinned_ids"]) | {interp["hook_segment"]})
            length = plan.segment_lengths(ctx.index, ctx.style)["hook"].get(
                interp["hook_segment"], 0.0)
            if length > ctx.style["hook"]["max_s"]:
                # The creator chose this opening; the cap gives way, and says so.
                brief, _ = _brief_with(ctx.brief, [_change(ctx, "hook.max_s", "set",
                                                           math.ceil(length) + 1)])
                ctx.brief = plan.save_brief(project, brief)
                ctx.style = styles.for_brief(doc["style"], ctx.brief)
                new["brief"] = ctx.brief
                interp["summary"] += f" (hook cap raised to {math.ceil(length) + 1} s to fit it)"
        labelled = learn.from_plans(project.id, ctx.index, doc["beats"], new["beats"], "refine")
        doc = plan._save(project, new, ctx.style, plan.segment_lengths(ctx.index, ctx.style), log)

    steps = history(project)
    step = {"id": len(steps) + 1, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "instruction": instruction, "kind": interp["kind"], "by": interp.get("by", "rules"),
            "summary": interp.get("summary", ""), "changes": interp.get("changes", []),
            "patch": interp.get("patch", []), "unmapped": interp.get("unmapped", []),
            "brief_before": brief_before, "plan_before": plan_before,
            "plan_after": doc["version"], "labelled": labelled, "undone": False}
    steps.append(step)
    _save_history(project, steps)
    return step


def update_step(project: Project, step: dict) -> None:
    steps = history(project)
    for n, s in enumerate(steps):
        if s["id"] == step["id"]:
            steps[n] = step
    _save_history(project, steps)


def undo(project: Project, log: Log = print) -> dict:
    steps = history(project)
    last = next((s for s in reversed(steps) if not s["undone"] and s["kind"] != "undo"), None)
    if last is None:
        raise RefineError("nothing to undo")
    plan.save_brief(project, last["brief_before"])
    version = last["plan_before"]
    current = plan.latest_plan(project, with_beats=True)
    if current and current["version"] != version:
        restored = plan.restore_version(project, version)
        version = restored["version"]
    last["undone"] = True
    step = {"id": len(steps) + 1, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "instruction": "undo", "kind": "undo", "by": "rules",
            "summary": f"undid \"{last['instruction']}\"", "changes": [], "patch": [],
            "unmapped": [], "brief_before": None, "plan_before": last["plan_after"],
            "plan_after": version, "labelled": 0, "undone": False, "undoes": last["id"]}
    steps.append(step)
    _save_history(project, steps)
    return step

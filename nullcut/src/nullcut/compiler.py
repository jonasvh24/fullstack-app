"""Rough-cut compiler: Clip Index in, ordered cut list out. No model, no randomness.

Phase 0 has no planner, so story order is clip order then time, and the only
creative decisions are rules 1 of plan §6: trim to word boundaries, remove
silences over threshold and filler words. Each item records which rules
shaped it, so a cut in the UI can explain itself.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from . import config

TOKEN = re.compile(r"[^\w']+", re.UNICODE)


@dataclass
class Item:
    clip_id: str
    segment_id: str
    src_in: float
    src_out: float
    frames: int = 0
    words: list[dict] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)
    beat_id: str = ""

    @property
    def duration(self) -> float:
        return self.src_out - self.src_in


@dataclass
class CutOptions:
    target_s: float | None = None
    tolerance: float = config.TARGET_TOLERANCE
    silence_s: float = config.SILENCE_THRESHOLD_S
    fps: float = config.DEFAULT_FPS
    remove_fillers: bool = True
    locked: set[str] = field(default_factory=set)  # segment ids the fit may not drop


@dataclass
class CutResult:
    items: list[Item]
    report: dict


def token(word: str) -> str:
    return TOKEN.sub("", word.lower())


def filler_mask(words: list[dict], segments: list[dict], lang: str) -> list[str | None]:
    """Per word: None to keep, or the reason it goes."""
    hard = config.HARD_FILLERS.get(lang, config.HARD_FILLERS["en"])
    soft = config.SOFT_FILLERS.get(lang, set())
    pause = config.SOFT_FILLER_PAUSE_S
    seg_first = {s["words"][0] for s in segments}
    reasons: list[str | None] = []
    for i, w in enumerate(words):
        t = token(w["w"])
        before = w["s"] - words[i - 1]["e"] if i > 0 else math.inf
        after = words[i + 1]["s"] - w["e"] if i + 1 < len(words) else math.inf
        if t in hard:
            reasons.append(f"filler '{t}'")
        elif t in soft and ((before >= pause and after >= pause)
                            or (i in seg_first and after >= pause)):
            reasons.append(f"soft filler '{t}'")
        else:
            reasons.append(None)
    return reasons


def min_cut_gap_s() -> float:
    """Shortest pause worth cutting: both handles plus 50 ms of real removal."""
    return config.HANDLE_PRE_S + config.HANDLE_POST_S + 0.05


def clip_spans(clip: dict, silence_s: float, remove_fillers: bool) -> tuple[list[Item], dict]:
    """Split each segment's kept words into spans at long gaps and removed fillers."""
    words, segments = clip["words"], clip["segments"]
    pre, post = config.HANDLE_PRE_S, config.HANDLE_POST_S
    reasons = (filler_mask(words, segments, clip.get("language") or "en")
               if remove_fillers else [None] * len(words))
    stats = {"fillers": 0, "filler_s": 0.0}
    for w, r in zip(words, reasons):
        if r:
            stats["fillers"] += 1
            stats["filler_s"] += w["e"] - w["s"]

    # A cut needs real material to remove, or it is just a visible jump.
    min_cut_gap = max(silence_s, min_cut_gap_s())
    raw: list[tuple[str, list[int], list[str]]] = []  # (segment, word indices, fillers)
    for seg in segments:
        current: list[int] = []
        pending: list[str] = []
        removed_between: list[int] = []
        for i in range(*seg["words"]):
            if reasons[i]:
                removed_between.append(i)
                continue
            if current:
                gap = words[i]["s"] - words[current[-1]]["e"]
                if removed_between or gap > min_cut_gap:
                    raw.append((seg["id"], current, pending))
                    current, pending = [], []
            pending.extend(reasons[j] for j in removed_between)
            removed_between = []
            current.append(i)
        if current:
            raw.append((seg["id"], current, pending))

    # Nearest removed filler on each side of every word, precomputed so span
    # bounding stays linear on two-hour projects.
    n_words = len(words)
    removed_end_before = [0.0] * (n_words + 1)
    for i, w in enumerate(words):
        removed_end_before[i + 1] = w["e"] if reasons[i] else removed_end_before[i]
    removed_start_after = [clip["duration"]] * (n_words + 1)
    for i in range(n_words - 1, -1, -1):
        removed_start_after[i] = words[i]["s"] if reasons[i] else removed_start_after[i + 1]

    items: list[Item] = []
    for n, (seg_id, idx, fillers) in enumerate(raw):
        first, last = words[idx[0]], words[idx[-1]]
        # Handles must not reach into a removed filler or the neighbouring span.
        prev_word_end = words[raw[n - 1][1][-1]]["e"] if n > 0 else 0.0
        next_word_start = words[raw[n + 1][1][0]]["s"] if n + 1 < len(raw) else clip["duration"]
        # A pause the style keeps stays whole, between sentences as within them: the
        # spans reach across it and meet, so it plays as one continuous take.
        keep_before = n > 0 and first["s"] - prev_word_end <= min_cut_gap \
            and raw[n - 1][0] != seg_id
        keep_after = n + 1 < len(raw) and next_word_start - last["e"] <= min_cut_gap \
            and raw[n + 1][0] != seg_id
        start = max(prev_word_end if keep_before else first["s"] - pre, prev_word_end,
                    removed_end_before[idx[0]], 0.0)
        end = min(next_word_start if keep_after else last["e"] + post,
                  removed_start_after[idx[-1] + 1],
                  clip["duration"])
        if items and start < items[-1].src_out:
            mid = (items[-1].src_out + start) / 2
            items[-1].src_out = start = mid
        items.append(Item(clip["id"], seg_id, start, end,
                          words=[{"w": words[i]["w"], "s": words[i]["s"], "e": words[i]["e"]}
                                 for i in idx],
                          rules=list(fillers)))

    items = [it for it in items if it.duration >= config.MIN_SPAN_S]
    prev_out = 0.0
    for it in items:
        removed = it.src_in - prev_out
        if removed > 0.01:
            label = "trim clip head" if prev_out == 0.0 else "jump cut"
            it.rules.append(f"{label}: removed {removed:.2f} s")
        if not it.rules:
            it.rules.append("continuous: sentence boundary")
        prev_out = it.src_out
    return items, stats


def build(clips: list[dict], silence_s: float, remove_fillers: bool) -> tuple[list[Item], dict]:
    items: list[Item] = []
    stats = {"fillers": 0, "filler_s": 0.0}
    for clip in clips:
        clip_items, s = clip_spans(clip, silence_s, remove_fillers)
        items.extend(clip_items)
        stats["fillers"] += s["fillers"]
        stats["filler_s"] += s["filler_s"]
    return items, stats


def total(items: list[Item]) -> float:
    return sum(it.duration for it in items)


def _solve_cpsat(seg_len: dict[str, float], scores: dict[str, float], target: float,
                 tolerance: float, locked: set[str]) -> set[str] | None:
    """Plan §6: max sum(s_i x_i) s.t. 0.95T <= sum(l_i x_i) <= 1.05T, locked kept.

    When no selection lands in the window, a second solve minimises the
    distance to T instead, with score as the tie-break. Returns the dropped
    segment ids, or None when OR-Tools is not installed.
    """
    try:
        from ortools.sat.python import cp_model
    except ImportError:
        return None

    ids = list(seg_len)
    ms = {s: max(1, round(seg_len[s] * 1000)) for s in ids}
    pts = {s: round(scores.get(s, 0.5) * 1000) for s in ids}
    lo, hi, t = (round(target * f * 1000) for f in (1 - tolerance, 1 + tolerance, 1))

    def solve(in_window: bool) -> set[str] | None:
        m = cp_model.CpModel()
        x = {s: m.NewBoolVar(s) for s in ids}
        for s in locked & set(ids):
            m.Add(x[s] == 1)
        kept = sum(ms[s] * x[s] for s in ids)
        score = sum(pts[s] * x[s] for s in ids)
        if in_window:
            m.Add(kept >= lo)
            m.Add(kept <= hi)
            m.Maximize(score)
        else:
            dev = m.NewIntVar(0, sum(ms.values()) + t, "dev")
            m.AddAbsEquality(dev, kept - t)
            m.Minimize(dev * (sum(pts.values()) + 1) - score)
        solver = cp_model.CpSolver()
        # One worker keeps the choice between equal-score solutions stable:
        # the same input must always give the same cut.
        solver.parameters.num_workers = 1
        solver.parameters.random_seed = 0
        solver.parameters.max_time_in_seconds = 5.0
        status = solver.Solve(m)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return None
        return {s for s in ids if not solver.Value(x[s])}

    in_window = solve(True)
    if in_window is not None:
        return in_window
    nearest = solve(False)
    return nearest if nearest is not None else set()


def _greedy(seg_len: dict[str, float], scores: dict[str, float], target: float,
            tolerance: float, locked: set[str]) -> set[str]:
    lo, hi = target * (1 - tolerance), target * (1 + tolerance)
    order = [s for s in sorted(seg_len, key=lambda s: (scores.get(s, 0.5), -seg_len[s]))
             if s not in locked]
    dropped: set[str] = set()
    current = sum(seg_len.values())
    for seg in order:
        if current <= hi:
            break
        if current - seg_len[seg] >= lo:
            dropped.add(seg)
            current -= seg_len[seg]
    if current > hi:
        remaining = [s for s in order if s not in dropped]
        if remaining:
            best = min(remaining, key=lambda s: abs(current - seg_len[s] - target))
            if abs(current - seg_len[best] - target) < abs(current - target):
                dropped.add(best)
    return dropped


def fit(items: list[Item], scores: dict[str, float], target: float, tolerance: float,
        locked: set[str] | None = None) -> tuple[list[Item], list[dict], str]:
    """Drop whole sentences to land on target. Story order is never touched.

    CP-SAT when OR-Tools is installed, a greedy lowest-score-first pass
    otherwise. Returns (kept items, dropped log, solver used).
    """
    locked = locked or set()
    seg_len: dict[str, float] = {}
    for it in items:
        seg_len[it.segment_id] = seg_len.get(it.segment_id, 0.0) + it.duration
    dropped = _solve_cpsat(seg_len, scores, target, tolerance, locked)
    solver = "cp-sat"
    if dropped is None:
        dropped, solver = _greedy(seg_len, scores, target, tolerance, locked), "greedy"
    order = sorted(dropped, key=lambda s: (scores.get(s, 0.5), -seg_len[s]))
    log = [{"segment": s, "seconds": round(seg_len[s], 2), "score": scores.get(s)}
           for s in order]
    return [it for it in items if it.segment_id not in dropped], log, solver


def contiguous(a: Item, b: Item, fps: float) -> bool:
    """True when b continues a's take with no material removed between them."""
    return a.clip_id == b.clip_id and abs(b.src_in - a.src_out) < 0.5 / fps


def quantise(items: list[Item], fps: float) -> list[Item]:
    """Snap every edge to the output frame grid.

    Both edges snap to the same absolute grid, so two items that touch in the
    source still touch after snapping and play as one continuous take.
    """
    out = []
    for it in items:
        src_in = round(it.src_in * fps) / fps
        src_out = round(it.src_out * fps) / fps
        frames = round((src_out - src_in) * fps)
        if frames < 1:
            continue
        it.src_in, it.src_out, it.frames = src_in, src_out, frames
        out.append(it)
    return out


def compile_rough(index: dict, opts: CutOptions) -> CutResult:
    clips = [c for c in index["clips"] if c["words"]]
    skipped = [c["name"] for c in index["clips"] if not c["words"]]
    scores = {s["id"]: s.get("score", 0.5) for c in clips for s in c["segments"]}
    source_s = sum(c["duration"] for c in index["clips"])

    silence = opts.silence_s
    items, stats = build(clips, silence, opts.remove_fillers)
    tightened = []
    if opts.target_s:
        hi = opts.target_s * (1 + opts.tolerance)
        for step in config.SILENCE_TIGHTEN_STEPS_S:
            if total(items) <= hi or step >= silence:
                continue
            silence = step
            items, stats = build(clips, silence, opts.remove_fillers)
            tightened.append(step)

    untrimmed = total(items)
    dropped: list[dict] = []
    solver = None
    if opts.target_s and untrimmed > opts.target_s * (1 + opts.tolerance):
        items, dropped, solver = fit(items, scores, opts.target_s, opts.tolerance, opts.locked)

    items = quantise(items, opts.fps)
    kept = sum(it.frames for it in items) / opts.fps
    speech_s = sum(w["e"] - w["s"] for c in clips for w in c["words"])
    cuts = sum(1 for a, b in zip(items, items[1:]) if not contiguous(a, b, opts.fps))

    report = {
        "source_s": round(source_s, 2),
        "clips": len(index["clips"]),
        "skipped_no_speech": skipped,
        "kept_s": round(kept, 2),
        "items": len(items),
        "jump_cuts": cuts,
        "dead_air_s": round(max(0.0, source_s - untrimmed - stats["filler_s"]), 2),
        "silence_threshold_s": silence,
        "tightened_to": tightened,
        "fillers": stats["fillers"],
        "filler_s": round(stats["filler_s"], 2),
        "dropped_segments": dropped,
        "dropped_s": round(sum(d["seconds"] for d in dropped), 2),
        "solver": solver,
        "speech_s": round(speech_s, 2),
        "target_s": opts.target_s,
        "tolerance": opts.tolerance,
    }
    if opts.target_s:
        lo, hi = opts.target_s * (1 - opts.tolerance), opts.target_s * (1 + opts.tolerance)
        report["target_met"] = lo <= kept <= hi
        # Never pad: a short cut is reported, not stretched with dead air.
        report["shortfall_s"] = round(max(0.0, lo - kept), 2)
        report["overshoot_s"] = round(max(0.0, kept - hi), 2)
    return CutResult(items, report)


STANDARD_RATES = (23.976, 24.0, 25.0, 29.97, 30.0, 50.0, 59.94, 60.0)


def pick_fps(index: dict) -> float:
    """Output rate: the most common source rate among speech clips, snapped to a standard one.

    Phone VFR averages land on 29.83 and similar; a viewer expects 29.97 or 30.
    """
    from collections import Counter

    rates = [c["fps"] for c in index["clips"] if c["words"] and c["fps"]]
    if not rates:
        return config.DEFAULT_FPS
    common = Counter(round(r, 2) for r in rates).most_common(1)[0][0]
    return min(STANDARD_RATES, key=lambda r: abs(r - common))

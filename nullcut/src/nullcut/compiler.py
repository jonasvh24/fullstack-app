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
    min_cut_gap = max(silence_s, pre + post + 0.05)
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
        start = max(first["s"] - pre, prev_word_end, removed_end_before[idx[0]], 0.0)
        next_word_start = words[raw[n + 1][1][0]]["s"] if n + 1 < len(raw) else clip["duration"]
        end = min(last["e"] + post, next_word_start, removed_start_after[idx[-1] + 1],
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


def fit(items: list[Item], scores: dict[str, float], target: float,
        tolerance: float) -> tuple[list[Item], list[dict]]:
    """Drop whole sentences, lowest score first, until inside the window.

    Greedy is enough for a talking head; Phase 1 swaps in CP-SAT with story
    and locked-beat constraints.
    """
    lo, hi = target * (1 - tolerance), target * (1 + tolerance)
    seg_len: dict[str, float] = {}
    for it in items:
        seg_len[it.segment_id] = seg_len.get(it.segment_id, 0.0) + it.duration
    order = sorted(seg_len, key=lambda s: (scores.get(s, 0.5), -seg_len[s]))
    dropped: set[str] = set()
    current = sum(seg_len.values())
    for seg in order:
        if current <= hi:
            break
        if current - seg_len[seg] >= lo:
            dropped.add(seg)
            current -= seg_len[seg]
    if current > hi:
        # Nothing fits the window exactly: take the single drop that lands
        # nearest the target, accepting a small undershoot over an overshoot.
        remaining = [s for s in order if s not in dropped]
        if remaining:
            best = min(remaining, key=lambda s: abs(current - seg_len[s] - target))
            if abs(current - seg_len[best] - target) < abs(current - target):
                dropped.add(best)
    log = [{"segment": s, "seconds": round(seg_len[s], 2), "score": scores.get(s)}
           for s in order if s in dropped]
    return [it for it in items if it.segment_id not in dropped], log


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
    if opts.target_s and untrimmed > opts.target_s * (1 + opts.tolerance):
        items, dropped = fit(items, scores, opts.target_s, opts.tolerance)

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

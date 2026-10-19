"""Compile an Edit Plan into a timeline: the rules of plan §6, in order.

No model runs here. The same plan, brief and style always give the same
timeline, and every element records the rule that created it, so a zoom in
the UI can answer "why is this here?".

  1  trim to word boundaries, remove silences and fillers  (compiler.py)
  2  snap cuts to music beats within ±120 ms, music-led styles only
  3  enforce max static seconds: zoom on an emphasis word, else B-roll, else zoom
  4  keyframed zooms on the tracked subject, eased, at the profile's scale
  5  word captions grouped per profile, emphasis words highlighted
  6  B-roll over jump cuts and long stretches, dialogue continuing under it
  7  music bed fitted to the edit, ducked under speech; SFX on the profile's share
  8  hard cuts (the shipped styles allow nothing else)
"""

from __future__ import annotations

import math
import re
from copy import deepcopy
from dataclasses import dataclass, field

import numpy as np

from . import compiler, music, plan as plan_mod, styles
from .compiler import Item, contiguous

SNAP_WINDOW_S = 0.12
ITEM_EDGE_GUARD_S = 0.25   # zooms start and stop clear of the cut
HOOK_FACE_S = 3.0          # the first seconds stay on the speaker's face
GOLDEN = (math.sqrt(5) - 1) / 2


@dataclass
class Composition:
    items: list[Item]
    beats: list[dict]
    zooms: list[dict] = field(default_factory=list)
    broll: list[dict] = field(default_factory=list)
    music: dict | None = None
    sfx: list[dict] = field(default_factory=list)
    captions: dict = field(default_factory=dict)
    colour: dict = field(default_factory=dict)
    report: dict = field(default_factory=dict)
    reframe: list = field(default_factory=list)


def _tok(w: str) -> str:
    return re.sub(r"[^\w']", "", w.lower())


def _starts(items: list[Item], fps: float) -> list[float]:
    out, cur = [], 0
    for it in items:
        out.append(cur / fps)
        cur += it.frames
    return out


def _cuts(items: list[Item], fps: float) -> list[float]:
    starts = _starts(items, fps)
    return [starts[k + 1] for k in range(len(items) - 1)
            if not contiguous(items[k], items[k + 1], fps)]


def _segment_items(index: dict, style: dict) -> dict[str, dict[str, list[Item]]]:
    clips = [c for c in index["clips"] if c["words"]]
    out = {}
    for key, ms in (("body", style["pacing"]["silence_ms"]),
                    ("hook", style["pacing"]["hook_silence_ms"])):
        items, _ = compiler.build(clips, ms / 1000, True)
        table: dict[str, list[Item]] = {}
        for it in items:
            table.setdefault(it.segment_id, []).append(it)
        out[key] = table
    return out


# --- rule 2 -----------------------------------------------------------------------

def _music_beats_out(track: dict, pieces: list[tuple[float, float]]) -> list[float]:
    out, offset = [], 0.0
    for a, b in pieces:
        out += [offset + (t - a) for t in track["beats"] if a <= t < b]
        offset += b - a
    return out


def snap_to_beats(items: list[Item], beats_from_end: list[float], index: dict,
                  fps: float) -> int:
    """Move each real cut onto the nearest beat within ±120 ms. Never into a word.

    The bed is fitted to end on its final downbeat, so the beat grid is fixed
    relative to the END of the edit and a cut's place on it depends only on
    what plays after it. Cut k therefore moves by changing the tail of item
    k+1 (the item it cuts to). Working from the last cut backwards, each
    change leaves every cut already placed where it is.
    """
    if not beats_from_end:
        return 0
    words_by_clip = {c["id"]: c["words"] for c in index["clips"]}
    dur_by_clip = {c["id"]: c["duration"] for c in index["clips"]}
    snapped = 0
    for k in range(len(items) - 2, -1, -1):
        if contiguous(items[k], items[k + 1], fps):
            continue
        dist = sum(it.frames for it in items[k + 1:]) / fps
        bt = min(beats_from_end, key=lambda b: abs(b - dist))
        delta = round((bt - dist) * fps)  # frames to add to item k+1
        if delta == 0 or abs(bt - dist) > SNAP_WINDOW_S:
            continue
        it = items[k + 1]
        if k + 2 < len(items) and contiguous(it, items[k + 2], fps):
            continue  # its tail is mid-take: moving it would open a jump
        last_word_end = max((w["e"] for w in it.words), default=it.src_in)
        new_out = it.src_out + delta / fps
        if delta < 0 and new_out < last_word_end + 0.02:
            continue
        if delta > 0:
            nxt = [w["s"] for w in words_by_clip[it.clip_id] if w["s"] >= it.src_out - 1e-6]
            limit = min(nxt[0] - 0.03 if nxt else dur_by_clip[it.clip_id],
                        dur_by_clip[it.clip_id])
            if new_out > limit:
                continue
        it.src_out = round(new_out * fps) / fps
        it.frames += delta
        items[k].rules.append(f"beat snap: next item {delta / fps * 1000:+.0f} ms so this "
                              "cut lands on the music")
        snapped += 1
    return snapped


# --- rules 3, 4, 6 ------------------------------------------------------------------

def _subject_centre(clip: dict, src_t: float) -> tuple[float, float]:
    faces = [f for f in clip.get("faces", []) if abs(f[0] - src_t) <= 1.0]
    if not faces:
        moving = [m for m in clip.get("motion", []) if abs(m[0] - src_t) <= 1.0]
        if moving:  # a screen recording: zoom where the action is
            m = min(moving, key=lambda m: abs(m[0] - src_t))
            return round(m[1], 4), round(m[2], 4)
        return 0.5, 0.45
    f = min(faces, key=lambda f: abs(f[0] - src_t))
    _, x, y, w, h, _ = f
    # Aim a little above the face centre: eyes, not chin, sit on the thirds line.
    return round(x + w / 2, 4), round(y + h * 0.42, 4)


class _Layout:
    """Output-time occupancy so rules never stack a zoom on a B-roll on a zoom."""

    def __init__(self, items: list[Item], fps: float):
        self.items, self.fps = items, fps
        self.starts = _starts(items, fps)
        self.duration = sum(it.frames for it in items) / fps
        self.busy: list[tuple[float, float]] = []
        self.face_until = 0.0

    def item_at(self, t: float) -> int:
        k = 0
        for i, s in enumerate(self.starts):
            if s <= t:
                k = i
        return k

    def item_bounds(self, k: int) -> tuple[float, float]:
        return self.starts[k], self.starts[k] + self.items[k].frames / self.fps

    def free(self, a: float, b: float) -> bool:
        return all(b <= x or a >= y for x, y in self.busy)

    def take(self, a: float, b: float) -> None:
        self.busy.append((a, b))


def place_zoom(layout: _Layout, clips: dict, word: dict, z: dict, scale: float,
               rule: str) -> dict | None:
    k = layout.item_at(word["s"])
    a_item, b_item = layout.item_bounds(k)
    start = max(word["s"] - z["ease_s"], a_item + ITEM_EDGE_GUARD_S)
    # The hold shrinks to fit a short item (down to 0.3 s) before the zoom is given up.
    end = min(start + 2 * z["ease_s"] + z["hold_s"], b_item - 0.05)
    if end - start < z["ease_s"] * 2 + 0.3 or not layout.free(start, end):
        return None
    it = layout.items[k]
    src_t = it.src_in + (word["s"] - a_item)
    cx, cy = _subject_centre(clips[it.clip_id], src_t)
    layout.take(start, end)
    return {"start_s": round(start, 3), "end_s": round(end, 3), "scale": round(scale, 3),
            "cx": cx, "cy": cy, "ease_s": z["ease_s"], "kind": "punch", "rule": rule,
            "word": word["w"]}


def place_broll(layout: _Layout, pool: list[dict], used: dict, a: float, length: float,
                spoken: list[dict], rule: str, min_len: float | None = None) -> dict | None:
    rooms = [s["end"] - s["start"] - 0.4 - used.get(s["id"], 0.0) for s in pool]
    if min_len is not None and rooms and max(rooms) < length:
        # A shorter insert beats none, down to the style's minimum.
        shorter = max(rooms)
        if shorter < min_len:
            return None
        a += (length - shorter) / 2
        length = shorter
    b = a + length
    if a < layout.face_until or b > layout.duration - 0.5 or not layout.free(a, b):
        return None
    terms = {_tok(w["w"]) for w in spoken if a <= w["s"] < b} - plan_mod.STOP
    best, best_score = None, -1.0
    for shot in pool:
        room = shot["end"] - shot["start"] - 0.4 - used.get(shot["id"], 0.0)
        if room < length:
            continue
        cap = {_tok(w) for w in (shot.get("caption") or "").split()}
        # Matching words beat a pretty shot; reuse is the last resort.
        score = 2.0 * len(terms & cap) + shot.get("score", 0.5) - 0.5 * (shot["id"] in used)
        if score > best_score:
            best, best_score = shot, score
    if best is None:
        return None
    src_in = best["start"] + 0.2 + used.get(best["id"], 0.0)
    used[best["id"]] = used.get(best["id"], 0.0) + length
    layout.take(a, b)
    matched = sorted(terms & {_tok(w) for w in (best.get("caption") or "").split()})
    return {"start_s": round(a, 3), "end_s": round(b, 3), "clip_id": best["clip_id"],
            "shot_id": best["id"], "src_in": round(src_in, 3),
            "rule": rule + (f" (matches: {', '.join(matched)})" if matched else "")}


REFRAME_STEP_S = 0.5
REFRAME_DEAD_ZONE = 0.04   # small head moves never move the frame
REFRAME_EASE = 0.35        # per step: the window follows, it does not snap


def subject_track(clip: dict, src_in: float, duration: float) -> list[list[float]]:
    """Horizontal subject centre over an item, smoothed for a 9:16 crop: [[t_local, cx], ...].

    Faces first; a screen recording with no face follows the region that moves.
    """
    samples = clip.get("faces") or []
    points = [(f[0], f[1] + f[3] / 2) for f in samples]
    if not points:
        points = [(m[0], m[1]) for m in clip.get("motion", [])]
    track, cx = [], None
    t = 0.0
    while True:
        src_t = src_in + t
        near = [c for st, c in points if abs(st - src_t) <= 1.0]
        target = float(np.median(near)) if near else (cx if cx is not None else 0.5)
        if cx is None:
            cx = target
        elif abs(target - cx) > REFRAME_DEAD_ZONE:
            cx = cx + REFRAME_EASE * (target - cx)
        track.append([round(t, 3), round(cx, 4)])
        if t >= duration:
            break
        t = min(duration, t + REFRAME_STEP_S)
    return track


def compose(index: dict, plan_doc: dict, brief: dict, fps: float,
            library: list[dict] | None = None) -> Composition:
    style = styles.apply_extras(styles.get(plan_doc["style"]), brief["extras"])
    clips = {c["id"]: c for c in index["clips"]}
    tables = _segment_items(index, style)

    # Rule 1 output, in plan order.
    items: list[Item] = []
    seg_beat: dict[str, dict] = {}
    for beat in plan_doc["beats"]:
        table = tables["hook" if beat["role"] == "hook" else "body"]
        for sid in beat["segment_ids"]:
            seg_beat[sid] = beat
            for it in table.get(sid, []):
                c = deepcopy(it)
                c.beat_id = beat["id"]
                items.append(c)
    items = compiler.quantise(items, fps)
    if not items:
        raise ValueError("plan has no usable material")

    # Rule 7 first: the beat grid has to exist before cuts can snap to it.
    track = music.choose(brief["music"], style, library or [])
    duration = sum(it.frames for it in items) / fps
    music_doc = None
    snapped = 0
    if track:
        pieces = music.fit_plan(track, duration)
        if style["music"]["beat_snap"]:
            from_end = [duration - b for b in _music_beats_out(track, pieces)]
            snapped = snap_to_beats(items, from_end, index, fps)
            duration = sum(it.frames for it in items) / fps
            pieces = music.fit_plan(track, duration)
        music_doc = {"id": track["id"], "track_id": track["id"], "name": track["name"],
                     "path": track["path"],
                     "licence": track["licence"], "bpm": track["bpm"], "pieces": pieces,
                     "duration": track["duration"],
                     "under_speech_db": style["music"]["under_speech_db"],
                     "gap_db": style["music"]["gap_db"],
                     "crossfade_s": min(0.08, style["music"]["crossfade_s"])}

    starts = _starts(items, fps)
    beats_out = []
    for beat in plan_doc["beats"]:
        ks = [k for k, it in enumerate(items) if it.beat_id == beat["id"]]
        if ks:
            beats_out.append({"id": beat["id"], "role": beat["role"], "title": beat["title"],
                              "start_s": round(starts[ks[0]], 3),
                              "end_s": round(starts[ks[-1]] + items[ks[-1]].frames / fps, 3),
                              "music_energy": beat.get("music_energy", "mid")})

    # Output-time words, each tagged with whether its beat asked to punch it.
    emph_by_beat = {b["id"]: {_tok(w) for w in b.get("emphasis", [])} for b in plan_doc["beats"]}
    words: list[dict] = []
    for it, t0 in zip(items, starts):
        t1 = t0 + it.frames / fps
        for w in it.words:
            a, b = max(t0, t0 + w["s"] - it.src_in), min(t1, t0 + w["e"] - it.src_in)
            if b > a:
                words.append({"w": w["w"], "s": a, "e": b,
                              "emph": _tok(w["w"]) in emph_by_beat.get(it.beat_id, set())})

    layout = _Layout(items, fps)
    hook = next((b for b in beats_out if b["role"] == "hook"), None)
    if hook:
        # The opening stays on the speaker's face; zooms are still allowed there.
        layout.face_until = hook["start_s"] + HOOK_FACE_S
    z = style["zoom"]
    zooms: list[dict] = []

    # Rule 4: punches on emphasis words at the profile's rate, or slow drifts.
    want = int(round(z["per_minute"] * duration / 60))
    if z["drift"]:
        longest = sorted(range(len(items)), key=lambda k: -items[k].frames)[:max(0, want)]
        for k in sorted(longest):
            a, b = layout.item_bounds(k)
            if b - a < 3.0 or not layout.free(a, b):
                continue
            it = items[k]
            cx, cy = _subject_centre(clips[it.clip_id], it.src_in)
            layout.take(a, b)
            zooms.append({"start_s": round(a, 3), "end_s": round(b, 3), "scale": z["scale_max"],
                          "cx": cx, "cy": cy, "ease_s": b - a, "kind": "drift",
                          "rule": "slow push-in: cinematic drift on a long take"})
    else:
        min_gap = max(z["hold_s"] + 2 * z["ease_s"], 30.0 / max(z["per_minute"], 0.1))
        placed_at: list[float] = []

        def punch(cands: list[dict], scale: float, why: str) -> None:
            for w in cands:
                if len(zooms) >= want:
                    return
                if any(abs(w["s"] - t) < min_gap for t in placed_at):
                    continue
                zm = place_zoom(layout, clips, w, z, scale, f"{why} '{w['w']}'")
                if zm:
                    zooms.append(zm)
                    placed_at.append(w["s"])

        punch([w for w in words if w["emph"]], z["scale_max"], "zoom punch on emphasis word")
        # Cadence is a style parameter: when the plan names too few emphasis words,
        # the longest content word of each item carries the punch instead.
        stressed = []
        for k in range(len(items)):
            a, b = layout.item_bounds(k)
            inside = [w for w in words if a <= w["s"] < b and _tok(w["w"]) not in plan_mod.STOP]
            if inside:
                stressed.append(max(inside, key=lambda w: len(_tok(w["w"]))))
        punch(stressed, z["scale_min"], "zoom punch for cadence on")

    # Rule 6: B-roll over jump cuts first (it hides them), up to the profile's density.
    pool = [dict(s, clip_id=c["id"]) for c in index["clips"] if not c["words"]
            for s in c.get("shots", [])]
    pool.sort(key=lambda s: -s.get("score", 0.5))
    used: dict[str, float] = {}
    broll: list[dict] = []
    br = style["broll"]
    budget = br["density"] * duration
    length = (br["min_s"] + br["max_s"]) / 2
    for cut in _cuts(items, fps):
        left = budget - sum(x["end_s"] - x["start_s"] for x in broll)
        if left < br["min_s"] or not pool:
            break
        want = min(length, left)
        x = place_broll(layout, pool, used, cut - want / 2, want, words,
                        "B-roll over a jump cut", br["min_s"])
        if x:
            broll.append(x)

    # Rule 3: no stretch longer than max_static without a cut, zoom or B-roll.
    max_static = style["pacing"]["max_static_s"]
    for _ in range(500):
        events = sorted({0.0, duration, *_cuts(items, fps),
                         *[x for zm in zooms for x in (zm["start_s"], zm["end_s"])],
                         *[x for b in broll for x in (b["start_s"], b["end_s"])]})
        gap = next(((a, b) for a, b in zip(events, events[1:]) if b - a > max_static + 1e-6), None)
        if gap is None:
            break
        a, b = gap
        window = [w for w in words if a + 0.5 <= w["s"] <= a + max_static]
        rule = f"pattern interrupt: {b - a:.1f} s static"
        placed = None
        zooms_allowed = z["per_minute"] > 0  # a style that never zooms never zooms
        for w in [w for w in window if w["emph"]] if zooms_allowed else []:
            placed = place_zoom(layout, clips, w, z, z["scale_max"], rule)
            if placed:
                zooms.append(placed)
                break
        if not placed and pool:
            placed = place_broll(layout, pool, used, a + max_static - br["min_s"], br["min_s"],
                                 words, rule)
            if placed:
                broll.append(placed)
        if not placed and zooms_allowed:
            # No emphasis word, no B-roll left: punch on any word (no graphics templates yet).
            for w in reversed(window):
                placed = place_zoom(layout, clips, w, z, z["scale_min"], rule + ", no B-roll left")
                if placed:
                    zooms.append(placed)
                    break
        if not placed:
            layout.take(a, b)  # nothing fits; do not try this stretch again

    zooms.sort(key=lambda zm: zm["start_s"])
    broll.sort(key=lambda b: b["start_s"])

    # Rule 7: SFX on an evenly spread share of cuts and punches.
    sfx = []
    cuts = _cuts(items, fps) + [b["start_s"] for b in broll]
    for n, t in enumerate(sorted(cuts)):
        if (n * GOLDEN) % 1 < style["sfx"]["cut_share"]:
            sfx.append({"t": round(max(0.0, t - 0.17), 3), "kind": "whoosh", "rule": "SFX on cut"})
    for n, zm in enumerate(z_ for z_ in zooms if z_["kind"] == "punch"):
        if (n * GOLDEN) % 1 < style["sfx"]["zoom_share"]:
            sfx.append({"t": round(zm["start_s"] + zm["ease_s"], 3), "kind": "pop",
                        "rule": "SFX on zoom punch"})
    sfx.sort(key=lambda s: s["t"])

    cap_mode = brief["captions"]["mode"]
    cap = {"enabled": cap_mode != "off", "style": cap_mode if cap_mode != "off" else "clean",
           "max_words": style["captions"]["max_words"], "case": style["captions"]["case"],
           "highlight": style["captions"]["highlight"],
           "emphasis": style["captions"]["emphasis"]}

    n_cuts = len(_cuts(items, fps))
    report = {
        "duration_s": round(duration, 2), "items": len(items), "jump_cuts": n_cuts,
        "cuts_per_min": round((n_cuts + len(broll) * 2) / max(duration / 60, 1e-6), 1),
        "zooms": len(zooms), "zooms_per_min": round(len(zooms) / max(duration / 60, 1e-6), 1),
        "broll": len(broll),
        "broll_share": round(sum(b["end_s"] - b["start_s"] for b in broll)
                             / max(duration, 1e-6), 3),
        "sfx": len(sfx), "beat_snaps": snapped, "music": track["name"] if track else None,
        "max_static_s": max_static,
    }
    reframe = [subject_track(clips[it.clip_id], it.src_in, it.frames / fps) for it in items]
    comp = Composition(items, beats_out, zooms, broll, music_doc, sfx, cap,
                       style["colour"], report)
    comp.reframe = reframe
    return comp

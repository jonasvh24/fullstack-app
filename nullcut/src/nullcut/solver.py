"""Beat-aware duration solver (plan §6).

The planner decides what goes in and why; this decides how long each piece
runs by choosing which sentences survive. Hard: the target window, locked
beats, the hook cap, at least one sentence per non-empty beat. Soft: each
beat's share of the target from the style template. Story order is never
the solver's to change; it only keeps or drops.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class BeatIn:
    id: str
    role: str
    segment_ids: list[str]
    target_s: float | None = None
    locked: bool = False


@dataclass
class Solution:
    kept: dict[str, list[str]]          # beat id -> kept segment ids, play order
    dropped: dict[str, list[str]]
    kept_s: float
    status: str                          # met | short | over | no_target
    shortfall_s: float = 0.0
    overshoot_s: float = 0.0
    notes: list[str] = field(default_factory=list)


def solve(beats: list[BeatIn], seg_len: dict[str, float], scores: dict[str, float],
          target_s: float | None, tolerance: float = 0.05, hook_max_s: float | None = None,
          time_limit_s: float = 5.0) -> Solution:
    from ortools.sat.python import cp_model

    segs = [(b, s) for b in beats for s in b.segment_ids if s in seg_len]
    ms = {s: max(1, round(seg_len[s] * 1000)) for _, s in segs}
    pts = {s: round(scores.get(s, 0.5) * 1000) for _, s in segs}
    total_ms = sum(ms.values())
    notes: list[str] = []

    def build(window: bool, min_one: bool):
        m = cp_model.CpModel()
        x = {s: m.NewBoolVar(s) for _, s in segs}
        devs = []
        for b in beats:
            ids = [s for s in b.segment_ids if s in x]
            if not ids:
                continue
            length = sum(ms[s] * x[s] for s in ids)
            if b.locked:
                for s in ids:
                    m.Add(x[s] == 1)
            elif min_one:
                m.Add(sum(x[s] for s in ids) >= 1)
            if b.role == "hook" and hook_max_s and not b.locked:
                m.Add(length <= round(hook_max_s * 1000))
            if b.target_s:
                dev = m.NewIntVar(0, total_ms, f"dev_{b.id}")
                m.AddAbsEquality(dev, length - round(b.target_s * 1000))
                devs.append(dev)
        kept = sum(ms[s] * x[s] for s in x)
        score = sum(pts[s] * x[s] for s in x)
        # Score is scaled x100 so one second off a beat's share (1000 ms)
        # costs as much as 0.01 of highlight score.
        beat_penalty = sum(devs) if devs else 0
        if target_s is None:
            m.Maximize(score * 100 - beat_penalty)
        elif window:
            lo = round(target_s * (1 - tolerance) * 1000)
            hi = round(target_s * (1 + tolerance) * 1000)
            m.Add(kept >= lo)
            m.Add(kept <= hi)
            m.Maximize(score * 100 - beat_penalty)
        else:
            gap = m.NewIntVar(0, total_ms + round(target_s * 1000), "gap")
            m.AddAbsEquality(gap, kept - round(target_s * 1000))
            m.Minimize(gap * 1000 - score)
        solver = cp_model.CpSolver()
        solver.parameters.num_workers = 1   # same input, same cut
        solver.parameters.random_seed = 0
        solver.parameters.max_time_in_seconds = time_limit_s
        status = solver.Solve(m)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return None
        return {s for s in x if solver.Value(x[s])}

    chosen = None
    for window, min_one in ((True, True), (True, False), (False, True), (False, False)):
        if target_s is None and not window:
            break
        chosen = build(window, min_one)
        if chosen is not None:
            if not window:
                notes.append("target window unreachable: landed as near as the footage allows")
            if not min_one:
                notes.append("a beat lost all its sentences to fit the length")
            break
    if chosen is None:  # only when locked material alone breaks the hook cap
        chosen = {s for _, s in segs}
        notes.append("no feasible cut: kept everything")

    kept = {b.id: [s for s in b.segment_ids if s in chosen] for b in beats}
    dropped = {b.id: [s for s in b.segment_ids if s in seg_len and s not in chosen] for b in beats}
    kept_s = sum(seg_len[s] for s in chosen)
    sol = Solution(kept, dropped, round(kept_s, 3), "no_target", notes=notes)
    if target_s:
        lo, hi = target_s * (1 - tolerance), target_s * (1 + tolerance)
        sol.status = "met" if lo <= kept_s <= hi else ("short" if kept_s < lo else "over")
        sol.shortfall_s = round(max(0.0, lo - kept_s), 2)
        sol.overshoot_s = round(max(0.0, kept_s - hi), 2)
    return sol

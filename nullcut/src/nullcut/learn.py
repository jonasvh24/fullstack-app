"""Learned highlight weights (plan §4: "the highlight score learns").

Weights start hand-set (config.HIGHLIGHT_WEIGHTS). Every sentence the creator
deletes or restores, in chat refinement or on the storyboard, is a labelled
example stored under $NULLCUT_HOME, so it follows the creator across
projects. A nightly refit turns them into a logistic model over the same
rank-normalised features the hand blend uses.

Two guards keep a handful of clicks from wrecking the ranking. The fit is
pulled towards the hand weights (an L2 prior, so twenty examples nudge and
two hundred decide), and a refit is only adopted when it ranks held-out
choices better than the hand weights do (AUC, by a margin): the solver uses
the score only to rank, so calibration alone must not win.

Labels: an explicit delete is 0 and an explicit restore is 1. The sentences
the creator saw and left alone at that moment are weak labels the other way
(kept: 1, still dropped: 0) at a third of the weight; without them a creator
who only ever deletes would give the fit a single class.
"""

from __future__ import annotations

import json
import random
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from . import config
from .project import read_json, stable_hash, write_json

FEATURES = tuple(config.HIGHLIGHT_WEIGHTS)
PRIOR_SHARPNESS = 6.0     # logistic slope that reproduces the hand blend's ranking
PRIOR_STRENGTH = 4.0      # L2 pull towards the hand weights
IMPLICIT_WEIGHT = 1 / 3
MIN_EXAMPLES = 20
MIN_PER_CLASS = 5
AUC_MARGIN = 0.02         # a refit must rank held-out choices this much better
AUC_FLOOR = 0.55          # and better than chance by a visible amount
CONTEXT_PER_EVENT = 4     # weak labels recorded alongside each explicit one


def learning_dir() -> Path:
    return config.nullcut_home() / "learning"


def examples_path() -> Path:
    return learning_dir() / "examples.jsonl"


def model_path() -> Path:
    return learning_dir() / "weights.json"


# --- features ------------------------------------------------------------------------

def ranks(index: dict) -> dict[str, list[float]]:
    """Per sentence, each feature's rank across the project, 0..1 (as the hand blend)."""
    segs = [s for c in index["clips"] for s in c["segments"]]
    if not segs:
        return {}
    cols = []
    for f in FEATURES:
        v = np.array([s.get("features", {}).get(f, 0.0) for s in segs], dtype=float)
        cols.append(np.full(len(v), 0.5) if len(v) == 1 else v.argsort().argsort() / (len(v) - 1))
    mat = np.stack(cols, axis=1)
    return {s["id"]: [round(float(x), 4) for x in row] for s, row in zip(segs, mat)}


def prior() -> tuple[float, np.ndarray]:
    w = np.array([config.HIGHLIGHT_WEIGHTS[f] for f in FEATURES], dtype=float)
    c = PRIOR_SHARPNESS * w / w.sum()
    return -PRIOR_SHARPNESS / 2, c


# --- examples -------------------------------------------------------------------------

def record(project_id: str, index: dict, explicit: list[tuple[str, int]],
           context: list[tuple[str, int]], source: str) -> int:
    """Append labelled sentences; returns how many were written."""
    feats = ranks(index)
    rng = random.Random(stable_hash([project_id, explicit]))
    context = [c for c in context if c[0] not in {s for s, _ in explicit}]
    rng.shuffle(context)
    rows = []
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for (sid, label), implicit in ([(e, False) for e in explicit]
                                   + [(c, True) for c in context[:CONTEXT_PER_EVENT
                                                                  * max(1, len(explicit))]]):
        if sid in feats:
            rows.append({"at": now, "project": project_id, "segment": sid, "label": int(label),
                         "implicit": implicit, "source": source, "x": feats[sid]})
    if rows:
        examples_path().parent.mkdir(parents=True, exist_ok=True)
        with examples_path().open("a") as f:
            f.writelines(json.dumps(r) + "\n" for r in rows)
    return len(rows)


def from_plans(project_id: str, index: dict, before: list[dict], after: list[dict],
               source: str) -> int:
    """Deletes and restores between two versions of a plan's beats become examples.
    Sentences the solver dropped are not the creator's choice and teach nothing."""
    kept_b = {s for b in before for s in b["segment_ids"]}
    kept_a = {s for b in after for s in b["segment_ids"]}
    all_a = kept_a | {s for b in after for s in b.get("dropped_ids", [])}
    removed = sorted(kept_b - all_a)
    restored = sorted(kept_a - kept_b)
    if not removed and not restored:
        return 0
    explicit = [(s, 0) for s in removed] + [(s, 1) for s in restored]
    context = [(s, 1) for s in sorted(kept_a - set(restored))] if removed else []
    if restored:
        context += [(s, 0) for b in after for s in b.get("dropped_ids", [])]
    return record(project_id, index, explicit, context, source)


def load_examples() -> list[dict]:
    path = examples_path()
    if not path.exists():
        return []
    latest: dict[tuple, dict] = {}
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue  # a torn last line from a crash costs one example, not the file
        key = (row["project"], row["segment"])
        prev = latest.get(key)
        # The creator's latest explicit word on a sentence wins over any weak label.
        if prev is None or not row["implicit"] or prev["implicit"]:
            latest[key] = row
    return list(latest.values())


# --- fitting --------------------------------------------------------------------------

def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1 / (1 + np.exp(-np.clip(z, -30, 30)))


def fit(x: np.ndarray, y: np.ndarray, w: np.ndarray, strength: float = PRIOR_STRENGTH
        ) -> tuple[float, np.ndarray]:
    """Weighted logistic regression by Newton steps, L2-pulled towards the prior."""
    b0, c0 = prior()
    theta0 = np.concatenate([[b0], c0])
    theta = theta0.copy()
    xa = np.hstack([np.ones((len(x), 1)), x])
    reg = np.full(len(theta), strength)
    reg[0] = strength * 0.1  # the bias moves freely: the keep rate differs per creator
    for _ in range(50):
        p = _sigmoid(xa @ theta)
        grad = xa.T @ (w * (p - y)) + reg * (theta - theta0)
        hess = (xa * (w * p * (1 - p))[:, None]).T @ xa + np.diag(reg)
        step = np.linalg.solve(hess, grad)
        theta -= step
        if np.abs(step).max() < 1e-7:
            break
    return float(theta[0]), theta[1:]


def _logloss(theta: tuple[float, np.ndarray], x, y, w) -> tuple[float, float]:
    p = _sigmoid(theta[0] + x @ theta[1])
    eps = 1e-9
    loss = -(w * (y * np.log(p + eps) + (1 - y) * np.log(1 - p + eps))).sum() / w.sum()
    acc = float((w * ((p >= 0.5) == (y == 1))).sum() / w.sum())
    return float(loss), acc


def _auc(score: np.ndarray, y: np.ndarray) -> float:
    """Probability a kept sentence outranks a deleted one (ties count half)."""
    pos, neg = score[y == 1], score[y == 0]
    if not len(pos) or not len(neg):
        return 0.5
    diff = pos[:, None] - neg[None, :]
    return float(((diff > 0).sum() + 0.5 * (diff == 0).sum()) / diff.size)


def refit(min_examples: int = MIN_EXAMPLES, folds: int = 5) -> dict:
    """Fit, cross-validate against the hand weights, adopt only if no worse."""
    rows = load_examples()
    explicit = [r for r in rows if not r["implicit"]]
    pos = sum(r["label"] for r in rows)
    status = {"fitted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "examples": len(rows), "explicit": len(explicit), "kept": pos,
              "deleted": len(rows) - pos, "adopted": False}
    if len(explicit) < min_examples or min(pos, len(rows) - pos) < MIN_PER_CLASS:
        status["reason"] = (f"needs {min_examples} explicit choices and {MIN_PER_CLASS} of each "
                            f"kind; have {len(explicit)} ({pos} kept, {len(rows) - pos} deleted)")
        _save_status(status)
        return status
    x = np.array([r["x"] for r in rows], dtype=float)
    y = np.array([r["label"] for r in rows], dtype=float)
    w = np.array([IMPLICIT_WEIGHT if r["implicit"] else 1.0 for r in rows])
    # Folds by project: a model that only memorised one edit must not look good.
    projects = sorted({r["project"] for r in rows})
    groups = np.array([projects.index(r["project"]) for r in rows])
    if len(projects) < 3:
        order = np.random.default_rng(0).permutation(len(rows))
        groups = np.empty(len(rows), dtype=int)
        groups[order] = np.arange(len(rows)) % folds
    learned_l, prior_l, learned_a, prior_a, n_held = 0.0, 0.0, 0.0, 0.0, 0.0
    held_learned = np.zeros(len(rows))
    held_prior = np.zeros(len(rows))
    b0, c0 = prior()
    for g in np.unique(groups):
        test = groups == g
        if test.all() or not test.any():
            continue
        theta = fit(x[~test], y[~test], w[~test])
        held_learned[test] = theta[0] + x[test] @ theta[1]
        held_prior[test] = b0 + x[test] @ c0
        ll, la = _logloss(theta, x[test], y[test], w[test])
        pl, pa = _logloss((b0, c0), x[test], y[test], w[test])
        share = w[test].sum()
        learned_l += ll * share
        prior_l += pl * share
        learned_a += la * share
        prior_a += pa * share
        n_held += share
    b, c = fit(x, y, w)
    status.update({
        "bias": round(b, 4), "coef": {f: round(float(v), 4) for f, v in zip(FEATURES, c)},
        "cv": {"learned_logloss": round(learned_l / n_held, 4),
               "hand_logloss": round(prior_l / n_held, 4),
               "learned_accuracy": round(learned_a / n_held, 3),
               "hand_accuracy": round(prior_a / n_held, 3),
               "learned_auc": round(_auc(held_learned, y), 3),
               "hand_auc": round(_auc(held_prior, y), 3)}})
    cv = status["cv"]
    status["adopted"] = bool(cv["learned_auc"] >= cv["hand_auc"] + AUC_MARGIN
                             and cv["learned_auc"] >= AUC_FLOOR)
    if not status["adopted"]:
        status["reason"] = (f"held-out choices ranked no better than the hand weights "
                            f"(AUC {cv['learned_auc']:.2f} against {cv['hand_auc']:.2f})")
    status["id"] = stable_hash({"b": status["bias"], "c": status["coef"]})[:12]
    _save_status(status)
    return status


def _save_status(status: dict) -> None:
    write_json(model_path(), status)


def current() -> dict | None:
    """The adopted model, if any."""
    path = model_path()
    if not path.exists():
        return None
    m = read_json(path)
    return m if m.get("adopted") else None


def apply(index: dict) -> dict:
    """Rescore every sentence with the adopted model, in place. Analysis stays untouched:
    the hand score lives on as score_hand, and a refit applies to every project at once."""
    model = current()
    if model is None:
        return index
    feats = ranks(index)
    c = np.array([model["coef"][f] for f in FEATURES])
    for clip in index["clips"]:
        for s in clip["segments"]:
            if s["id"] in feats:
                s.setdefault("score_hand", s.get("score", 0.5))
                s["score"] = round(float(_sigmoid(model["bias"] + np.array(feats[s["id"]]) @ c)),
                                   3)
    index["scored_with"] = model["id"]
    return index


def status() -> dict:
    rows = load_examples()
    explicit = [r for r in rows if not r["implicit"]]
    path = model_path()
    last = read_json(path) if path.exists() else None
    stale = bool(last) and examples_path().exists() and \
        examples_path().stat().st_mtime > path.stat().st_mtime
    hand = {f: config.HIGHLIGHT_WEIGHTS[f] for f in FEATURES}
    return {"examples": len(rows), "explicit": len(explicit),
            "kept": sum(r["label"] for r in rows),
            "deleted": sum(1 - r["label"] for r in rows),
            "projects": len({r["project"] for r in rows}),
            "min_examples": MIN_EXAMPLES, "hand_weights": hand, "last_fit": last,
            "active": "learned" if last and last.get("adopted") else "hand",
            "new_since_fit": stale or (last is None and bool(rows)),
            "next_refit": next_refit().isoformat(timespec="minutes") if refit_hour() is not None
            else None}


# --- nightly ------------------------------------------------------------------------

def refit_hour() -> int | None:
    import os
    raw = os.environ.get("NULLCUT_REFIT_HOUR", "3")
    return None if raw.lower() in ("off", "none", "") else int(raw) % 24


def next_refit(now: datetime | None = None) -> datetime:
    now = now or datetime.now().astimezone()
    at = now.replace(hour=refit_hour() or 0, minute=0, second=0, microsecond=0)
    return at if at > now else at + timedelta(days=1)


def start_nightly(stop: threading.Event, log=print) -> threading.Thread | None:
    """Refit once a night when there are new examples (plan §4: "refitted nightly")."""
    if refit_hour() is None:
        return None

    def loop() -> None:
        while not stop.wait(max(1.0, (next_refit() - datetime.now().astimezone())
                                .total_seconds())):
            try:
                if status()["new_since_fit"]:
                    s = refit()
                    log(f"nightly refit: {'adopted' if s['adopted'] else 'kept hand weights'} "
                        f"({s['examples']} examples)")
            except Exception as exc:  # a bad night must not kill the server's thread
                log(f"nightly refit failed: {exc}")

    t = threading.Thread(target=loop, name="nightly-refit", daemon=True)
    t.start()
    return t

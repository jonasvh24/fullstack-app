"""SFX pack and colour LUTs, both generated: licence-free by construction.

The plan calls for a curated SFX pack (§8). Until one is chosen, a whoosh
and a pop are synthesised here; swapping in recorded files changes
`sfx_sample` only.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from . import config


def sfx_sample(kind: str) -> np.ndarray:
    """Mono float32 at the mix rate, peak about -6 dBFS."""
    sr = config.MIX_SAMPLE_RATE
    rng = np.random.default_rng(11 if kind == "whoosh" else 12)
    if kind == "whoosh":
        n = int(0.35 * sr)
        t = np.arange(n) / sr
        noise = rng.standard_normal(n).astype(np.float32)
        # Band-limited noise swept upwards by a moving one-pole low-pass.
        cutoff = 300 + 5000 * (t / t[-1]) ** 2
        alpha = 1 - np.exp(-2 * np.pi * cutoff / sr)
        out = np.zeros(n, dtype=np.float32)
        acc = 0.0
        for i in range(n):
            acc += alpha[i] * (noise[i] - acc)
            out[i] = acc
        env = np.sin(np.pi * np.clip(t / t[-1], 0, 1)) ** 2
        out *= env
    elif kind == "pop":
        n = int(0.09 * sr)
        t = np.arange(n) / sr
        freq = 900 * np.exp(-t * 25) + 300
        out = np.sin(2 * np.pi * np.cumsum(freq) / sr).astype(np.float32) * np.exp(-t * 45)
    else:
        raise ValueError(kind)
    return (out / max(1e-6, np.abs(out).max()) * 0.5).astype(np.float32)


def grade(rgb: np.ndarray, colour: dict) -> np.ndarray:
    """The style grade on RGB in 0..1. The LUT is this function sampled on a grid, and the
    match score applies it to source frames to know exactly what the output should be."""
    sat = float(colour.get("saturation", 1.0))
    con = float(colour.get("contrast", 1.0))
    warm = float(colour.get("warmth", 0.0))
    lift = float(colour.get("lift", 0.0))
    luma = (rgb * [0.2126, 0.7152, 0.0722]).sum(-1, keepdims=True)
    out = luma + (rgb - luma) * sat
    out = (out - 0.5) * con + 0.5
    out = out + np.array([warm, warm * 0.3, -warm])  # warm: up red, down blue
    out = lift + out * (1 - lift)
    return np.clip(out, 0, 1)


def write_cube(path: Path, colour: dict, size: int = 33) -> Path:
    """A 3D LUT from the style's colour parameters; FFmpeg applies it with lut3d."""
    if path.exists():
        return path
    g = np.linspace(0, 1, size, dtype=np.float64)
    # .cube order: red varies fastest, then green, then blue.
    b, gg, r = np.meshgrid(g, g, g, indexing="ij")
    rgb = grade(np.stack([r, gg, b], axis=-1), colour)
    rgb = np.clip(rgb, 0, 1).reshape(-1, 3)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"LUT_3D_SIZE {size}", "DOMAIN_MIN 0 0 0", "DOMAIN_MAX 1 1 1"]
    lines += [f"{x:.6f} {y:.6f} {z:.6f}" for x, y, z in rgb]
    path.write_text("\n".join(lines) + "\n")
    return path


def affine(colour: dict) -> tuple[np.ndarray, np.ndarray]:
    """grade() is affine in RGB before its final clip: out = clip(M @ rgb + b)."""
    sat = float(colour.get("saturation", 1.0))
    con = float(colour.get("contrast", 1.0))
    warm = float(colour.get("warmth", 0.0))
    lift = float(colour.get("lift", 0.0))
    w = np.array([0.2126, 0.7152, 0.0722])
    a1 = sat * np.eye(3) + (1 - sat) * np.tile(w, (3, 1))
    m = (1 - lift) * con * a1
    b = (1 - lift) * (0.5 * (1 - con) + np.array([warm, warm * 0.3, -warm])) + lift
    return m, b


def preview_grade_filter(colour: dict) -> str:
    """The grade as a channel mixer and a per-channel offset: about 2.5x cheaper than a
    tetrahedral 3D LUT. Identical except where the mixer already clips, where it differs
    by at most the offset. Previews only; masters keep the LUT."""
    m, b = affine(colour)
    mix = ":".join(f"{ch}{src}={m[i, j]:.5f}" for i, ch in enumerate("rgb")
                   for j, src in enumerate("rgb"))
    off = ":".join(f"{ch}='clip(val+{b[i] * 255:.3f},0,255)'" for i, ch in enumerate("rgb"))
    return f"format=rgb24,colorchannelmixer={mix},lutrgb={off}"


def nudge(colour: dict, change: dict | None) -> dict:
    """A refinement's colour tweak on top of the fitted grade: ratios for saturation and
    contrast, offsets for warmth and lift, so "warmer" means the same on any look."""
    if not change:
        return colour
    base = {"saturation": 1.0, "contrast": 1.0, "warmth": 0.0, "lift": 0.0,
            **{k: v for k, v in (colour or {}).items() if k != "target"}}
    return {"saturation": round(base["saturation"] * change.get("saturation", 1.0), 4),
            "contrast": round(base["contrast"] * change.get("contrast", 1.0), 4),
            "warmth": round(base["warmth"] + change.get("warmth", 0.0), 4),
            "lift": round(base["lift"] + change.get("lift", 0.0), 4)}


def is_identity(colour: dict) -> bool:
    return (abs(colour.get("saturation", 1) - 1) < 1e-3
            and abs(colour.get("contrast", 1) - 1) < 1e-3
            and abs(colour.get("warmth", 0)) < 1e-3 and abs(colour.get("lift", 0)) < 1e-3)


def colour_transfer(target: dict, source: dict) -> dict:
    """LUT parameters that move footage with `source` statistics towards `target`.

    A first-order fit (plan §5: "fitted colour transfer"): ratios for
    saturation and contrast, offsets for warmth and lift, bounded so a
    strange reference cannot push the grade into caricature.
    """
    sat = target["saturation"] / max(source["saturation"], 1e-3)
    con = target["contrast"] / max(source["contrast"], 1e-3)
    # Bounded: a reference far from this footage gets a firm grade, never a caricature.
    return {"saturation": round(float(np.clip(sat, 0.7, 1.35)), 3),
            "contrast": round(float(np.clip(con, 0.85, 1.25)), 3),
            # grade() moves R-B by twice the warmth parameter.
            "warmth": round(float(np.clip((target["warmth"] - source["warmth"]) / 2,
                                          -0.08, 0.08)), 3),
            "lift": round(float(np.clip((target["lift"] - source["lift"])
                                        / max(1 - source["lift"], 0.5), -0.03, 0.05)), 3)}


def _keyframes(project, index: dict) -> list:
    import cv2

    frames = []
    for c in index["clips"]:
        kfs = list((c.get("keyframes") or {}).values())
        for rel in kfs[:: max(1, len(kfs) // 8)][:8]:
            img = cv2.imread(str(project.path(rel)))
            if img is not None:
                frames.append(img)
    return frames


def footage_colour(project, index: dict) -> dict | None:
    """Colour statistics of the project's footage, from its keyframes (cached)."""
    import json

    from . import measure

    cache = project.path("index", "colour.json")
    if cache.exists():
        return json.loads(cache.read_text())
    frames = _keyframes(project, index)
    if not frames:
        return None
    stats = measure.colour_stats(frames)
    cache.write_text(json.dumps(stats))
    return stats


def graded_colour(project, index: dict, colour: dict) -> dict | None:
    """What the output's colour statistics should be: the same keyframes, graded by the
    same function, measured by the same code."""
    from . import measure

    frames = _keyframes(project, index)
    if not frames:
        return None
    graded = [(grade(f[..., ::-1].astype(np.float64) / 255, colour)[..., ::-1] * 255)
              .astype(np.uint8) for f in frames]
    return measure.colour_stats(graded)

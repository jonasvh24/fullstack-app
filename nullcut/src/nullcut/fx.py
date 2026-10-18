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


def write_cube(path: Path, colour: dict, size: int = 33) -> Path:
    """A 3D LUT from the style's colour parameters; FFmpeg applies it with lut3d."""
    if path.exists():
        return path
    sat = float(colour.get("saturation", 1.0))
    con = float(colour.get("contrast", 1.0))
    warm = float(colour.get("warmth", 0.0))
    lift = float(colour.get("lift", 0.0))
    g = np.linspace(0, 1, size, dtype=np.float64)
    # .cube order: red varies fastest, then green, then blue.
    b, gg, r = np.meshgrid(g, g, g, indexing="ij")
    rgb = np.stack([r, gg, b], axis=-1)
    luma = (rgb * [0.2126, 0.7152, 0.0722]).sum(-1, keepdims=True)
    rgb = luma + (rgb - luma) * sat
    rgb = (rgb - 0.5) * con + 0.5
    rgb = rgb + np.array([warm, warm * 0.3, -warm])  # warm: up red, down blue
    rgb = lift + rgb * (1 - lift)
    rgb = np.clip(rgb, 0, 1).reshape(-1, 3)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"LUT_3D_SIZE {size}", "DOMAIN_MIN 0 0 0", "DOMAIN_MAX 1 1 1"]
    lines += [f"{x:.6f} {y:.6f} {z:.6f}" for x, y, z in rgb]
    path.write_text("\n".join(lines) + "\n")
    return path


def is_identity(colour: dict) -> bool:
    return (abs(colour.get("saturation", 1) - 1) < 1e-3
            and abs(colour.get("contrast", 1) - 1) < 1e-3
            and abs(colour.get("warmth", 0)) < 1e-3 and abs(colour.get("lift", 0)) < 1e-3)

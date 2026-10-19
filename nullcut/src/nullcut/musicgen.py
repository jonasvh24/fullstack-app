"""Music generation (plan §8): a bed written to the edit's exact length, tempo and sections.

Two backends.

  builtin   A small synthesiser in numpy: pads, bass, drums and a pluck arpeggio over a
            four-bar progression, following the plan's energy per beat (low under
            explanation, high on the hook and payoff). No model, no download, the same
            output for the same edit, and CC0: nothing in it belongs to anyone else.
            Its beat grid is known before a sample exists, so cuts snap to it exactly
            and the final bar ends on the last frame by construction.

  command   Any local generator, ACE-Step 1.5 the intended one, through a command
            template in NULLCUT_MUSICGEN_CMD with {prompt} {seconds} {bpm} {seed} {out}.
            Its output is analysed and fitted like a library track. The creator declares
            the licence in NULLCUT_MUSICGEN_LICENCE; it is recorded and credited.
"""

from __future__ import annotations

import json
import math
import os
import shlex
import subprocess
import wave
from pathlib import Path

import numpy as np

from . import config
from .project import stable_hash

SR = config.MIX_SAMPLE_RATE
BUILTIN_LICENCE = ("Generated locally by nullcut's built-in synthesiser; no samples or "
                   "third-party models. CC0-1.0")
# Tempo and mode per mood; energy nudges the tempo.
MOODS = {
    "": (100, "major"), "happy": (112, "major"), "uplifting": (110, "major"),
    "corporate": (104, "major"), "acoustic": (96, "major"), "energetic": (122, "major"),
    "electronic": (124, "minor"), "epic": (92, "minor"), "cinematic": (84, "minor"),
    "dark": (88, "minor"), "lofi": (80, "minor"), "chill": (88, "major"), "ambient": (72, "major"),
    "piano": (84, "major"), "jazzy": (96, "major"), "jazz": (96, "major"), "funky": (108, "minor"),
    "orchestral": (90, "minor"), "upbeat": (118, "major"),
}
PROGRESSIONS = {"major": [(0, "maj"), (7, "maj"), (9, "min"), (5, "maj")],
                "minor": [(0, "min"), (8, "maj"), (3, "maj"), (10, "maj")]}
ENERGY_LEVEL = {"low": 0, "mid": 1, "high": 2}


def generated_dir() -> Path:
    return config.nullcut_home() / "music" / "generated"


def backend() -> str:
    return "command" if os.environ.get("NULLCUT_MUSICGEN_CMD") else "builtin"


def spec(brief_music: dict, style: dict, seed_from: object) -> dict:
    mood = next((m for m in MOODS if m and m in (brief_music.get("mood") or "").lower()), "")
    bpm, mode = MOODS[mood]
    energy = brief_music.get("energy") or style["music"].get("energy", "mid")
    if not mood:
        bpm = {"low": 88, "mid": 100, "high": 118}[energy]
    seed = int(stable_hash([seed_from, mood, energy])[:8], 16)
    return {"backend": backend(), "mood": mood, "energy": energy, "bpm": float(bpm),
            "mode": mode, "key": seed % 12, "seed": seed}


# --- builtin: grid first, audio after ---------------------------------------------------

def grid_track(sp: dict, duration: float) -> dict:
    """A library-shaped track whose beat grid is fixed to the END of the edit."""
    spb = 60.0 / sp["bpm"]
    n = int(duration / spb)
    beats = sorted(round(duration - k * spb, 4) for k in range(n + 1) if duration - k * spb >= 0)
    downbeats = [b for b in beats if round((duration - b) / spb) % 4 == 0]
    return {"id": None, "name": _name(sp), "duration": round(duration, 4), "bpm": sp["bpm"],
            "beats": beats, "downbeats": downbeats, "energy": 0.5, "tags": [],
            "licence": BUILTIN_LICENCE}


def _name(sp: dict) -> str:
    return f"Generated {sp['mood'] or sp['energy'] + ' energy'} bed, {sp['bpm']:g} bpm"


def _midi(n: float) -> float:
    return 440.0 * 2 ** ((n - 69) / 12)


def _chord(root: int, quality: str, base: int) -> list[float]:
    third = 3 if quality == "min" else 4
    return [_midi(base + root + i) for i in (0, third, 7)]


def _env(n: int, attack: int, release: int) -> np.ndarray:
    e = np.ones(n, dtype=np.float32)
    a = min(attack, n)
    r = min(release, n - a)
    if a:
        e[:a] = np.linspace(0, 1, a, dtype=np.float32)
    if r:
        e[n - r:] *= np.linspace(1, 0, r, dtype=np.float32)
    return e


def _tone(freq: float, n: int, harmonics=(1.0, 0.35, 0.15), detune: float = 0.0) -> np.ndarray:
    t = np.arange(n, dtype=np.float32) / SR
    out = np.zeros(n, dtype=np.float32)
    for h, amp in enumerate(harmonics, 1):
        out += amp * np.sin(2 * np.pi * freq * h * (1 + detune) * t, dtype=np.float32)
    return out


def _kick() -> np.ndarray:
    n = int(0.35 * SR)
    t = np.arange(n) / SR
    f = 48 + 110 * np.exp(-t * 28)
    phase = 2 * np.pi * np.cumsum(f) / SR
    return (np.sin(phase) * np.exp(-t * 9)).astype(np.float32)


def _noise(n: int, rng: np.random.Generator) -> np.ndarray:
    return rng.standard_normal(n).astype(np.float32)


def _snare(rng) -> np.ndarray:
    n = int(0.2 * SR)
    t = np.arange(n) / SR
    noise = np.diff(_noise(n + 1, rng)) * np.exp(-t * 22) * 0.5
    body = np.sin(2 * np.pi * 185 * t) * np.exp(-t * 30) * 0.4
    return (noise + body).astype(np.float32)


def _hat(rng) -> np.ndarray:
    n = int(0.045 * SR)
    t = np.arange(n) / SR
    x = np.diff(np.diff(_noise(n + 2, rng)))
    return (x * np.exp(-t * 90) * 0.25).astype(np.float32)


def _crash(rng) -> np.ndarray:
    n = int(1.6 * SR)
    t = np.arange(n) / SR
    return (np.diff(_noise(n + 1, rng)) * np.exp(-t * 2.6) * 0.18).astype(np.float32)


def _add(buf: np.ndarray, at: float, sample: np.ndarray, gain: float, pan: float = 0.0) -> None:
    i = int(round(at * SR))
    if i >= len(buf) or i + len(sample) <= 0:
        return
    s = sample[max(0, -i):]
    i = max(0, i)
    n = min(len(s), len(buf) - i)
    left, right = gain * (1 - pan) * 0.5 ** 0.5, gain * (1 + pan) * 0.5 ** 0.5
    buf[i:i + n, 0] += s[:n] * left
    buf[i:i + n, 1] += s[:n] * right


def synthesise(sp: dict, duration: float, sections: list[list]) -> np.ndarray:
    """Stereo float32 of exactly `duration`, last bar ending on the last sample."""
    rng = np.random.default_rng(sp["seed"])
    spb = 60.0 / sp["bpm"]
    bar = 4 * spb
    n_bars = int(math.ceil(duration / bar)) + 1
    t0 = duration - n_bars * bar           # grid from the end: may start before zero
    offset = -t0
    buf = np.zeros((int(round(n_bars * bar * SR)) + SR, 2), dtype=np.float32)
    prog = PROGRESSIONS[sp["mode"]]
    lofi = sp["mood"] in ("lofi", "chill")
    sparse = sp["mood"] in ("ambient", "cinematic", "piano", "orchestral")
    swing = 0.58 if lofi else 0.5
    kick, snare, hat, crash = _kick(), _snare(rng), _hat(rng), _crash(rng)
    pads: dict[tuple, np.ndarray] = {}
    bars_n = int(round(bar * SR))

    def level_at(t: float) -> int:
        for a, b, e in sections:
            if a <= t < b:
                return ENERGY_LEVEL.get(e, 1)
        return ENERGY_LEVEL.get(sp["energy"], 1)

    prev_level = None
    for k in range(n_bars):
        start = t0 + k * bar
        final = k == n_bars - 1
        root, quality = prog[0] if final else prog[k % 4]
        level = level_at(max(0.0, start + bar / 2))
        if sparse:
            level = max(0, level - 1)
        at = start + offset
        # Pad: the chord held across the bar, slow attack, a little overlap into the next.
        key = (root, quality)
        if key not in pads:
            n = bars_n + int(0.25 * SR)
            freqs = _chord(root + sp["key"], quality, 57)
            left = sum(_tone(f, n, detune=-0.003) for f in freqs)
            right = sum(_tone(f, n, detune=0.003) for f in freqs)
            env = _env(n, int(0.35 * SR), int(0.3 * SR))
            pads[key] = np.stack([left * env, right * env], axis=1) / 3
        pad = pads[key] * (0.12, 0.18, 0.22)[level]
        if final:
            pad = pad * np.linspace(1, 0, len(pad), dtype=np.float32)[:, None] ** 1.5
        i = int(round(at * SR))
        buf[i:i + len(pad)] += pad[: len(buf) - i]
        # Bass: whole notes when calm, halves in the middle, driving eighths when high.
        bass_f = _midi(33 + root + sp["key"])
        steps = {0: 1, 1: 2, 2: 8}[level] if not final else 1
        step = bar / steps
        n = int(step * SR * 0.92)
        note = np.tanh(1.6 * _tone(bass_f, n, (1.0, 0.3))) * _env(n, int(0.005 * SR),
                                                                  int(0.06 * SR))
        for s in range(steps):
            _add(buf, at + s * step, note * (0.8 if s % 2 else 1.0), (0.18, 0.28, 0.34)[level])
        if final:
            continue
        if level == 2 and prev_level is not None and prev_level < 2:
            _add(buf, at, crash, 0.9, 0.2)
        # Drums.
        if level >= 1:
            kicks = range(4) if level == 2 else (0, 2)
            for b in kicks:
                _add(buf, at + b * spb, kick, 0.75)
            for b in (1, 3):
                _add(buf, at + b * spb, snare, 0.45 if level == 2 else 0.3, -0.1)
            eighths = range(8) if level == 2 else (1, 3, 5, 7)
            for e in eighths:
                pos = (e // 2) * spb + (swing * spb if e % 2 else 0.0)
                _add(buf, at + pos, hat, 0.5 if e % 2 else 0.35, 0.3)
        # Pluck arpeggio on the high sections.
        if level == 2:
            tones = _chord(root + sp["key"], quality, 69)
            n = int(0.3 * SR)
            pluck_env = np.exp(-np.arange(n) / SR * 9).astype(np.float32)
            for e in range(8):
                f = tones[(e + k) % 3] * (2 if e in (3, 7) else 1)
                _add(buf, at + e * spb / 2, _tone(f, n, (1.0, 0.4)) * pluck_env, 0.1,
                     0.35 if e % 2 else -0.35)
        prev_level = level

    out = buf[int(round(offset * SR)): int(round(offset * SR)) + int(round(duration * SR))]
    if lofi:
        from scipy.signal import butter, lfilter
        b, a = butter(2, 3200 / (SR / 2))
        out = lfilter(b, a, out, axis=0).astype(np.float32)
    fade = min(len(out), int(0.5 * SR))
    out[:fade] *= np.linspace(0, 1, fade, dtype=np.float32)[:, None]
    peak = float(np.abs(out).max()) or 1.0
    return (out * (10 ** (-1 / 20) / peak)).astype(np.float32)


def write_wav(audio: np.ndarray, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}")
    pcm = np.clip(audio * 32767, -32768, 32767).astype("<i2")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    tmp.replace(path)


def builtin_track(sp: dict, duration: float, sections: list[list]) -> dict:
    """The bed for this exact edit, generated once and cached by everything that shapes it."""
    full = {**sp, "duration": round(duration, 4),
            "sections": [[round(a, 3), round(b, 3), e] for a, b, e in sections]}
    key = stable_hash(full)[:16]
    path = generated_dir() / f"gen_{key}.wav"
    if not path.exists():
        write_wav(synthesise(sp, duration, full["sections"]), path)
        (path.with_suffix(".json")).write_text(json.dumps(full, indent=1))
    return {**grid_track(sp, duration), "id": f"gen_{key}", "path": str(path)}


# --- command backend -----------------------------------------------------------------------

def prompt(sp: dict) -> str:
    return (f"{sp['mood'] or 'modern'} instrumental background music for a video, "
            f"{sp['energy']} energy, {sp['bpm']:g} bpm, no vocals, steady groove")


def command_track(sp: dict, seconds: float, log=print) -> dict:
    """Run the configured generator for a bed a little longer than the edit, then analyse
    it like a library track so it can be fitted to end on its final downbeat."""
    from . import music

    template = os.environ["NULLCUT_MUSICGEN_CMD"]
    want = math.ceil(seconds + 8)
    key = stable_hash({**sp, "seconds": want, "cmd": template})[:16]
    path = generated_dir() / f"gen_{key}.wav"
    meta_path = path.with_suffix(".json")
    if meta_path.exists() and path.exists():
        return json.loads(meta_path.read_text())
    path.parent.mkdir(parents=True, exist_ok=True)
    args = [a.format(prompt=prompt(sp), seconds=want, bpm=f"{sp['bpm']:g}", seed=sp["seed"],
                     out=str(path)) for a in shlex.split(template)]
    log(f"music generation: {args[0]} ({want} s)")
    proc = subprocess.run(args, capture_output=True, text=True,
                          timeout=int(os.environ.get("NULLCUT_MUSICGEN_TIMEOUT", "1800")))
    if proc.returncode != 0 or not path.exists():
        raise RuntimeError(f"music generator failed ({proc.returncode}): "
                           f"{(proc.stderr or proc.stdout)[-400:]}")
    track = music.analyse_track(path, path.parent)
    track.update({"name": _name(sp), "licence": os.environ.get(
        "NULLCUT_MUSICGEN_LICENCE", "external generator, licence not declared")})
    meta_path.write_text(json.dumps(track))
    return track

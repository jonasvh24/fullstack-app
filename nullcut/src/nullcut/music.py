"""Music library and fitting (plan §8).

Every track must be cleared for the creator's channel: the library holds
files the user owns or licenses, with the licence recorded per track and
credited in the description. Nothing is fetched from the web.

Fitting follows the edit, never the reverse: the bed is cut on bar
boundaries so the final downbeat lands on the last frame, looped on bars
with short crossfades when the edit outruns the track.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Callable

import numpy as np

from . import config, media
from .project import read_json, write_json

AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus"}
ANALYSE_SR = 22_050
ENERGY_GAIN_DB = {"high": 0.0, "mid": -3.0, "low": -6.0}
DUCK_ATTACK_S = 0.15
DUCK_RELEASE_S = 0.45

Log = Callable[[str], None]


def library_dir() -> Path:
    return config.nullcut_home() / "music"


def library_path() -> Path:
    return library_dir() / "library.json"


def load_library() -> list[dict]:
    path = library_path()
    return read_json(path)["tracks"] if path.exists() else []


def _licence_for(path: Path) -> str:
    for candidate in (path.with_suffix(".licence.txt"), path.with_suffix(".license.txt"),
                      path.parent / "LICENCE.txt", path.parent / "LICENSE.txt",
                      path.parent / "LICENCE", path.parent / "LICENSE"):
        if candidate.exists():
            return candidate.read_text().strip().splitlines()[0][:200]
    return "user-supplied, licence not recorded"


def _tags(path: Path, root: Path) -> list[str]:
    parts = list(path.relative_to(root).parts[:-1]) + [path.stem]
    words = re.findall(r"[a-zA-Z]+", " ".join(parts).lower())
    return sorted({w for w in words if len(w) > 2})


def decode_pcm(path: Path, out: Path, rate: int = config.MIX_SAMPLE_RATE,
               channels: int = config.MIX_CHANNELS) -> Path:
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(f".{out.name}")
        media.ffmpeg("-i", str(path), "-vn", "-ac", str(channels), "-ar", str(rate),
                     "-f", "s16le", "-c:a", "pcm_s16le", str(tmp))
        tmp.replace(out)
    return out


def analyse_track(path: Path, root: Path) -> dict:
    import librosa

    track_id = "m_" + hashlib.blake2b(str(path.resolve()).encode(), digest_size=6).hexdigest()
    pcm = decode_pcm(path, library_dir() / "pcm" / f"{track_id}.analyse.pcm", ANALYSE_SR, 1)
    y = np.fromfile(pcm, dtype="<i2").astype(np.float32) / 32768.0
    duration = len(y) / ANALYSE_SR
    tempo, beats = librosa.beat.beat_track(y=y, sr=ANALYSE_SR, units="time")
    tempo = float(np.atleast_1d(tempo)[0])
    beats = [round(float(b), 3) for b in beats]

    # Downbeats: assume 4/4 and take the phase whose beats carry the most onset energy.
    onset = librosa.onset.onset_strength(y=y, sr=ANALYSE_SR)
    times = librosa.times_like(onset, sr=ANALYSE_SR)
    strength = np.interp(beats, times, onset) if beats else np.array([])
    phase = int(np.argmax([strength[p::4].sum() for p in range(4)])) if len(beats) >= 8 else 0
    downbeats = beats[phase::4]

    rms = librosa.feature.rms(y=y, frame_length=ANALYSE_SR, hop_length=ANALYSE_SR)[0]
    energy_db = 20 * np.log10(np.maximum(rms, 1e-5))
    energy = float(np.clip((np.mean(energy_db) + 40) / 30, 0, 1))  # ~-40 dB quiet .. -10 dB loud
    return {"id": track_id, "name": path.stem, "path": str(path.resolve()),
            "duration": round(duration, 2), "bpm": round(tempo, 1), "beats": beats,
            "downbeats": downbeats, "energy": round(energy, 2),
            "energy_curve": np.round(energy_db, 1).tolist(),
            "tags": _tags(path, root), "licence": _licence_for(path)}


def import_folder(folder: Path, log: Log = print,
                  progress: Callable[[float, str], None] | None = None) -> list[dict]:
    progress = progress or (lambda f, m: None)
    files = sorted(p for p in folder.rglob("*") if p.suffix.lower() in AUDIO_EXTENSIONS
                   and not p.name.startswith("."))
    if not files:
        raise ValueError(f"no audio files in {folder}")
    library = {t["id"]: t for t in load_library()}
    for n, f in enumerate(files, 1):
        progress((n - 1) / len(files), f"{f.name}: analysing")
        track = analyse_track(f, folder)
        library[track["id"]] = track
        log(f"[{n}/{len(files)}] {f.name}: {track['bpm']:.0f} bpm, energy {track['energy']:.2f}")
    write_json(library_path(), {"tracks": sorted(library.values(), key=lambda t: t["name"])})
    progress(1.0, f"{len(files)} tracks in library")
    return list(library.values())


def choose(brief_music: dict, style: dict, library: list[dict]) -> dict | None:
    """Pick a bed: the named track, or the best mood/energy match from the library."""
    if brief_music.get("mode") == "none" or not library or not style["music"]["presence"]:
        return None
    if brief_music.get("mode") == "track":
        return next((t for t in library if t["id"] == brief_music.get("track_id")), None)
    want_energy = {"low": 0.25, "mid": 0.55, "high": 0.85}[brief_music.get("energy")
                                                            or style["music"]["energy"]]
    mood = set(re.findall(r"[a-z]+", (brief_music.get("mood") or "").lower()))

    def score(t: dict) -> tuple:
        return (len(mood & set(t["tags"])), -abs(t["energy"] - want_energy), t["name"])

    return max(library, key=score)


def fit_plan(track: dict, duration_s: float) -> list[tuple[float, float]]:
    """Source ranges in the track, played back to back, ending on the final downbeat.

    Short edits take the last `duration_s` before the final downbeat. Long
    edits play the track through, then loop the body (first downbeat after
    25 % to the final downbeat) whole, so every join is downbeat to downbeat.
    Any excess comes off the very start, under the fade-in.
    """
    downbeats = track.get("downbeats") or []
    end = downbeats[-1] if downbeats and downbeats[-1] > track["duration"] * 0.5 \
        else track["duration"]
    if end >= duration_s:
        return [(round(end - duration_s, 4), end)]
    loop_from = next((d for d in downbeats if d >= end * 0.25), 0.0)
    body = end - loop_from
    if body < 1.0:
        loop_from, body = 0.0, end
    loops = int(np.ceil((duration_s - end) / body))
    pieces = [(0.0, end)] + [(loop_from, end)] * loops
    overshoot = end + loops * body - duration_s
    pieces[0] = (round(overshoot, 4), end)
    return pieces


def render_bed(track: dict, pieces: list[tuple[float, float]], total_samples: int,
               crossfade_s: float = 0.05) -> np.ndarray:
    """Float stereo bed of exactly total_samples, joined with short crossfades on bar lines."""
    sr, ch = config.MIX_SAMPLE_RATE, config.MIX_CHANNELS
    pcm = decode_pcm(Path(track["path"]), library_dir() / "pcm" / f"{track['id']}.mix.pcm")
    src = np.memmap(pcm, dtype="<i2", mode="r").reshape(-1, ch)
    out = np.zeros((total_samples, ch), dtype=np.float32)
    xf = max(1, int(crossfade_s * sr))
    cursor = 0
    for n, (a, b) in enumerate(pieces):
        s0, s1 = int(round(a * sr)), int(round(b * sr))
        chunk = src[s0:s1].astype(np.float32) / 32768.0
        if n > 0 and len(chunk) > xf and cursor >= xf:
            ramp = np.linspace(0, 1, xf, dtype=np.float32)[:, None]
            out[cursor - xf:cursor] *= (1 - ramp)
            out[cursor - xf:cursor] += chunk[:xf] * ramp
            chunk = chunk[xf:]
        end = min(total_samples, cursor + len(chunk))
        out[cursor:end] = chunk[: end - cursor]
        cursor = end
        if cursor >= total_samples:
            break
    return out


def gain_envelope(total_samples: int, speech: list[tuple[float, float]],
                  beat_energy: list[tuple[float, float, str]], under_db: float, gap_db: float,
                  fade_in_s: float = 1.0) -> np.ndarray:
    """Music gain per sample in linear terms: ducked under speech, shaped by beat energy."""
    sr = config.MIX_SAMPLE_RATE
    hop = sr // 100  # 10 ms control rate, interpolated to samples
    n = total_samples // hop + 2
    t = np.arange(n) * hop / sr
    active = np.zeros(n, dtype=np.float32)
    for a, b in speech:
        active[(t >= a - DUCK_ATTACK_S) & (t < b + DUCK_RELEASE_S)] = 1.0
    # Smooth the switch so the duck breathes instead of clicking.
    k = max(1, int(0.12 * 100))
    active = np.convolve(active, np.ones(k) / k, mode="same")
    db = gap_db + (under_db - gap_db) * active
    shape = np.zeros(n, dtype=np.float32)
    for a, b, energy in beat_energy:
        shape[(t >= a) & (t < b)] = ENERGY_GAIN_DB.get(energy, -3.0)
    k = max(1, int(1.0 * 100))
    shape = np.convolve(np.pad(shape, (k // 2, k - k // 2 - 1), mode="edge"),
                        np.ones(k) / k, mode="valid")
    db = db + shape[: len(db)]
    gain = 10 ** (db / 20)
    gain *= np.clip(t / max(fade_in_s, 1e-3), 0, 1)
    out = np.interp(np.arange(total_samples) / sr, t, gain).astype(np.float32)
    return out[:, None]

"""Ingest: proxies first, then analysis, into a Clip Index (plan §4).

Phase 0 indexes only what the rough cut needs: words, audio energy and
sentence segments with a highlight score. Shots, faces, VLM captions and
embeddings join the same record in Phase 1 without changing its shape.
"""

from __future__ import annotations

import re
import wave
from pathlib import Path
from typing import Callable

import numpy as np

from . import asr, config, media, vision
from .project import Project, file_fingerprint, find_clips, read_json, write_json

# Bump when the per-clip record changes shape or meaning; stale caches re-run.
ANALYSIS_VERSION = 6
ENERGY_HOP_S = 0.1
SENTENCE_END = re.compile(r"[.?!…]['\"”’)]*$")

Log = Callable[[str], None]


def rms_envelope(wav: Path, hop_s: float = ENERGY_HOP_S) -> list[float]:
    with wave.open(str(wav), "rb") as w:
        rate = w.getframerate()
        samples = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    hop = max(1, int(rate * hop_s))
    n = len(samples) // hop
    if n == 0:
        return []
    frames = samples[: n * hop].astype(np.float32).reshape(n, hop) / 32768.0
    rms = np.sqrt(np.mean(frames * frames, axis=1))
    return np.round(20 * np.log10(np.maximum(rms, 1e-4)), 1).tolist()


def refine_word_edges(words: list[dict], wav: Path, pause_s: float = 0.15,
                      hop_s: float = 0.01) -> list[dict]:
    """Snap word edges that border a pause to where the voice actually starts and stops.

    ASR timestamps are only as fine as the encoder frame (80 ms for Parakeet)
    and drift further at pauses, where the last piece is emitted after the
    sound has ended. The jump-cut rule cuts exactly at these edges, so they
    are tightened against a 10 ms energy envelope. Edges only ever move
    inwards, and only next to a pause, so a word is never extended or split.
    """
    if not words:
        return words
    with wave.open(str(wav), "rb") as w:
        rate = w.getframerate()
        samples = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    hop = max(1, int(rate * hop_s))
    n = len(samples) // hop
    if n < 10:
        return words
    frames = samples[: n * hop].astype(np.float32).reshape(n, hop) / 32768.0
    db = 20 * np.log10(np.maximum(np.sqrt(np.mean(frames * frames, axis=1)), 1e-5))
    floor, peak = np.percentile(db, 10), np.percentile(db, 99)
    if peak - floor < 15:
        return words  # no clear speech/room-tone contrast: trust the ASR
    # Generous threshold: soft consonants sit well below vowels but far above room tone.
    voiced = db > max(floor + 12, peak - 55)

    out = []
    for i, w in enumerate(words):
        s, e = w["s"], w["e"]
        # Clip head and tail always count as pauses: a jump cut trims there too.
        gap_before = s - words[i - 1]["e"] if i else np.inf
        gap_after = words[i + 1]["s"] - e if i + 1 < len(words) else np.inf
        lo, hi = int(s / hop_s), min(n, int(np.ceil(e / hop_s)))
        idx = np.flatnonzero(voiced[lo:hi])
        if len(idx):
            if gap_before >= pause_s:
                s = max(s, (lo + idx[0]) * hop_s - hop_s)
            if gap_after >= pause_s:
                e = min(e, (lo + idx[-1] + 1) * hop_s + hop_s)
        if e - s < 0.05:  # never collapse a word to nothing
            s, e = w["s"], w["e"]
        out.append({**w, "s": round(s, 3), "e": round(e, 3)})
    return out


def build_segments(clip_id: str, words: list[dict]) -> list[dict]:
    """Group words into sentence units, the smallest piece the fit may drop."""
    segments: list[dict] = []
    start = 0
    for i, w in enumerate(words):
        last = i == len(words) - 1
        gap = (words[i + 1]["s"] - w["e"]) if not last else 0.0
        too_long = (not last) and words[i + 1]["e"] - words[start]["s"] > config.SEGMENT_MAX_S
        if last or SENTENCE_END.search(w["w"]) or gap > config.SEGMENT_BREAK_GAP_S or too_long:
            chunk = words[start: i + 1]
            segments.append({
                "id": f"{clip_id}:s{len(segments):03d}",
                "start": chunk[0]["s"], "end": chunk[-1]["e"],
                "words": [start, i + 1],
                "text": " ".join(x["w"] for x in chunk),
            })
            start = i + 1
    return segments


def segment_features(seg: dict, words: list[dict], energy: list[float],
                     visual: dict | None = None) -> dict:
    chunk = words[seg["words"][0]: seg["words"][1]]
    dur = max(seg["end"] - seg["start"], 0.1)
    lo = int(seg["start"] / ENERGY_HOP_S)
    hi = max(lo + 1, int(np.ceil(seg["end"] / ENERGY_HOP_S)))
    window = energy[lo:hi]
    feats = {
        "speech_rate": len(chunk) / dur,
        "confidence": float(np.mean([w["p"] for w in chunk])),
        "energy": float(np.mean(window)) if window else -90.0,
        "face": 0.0, "sharp": 0.0, "steady": 0.0,
    }
    if visual:
        q = visual["quality"]
        a, b = int(seg["start"]), max(int(seg["start"]) + 1, int(np.ceil(seg["end"])))
        faces = [f for f in visual["faces"] if seg["start"] <= f[0] < seg["end"]]
        samples = max(1.0, (seg["end"] - seg["start"]) * vision.SAMPLE_FPS)
        feats["face"] = min(1.0, len(faces) / samples)
        feats["sharp"] = float(np.mean(q["sharp"][a:b] or [0.0]))
        # Shaky spans read as amateur; steadiness is the negative of motion.
        feats["steady"] = -float(np.mean(q["motion"][a:b] or [0.0]))
    return feats


def score_segments(clips: list[dict]) -> None:
    """Rank-normalise each feature across the project, then blend.

    Ranks rather than raw values: one shouted sentence must not flatten every
    other segment's energy score towards zero.
    """
    _score_broll(clips)
    segs = [s for c in clips for s in c["segments"]]
    if not segs:
        return
    weights = config.HIGHLIGHT_WEIGHTS
    total = sum(weights.values())
    blended = np.zeros(len(segs))
    for feature, weight in weights.items():
        values = np.array([s["features"].get(feature, 0.0) for s in segs])
        if len(segs) == 1:
            pct = np.array([0.5])
        else:
            pct = values.argsort().argsort() / (len(segs) - 1)
        blended += weight * pct
    for s, v in zip(segs, blended / total):
        s["score"] = round(float(v), 3)


def _rank(values: list[float]) -> np.ndarray:
    v = np.array(values, dtype=float)
    if len(v) <= 1:
        return np.full(len(v), 0.5)
    return v.argsort().argsort() / (len(v) - 1)


def _score_broll(clips: list[dict]) -> None:
    """B-roll units are shots of clips without speech: sharp, steady and well exposed wins."""
    shots = [s for c in clips if not c["words"] for s in c.get("shots", [])]
    if not shots:
        return
    exposure = [-abs(s["luma"] - 0.45) for s in shots]
    blended = (0.45 * _rank([s["sharp"] for s in shots]) +
               0.35 * _rank([-s["motion"] for s in shots]) + 0.2 * _rank(exposure))
    for s, v in zip(shots, blended):
        s["score"] = round(float(v), 3)


def analyse_clip(project: Project, src: Path, clip_id: str, probe: media.Probe,
                 backend: str, language: str | None, transcripts_dir: Path | None,
                 log: Log = print) -> dict:
    cache = project.path("index", "clips", f"{clip_id}.json")
    if cache.exists():
        cached = read_json(cache)
        if cached.get("analysis_version") == ANALYSIS_VERSION:
            cached["path"] = str(src)  # a moved original keeps its analysis
            return cached

    wav = project.path("media", "audio", f"{clip_id}.wav")
    words, lang, used = ([], language or "en", "none")
    energy: list[float] = []
    if probe.has_audio:
        words, lang, used = asr.transcribe(src, wav, backend, language, transcripts_dir)
        # A sidecar is a hand-checked transcript: its times are the truth.
        if not used.startswith("sidecar"):
            words = refine_word_edges(words, wav)
        energy = rms_envelope(wav)

    record = {
        "id": clip_id, "name": src.name, "proxy": f"media/proxies/{clip_id}.mp4",
        "duration": probe.duration, "width": probe.width, "height": probe.height,
    }
    visual = vision.analyse_visual(project, record, log)

    segments = build_segments(clip_id, words)
    for seg in segments:
        seg["features"] = segment_features(seg, words, energy, visual)
        kf = vision.nearest_keyframe(visual["keyframes"], (seg["start"] + seg["end"]) / 2)
        seg["keyframe"] = kf[1] if kf else None

    record = {
        "analysis_version": ANALYSIS_VERSION,
        "id": clip_id, "path": str(src), "name": src.name,
        "duration": probe.duration, "width": probe.width, "height": probe.height,
        "fps": probe.fps, "has_audio": probe.has_audio,
        "proxy": f"media/proxies/{clip_id}.mp4",
        "mix_audio": f"media/audio/{clip_id}.pcm" if probe.has_audio else None,
        "language": lang, "asr": used,
        "words": words,
        "energy": {"hop": ENERGY_HOP_S, "rms_db": energy},
        "segments": segments,
        "speech": bool(words),
        **visual,
    }
    write_json(cache, record)
    return record


def ingest(project: Project, folder: Path, backend: str = "auto", language: str | None = None,
           transcripts_dir: Path | None = None, recursive: bool = False,
           log: Log = print, progress: Callable[[float, str], None] | None = None) -> dict:
    progress = progress or (lambda frac, msg: None)
    media.require_ffmpeg()
    sources = find_clips(folder, recursive)
    if not sources:
        raise SystemExit(f"no video files in {folder}")

    # Pass 1: proxies and audio, so a preview is possible before ASR finishes.
    prepared = []
    seen: dict[str, Path] = {}
    for n, src in enumerate(sources, 1):
        clip_id = "c_" + file_fingerprint(src)[:12]
        if clip_id in seen:
            log(f"[{n}/{len(sources)}] {src.name}: same content as {seen[clip_id].name}, skipped")
            continue
        seen[clip_id] = src
        progress(0.5 * (n - 1) / len(sources), f"{src.name}: proxy")
        probe = media.probe(src)
        if not probe.has_video:
            log(f"[{n}/{len(sources)}] {src.name}: no video stream, skipped")
            continue
        media.make_proxy(src, project.path("media", "proxies", f"{clip_id}.mp4"), probe)
        if probe.has_audio:
            media.extract_audio(src, project.path("media", "audio", f"{clip_id}.wav"),
                                project.path("media", "audio", f"{clip_id}.pcm"))
        log(f"[{n}/{len(sources)}] {src.name}: proxy ready "
            f"({probe.width}x{probe.height} @ {probe.fps:g} fps, {probe.duration:.1f} s)")
        prepared.append((src, clip_id, probe))

    # Pass 2: analysis, cached by content so a clip is analysed once, ever.
    clips = []
    for n, (src, clip_id, probe) in enumerate(prepared, 1):
        progress(0.5 + 0.5 * (n - 1) / len(prepared), f"{src.name}: analysing")
        record = analyse_clip(project, src, clip_id, probe, backend, language, transcripts_dir,
                              log)
        log(f"[{n}/{len(prepared)}] {src.name}: {len(record['words'])} words, "
            f"{len(record['segments'])} segments, {len(record['shots'])} shots "
            f"({record['asr']}{', faces' if record['faces_available'] else ''})")
        clips.append(record)

    score_segments(clips)
    index = {"version": ANALYSIS_VERSION, "clips": clips}
    write_json(project.clip_index_path, index)
    progress(1.0, f"{len(clips)} clips indexed")
    return index

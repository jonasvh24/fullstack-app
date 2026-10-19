"""The measurements behind the style engine (plan §5).

One function measures any video: a creator's reference or nullcut's own
render. Extraction runs it on references to build a Style Profile; the match
score runs it on the output and compares. The closed loop only means
something because both sides are measured by the same code.

Everything is measured from pixels and samples. For nullcut's own renders the
transcript is passed in (it is already known exactly); for references it
comes from ASR.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Callable

import numpy as np

from . import config, media, vision

SAMPLE_FPS = 10.0       # fast punches ease in over ~0.12 s; 10 fps still lands inside one
DECODE_W = 480
OCR_FPS = 1.0
ZOOM_MIN = 1.035        # a smaller change reads as handheld drift, not a zoom
HOOK_S = 15.0

Log = Callable[[str], None]


def _frames(path: Path, fps: float, width: int):
    probe = media.probe(path)
    w = width
    h = max(2, int(round(width * (probe.height or 9) / (probe.width or 16) / 2)) * 2)
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-i", str(path), "-an",
           "-vf", f"fps={fps},scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    size = w * h * 3
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
        n = 0
        while (buf := proc.stdout.read(size)) and len(buf) == size:
            yield n / fps, np.frombuffer(buf, np.uint8).reshape(h, w, 3)
            n += 1


def detect_cuts(path: Path, fps: float) -> list[float]:
    """Hard cuts, including jump cuts within one talking-head take.

    FFmpeg's scene score misses jump cuts: same speaker, same framing, little
    global change. A cut is a *local discontinuity*: one frame differs from
    the previous far more than its neighbours do. Measuring that per cell of a
    4x4 grid and taking the median across cells means a caption swap (a few
    cells) cannot fake a cut, while a cut moves every cell.
    """
    w, h, grid = 128, 72, 4
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-i", str(path), "-an",
           "-vf", f"scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    frames = np.frombuffer(raw, np.uint8)[: len(raw) // (w * h) * w * h].reshape(-1, h, w)
    if len(frames) < 3:
        return []
    diff = np.abs(np.diff(frames.astype(np.float32), axis=0))
    cells = diff.reshape(len(diff), grid, h // grid, grid, w // grid).mean(axis=(2, 4))
    cells = cells.reshape(len(diff), -1)
    cuts = []
    for i in range(len(cells)):
        lo, hi = max(0, i - 6), min(len(cells), i + 7)
        ctx = np.concatenate([cells[lo:i], cells[i + 1:hi]])
        if len(ctx) < 3:
            continue
        ratio = cells[i] / (np.median(ctx, axis=0) + 0.75)
        if np.median(ratio) > 3.0 and cells[i].mean() > 1.5:
            # A zoom also moves every cell, but the previous frame scaled explains
            # it; a cut has no such transform. Zoom motion is measured separately.
            ds = _scale_step(frames[i], frames[i + 1], min_points=15)
            if ds is not None and abs(ds) > 0.006:
                continue
            cuts.append((i + 1) / fps)
    out: list[float] = []
    for c in cuts:
        if not out or c - out[-1] > 0.2:
            out.append(round(c, 3))
    return out


def _scale_step(prev, cur, min_points: int = 30) -> float | None:
    """log scale between two frames from tracked features; None when it cannot tell."""
    import cv2

    pts = cv2.goodFeaturesToTrack(prev, maxCorners=300, qualityLevel=0.01,
                                  minDistance=7 if min_points >= 30 else 3)
    if pts is None or len(pts) < min_points:
        return None
    nxt, status, _ = cv2.calcOpticalFlowPyrLK(prev, cur, pts, None)
    good = status.ravel() == 1
    if good.sum() < min_points:
        return None
    m, inliers = cv2.estimateAffinePartial2D(pts[good], nxt[good], method=cv2.RANSAC,
                                             ransacReprojThreshold=2.0)
    if m is None or inliers is None or inliers.sum() < min_points * 2 // 3:
        return None
    return float(np.log(np.hypot(m[0, 0], m[1, 0])))


def zoom_events(steps: list[tuple[float, float | None]]) -> list[dict]:
    """Zoom-ins: runs of clearly growing scale whose total passes ZOOM_MIN.

    A run continues while scale grows by more than 0.2 % a step, with one quiet
    step of grace; handheld wobble neither starts nor extends a run, and a cut
    (None) ends it.
    """
    events: list[dict] = []
    run, start, last, grace = 0.0, None, None, 0

    def close() -> None:
        if start is not None and run >= np.log(ZOOM_MIN):
            events.append({"t": round(start, 2), "scale": round(float(np.exp(run)), 3),
                           "duration": round(last - start, 2)})

    for t, ds in steps:
        rising = ds is not None and ds > 0.002
        if rising:
            if start is None:
                start, run = t, 0.0
            run += ds
            last, grace = t, 1
        elif start is not None and ds is not None and ds > -0.004 and grace:
            grace -= 1
        else:
            close()
            run, start, last = 0.0, None, None
    close()
    return events


def _vivid(pixels: np.ndarray) -> np.ndarray:
    mx, mn = pixels.max(1), pixels.min(1)
    return pixels[(mx > 150) & ((mx - mn) / np.maximum(mx, 1) > 0.45)]


def _hue_bins(pixels: np.ndarray) -> np.ndarray:
    import cv2

    if len(pixels) == 0:
        return np.zeros(18)
    hsv = cv2.cvtColor(pixels.reshape(-1, 1, 3).astype(np.uint8), cv2.COLOR_BGR2HSV)
    return np.bincount(hsv[:, 0, 0] // 10, minlength=18)[:18].astype(float)


def _text_colour(frame: np.ndarray, box) -> np.ndarray | None:
    """The highlight colour in a caption box: a vivid hue the background ring lacks."""
    h, w = frame.shape[:2]
    x0, y0 = np.clip(box.min(0).astype(int), 0, [w - 1, h - 1])
    x1, y1 = np.clip(box.max(0).astype(int), 0, [w - 1, h - 1])
    if x1 <= x0 or y1 <= y0:
        return None
    pad_x, pad_y = max(4, (x1 - x0) // 4), max(4, (y1 - y0))
    X0, Y0 = max(0, x0 - pad_x), max(0, y0 - pad_y)
    X1, Y1 = min(w - 1, x1 + pad_x), min(h - 1, y1 + pad_y)
    inner = frame[y0:y1 + 1, x0:x1 + 1].reshape(-1, 3).astype(np.float32)
    outer = frame[Y0:Y1 + 1, X0:X1 + 1].astype(np.float32).copy()
    outer[y0 - Y0:y1 - Y0 + 1, x0 - X0:x1 - X0 + 1] = 0
    ring = outer.reshape(-1, 3)
    vivid_in, vivid_ring = _vivid(inner), _vivid(ring[ring.any(1)])
    if len(vivid_in) < 0.03 * len(inner):
        return None
    hin = _hue_bins(vivid_in) / max(1, len(inner))
    hring = _hue_bins(vivid_ring) / max(1, len(ring))
    excess = hin - 1.5 * hring
    b = int(np.argmax(excess))
    if excess[b] < 0.02:
        return None
    import cv2
    hues = cv2.cvtColor(vivid_in.reshape(-1, 1, 3).astype(np.uint8), cv2.COLOR_BGR2HSV)[:, 0, 0]
    return np.median(vivid_in[hues // 10 == b], axis=0)


def _caption_stats(ocr_frames: list[tuple[float, list, np.ndarray]],
                   spoken: set[str] | None) -> tuple[dict, float]:
    """Captions: text that matches what is said (or, without a transcript, low text that
    keeps changing). Everything else on screen counts as an overlay."""
    blocks, ys, uppers, colours, overlays = [], [], [], [], set()
    for _, result, frame in ocr_frames:
        h = frame.shape[0]
        lines = []
        for box, text, score in result or []:
            if score < 0.6 or not text.strip():
                continue
            box = np.array(box)
            cy = float(box[:, 1].mean()) / h
            words = re.findall(r"[\w']+", text)
            hit = spoken is not None and words and \
                sum(w.lower() in spoken for w in words) / len(words) >= 0.5
            if hit or (spoken is None and cy > 0.55):
                lines.append((cy, text, box))
            else:
                overlays.add(re.sub(r"\W+", "", text.lower()))
        if not lines:
            continue
        # One caption block is the lowest stack of lines: a two-line caption is one block.
        lines.sort(key=lambda x: x[0])
        block = [lines[-1]]
        for ln in reversed(lines[:-1]):
            if block[-1][0] - ln[0] < 0.12:
                block.append(ln)
            else:
                break
        text = " ".join(t for _cy, t, _box in reversed(block))
        words = re.findall(r"[\w']+", text)
        if not words:
            continue
        blocks.append(len(words))
        ys.append(block[0][0])
        letters = [c for c in text if c.isalpha()]
        uppers.append(sum(c.isupper() for c in letters) / max(1, len(letters)))
        for _, _, box in block:
            colour = _text_colour(frame, box)
            if colour is not None:
                colours.append(colour)
    stats: dict = {"present": len(blocks) >= max(3, 0.2 * len(ocr_frames))}
    if blocks:
        stats.update({"words_per_block": round(float(np.median(blocks)), 1),
                      "words_p90": round(float(np.percentile(blocks, 90)), 1),
                      "position_y": round(float(np.median(ys)), 2),
                      "upper_ratio": round(float(np.mean(uppers)), 2)})
        if len(colours) >= max(2, 0.15 * len(blocks)):
            b, g, r = np.median(colours, axis=0)
            stats["highlight"] = f"#{int(r):02X}{int(g):02X}{int(b):02X}"
    return stats, len(overlays)


def _audio(path: Path, rate: int = 16_000) -> np.ndarray:
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "a.wav"
        media.ffmpeg("-i", str(path), "-vn", "-ac", "1", "-ar", str(rate), "-c:a", "pcm_s16le",
                     str(wav))
        with wave.open(str(wav)) as w:
            return np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768


def _rms_db(x: np.ndarray) -> float:
    return float(20 * np.log10(max(np.sqrt(np.mean(x * x)) if x.size else 0.0, 1e-6)))


def music_stats(audio: np.ndarray, rate: int, words: list[dict], duration: float) -> dict:
    """Music presence and level from the mix alone, without stem separation.

    The duck releases slowly, so the music level in a pause depends on the
    pause: long pauses (over 1.2 s) hear the bed at its gap level, short ones
    (0.3 to 0.7 s) still hear it ducked, which is the level under speech that
    the plan wanted Demucs for. Both are relative to the speech level.
    """
    speech = np.zeros(len(audio), bool)
    for w in words:
        speech[int(w["s"] * rate):int(w["e"] * rate)] = True
    speech_db = _rms_db(audio[speech]) if speech.any() else _rms_db(audio)
    edges = [0.0] + [x for w in words for x in (w["s"], w["e"])] + [duration]
    short, long = [], []
    for a, b in zip(edges[0::2], edges[1::2]):
        if b - a >= 1.2:
            long.append(audio[int((a + 0.5) * rate):int((b - 0.1) * rate)])
        elif 0.25 <= b - a <= 0.7:
            # The middle of the pause only: its edges are word decay, not music.
            third = (b - a) / 3
            short.append(audio[int((a + third) * rate):int((b - third) * rate)])
    gap_db = _rms_db(np.concatenate(long)) - speech_db if long else None
    under_db = _rms_db(np.concatenate(short)) - speech_db if short else None
    level = gap_db if gap_db is not None else under_db
    # No pause to listen into (a very tight cut) means unknown, not absent.
    out = {"gap_db": round(gap_db, 1) if gap_db is not None else None,
           "under_speech_db": round(under_db, 1) if under_db is not None else None,
           "presence": None if level is None else level > -38.0}
    if out["presence"]:
        import librosa
        # Track the beat below 150 Hz, where the kick lives and speech mostly does not;
        # on the full mix, speech syllables drag the tempo estimate.
        env = librosa.onset.onset_strength(y=audio, sr=rate, fmax=150, n_mels=32)
        tempo, beats = librosa.beat.beat_track(onset_envelope=env, sr=rate, units="time")
        out["bpm"] = round(float(np.atleast_1d(tempo)[0]), 1)
        out["beats"] = [round(float(b), 3) for b in beats]
    return out


def sfx_share(audio: np.ndarray, rate: int, cuts: list[float],
              words: list[dict] | None = None) -> float | None:
    """Share of cuts with a whoosh or impact on them.

    A jump cut sits in a gap with no speech (the handles either side), and an
    SFX fills that gap. So: high-band energy (3 to 7.5 kHz) in the speech-free
    part of the gap, against the same band in other speech-free moments. Cuts
    with speech across them cannot be judged and are left out.
    """
    if not cuts:
        return None
    import librosa

    hop = 256
    S = np.abs(librosa.stft(audio, n_fft=1024, hop_length=hop))
    freqs = librosa.fft_frequencies(sr=rate, n_fft=1024)
    band = S[(freqs >= 3000) & (freqs <= 7500)].sum(axis=0)
    times = librosa.frames_to_time(np.arange(len(band)), sr=rate, hop_length=hop)
    speech = np.zeros(len(band), bool)
    for w in words or []:
        # 40 ms either side: fricative tails ring past the word's edge.
        speech |= (times >= w["s"] - 0.04) & (times <= w["e"] + 0.04)
    # The reference is the speech itself: SFX are mixed relative to dialogue, and speech
    # exists in every edit, tight or loose. Measured on Gate 2 renders, gaps with a whoosh
    # sit at 3x to 7x this level and gaps without one at 0.002x to 0.02x.
    ref = np.median(band[speech]) if speech.sum() > 20 else np.median(band)
    judged = hits = 0
    for c in cuts:
        win = (times >= c - 0.3) & (times <= c + 0.1) & ~speech
        # A jump cut leaves ~0.2 s without speech (its handles). A visual cut in the
        # middle of a sentence (a B-roll edge) leaves only the gap between two words,
        # which cannot be judged.
        if win.sum() * hop / rate < 0.08:
            continue
        judged += 1
        if np.median(band[win]) > 0.3 * ref + 1e-9:
            hits += 1
    return round(hits / judged, 2) if judged else None


def colour_stats(frames: list[np.ndarray]) -> dict:
    import cv2

    sats, cons, warms, lifts = [], [], [], []
    for f in frames:
        hsv = cv2.cvtColor(f, cv2.COLOR_BGR2HSV).astype(np.float32)
        gray = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255
        sats.append(hsv[..., 1].mean() / 255)
        cons.append(gray.std())
        b, _, r = f.reshape(-1, 3).astype(np.float32).mean(0) / 255
        warms.append(r - b)
        lifts.append(np.percentile(gray, 2))
    return {"saturation": round(float(np.median(sats)), 3),
            "contrast": round(float(np.median(cons)), 3),
            "warmth": round(float(np.median(warms)), 3),
            "lift": round(float(np.median(lifts)), 3)}


def _ocr():
    from rapidocr_onnxruntime import RapidOCR

    return RapidOCR()


def measure(path: Path, words: list[dict] | None = None, log: Log = lambda m: None,
            with_ocr: bool = True) -> dict:
    """Every style parameter of plan §5 that can be read from a finished video."""
    import cv2

    probe = media.probe(path)
    duration = probe.duration
    minutes = max(duration / 60, 1e-6)

    log(f"{path.name}: cuts")
    scene = [a for a, _ in vision.detect_shots(path, duration)[1:]]
    local = detect_cuts(path, probe.fps or 30.0)
    cuts = sorted(local + [c for c in scene if all(abs(c - x) > 0.2 for x in local)])
    bounds = [0.0] + cuts + [duration]
    lengths = [b - a for a, b in zip(bounds, bounds[1:])]

    if words is None:
        log(f"{path.name}: transcribing")
        words = _transcribe(path)
    spoken = {w2.lower() for w in words for w2 in re.findall(r"[\w']+", w["w"])}

    log(f"{path.name}: motion, faces, colour")
    faces = vision.FaceDetector(DECODE_W, 2)  # resized per frame below
    steps: list[tuple[float, float | None]] = []
    face_at: list[tuple[float, bool]] = []
    colour_frames, ocr_frames = [], []
    prev, prev_t = None, None
    ocr = _ocr() if with_ocr else None
    for t, frame in _frames(path, SAMPLE_FPS, DECODE_W):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if prev is not None:
            crossed = any(prev_t < c <= t + 1e-6 for c in cuts)
            steps.append((t, None if crossed else _scale_step(prev, gray)))
        prev, prev_t = gray, t
        k = int(round(t * SAMPLE_FPS))
        if k % 2 == 0 and faces.model is not None:
            h, w = frame.shape[:2]
            faces.model.setInputSize((w, h))
            faces.w, faces.h = w, h
            face_at.append((t, faces.largest(frame) is not None))
        if k % int(SAMPLE_FPS) == 0:
            colour_frames.append(frame)
        if ocr is not None and k % int(SAMPLE_FPS / OCR_FPS) == 0:
            result, _ = ocr(frame)
            ocr_frames.append((t, result, frame))

    # Punch-in cuts: the same picture, tighter. Count them as zooms as well as cuts.
    zooms = zoom_events(steps)
    events = sorted(set(cuts) | {z["t"] for z in zooms} | {z["t"] + z["duration"] for z in zooms})
    statics = np.diff([0.0] + events + [duration]) if duration else np.array([0.0])

    pauses = [b["s"] - a["e"] for a, b in zip(words, words[1:]) if 0.05 < b["s"] - a["e"] < 3.0]
    caption, overlays = _caption_stats(ocr_frames, spoken if words else None) if ocr else ({}, 0)

    speech_samples = [hit for t, hit in face_at
                      if any(w["s"] <= t < w["e"] for w in words)]
    broll = (round(1 - float(np.mean(speech_samples)), 3)
             if speech_samples and faces.model is not None else None)

    audio = _audio(path)
    music = music_stats(audio, 16_000, words, duration)
    if music.get("beats") and cuts:
        on_beat = [min(abs(b - c) for b in music["beats"]) <= 0.06 for c in cuts]
        music["cuts_on_beat"] = round(float(np.mean(on_beat)), 2)
    music.pop("beats", None)

    hook_cuts = [c for c in cuts if c < HOOK_S]
    return {
        "file": path.name, "duration": round(duration, 2),
        "pacing": {"cuts_per_min": round(len(cuts) / minutes, 1),
                   "hook_cuts_per_min": round(len(hook_cuts) / (min(HOOK_S, duration) / 60), 1)
                   if duration else 0.0,
                   "shot_median": round(float(np.median(lengths)), 2) if lengths else None,
                   "shot_p10": round(float(np.percentile(lengths, 10)), 2) if lengths else None,
                   "shot_p90": round(float(np.percentile(lengths, 90)), 2) if lengths else None},
        # The longest pauses an edit keeps sit just under its cut threshold. p75
        # rather than the maximum: ASR edges move in 80 ms steps.
        "silence_ms": round(float(np.percentile(pauses, 75)) * 1000) if len(pauses) >= 5
        else None,
        "zoom": {"per_minute": round(len(zooms) / minutes, 1),
                 "scale_median": round(float(np.median([z["scale"] for z in zooms])), 3)
                 if zooms else None,
                 "rise_s": round(float(np.median([z["duration"] for z in zooms])), 2)
                 if zooms else None},
        "max_static_s": round(float(np.percentile(statics, 90)), 2),
        "captions": caption,
        "overlays_per_min": round(overlays / minutes, 1),
        "broll_ratio": broll,
        "music": music,
        "sfx_cut_share": sfx_share(audio, 16_000, cuts, words),
        "colour": colour_stats(colour_frames) if colour_frames else {},
        "words": len(words),
    }


def _transcribe(path: Path) -> list[dict]:
    from . import asr

    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "a.wav"
        media.ffmpeg("-i", str(path), "-vn", "-ac", "1", "-ar", str(config.ASR_SAMPLE_RATE),
                     "-c:a", "pcm_s16le", str(wav))
        try:
            words, _, _ = asr.transcribe(path, wav, "auto")
        except SystemExit:
            return []  # no ASR available: speech-dependent measures come back empty
    # Raw ASR edges on purpose: the energy refinement of the Clip Index assumes
    # dialogue alone, and a finished video has music under it.
    return words

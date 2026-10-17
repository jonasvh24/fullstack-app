from __future__ import annotations

import json
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")


def words_from(script: list[tuple[str, float, float]]) -> list[dict]:
    return [{"w": w, "s": s, "e": e, "p": 0.95} for w, s, e in script]


def make_clip_record(clip_id: str, script: list[tuple[str, float, float]], duration: float,
                     lang: str = "en", fps: float = 30.0) -> dict:
    """A Clip Index record built in memory, for compiler tests that need no media."""
    from nullcut import analyse

    words = words_from(script)
    segments = analyse.build_segments(clip_id, words)
    for seg in segments:
        seg["features"] = {"speech_rate": 2.0, "confidence": 0.9, "energy": -20.0}
    return {"id": clip_id, "name": f"{clip_id}.mp4", "path": f"/tmp/{clip_id}.mp4",
            "duration": duration, "fps": fps, "width": 1920, "height": 1080,
            "has_audio": True, "proxy": f"media/proxies/{clip_id}.mp4",
            "mix_audio": f"media/audio/{clip_id}.pcm", "language": lang, "asr": "test",
            "words": words, "energy": {"hop": 0.1, "rms_db": []}, "segments": segments}


def make_media(path: Path, script: list[tuple[str, float, float]], duration: float,
               fps: int = 30, size: str = "640x360") -> None:
    """Video test pattern plus audio where every word is a tone burst at its exact time.

    The sidecar transcript then describes the audio truthfully, so loudness and
    timing checks on the render mean something.
    """
    sr = 48_000
    t = np.arange(int(duration * sr)) / sr
    audio = np.zeros_like(t)
    rng = np.random.default_rng(7)
    for n, (_, s, e) in enumerate(script):
        mask = (t >= s) & (t < e)
        freq = 180 + 40 * (n % 5)
        audio[mask] = 0.3 * np.sin(2 * np.pi * freq * t[mask])
    audio += 0.002 * rng.standard_normal(len(t))  # room tone, so silence is not digital zero
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2")
    wav = path.with_suffix(".src.wav")
    with wave.open(str(wav), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc2=s={size}:r={fps}:d={duration}",
         "-i", str(wav), "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", str(path)],
        check=True)
    wav.unlink()
    path.with_name(f"{path.stem}.words.json").write_text(
        json.dumps({"language": "en", "words": words_from(script)}))


# A talking head with every case the rough cut must handle: long pauses,
# a hard filler, a soft filler isolated by pauses, and sentence breaks.
CLIP_A = [
    ("Welcome", 0.50, 0.90), ("to", 0.95, 1.05), ("the", 1.10, 1.25), ("channel.", 1.30, 1.80),
    ("um", 2.60, 2.90),
    ("Today", 3.00, 3.35), ("we", 3.40, 3.55), ("build", 3.60, 3.95), ("things.", 4.00, 4.50),
    ("So", 5.60, 5.80),
    ("This", 6.20, 6.45), ("is", 6.50, 6.60), ("the", 6.65, 6.75), ("plan.", 6.80, 7.40),
]
CLIP_B = [
    ("Second", 0.30, 0.70), ("clip", 0.75, 1.10), ("starts", 1.15, 1.50), ("here.", 1.55, 2.00),
    ("And", 3.50, 3.70), ("it", 3.75, 3.85), ("ends", 3.90, 4.20), ("well.", 4.25, 4.80),
]


@pytest.fixture
def footage(tmp_path: Path) -> Path:
    folder = tmp_path / "footage"
    folder.mkdir()
    make_media(folder / "01_intro.mp4", CLIP_A, 8.0)
    make_media(folder / "02_outro.mp4", CLIP_B, 5.5)
    return folder

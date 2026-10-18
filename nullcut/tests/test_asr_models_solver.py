"""ASR plumbing, edge refinement, model licences and the CP-SAT fit.

The tests marked `needs_parakeet` run the real model on synthesised speech
and are skipped where the weights have not been pulled.
"""

import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest

from nullcut import analyse, asr, models
from nullcut.compiler import CutOptions, Item, compile_rough, fit

from .conftest import HAS_FFMPEG, make_clip_record

PARAKEET = models.DEFAULT_PROFILE["asr"]
needs_parakeet = pytest.mark.skipif(
    not (HAS_FFMPEG and models.installed(PARAKEET)),
    reason=f"{PARAKEET} not pulled (nullcut models pull {PARAKEET})")


def write_wav(path: Path, audio: np.ndarray, rate: int = 16_000) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())


# --- model registry -------------------------------------------------------

def test_default_profile_is_commercially_licensed():
    # Plan §14: one non-commercial weight in the default path blocks a release.
    for job, name in models.DEFAULT_PROFILE.items():
        model = models.REGISTRY[name]
        assert model.job == job
        assert model.commercial, f"{name} ({model.licence}) is not cleared for commercial use"


def test_safe_extract_refuses_path_escape(tmp_path: Path):
    import io
    import tarfile

    archive = tmp_path / "evil.tar.bz2"
    with tarfile.open(archive, "w:bz2") as tar:
        data = b"x"
        info = tarfile.TarInfo("../escape.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    with pytest.raises(Exception):
        models._safe_extract(archive, tmp_path / "out")
    assert not (tmp_path / "escape.txt").exists()


# --- ASR plumbing ---------------------------------------------------------

def test_tokens_merge_into_words_with_punctuation_attached():
    tokens = [" W", "el", "come", ",", " to", " the", " ch", "annel", "."]
    starts = [0.16, 0.40, 0.56, 0.64, 0.72, 0.88, 1.04, 1.20, 1.52]
    durs = [0.24, 0.16, 0.08, 0.08, 0.16, 0.16, 0.16, 0.32, 0.08]
    words = asr.tokens_to_words(tokens, starts, durs, [-0.1] * 9, offset=10.0)
    assert [w["w"] for w in words] == ["Welcome,", "to", "the", "channel."]
    assert words[0]["s"] == pytest.approx(10.16)
    assert words[0]["e"] == pytest.approx(10.64)  # the comma does not stretch the word
    assert words[3]["e"] == pytest.approx(11.52)
    assert 0.85 < words[0]["p"] < 0.95


def test_chunks_split_at_the_quietest_point():
    rate = 16_000
    t = np.arange(rate * 100) / rate
    audio = 0.3 * np.sin(2 * np.pi * 200 * t)
    audio[int(41.0 * rate): int(41.3 * rate)] = 0.0  # the only pause before 45 s
    samples = (audio * 32767).astype(np.int16)
    bounds = asr.chunk_bounds(samples, rate)
    assert bounds[0][0] == 0 and bounds[-1][1] == len(samples)
    assert 41.0 <= bounds[0][1] / rate <= 41.3
    assert all(a[1] == b[0] for a, b in zip(bounds, bounds[1:]))


def test_language_detection():
    nl = [{"w": w} for w in "ik denk dat het een goed plan is voor de film".split()]
    en = [{"w": w} for w in "I think that the plan is good for the film".split()]
    assert asr.detect_language(nl) == "nl"
    assert asr.detect_language(en) == "en"


def test_edge_refinement_snaps_to_voice_and_only_shrinks(tmp_path: Path):
    rate = 16_000
    t = np.arange(int(4.0 * rate)) / rate
    audio = 0.0005 * np.random.default_rng(1).standard_normal(len(t))  # room tone
    voice = (t >= 1.00) & (t < 1.60)
    audio[voice] += 0.3 * np.sin(2 * np.pi * 180 * t[voice])
    wav = tmp_path / "a.wav"
    write_wav(wav, audio)
    # ASR-style slop: starts 0.12 s early, ends 0.30 s late.
    words = [{"w": "hello.", "s": 0.88, "e": 1.90, "p": 0.9}]
    out = analyse.refine_word_edges(words, wav)
    assert out[0]["s"] == pytest.approx(1.00, abs=0.03)
    assert out[0]["e"] == pytest.approx(1.60, abs=0.03)
    # A word already inside the voice is never extended outwards.
    tight = analyse.refine_word_edges([{"w": "x", "s": 1.2, "e": 1.4, "p": 1}], wav)
    assert (tight[0]["s"], tight[0]["e"]) == (1.2, 1.4)


# --- CP-SAT duration fit ----------------------------------------------------

def items_for(lengths: dict[str, float]) -> list[Item]:
    out, t = [], 0.0
    for seg, length in lengths.items():
        out.append(Item("c", seg, t, t + length))
        t += length + 1.0
    return out


def test_cpsat_maximises_score_inside_the_window():
    lengths = {"a": 10.0, "b": 10.0, "c": 5.0, "d": 5.0}
    scores = {"a": 0.9, "b": 0.1, "c": 0.8, "d": 0.7}
    kept, dropped, solver = fit(items_for(lengths), scores, target=20.0, tolerance=0.05)
    assert solver == "cp-sat"
    # a+c+d and a+b both run 20 s; a+c+d carries the higher score.
    assert {it.segment_id for it in kept} == {"a", "c", "d"}
    assert [d["segment"] for d in dropped] == ["b"]


def test_cpsat_respects_locked_segments():
    lengths = {"a": 10.0, "b": 10.0, "c": 10.0}
    scores = {"a": 0.9, "b": 0.1, "c": 0.8}
    kept, _, _ = fit(items_for(lengths), scores, target=20.0, tolerance=0.05, locked={"b"})
    assert "b" in {it.segment_id for it in kept}


def test_cpsat_lands_nearest_when_the_window_is_unreachable():
    lengths = {"a": 12.0, "b": 12.0, "c": 12.0}
    # 24 s misses 28 s by 4; 36 s misses by 8. Nearest wins over score.
    kept, _, _ = fit(items_for(lengths), {"a": 0.5, "b": 0.5, "c": 0.5}, target=28.0,
                     tolerance=0.05)
    assert sum(it.duration for it in kept) == pytest.approx(24.0)


def test_cpsat_is_deterministic():
    rec = make_clip_record("c_d", [(f"w{i}.", i * 1.0, i * 1.0 + 0.6) for i in range(40)], 41.0)
    idx = {"version": 1, "clips": [rec]}
    analyse.score_segments(idx["clips"])
    runs = {tuple((i.src_in, i.frames) for i in compile_rough(idx, CutOptions(target_s=20)).items)
            for _ in range(3)}
    assert len(runs) == 1


# --- real model --------------------------------------------------------------

def flite(tmp_path: Path, text: str, pause_s: float) -> Path:
    """Synthesised speech followed by a pause, as 16 kHz mono."""
    wav = tmp_path / "speech.wav"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", f"flite=text='{text}':voice=slt", "-af", f"apad=pad_dur={pause_s}",
                    "-ar", "16000", "-ac", "1", str(wav)], check=True)
    return wav


@needs_parakeet
def test_parakeet_transcribes_real_speech_with_tight_edges(tmp_path: Path):
    wav = flite(tmp_path, "Welcome back to the channel. Today we build a video editor.", 1.0)
    words, lang, used = asr.transcribe(tmp_path / "speech.mp4", wav, "parakeet")
    assert used == "parakeet" and lang == "en"
    text = " ".join(w["w"].lower().strip(".,") for w in words)
    assert "welcome back to the channel" in text and "video editor" in text
    words = analyse.refine_word_edges(words, wav)
    with wave.open(str(wav)) as w:
        a = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32)
    voiced = np.flatnonzero(np.abs(a) > 300)  # clearly audible, about -40 dBFS
    first, last = voiced[0] / 16_000, voiced[-1] / 16_000
    # What a jump cut needs: never inside the voice, and little dead air outside it.
    assert first - 0.15 <= words[0]["s"] <= first + 0.02
    assert last - 0.02 <= words[-1]["e"] <= last + 0.15

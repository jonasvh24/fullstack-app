"""Speech recognition with word timestamps.

Three backends behind one function. A sidecar transcript wins when present,
which keeps tests deterministic and lets a user correct a bad transcript by
hand without re-running a model.
"""

from __future__ import annotations

import functools
import json
import os
from pathlib import Path

# Whisper is trained on cleaned transcripts and silently drops disfluencies.
# Priming the decoder with a disfluent prompt is the known workaround; without
# it the filler rule has nothing to match [MED].
DISFLUENT_PROMPTS = {
    "en": "Umm, let me think like, hmm... Okay, here's what I'm, like, thinking.",
    "nl": "Euh, even denken, ehm... Oké, dus, wat ik eigenlijk, euh, bedoel is.",
}

BACKENDS = ("auto", "sidecar", "whisperx", "faster-whisper")


def sidecar_for(clip: Path, transcripts_dir: Path | None) -> Path | None:
    names = (f"{clip.stem}.words.json", f"{clip.name}.words.json")
    dirs = [d for d in (transcripts_dir, clip.parent) if d is not None]
    for d in dirs:
        for n in names:
            if (d / n).exists():
                return d / n
    return None


def normalise_words(raw: list[dict]) -> list[dict]:
    """Coerce any supported shape to {w, s, e, p} and fill missing times.

    WhisperX leaves numerals and symbols unaligned; dropping them would lose
    them from captions, so they inherit time from their neighbours instead.
    """
    words = []
    for r in raw:
        text = (r.get("w") or r.get("word") or r.get("text") or "").strip()
        if not text:
            continue
        s = r.get("s", r.get("start"))
        e = r.get("e", r.get("end"))
        p = r.get("p", r.get("probability", r.get("score", 1.0)))
        words.append({"w": text, "s": s, "e": e, "p": float(p if p is not None else 1.0)})

    n = len(words)
    i = 0
    while i < n:
        if words[i]["s"] is not None and words[i]["e"] is not None:
            i += 1
            continue
        j = i
        while j < n and (words[j]["s"] is None or words[j]["e"] is None):
            j += 1
        left = words[i - 1]["e"] if i > 0 else (words[j]["s"] if j < n else 0.0)
        right = words[j]["s"] if j < n else left + 0.3 * (j - i)
        step = max(right - left, 0.0) / (j - i)
        for k in range(i, j):
            words[k]["s"] = left + step * (k - i)
            words[k]["e"] = left + step * (k - i + 1)
        i = j

    for w in words:
        w["s"], w["e"] = round(float(w["s"]), 3), round(float(w["e"]), 3)
        if w["e"] < w["s"]:
            w["e"] = w["s"]
    words.sort(key=lambda w: w["s"])
    return words


def load_sidecar(path: Path) -> tuple[list[dict], str | None]:
    data = json.loads(path.read_text())
    if isinstance(data, list):
        return normalise_words(data), None
    if "words" in data:
        return normalise_words(data["words"]), data.get("language")
    raw = [w for seg in data.get("segments", []) for w in seg.get("words", [])]
    return normalise_words(raw), data.get("language")


@functools.lru_cache(maxsize=2)
def _faster_whisper_model(name: str):
    from faster_whisper import WhisperModel
    return WhisperModel(name, device="auto", compute_type="default")


def _transcribe_faster_whisper(wav: Path, language: str | None) -> tuple[list[dict], str]:
    model = _faster_whisper_model(os.environ.get("NULLCUT_ASR_MODEL", "large-v3"))
    segments, info = model.transcribe(
        str(wav), language=language, word_timestamps=True, vad_filter=True,
        condition_on_previous_text=False,
        initial_prompt=DISFLUENT_PROMPTS.get(language or "", DISFLUENT_PROMPTS["en"]),
    )
    raw = [{"w": w.word, "s": w.start, "e": w.end, "p": w.probability}
           for seg in segments for w in (seg.words or [])]
    return normalise_words(raw), info.language


def _transcribe_whisperx(wav: Path, language: str | None) -> tuple[list[dict], str]:
    import torch
    import whisperx

    device = "cuda" if torch.cuda.is_available() else "cpu"
    compute = "float16" if device == "cuda" else "int8"
    prompt = DISFLUENT_PROMPTS.get(language or "", DISFLUENT_PROMPTS["en"])
    model = whisperx.load_model(os.environ.get("NULLCUT_ASR_MODEL", "large-v3"), device,
                                compute_type=compute, language=language,
                                asr_options={"initial_prompt": prompt})
    audio = whisperx.load_audio(str(wav))
    result = model.transcribe(audio, batch_size=16, language=language)
    lang = result.get("language") or language or "en"
    align_model, meta = whisperx.load_align_model(language_code=lang, device=device)
    aligned = whisperx.align(result["segments"], align_model, meta, audio, device,
                             return_char_alignments=False)
    raw = [w for seg in aligned["segments"] for w in seg.get("words", [])]
    return normalise_words(raw), lang


def _importable(module: str) -> bool:
    try:
        __import__(module)
        return True
    except ImportError:
        return False


def transcribe(clip: Path, wav: Path, backend: str = "auto", language: str | None = None,
               transcripts_dir: Path | None = None) -> tuple[list[dict], str, str]:
    """Return (words, language, backend actually used)."""
    sidecar = sidecar_for(clip, transcripts_dir)
    if backend in ("auto", "sidecar") and sidecar is not None:
        words, lang = load_sidecar(sidecar)
        return words, lang or language or "en", f"sidecar:{sidecar.name}"
    if backend == "sidecar":
        raise SystemExit(f"{clip.name}: no sidecar transcript ({clip.stem}.words.json)")

    if backend == "whisperx" or (backend == "auto" and _importable("whisperx")):
        words, lang = _transcribe_whisperx(wav, language)
        return words, lang, "whisperx"
    if backend == "faster-whisper" or (backend == "auto" and _importable("faster_whisper")):
        words, lang = _transcribe_faster_whisper(wav, language)
        return words, lang, "faster-whisper"
    raise SystemExit(
        "no ASR backend: `pip install 'nullcut[asr]'` (faster-whisper) or "
        "'nullcut[whisperx]', or put <clip>.words.json next to each clip")

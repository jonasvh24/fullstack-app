"""Speech recognition with word timestamps.

Four backends behind one function. A sidecar transcript wins when present,
which keeps tests deterministic and lets a user correct a bad transcript by
hand without re-running a model.
"""

from __future__ import annotations

import functools
import json
import os
import re
import wave
from pathlib import Path

import numpy as np

# Whisper is trained on cleaned transcripts and silently drops disfluencies.
# Priming the decoder with a disfluent prompt is the known workaround; without
# it the filler rule has nothing to match [MED].
DISFLUENT_PROMPTS = {
    "en": "Umm, let me think like, hmm... Okay, here's what I'm, like, thinking.",
    "nl": "Euh, even denken, ehm... Oké, dus, wat ik eigenlijk, euh, bedoel is.",
}

BACKENDS = ("auto", "sidecar", "parakeet", "whisperx", "faster-whisper")

# Parakeet does not report language; the filler rule needs it, and stopword
# counts settle English versus Dutch reliably on any real sentence.
STOPWORDS = {
    "en": {"the", "and", "of", "to", "a", "in", "that", "it", "you", "this", "with", "for"},
    "nl": {"de", "het", "een", "en", "van", "dat", "ik", "je", "niet", "met", "op", "voor"},
}
CHUNK_S = 45.0
CHUNK_SEARCH_S = 8.0


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


def detect_language(words: list[dict]) -> str:
    tokens = [re.sub(r"[^\w]", "", w["w"].lower()) for w in words]
    counts = {lang: sum(t in sw for t in tokens) for lang, sw in STOPWORDS.items()}
    return max(counts, key=counts.get) if any(counts.values()) else "en"


def chunk_bounds(samples: np.ndarray, rate: int, target_s: float = CHUNK_S,
                 search_s: float = CHUNK_SEARCH_S) -> list[tuple[int, int]]:
    """Split long audio at the quietest 100 ms before each target boundary.

    The encoder's memory grows with input length, and a boundary through a
    word would split it in two; the quietest point near the boundary is
    almost always a pause between words.
    """
    n = len(samples)
    target, search, hop = int(target_s * rate), int(search_s * rate), rate // 10
    bounds, start = [], 0
    while n - start > target + search:
        lo, hi = start + target - search, start + target
        window = samples[lo:hi].astype(np.float32)
        frames = window[: (len(window) // hop) * hop].reshape(-1, hop)
        quietest = int(np.argmin((frames * frames).mean(axis=1)))
        cut = lo + quietest * hop + hop // 2
        bounds.append((start, cut))
        start = cut
    bounds.append((start, n))
    return bounds


@functools.lru_cache(maxsize=1)
def _parakeet_recognizer(model_dir: str):
    import sherpa_onnx

    d = Path(model_dir)
    return sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=str(d / "encoder.int8.onnx"), decoder=str(d / "decoder.int8.onnx"),
        joiner=str(d / "joiner.int8.onnx"), tokens=str(d / "tokens.txt"),
        num_threads=int(os.environ.get("NULLCUT_ASR_THREADS", min(8, os.cpu_count() or 1))),
        provider=os.environ.get("NULLCUT_ASR_PROVIDER", "cpu"),
        model_type="nemo_transducer", decoding_method="greedy_search",
    )


def tokens_to_words(tokens: list[str], starts: list[float], durations: list[float],
                    log_probs: list[float], offset: float) -> list[dict]:
    """Merge BPE pieces into words. A leading space or SentencePiece mark opens a word;
    punctuation joins the word it follows without stretching its end time."""
    words: list[dict] = []
    lps: list[list[float]] = []
    for i, tok in enumerate(tokens):
        piece = tok.replace("\u2581", " ")
        text = piece.strip()
        if not text:
            continue
        dur = durations[i] if i < len(durations) else 0.08
        lp = log_probs[i] if i < len(log_probs) else 0.0
        is_punct = not any(ch.isalnum() for ch in text)
        if words and (is_punct or not piece.startswith(" ")):
            words[-1]["w"] += text
            if not is_punct:
                words[-1]["e"] = offset + starts[i] + dur
                lps[-1].append(lp)
            continue
        words.append({"w": text, "s": offset + starts[i], "e": offset + starts[i] + dur})
        lps.append([lp])
    for w, lp in zip(words, lps):
        w["p"] = float(np.exp(np.mean(lp))) if lp else 1.0
    return words


def _transcribe_parakeet(wav: Path, language: str | None) -> tuple[list[dict], str]:
    from . import models

    name = models.DEFAULT_PROFILE["asr"]
    if not models.installed(name):
        raise SystemExit(f"Parakeet model not installed: run `nullcut models pull {name}`")
    rec = _parakeet_recognizer(str(models.path_of(name)))
    with wave.open(str(wav), "rb") as w:
        rate = w.getframerate()
        samples = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    audio = samples.astype(np.float32) / 32768.0

    bounds = chunk_bounds(samples, rate)
    streams = []
    for a, b in bounds:
        st = rec.create_stream()
        st.accept_waveform(rate, audio[a:b])
        streams.append(st)
    rec.decode_streams(streams)

    raw: list[dict] = []
    for (a, _), st in zip(bounds, streams):
        r = st.result
        raw.extend(tokens_to_words(list(r.tokens), list(r.timestamps),
                                   list(getattr(r, "durations", []) or []),
                                   list(getattr(r, "ys_log_probs", []) or []), a / rate))
    words = normalise_words(raw)
    return words, language or detect_language(words)


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

    if backend == "parakeet":
        words, lang = _transcribe_parakeet(wav, language)
        return words, lang, "parakeet"
    if backend == "whisperx" or (backend == "auto" and _importable("whisperx")):
        words, lang = _transcribe_whisperx(wav, language)
        return words, lang, "whisperx"
    if backend == "auto" and _parakeet_ready():
        words, lang = _transcribe_parakeet(wav, language)
        return words, lang, "parakeet"
    if backend == "faster-whisper" or (backend == "auto" and _importable("faster_whisper")):
        words, lang = _transcribe_faster_whisper(wav, language)
        return words, lang, "faster-whisper"
    raise SystemExit(
        "no ASR backend: run `nullcut models pull parakeet-tdt-0.6b-v3`, install "
        "'nullcut[whisper]' or 'nullcut[whisperx]', or put <clip>.words.json next to each clip")


def _parakeet_ready() -> bool:
    from . import models

    return _importable("sherpa_onnx") and models.installed(models.DEFAULT_PROFILE["asr"])

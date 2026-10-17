# nullcut

Phase 0 of the Local AI Video Editor build plan: the spine. Footage in, OTIO timeline, rendered video out. No UI, no LLM, no styles. If this holds on real footage, everything after it is additive.

```
nullcut ingest ~/footage/lisbon
nullcut rough --captions --target 8m
```

`rough` also takes a folder directly and ingests it on first sight, so Gate 0 is one command: `nullcut rough ~/footage/lisbon --captions --target 8m`.

## What it does

| Stage | Module | Output |
|---|---|---|
| Ingest | `analyse.py`, `media.py` | 720p proxies, 16 kHz ASR audio, 48 kHz mix PCM |
| Analyse | `asr.py`, `analyse.py` | `index/clip_index.json`: words, energy, sentence segments, highlight score |
| Compile | `compiler.py` | ordered cut list on the output frame grid, each item tagged with the rules that made it |
| Timeline | `timeline.py` | `timelines/timeline.v{n}.otio`, the only thing the renderer reads |
| Render | `render.py`, `captions.py` | `renders/rough.v{n}.{master,preview}.mp4` plus SRT and VTT |

The compiler, in order: trim to word boundaries; remove hard fillers everywhere and soft fillers only when isolated; jump-cut pauses over the threshold. When a target is set and the cut runs long, the silence threshold tightens (350 → 300 → 250 → 200 → 150 ms) first, then the lowest-scoring sentences drop. A cut that comes up short is reported in seconds, never padded.

The renderer encodes each item to its own file with identical settings, keyed by a hash of everything that affects its pixels, then joins them by stream copy. Audio is cut once from PCM at sample-exact frame boundaries, so it cannot drift from the video however many cuts there are, then two-pass loudnorm to −14 LUFS / −1 dBTP. The delivered file is re-measured with `ebur128` and the result is printed: a measurement, not loudnorm's estimate.

## Install

```
uv venv && . .venv/bin/activate
uv pip install -e ".[asr,dev]"        # faster-whisper
uv pip install -e ".[whisperx,dev]"   # or WhisperX, preferred when present
```

Needs FFmpeg on `PATH`, built with libass. NVENC is used when a test encode succeeds, libx264 otherwise; force either with `NULLCUT_ENCODER`.

## Configuration

| Variable | Default | Effect |
|---|---|---|
| `NULLCUT_HOME` | `~/nullcut` | projects live in `$NULLCUT_HOME/projects/<id>/` |
| `NULLCUT_ASR_MODEL` | `large-v3` | Whisper model name for either backend |
| `NULLCUT_ENCODER` | probed | `h264_nvenc` or `libx264` |
| `NULLCUT_RENDER_JOBS` | `min(4, cores)` | parallel item encodes |
| `NULLCUT_CAPTION_FONT` | `DejaVu Sans` | any font fontconfig can find |

Thresholds, filler lists and highlight weights are constants in `config.py`. Phase 2 replaces them with a measured Style Profile without touching the compiler.

## Sidecar transcripts

A `<clip stem>.words.json` next to a clip (or in `--transcripts DIR`) wins over ASR. It accepts nullcut's own shape (`{"language": "en", "words": [{"w", "s", "e", "p"}]}`) and WhisperX output as is. Hand-correct a bad transcript once and every later run uses it.

## Tests

```
python -m pytest -q
```

33 tests. The end-to-end tests generate footage whose audio "words" are tone bursts at known times, with matching sidecars. They check frame-grid edges, A/V drift under one frame, loudness within ±1 dB of −14 LUFS, cache reuse on an unchanged re-run, and that a caption change re-encodes only what it touched.

## Known gaps against Gate 0

- **ASR has not run in this build environment** (no model download access). Both backends are written against their documented APIs and sit behind one function; the first real run on the Sparks is the test. `[MED]`
- **Whisper suppresses disfluencies.** The backends prime the decoder with a disfluent prompt so "um" and "euh" get transcribed at all. Whether that is enough on your voice is the cheapest thing to check first: transcribe one clip and count fillers against what you hear. `[MED]`
- **Soft fillers diverge from the plan.** The plan lists "dus" as a filler. It is also a real word, so it goes only when isolated by pauses or when it opens a sentence followed by a pause.
- **The duration fit is greedy**, not CP-SAT. Fine for one talking head; Phase 1 needs CP-SAT for story order and locked beats.
- **Clips with no speech are skipped**, and reported. B-roll placement is a Phase 1 rule.

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
| Analyse | `asr.py`, `analyse.py` | `index/clip_index.json`: words with voice-snapped edges, energy, sentence segments, highlight score |
| Compile | `compiler.py` | ordered cut list on the output frame grid, each item tagged with the rules that made it |
| Timeline | `timeline.py` | `timelines/timeline.v{n}.otio`, the only thing the renderer reads |
| Render | `render.py`, `captions.py` | `renders/rough.v{n}.{master,preview}.mp4` plus SRT and VTT |

The compiler, in order: trim to word boundaries; remove hard fillers everywhere and soft fillers only when isolated; jump-cut pauses over the threshold. When a target is set and the cut runs long, the silence threshold tightens (350 → 300 → 250 → 200 → 150 ms) first, then CP-SAT drops whole sentences: maximise Σ highlight score subject to 0.95T ≤ length ≤ 1.05T, locked segments kept, story order untouched. If no selection fits the window, it lands as near T as possible. A cut that comes up short is reported in seconds, never padded.

ASR timestamps are only as fine as the encoder frame and drift at pauses, which is exactly where jump cuts land. Every word edge that borders a pause is snapped to the voice on a 10 ms energy envelope, inwards only. Sidecar transcripts are trusted as-is.

The renderer encodes each item to its own file with identical settings, keyed by a hash of everything that affects its pixels, then joins them by stream copy. Audio is cut once from PCM at sample-exact frame boundaries, so it cannot drift from the video however many cuts there are, then two-pass loudnorm to −14 LUFS / −1 dBTP. The delivered file is re-measured with `ebur128` and the result is printed: a measurement, not loudnorm's estimate.

## Install

```
uv venv && . .venv/bin/activate
uv pip install -e ".[dev]"
nullcut models pull parakeet-tdt-0.6b-v3    # 487 MB, from the sherpa-onnx GitHub release
```

Needs FFmpeg on `PATH`, built with libass. NVENC is used when a test encode succeeds, libx264 otherwise; force either with `NULLCUT_ENCODER`. Wheels exist for x86_64 and aarch64, so the same install runs on the Sparks.

## ASR

| Backend | Selected by `--asr auto` when | Licence | Notes |
|---|---|---|---|
| sidecar | `<clip>.words.json` exists | n/a | hand-corrected truth, never refined |
| `whisperx` | `nullcut[whisperx]` installed | MIT / BSD | the plan's pick; forced alignment; weights from Hugging Face |
| `parakeet` | model pulled | CC-BY-4.0 | default profile; 25 European languages incl. en, nl; ~14× real time on 4 CPU cores |
| `faster-whisper` | `nullcut[whisper]` installed | MIT | weights from Hugging Face |

`nullcut models` lists every registered weight with its licence. The default profile is tested to contain commercially licensed weights only (plan §14).

## Configuration

| Variable | Default | Effect |
|---|---|---|
| `NULLCUT_HOME` | `~/nullcut` | projects live in `$NULLCUT_HOME/projects/<id>/` |
| `NULLCUT_MODELS` | `$NULLCUT_HOME/models` | where pulled weights live |
| `NULLCUT_ASR_MODEL` | `large-v3` | Whisper model name for either Whisper backend |
| `NULLCUT_ASR_THREADS` | `min(8, cores)` | Parakeet CPU threads |
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

44 tests; the real-model test skips when Parakeet has not been pulled. End-to-end tests run on generated footage whose audio "words" are tone bursts at known times. They check frame-grid edges, A/V drift under one frame, loudness within ±1 dB of −14 LUFS, cache reuse, and that a caption change re-encodes only what it touched. The real-model test synthesises speech and checks transcript and edge accuracy.

Verified on synthesised speech (flite) with Parakeet, no sidecars: every word correct, pause-adjacent edges within ~20 ms of ground truth, every jump cut 55 dB or more below speech level, 0 ms A/V drift, −13.9 LUFS.

## Known gaps

- **Not yet run on a human voice.** Flite is intelligible but robotic. One real talking-head clip is the test that matters. `[MED]`
- **Filler capture.** Parakeet transcribed an isolated synthetic "Um" but turned one mid-sentence into "and". Whisper-family models do the same; the Whisper backends are primed with a disfluent prompt. Fillers the ASR drops still show up as pauses and fall to the jump-cut rule. Cheapest check: one real clip, count fillers heard versus removed. `[MED]`
- **Whisper backends have not run here.** Hugging Face is blocked in this build environment; both are written against their documented APIs. Parakeet is the verified default. `[MED]`
- **NVENC untested.** No GPU here; selection is by a real test encode, so a broken NVENC falls back to libx264 rather than failing. `[HIGH]`
- **ASR spelling is American** ("Analyze") in captions. A British spelling pass belongs in Phase 1 with the caption styles.
- **Soft fillers diverge from the plan.** "dus" goes only when isolated by pauses or opening a sentence before a pause; it is also a real word.
- **Clips with no speech are skipped**, and reported. B-roll placement is a Phase 1 rule.

# nullcut

Local AI video editor, Phases 0 to 2 of the build plan. Three engines with a timeline file between them: **analyse** the footage once, **plan** the edit against a brief and a style, **compile and render** deterministically. The AI never touches pixels; it writes an Edit Plan, and plain code turns that into an OTIO timeline that FFmpeg executes.

```
nullcut models pull defaults            # Parakeet ASR + YuNet faces, commercial licences
nullcut serve                           # UI and API on http://127.0.0.1:8765
```

Or from the command line, the same seven steps (plan §2):

```
nullcut ingest ~/Footage/lisbon
nullcut brief --text "fast travel vlog, open on the cliff jump" --target 4:00 --style cinematic-vlog --music energetic
nullcut plan                            # may ask up to three questions; answer with --answer "..."
nullcut preview                         # 720p from proxies, with Gate 1 QC and style match
nullcut master                          # source resolution, titles, thumbnails, chapters, OTIO, FCPXML
```

## Style engine (Phase 2)

Style is a parameter vector, not a vibe (plan §5). `measure.py` reads every parameter from a finished video, and the same code runs on references and on nullcut's own renders. That closed loop is the point.

| Parameter | Measured by |
|---|---|
| Pacing | hard cuts, including jump cuts: a per-cell discontinuity on a 4x4 grid, so a caption swap cannot fake one and a zoom (explained by a similarity transform) is not counted |
| Silence | the pauses the transcript shows survived the edit |
| Zooms | frame-to-frame scale from tracked features: eased zooms and punch-in cuts alike |
| Max static | longest stretches between cuts and zooms |
| Captions | OCR (PP-OCRv4, bundled): words per block, position, case, highlight colour (a hue inside the text the ring around it lacks) |
| B-roll | share of speech time with no face on screen |
| Music | level in long pauses (the bed) and in short ones (ducked under speech), tempo from below 150 Hz, cuts on the beat |
| SFX | sustained high-band sweeps at cuts, not fricatives |
| Colour | saturation, contrast, warmth, lift; an extracted look is fitted per project as a LUT |

**Extract** 3 to 10 reference videos into a profile (`Styles` screen, or `POST /api/styles/extract`). What a finished video cannot show (beat template, hook cap, zoom easing) comes from the nearest shipped style, and the profile says which.

**Match** runs on every render. Each parameter gets a score; anything under 80 % names the compiler rule that missed. **Try styles** renders the first 60 s of the current edit in two to four styles and measures each, so the creator picks by watching.

**Six shipped styles:** Explainer, Retention max, Cinematic vlog, Podcast clip, Documentary, Tutorial. Tutorial's zooms follow the region of the screen that changes when there is no face.

**9:16:** a full-height window follows the subject (faces, else motion) with a dead zone and eased motion; captions sit above the platform's own UI. Brief format `16:9`, `9:16` or `both`.

**Thumbnails** (plan §9): frames mined from clean source footage (never the captioned master), scored for face size, expression, sharpness, stillness and nearness to the title's moment; IS-Net cutout (GrabCut fallback); three shipped profiles (reaction face, object hero with arrow, before/after split) or one extracted from 20 to 50 reference thumbnails. Every candidate is checked at 240 px wide: text contrast 4.5:1 or better, a readable face, nothing under the timestamp corner. A failed check means recompose, never ship.

**Titles** follow the pattern of the creator's own reference titles when given (length, numbers, questions, case).

## Gate 2

`scripts/gate2.py` closes the loop. Three creator styles with hidden parameters render three reference videos each; extraction sees only the videos; each learned profile is applied to different footage (another face, other words) and the render is measured.

| Hidden style | Match | Recovered from 3 references |
|---|---|---|
| fast-talker | **92 %** | zooms 12 → 11.1/min, word captions, upper case, highlight #00E5FF → #00E0F5, SFX on 50 → 57 % of cuts |
| calm-teacher | **84 %** | zooms 2 → 2.7/min, clean captions, sentence case, music −26 → −27.6 dB under speech |
| music-montage | **84 %** | no captions, B-roll 45 → 47 %, music −14 → −16.8 dB under speech |

Gate 2 passes: 80 % or better on all three. Through the API, the learned fast-talker profile mastered at 91 % in both 16:9 and 9:16, with three thumbnails passing every check and titles in the reference pattern.

What it did not recover, and why:

- **Silence threshold** reads 240 ms whatever the hidden value (150, 300 or 420 ms). Synthesised speech has few natural pauses in the band that separates those styles. `[MED]`
- **Beat-snapped cuts** were not recovered for music-montage: cuts on the beat stayed under the 40 % that turns snapping on. `[MED]`
- **Red highlight over red skin** read as white. The hue-ring test needs the highlight to differ from what surrounds the text. `[MED]`
- **SFX on calm-teacher and music-montage** scored 0: both read 25–29 % SFX against a true 0 %. Fricatives at B-roll edges mid-sentence survive the speech margin because ASR trims word ends. `[LOW]` Cheapest test: ten real creator videos with known SFX use, scored by hand against the measure.

References made by nullcut itself are friendlier than real creators' videos. This proves the loop and the measurements, not taste.

## Pipeline

| Step | Module | Writes |
|---|---|---|
| Ingest | `analyse.py`, `media.py` | proxies, ASR audio, mix PCM |
| Analyse | `asr.py`, `vision.py` | `index/clip_index.json`: words, shots, keyframes, faces at 5 fps, motion, sharpness, exposure, scene captions (with a VLM), highlight scores |
| Plan | `plan.py`, `solver.py` | `plans/edit_plan.v{n}.json`: beats, intent, sentences, emphasis words, music energy |
| Compile | `composer.py`, `compiler.py` | `timelines/timeline.v{n}.otio`: V1 cuts, V2 B-roll, A2 music, beat markers, zooms, SFX |
| Render | `render.py`, `music.py`, `fx.py`, `captions.py` | `renders/edit.v{n}.{preview,master}.mp4` + SRT/VTT |
| Package | `package.py`, `qc.py` | `package/`: titles, description, chapters, OTIO, FCPXML |

**Planner.** An LLM agent on any OpenAI-compatible endpoint (the Council seat by default) reads a contact sheet, never footage, and may call `search_clips`, `get_segment`, `get_transcript` and `ask_user` (three questions per project at most). Output is schema-constrained JSON, validated (unknown ids, reused sentences, broken locks) with one repair round. With no LLM, or one that fails, a deterministic planner produces the same shape. Locked beats survive every re-plan.

**Solver.** CP-SAT over sentences: maximise highlight score inside the ±5 % window, each beat near its style share, hook under the style's cap, no beat emptied, locked beats kept. Story order is never the solver's to change.

**Compiler rules, in order.** Trim to word boundaries and remove silences and fillers; snap cuts to music beats within ±120 ms (music-led styles); enforce max static seconds with a zoom on an emphasis word, else B-roll, else a zoom; eased zooms centred on the tracked face; word captions per profile with emphasis highlighted; B-roll over jump cuts first, dialogue continuing under it; music bed fitted to end on its final downbeat, ducked under speech, shaped by beat energy; SFX on the profile's share of cuts and punches. Every element records the rule that placed it.

**Renderer.** Zooms are sub-pixel crop-and-scale through FFmpeg's `perspective` filter, not `zoompan`. Each item renders to its own cached file and joins by stream copy, so new music re-renders audio only. Audio is cut from PCM at sample-exact frame boundaries, so A/V drift is zero by construction, then two-pass loudnorm to −14 LUFS / −1 dBTP, re-measured on the delivered file.

## Styles

| | Explainer | Retention max | Cinematic vlog |
|---|---|---|---|
| Silence cut | 300 ms | 150 ms | 400 ms |
| Max static | 7 s | 3 s | 9 s |
| Zooms | 3/min, 1.06–1.12× | 10/min, 1.10–1.25× | slow push-ins |
| Captions | clean, 6 words | word-by-word, upper | clean, 5 words |
| B-roll density | 15 % | 25 % | 45 % |
| Music | −22 dB under speech | −20 dB, SFX on cuts | music-led, beat-snapped cuts |
| Grade | neutral | +15 % saturation | warm |

Profiles are JSON in `src/nullcut/styles/`; the brief's zoom and B-roll sliders scale them.

## Install

```
uv venv && . .venv/bin/activate && uv pip install -e ".[dev]"
nullcut models pull defaults
nullcut music import ~/Music/licensed     # optional: tempo, beat grid, energy, licence per track
```

FFmpeg on `PATH` with libass. NVENC is used when a real test encode succeeds, libx264 otherwise. Wheels exist for x86_64 and aarch64.

**Docker:** `docker compose up -d` builds one image for API, UI and render workers, with GPU and NVENC passed through. The LLM and VLM stay outside: on the Sparks they are the Council's existing endpoints.

## Configuration

| Variable | Default | Effect |
|---|---|---|
| `NULLCUT_HOME` | `~/nullcut` | projects, models, music library, job database |
| `NULLCUT_LLM_URL`, `_MODEL`, `_KEY` | `http://127.0.0.1:8000/v1`, `glm-4.5-air` | planner and titles; `NULLCUT_LLM=off` forces the deterministic planner |
| `NULLCUT_CLOUD_PLANNER=1` + `NULLCUT_CLOUD_LLM_*` | off | route planning text, never footage, to a frontier model |
| `NULLCUT_VLM_URL`, `_MODEL` | unset | scene captions per keyframe (Molmo2 or Qwen3-VL via vLLM) |
| `NULLCUT_MODELS` | `$NULLCUT_HOME/models` | pulled weights |
| `NULLCUT_ENCODER` | probed | `h264_nvenc` or `libx264` |
| `NULLCUT_RENDER_JOBS` | `min(4, cores)` | parallel item encodes |

## UI

React + TypeScript + Vite in `web/`, built into `src/nullcut/web_dist/` and served by the API. Steps: Clips (ingest progress), Brief, Storyboard (drag to reorder, lock, delete, remove sentences, answer questions), Preview (player, canvas timeline with beats, B-roll, zooms and music, hover for rules, QC), Deliver (master, titles, description, chapters, downloads). API contract: `docs/api.md`.

## Tests

`python -m pytest -q`: 87 tests, including the style engine (measurement, extraction, match) and the Phase 2 API: extract, compare, try, both formats, thumbnails. The LLM planner protocol runs against a scripted OpenAI-compatible server: tool calls, schema request, repair, questions, fallback. The zoom renderer is checked against a reference crop. The HTTP API is driven end to end, from folder to package.

## Verified

On synthesised speech (flite) over a real face photo, Parakeet ASR, no sidecars: Gate 1 holds. Length is within ±5 %, every cut sits 54 dB or more under speech, and loudness lands at −14.0 LUFS. Zooms centre on the detected face, B-roll covers jump cuts, beat-snapped cuts land within 6 ms of the beat, and titles that the transcript does not support are dropped. The full flow was driven through the real UI in headless Chromium and inside the Docker image.

## Known gaps

- **No LLM has planned an edit yet.** No model endpoint is reachable from the build environment; the protocol is tested against a scripted server. The first real run against the Council is the test. `[MED]`
- **No human voice or real footage yet.** Everything above ran on synthesised speech and test patterns. One real 10-minute talking head through `nullcut serve` settles most of what remains. `[MED]`
- **No VLM or embeddings.** Scene captions need `NULLCUT_VLM_URL`; B-roll matching falls back to shot quality when there are none. SigLIP embeddings for `search_clips` are deferred; search is lexical over transcripts and captions.
- **Expression intensity is not measured.** YuNet gives boxes, not blendshapes; zoom targets and thumbnail scoring use box size, sharpness and stillness. MediaPipe blendshapes remain the upgrade.
- **No real creator references yet.** Gate 2 ran on nullcut's own renders. Three real channels, three videos each, is the next test of extraction. `[MED]`
- **Colour looks beyond the transfer clamps** (heavy teal-orange, crushed blacks) are approximated, not matched; the clamps keep skin tones safe.
- **Music analysis is librosa's.** It read a 120 bpm test track as 117.5 bpm; snapping absorbs that, but a real library needs checking. Vocal rejection (Demucs) is not built.
- **Graphics are not built.** Rule 3's third fallback ("else a graphic") falls back to a zoom.
- **SFX are synthesised** (whoosh, pop) until a curated pack is chosen.
- **NVENC is untested.** There is no GPU in the build environment.

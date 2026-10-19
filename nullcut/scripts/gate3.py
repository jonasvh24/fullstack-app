"""Gate 3: chat refinement on a 10-minute edit, and exports Resolve can import.

Builds about 12 minutes of synthesised talking head (flite speech over a face photo,
plus B-roll and a music bed), plans a 10-minute edit, renders the preview, then
times a sequence of chat instructions from submission to playable preview, with how
many items each one re-encoded. Finally renders the master and checks the FCPXML:
structure, read-back through the independent OTIO reader, and (when a DTD is given)
validation against Apple's FCPXML 1.8 DTD.

Gate 3 passes when every parameter tweak reaches its preview in under 30 s and the
FCPXML passes every check. The 30 s target is for the reference build (DGX Spark,
NVENC); this harness reports what the machine it runs on achieves.

    NULLCUT_MODELS=... python scripts/gate3.py WORKDIR FACE.jpg [FCPXMLv1_8.dtd]
"""

from __future__ import annotations

import json
import os
import random
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

TOPICS = [
    ("the lake", "We drove out to the lake before sunrise"),
    ("pricing", "The pricing question comes up in every comment"),
    ("the camera", "This camera cost less than a phone"),
    ("editing", "Editing used to take me a full weekend"),
    ("the microphone", "The microphone matters more than the lens"),
    ("lighting", "One window gives better light than three lamps"),
    ("the script", "I never write a full script any more"),
    ("thumbnails", "Thumbnails decide whether anyone clicks at all"),
    ("music", "Music sets the pace before a single word lands"),
    ("the budget", "The whole budget for this video was forty euros"),
    ("travel", "Travel days are where the best stories happen"),
    ("the drone", "The drone stayed in the bag for the whole trip"),
]
FILLERS = ["Um.", "So.", "Right."]
CLAIMS = [
    "That changed how I plan every shoot.", "Most people get this part wrong.",
    "It took three attempts to get right.", "Here is the number that surprised me.",
    "The difference is easy to hear.", "Nobody tells you this when you start.",
    "It sounds simple, and it is.", "I tested it twice to be sure.",
    "That is the whole trick.", "You can see the result straight away.",
    "This is the part I would change first.", "It works on any laptop.",
]


def script(n_sentences: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    out = []
    for i in range(n_sentences):
        topic, lead = TOPICS[(i // 6) % len(TOPICS)]
        r = rng.random()
        if i % 6 == 0:
            out.append(lead + ".")
        elif r < 0.08:
            out.append(rng.choice(FILLERS))
        elif r < 0.55:
            out.append(rng.choice(CLAIMS))
        else:
            out.append(f"When it comes to {topic}, {rng.choice(CLAIMS)[0].lower()}"
                       f"{rng.choice(CLAIMS)[1:]}")
    return out


def footage(folder: Path, face: Path) -> Path:
    import gate2

    if (folder / "done").exists():
        return folder
    folder.mkdir(parents=True, exist_ok=True)
    rng = random.Random(3)
    lines = script(216, 1)
    for k in range(6):
        chunk = lines[k * 36:(k + 1) * 36]
        pauses = [rng.choice([0.15, 0.25, 0.35, 0.5, 0.7, 1.1]) for _ in chunk]
        gate2.speech_clip(folder / f"{k * 2 + 1:02d}_talk.mp4", face, chunk, pauses)
        if k in (1, 4):
            gate2.broll(folder / f"{k * 2 + 2:02d}_broll.mp4", k)
    (folder / "done").write_text("ok")
    return folder


def main(work: Path, face: Path, dtd: Path | None) -> int:
    os.environ["NULLCUT_HOME"] = str(work / "home")
    os.environ["NULLCUT_LLM"] = "off"
    from nullcut import export, music, pipeline, plan, refine
    from nullcut.project import open_or_create

    work.mkdir(parents=True, exist_ok=True)
    lib = work / "musiclib"
    if not lib.exists():
        from tests.conftest import make_music
        make_music(lib / "pulse 120.wav", 120.0, 60.0)
        (lib / "LICENCE.txt").write_text("Synthesised test track, CC0\n")
    music.import_folder(lib, log=lambda m: None)

    t = time.perf_counter()
    src = footage(work / "footage", face)
    report: dict = {"footage_s": round(time.perf_counter() - t)}
    project = open_or_create(work / "home", src, "gate3")
    t = time.perf_counter()
    if not project.clip_index_path.exists():
        pipeline.ingest(project, src)
    report["ingest_s"] = round(time.perf_counter() - t, 1)
    plan.save_brief(project, {"style": "retention-max", "target_s": 600, "format": "16:9",
                              "captions": {"mode": "word"}, "overrides": {},
                              "music": {"mode": "library", "mood": "", "energy": "high",
                                        "track_id": None}})
    t = time.perf_counter()
    doc = pipeline.make_plan(project)
    report["plan_s"] = round(time.perf_counter() - t, 1)
    report["plan_kept_s"] = doc["solver"]["kept_s"]
    t = time.perf_counter()
    rec = pipeline.render_edit(project, "preview", measure_now=False)
    report["preview_cold_s"] = round(time.perf_counter() - t, 1)
    report["edit_s"] = rec["duration_s"]
    report["items"] = rec["encoded"] + rec["cached"]
    print(json.dumps(report), flush=True)

    steps = [
        "less zoom", "music quieter", "no sound effects", "warmer", "bigger captions",
        "punchier", "drop the part about the drone", "bring back the line about the budget",
        "open on the part about thumbnails instead", "undo", "make it 9:00",
        "generate the music",
    ]
    rows = []
    for text in steps:
        try:
            out = pipeline.refine_edit(project, text, use_llm=False)
        except refine.RefineError as exc:
            rows.append({"instruction": text, "error": str(exc)})
            print(json.dumps(rows[-1]), flush=True)
            continue
        st = out["step"]
        row = {"instruction": text, "kind": st["kind"], "summary": st["summary"][:90],
               "to_preview_s": st["timing"]["to_preview_s"],
               "encoded": st["render"]["encoded"], "items": st["render"]["items"],
               "audio_remixed": st["render"]["audio_remixed"],
               "style_overall": out["render"]["qc"]["style_overall"]}
        rows.append(row)
        print(json.dumps(row), flush=True)

    t = time.perf_counter()
    m = pipeline.master(project)
    report["master_s"] = round(time.perf_counter() - t, 1)
    pkg = m["package"]
    checks = pkg["export_checks"]["fcpxml"]
    fcp = project.path(pkg["files"]["fcpxml"])
    if dtd and dtd.exists():
        from lxml import etree
        d = etree.DTD(str(dtd))
        valid = d.validate(etree.parse(str(fcp)))
        checks["dtd_valid"] = bool(valid)
        checks["dtd_errors"] = [e.message for e in list(d.error_log)[:5]]
    report["fcpxml"] = {k: checks[k] for k in ("ok", "errors", "warnings", "spine_clips",
                                                "connected_clips", "roundtrip")
                        if k in checks}
    report["fcpxml"].update({k: checks[k] for k in ("dtd_valid", "dtd_errors") if k in checks})
    report["recheck"] = export.check_fcpxml(fcp, project.path(
        "timelines", f"timeline.v{pkg['version']}.otio"))["ok"]

    tweaks = [r for r in rows if r.get("kind") == "tweak"]
    report["tweak_max_s"] = max((r["to_preview_s"] for r in tweaks), default=None)
    report["steps"] = rows
    passed = (all("error" not in r for r in rows)
              and report["tweak_max_s"] is not None and report["tweak_max_s"] < 30
              and checks["ok"] and checks.get("dtd_valid", True))
    report["passed"] = passed
    (work / "gate3.json").write_text(json.dumps(report, indent=1))
    print("GATE 3", "PASS" if passed else "FAIL", f"slowest tweak {report['tweak_max_s']} s,",
          f"FCPXML ok={checks['ok']} dtd={checks.get('dtd_valid')}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]), Path(sys.argv[2]),
                  Path(sys.argv[3]) if len(sys.argv) > 3 else None))

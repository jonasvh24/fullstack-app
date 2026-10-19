"""Gate 2, closed loop: can nullcut learn a style from references and hit it on new footage?

Three "creator" styles with hidden parameters render three reference videos
each (synthesised speech over a face photo, B-roll, music). Extraction sees
only those videos. Each extracted profile is then applied to different
footage (another face, other words) and the render is measured against it.
Gate 2 passes when every style matches at 80 % or better.

References made by nullcut itself are friendlier than real creators' videos;
this proves the loop and the measurements, not taste.

    NULLCUT_MODELS=... python scripts/gate2.py WORKDIR FACE_REF.jpg FACE_TARGET.jpg
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from copy import deepcopy
from pathlib import Path

SENTENCES = [
    "Welcome back to the channel.", "Today we test a new idea.", "Um.",
    "This one surprised me.", "The setup takes five minutes.", "Then everything changes.",
    "Here is the part nobody tells you.", "It works on any laptop.",
    "So, let us look at the numbers.", "The result was twice as fast.",
    "That is the whole trick.", "Thanks for watching.",
]
# Pauses spanning every style's threshold, so the threshold is measurable at all.
PAUSES = [0.18, 0.32, 0.9, 0.22, 0.45, 1.6, 0.28, 0.6, 0.2, 1.1, 0.38, 0.5]

HIDDEN = {
    "fast-talker": {"pacing": {"silence_ms": 150, "hook_silence_ms": 120, "max_static_s": 2.5,
                               "cuts_per_min": 30},
                    "zoom": {"per_minute": 12, "scale_min": 1.12, "scale_max": 1.2},
                    "captions": {"mode": "word", "max_words": 3, "case": "upper",
                                 "highlight": "#00E5FF", "emphasis": True},
                    "broll": {"density": 0.2}, "sfx": {"cut_share": 0.5, "zoom_share": 0.5},
                    "music": {"presence": True, "under_speech_db": -18, "gap_db": -8},
                    "colour": {"saturation": 1.2, "contrast": 1.08, "warmth": 0.0, "lift": 0.0}},
    "calm-teacher": {"pacing": {"silence_ms": 420, "hook_silence_ms": 300, "max_static_s": 8.0,
                                "cuts_per_min": 8},
                     "zoom": {"per_minute": 2, "scale_min": 1.05, "scale_max": 1.08},
                     "captions": {"mode": "clean", "max_words": 6, "case": "sentence",
                                  "highlight": "#FF5A5A", "emphasis": True},
                     "broll": {"density": 0.3}, "sfx": {"cut_share": 0.0, "zoom_share": 0.0},
                     "music": {"presence": True, "under_speech_db": -26, "gap_db": -14},
                     "colour": {"saturation": 0.9, "contrast": 1.0, "warmth": 0.06,
                                "lift": 0.02}},
    "music-montage": {"pacing": {"silence_ms": 300, "hook_silence_ms": 250, "max_static_s": 6.0,
                                 "cuts_per_min": 14},
                      "zoom": {"per_minute": 0, "scale_min": 1.03, "scale_max": 1.05,
                               "drift": False},
                      "captions": {"mode": "off"},
                      "broll": {"density": 0.45},
                      "sfx": {"cut_share": 0.0, "zoom_share": 0.0},
                      "music": {"presence": True, "under_speech_db": -14, "gap_db": -6,
                                "beat_snap": True},
                      "colour": {"saturation": 1.05, "contrast": 1.12, "warmth": -0.04,
                                 "lift": 0.0}},
}


def sh(*cmd: str) -> None:
    subprocess.run(cmd, check=True)


def speech_clip(out: Path, image: Path, sentences: list[str], pauses: list[float]) -> None:
    tmp = out.with_suffix("")
    tmp.mkdir(parents=True, exist_ok=True)
    parts = []
    for n, (text, pause) in enumerate(zip(sentences, pauses)):
        u, s = tmp / f"u{n}.wav", tmp / f"s{n}.wav"
        sh("ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
           f"flite=text='{text.replace(chr(39), '')}':voice=slt", "-ar", "48000", "-ac", "1",
           str(u))
        sh("ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono",
           "-t", str(pause), str(s))
        parts += [u, s]
    lst = tmp / "list.txt"
    lst.write_text("".join(f"file '{p}'\n" for p in parts))
    wav = tmp / "all.wav"
    sh("ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), str(wav))
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                "-of", "csv=p=0", str(wav)], capture_output=True,
                               text=True).stdout)
    sh("ffmpeg", "-v", "error", "-y", "-loop", "1", "-i", str(image), "-i", str(wav),
       # A speaker moves: sway and bob, so a jump cut shifts the picture as it does on a
       # real talking head. A still photo with a slow pan hides every cut.
       "-filter_complex", "[0]scale=1500:-2,"
                          "crop=1280:720:'(iw-1280)/2+70*sin(t*1.3)+35*sin(t*0.7)':"
                          "'(ih-720)/2+25*sin(t*1.1)',fps=25,format=yuv420p[v]",
       "-map", "[v]", "-map", "1:a", "-t", f"{dur}", "-c:v", "libx264", "-preset", "veryfast",
       "-c:a", "aac", str(out))
    shutil.rmtree(tmp)


def broll(out: Path, seed: int) -> None:
    srcs = ["mandelbrot", "testsrc2", "smptehdbars", "cellauto", "life"]
    picks = [srcs[(seed + k) % len(srcs)] for k in range(4)]
    inputs, chains = [], []
    for k, src in enumerate(picks):
        inputs += ["-f", "lavfi", "-i", f"{src}=s=1280x720:r=25"]
        chains.append(f"[{k}]trim=0:3,setpts=PTS-STARTPTS,format=yuv420p,scale=1280:720[x{k}]")
    graph = ";".join(chains) + ";" + "".join(f"[x{k}]" for k in range(4)) + "concat=n=4:v=1[v]"
    sh("ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", graph, "-map", "[v]",
       "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", str(out))


def footage(folder: Path, image: Path, seed: int) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    k = len(SENTENCES)
    rot = [SENTENCES[(i + seed * 5) % k] for i in range(k)]
    pz = [PAUSES[(i + seed * 3) % k] for i in range(k)]
    speech_clip(folder / "01_talk.mp4", image, rot[:6], pz[:6])
    broll(folder / "02_broll.mp4", seed)
    speech_clip(folder / "03_talk.mp4", image, rot[6:], pz[6:])
    return folder


def hidden_style(name: str, base: dict) -> dict:
    s = deepcopy(base)
    for k, v in HIDDEN[name].items():
        s[k] = {**s[k], **v} if isinstance(s.get(k), dict) else v
    s.update({"id": name, "name": name, "description": "hidden test style"})
    return s


def edit(home: Path, folder: Path, style: str, captions: str) -> Path:
    from nullcut import pipeline, plan
    from nullcut.project import open_or_create

    project = open_or_create(home, folder, f"{folder.name}-{style}")
    if not project.clip_index_path.exists():
        pipeline.ingest(project, folder)
    plan.save_brief(project, {"style": style, "target_s": None, "captions": {"mode": captions},
                              "music": {"mode": "library", "mood": "", "energy": "high",
                                        "track_id": None}})
    pipeline.make_plan(project)
    rec = pipeline.render_edit(project, "preview")
    return project.path(rec["path"]), rec


def main(work: Path, face_ref: Path, face_target: Path) -> int:
    os.environ["NULLCUT_HOME"] = str(work / "home")
    os.environ["NULLCUT_LLM"] = "off"
    from nullcut import music, style_engine, styles

    work.mkdir(parents=True, exist_ok=True)
    lib = work / "musiclib"
    if not lib.exists():
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from tests.conftest import make_music
        make_music(lib / "pulse 120.wav", 120.0, 60.0)
        (lib / "LICENCE.txt").write_text("Synthesised test track, CC0\n")
    music.import_folder(lib, log=lambda m: None)

    base = styles.get("explainer")
    styles.user_dir().mkdir(parents=True, exist_ok=True)
    results = {}
    for name in HIDDEN:
        t0 = time.time()
        hidden = hidden_style(name, base)
        (styles.user_dir() / f"{name}.json").write_text(json.dumps(hidden))
        refs = work / "refs" / name
        refs.mkdir(parents=True, exist_ok=True)
        cap = hidden["captions"]["mode"]
        for seed in range(3):
            src = footage(work / "footage" / f"ref{seed}", face_ref, seed)
            mp4, _ = edit(work / "home", src, name, cap)
            shutil.copyfile(mp4, refs / f"{name}-{seed}.mp4")
        # The extractor must not see the hidden profile.
        (styles.user_dir() / f"{name}.json").unlink()
        profile = style_engine.extract(f"learned {name}", refs, log=lambda m: None)
        target = footage(work / "footage" / "target", face_target, 7)
        _, rec = edit(work / "home", target, profile["id"], profile["captions"]["mode"])
        results[name] = {"overall": rec["qc"]["style_overall"],
                         "detail": {k: v["score"] for k, v in rec["qc"]["style_detail"].items()},
                         "notes": {k: v["note"] for k, v in rec["qc"]["style_detail"].items()
                                   if v["note"]},
                         "recovered": {
                             "silence_ms": (hidden["pacing"]["silence_ms"],
                                            profile["pacing"]["silence_ms"]),
                             "zoom_per_min": (hidden["zoom"]["per_minute"],
                                              profile["zoom"]["per_minute"]),
                             "captions": (hidden["captions"].get("mode"),
                                          profile["captions"]["mode"]),
                             "case": (hidden["captions"].get("case"), profile["captions"]["case"]),
                             "highlight": (hidden["captions"].get("highlight"),
                                           profile["captions"].get("highlight")),
                             "broll": (hidden["broll"]["density"], profile["broll"]["density"]),
                             "sfx": (hidden["sfx"]["cut_share"], profile["sfx"]["cut_share"]),
                             "under_db": (hidden["music"]["under_speech_db"],
                                          profile["music"]["under_speech_db"]),
                             "beat_snap": (hidden["music"].get("beat_snap", False),
                                           profile["music"]["beat_snap"]),
                         },
                         "seconds": round(time.time() - t0)}
        print(json.dumps({name: results[name]}, indent=1), flush=True)
    passed = all(r["overall"] >= 0.8 for r in results.values())
    (work / "gate2.json").write_text(json.dumps(results, indent=1))
    print("GATE 2", "PASS" if passed else "FAIL",
          {k: r["overall"] for k, r in results.items()})
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])))

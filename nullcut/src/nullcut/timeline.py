"""OTIO timeline: the contract between compiler and renderer (plan §3).

Standard OTIO fields carry what Resolve and Premiere need to conform the cut.
Everything nullcut needs to re-render exactly (sample-accurate edges, the
words under each item, the rules that made it) rides in metadata, so the
renderer reads only this file and never the compiler's in-memory state.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import opentimelineio as otio

from .compiler import Item
from .project import Project

SCHEMA = 1


def _ref(clip: dict, fps: float) -> otio.schema.ExternalReference:
    available = otio.opentime.TimeRange(
        otio.opentime.RationalTime(0, fps),
        otio.opentime.RationalTime(round(clip["duration"] * fps), fps),
    )
    return otio.schema.ExternalReference(target_url=Path(clip["path"]).as_uri(),
                                         available_range=available)


def build(items: list[Item], index: dict, meta: dict) -> otio.schema.Timeline:
    fps = meta["fps"]
    clips = {c["id"]: c for c in index["clips"]}
    tl = otio.schema.Timeline(name=meta.get("name", "nullcut rough"))
    tl.global_start_time = otio.opentime.RationalTime(0, fps)
    v1 = otio.schema.Track(name="V1", kind=otio.schema.TrackKind.Video)
    a1 = otio.schema.Track(name="A1", kind=otio.schema.TrackKind.Audio)

    for n, it in enumerate(items):
        clip = clips[it.clip_id]
        # Edges sit on the output frame grid already, so frame values are exact.
        rng = otio.opentime.TimeRange(
            otio.opentime.RationalTime(round(it.src_in * fps), fps),
            otio.opentime.RationalTime(it.frames, fps),
        )
        name = f"{clip['name']} [{n:03d}]"
        v = otio.schema.Clip(name=name, media_reference=_ref(clip, fps), source_range=rng)
        v.metadata["nullcut"] = {
            "clip_id": it.clip_id, "segment_id": it.segment_id,
            "src_in": it.src_in, "src_out": it.src_out, "frames": it.frames,
            "rules": it.rules, "words": it.words, "beat_id": it.beat_id,
        }
        a = otio.schema.Clip(name=name, media_reference=_ref(clip, fps), source_range=rng)
        a.metadata["nullcut"] = {"clip_id": it.clip_id, "segment_id": it.segment_id}
        v1.append(v)
        a1.append(a)

    # Beats as markers: the storyboard's structure survives into Resolve and Premiere.
    for b in meta.get("beats", []):
        v1.markers.append(otio.schema.Marker(
            name=f"{b['role']}: {b['title']}", color=otio.schema.MarkerColor.BLUE,
            marked_range=otio.opentime.TimeRange(
                otio.opentime.RationalTime(round(b["start_s"] * fps), fps),
                otio.opentime.RationalTime(max(1, round((b["end_s"] - b["start_s"]) * fps)), fps))))

    tl.tracks.append(v1)
    if meta.get("broll"):
        v2 = otio.schema.Track(name="V2 B-roll", kind=otio.schema.TrackKind.Video)
        cursor = 0
        for b in meta["broll"]:
            start, end = round(b["start_s"] * fps), round(b["end_s"] * fps)
            if start > cursor:
                v2.append(otio.schema.Gap(source_range=otio.opentime.TimeRange(
                    duration=otio.opentime.RationalTime(start - cursor, fps))))
            clip = clips[b["clip_id"]]
            c = otio.schema.Clip(name=f"{clip['name']} B-roll", media_reference=_ref(clip, fps),
                                 source_range=otio.opentime.TimeRange(
                                     otio.opentime.RationalTime(round(b["src_in"] * fps), fps),
                                     otio.opentime.RationalTime(end - start, fps)))
            c.metadata["nullcut"] = {"rule": b["rule"], "shot_id": b["shot_id"]}
            v2.append(c)
            cursor = end
        tl.tracks.append(v2)
    tl.tracks.append(a1)
    if meta.get("music"):
        m = meta["music"]
        total = sum(it.frames for it in items)
        a2 = otio.schema.Track(name="A2 music", kind=otio.schema.TrackKind.Audio)
        # Conform tools (the FCPXML adapter among them) need every reference's extent.
        ref = otio.schema.ExternalReference(
            target_url=Path(m["path"]).as_uri(),
            available_range=otio.opentime.TimeRange(
                otio.opentime.RationalTime(0, fps),
                otio.opentime.RationalTime(round(m.get("duration", total / fps) * fps), fps)))
        # One clip per bar-line piece, so a loop conforms as the loop it is.
        placed = 0
        for k, (s0, s1) in enumerate(m["pieces"]):
            frames = min(round((s1 - s0) * fps), total - placed)
            if frames <= 0:
                break
            mc = otio.schema.Clip(name=f"{m['name']} ({k + 1})", media_reference=ref,
                                  source_range=otio.opentime.TimeRange(
                                      otio.opentime.RationalTime(round(s0 * fps), fps),
                                      otio.opentime.RationalTime(frames, fps)))
            mc.metadata["nullcut"] = {"licence": m["licence"], "piece": k}
            a2.append(mc)
            placed += frames
        tl.tracks.append(a2)
    tl.metadata["nullcut"] = {
        "schema": SCHEMA,
        **meta,
        "clips": {cid: {"path": c["path"], "proxy": c["proxy"], "mix_audio": c["mix_audio"],
                        "fps": c["fps"], "duration": c["duration"], "name": c["name"]}
                  for cid, c in clips.items()},
    }
    return tl


def save(project: Project, items: list[Item], index: dict, meta: dict) -> tuple[int, Path, bool]:
    """Write a new version unless the latest already holds the same inputs.

    Returns (version, path, created).
    """
    latest = project.latest("timelines", "timeline", ".otio")
    if latest:
        prior = otio.adapters.read_from_file(str(latest[1]))
        if dict(prior.metadata.get("nullcut", {})).get("input_hash") == meta["input_hash"]:
            return latest[0], latest[1], False
    version, path = project.next_version("timelines", "timeline", ".otio")
    tl = build(items, index, {**meta, "version": version})
    otio.adapters.write_to_file(tl, str(path))
    return version, path, True


def _plain(value):
    """OTIO metadata comes back as AnyDictionary/AnyVector; convert to built-ins."""
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str):
        return [_plain(v) for v in value]
    return value


def load(path: Path) -> tuple[dict, list[Item]]:
    tl = otio.adapters.read_from_file(str(path))
    meta = _plain(tl.metadata["nullcut"])
    if meta.get("schema") != SCHEMA:
        raise SystemExit(f"{path.name}: unsupported timeline schema {meta.get('schema')}")
    video = next(t for t in tl.tracks if t.kind == otio.schema.TrackKind.Video and t.name == "V1")
    items = []
    for clip in video.find_clips():
        m = _plain(clip.metadata["nullcut"])
        items.append(Item(m["clip_id"], m["segment_id"], m["src_in"], m["src_out"],
                          m["frames"], m["words"], m["rules"], m.get("beat_id", "")))
    return meta, items

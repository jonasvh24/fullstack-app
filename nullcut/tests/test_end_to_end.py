"""Gate 0 in miniature: folder of clips to a captioned, loudness-normalised edit."""

from pathlib import Path

import opentimelineio as otio
import pytest

from nullcut import media
from nullcut.cli import main
from nullcut.project import read_json

from .conftest import needs_ffmpeg

pytestmark = needs_ffmpeg


def run(*argv: str) -> int:
    return main(list(argv))


def project_root(home: Path) -> Path:
    return home / "projects" / (home / ".current").read_text().strip()


def test_ingest_then_rough(footage: Path, tmp_path: Path, capsys):
    home = tmp_path / "home"
    assert run("--home", str(home), "ingest", str(footage)) == 0
    root = project_root(home)
    index = read_json(root / "index" / "clip_index.json")
    assert [c["name"] for c in index["clips"]] == ["01_intro.mp4", "02_outro.mp4"]
    assert all((root / c["proxy"]).exists() for c in index["clips"])
    assert all(c["asr"].startswith("sidecar") for c in index["clips"])

    # The plan's own command: no project argument, last one used.
    # 8 s of speech against a 6 s target: the fit has to drop sentences.
    assert run("--home", str(home), "rough", "--captions", "--target", "6s", "--preview") == 0
    out = capsys.readouterr().out
    assert "±5%: met" in out and "lowest-scoring sentences" in out

    mp4 = root / "renders" / "rough.v1.preview.mp4"
    assert mp4.exists()
    assert (root / "renders" / "rough.v1.srt").read_text().startswith("1\n")

    # The .otio is the contract: readable by stock OTIO, two tracks, real paths.
    tl = otio.adapters.read_from_file(str(root / "timelines" / "timeline.v1.otio"))
    assert [t.kind for t in tl.tracks] == ["Video", "Audio"]
    first = next(iter(tl.tracks[0].find_clips()))
    assert first.media_reference.target_url.startswith("file://")
    assert first.metadata["nullcut"]["rules"]

    # Video and audio must be the same length to within a frame: drift here
    # means every later cut is out of sync.
    v, a = media.stream_duration(mp4, "v"), media.stream_duration(mp4, "a")
    assert abs(v - a) < 1 / 30 + 0.025  # one frame plus one AAC frame
    assert 5.7 <= v <= 6.3

    meta = tl.metadata["nullcut"]
    expected = sum(c.metadata["nullcut"]["frames"] for c in tl.tracks[0].find_clips()) / meta["fps"]
    assert v == pytest.approx(expected, abs=1 / 30)


def test_loudness_lands_on_target(footage: Path, tmp_path: Path):
    home = tmp_path / "home"
    assert run("--home", str(home), "rough", str(footage), "--preview") == 0
    mp4 = project_root(home) / "renders" / "rough.v1.preview.mp4"
    from nullcut.render import measure_loudness
    loud = measure_loudness(mp4)
    assert loud["integrated_lufs"] == pytest.approx(-14.0, abs=1.0)
    assert loud["true_peak_dbtp"] <= -0.5


def test_unchanged_rerun_reuses_everything_and_caption_change_reencodes(
        footage: Path, tmp_path: Path, capsys):
    home = tmp_path / "home"
    assert run("--home", str(home), "rough", str(footage), "--preview") == 0
    capsys.readouterr()

    assert run("--home", str(home), "rough", "--preview") == 0
    out = capsys.readouterr().out
    assert "unchanged, reused" in out
    assert "0 items encoded" in out

    assert run("--home", str(home), "rough", "--preview", "--captions",
               "--caption-style", "word") == 0
    out = capsys.readouterr().out
    assert "timeline v2" in out
    assert " 0 reused from cache" in out  # every item now carries captions

    root = project_root(home)
    assert (root / "renders" / "rough.v2.preview.mp4").exists()
    assert run("--home", str(home), "render", "--timeline", "1", "--preview") == 0
    assert "0 items encoded" in capsys.readouterr().out


def test_shortfall_is_reported_not_padded(footage: Path, tmp_path: Path, capsys):
    home = tmp_path / "home"
    assert run("--home", str(home), "rough", str(footage), "--target", "9s", "--no-render") == 0
    out = capsys.readouterr().out
    assert "MISSED" in out and "Not padded" in out

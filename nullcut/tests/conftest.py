from __future__ import annotations

import json
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")


def words_from(script: list[tuple[str, float, float]]) -> list[dict]:
    return [{"w": w, "s": s, "e": e, "p": 0.95} for w, s, e in script]


def make_clip_record(clip_id: str, script: list[tuple[str, float, float]], duration: float,
                     lang: str = "en", fps: float = 30.0) -> dict:
    """A Clip Index record built in memory, for compiler tests that need no media."""
    from nullcut import analyse

    words = words_from(script)
    segments = analyse.build_segments(clip_id, words)
    for seg in segments:
        seg["features"] = {"speech_rate": 2.0, "confidence": 0.9, "energy": -20.0}
    return {"id": clip_id, "name": f"{clip_id}.mp4", "path": f"/tmp/{clip_id}.mp4",
            "duration": duration, "fps": fps, "width": 1920, "height": 1080,
            "has_audio": True, "proxy": f"media/proxies/{clip_id}.mp4",
            "mix_audio": f"media/audio/{clip_id}.pcm", "language": lang, "asr": "test",
            "words": words, "energy": {"hop": 0.1, "rms_db": []}, "segments": segments}


def make_media(path: Path, script: list[tuple[str, float, float]], duration: float,
               fps: int = 30, size: str = "640x360") -> None:
    """Video test pattern plus audio where every word is a tone burst at its exact time.

    The sidecar transcript then describes the audio truthfully, so loudness and
    timing checks on the render mean something.
    """
    sr = 48_000
    t = np.arange(int(duration * sr)) / sr
    audio = np.zeros_like(t)
    rng = np.random.default_rng(7)
    for n, (_, s, e) in enumerate(script):
        mask = (t >= s) & (t < e)
        freq = 180 + 40 * (n % 5)
        audio[mask] = 0.3 * np.sin(2 * np.pi * freq * t[mask])
    audio += 0.002 * rng.standard_normal(len(t))  # room tone, so silence is not digital zero
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2")
    wav = path.with_suffix(".src.wav")
    with wave.open(str(wav), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc2=s={size}:r={fps}:d={duration}",
         "-i", str(wav), "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", str(path)],
        check=True)
    wav.unlink()
    path.with_name(f"{path.stem}.words.json").write_text(
        json.dumps({"language": "en", "words": words_from(script)}))


# A talking head with every case the rough cut must handle: long pauses,
# a hard filler, a soft filler isolated by pauses, and sentence breaks.
CLIP_A = [
    ("Welcome", 0.50, 0.90), ("to", 0.95, 1.05), ("the", 1.10, 1.25), ("channel.", 1.30, 1.80),
    ("um", 2.60, 2.90),
    ("Today", 3.00, 3.35), ("we", 3.40, 3.55), ("build", 3.60, 3.95), ("things.", 4.00, 4.50),
    ("So", 5.60, 5.80),
    ("This", 6.20, 6.45), ("is", 6.50, 6.60), ("the", 6.65, 6.75), ("plan.", 6.80, 7.40),
]
CLIP_B = [
    ("Second", 0.30, 0.70), ("clip", 0.75, 1.10), ("starts", 1.15, 1.50), ("here.", 1.55, 2.00),
    ("And", 3.50, 3.70), ("it", 3.75, 3.85), ("ends", 3.90, 4.20), ("well.", 4.25, 4.80),
]


@pytest.fixture
def footage(tmp_path: Path) -> Path:
    folder = tmp_path / "footage"
    folder.mkdir()
    make_media(folder / "01_intro.mp4", CLIP_A, 8.0)
    make_media(folder / "02_outro.mp4", CLIP_B, 5.5)
    return folder


# --- a scripted OpenAI-compatible server ----------------------------------------------

class FakeLLM:
    """Answers /v1/chat/completions from a script; records every request body.

    Each script entry is a dict (the assistant message) or a callable taking the
    request body and returning one, so a test can answer from what was sent.
    """

    def __init__(self, script: list):
        import http.server
        import threading

        self.script = list(script)
        self.requests: list[dict] = []
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code: int, body: dict) -> None:
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._send(200, {"data": [{"id": "fake"}]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(body)
                if not fake.script:
                    return self._send(500, {"error": "script exhausted"})
                step = fake.script.pop(0)
                msg = step(body) if callable(step) else step
                self._send(200, {"choices": [{"message": {"role": "assistant", **msg}}]})

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()


@pytest.fixture
def fake_llm(monkeypatch):
    servers = []

    def start(script: list) -> FakeLLM:
        f = FakeLLM(script)
        servers.append(f)
        monkeypatch.setenv("NULLCUT_LLM_URL", f.url)
        monkeypatch.setenv("NULLCUT_LLM_MODEL", "fake")
        monkeypatch.delenv("NULLCUT_LLM", raising=False)
        return f

    yield start
    for s in servers:
        s.close()


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """No test reads or writes the real ~/nullcut, and no test reaches a real LLM."""
    monkeypatch.setenv("NULLCUT_HOME", str(tmp_path / "nullcut-home"))
    monkeypatch.setenv("NULLCUT_LLM", "off")
    monkeypatch.delenv("NULLCUT_VLM_URL", raising=False)
    monkeypatch.delenv("NULLCUT_CLOUD_PLANNER", raising=False)


def make_broll(path: Path, size: str = "640x360", fps: int = 30) -> None:
    """Three silent 3 s shots with hard cuts at 3 s and 6 s."""
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"mandelbrot=s={size}:r={fps}",
         "-f", "lavfi", "-i", f"testsrc2=s={size}:r={fps}",
         "-f", "lavfi", "-i", f"smptehdbars=s={size}:r={fps}",
         "-filter_complex", "[0]trim=0:3,setpts=PTS-STARTPTS[x];[1]trim=0:3,setpts=PTS-STARTPTS[y];"
                            "[2]trim=0:3,setpts=PTS-STARTPTS[z];[x][y][z]concat=n=3:v=1[v]",
         "-map", "[v]", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         str(path)], check=True)


def make_music(path: Path, bpm: float = 120.0, seconds: float = 40.0) -> None:
    """A kick on every beat, accented downbeats: a track with an unambiguous grid."""
    sr = 48_000
    t = np.arange(int(sr * seconds)) / sr
    y = np.zeros_like(t)
    beat = 60 / bpm
    for k in range(int(seconds / beat)):
        i = int(k * beat * sr)
        tt = np.arange(int(0.25 * sr)) / sr
        kick = (0.9 if k % 4 == 0 else 0.45) * np.sin(
            2 * np.pi * (60 + 120 * np.exp(-tt * 30)) * tt) * np.exp(-tt * 12)
        y[i:i + len(tt)] += kick[: len(y) - i]
    y += 0.05 * np.sin(2 * np.pi * 220 * t)
    y /= np.abs(y).max() * 1.1
    pcm = (np.stack([y, y], 1) * 32767).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())


@pytest.fixture
def footage_broll(footage: Path) -> Path:
    make_broll(footage / "03_broll.mp4")
    return footage


@pytest.fixture(scope="session")
def _ingested_template(tmp_path_factory) -> Path:
    """Footage with two speech clips (sidecar transcripts) and one B-roll clip, ingested once."""
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg not installed")
    from nullcut import analyse
    from nullcut.project import open_or_create

    base = tmp_path_factory.mktemp("ingested")
    folder = base / "footage"
    folder.mkdir()
    make_media(folder / "01_intro.mp4", CLIP_A, 8.0)
    make_media(folder / "02_outro.mp4", CLIP_B, 5.5)
    make_broll(folder / "03_broll.mp4")
    project = open_or_create(base / "home", folder, "template")
    analyse.ingest(project, folder, backend="sidecar", log=lambda m: None)
    return project.root


@pytest.fixture
def project(_ingested_template, tmp_path):
    """A private copy of the ingested project, so tests can plan and render freely."""
    import shutil

    from nullcut.project import Project

    root = tmp_path / "home" / "projects" / "template"
    shutil.copytree(_ingested_template, root)
    return Project(root)

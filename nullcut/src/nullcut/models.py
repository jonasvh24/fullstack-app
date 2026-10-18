"""Model registry: every weight file behind a config entry, with its licence (plan §11, §14).

If nullcut is ever sold, one non-commercial weight file in the default path
blocks the release. The default profile is checked by a test, so that
mistake fails the build instead of shipping.
"""

from __future__ import annotations

import os
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import config

SHERPA_RELEASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models"


@dataclass(frozen=True)
class Model:
    name: str
    job: str
    licence: str
    commercial: bool
    notes: str
    url: str | None = None  # None: fetched by its own library on first use
    directory: str | None = None
    files: tuple[str, ...] = field(default_factory=tuple)
    size_mb: int = 0
    archive: bool = True  # False: url is the single file itself


REGISTRY = {m.name: m for m in (
    Model("parakeet-tdt-0.6b-v3", "asr", "CC-BY-4.0", True,
          "NVIDIA Parakeet TDT, int8 ONNX via sherpa-onnx; 25 European languages "
          "incl. en and nl; word timestamps; CPU faster than real time",
          url=f"{SHERPA_RELEASE}/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8.tar.bz2",
          directory="sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8",
          files=("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt"),
          size_mb=487),
    Model("yunet-2023mar", "faces", "MIT", True,
          "OpenCV Zoo YuNet face detector, 230 KB ONNX; runs through cv2.FaceDetectorYN",
          url="https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/"
              "face_detection_yunet/face_detection_yunet_2023mar.onnx",
          directory="yunet-2023mar", files=("face_detection_yunet_2023mar.onnx",),
          size_mb=1, archive=False),
    Model("whisper-large-v3", "asr", "MIT", True,
          "faster-whisper / WhisperX; downloaded from Hugging Face by the library"),
)}

# What runs when nothing else is configured. Commercial licences only.
DEFAULT_PROFILE = {"asr": "parakeet-tdt-0.6b-v3", "faces": "yunet-2023mar"}


def models_dir() -> Path:
    env = os.environ.get("NULLCUT_MODELS")
    return Path(env).expanduser().resolve() if env else config.nullcut_home() / "models"


def path_of(name: str) -> Path:
    model = REGISTRY[name]
    if model.directory is None:
        raise ValueError(f"{name} is managed by its own library")
    return models_dir() / model.directory


def installed(name: str) -> bool:
    model = REGISTRY[name]
    if model.directory is None:
        return False
    root = path_of(name)
    return all((root / f).exists() for f in model.files)


def _progress(done: int, total: int) -> None:
    if total:
        pct = done * 100 // total
        sys.stderr.write(f"\r  {done >> 20} / {total >> 20} MB  {pct:3d}%")
        sys.stderr.flush()


def _safe_extract(archive: Path, dest: Path) -> None:
    with tarfile.open(archive) as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(dest, filter="data")
            return
        # Python < 3.12: refuse absolute paths and parent escapes by hand.
        root = dest.resolve()
        for member in tar.getmembers():
            target = (dest / member.name).resolve()
            if root not in target.parents and target != root:
                raise RuntimeError(f"unsafe path in archive: {member.name}")
            if member.issym() or member.islnk():
                raise RuntimeError(f"link in archive: {member.name}")
        tar.extractall(dest)


def pull(name: str, log=print) -> Path:
    model = REGISTRY[name]
    if model.url is None:
        raise SystemExit(f"{name} downloads itself on first use; nothing to pull")
    if installed(name):
        log(f"{name}: already installed at {path_of(name)}")
        return path_of(name)

    dest_root = models_dir()
    dest_root.mkdir(parents=True, exist_ok=True)
    log(f"{name}: downloading {model.size_mb} MB ({model.licence})")
    with tempfile.TemporaryDirectory(dir=dest_root, prefix=".pull-") as tmp:
        download = Path(tmp) / ("model.tar.bz2" if model.archive else model.files[0])
        with urllib.request.urlopen(model.url) as resp, download.open("wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while chunk := resp.read(1 << 20):
                out.write(chunk)
                done += len(chunk)
                _progress(done, total)
        sys.stderr.write("\n")
        if model.archive:
            log(f"{name}: unpacking")
            unpack = Path(tmp) / "unpack"
            _safe_extract(download, unpack)
            src = unpack / model.directory
        else:
            src = Path(tmp) / "single"
            src.mkdir()
            shutil.move(str(download), src / model.files[0])
        missing = [f for f in model.files if not (src / f).exists()]
        if missing:
            raise SystemExit(f"{name}: download is missing {', '.join(missing)}")
        final = path_of(name)
        if final.exists():
            shutil.rmtree(final)
        shutil.move(str(src), final)
    log(f"{name}: installed at {final}")
    return final

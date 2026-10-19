"""Project layout, content hashing and versioned artefacts (plan §3)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config

SUBDIRS = ("media/proxies", "media/audio", "index/clips", "plans", "timelines",
           "renders/cache", "package")
CURRENT_FILE = ".current"


def stable_hash(obj: Any, length: int = 16) -> str:
    """Hash any JSON-able value; dict key order never changes the result."""
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.blake2b(blob.encode(), digest_size=16).hexdigest()[:length]


def file_fingerprint(path: Path, chunk: int = 8 << 20) -> str:
    """Content id from size plus head and tail bytes.

    Hashing a 40 GB original in full would dominate ingest. Head, tail and
    size catch re-encodes, trims and renames-over without reading the middle.
    """
    h = hashlib.blake2b(digest_size=16)
    size = path.stat().st_size
    h.update(str(size).encode())
    with path.open("rb") as f:
        h.update(f.read(chunk))
        if size > chunk:
            f.seek(max(size - chunk, chunk))
            h.update(f.read(chunk))
    return h.hexdigest()


def write_json(path: Path, data: Any) -> None:
    # Write-then-rename so a crash mid-write never leaves a half file that the
    # next stage would parse as a valid artefact.
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def read_json(path: Path) -> Any:
    with path.open() as f:
        return json.load(f)


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:40] or "project"


def natural_key(name: str) -> list:
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


@dataclass
class Project:
    root: Path

    @property
    def id(self) -> str:
        return self.root.name

    @property
    def manifest_path(self) -> Path:
        return self.root / "project.json"

    @property
    def clip_index_path(self) -> Path:
        return self.root / "index" / "clip_index.json"

    def path(self, *parts: str) -> Path:
        return self.root.joinpath(*parts)

    def manifest(self) -> dict:
        return read_json(self.manifest_path)

    def clip_index(self) -> dict:
        if not self.clip_index_path.exists():
            raise SystemExit(f"{self.id}: not analysed yet, run `nullcut ingest` first")
        from . import learn  # scores follow the creator's learned weights, when adopted
        return learn.apply(read_json(self.clip_index_path))

    def next_version(self, subdir: str, stem: str, suffix: str) -> tuple[int, Path]:
        pattern = re.compile(rf"^{re.escape(stem)}\.v(\d+){re.escape(suffix)}$")
        versions = [int(m.group(1)) for p in self.path(subdir).iterdir()
                    if (m := pattern.match(p.name))]
        n = max(versions, default=0) + 1
        return n, self.path(subdir, f"{stem}.v{n}{suffix}")

    def latest(self, subdir: str, stem: str, suffix: str) -> tuple[int, Path] | None:
        n, _ = self.next_version(subdir, stem, suffix)
        if n == 1:
            return None
        return n - 1, self.path(subdir, f"{stem}.v{n - 1}{suffix}")


def project_id_for(folder: Path) -> str:
    folder = folder.resolve()
    return f"{slugify(folder.name)}-{stable_hash(str(folder), 6)}"


def open_or_create(home: Path, folder: Path, name: str | None = None) -> Project:
    folder = folder.resolve()
    pid = slugify(name) if name else project_id_for(folder)
    project = Project(home / "projects" / pid)
    for sub in SUBDIRS:
        project.path(sub).mkdir(parents=True, exist_ok=True)
    if not project.manifest_path.exists():
        write_json(project.manifest_path, {
            "id": pid, "name": name or folder.name, "source_dir": str(folder),
        })
    set_current(home, project)
    return project


def resolve(home: Path, ref: str | None) -> Project:
    """Find a project by id, by its source folder, or fall back to the last one used."""
    projects = home / "projects"
    if ref is None:
        current = home / CURRENT_FILE
        if not current.exists():
            raise SystemExit("no project yet: run `nullcut ingest <folder>` first")
        ref = current.read_text().strip()
    # An id is a bare name. Joining an absolute path onto `projects` would
    # silently return that path and treat the footage folder as a project.
    is_id = "/" not in ref and "\\" not in ref and ref not in (".", "..")
    if is_id and (projects / ref / "project.json").exists():
        project = Project(projects / ref)
    else:
        folder = Path(ref).expanduser()
        if not folder.is_dir():
            raise SystemExit(f"no project or folder called {ref!r}")
        match = None
        if projects.is_dir():
            for p in projects.iterdir():
                mf = p / "project.json"
                if mf.exists() and read_json(mf)["source_dir"] == str(folder.resolve()):
                    match = p
                    break
        project = Project(match or projects / project_id_for(folder))
        if not project.manifest_path.exists():
            raise SystemExit(f"{folder} has not been ingested")
    set_current(home, project)
    return project


def set_current(home: Path, project: Project) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / CURRENT_FILE).write_text(project.id)


def find_clips(folder: Path, recursive: bool = False) -> list[Path]:
    walker = folder.rglob("*") if recursive else folder.iterdir()
    clips = [p for p in walker
             if p.is_file() and p.suffix.lower() in config.VIDEO_EXTENSIONS
             and not p.name.startswith(".")]
    return sorted(clips, key=lambda p: natural_key(str(p.relative_to(folder))))

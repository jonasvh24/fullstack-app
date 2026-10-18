"""Background jobs: SQLite-backed, one worker per resource class (plan §3).

One creator, one machine: an in-process pool per class replaces Redis
without changing the contract. Analysis, LLM and encode work run in their
own lanes, so a master render never blocks planning and two encodes never
fight over NVENC. A restart marks interrupted jobs failed rather than
pretending they finished.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

LANES = {"ingest": "analyse", "music_import": "analyse", "plan": "llm",
         "preview": "encode", "master": "encode"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.db.execute("""create table if not exists jobs (
                id text primary key, project_id text, kind text, status text,
                progress real, message text, error text, result text,
                created text, updated text)""")
            self.db.execute("update jobs set status='failed', error='interrupted by a restart', "
                            "updated=? where status in ('queued','running')", (_now(),))
            self.db.commit()
        self.pools = {lane: ThreadPoolExecutor(max_workers=1, thread_name_prefix=lane)
                      for lane in set(LANES.values())}

    def _row(self, row) -> dict:
        keys = ("id", "project_id", "kind", "status", "progress", "message", "error", "result",
                "created", "updated")
        job = dict(zip(keys, row))
        job["result"] = json.loads(job["result"]) if job["result"] else None
        return job

    def get(self, job_id: str) -> dict | None:
        with self.lock:
            row = self.db.execute("select * from jobs where id=?", (job_id,)).fetchone()
        return self._row(row) if row else None

    def for_project(self, project_id: str, limit: int = 20) -> list[dict]:
        with self.lock:
            rows = self.db.execute("select * from jobs where project_id=? order by created desc, "
                                   "rowid desc limit ?", (project_id, limit)).fetchall()
        return [self._row(r) for r in rows]

    def update(self, job_id: str, **fields: Any) -> None:
        if "result" in fields:
            fields["result"] = json.dumps(fields["result"])
        fields["updated"] = _now()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.lock:
            self.db.execute(f"update jobs set {cols} where id=?", (*fields.values(), job_id))
            self.db.commit()

    def submit(self, project_id: str, kind: str,
               fn: Callable[[Callable[[float, str], None]], Any]) -> dict:
        job_id = uuid.uuid4().hex[:12]
        with self.lock:
            self.db.execute("insert into jobs values (?,?,?,?,?,?,?,?,?,?)",
                            (job_id, project_id, kind, "queued", 0.0, "queued", None, None,
                             _now(), _now()))
            self.db.commit()

        def progress(frac: float, message: str) -> None:
            self.update(job_id, progress=round(max(0.0, min(1.0, frac)), 3),
                        message=message[:200])

        def run() -> None:
            self.update(job_id, status="running", message="starting")
            try:
                result = fn(progress)
                self.update(job_id, status="done", progress=1.0, result=result,
                            message="done")
            except BaseException as exc:  # SystemExit from shared CLI code included
                detail = str(exc) or exc.__class__.__name__
                self.update(job_id, status="failed", error=detail[:2000],
                            message="failed")
                traceback.print_exc()

        self.pools[LANES[kind]].submit(run)
        return self.get(job_id)

    def busy(self, project_id: str, kind: str) -> dict | None:
        """The queued or running job of this kind, if any: one at a time per project."""
        for job in self.for_project(project_id):
            if job["kind"] == kind and job["status"] in ("queued", "running"):
                return job
        return None

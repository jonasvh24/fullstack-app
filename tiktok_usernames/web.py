"""Local web UI: python -m tiktok_usernames serve

Standard library only. One check run at a time, in a background thread; the
page polls /api/status for progress. Binds to 127.0.0.1 by default.

Because the server can start network activity, it refuses requests whose
Host header is not the address it was started on (blocks DNS-rebinding
pages) and only accepts JSON POST bodies (a cross-origin page cannot send
those without a CORS preflight, which we never answer).
"""
from __future__ import annotations

import csv
import io
import json
import threading
import time
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import candidates
from .checker import Blocked, Checker, Fetcher, Status, http_fetch
from .rules import MAX_LEN
from .store import Store

STATIC = Path(__file__).parent / "static"
MAX_BODY = 2_000_000
MAX_CANDIDATES = 20_000
PREVIEW_SAMPLE = 120


class BadRequest(ValueError):
    pass


class _Stopped(Exception):
    """Raised inside the fetcher to abandon a check when the user stops a run."""


def _num(body: dict, key: str, default, lo, hi, cast=int):
    value = body.get(key, default)
    if value in (None, ""):
        return default
    try:
        value = cast(value)
    except (TypeError, ValueError):
        raise BadRequest(f"{key} must be a number") from None
    if not lo <= value <= hi:
        raise BadRequest(f"{key} must be between {lo} and {hi}")
    return value


def _strings(body: dict, key: str, max_items: int) -> list[str]:
    value = body.get(key) or []
    if isinstance(value, str):
        value = value.replace(",", " ").split()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise BadRequest(f"{key} must be a list of strings")
    if len(value) > max_items:
        raise BadRequest(f"{key}: at most {max_items} entries")
    return [v.strip() for v in value if v.strip()]


def parse_spec(body: dict) -> candidates.Spec:
    patterns = _strings(body, "patterns", 20)
    if any(len(p) > MAX_LEN for p in patterns):
        raise BadRequest(f"patterns can be at most {MAX_LEN} characters")
    lengths = body.get("lengths") or []
    if not isinstance(lengths, list) or not all(isinstance(n, int) and 2 <= n <= 8 for n in lengths):
        raise BadRequest("lengths must be whole numbers from 2 to 8")

    spec = candidates.Spec(
        patterns=patterns,
        lengths=lengths,
        words=_strings(body, "words", 200_000),
        repeats=bool(body.get("repeats")),
        names=_strings(body, "names", MAX_CANDIDATES),
        limit=_num(body, "limit", 200, 1, 5000),
        min_rarity=_num(body, "min_rarity", 0, 0, 100),
        seed=_num(body, "seed", None, 0, 2**31),
    )
    if not (spec.patterns or spec.lengths or spec.words or spec.repeats or spec.names):
        raise BadRequest("Add at least one pattern, length, word, repeat shape or name.")
    return spec


class Run:
    def __init__(self, todo: list[tuple[str, int]], cached: int, delay: float, jitter: float):
        self.todo = todo
        self.cached = cached
        self.delay = delay
        self.jitter = jitter
        self.state = "running"
        self.message = ""
        self.done = 0
        self.counts = {s.value: 0 for s in Status}
        self.feed: deque[dict] = deque(maxlen=100)
        self.started = time.time()
        self.finished: float | None = None
        self.stop_event = threading.Event()

    def snapshot(self) -> dict:
        remaining = len(self.todo) - self.done
        return {
            "state": self.state,
            "message": self.message,
            "total": len(self.todo),
            "done": self.done,
            "cached": self.cached,
            "counts": self.counts,
            "eta_seconds": round(remaining * (self.delay + self.jitter / 2)) if self.state == "running" else 0,
            "started": self.started,
            "finished": self.finished,
            "feed": list(self.feed),
        }


class App:
    def __init__(self, db: Path, fetch: Fetcher = http_fetch):
        self.db = db
        self.fetch = fetch
        self.lock = threading.Lock()
        self.run: Run | None = None
        self.thread: threading.Thread | None = None
        Store(db).close()  # create the schema up front

    def _split(self, spec: candidates.Spec, max_age_hours: float) -> tuple[list, list]:
        scored = candidates.build(spec)
        if len(scored) > MAX_CANDIDATES:
            raise BadRequest(f"{len(scored):,} candidates; narrow it to {MAX_CANDIDATES:,} or fewer.")
        store = Store(self.db)
        try:
            fresh = [s for s in scored if store.is_fresh(s[0], max_age_hours)]
        finally:
            store.close()
        fresh_names = {n for n, _ in fresh}
        return [s for s in scored if s[0] not in fresh_names], fresh

    def preview(self, body: dict) -> dict:
        delay = _num(body, "delay", 3.0, 1.0, 120.0, float)
        jitter = _num(body, "jitter", 1.5, 0.0, 60.0, float)
        todo, fresh = self._split(parse_spec(body), _num(body, "max_age_hours", 72.0, 0.0, 10_000.0, float))
        cached = {n for n, _ in fresh}
        sample = sorted(todo + fresh, key=lambda s: (-s[1], len(s[0]), s[0]))[:PREVIEW_SAMPLE]
        return {
            "total": len(todo) + len(fresh),
            "to_check": len(todo),
            "cached": len(fresh),
            "eta_seconds": round(len(todo) * (delay + jitter / 2)),
            "sample": [{"username": n, "rarity": r, "cached": n in cached} for n, r in sample],
        }

    def start(self, body: dict) -> dict:
        delay = _num(body, "delay", 3.0, 1.0, 120.0, float)
        jitter = _num(body, "jitter", 1.5, 0.0, 60.0, float)
        todo, fresh = self._split(parse_spec(body), _num(body, "max_age_hours", 72.0, 0.0, 10_000.0, float))
        with self.lock:
            if self.run and self.run.state == "running":
                raise BadRequest("A run is already in progress. Stop it first.")
            run = Run(todo, len(fresh), delay, jitter)
            self.run = run
            self.thread = threading.Thread(target=self._work, args=(run,), daemon=True)
            self.thread.start()
        return run.snapshot()

    def stop(self) -> dict:
        with self.lock:
            if self.run and self.run.state == "running":
                self.run.stop_event.set()
            return self.status()

    def status(self) -> dict:
        store = Store(self.db)
        try:
            counts = store.counts()
        finally:
            store.close()
        return {"run": self.run.snapshot() if self.run else None, "totals": counts}

    def _work(self, run: Run) -> None:
        def fetch(url: str) -> tuple[int, str]:
            if run.stop_event.is_set():
                raise _Stopped
            return self.fetch(url)

        # Event.wait doubles as an interruptible sleep, so Stop is immediate
        # even in the middle of a long delay or 429 backoff.
        checker = Checker(delay=run.delay, jitter=run.jitter, fetch=fetch, sleep=run.stop_event.wait)
        store = Store(self.db)
        scores = dict(run.todo)

        def record(result) -> None:
            store.save(result, scores[result.username])
            run.done += 1
            run.counts[result.status.value] += 1
            run.feed.appendleft({
                "username": result.username,
                "rarity": scores[result.username],
                "status": result.status.value,
                "tiktok_code": result.tiktok_code,
                "detail": result.detail,
            })

        try:
            for name, _ in run.todo:
                if run.stop_event.is_set():
                    raise _Stopped
                record(checker.check(name))
            run.state = "done"
            run.message = "Finished."
        except _Stopped:
            run.state = "stopped"
            run.message = "Stopped. Results so far are saved."
        except Blocked as err:
            record(err.result)
            run.state = "blocked"
            run.message = str(err)
        except Exception as err:  # keep the UI informed rather than dying silently
            run.state = "error"
            run.message = f"{type(err).__name__}: {err}"
        finally:
            run.finished = time.time()
            store.close()

    def results(self, query: dict[str, list[str]]) -> list[dict]:
        status_raw = (query.get("status") or ["no_profile"])[0]
        try:
            status = None if status_raw == "all" else Status(status_raw)
        except ValueError:
            raise BadRequest("status must be no_profile, taken, unknown or all") from None
        min_rarity = _num({"m": (query.get("min_rarity") or [0])[0]}, "m", 0, 0, 100)
        limit = _num({"l": (query.get("limit") or [500])[0]}, "l", 500, 1, 100_000)
        search = (query.get("q") or [""])[0].strip().lower()[:MAX_LEN]
        store = Store(self.db)
        try:
            return store.rows(status, min_rarity, search, limit)
        finally:
            store.close()


def make_handler(app: App, allowed_hosts: set[str]):
    class Handler(BaseHTTPRequestHandler):
        server_version = "tiktok-usernames"

        def log_message(self, fmt, *args):  # keep the terminal for run output
            pass

        def _host_ok(self) -> bool:
            if "*" in allowed_hosts:
                return True
            return (self.headers.get("Host") or "").lower() in allowed_hosts

        def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload, status: int = 200) -> None:
            self._send(status, json.dumps(payload).encode(), "application/json")

        def _error(self, status: int, message: str) -> None:
            self._json({"error": message}, status)

        def _body(self) -> dict:
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                raise BadRequest("Content-Type must be application/json")
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise BadRequest("Request too large")
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                raise BadRequest("Body is not valid JSON") from None
            if not isinstance(data, dict):
                raise BadRequest("Body must be a JSON object")
            return data

        def _dispatch(self, routes: dict) -> None:
            if not self._host_ok():
                return self._error(HTTPStatus.FORBIDDEN, "Unexpected Host header")
            url = urlparse(self.path)
            handler = routes.get(url.path)
            if handler is None:
                return self._error(HTTPStatus.NOT_FOUND, "Not found")
            try:
                handler(parse_qs(url.query))
            except BadRequest as err:
                self._error(HTTPStatus.BAD_REQUEST, str(err))

        def do_GET(self) -> None:
            self._dispatch({
                "/": lambda q: self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8"),
                "/api/status": lambda q: self._json(app.status()),
                "/api/results": lambda q: self._json(app.results(q)),
                "/api/results.csv": self._csv,
            })

        def do_POST(self) -> None:
            self._dispatch({
                "/api/preview": lambda q: self._json(app.preview(self._body())),
                "/api/run": lambda q: self._json(app.start(self._body())),
                "/api/run/stop": lambda q: (self._body(), self._json(app.stop())),
            })

        def _csv(self, query) -> None:
            rows = app.results(query)
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(["username", "status", "rarity", "tiktok_code", "checked_at_utc"])
            for r in rows:
                checked = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(r["checked_at"]))
                writer.writerow([r["username"], r["status"], r["rarity"], r["tiktok_code"], checked])
            self._send(200, buf.getvalue().encode(), "text/csv; charset=utf-8",
                       {"Content-Disposition": 'attachment; filename="tiktok_usernames.csv"'})

    return Handler


def make_server(db: Path, host: str, port: int, fetch: Fetcher = http_fetch) -> ThreadingHTTPServer:
    app = App(db, fetch)
    # Placeholder handler so the server binds and we learn the real port (port 0 in tests).
    server = ThreadingHTTPServer((host, port), BaseHTTPRequestHandler)
    real_port = server.server_address[1]
    if host in ("0.0.0.0", "::", ""):
        allowed = {"*"}
    else:
        allowed = {f"{h}:{real_port}" for h in (host, "localhost", "127.0.0.1")}
    server.RequestHandlerClass = make_handler(app, allowed)
    server.app = app  # type: ignore[attr-defined]
    return server


def serve(db: Path, host: str = "127.0.0.1", port: int = 8000) -> None:
    server = make_server(db, host, port)
    shown = "localhost" if host in ("127.0.0.1", "localhost") else host
    print(f"Serving on http://{shown}:{server.server_address[1]}  (Ctrl+C to stop)")
    print(f"Results database: {db.resolve()}")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("Warning: reachable from other machines on your network.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.server_close()

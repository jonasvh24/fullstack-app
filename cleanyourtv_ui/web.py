"""Local web UI for CleanYourTV: python -m cleanyourtv_ui

Standard library only. Every change to the TV runs one of the checkout's own scripts as a job in a
background thread, one job at a time; the page polls /api/job for its output. Short reads (probe,
screenshot, package states) run inline, one at a time, so two of them never race for the TV.

The server drives a TV on your network, so it is as strict as the username finder's: it binds to
127.0.0.1, refuses requests whose Host header is not the address it was started on (blocks
DNS-rebinding pages) and only accepts JSON POST bodies (a cross-origin page cannot send those without
a CORS preflight, which it never answers). Safety overrides such as FORCE_HOME are stripped from the
environment of every script it starts.
"""
import json
import os
import re
import signal
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .upstream import REMOTE_KEYS, SAFETY_OVERRIDES, Invalid, Upstream, check_host

STATIC = Path(__file__).parent / "static"
MAX_BODY = 1_000_000
MAX_LINES = 20_000
# tools/tv.py waits up to 120 s for "Allow USB debugging?" on a first connection.
TV_TIMEOUT = 150
PROBE_KEYS = {
    "ro.product.model": "model", "ro.product.device": "device", "ro.build.version.release": "android",
    "ro.build.version.sdk": "sdk", "ro.build.display.id": "build", "ro.product.manufacturer": "manufacturer",
}


class BadRequest(ValueError):
    pass


class Busy(RuntimeError):
    pass


class Job:
    def __init__(self, action: str, title: str, steps: list[list[str]]):
        self.id = f"{int(time.time() * 1000):x}"
        self.action = action
        self.title = title
        self.steps = steps
        self.state = "running"
        self.exit_code: int | None = None
        self.lines: list[str] = []
        self.dropped = 0
        self.started = time.time()
        self.finished: float | None = None
        self.proc: subprocess.Popen | None = None
        self.stopping = False

    def add(self, line: str) -> None:
        self.lines.append(line)
        if len(self.lines) > MAX_LINES:
            cut = len(self.lines) - MAX_LINES
            del self.lines[:cut]
            self.dropped += cut

    def snapshot(self, since: int | None = None) -> dict:
        out = {"id": self.id, "action": self.action, "title": self.title, "state": self.state,
               "exit_code": self.exit_code, "started": self.started, "finished": self.finished,
               "total": self.dropped + len(self.lines)}
        if since is not None:
            out["since"] = max(since, self.dropped)
            out["lines"] = self.lines[out["since"] - self.dropped:]
        return out


class App:
    def __init__(self, upstream: Upstream, host: str = ""):
        self.up = upstream
        self.host = check_host(host) if host else ""
        self.tv: dict | None = None
        self.tv_error = ""
        self.lock = threading.Lock()
        self.tv_lock = threading.Lock()
        self.job: Job | None = None
        self.history: list[dict] = []
        self.shot = upstream.data / "screen.png"

    # -- environment and processes

    def env(self) -> dict:
        env = {k: v for k, v in os.environ.items() if k not in SAFETY_OVERRIDES}
        env["PYTHONUNBUFFERED"] = "1"
        if self.host:
            env["TV_HOST"] = self.host
        return env

    def _need_host(self) -> None:
        if not self.host:
            raise BadRequest("Enter the TV's IP address first.")
        if not self.up.ready:
            raise BadRequest("Set up the tools first (Setup on the Connect page).")

    def tv_call(self, *args: str, stdin: str | None = None, timeout: int = TV_TIMEOUT) -> tuple[int, str]:
        """Run tools/tv.py once and return (exit code, stdout+stderr)."""
        self._need_host()
        if not self.tv_lock.acquire(blocking=False):
            raise Busy("The TV is answering another request; try again in a moment.")
        try:
            done = subprocess.run(self.up.tv_cmd(*args), cwd=self.up.root, env=self.env(), input=stdin,
                                  capture_output=True, text=True, timeout=timeout)
            return done.returncode, (done.stdout + done.stderr).strip()
        except subprocess.TimeoutExpired:
            return 124, "No answer from the TV in time. Is it on, and is network debugging enabled?"
        finally:
            self.tv_lock.release()

    # -- the TV

    def set_host(self, body: dict) -> dict:
        try:
            host = check_host(body.get("host", ""))
        except Invalid as err:
            raise BadRequest(str(err)) from None
        if host != self.host:
            self.host, self.tv, self.tv_error = host, None, ""
        return self.state()

    def probe(self) -> dict:
        code, out = self.tv_call("--probe")
        info = {}
        for line in out.splitlines():
            key, sep, value = line.partition(" = ")
            if sep and key.strip() in PROBE_KEYS:
                info[PROBE_KEYS[key.strip()]] = value.strip()
        if code != 0 or not info.get("model"):
            self.tv, self.tv_error = None, out or f"tv.py exited with {code}"
        else:
            info["matches"] = self.up.match_examples(info)
            self.tv, self.tv_error = info, ""
        return self.state()

    def packages(self) -> dict:
        code, out = self.tv_call("--batch", stdin="pm list packages\npm list packages -d\n")
        if code != 0:
            raise BadRequest(out or f"tv.py exited with {code}")
        groups: dict[str, set[str]] = {}
        current = None
        for line in out.splitlines():
            if line.startswith("$ "):
                current = groups.setdefault(line[2:].strip(), set())
            elif current is not None and line.startswith("package:"):
                current.add(line[8:].strip())
        installed = groups.get("pm list packages", set())
        disabled = groups.get("pm list packages -d", set())
        if not installed:
            raise BadRequest("The TV listed no packages: " + (out[:300] or "empty answer"))
        return {"installed": sorted(installed), "disabled": sorted(disabled)}

    def screenshot(self, body: dict) -> dict:
        key = body.get("key")
        if key is not None and key not in REMOTE_KEYS:
            raise BadRequest("unknown key")
        self.shot.parent.mkdir(parents=True, exist_ok=True)
        args = ["--screenshot", str(self.shot)] + ([key, "--wait", "0.8"] if key else [])
        code, out = self.tv_call(*args, timeout=90)
        return {"ok": code == 0, "message": out, "at": time.time()}

    def key(self, body: dict) -> dict:
        key = body.get("key")
        if key not in REMOTE_KEYS:
            raise BadRequest("unknown key")
        code, out = self.tv_call(f"input keyevent KEYCODE_{key}", timeout=30)
        return {"ok": code == 0, "message": out}

    # -- jobs

    def start(self, body: dict) -> dict:
        action = body.get("action", "")
        if action != "setup":
            self._need_host()
        try:
            title, steps = self.up.action(action, body, self.host)
        except Invalid as err:
            raise BadRequest(str(err)) from None
        with self.lock:
            if self.job and self.job.state == "running":
                raise BadRequest("Another job is running. Wait for it or stop it first.")
            job = Job(action, title, steps)
            self.job = job
        threading.Thread(target=self._work, args=(job,), daemon=True).start()
        return job.snapshot(0)

    def stop(self) -> dict:
        with self.lock:
            job = self.job
        if job and job.state == "running" and job.proc:
            job.stopping = True
            try:
                os.killpg(job.proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        return self.job_state(None)

    def _work(self, job: Job) -> None:
        code = 0
        try:
            for argv in job.steps:
                job.add(f"$ {' '.join(argv)}")
                # A process group of its own, so Stop reaches the bash script and the python it started.
                job.proc = subprocess.Popen(argv, cwd=self.up.root, env=self.env(), stdin=subprocess.DEVNULL,
                                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                            errors="replace", bufsize=1, start_new_session=True)
                for line in job.proc.stdout:
                    job.add(line.rstrip("\n"))
                code = job.proc.wait()
                if code != 0 or job.stopping:
                    break
            if job.stopping:
                job.state = "stopped"
            else:
                job.state = "done" if code == 0 else "failed"
        except Exception as err:  # keep the page informed rather than dying silently
            job.add(f"{type(err).__name__}: {err}")
            job.state, code = "failed", -1
        finally:
            job.exit_code = code
            job.finished = time.time()
            if job.action == "setup":
                self.up.reset()
            self.history.insert(0, job.snapshot())
            del self.history[20:]

    def job_state(self, since: int | None) -> dict:
        return {"job": self.job.snapshot(since) if self.job else None, "history": self.history}

    def state(self) -> dict:
        return {"upstream": self.up.info(), "host": self.host, "tv": self.tv, "tv_error": self.tv_error,
                "job": self.job.snapshot() if self.job else None}


def make_handler(app: App, allowed_hosts: set[str]):
    class Handler(BaseHTTPRequestHandler):
        server_version = "cleanyourtv-ui"

        def log_message(self, fmt, *args):  # keep the terminal quiet
            pass

        def _host_ok(self) -> bool:
            if "*" in allowed_hosts:
                return True
            return (self.headers.get("Host") or "").lower() in allowed_hosts

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload, status: int = 200) -> None:
            self._send(status, json.dumps(payload).encode(), "application/json")

        def _error(self, status: int, message: str) -> None:
            self._json({"error": message}, status)

        def _file(self, path: Path, content_type: str) -> None:
            if not path.is_file():
                return self._error(HTTPStatus.NOT_FOUND, "Not found")
            self._send(200, path.read_bytes(), content_type)

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
                handler({k: v[0] for k, v in parse_qs(url.query).items()})
            except (BadRequest, Invalid) as err:
                self._error(HTTPStatus.BAD_REQUEST, str(err))
            except Busy as err:
                self._error(HTTPStatus.CONFLICT, str(err))

        def _since(self, q: dict) -> int:
            return int(q["since"]) if re.fullmatch(r"\d{1,9}", q.get("since", "")) else 0

        def do_GET(self) -> None:
            self._dispatch({
                "/": lambda q: self._file(STATIC / "index.html", "text/html; charset=utf-8"),
                "/api/state": lambda q: self._json(app.state()),
                "/api/lists": lambda q: self._json({"examples": app.up.examples(), "custom": app.up.custom_lists()}),
                "/api/list": lambda q: self._json(app.up.list_detail(q.get("id", ""))),
                "/api/themes": lambda q: self._json(app.up.themes()),
                "/api/theme.jpg": lambda q: self._file(app.up.theme_dir(q.get("name", "")) / "screenshot.jpg",
                                                       "image/jpeg"),
                "/api/job": lambda q: self._json(app.job_state(self._since(q))),
                "/api/tv/screen.png": lambda q: self._file(app.shot, "image/png"),
            })

        def do_POST(self) -> None:
            self._dispatch({
                "/api/tv/host": lambda q: self._json(app.set_host(self._body())),
                "/api/tv/probe": lambda q: (self._body(), self._json(app.probe())),
                "/api/tv/packages": lambda q: (self._body(), self._json(app.packages())),
                "/api/tv/screenshot": lambda q: self._json(app.screenshot(self._body())),
                "/api/tv/key": lambda q: self._json(app.key(self._body())),
                "/api/lists/save": lambda q: self._save(self._body()),
                "/api/lists/delete": lambda q: (app.up.delete_list(self._body().get("id", "")), self._json({})),
                "/api/run": lambda q: self._json(app.start(self._body())),
                "/api/job/stop": lambda q: (self._body(), self._json(app.stop())),
            })

        def _save(self, body: dict) -> None:
            on, added = body.get("on") or [], body.get("added") or []
            if not (isinstance(on, list) and isinstance(added, list) and all(isinstance(v, str) for v in on + added)):
                raise BadRequest("on and added must be lists of package names")
            self._json({"id": app.up.save_list(body.get("name", ""), body.get("base"), on, added)})

    return Handler


def make_server(upstream: Upstream, host: str, port: int, tv_host: str = "") -> ThreadingHTTPServer:
    app = App(upstream, tv_host)
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


def serve(upstream: Upstream, host: str = "127.0.0.1", port: int = 8010, tv_host: str = "") -> None:
    server = make_server(upstream, host, port, tv_host)
    shown = "localhost" if host in ("127.0.0.1", "localhost") else host
    print(f"Serving on http://{shown}:{server.server_address[1]}  (Ctrl+C to stop)")
    print(f"CleanYourTV checkout: {upstream.root}" + ("" if upstream.present else "  (missing, see README)"))
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("Warning: reachable from other machines on your network, and it can change your TV. No login.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.server_close()

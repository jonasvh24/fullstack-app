import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from cleanyourtv_ui.tests.fake_upstream import build
from cleanyourtv_ui.upstream import Upstream
from cleanyourtv_ui.web import make_server


class Web(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = build(Path(self.tmp.name) / "repo")
        self.up = Upstream(root, Path(self.tmp.name) / "data")
        self.server = make_server(self.up, "127.0.0.1", 0)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def call(self, path, body=None, headers=None):
        data = None if body is None else json.dumps(body).encode()
        hdrs = {"Content-Type": "application/json"} if body is not None else {}
        hdrs.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=20) as res:
                raw = res.read()
                return res.status, (json.loads(raw) if res.headers["Content-Type"] == "application/json" else raw)
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    def wait_job(self):
        for _ in range(200):
            _, r = self.call("/api/job?since=0")
            if r["job"] and r["job"]["state"] != "running":
                return r["job"]
            time.sleep(0.05)
        self.fail("job did not finish")

    def connect(self):
        self.call("/api/tv/host", {"host": "192.168.1.50"})
        return self.call("/api/tv/probe", {})[1]

    def test_page_and_state(self):
        status, page = self.call("/")
        self.assertEqual(status, 200)
        self.assertIn(b"CleanYourTV", page)
        _, st = self.call("/api/state")
        self.assertTrue(st["upstream"]["ready"])
        self.assertEqual(st["host"], "")

    def test_foreign_host_and_form_posts_refused(self):
        self.assertEqual(self.call("/api/state", headers={"Host": "evil.example:80"})[0], 403)
        req = urllib.request.Request(self.base + "/api/run", data=b"action=disable",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        with self.assertRaises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req)
        self.assertEqual(err.exception.code, 400)

    def test_needs_host_before_tv_calls(self):
        status, r = self.call("/api/tv/probe", {})
        self.assertEqual(status, 400)
        self.assertIn("IP address", r["error"])
        self.assertEqual(self.call("/api/tv/host", {"host": "1.2.3.4; reboot"})[0], 400)

    def test_probe_and_matches(self):
        st = self.connect()
        self.assertEqual(st["tv"]["model"], "MiTV-MOOQ1")
        self.assertEqual(st["tv"]["android"], "10")
        self.assertEqual(st["tv"]["matches"], ["xiaomi-mi-tv-4a"])

    def test_probe_failure_is_reported(self):
        self.call("/api/tv/host", {"host": "10.9.9.9"})
        _, st = self.call("/api/tv/probe", {})
        self.assertIsNone(st["tv"])
        self.assertIn("timed out", st["tv_error"])

    def test_packages(self):
        self.connect()
        _, r = self.call("/api/tv/packages", {})
        self.assertEqual(r["disabled"], ["com.acme.tracker"])
        self.assertIn("com.acme.ads", r["installed"])

    def test_screenshot_and_keys(self):
        self.connect()
        _, r = self.call("/api/tv/screenshot", {"key": "DPAD_RIGHT"})
        self.assertTrue(r["ok"], r)
        self.assertIn("DPAD_RIGHT", r["message"])
        status, png = self.call("/api/tv/screen.png")
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertEqual(self.call("/api/tv/key", {"key": "KEYCODE_POWER; reboot"})[0], 400)
        _, r = self.call("/api/tv/key", {"key": "HOME"})
        self.assertIn("input keyevent KEYCODE_HOME", r["message"])

    def test_lists_and_save(self):
        _, lists = self.call("/api/lists")
        self.assertEqual(len(lists["examples"]), 2)
        _, d = self.call("/api/list?id=ex:acme-tv-1/debloat.txt")
        self.assertEqual(d["readme"], "# Acme notes\n")
        self.assertFalse(d["recovery_record"])
        _, r = self.call("/api/lists/save", {"name": "mine", "base": d["id"], "on": ["com.acme.ads"], "added": []})
        self.assertEqual(r["id"], "custom:mine")
        self.assertEqual(self.call("/api/lists/save", {"name": "x", "on": "com.a.b"})[0], 400)
        self.assertEqual(self.call("/api/list?id=custom:../../x")[0], 400)

    def test_disable_job_strips_safety_overrides(self):
        self.connect()
        with mock.patch.dict(os.environ, {"FORCE_HOME": "1"}):
            _, j = self.call("/api/run", {"action": "disable", "list": "ex:acme-tv-1/debloat.txt"})
            self.assertEqual(j["state"], "running")
            job = self.wait_job()
        self.assertEqual(job["state"], "done")
        _, r = self.call("/api/job?since=0")
        out = "\n".join(r["job"]["lines"])
        self.assertIn("TV_HOST=192.168.1.50 FORCE_HOME=unset", out)
        self.assertIn("examples/acme-tv-1/debloat.txt", out)
        self.assertEqual(r["history"][0]["title"], "Disable debloat.txt")

    def test_failed_job(self):
        self.connect()
        with mock.patch.object(self.up, "action", return_value=("Fails", [[str(self.up.python), "-I", "tools/check.py", "--fail"]])):
            self.call("/api/run", {"action": "check"})
            job = self.wait_job()
        self.assertEqual((job["state"], job["exit_code"]), ("failed", 1))

    def test_one_job_at_a_time_and_stop(self):
        self.connect()
        with mock.patch.object(self.up, "action", return_value=("Slow", [["bash", "tools/slow.sh"]])):
            self.call("/api/run", {"action": "check"})
            status, r = self.call("/api/run", {"action": "check"})
            self.assertEqual(status, 400)
            self.assertIn("Another job", r["error"])
            time.sleep(0.3)
            self.call("/api/job/stop", {})
            started = time.time()
            job = self.wait_job()
        self.assertEqual(job["state"], "stopped")
        self.assertLess(time.time() - started, 5)

    def test_incremental_output(self):
        self.connect()
        self.call("/api/run", {"action": "theme", "theme": "aurora"})
        self.wait_job()
        _, all_lines = self.call("/api/job?since=0")
        _, tail = self.call("/api/job?since=2")
        self.assertEqual(tail["job"]["lines"], all_lines["job"]["lines"][2:])
        self.assertEqual(self.call("/api/run", {"action": "theme", "theme": "../examples"})[0], 400)

    def test_theme_screenshot(self):
        status, jpg = self.call("/api/theme.jpg?name=aurora")
        self.assertEqual((status, jpg[:2]), (200, b"\xff\xd8"))
        self.assertEqual(self.call("/api/theme.jpg?name=..")[0], 400)


if __name__ == "__main__":
    unittest.main()

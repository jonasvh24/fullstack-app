import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from tiktok_usernames.tests.test_usernames import MISSING_PAGE, universal_page
from tiktok_usernames.verify import KNOWN_TAKEN
from tiktok_usernames.web import make_server

TAKEN = {"kavo", "bako", *KNOWN_TAKEN}


def fake_fetch(url: str) -> tuple[int, str]:
    name = url.rsplit("@", 1)[1]
    if name in TAKEN:
        return 200, universal_page({"statusCode": 0, "userInfo": {"user": {"uniqueId": name}}})
    return 200, MISSING_PAGE


class WebTest(unittest.TestCase):
    fetch = staticmethod(fake_fetch)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = make_server(Path(self.tmp.name) / "w.db", "127.0.0.1", 0, fetch=self.fetch)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        run = self.server.app.run
        if run:
            run.stop_event.set()
            self.server.app.thread.join(5)
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def request(self, path, body=None, headers=None, raw=False):
        data = None if body is None else json.dumps(body).encode()
        hdrs = {"Content-Type": "application/json"} if body is not None else {}
        hdrs.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                payload = resp.read()
                return resp.status, payload if raw else json.loads(payload)
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    def wait_for(self, states, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            _, s = self.request("/api/status")
            if s["run"] and s["run"]["state"] in states:
                return s
            time.sleep(0.05)
        self.fail(f"run never reached {states}")

    def test_index_served(self):
        status, body = self.request("/", raw=True)
        self.assertEqual(status, 200)
        self.assertIn(b"Rare Handle Finder", body)

    def test_rejects_foreign_host(self):
        status, body = self.request("/api/status", headers={"Host": "evil.example:80"})
        self.assertEqual(status, 403)

    def test_rejects_non_json_post(self):
        req = urllib.request.Request(self.base + "/api/run", data=b"names=kavo",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 400)

    def test_validation(self):
        self.assertEqual(self.request("/api/preview", {})[0], 400)
        self.assertEqual(self.request("/api/preview", {"names": ["kavo"], "delay": 0.1})[0], 400)
        self.assertEqual(self.request("/api/preview", {"lengths": [30]})[0], 400)
        self.assertEqual(self.request("/api/results?status=bogus")[0], 400)

    def test_preview(self):
        status, p = self.request("/api/preview", {"patterns": ["LL"], "names": ["kavo"], "limit": 1000})
        self.assertEqual(status, 200)
        self.assertEqual(p["total"], 26 * 26 + 1)
        self.assertEqual(p["to_check"], p["total"])
        self.assertLessEqual(len(p["sample"]), 120)

    def test_run_to_completion_then_results_and_csv(self):
        status, _ = self.request("/api/run", {"names": ["kavo", "zqxv"], "delay": 1, "jitter": 0})
        self.assertEqual(status, 200)
        s = self.wait_for({"done"})
        self.assertTrue(s["run"]["verify"]["ok"])
        self.assertEqual(s["run"]["counts"], {"taken": 1, "no_profile": 1, "unknown": 0})
        self.assertEqual(s["totals"], {"taken": 1, "no_profile": 1})

        _, rows = self.request("/api/results?status=no_profile")
        self.assertEqual([r["username"] for r in rows], ["zqxv"])
        _, rows = self.request("/api/results?status=all&q=kav")
        self.assertEqual([r["username"] for r in rows], ["kavo"])

        status, csv_body = self.request("/api/results.csv?status=all", raw=True)
        lines = csv_body.decode().strip().splitlines()
        self.assertEqual(lines[0], "username,status,rarity,tiktok_code,checked_at_utc")
        self.assertEqual(len(lines), 3)

        # Second preview sees both as cached, and a rerun finishes without any requests.
        _, p = self.request("/api/preview", {"names": ["kavo", "zqxv"]})
        self.assertEqual((p["to_check"], p["cached"]), (0, 2))
        self.request("/api/run", {"names": ["kavo", "zqxv"], "delay": 1})
        s = self.wait_for({"done"})
        self.assertIsNone(s["run"]["verify"])
        self.assertIn("cached", s["run"]["message"])

    def test_verify_endpoint(self):
        status, v = self.request("/api/verify", {"delay": 1})
        self.assertEqual(status, 200)
        self.assertTrue(v["ok"])
        self.assertEqual(len(v["lines"]), 3)
        _, s = self.request("/api/status")
        self.assertTrue(s["last_verify"]["ok"])

    def test_stop_interrupts_delay_and_blocks_concurrent_runs(self):
        names = [f"zz{c}{c}" for c in "abcdefgh"]
        self.request("/api/run", {"names": names, "delay": 30, "jitter": 0})
        self.assertEqual(self.request("/api/run", {"names": ["kavo"]})[0], 400)
        started = time.time()
        self.request("/api/run/stop", {})
        s = self.wait_for({"stopped"}, timeout=5)
        self.assertLess(time.time() - started, 5)  # did not sit out the 30s delay
        self.assertLess(s["run"]["done"], len(names))



class BlockedWebTest(WebTest):
    """Same server, but TikTok answers every request with its bot check."""

    fetch = staticmethod(lambda url: (200, '<html><div id="cs" class="x"></div>Please wait...</html>'))

    def test_run_refuses_to_start_when_verify_fails(self):
        self.request("/api/run", {"names": ["kavo", "zqxv"], "delay": 1, "jitter": 0})
        s = self.wait_for({"failed"})
        self.assertFalse(s["run"]["verify"]["ok"])
        self.assertIn("not serving", s["run"]["verify"]["explanation"])
        self.assertEqual(s["run"]["done"], 0)
        self.assertEqual(s["totals"], {})  # nothing was recorded as available

    # The inherited happy-path tests assume a working TikTok; skip them here.
    test_run_to_completion_then_results_and_csv = None
    test_stop_interrupts_delay_and_blocks_concurrent_runs = None
    test_verify_endpoint = None


if __name__ == "__main__":
    unittest.main()

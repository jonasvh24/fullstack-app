import json
import tempfile
import unittest
from pathlib import Path

from tiktok_usernames import generate
import random

from tiktok_usernames.checker import LOGIN_REDIRECT, Blocked, Checker, Status, parse_profile_html
from tiktok_usernames.verify import KNOWN_TAKEN, verify
from tiktok_usernames.rules import is_valid, normalise
from tiktok_usernames.score import is_pronounceable, rarity
from tiktok_usernames.store import Store


def universal_page(detail: dict) -> str:
    data = {"__DEFAULT_SCOPE__": {"webapp.user-detail": detail}}
    return (
        '<html><script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">'
        f"{json.dumps(data)}</script></html>"
    )


TAKEN_PAGE = universal_page({"statusCode": 0, "userInfo": {"user": {"uniqueId": "Kavo"}}})
MISSING_PAGE = universal_page({"statusCode": 10221, "statusMsg": "", "userInfo": {}})


class RulesTest(unittest.TestCase):
    def test_valid_and_invalid(self):
        for ok in ["ab", "kavo", "a.b", "a_b", "x" * 24, "abc123"]:
            self.assertTrue(is_valid(ok), ok)
        for bad in ["a", "x" * 25, "ab.", "a..b", "a-b", "Ab", "a b", "ä€"]:
            self.assertFalse(is_valid(bad), bad)

    def test_normalise(self):
        self.assertEqual(normalise(" @Kavo "), "kavo")


class GenerateTest(unittest.TestCase):
    def test_pattern_shape(self):
        names = generate.from_pattern("CVCV", 100, seed=1)
        self.assertEqual(len(names), 100)
        for n in names:
            self.assertIn(n[0], generate.CONSONANTS)
            self.assertIn(n[1], generate.VOWELS)

    def test_small_space_is_exhaustive(self):
        self.assertEqual(len(generate.from_pattern("LL", 10_000)), 26 * 26)

    def test_large_space_is_sampled_without_duplicates(self):
        names = generate.from_pattern("LLLLLLL", 500, seed=7)
        self.assertEqual(len(names), len(set(names)), 500)

    def test_literals_and_invalid_filtered(self):
        names = generate.from_pattern("xV.", 100)
        self.assertEqual(names, [])  # trailing period is never valid
        self.assertTrue(all(n.startswith("x_") for n in generate.from_pattern("x_V", 10)))

    def test_repeats(self):
        r = generate.repeats(3, 4)
        for n in ["aaa", "zzzz", "abab", "abba", "aabb"]:
            self.assertIn(n, r)
        self.assertTrue(all(3 <= len(n) <= 4 for n in r))


class ScoreTest(unittest.TestCase):
    def test_shorter_is_rarer(self):
        self.assertGreater(rarity("kav"), rarity("kavo"))
        self.assertGreater(rarity("kavo"), rarity("kavoru"))

    def test_clean_beats_cluttered(self):
        self.assertGreater(rarity("kavo"), rarity("kav0"))
        self.assertGreater(rarity("kavo"), rarity("ka_vo"))

    def test_dictionary_bonus(self):
        self.assertGreater(rarity("lion", {"lion"}), rarity("lion"))

    def test_pronounceable(self):
        self.assertTrue(is_pronounceable("kavo"))
        self.assertFalse(is_pronounceable("xkcd"))
        self.assertFalse(is_pronounceable("strk"))

    def test_bounds(self):
        for n in ["ab", "aa", "x" * 24, "1_2.3"]:
            self.assertTrue(0 <= rarity(n, {n}) <= 100)


class ParseTest(unittest.TestCase):
    def test_taken(self):
        status, code, _ = parse_profile_html("kavo", TAKEN_PAGE)
        self.assertEqual((status, code), (Status.TAKEN, 0))

    def test_private_account_counts_as_taken(self):
        page = universal_page({"statusCode": 10222, "userInfo": {"user": {"uniqueId": "kavo"}}})
        self.assertEqual(parse_profile_html("kavo", page)[0], Status.TAKEN)

    def test_no_profile(self):
        status, code, _ = parse_profile_html("kavo", MISSING_PAGE)
        self.assertEqual((status, code), (Status.NO_PROFILE, 10221))

    def test_private_account_without_user_object_is_taken(self):
        page = universal_page({"statusCode": 10222, "statusMsg": "", "userInfo": {}})
        self.assertEqual(parse_profile_html("kavo", page)[:2], (Status.TAKEN, 10222))

    def test_bot_challenge_is_unknown_and_named(self):
        page = '<html><div id="cs" class="eyJ2Ijp7fX0"></div><div id="wci" class="_wafchallengeid"></div>Please wait...</html>'
        status, _, detail = parse_profile_html("kavo", page)
        self.assertEqual(status, Status.UNKNOWN)
        self.assertIn("bot check", detail)

    def test_bot_wall_is_unknown(self):
        self.assertEqual(parse_profile_html("kavo", "<html>captcha</html>")[0], Status.UNKNOWN)

    def test_mismatched_user_is_unknown(self):
        self.assertEqual(parse_profile_html("other", TAKEN_PAGE)[0], Status.UNKNOWN)

    def test_legacy_sigi_state(self):
        page = '<script id="SIGI_STATE">' + json.dumps({"UserModule": {"users": {"kavo": {}}}}) + "</script>"
        self.assertEqual(parse_profile_html("kavo", page)[0], Status.TAKEN)
        self.assertEqual(parse_profile_html("zzqx", page)[0], Status.NO_PROFILE)


class CheckerTest(unittest.TestCase):
    def make(self, responses, **kw):
        calls, sleeps = [], []
        it = iter(responses)

        def fetch(url):
            calls.append(url)
            return next(it)

        return Checker(delay=1, jitter=0, fetch=fetch, sleep=sleeps.append, **kw), calls, sleeps

    def test_taken_and_no_profile(self):
        checker, calls, _ = self.make([(200, TAKEN_PAGE), (200, MISSING_PAGE)])
        self.assertEqual(checker.check("kavo").status, Status.TAKEN)
        self.assertEqual(checker.check("kavo").status, Status.NO_PROFILE)
        self.assertEqual(calls[0], "https://www.tiktok.com/@kavo")

    def test_backs_off_on_429_then_succeeds(self):
        checker, calls, sleeps = self.make([(429, ""), (429, ""), (200, TAKEN_PAGE)])
        self.assertEqual(checker.check("kavo").status, Status.TAKEN)
        self.assertEqual(len(calls), 3)
        self.assertIn(4.0, sleeps)
        self.assertIn(8.0, sleeps)

    def test_login_redirect_is_unknown(self):
        checker, _, _ = self.make([(LOGIN_REDIRECT, "")])
        result = checker.check("kavo")
        self.assertEqual(result.status, Status.UNKNOWN)
        self.assertIn("login", result.detail)

    def test_gives_up_after_retries(self):
        checker, _, _ = self.make([(429, "")] * 4, max_retries=3)
        result = checker.check("kavo")
        self.assertEqual((result.status, result.http_status), (Status.UNKNOWN, 429))

    def test_stops_when_blocked(self):
        checker, _, _ = self.make([(200, "<html>captcha</html>")] * 3, max_consecutive_unknown=3)
        checker.check("aaa")
        checker.check("bbb")
        with self.assertRaises(Blocked):
            checker.check("ccc")

    def test_good_response_resets_block_counter(self):
        pages = [(200, "x"), (200, "x"), (200, TAKEN_PAGE), (200, "x"), (200, "x")]
        checker, _, _ = self.make(pages, max_consecutive_unknown=3)
        for name in ["a1", "a2", "kavo", "a3", "a4"]:
            checker.check(name)  # never three unknowns in a row


def page_for(taken: set[str]):
    def fetch(url):
        name = url.rsplit("@", 1)[1]
        if name in taken:
            return 200, universal_page({"statusCode": 0, "userInfo": {"user": {"uniqueId": name}}})
        return 200, MISSING_PAGE
    return fetch


class VerifyTest(unittest.TestCase):
    def run_verify(self, fetch, debug_dir=None):
        checker = Checker(delay=1, jitter=0, fetch=fetch, sleep=lambda s: None)
        return verify(checker, debug_dir=debug_dir, rng=random.Random(4))

    def test_passes_when_known_accounts_are_taken(self):
        report = self.run_verify(page_for(set(KNOWN_TAKEN)))
        self.assertTrue(report.ok)
        self.assertEqual(len(report.probes), 3)
        self.assertTrue(all(line.startswith("PASS") for line in report.as_dict()["lines"]))

    def test_fails_when_real_accounts_look_free(self):
        with tempfile.TemporaryDirectory() as d:
            report = self.run_verify(page_for(set()), Path(d) / "dbg")
            self.assertFalse(report.ok)
            self.assertIn("format", report.explanation())
            self.assertEqual(len(report.debug_files), 2)  # the two known accounts
            self.assertTrue(all(Path(f).exists() for f in report.debug_files))

    def test_fails_when_tiktok_blocks(self):
        report = self.run_verify(lambda url: (200, "<html>Please wait...</html>"))
        self.assertFalse(report.ok)
        self.assertIn("not serving", report.explanation())

    def test_fails_when_everything_looks_taken(self):
        def fetch(url):
            name = url.rsplit("@", 1)[1]
            return 200, universal_page({"statusCode": 0, "userInfo": {"user": {"uniqueId": name}}})
        report = self.run_verify(fetch)
        self.assertFalse(report.ok)
        self.assertIn("random name", report.explanation())


class StoreTest(unittest.TestCase):
    def test_roundtrip_and_freshness(self):
        from tiktok_usernames.checker import CheckResult

        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            store.save(CheckResult("kavo", Status.NO_PROFILE, 200, 10221), 63)
            store.save(CheckResult("zzz", Status.UNKNOWN, 200), 70)
            self.assertTrue(store.is_fresh("kavo", 1))
            self.assertFalse(store.is_fresh("zzz", 1))  # unknowns always get rechecked
            self.assertFalse(store.is_fresh("nope", 1))
            row = store.rows("likely", 0, "", 10)[0]
            self.assertEqual((row["username"], row["rarity"], row["tiktok_code"]), ("kavo", 63, 10221))
            self.assertEqual(store.counts(), {"no_profile": 1, "unknown": 1})
            store.close()

    def test_confirmations(self):
        from tiktok_usernames.checker import CheckResult

        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            for name, code in [("kavo", 10221), ("lumi", 10221), ("zeyn", 10202)]:
                store.save(CheckResult(name, Status.NO_PROFILE, 200, code), 63)
            store.save(CheckResult("bako", Status.TAKEN, 200, 0), 63)

            self.assertTrue(store.mark("kavo", True))
            self.assertTrue(store.mark("zeyn", False))
            self.assertFalse(store.mark("never", True))  # never checked

            names = lambda view: [r["username"] for r in store.rows(view, 0, "", 50)]
            self.assertEqual(names("confirmed"), ["kavo"])
            self.assertEqual(names("rejected"), ["zeyn"])
            self.assertEqual(names("likely"), ["lumi"])
            counts = store.view_counts()
            self.assertEqual((counts["confirmed"], counts["likely"], counts["taken"], counts["all"]), (1, 1, 1, 4))
            self.assertEqual(store.code_stats(), [
                {"code": 10202, "claimable": 0, "not_claimable": 1},
                {"code": 10221, "claimable": 1, "not_claimable": 0},
            ])

            # A recheck that still finds no profile keeps the confirmation...
            store.save(CheckResult("kavo", Status.NO_PROFILE, 200, 10221), 63)
            self.assertEqual(names("confirmed"), ["kavo"])
            # ...but once someone takes the name, the confirmation is stale and cleared.
            store.save(CheckResult("kavo", Status.TAKEN, 200, 0), 63)
            self.assertEqual(names("confirmed"), [])

            store.mark("zeyn", None)
            self.assertEqual(names("rejected"), [])
            store.close()

    def test_migrates_old_database(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "old.db"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE results (username TEXT PRIMARY KEY, status TEXT NOT NULL, "
                         "rarity INTEGER NOT NULL, http_status INTEGER, tiktok_code INTEGER, "
                         "detail TEXT, checked_at REAL NOT NULL)")
            conn.execute("INSERT INTO results VALUES ('kavo', 'no_profile', 63, 200, 10221, '', 0)")
            conn.commit()
            conn.close()

            store = Store(path)
            self.assertTrue(store.mark("kavo", True))
            self.assertEqual(store.rows("confirmed", 0, "", 5)[0]["username"], "kavo")
            store.close()



class CliMarkTest(unittest.TestCase):
    def test_mark_and_report(self):
        import contextlib
        import io

        from tiktok_usernames.checker import CheckResult
        from tiktok_usernames.cli import main

        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "c.db"
            store = Store(db)
            store.save(CheckResult("kavo", Status.NO_PROFILE, 200, 10221), 63)
            store.close()
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                main(["--db", str(db), "mark", "yes", "@Kavo", "ghost"])
                main(["--db", str(db), "report"])
            text = out.getvalue()
            self.assertIn("@kavo: marked yes", text)
            self.assertIn("@ghost: not checked yet", text)
            self.assertIn("confirmed available=1", text)
            self.assertIn("TikTok code 10221: 1 claimable", text)


if __name__ == "__main__":
    unittest.main()

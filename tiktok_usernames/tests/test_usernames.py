import json
import tempfile
import unittest
from pathlib import Path

from tiktok_usernames import generate
from tiktok_usernames.checker import Blocked, Checker, Status, parse_profile_html
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
            self.assertEqual(store.top(Status.NO_PROFILE, 10)[0][:3], ("kavo", 63, 10221))
            self.assertEqual(store.counts(), {"no_profile": 1, "unknown": 1})
            store.close()


if __name__ == "__main__":
    unittest.main()

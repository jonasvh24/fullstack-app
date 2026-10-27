import tempfile
import unittest
from pathlib import Path

from cleanyourtv_ui.tests.fake_upstream import LIST, build
from cleanyourtv_ui.upstream import Invalid, Upstream, check_host, is_core, parse_list, rewrite_list


class ParseList(unittest.TestCase):
    def test_header_sections_and_optional_entries(self):
        parsed = parse_list(LIST)
        self.assertEqual(parsed.header[0], "Acme TV 1 (AT-100), Android TV 11")
        self.assertEqual(len(parsed.header), 2)
        got = [(e.pkg, e.on, e.section, e.note) for e in parsed.entries]
        self.assertEqual(got, [
            ("com.acme.tracker", True, "Acme telemetry", ""),
            ("com.acme.ads", True, "Acme telemetry", "sponsored row"),
            ("com.acme.music", False, "Optional, remove the # if you never use it", "music app"),
            ("com.android.settings", False, "Never", ""),
        ])
        self.assertTrue(parsed.entries[-1].core)

    def test_prose_comments_are_not_packages(self):
        parsed = parse_list("# Apply: TV_HOST=1.2.3.4 tools/disable.sh x\n# see docs/TROUBLESHOOTING.md\ncom.a.b\n")
        self.assertEqual([e.pkg for e in parsed.entries], ["com.a.b"])

    def test_bom_and_crlf(self):
        parsed = parse_list("﻿# h\r\ncom.a.b\r\n")
        self.assertEqual([e.pkg for e in parsed.entries], ["com.a.b"])

    def test_core(self):
        self.assertTrue(is_core("com.android.providers.media"))
        self.assertFalse(is_core("com.android.providers.calendar"))
        self.assertTrue(is_core("com.mediatek.tv.service"))
        self.assertFalse(is_core("com.acme.ads"))


class RewriteList(unittest.TestCase):
    def test_toggles_keep_comments(self):
        out = rewrite_list(LIST, {"com.acme.tracker", "com.acme.music"}, [], "examples/acme-tv-1/debloat.txt")
        self.assertIn("# Saved by CleanYourTV UI from examples/acme-tv-1/debloat.txt", out.splitlines()[0])
        self.assertIn("\n# com.acme.ads          # sponsored row\n", out)
        self.assertIn("\ncom.acme.music      # music app\n", out)
        self.assertIn("# Acme telemetry", out)
        on = {e.pkg for e in parse_list(out).entries if e.on}
        self.assertEqual(on, {"com.acme.tracker", "com.acme.music"})

    def test_added_and_restamped(self):
        once = rewrite_list(LIST, {"com.new.thing"}, ["com.new.thing", "com.acme.ads"], "a")
        twice = rewrite_list(once, {"com.new.thing"}, [], "b")
        self.assertEqual(twice.count("# Saved by CleanYourTV UI from"), 1)
        self.assertEqual(once.count("com.acme.ads"), 1)  # already in the list, not appended again
        self.assertEqual({e.pkg for e in parse_list(twice).entries if e.on}, {"com.new.thing"})


class Hosts(unittest.TestCase):
    def test_accepts(self):
        for h in ("192.168.1.50", "192.168.1.50:5555", "tv.local", "[fe80::1]:5555", "127.0.0.1:15555"):
            self.assertEqual(check_host(h), h)

    def test_refuses(self):
        for h in ("", "1.2.3.4;reboot", "$(id)", "a b", "1.2.3.4:0", "1.2.3.4:70000", "-x", "http://tv"):
            with self.assertRaises(Invalid, msg=h):
                check_host(h)


class Checkout(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = build(Path(self.tmp.name) / "repo")
        self.up = Upstream(root, Path(self.tmp.name) / "data")

    def tearDown(self):
        self.tmp.cleanup()

    def test_examples(self):
        ex = self.up.examples()
        self.assertEqual([e["slug"] for e in ex], ["acme-tv-1", "xiaomi-mi-tv-4a"])  # no-list has no list
        self.assertEqual(ex[0]["notes"], "see notes")
        self.assertTrue(ex[0]["readme"])

    def test_list_paths_stay_inside(self):
        for bad in ("ex:../acme-tv-1/debloat.txt", "ex:acme-tv-1/../../x.txt", "ex:acme-tv-1/tv.json",
                    "custom:../../etc/passwd", "custom:", "file:/etc/passwd", "ex:acme-tv-1/debloat-x.txt"):
            with self.assertRaises(Invalid, msg=bad):
                self.up.list_path(bad)
        self.assertTrue(self.up.list_path("ex:acme-tv-1/debloat.txt").is_file())

    def test_save_and_delete(self):
        list_id = self.up.save_list("my tv", "ex:acme-tv-1/debloat.txt", ["com.acme.ads"], ["com.extra.one"])
        self.assertEqual(list_id, "custom:my tv")
        detail = self.up.list_detail(list_id)
        self.assertEqual({e["pkg"] for e in detail["entries"] if e["on"]}, {"com.acme.ads", "com.extra.one"})
        self.assertEqual(self.up.custom_lists()[0]["count"], 2)
        with self.assertRaises(Invalid):
            self.up.delete_list("ex:acme-tv-1/debloat.txt")
        self.up.delete_list(list_id)
        self.assertEqual(self.up.custom_lists(), [])

    def test_save_refuses(self):
        with self.assertRaises(Invalid):
            self.up.save_list("../x", None, [], [])
        with self.assertRaises(Invalid):
            self.up.save_list("ok", None, ["rm -rf /"], [])
        with self.assertRaises(Invalid):
            self.up.save_list("ok", None, [], ["com.android.systemui"])

    def test_themes(self):
        self.assertEqual(self.up.themes(), [{"name": "aurora", "about": "A dark aurora wallpaper", "screenshot": True}])
        with self.assertRaises(Invalid):
            self.up.theme_dir("../examples")

    def test_match(self):
        self.assertEqual(self.up.match_examples({"model": "MiTV-MOOQ1", "device": "tarzan", "manufacturer": "Xiaomi"}),
                         ["xiaomi-mi-tv-4a"])
        self.assertEqual(self.up.match_examples({"model": "AT-100", "manufacturer": "Nobody"}), ["acme-tv-1"])
        self.assertEqual(self.up.match_examples({"model": "zzzz"}), [])

    def test_actions(self):
        title, steps = self.up.action("disable", {"list": "ex:acme-tv-1/debloat.txt"})
        self.assertEqual(steps[0][:2], ["bash", "tools/disable.sh"])
        self.assertTrue(steps[0][2].endswith("examples/acme-tv-1/debloat.txt"))
        _, steps = self.up.action("fix", {"list": "ex:acme-tv-1/debloat.txt"})
        self.assertEqual(steps[0][1:4], ["-I", "tools/check.py", "--fix"])
        _, steps = self.up.action("adb-closed", {"host": "evil;id"}, "192.168.1.50")
        self.assertEqual(steps[0][-1], "192.168.1.50")
        with self.assertRaises(Invalid):
            self.up.action("rm", {})


if __name__ == "__main__":
    unittest.main()

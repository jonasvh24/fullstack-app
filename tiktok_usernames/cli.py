"""Command-line entry point.

    python -m tiktok_usernames generate --pattern CVCV --limit 50
    python -m tiktok_usernames check --length 4 --limit 200 --delay 4
    python -m tiktok_usernames report --top 30
    python -m tiktok_usernames verify           # are checks reading real TikTok?
    python -m tiktok_usernames serve            # web UI on http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from . import candidates
from .checker import Blocked, Checker, Status
from .rules import normalise
from .store import Store
from .verify import verify

DEBUG_DIR = Path("tiktok_debug")

DEFAULT_DB = Path("tiktok_usernames.db")


def _add_source_args(p: argparse.ArgumentParser) -> None:
    src = p.add_argument_group("candidate source (combine freely)")
    src.add_argument("--pattern", action="append", default=[],
                     help="C=consonant V=vowel L=letter D=digit A=alnum, e.g. CVCV. Repeatable.")
    src.add_argument("--length", type=int, action="append", default=[],
                     help="shortcut for an all-letters pattern of this length. Repeatable.")
    src.add_argument("--wordlist", type=Path, help="dictionary file, one word per line")
    src.add_argument("--repeats", action="store_true", help="include aaa / abab / abba / aabb shapes")
    src.add_argument("--file", type=Path, help="your own candidate list, one per line")
    p.add_argument("--limit", type=int, default=200, help="max candidates per pattern (default 200)")
    p.add_argument("--min-rarity", type=int, default=0, help="drop candidates scoring below this")
    p.add_argument("--seed", type=int, help="fix random sampling for reproducible runs")


def _candidates(args: argparse.Namespace) -> list[tuple[str, int]]:
    def lines(path: Path | None) -> list[str]:
        return path.read_text(encoding="utf-8", errors="ignore").split() if path else []

    spec = candidates.Spec(
        patterns=args.pattern,
        lengths=args.length,
        words=lines(args.wordlist),
        repeats=args.repeats,
        names=lines(args.file),
        limit=args.limit,
        min_rarity=args.min_rarity,
        seed=args.seed,
    )
    if not (spec.patterns or spec.lengths or spec.words or spec.repeats or spec.names):
        sys.exit("No candidates: pass at least one of --pattern, --length, --wordlist, --repeats, --file.")
    return candidates.build(spec)


def cmd_generate(args: argparse.Namespace) -> None:
    for name, score in _candidates(args):
        print(f"{score:3d}  {name}")


def cmd_check(args: argparse.Namespace) -> None:
    candidates = _candidates(args)
    store = Store(args.db)
    checker = Checker(delay=args.delay, jitter=args.jitter)

    todo = [(n, s) for n, s in candidates if not store.is_fresh(n, args.max_age_hours)]
    skipped = len(candidates) - len(todo)
    eta_min = len(todo) * (args.delay + args.jitter / 2) / 60
    print(f"{len(todo)} to check, {skipped} cached. ETA ~{eta_min:.0f} min.", file=sys.stderr)
    if todo and not args.skip_verify and not _verify_or_explain(checker):
        sys.exit(1)

    found = []
    try:
        for i, (name, score) in enumerate(todo, 1):
            result = checker.check(name)
            store.save(result, score)
            marker = {"taken": " ", "no_profile": "*", "unknown": "?"}[result.status.value]
            code = f" code={result.tiktok_code}" if result.tiktok_code not in (None, 0) else ""
            print(f"[{i}/{len(todo)}] {marker} {score:3d} {name:<24} {result.status.value}{code}")
            if result.status is Status.NO_PROFILE:
                found.append((name, score, result.tiktok_code))
    except Blocked as err:
        store.save(err.result, dict(todo)[err.result.username])
        print(f"\nStopped: {err}", file=sys.stderr)
    except KeyboardInterrupt:
        print("\nInterrupted; results so far are saved.", file=sys.stderr)
    finally:
        store.close()

    print(f"\n{len(found)} without a public profile this run. "
          "Confirm each in the TikTok app before counting on it.", file=sys.stderr)
    if args.out and found:
        with args.out.open("w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["username", "rarity", "tiktok_code"])
            writer.writerows(found)
        print(f"Wrote {args.out}", file=sys.stderr)


def _verify_or_explain(checker: Checker) -> bool:
    print("Verifying against real TikTok accounts...", file=sys.stderr)
    report = verify(checker, debug_dir=DEBUG_DIR)
    for line in report.as_dict()["lines"]:
        print(f"  {line}", file=sys.stderr)
    print(report.explanation(), file=sys.stderr)
    for path in report.debug_files:
        print(f"  Saved the page TikTok returned: {path}", file=sys.stderr)
    return report.ok


def cmd_verify(args: argparse.Namespace) -> None:
    if not _verify_or_explain(Checker(delay=args.delay, jitter=0)):
        sys.exit(1)


def cmd_report(args: argparse.Namespace) -> None:
    store = Store(args.db)
    v = store.view_counts()
    if not v["all"]:
        print("No results yet.")
    else:
        print(f"confirmed available={v['confirmed']}  likely available={v['likely']}  "
              f"not claimable={v['rejected']}  taken={v['taken']}  unknown={v['unknown']}")
    for view, title in (("confirmed", "Confirmed available (you checked in the app)"),
                        ("likely", "Likely available (no public profile; confirm in the app)")):
        rows = store.rows(view, args.min_rarity, "", args.top)
        if rows:
            print(f"\n{title}\n{'rarity':>6}  {'username':<24} {'code':>6}")
            for r in rows:
                code = r["tiktok_code"] if r["tiktok_code"] is not None else "-"
                print(f"{r['rarity']:>6}  {r['username']:<24} {code:>6}")
    for s in store.code_stats():
        print(f"\nTikTok code {s['code']}: {s['claimable']} claimable, {s['not_claimable']} not, of those you confirmed")
    store.close()


def cmd_mark(args: argparse.Namespace) -> None:
    claimable = {"yes": True, "no": False, "clear": None}[args.verdict]
    store = Store(args.db)
    try:
        for raw in args.usernames:
            name = normalise(raw)
            ok = store.mark(name, claimable)
            print(f"@{name}: {'marked ' + args.verdict if ok else 'not checked yet, skipped'}")
    finally:
        store.close()


def cmd_serve(args: argparse.Namespace) -> None:
    from .web import serve

    serve(args.db, args.host, args.port)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="tiktok_usernames",
        description="Find rare TikTok handles with no public profile.",
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"results cache (default {DEFAULT_DB})")
    sub = parser.add_subparsers(dest="command", required=True)

    p_gen = sub.add_parser("generate", help="list scored candidates without any network calls")
    _add_source_args(p_gen)
    p_gen.set_defaults(func=cmd_generate)

    p_chk = sub.add_parser("check", help="check candidates against TikTok")
    _add_source_args(p_chk)
    p_chk.add_argument("--delay", type=float, default=3.0, help="seconds between requests (default 3)")
    p_chk.add_argument("--jitter", type=float, default=1.5, help="random extra delay, seconds (default 1.5)")
    p_chk.add_argument("--max-age-hours", type=float, default=72,
                       help="recheck cached names older than this (default 72)")
    p_chk.add_argument("--out", type=Path, help="write this run's no-profile hits to CSV")
    p_chk.add_argument("--skip-verify", action="store_true",
                       help="don't check known accounts first (not recommended)")
    p_chk.set_defaults(func=cmd_check)

    p_ver = sub.add_parser("verify", help="prove checks work: known accounts must come back taken")
    p_ver.add_argument("--delay", type=float, default=3.0, help="seconds between the three requests")
    p_ver.set_defaults(func=cmd_verify)

    p_mark = sub.add_parser("mark", help="record whether TikTok's app accepted a username")
    p_mark.add_argument("verdict", choices=["yes", "no", "clear"],
                        help="yes = the app let you claim it, no = it refused, clear = forget")
    p_mark.add_argument("usernames", nargs="+")
    p_mark.set_defaults(func=cmd_mark)

    p_rep = sub.add_parser("report", help="show confirmed and likely available handles")
    p_rep.add_argument("--top", type=int, default=25)
    p_rep.add_argument("--min-rarity", type=int, default=0)
    p_rep.set_defaults(func=cmd_report)

    p_srv = sub.add_parser("serve", help="run the local web UI")
    p_srv.add_argument("--host", default="127.0.0.1",
                       help="interface to bind (default 127.0.0.1, this machine only)")
    p_srv.add_argument("--port", type=int, default=8000)
    p_srv.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    if getattr(args, "delay", 3.0) < 1.0:
        parser.error("--delay below 1s will get you blocked; keep it at 1 or above.")
    try:
        args.func(args)
    except BrokenPipeError:
        # Output piped into head/less that closed early; not an error.
        sys.stderr.close()

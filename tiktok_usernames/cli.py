"""Command-line entry point.

    python -m tiktok_usernames generate --pattern CVCV --limit 50
    python -m tiktok_usernames check --length 4 --limit 200 --delay 4
    python -m tiktok_usernames report --top 30
    python -m tiktok_usernames serve            # web UI on http://127.0.0.1:8000
"""
import argparse
import csv
import sys
from pathlib import Path

from . import candidates
from .checker import Blocked, Checker, Status
from .store import Store

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


def cmd_report(args: argparse.Namespace) -> None:
    store = Store(args.db)
    counts = store.counts()
    print("  ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "No results yet.")
    rows = store.top(Status.NO_PROFILE, args.top, args.min_rarity)
    if rows:
        print(f"\n{'rarity':>6}  {'username':<24} {'code':>6}  checked (UTC)")
        for name, score, code, checked in rows:
            print(f"{score:>6}  {name:<24} {code if code is not None else '-':>6}  {checked}")
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
    p_chk.set_defaults(func=cmd_check)

    p_rep = sub.add_parser("report", help="show the rarest no-profile handles found so far")
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

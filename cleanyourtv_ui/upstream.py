"""Everything the UI knows about a CleanYourTV checkout: lists, themes, and the commands it runs.

The UI never reimplements a step. Disabling, restoring, drift checks, themes and the launcher all go
through the checkout's own scripts, because that is where the backup, the Home guard and the recovery
record live. This module only reads the checkout and builds argv lists for those scripts.
"""
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

UPSTREAM_URL = "https://github.com/Gathin23/cleanyourtv"

# Same shape check as tools/disable.sh: a list entry is pasted into a shell command on the TV.
PACKAGE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")
# Same as CORE_RE / CORE_OK in tools/disable.sh. disable.sh refuses these anyway; the UI marks them
# up front so a list is never saved in a shape the script will reject.
CORE_RE = re.compile(
    r"^(android|android\.ext\.(services|shared)|com\.android\.systemui|com\.android\.shell|com\.android\.phone"
    r"|com\.android\.settings|com\.android\.tv\.settings"
    r"|com\.(google\.)?android\.(permissioncontroller|packageinstaller|ext\.services|ext\.shared|modulemetadata)"
    r"|com\.google\.android\.overlay\.modules.*|com\.android\.providers\..*|com\.cltv\.mal"
    r"|com\.mediatek\.(tis|tv\.service|tv\.agent))$"
)
CORE_OK = re.compile(r"^com\.android\.providers\.(calendar|contacts|userdictionary|blockednumber)$")

# One package per line, optionally commented out, optionally with a trailing note. A commented line
# only counts as a package when the package is all there is, so "# Apply: TV_HOST=..." stays prose.
_LINE_RE = re.compile(r"^(?P<off>#\s*)?(?P<pkg>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+)\s*(?:#\s*(?P<note>.*))?$")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,120}$")
_FILE_RE = re.compile(r"^debloat[a-z0-9._-]*\.txt$")
_CUSTOM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,59}$")
# TV_HOST as tools/tv.py reads it: an IPv4 address or host name with an optional port, or [ipv6]:port.
_HOST_RE = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?|\[[0-9A-Fa-f:.]+\])(?::(?P<port>\d{1,5}))?$")
# Remote keys accepted by tools/tv.py --screenshot (KEY_NAME there), limited to what the remote shows.
REMOTE_KEYS = {
    "HOME", "BACK", "MENU", "DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT", "DPAD_CENTER",
    "VOLUME_UP", "VOLUME_DOWN", "VOLUME_MUTE", "MEDIA_PLAY_PAUSE", "SETTINGS", "TV_INPUT", "POWER",
}
# Overrides that switch off a safety check in disable.sh / check.py. The UI never passes them on.
SAFETY_OVERRIDES = ("FORCE_SETUPWRAITH", "FORCE_HOME", "TVINPUT_ALLOW", "HANDLERS_ALLOW")


class Invalid(ValueError):
    """A request the UI refuses before anything runs."""


def is_core(pkg: str) -> bool:
    return bool(CORE_RE.match(pkg)) and not CORE_OK.match(pkg)


def check_host(host: str) -> str:
    host = (host or "").strip()
    m = _HOST_RE.match(host)
    if not m:
        raise Invalid("TV address must be an IP address or host name, optionally with :port")
    if m.group("port") and not 1 <= int(m.group("port")) <= 65535:
        raise Invalid("port must be between 1 and 65535")
    return host


# ---------------------------------------------------------------- debloat lists

@dataclass
class Entry:
    pkg: str
    on: bool
    note: str
    section: str
    core: bool


@dataclass
class ParsedList:
    header: list[str] = field(default_factory=list)
    entries: list[Entry] = field(default_factory=list)


def parse_list(text: str) -> ParsedList:
    """Split a debloat list into its leading comment block and its package entries.

    A package line that is commented out is an entry that is off: lists use that for "optional,
    remove the # if you don't use it". Each entry carries the comment heading above it, which is how
    contributors group their lists ("# Xiaomi telemetry", "# Preloaded apps nobody asked for").
    """
    out = ParsedList()
    section, in_header, prev_blank = "", True, True
    for raw in text.lstrip("\ufeff").splitlines():
        line = raw.strip()
        if not line:
            in_header, prev_blank = False, True
            continue
        m = _LINE_RE.match(line)
        if m:
            pkg = m.group("pkg")
            out.entries.append(Entry(pkg, not m.group("off"), (m.group("note") or "").strip(), section, is_core(pkg)))
            in_header = False
        elif line.startswith("#"):
            words = line.lstrip("#").strip()
            if in_header:
                out.header.append(words)
            elif prev_blank and words:
                section = words
        prev_blank = False
    return out


def rewrite_list(text: str, wanted_on: set[str], added: list[str], source: str) -> str:
    """The list text with each package line switched on or off to match `wanted_on`.

    Comments, grouping and notes stay as the contributor wrote them: an entry turned off gets a "# "
    in front, one turned on loses it. Packages not in the list yet go in a section of their own.
    """
    lines, present = [], set()
    for raw in text.lstrip("﻿").splitlines():
        m = _LINE_RE.match(raw.strip())
        if m:
            pkg = m.group("pkg")
            present.add(pkg)
            body = raw.strip()
            if m.group("off") and pkg in wanted_on:
                body = body[len(m.group("off")):]
            elif not m.group("off") and pkg not in wanted_on:
                body = "# " + body
            lines.append(body)
        else:
            lines.append(raw.rstrip())
    new = [p for p in dict.fromkeys(added) if p not in present]
    if new:
        lines += ["", f"# Added in CleanYourTV UI ({date.today().isoformat()})"] + new
    stamp = "# Saved by CleanYourTV UI from "
    lines = [f"{stamp}{source} on {date.today().isoformat()}"] + [line for line in lines if not line.startswith(stamp)]
    return "\n".join(lines).rstrip() + "\n"


def check_packages(names: list[str]) -> list[str]:
    bad = [n for n in names if not PACKAGE_RE.match(n)]
    if bad:
        raise Invalid("not a package name: " + ", ".join(bad[:5]))
    core = [n for n in names if is_core(n)]
    if core:
        raise Invalid("core Android or TV middleware, the TV may not boot without it: " + ", ".join(core[:5]))
    return names


# ---------------------------------------------------------------- the checkout

class Upstream:
    def __init__(self, root: Path, data: Path):
        self.root = root.resolve()
        self.data = data
        self.lists_dir = data / "lists"
        self._examples: list[dict] | None = None

    # -- state

    @property
    def present(self) -> bool:
        return (self.root / "tools" / "tv.py").is_file() and (self.root / "examples").is_dir()

    @property
    def python(self) -> Path:
        for p in (self.root / "tools" / "venv" / "bin" / "python",
                  self.root / "tools" / "venv" / "Scripts" / "python.exe"):
            if p.is_file():
                return p
        return self.root / "tools" / "venv" / "bin" / "python"

    @property
    def ready(self) -> bool:
        return self.present and self.python.is_file()

    def info(self) -> dict:
        head = None
        ref = self.root / ".git" / "HEAD"
        try:
            text = ref.read_text().strip()
            if text.startswith("ref: "):
                text = (self.root / ".git" / text[5:]).read_text().strip()
            head = text[:10]
        except OSError:
            pass
        return {"root": str(self.root), "present": self.present, "ready": self.ready, "commit": head,
                "url": UPSTREAM_URL, "lists_dir": str(self.lists_dir)}

    # -- lists

    def examples(self) -> list[dict]:
        if self._examples is None:
            found = []
            for folder in sorted((self.root / "examples").iterdir()) if self.present else []:
                if not folder.is_dir() or not _SLUG_RE.match(folder.name):
                    continue
                files = sorted(f.name for f in folder.iterdir() if _FILE_RE.match(f.name))
                if not files:
                    continue
                try:
                    meta = json.loads((folder / "tv.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    meta = {}
                found.append({
                    "slug": folder.name,
                    "tv": str(meta.get("tv") or folder.name),
                    "brand": str(meta.get("brand") or ""),
                    "os": str(meta.get("os") or ""),
                    "notes": re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", str(meta.get("notes") or "")),
                    "files": files,
                    "readme": (folder / "README.md").is_file(),
                })
            self._examples = found
        return self._examples

    def reset(self) -> None:
        """Forget what was read from the checkout, after setup or an update changed it."""
        self._examples = None

    def match_examples(self, tv: dict) -> list[str]:
        """Example folders that look like the probed TV, best first.

        tv.json names the set the way its contributor saw it ("Xiaomi Mi TV 4A (MiTV-MOOQ1)"), so the
        model and device strings from getprop are looked for inside it; the brand alone is the fallback.
        """
        def norm(s: str) -> str:
            return re.sub(r"[^a-z0-9]", "", s.lower())

        model, device = norm(tv.get("model", "")), norm(tv.get("device", ""))
        maker = norm(tv.get("manufacturer", ""))
        scored = []
        for ex in self.examples():
            hay = norm(ex["tv"] + ex["slug"])
            score = (3 if len(model) >= 4 and model in hay else 0) + (2 if len(device) >= 4 and device in hay else 0)
            if maker and maker in norm(ex["brand"] + ex["slug"]):
                score += 1
            if score:
                scored.append((-score, ex["slug"]))
        scored.sort()
        best = [slug for s, slug in scored if s <= -2][:6]
        return best or [slug for _, slug in scored][:6]

    def custom_lists(self) -> list[dict]:
        if not self.lists_dir.is_dir():
            return []
        out = []
        for f in sorted(self.lists_dir.glob("*.txt")):
            entries = parse_list(f.read_text(encoding="utf-8", errors="replace")).entries
            out.append({"id": f"custom:{f.stem}", "name": f.stem, "count": sum(e.on for e in entries),
                        "modified": f.stat().st_mtime})
        return out

    def list_path(self, list_id: str) -> Path:
        """The file behind a list id, refusing anything outside examples/ or the UI's own lists folder."""
        kind, _, rest = (list_id or "").partition(":")
        if kind == "ex":
            slug, _, name = rest.partition("/")
            if not _SLUG_RE.match(slug) or not _FILE_RE.match(name):
                raise Invalid("unknown list")
            path = self.root / "examples" / slug / name
        elif kind == "custom":
            if not _CUSTOM_RE.match(rest):
                raise Invalid("unknown list")
            path = self.lists_dir / f"{rest}.txt"
        else:
            raise Invalid("unknown list")
        if not path.is_file():
            raise Invalid("list not found")
        return path

    def list_detail(self, list_id: str) -> dict:
        path = self.list_path(list_id)
        text = path.read_text(encoding="utf-8", errors="replace")
        parsed = parse_list(text)
        readme = None
        if list_id.startswith("ex:"):
            note = path.parent / "README.md"
            if note.is_file():
                readme = note.read_text(encoding="utf-8", errors="replace")[:20_000]
        return {
            "id": list_id,
            "path": str(path),
            "header": parsed.header,
            "entries": [e.__dict__ for e in parsed.entries],
            "readme": readme,
            "recovery_record": self.has_record(path),
        }

    def save_list(self, name: str, base_id: str | None, on: list[str], added: list[str]) -> str:
        name = (name or "").strip()
        if not _CUSTOM_RE.match(name):
            raise Invalid("name: letters, digits, spaces, dot, dash or underscore, up to 60 characters")
        check_packages(on)
        check_packages(added)
        text, source = "", "scratch"
        if base_id:
            text = self.list_path(base_id).read_text(encoding="utf-8", errors="replace")
            source = base_id.replace("ex:", "examples/").replace("custom:", "my list ")
        body = rewrite_list(text, set(on) | set(added), added, source)
        self.lists_dir.mkdir(parents=True, exist_ok=True)
        (self.lists_dir / f"{name}.txt").write_text(body, encoding="utf-8")
        return f"custom:{name}"

    def delete_list(self, list_id: str) -> None:
        if not list_id.startswith("custom:"):
            raise Invalid("only your own lists can be deleted")
        self.list_path(list_id).unlink()

    @staticmethod
    def has_record(list_path: Path) -> bool:
        """Whether tools/recovery.py has a record for this list, under any TV address.

        Same naming as recovery.record_name(): recovery-<tv>-<sha1 of the list's absolute path>.json.
        """
        digest = hashlib.sha1(str(list_path.resolve()).encode()).hexdigest()[:8]
        folder = Path.home() / ".cleanyourtv"
        return folder.is_dir() and any(folder.glob(f"recovery-*-{digest}.json"))

    # -- themes

    def themes(self) -> list[dict]:
        if not self.present:
            return []
        about = {}
        readme = self.root / "themes" / "README.md"
        if readme.is_file():
            for line in readme.read_text(encoding="utf-8", errors="replace").splitlines():
                m = re.match(r"^\| `([a-z0-9._-]+)/` \| (.+?) \| <img", line)
                if m:
                    about[m.group(1)] = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", m.group(2).replace("`", ""))
        out = []
        for folder in sorted((self.root / "themes").iterdir()):
            if folder.is_dir() and _SLUG_RE.match(folder.name) and (folder / "config.json").is_file():
                out.append({"name": folder.name, "about": about.get(folder.name, ""),
                            "screenshot": (folder / "screenshot.jpg").is_file()})
        return out

    def theme_dir(self, name: str) -> Path:
        if not _SLUG_RE.match(name or "") or not (self.root / "themes" / name / "config.json").is_file():
            raise Invalid("unknown theme")
        return self.root / "themes" / name

    # -- commands (argv, run from the checkout root)

    def _py(self, script: str, *args: str) -> list[str]:
        return [str(self.python), "-I", f"tools/{script}", *args]

    def setup_steps(self) -> list[list[str]]:
        venv = str(self.root / "tools" / "venv")
        pip = self.root / "tools" / "venv" / "bin" / "pip"
        return [[sys.executable, "-m", "venv", venv],
                [str(pip), "install", "--disable-pip-version-check", "-r", "tools/requirements.txt"]]

    def action(self, name: str, body: dict, host: str = "") -> tuple[str, list[list[str]]]:
        """(title, argv steps) for a job the page asked for."""
        if name == "setup":
            return "Set up tools/venv", self.setup_steps()
        if not self.ready:
            raise Invalid("Set up the tools first (Setup on the Connect page).")
        if name in ("disable", "restore", "check", "fix"):
            path = str(self.list_path(body.get("list", "")))
            return {
                "disable": (f"Disable {Path(path).name}", [["bash", "tools/disable.sh", path]]),
                "restore": (f"Restore {Path(path).name}", [["bash", "tools/restore.sh", path]]),
                "check": (f"Check drift for {Path(path).name}", [self._py("check.py", path)]),
                "fix": (f"Fix drift for {Path(path).name}", [self._py("check.py", "--fix", path)]),
            }[name]
        if name == "privacy":
            return "Privacy audit", [self._py("privacy.py")]
        if name == "privacy-write":
            # privacy.py refuses to overwrite a file, so each run gets its own name.
            stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
            self.lists_dir.mkdir(parents=True, exist_ok=True)
            target = self.lists_dir / f"privacy-{stamp}.txt"
            extra = ["--include-low"] if body.get("include_low") else []
            return f"Privacy list privacy-{stamp}", [self._py("privacy.py", "--write", str(target), *extra)]
        if name == "theme":
            folder = self.theme_dir(body.get("theme", ""))
            return f"Push theme {folder.name}", [["bash", "tools/theme.sh", str(folder)]]
        if name == "theme-reset":
            return "Reset theme", [["bash", "tools/theme.sh", "--reset"]]
        if name == "launcher":
            return "Build and install launcher", [["bash", "launcher/build-and-install.sh"]]
        if name == "adb-closed":
            return "Check ADB is closed", [self._py("adb-closed.py", check_host(host))]
        raise Invalid("unknown action")

    def tv_cmd(self, *args: str) -> list[str]:
        return self._py("tv.py", *args)

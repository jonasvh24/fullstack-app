"""A CleanYourTV checkout in miniature: the scripts the UI runs, answering like a TV would."""
import os
import sys
import textwrap
from pathlib import Path

TV_PY = '''
import os, sys
args = sys.argv[1:]
if args == ["--probe"]:
    if os.environ["TV_HOST"].startswith("10.9."):
        print("Could not connect to 10.9.9.9:5555: timed out", file=sys.stderr); sys.exit(2)
    for k, v in [("ro.product.model", "MiTV-MOOQ1"), ("ro.product.device", "tarzan"),
                 ("ro.build.version.release", "10"), ("ro.build.version.sdk", "29"),
                 ("ro.build.display.id", "QTG3.200305.006"), ("ro.product.manufacturer", "Xiaomi")]:
        print(f"{k} = {v}")
elif args == ["--batch"]:
    for line in sys.stdin:
        line = line.strip()
        print("$ " + line)
        if line == "pm list packages":
            print("package:com.acme.ads\\npackage:com.acme.tracker\\npackage:com.android.settings")
        elif line == "pm list packages -d":
            print("package:com.acme.tracker")
elif args[:1] == ["--screenshot"]:
    open(args[1], "wb").write(b"\\x89PNG\\r\\n\\x1a\\nfake")
    print("screenshot", args[1], "keys", args[2:])
else:
    print("shell:", " ".join(args))
'''

SCRIPT = '''#!/usr/bin/env bash
echo "{name} $*"
echo "TV_HOST=$TV_HOST FORCE_HOME=${{FORCE_HOME:-unset}}"
{extra}
'''

LIST = textwrap.dedent("""\
    # Acme TV 1 (AT-100), Android TV 11
    # Apply: TV_HOST=<tv ip> tools/disable.sh examples/acme-tv-1/debloat.txt

    # Acme telemetry
    com.acme.tracker
    com.acme.ads          # sponsored row

    # Optional, remove the # if you never use it
    # com.acme.music      # music app

    # Never
    # com.android.settings
""")


def build(root: Path) -> Path:
    tools = root / "tools"
    (tools / "venv" / "bin").mkdir(parents=True)
    os.symlink(sys.executable, tools / "venv" / "bin" / "python")
    (tools / "tv.py").write_text(TV_PY)
    (tools / "requirements.txt").write_text("")
    scripts = {
        "disable.sh": "",
        "restore.sh": "",
        "theme.sh": "",
        "slow.sh": "sleep 30",
    }
    for name, extra in scripts.items():
        (tools / name).write_text(SCRIPT.format(name=name, extra=extra))
    for name in ("check.py", "privacy.py", "adb-closed.py"):
        (tools / name).write_text(f"import sys\nprint('{name}', sys.argv[1:])\nsys.exit(1 if '--fail' in sys.argv else 0)\n")
    ex = root / "examples" / "acme-tv-1"
    ex.mkdir(parents=True)
    (ex / "tv.json").write_text('{"tv": "Acme TV 1 (AT-100)", "brand": "Acme", "os": "Android TV 11", '
                                '"notes": "see [notes](acme-tv-1/README.md)"}')
    (ex / "debloat.txt").write_text(LIST)
    (ex / "README.md").write_text("# Acme notes\n")
    mi = root / "examples" / "xiaomi-mi-tv-4a"
    mi.mkdir()
    (mi / "tv.json").write_text('{"tv": "Xiaomi Mi TV 4A (MiTV-MOOQ1)", "brand": "Xiaomi"}')
    (mi / "debloat.txt").write_text("com.miui.tv.analytics\n")
    (root / "examples" / "no-list").mkdir()
    theme = root / "themes" / "aurora"
    theme.mkdir(parents=True)
    (theme / "config.json").write_text("{}")
    (theme / "screenshot.jpg").write_bytes(b"\xff\xd8jpeg")
    (root / "themes" / "README.md").write_text(
        "| Theme | What it does | Screenshot |\n|---|---|---|\n"
        "| `aurora/` | A dark [aurora](x) wallpaper | <img src=\"aurora/screenshot.jpg\"> |\n")
    (root / "launcher").mkdir()
    (root / "launcher" / "build-and-install.sh").write_text(SCRIPT.format(name="launcher", extra=""))
    return root

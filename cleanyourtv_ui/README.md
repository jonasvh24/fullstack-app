# cleanyourtv_ui

A local web console for [CleanYourTV](https://github.com/Gathin23/cleanyourtv): connect to an Android TV
over Wi-Fi, pick the tested debloat list for your model, untick what you use, disable the rest, push a
theme and drive the TV with an on-screen remote. Python 3.10+, standard library only, no build step.

It does not reimplement anything. Every change to the TV runs CleanYourTV's own scripts
(`tools/disable.sh`, `restore.sh`, `check.py`, `theme.sh`, `launcher/build-and-install.sh`), because the
backup, the Home guard and the recovery record live there. The console is a front end for a checkout of
that repo, not a fork of it.

## Quick start

```bash
cleanyourtv_ui/start.sh          # clones CleanYourTV, sets up its tools, serves http://localhost:8010
```

Or against a checkout you already have:

```bash
python -m cleanyourtv_ui --repo ~/src/cleanyourtv --tv 192.168.1.50
```

| Flag | Default | What it does |
|---|---|---|
| `--repo` | `$CLEANYOURTV_DIR`, else `cleanyourtv_ui/upstream` | The CleanYourTV checkout to drive |
| `--tv` | `$TV_HOST` | The TV's IP address; can also be set on the Connect page |
| `--data` | `~/.cleanyourtv/ui` | Your own lists and the last screenshot |
| `--host`, `--port` | `127.0.0.1`, `8010` | Where the console listens |

Linux and macOS: the scripts it runs are bash. On Windows use WSL, or CleanYourTV's PowerShell scripts by hand.

## The four steps

| Page | What it does | Runs |
|---|---|---|
| **Connect** | Sets up `tools/venv`, probes the TV (model, Android, build), suggests lists from owners of the same model. Checks the ADB port is closed when you finish. | `tv.py --probe`, `adb-closed.py` |
| **Debloat** | 341 tested lists, searchable by model and brand. **Compare with TV** marks each package enabled, disabled or not installed. Untick to keep, add your own, save as a list. Then Disable, Check drift, Fix drift or Restore. A privacy audit covers TVs with no list. | `disable.sh`, `restore.sh`, `check.py [--fix]`, `privacy.py [--write]` |
| **Home screen** | Builds and installs the launcher; theme gallery with screenshots, one click to push or reset. | `build-and-install.sh`, `theme.sh` |
| **Remote** | D-pad, Back, Home, volume and media keys, with a screenshot after each press. Keyboard arrows, Enter, Backspace and H work too. | `tv.py --screenshot`, `input keyevent` |

Script output streams into the console panel at the bottom, one job at a time, with Stop. The Activity
page keeps the last 20 runs.

## Your lists

Tested lists run as they are, straight from `examples/`. Change one and the console saves a copy under
`~/.cleanyourtv/ui/lists/`, keeping the contributor's comments and grouping: an unticked package is
commented out, an added one goes in its own section. Restore needs the same file the list ran from, so
keep a list until you have restored the TV with it.

## Safety

| Guard | Why |
|---|---|
| Core Android and TV middleware packages are locked off | Same `CORE_RE` as `disable.sh`, which refuses them anyway: the TV may not boot without them |
| `FORCE_HOME`, `FORCE_SETUPWRAITH`, `TVINPUT_ALLOW`, `HANDLERS_ALLOW` are stripped from every script's environment | The console never switches off a CleanYourTV safety check, even if your shell has one exported |
| Lists, themes and keys are referenced by id, never by path | Nothing outside `examples/`, `themes/` or your lists folder can be passed to a script |
| Binds to `127.0.0.1`, refuses foreign `Host` headers and non-JSON POSTs | A web page you happen to have open cannot drive your TV |

`--host 0.0.0.0` exposes the console to your network with no login. Only do that on a network you trust.

## Tests

```bash
python -m unittest discover -s cleanyourtv_ui/tests -t .
```

The tests run against a fake checkout (`tests/fake_upstream.py`) whose `tv.py` answers like a TV, so they
need neither the real repo nor a TV.

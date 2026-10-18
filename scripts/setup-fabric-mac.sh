#!/usr/bin/env bash
# setup-fabric-mac.sh — install Java (Temurin 25 LTS), Fabric Loader and Fabric API on macOS.
#
# Usage:
#   bash setup-fabric-mac.sh            # latest stable Minecraft release
#   bash setup-fabric-mac.sh 1.21.8     # pin a specific Minecraft version
#
# Safe to re-run: each step checks what is already there and skips it.
# Written for macOS's stock bash 3.2, so no bash 4+ features.

set -euo pipefail

MC_VERSION="${1:-}"
JAVA_MAJOR=25
MC_DIR="$HOME/Library/Application Support/minecraft"
WORK_DIR="$(mktemp -d)"
UA="setup-fabric-mac/1.0 (personal script)"   # Modrinth asks for an identifying User-Agent
trap 'rm -rf "$WORK_DIR"' EXIT

say()  { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '    \033[32mok\033[0m  %s\n' "$*"; }
die()  { printf '\n\033[1;31mxx\033[0m  %s\n' "$*" >&2; exit 1; }

# macOS ships no jq and python3 may trigger the Xcode CLT prompt, but JavaScript for
# Automation is always present. Usage: json_eval '<js expression over d>' <<< "$json"
json_eval() {
  osascript -l JavaScript -e '
    function run(argv) {
      var d = JSON.parse(argv[0]);
      var r = (function (d) { return eval(argv[1]); })(d);
      return r === undefined || r === null ? "" : String(r);
    }' "$(cat)" "$1"
}

[ "$(uname -s)" = "Darwin" ] || die "This script is for macOS only."

# --- 1. Homebrew -----------------------------------------------------------------
say "Checking Homebrew"
if ! command -v brew >/dev/null 2>&1; then
  for p in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    [ -x "$p" ] && eval "$("$p" shellenv)" && break
  done
fi
if ! command -v brew >/dev/null 2>&1; then
  echo "    Homebrew is not installed. It is the cleanest way to install and update Java."
  read -r -p "    Install Homebrew now? (needs your Mac password) [y/N] " ans
  case "$ans" in [yY]*) ;; *) die "Homebrew required. Install it from https://brew.sh and re-run." ;; esac
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  for p in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    [ -x "$p" ] && eval "$("$p" shellenv)" && break
  done
  command -v brew >/dev/null 2>&1 || die "Homebrew installed but not on PATH. Open a new terminal and re-run."
fi
ok "Homebrew $(brew --version | head -n1 | awk '{print $2}')"

# --- 2. Java ---------------------------------------------------------------------
# The official Minecraft Launcher bundles its own Java for the game itself. A system
# JDK is still needed to run the Fabric installer, and by third-party launchers
# (Prism, MultiMC) and dedicated servers. Temurin 25 is the current LTS.
say "Checking Java $JAVA_MAJOR"
if ! JAVA_HOME_DIR="$(/usr/libexec/java_home -v "$JAVA_MAJOR" 2>/dev/null)"; then
  echo "    Installing Eclipse Temurin $JAVA_MAJOR via Homebrew..."
  brew install --cask "temurin@$JAVA_MAJOR" || brew install --cask temurin
  JAVA_HOME_DIR="$(/usr/libexec/java_home -v "$JAVA_MAJOR" 2>/dev/null)" \
    || JAVA_HOME_DIR="$(/usr/libexec/java_home 2>/dev/null)" \
    || die "Java installed but java_home cannot find it."
fi
JAVA="$JAVA_HOME_DIR/bin/java"
ok "$("$JAVA" -version 2>&1 | head -n1)"
ok "JAVA_HOME=$JAVA_HOME_DIR"

# --- 3. Minecraft Launcher -------------------------------------------------------
# The Fabric installer writes a profile into launcher_profiles.json, which only exists
# once the official launcher has been started at least once.
say "Checking Minecraft Launcher"
if [ ! -f "$MC_DIR/launcher_profiles.json" ]; then
  if [ ! -d "/Applications/Minecraft.app" ]; then
    echo "    Minecraft Launcher not found; installing via Homebrew..."
    brew install --cask minecraft
  fi
  echo
  echo "    Open the Minecraft Launcher, sign in with your Microsoft account,"
  echo "    let it finish its first start, then quit it and come back here."
  open -a Minecraft || true
  read -r -p "    Press Enter once you have quit the launcher... " _
  [ -f "$MC_DIR/launcher_profiles.json" ] || die "Still no $MC_DIR/launcher_profiles.json. Start the launcher once, then re-run."
fi
ok "Launcher data at $MC_DIR"

# --- 4. Resolve versions ---------------------------------------------------------
say "Resolving versions"
if [ -z "$MC_VERSION" ]; then
  MC_VERSION="$(curl -fsSL https://meta.fabricmc.net/v2/versions/game \
    | json_eval 'd.filter(function (g) { return g.stable; })[0].version')"
  [ -n "$MC_VERSION" ] || die "Could not determine latest Minecraft version from meta.fabricmc.net."
fi
ok "Minecraft $MC_VERSION"

INSTALLER_VERSION="$(curl -fsSL https://maven.fabricmc.net/net/fabricmc/fabric-installer/maven-metadata.xml \
  | sed -n 's:.*<release>\(.*\)</release>.*:\1:p')"
[ -n "$INSTALLER_VERSION" ] || die "Could not determine latest Fabric installer version."
ok "Fabric installer $INSTALLER_VERSION"

# --- 5. Fabric Loader ------------------------------------------------------------
say "Installing Fabric Loader for $MC_VERSION"
INSTALLER_JAR="$WORK_DIR/fabric-installer.jar"
curl -fsSL -o "$INSTALLER_JAR" \
  "https://maven.fabricmc.net/net/fabricmc/fabric-installer/$INSTALLER_VERSION/fabric-installer-$INSTALLER_VERSION.jar"
# Headless mode: no GUI, creates the "fabric-loader-<mc>" launcher profile.
"$JAVA" -jar "$INSTALLER_JAR" client -dir "$MC_DIR" -mcversion "$MC_VERSION"
ok "Fabric Loader installed"

# --- 6. Fabric API ---------------------------------------------------------------
# Almost every Fabric mod depends on Fabric API, so it goes in by default.
say "Installing Fabric API for $MC_VERSION"
mkdir -p "$MC_DIR/mods"
FAPI_JSON="$(curl -fsSL -A "$UA" \
  "https://api.modrinth.com/v2/project/fabric-api/version?loaders=%5B%22fabric%22%5D&game_versions=%5B%22$MC_VERSION%22%5D")"
FAPI_URL="$(printf '%s' "$FAPI_JSON" | json_eval \
  'd.length ? (d[0].files.filter(function (f) { return f.primary; })[0] || d[0].files[0]).url : ""')"
if [ -z "$FAPI_URL" ]; then
  echo "    No Fabric API build published for $MC_VERSION yet; skipping. Re-run later."
else
  # Drop older Fabric API jars so two versions never load side by side.
  find "$MC_DIR/mods" -maxdepth 1 -name 'fabric-api-*.jar' -delete
  curl -fsSL -A "$UA" -o "$MC_DIR/mods/$(basename "$FAPI_URL")" "$FAPI_URL"
  ok "$(basename "$FAPI_URL")"
fi

# --- 7. Summary ------------------------------------------------------------------
say "Done"
echo "    Java:          $("$JAVA" -version 2>&1 | head -n1)"
echo "    Fabric:        $(ls -1 "$MC_DIR/versions" | grep "^fabric-loader-.*-$MC_VERSION\$" | tail -n1)"
echo "    Mods folder:   $MC_DIR/mods"
echo
echo "    Next: open the Minecraft Launcher, pick the 'fabric-loader-$MC_VERSION' profile"
echo "    in the bottom-left dropdown, press Play once, then drop mod .jar files into the"
echo "    mods folder. Only use mods built for Fabric and for Minecraft $MC_VERSION."
echo
read -r -p "    Open the mods folder in Finder now? [Y/n] " ans
case "$ans" in [nN]*) ;; *) open "$MC_DIR/mods" ;; esac

#!/usr/bin/env bash
# setup-fabric-mac.sh — install Java (Temurin 25 LTS), Fabric Loader and Fabric API on macOS.
#
# Usage:
#   bash setup-fabric-mac.sh            # latest stable Minecraft release
#   bash setup-fabric-mac.sh 1.21.8     # pin a specific Minecraft version
#
# Needs no Homebrew and no admin password: Java goes into ~/Library/Java, which
# macOS's java_home already searches. Safe to re-run; each step skips what is done.
# Written for macOS's stock bash 3.2, so no bash 4+ features.

set -euo pipefail

MC_VERSION="${1:-}"
JAVA_MAJOR=25
MC_DIR="$HOME/Library/Application Support/minecraft"
JVM_DIR="$HOME/Library/Java/JavaVirtualMachines"
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

# --- 1. Java ---------------------------------------------------------------------
# The official Minecraft Launcher bundles its own Java for the game itself. A JDK is
# still needed to run the Fabric installer, and by third-party launchers (Prism,
# MultiMC) and dedicated servers. Temurin 25 is the current LTS.
say "Checking Java $JAVA_MAJOR"
JAVA_HOME_DIR=""
if [ -x "$JVM_DIR/temurin-$JAVA_MAJOR.jdk/Contents/Home/bin/java" ]; then
  JAVA_HOME_DIR="$JVM_DIR/temurin-$JAVA_MAJOR.jdk/Contents/Home"
elif JH="$(/usr/libexec/java_home -v "$JAVA_MAJOR" 2>/dev/null)"; then
  JAVA_HOME_DIR="$JH"
fi

if [ -z "$JAVA_HOME_DIR" ]; then
  case "$(uname -m)" in
    arm64)  ARCH=aarch64 ;;   # Apple Silicon (M1/M2/M3/M4)
    x86_64) ARCH=x64 ;;       # Intel Macs
    *)      die "Unsupported CPU: $(uname -m)" ;;
  esac
  echo "    Downloading Eclipse Temurin $JAVA_MAJOR for $ARCH (about 200 MB)..."
  curl -fL --progress-bar -o "$WORK_DIR/jdk.tar.gz" \
    "https://api.adoptium.net/v3/binary/latest/$JAVA_MAJOR/ga/mac/$ARCH/jdk/hotspot/normal/eclipse"
  mkdir -p "$WORK_DIR/jdk" "$JVM_DIR"
  tar -xzf "$WORK_DIR/jdk.tar.gz" -C "$WORK_DIR/jdk"
  # The tarball holds a single jdk-25.x.y+z/ folder laid out as a macOS .jdk bundle.
  SRC="$(find "$WORK_DIR/jdk" -mindepth 1 -maxdepth 1 -type d | head -n1)"
  [ -x "$SRC/Contents/Home/bin/java" ] || die "Unexpected JDK archive layout."
  rm -rf "$JVM_DIR/temurin-$JAVA_MAJOR.jdk"
  mv "$SRC" "$JVM_DIR/temurin-$JAVA_MAJOR.jdk"
  JAVA_HOME_DIR="$JVM_DIR/temurin-$JAVA_MAJOR.jdk/Contents/Home"
fi
JAVA="$JAVA_HOME_DIR/bin/java"
"$JAVA" -version >/dev/null 2>&1 || die "Java at $JAVA does not run."
ok "$("$JAVA" -version 2>&1 | head -n1)"
ok "JAVA_HOME=$JAVA_HOME_DIR"

# --- 2. Minecraft Launcher -------------------------------------------------------
# The Fabric installer writes a profile into launcher_profiles.json, which only exists
# once the official launcher has been started at least once.
say "Checking Minecraft Launcher"
if [ ! -f "$MC_DIR/launcher_profiles.json" ]; then
  [ -d "/Applications/Minecraft.app" ] \
    || die "Minecraft Launcher not found. Install it from https://www.minecraft.net/download and re-run."
  echo
  echo "    Open the Minecraft Launcher, sign in with your Microsoft account,"
  echo "    let it finish its first start, then quit it and come back here."
  open -a Minecraft || true
  read -r -p "    Press Enter once you have quit the launcher... " _
  [ -f "$MC_DIR/launcher_profiles.json" ] || die "Still no $MC_DIR/launcher_profiles.json. Start the launcher once, then re-run."
fi
ok "Launcher data at $MC_DIR"

# --- 3. Resolve versions ---------------------------------------------------------
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

# --- 4. Fabric Loader ------------------------------------------------------------
say "Installing Fabric Loader for $MC_VERSION"
INSTALLER_JAR="$WORK_DIR/fabric-installer.jar"
curl -fsSL -o "$INSTALLER_JAR" \
  "https://maven.fabricmc.net/net/fabricmc/fabric-installer/$INSTALLER_VERSION/fabric-installer-$INSTALLER_VERSION.jar"
# Headless mode: no GUI, creates the "fabric-loader-<mc>" launcher profile.
"$JAVA" -jar "$INSTALLER_JAR" client -dir "$MC_DIR" -mcversion "$MC_VERSION"
ok "Fabric Loader installed"

# --- 5. Fabric API ---------------------------------------------------------------
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

# --- 6. Summary ------------------------------------------------------------------
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

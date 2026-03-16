#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_NAME="Nexus"
EXECUTABLE_NAME="NexusMac"
BUNDLE_ID="${BUNDLE_ID:-ai.nexus.macos}"
VERSION="${VERSION:-0.1.0}"
BUILD_NUMBER="${BUILD_NUMBER:-$(date +%Y%m%d%H%M)}"
DIST_DIR="$ROOT_DIR/dist"
APP_DIR="$DIST_DIR/$APP_NAME.app"
CONTENTS_DIR="$APP_DIR/Contents"
MACOS_DIR="$CONTENTS_DIR/MacOS"
RESOURCES_DIR="$CONTENTS_DIR/Resources"
TEMPLATE_PATH="$ROOT_DIR/Resources/Info.plist.template"
ICONSET_DIR="$ROOT_DIR/Resources/NexusMac.iconset"
ICON_PATH="$ROOT_DIR/Resources/NexusMac.icns"
ICON_GENERATOR="$ROOT_DIR/scripts/generate_icon.py"

run_icon_generator() {
  if [[ -n "${CONDA_EXE:-}" ]]; then
    "$CONDA_EXE" run -n ai_assist python "$ICON_GENERATOR"
    return
  fi

  if [[ -x "$HOME/miniconda3/bin/conda" ]]; then
    "$HOME/miniconda3/bin/conda" run -n ai_assist python "$ICON_GENERATOR"
    return
  fi

  if command -v python >/dev/null 2>&1; then
    python "$ICON_GENERATOR"
    return
  fi

  echo "Unable to run icon generator: conda or python not found" >&2
  exit 1
}

mkdir -p "$DIST_DIR"
rm -rf "$APP_DIR"

if [[ ! -d "$ICONSET_DIR" ]]; then
  echo "Iconset not found at $ICONSET_DIR" >&2
  exit 1
fi

run_icon_generator >/dev/null
rm -f "$ICON_PATH"
/usr/bin/iconutil -c icns "$ICONSET_DIR" -o "$ICON_PATH"

swift build -c release --package-path "$ROOT_DIR" --product "$EXECUTABLE_NAME" >/dev/null
BIN_DIR="$(swift build -c release --package-path "$ROOT_DIR" --show-bin-path)"
BIN_PATH="$BIN_DIR/$EXECUTABLE_NAME"

if [[ ! -x "$BIN_PATH" ]]; then
  echo "Build output not found at $BIN_PATH" >&2
  exit 1
fi

mkdir -p "$MACOS_DIR" "$RESOURCES_DIR"
cp "$BIN_PATH" "$MACOS_DIR/$EXECUTABLE_NAME"
cp "$ICON_PATH" "$RESOURCES_DIR/NexusMac.icns"

sed \
  -e "s#__EXECUTABLE__#$EXECUTABLE_NAME#g" \
  -e "s#__BUNDLE_ID__#$BUNDLE_ID#g" \
  -e "s#__VERSION__#$VERSION#g" \
  -e "s#__BUILD__#$BUILD_NUMBER#g" \
  "$TEMPLATE_PATH" > "$CONTENTS_DIR/Info.plist"

chmod +x "$MACOS_DIR/$EXECUTABLE_NAME"

echo "Packaged app bundle:"
echo "  $APP_DIR"

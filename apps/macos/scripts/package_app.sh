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

# ---------- Bundle Python backend + config into Resources ----------
NEXUS_REPO="$(cd "$ROOT_DIR/../.." && pwd)"
BACKEND_DIR="$RESOURCES_DIR/nexus-backend"
mkdir -p "$BACKEND_DIR"

echo "Bundling Python backend from $NEXUS_REPO ..."

# 1) Copy the nexus Python package (exclude __pycache__, .pyc)
rsync -a --delete \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.DS_Store' \
  "$NEXUS_REPO/nexus/" "$BACKEND_DIR/nexus/"

# 2) Copy config directory (app.yaml + node cards)
mkdir -p "$BACKEND_DIR/config/node_cards"
cp "$NEXUS_REPO/config/app.yaml" "$BACKEND_DIR/config/app.yaml" 2>/dev/null || true

# Copy a sidecar-specific minimal config if it exists, otherwise create one
if [[ -f "$NEXUS_REPO/config/sidecar.yaml" ]]; then
  cp "$NEXUS_REPO/config/sidecar.yaml" "$BACKEND_DIR/config/app.yaml"
fi

# Copy node cards
cp "$NEXUS_REPO/config/node_cards/"*.yaml "$BACKEND_DIR/config/node_cards/" 2>/dev/null || true

# 3) Copy .env if exists
cp "$NEXUS_REPO/.env" "$BACKEND_DIR/.env" 2>/dev/null || true

# 4) Copy the web frontend dist (for workspace view)
if [[ -d "$ROOT_DIR/../desktop/dist" ]]; then
  rsync -a --delete "$ROOT_DIR/../desktop/dist/" "$RESOURCES_DIR/web-ui/"
fi

# 5) Create a self-contained Python venv with all dependencies
VENV_DIR="$BACKEND_DIR/venv"
SYSTEM_PYTHON=""

# Find a suitable Python 3.10+ interpreter
for py_candidate in \
    python3.12 python3.11 python3.10 python3 \
    /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 \
    /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
    /opt/homebrew/bin/python3 \
    /usr/local/bin/python3 \
    /usr/bin/python3; do
  if command -v "$py_candidate" >/dev/null 2>&1; then
    SYSTEM_PYTHON="$(command -v "$py_candidate")"
    break
  elif [[ -x "$py_candidate" ]]; then
    SYSTEM_PYTHON="$py_candidate"
    break
  fi
done

if [[ -z "$SYSTEM_PYTHON" ]]; then
  echo "ERROR: No Python 3 interpreter found for creating venv" >&2
  exit 1
fi

echo "Creating bundled venv with $SYSTEM_PYTHON ..."
"$SYSTEM_PYTHON" -m venv --without-pip "$VENV_DIR"

# Bootstrap pip inside the venv
echo "Bootstrapping pip ..."
"$VENV_DIR/bin/python" -m ensurepip --default-pip 2>/dev/null \
  || "$SYSTEM_PYTHON" -m venv "$VENV_DIR"   # re-create with pip if ensurepip failed

# Install runtime dependencies (no dev/optional extras)
echo "Installing dependencies into bundled venv ..."
"$VENV_DIR/bin/pip" install --quiet --no-cache-dir \
  "fastapi>=0.111.0" \
  "uvicorn[standard]>=0.30.0" \
  "httpx>=0.27.0" \
  "aiomqtt>=2.5.1" \
  "PyYAML>=6.0.1" \
  "python-dotenv>=1.0.1" \
  "openai>=1.30.0" \
  "pydantic>=2.0" \
  "aiohttp>=3.9.0" \
  "pypdf>=4.0" \
  "markdownify>=0.11.0" \
  "lark-oapi>=1.4.20"

# Install nexus itself in editable-like mode (just add the package root to .pth)
SITE_PACKAGES="$(ls -d "$VENV_DIR"/lib/python*/site-packages | head -1)"
echo "$BACKEND_DIR" > "$SITE_PACKAGES/nexus-backend.pth"

# Strip __pycache__ from venv site-packages to save space
find "$VENV_DIR" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

echo "  Bundled venv: $VENV_DIR"
echo "  Backend bundled: $BACKEND_DIR"

echo "Packaged app bundle:"
echo "  $APP_DIR"

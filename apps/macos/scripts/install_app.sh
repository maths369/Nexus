#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_DIR="${TARGET_DIR:-$HOME/Applications}"
APP_NAME="Nexus.app"
LEGACY_APP="$TARGET_DIR/NexusMac.app"
SOURCE_APP="$ROOT_DIR/dist/$APP_NAME"
TARGET_APP="$TARGET_DIR/$APP_NAME"

"$ROOT_DIR/scripts/package_app.sh"

mkdir -p "$TARGET_DIR"
rm -rf "$LEGACY_APP"
rm -rf "$TARGET_APP"
cp -R "$SOURCE_APP" "$TARGET_APP"

/usr/bin/touch "$TARGET_APP"

echo "Installed app bundle:"
echo "  $TARGET_APP"
echo
echo "Launch with:"
echo "  open \"$TARGET_APP\""

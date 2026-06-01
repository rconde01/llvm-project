#!/bin/bash
# Restore the prebuilt flang binary into the project's build dir.
# Usage: bash tools/install-flang.sh
set -e
ROOT="$(git rev-parse --show-toplevel 2>/dev/null || cd "$(dirname "$0")/.." && pwd)"
BIN_DST="$ROOT/build/bin"
mkdir -p "$BIN_DST"
xz -dkc "$(dirname "$0")/flang-23.xz" > "$BIN_DST/flang-23"
chmod +x "$BIN_DST/flang-23"
ln -sfn flang-23 "$BIN_DST/flang"
echo "Restored $BIN_DST/flang-23 ($(stat -c%s "$BIN_DST/flang-23") bytes)"

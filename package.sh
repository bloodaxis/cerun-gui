#!/bin/bash
# Build the release archive next to this repository. Never replaces an
# existing archive: every release gets a new version.
set -eu
cd -- "$(dirname -- "$(readlink -f -- "$0")")"
version=$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' lib/cerun-gui/cerun_gui.py)
[ -n "$version" ] || { echo "VERSION not found in cerun_gui.py" >&2; exit 1; }
archive="../cerun-gui-local-v$version.tar.gz"
[ ! -e "$archive" ] || { echo "$archive already exists; raise VERSION first." >&2; exit 1; }
grep -q "v$version\.tar\.gz" README.md || { echo "README.md does not name v$version." >&2; exit 1; }
QT_QPA_PLATFORM=offscreen python3 -B -m unittest tests/test_gui.py
tar --owner=0 --group=0 --exclude='__pycache__' -czf "$archive" bin lib share
echo "Wrote $(readlink -f -- "$archive")"

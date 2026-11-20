#!/usr/bin/env bash
# Build a distributable .ankiaddon file from src/ai_srs.
#
# An .ankiaddon is just a zip whose ROOT contains the add-on's files
# (manifest.json, __init__.py, …) — NOT a wrapping folder. Users install it via
# Anki → Tools → Add-ons → Install from file…
#
# This ships ONLY the add-on code + default config. It deliberately EXCLUDES:
#   - __pycache__/ and *.pyc        (build artifacts; AnkiWeb rejects pycache)
#   - user_files/                   (the user's sidecar DBs, settings, logs — never ship)
#   - meta.json                     (per-user runtime config Anki writes locally)
#   - *.log, *.sqlite, *.db, *.tmp  (runtime data)
#   - .DS_Store                     (macOS cruft)
# Tests, venv/, and .git live at the repo root (outside src/ai_srs), so they are
# excluded by construction.
#
# Portable: the zip is written with Python's zipfile (no `zip` binary needed —
# Git Bash on Windows has none), using whichever of python3 / python actually
# runs (on Windows `python3` can be the Microsoft Store stub).
#
# Usage:
#   bash scripts/build_ankiaddon.sh
# Output:
#   dist/ai_srs-<version>.ankiaddon   (version read from manifest.json)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$REPO_ROOT/src/ai_srs"
DIST="$REPO_ROOT/dist"

PY=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c "import sys" >/dev/null 2>&1; then
    PY="$candidate"
    break
  fi
done
if [[ -z "$PY" ]]; then
  echo "error: no working python3/python on PATH" >&2
  exit 1
fi

if [[ ! -f "$SRC/manifest.json" ]]; then
  echo "error: $SRC/manifest.json not found" >&2
  exit 1
fi

mkdir -p "$DIST"

"$PY" - "$SRC" "$DIST" <<'PY'
import fnmatch
import json
import os
import sys
import zipfile

src, dist = sys.argv[1], sys.argv[2]
version = json.load(open(os.path.join(src, "manifest.json"), encoding="utf-8")).get(
    "human_version", "0.0.0")
out = os.path.join(dist, f"ai_srs-{version}.ankiaddon")

EXCLUDED_DIRS = {"__pycache__", "user_files"}
EXCLUDED_FILES = ("meta.json", "*.pyc", "*.pyo", "*.log", "*.sqlite",
                  "*.sqlite-journal", "*.db", "*.tmp", ".DS_Store")

if os.path.exists(out):
    os.remove(out)
names = []
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
    for root, dirs, files in os.walk(src):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED_DIRS)
        for name in sorted(files):
            if any(fnmatch.fnmatch(name, pat) for pat in EXCLUDED_FILES):
                continue
            path = os.path.join(root, name)
            arc = os.path.relpath(path, src).replace(os.sep, "/")
            zf.write(path, arc)
            names.append(arc)

bad = [n for n in names
       if n.split("/")[0] in EXCLUDED_DIRS or "__pycache__" in n
       or any(fnmatch.fnmatch(n.split("/")[-1], p) for p in EXCLUDED_FILES)]
if "manifest.json" not in names:
    sys.exit("error: manifest.json is not at the archive root")
if bad:
    sys.exit(f"error: excluded files made it into the archive: {bad}")

print(f"built: {out}  ({os.path.getsize(out)} bytes, {len(names)} files)")
for n in names:
    print("  " + n)
print("checked: manifest.json at the root; no user_files, meta.json, logs, DBs or __pycache__")
PY

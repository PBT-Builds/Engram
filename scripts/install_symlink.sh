#!/usr/bin/env bash
# Symlink src/ai_srs into Anki's addons21 folder so Anki picks up edits
# without you having to copy the folder after every change.
#
# Usage:
#   bash scripts/install_symlink.sh                 # macOS default Anki dir
#   ANKI_ADDONS_DIR=/custom/path bash scripts/install_symlink.sh
#
# After running, fully quit and re-open Anki for it to load the add-on.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$REPO_ROOT/src/ai_srs"

if [[ ! -d "$SRC" ]]; then
  echo "error: $SRC does not exist" >&2
  exit 1
fi

DEFAULT_DIR="$HOME/Library/Application Support/Anki2/addons21"
ADDONS_DIR="${ANKI_ADDONS_DIR:-$DEFAULT_DIR}"

if [[ ! -d "$ADDONS_DIR" ]]; then
  echo "error: addons dir does not exist: $ADDONS_DIR" >&2
  echo "hint:  launch Anki once so it creates the profile, or set ANKI_ADDONS_DIR" >&2
  exit 1
fi

DEST="$ADDONS_DIR/ai_srs"

if [[ -L "$DEST" ]]; then
  existing="$(readlink "$DEST")"
  if [[ "$existing" == "$SRC" ]]; then
    echo "ok: symlink already in place → $DEST"
    exit 0
  fi
  echo "error: $DEST exists and points to $existing (not $SRC)" >&2
  echo "       remove it manually if you want to replace it" >&2
  exit 1
fi

if [[ -e "$DEST" ]]; then
  echo "error: $DEST exists and is not a symlink — refusing to overwrite" >&2
  exit 1
fi

ln -s "$SRC" "$DEST"
echo "linked: $DEST → $SRC"
echo "next:   quit Anki fully (Cmd-Q) and reopen; check Tools → Engram"

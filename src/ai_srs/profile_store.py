"""Per-profile storage: one sidecar DB and one settings file per Anki profile.

Why: the sidecar DB holds Anki card and note IDs. Those only mean something in
the collection they came from, so a DB shared by every profile can apply one
collection's IDs to another. Each profile therefore gets its own folder:

    user_files/profiles/<profile name>/pipeline.sqlite
    user_files/profiles/<profile name>/settings.json

`user_files/` is the folder Anki preserves across add-on updates. The log and
the global add-on config (`meta.json`: providers, models, thresholds…) stay
global; only the collection- or person-specific keys in `PER_PROFILE_DEFAULTS`
live per profile.

Safety layers, all in `ensure_store`:
  - a fingerprint of the collection (Anki's `col.crt`, read-only; it is the
    creation DAY, so not unique on its own) is stamped
    into each DB; a DB whose fingerprint doesn't match the open collection AND
    whose card/note IDs aren't in it is refused (ProfileStoreError);
  - a renamed profile picks its folder back up by fingerprint, but only from a
    folder whose profile no longer exists;
  - the old shared `user_files/pipeline.sqlite` is migrated once, by COPY,
    to the profile its IDs belong to; the original is renamed, never deleted.

Pure: stdlib only, no Anki. The Anki side (profile name, fingerprint, ID
lookups) is injected by `profile_runtime`.
"""

from __future__ import annotations

import copy
import json
import os
import sqlite3
import time
import urllib.request
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from . import db
from .log import get_logger

log = get_logger()

# Keys that belong to one profile (its collection's decks, its owner's vault),
# with the value a fresh profile starts from.
PER_PROFILE_DEFAULTS = {
    "deck_category_map": {},
    "vault_path": "",
    "vault_include_globs": ["**/*.md"],
    "vault_category_map": {},
}
PER_PROFILE_KEYS = tuple(PER_PROFILE_DEFAULTS)

PROFILES_DIRNAME = "profiles"
DB_FILENAME = "pipeline.sqlite"
SETTINGS_FILENAME = "settings.json"
LEGACY_MARKER = "legacy_migrated.json"
_FP_KEY = "collection_fingerprint"

# A legacy DB with card/note IDs is claimed only if at least this share of them
# exist in the opening collection (and at least one does).
CLAIM_THRESHOLD = 0.5


class ProfileStoreError(Exception):
    """The profile's store can't be used safely (bad name, foreign DB)."""


@dataclass
class StoreResult:
    db_path: str
    # created | opened | fingerprint-updated | adopted | migrated
    action: str
    # Per-profile keys taken from the old global config on migration (the
    # caller removes them from meta.json); None when nothing was migrated.
    migrated_settings: Optional[dict] = None


# ---------- layout ----------

def profile_dir(user_files: str, name) -> str:
    if not isinstance(name, str) or not name.strip() or name in (".", "..") \
            or "/" in name or "\\" in name or "\0" in name:
        raise ProfileStoreError(f"unusable profile name: {name!r}")
    return os.path.join(user_files, PROFILES_DIRNAME, name)


# ---------- settings ----------

def split_config(cfg: dict) -> "tuple[dict, dict]":
    """(global part, per-profile part) of a full config dict."""
    g = {k: v for k, v in cfg.items() if k not in PER_PROFILE_DEFAULTS}
    p = {k: v for k, v in cfg.items() if k in PER_PROFILE_DEFAULTS}
    return g, p


def merge_config(global_cfg: dict, settings: dict) -> dict:
    """The effective config for one profile. Per-profile keys come ONLY from
    that profile's settings (or the defaults) — a leftover copy in the global
    config is ignored, otherwise it would be shared again."""
    eff = {k: v for k, v in (global_cfg or {}).items() if k not in PER_PROFILE_DEFAULTS}
    for key, default in PER_PROFILE_DEFAULTS.items():
        if key in (settings or {}):
            eff[key] = copy.deepcopy(settings[key])
        else:
            eff[key] = copy.deepcopy(default)
    return eff


def load_settings(pdir: str) -> dict:
    path = os.path.join(pdir, SETTINGS_FILENAME)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        log.exception("profile settings unreadable, using defaults: %s", path)
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k in PER_PROFILE_DEFAULTS}


def save_settings(pdir: str, settings: dict) -> None:
    """Atomic write (temp file + replace) of the per-profile keys only."""
    os.makedirs(pdir, exist_ok=True)
    data = {k: v for k, v in (settings or {}).items() if k in PER_PROFILE_DEFAULTS}
    path = os.path.join(pdir, SETTINGS_FILENAME)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _meaningful(settings: dict) -> dict:
    """Per-profile keys whose value differs from the fresh default."""
    return {
        k: settings[k] for k in PER_PROFILE_KEYS
        if k in settings and settings[k] != PER_PROFILE_DEFAULTS[k]
    }


# ---------- fingerprint + IDs ----------

def read_fingerprint(conn) -> Optional[int]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (_FP_KEY,)).fetchone()
    if row is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


def write_fingerprint(conn, fingerprint) -> None:
    with conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_FP_KEY, str(int(fingerprint))),
        )


def collection_ids(conn) -> "tuple[set, set]":
    """Every Anki card ID and note ID a sidecar DB refers to."""
    cards, notes = set(), set()
    for (cid,) in conn.execute(
            "SELECT current_card_id FROM words WHERE current_card_id IS NOT NULL"):
        cards.add(int(cid))
    for (cid,) in conn.execute("SELECT source_card_id FROM generated_sources"):
        cards.add(int(cid))
    for (nid,) in conn.execute(
            "SELECT source_note_id FROM words WHERE source_note_id IS NOT NULL"):
        notes.add(int(nid))
    for (nid,) in conn.execute(
            "SELECT source_note_id FROM generated_sources WHERE source_note_id IS NOT NULL"):
        notes.add(int(nid))
    return cards, notes


def ids_belong(card_ids: set, note_ids: set,
               existing_cards: Callable[[list], Iterable],
               existing_notes: Callable[[list], Iterable]) -> bool:
    """True if these IDs plausibly come from the open collection: at least one
    exists there, and at least CLAIM_THRESHOLD of them do. (Some may be gone
    because the user deleted cards; Anki IDs are millisecond timestamps, so a
    chance match across collections is negligible.)"""
    total = len(card_ids) + len(note_ids)
    if total == 0:
        return False
    found = len(set(existing_cards(sorted(card_ids)) or ())) if card_ids else 0
    found += len(set(existing_notes(sorted(note_ids)) or ())) if note_ids else 0
    return found >= 1 and found / total >= CLAIM_THRESHOLD


def _any_exist(card_ids, note_ids, existing_cards, existing_notes) -> bool:
    if card_ids and set(existing_cards(sorted(card_ids)) or ()):
        return True
    return bool(note_ids and set(existing_notes(sorted(note_ids)) or ()))


def _db_ids(path: str) -> "tuple[set, set]":
    """IDs in a DB we may not own (the legacy file): opened READ-ONLY, so
    inspecting it never adds tables or otherwise changes it. A DB too old to
    have one of the tables simply contributes no IDs from it."""
    uri = "file:" + urllib.request.pathname2url(os.path.abspath(path)) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        cards, notes = set(), set()
        queries = (
            (cards, "SELECT current_card_id FROM words WHERE current_card_id IS NOT NULL"),
            (cards, "SELECT source_card_id FROM generated_sources"),
            (notes, "SELECT source_note_id FROM words WHERE source_note_id IS NOT NULL"),
            (notes, "SELECT source_note_id FROM generated_sources "
                    "WHERE source_note_id IS NOT NULL"),
        )
        for target, sql in queries:
            try:
                target.update(int(r[0]) for r in conn.execute(sql))
            except sqlite3.OperationalError:  # table missing in an old DB
                pass
        return cards, notes
    finally:
        conn.close()


def _stamp(path: str, fingerprint) -> None:
    conn = db.connect_path(path)
    try:
        write_fingerprint(conn, fingerprint)
    finally:
        conn.close()


def _fingerprint_of(path: str) -> Optional[int]:
    conn = db.connect_path(path)
    try:
        return read_fingerprint(conn)
    finally:
        conn.close()


# ---------- the one entry point ----------

def ensure_store(
    user_files: str,
    profile_name: str,
    *,
    fingerprint: int,
    live_profiles: Iterable[str],
    existing_cards: Callable[[list], Iterable],
    existing_notes: Callable[[list], Iterable],
    legacy_settings: Optional[dict] = None,
) -> StoreResult:
    """Make sure `profile_name` has a usable, verified store; return where.

    Order: existing folder (verify fingerprint) → adopt a renamed profile's
    orphan folder → migrate the old shared DB/settings → create fresh.
    Raises ProfileStoreError when the existing DB belongs to another collection.
    """
    pdir = profile_dir(user_files, profile_name)
    path = os.path.join(pdir, DB_FILENAME)

    if os.path.exists(path):
        return _verify(path, profile_name, fingerprint, existing_cards, existing_notes)

    adopted = _adopt_orphan(user_files, pdir, fingerprint, set(live_profiles),
                            existing_cards, existing_notes)
    if adopted:
        log.info("profile store: %r adopted folder %r (renamed profile, same collection)",
                 profile_name, adopted)
        return StoreResult(path, "adopted")

    migrated = _migrate_legacy(
        user_files, profile_name, pdir, path, fingerprint,
        existing_cards, existing_notes, legacy_settings or {},
    )
    if migrated is not None:
        return migrated

    _stamp(path, fingerprint)
    log.info("profile store: created %s", path)
    return StoreResult(path, "created")


def _verify(path, profile_name, fingerprint, existing_cards, existing_notes) -> StoreResult:
    stored = _fingerprint_of(path)
    if stored is None:
        _stamp(path, fingerprint)
        return StoreResult(path, "opened")
    cards, notes = _db_ids(path)
    if stored == int(fingerprint):
        # col.crt is the collection's creation DAY, so collections made on the
        # same day share it. With a match, still refuse if NONE of the DB's IDs
        # exist here (one surviving ID is enough: the user may delete cards).
        if (cards or notes) and not _any_exist(cards, notes, existing_cards, existing_notes):
            raise ProfileStoreError(
                f"The Engram data for profile {profile_name!r} refers to "
                f"{len(cards)} cards / {len(notes)} notes, none of which exist in "
                f"the open collection, so it was not used. Folder: "
                f"{os.path.dirname(path)}"
            )
        return StoreResult(path, "opened")
    if not cards and not notes:
        # Nothing in it that could be misapplied (e.g. only a vault ledger).
        _stamp(path, fingerprint)
        log.warning("profile store: %r fingerprint changed (%s -> %s); DB holds no "
                    "card IDs, accepted", profile_name, stored, fingerprint)
        return StoreResult(path, "fingerprint-updated")
    if ids_belong(cards, notes, existing_cards, existing_notes):
        _stamp(path, fingerprint)
        log.warning("profile store: %r fingerprint changed (%s -> %s); its card IDs "
                    "are in this collection, accepted", profile_name, stored, fingerprint)
        return StoreResult(path, "fingerprint-updated")
    raise ProfileStoreError(
        f"The Engram data for profile {profile_name!r} belongs to a different "
        f"collection (fingerprint {stored}, open collection {fingerprint}), so it "
        f"was not used. Folder: {os.path.dirname(path)}"
    )


def _adopt_orphan(user_files, pdir, fingerprint, live: set,
                  existing_cards=None, existing_notes=None) -> Optional[str]:
    root = os.path.join(user_files, PROFILES_DIRNAME)
    try:
        names = sorted(os.listdir(root))
    except FileNotFoundError:
        return None
    for name in names:
        if name in live:
            continue
        candidate = os.path.join(root, name, DB_FILENAME)
        if not os.path.isfile(candidate):
            continue
        try:
            if _fingerprint_of(candidate) != int(fingerprint):
                continue
            # The fingerprint is only the creation day: also require that the
            # folder's IDs (if any) exist in this collection.
            cards, notes = _db_ids(candidate)
            if (cards or notes) and existing_cards is not None and not _any_exist(
                    cards, notes, existing_cards, existing_notes):
                continue
        except Exception:
            continue
        os.rename(os.path.join(root, name), pdir)
        return name
    return None


def _legacy_marker(user_files) -> str:
    return os.path.join(user_files, LEGACY_MARKER)


def _migrate_legacy(user_files, profile_name, pdir, path, fingerprint,
                    existing_cards, existing_notes, legacy_settings) -> Optional[StoreResult]:
    if os.path.exists(_legacy_marker(user_files)):
        return None
    legacy_db = os.path.join(user_files, DB_FILENAME)
    has_db = os.path.isfile(legacy_db)
    settings = _meaningful(legacy_settings)
    if not has_db and not settings:
        return None

    if has_db:
        cards, notes = _db_ids(legacy_db)
        if (cards or notes) and not ids_belong(cards, notes, existing_cards, existing_notes):
            log.info("profile store: legacy DB (%d card / %d note IDs) does not belong to "
                     "profile %r; left for another profile", len(cards), len(notes), profile_name)
            return None

    os.makedirs(pdir, exist_ok=True)
    if has_db:
        # sqlite's backup API: a consistent copy even if the file is open.
        src = sqlite3.connect(legacy_db)
        dst = sqlite3.connect(path)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
    _stamp(path, fingerprint)

    if settings and not os.path.exists(os.path.join(pdir, SETTINGS_FILENAME)):
        save_settings(pdir, settings)

    # The marker is the source of truth for "already claimed": the rename below
    # can fail on Windows if something holds the file open.
    with open(_legacy_marker(user_files), "w", encoding="utf-8") as f:
        json.dump({"claimed_by": profile_name, "at": int(time.time()),
                   "had_db": has_db, "settings_keys": sorted(settings)}, f)
    if has_db:
        try:
            os.replace(legacy_db, legacy_db + f".migrated-to-{profile_name}")
        except Exception:
            log.warning("profile store: could not rename legacy DB (kept in place; "
                        "marker written)", exc_info=True)

    log.info("profile store: migrated legacy state to profile %r (db=%s, settings=%s)",
             profile_name, has_db, sorted(settings))
    return StoreResult(path, "migrated", migrated_settings=settings or {})

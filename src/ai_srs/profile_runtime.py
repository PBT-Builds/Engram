"""Anki glue for `profile_store`: which profile is open, its store, its config.

Everything Anki-specific about per-profile isolation lives here, so
`profile_store` stays pure and testable.

- `on_profile_open()` runs on Anki's `profile_did_open` hook (main thread): it
  verifies/migrates the profile's store once and caches the result.
- `current_db_path()` is what `db.connect()` uses. Outside Anki it returns None
  (callers fall back to the add-on folder). Inside Anki it NEVER falls back to
  the old shared DB: if the store can't be verified it raises.
- `effective_config()` / `write_config()` are the one way the add-on reads and
  writes its settings: global keys go to Anki's add-on config, per-profile keys
  to the profile's `settings.json`.
"""

from __future__ import annotations

import os
from typing import Optional

from . import profile_store
from .log import get_logger

log = get_logger()

_PACKAGE = __name__.split(".")[0]

# profile name -> verified db path; cleared when the profile closes.
_verified: "dict[str, str]" = {}
# profile name -> last error, so a refused store is reported once, not per call.
_errors: "dict[str, str]" = {}


def _mw():
    try:
        from aqt import mw
    except Exception:
        return None
    return mw


def user_files_dir() -> str:
    return os.path.join(os.path.dirname(os.path.realpath(__file__)), "user_files")


def current_profile() -> Optional[str]:
    mw = _mw()
    if mw is None or getattr(mw, "pm", None) is None:
        return None
    return mw.pm.name


def _id_lookup(table: str):
    def lookup(ids):
        mw = _mw()
        found = set()
        ids = list(ids)
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            found.update(mw.col.db.list(f"select id from {table} where id in ({marks})", *chunk))
        return found
    return lookup


def _global_config() -> dict:
    mw = _mw()
    return mw.addonManager.getConfig(_PACKAGE) or {}


def ensure_current() -> str:
    """Verify (once per profile load) and return the open profile's DB path."""
    mw = _mw()
    name = current_profile()
    if mw is None or not name or getattr(mw, "col", None) is None:
        raise profile_store.ProfileStoreError("no Anki profile is open")
    if name in _verified:
        return _verified[name]
    if name in _errors:
        raise profile_store.ProfileStoreError(_errors[name])
    try:
        res = profile_store.ensure_store(
            user_files_dir(), name,
            fingerprint=int(mw.col.crt),
            live_profiles=mw.pm.profiles(),
            existing_cards=_id_lookup("cards"),
            existing_notes=_id_lookup("notes"),
            legacy_settings=_global_config(),
        )
    except profile_store.ProfileStoreError as e:
        _errors[name] = str(e)
        log.error("profile store refused for %r: %s", name, e)
        _notify_refused(str(e))
        raise
    if res.migrated_settings is not None:
        _strip_global_profile_keys()
    log.info("profile store: profile=%r action=%s db=%s", name, res.action, res.db_path)
    _verified[name] = res.db_path
    return res.db_path


def _strip_global_profile_keys() -> None:
    """After a migration, remove the per-profile keys from the global add-on
    config so another profile can't read them as its own."""
    mw = _mw()
    meta_cfg = (mw.addonManager.addonMeta(_PACKAGE) or {}).get("config")
    if not isinstance(meta_cfg, dict):
        return
    global_part, _ = profile_store.split_config(meta_cfg)
    if global_part != meta_cfg:
        mw.addonManager.writeConfig(_PACKAGE, global_part)


def _notify_refused(message: str) -> None:
    try:
        from aqt.utils import showWarning

        showWarning(
            "Engram is paused for this profile.\n\n" + message +
            "\n\nNothing in your collection was changed.",
            title="Engram",
        )
    except Exception:
        pass


def current_db_path() -> Optional[str]:
    """None outside Anki (tests/scripts). Inside Anki: the open profile's
    verified DB, or a ProfileStoreError — never the old shared DB."""
    mw = _mw()
    if mw is None:
        return None
    return ensure_current()


def current_profile_dir() -> str:
    return profile_store.profile_dir(user_files_dir(), current_profile())


def effective_config() -> dict:
    """Global add-on config merged with the open profile's settings."""
    global_cfg = _global_config()
    try:
        ensure_current()  # runs a pending migration before settings are read
        settings = profile_store.load_settings(current_profile_dir())
    except profile_store.ProfileStoreError:
        settings = {}
    return profile_store.merge_config(global_cfg, settings)


def write_config(cfg: dict) -> None:
    """Persist a full config dict: global keys to Anki's add-on config,
    per-profile keys to this profile's settings.json."""
    mw = _mw()
    global_part, profile_part = profile_store.split_config(cfg)
    pdir = current_profile_dir()
    merged = dict(profile_store.load_settings(pdir))
    merged.update(profile_part)
    profile_store.save_settings(pdir, merged)
    mw.addonManager.writeConfig(_PACKAGE, global_part)


def on_profile_open() -> None:
    try:
        ensure_current()
    except Exception:
        log.exception("profile store check failed on profile open")


def on_profile_close() -> None:
    _verified.clear()
    _errors.clear()


def register() -> None:
    from aqt import gui_hooks

    gui_hooks.profile_did_open.append(on_profile_open)
    gui_hooks.profile_will_close.append(on_profile_close)

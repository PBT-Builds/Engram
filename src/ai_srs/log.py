"""Logging helper.

CRITICAL — the no-popup rule (spec: no failure may surface mid-review):
Anki's error reporter (`aqt/errors.py`) intercepts EVERYTHING written to
`sys.stderr` and raises its "Anki hit an issue" dialog. A logging
`StreamHandler()` defaults to `sys.stderr`, so ANY log line — even a caught,
swallowed exception logged from inside a fail-safe wrapper — would surface a
popup that interrupts the review. (This was the actual cause of the reported
mid-review error dialogs: the hooks correctly caught their exceptions, but then
LOGGED them to stderr, which Anki turned into a dialog.)

So: when running inside Anki we route logging to a FILE under the add-on's
`user_files/` and NEVER to stderr, and we stop records propagating to the root
logger (whose handlers may also write to stderr). Outside Anki (unit tests /
standalone) stderr is fine — there's no Anki to intercept it — so test output
stays visible.
"""

from __future__ import annotations

import logging
import os

_LOGGER_NAME = "ai_srs"
_LOG_FILE_NAME = "ai_srs.log"


def _log_file_path() -> "str | None":
    """Path to the add-on's log file under `user_files/`, or None if it can't
    be determined / created (then we fall back to stderr)."""
    try:
        base = os.path.dirname(os.path.realpath(__file__))
        user_files = os.path.join(base, "user_files")
        os.makedirs(user_files, exist_ok=True)
        return os.path.join(user_files, _LOG_FILE_NAME)
    except Exception:
        return None


def _in_anki() -> bool:
    """True iff we're running inside a live Anki GUI (so stderr is intercepted
    by Anki's error dialog). `aqt.mw` is only set once the GUI is up; add-ons
    are imported after that, so this is reliably True at our import time."""
    try:
        from aqt import mw
    except Exception:
        return False
    return mw is not None


def make_formatter() -> logging.Formatter:
    """`2026-11-19 21:15:03 [engram] INFO [EngramTest] message`. The log file is
    shared by every Anki profile, so each line names the one that was open."""
    return logging.Formatter(
        "%(asctime)s [engram] %(levelname)s [%(profile)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _current_profile():
    from aqt import mw

    return mw.pm.name if mw is not None and getattr(mw, "pm", None) else None


class ProfileFilter(logging.Filter):
    """Stamps `record.profile` (or "-"). Never drops a record and never raises,
    whatever the lookup does."""

    def __init__(self, lookup):
        super().__init__()
        self._lookup = lookup

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            name = self._lookup()
        except Exception:
            name = None
        record.profile = name or "-"
        return True


def get_logger() -> logging.Logger:
    logger = logging.getLogger(_LOGGER_NAME)
    if not logger.handlers:
        handler: "logging.Handler | None" = None
        # Inside Anki: log to a FILE, never to stderr (stderr -> error popup).
        # If the file can't be opened, drop log lines rather than fall back to
        # stderr — otherwise every INFO line becomes an error dialog.
        if _in_anki():
            path = _log_file_path()
            if path is not None:
                try:
                    handler = logging.FileHandler(path, encoding="utf-8")
                except Exception:
                    handler = None
            if handler is None:
                handler = logging.NullHandler()
        # Standalone: stderr is safe (no Anki to intercept it).
        if handler is None:
            handler = logging.StreamHandler()
        handler.setFormatter(make_formatter())
        handler.addFilter(ProfileFilter(_current_profile))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        # Never let records bubble to the root logger, whose handlers may write
        # to stderr (and thus trip Anki's "Anki hit an issue" dialog).
        logger.propagate = False
    return logger


# ---- add-on updates ----
# Anki's installer renames this add-on's user_files/ to back it up while it
# replaces the code. On Windows a folder can't be renamed while a file in it
# is open, and our FileHandler keeps ai_srs.log open, so without this every
# in-app update failed with "Access is denied". The installer's own hooks let
# us let go of the file for the duration of our own install.
PACKAGE = __name__.split(".")[0]


def release_file_handlers() -> None:
    """Close and drop file handlers; logging goes nowhere until reattached
    (a NullHandler stays so get_logger doesn't reopen the file meanwhile)."""
    logger = logging.getLogger(_LOGGER_NAME)
    for h in list(logger.handlers):
        if isinstance(h, logging.FileHandler):
            logger.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())


def reattach_file_handlers() -> None:
    """Rebuild the normal handler (the log file, inside Anki)."""
    logger = logging.getLogger(_LOGGER_NAME)
    for h in list(logger.handlers):
        if isinstance(h, (logging.FileHandler, logging.NullHandler)):
            logger.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
    get_logger()


def on_will_install(_manager, package) -> None:
    if package == PACKAGE:
        release_file_handlers()


def on_did_install(_manager, package) -> None:
    if package == PACKAGE:
        reattach_file_handlers()


def on_will_delete(_dialog, ids) -> None:
    """Tools > Add-ons > Delete moves the folder to the trash, which fails on
    Windows while ai_srs.log is open (WinError 32). Logging stays off for the
    rest of the session; the add-on is gone after the restart anyway."""
    if PACKAGE in (ids or ()):
        release_file_handlers()


def register_install_hooks() -> None:
    from aqt import gui_hooks

    gui_hooks.addon_manager_will_install_addon.append(on_will_install)
    gui_hooks.addon_manager_did_install_addon.append(on_did_install)
    gui_hooks.addons_dialog_will_delete_addons.append(on_will_delete)

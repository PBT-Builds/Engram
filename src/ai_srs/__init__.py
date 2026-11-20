"""Engram add-on entry point.

Per spec §9, this is an add-on (not a fork). FSRS is untouched; we only add a
sidecar pipeline that reads notes from Original decks and generates AI cards
into Learning/Recycled decks.

This module is imported by Anki on startup. Heavy work is deferred to lazy
imports inside register functions so that import errors in submodules don't
prevent Anki from booting.
"""

from __future__ import annotations

# Anki imports are only valid when running inside Anki; guard so that the
# package — and its submodules — stay importable for tooling and unit tests
# outside Anki.
#
# IMPORTANT: gate on `mw is not None`, not merely on `aqt` being importable.
# `aqt` can be import-successful in a plain Python/test environment (e.g. when
# it's pip-installed) while there is NO running QApplication. In that case
# `_setup()` would construct a QMenu and crash with "QWidget: Must construct a
# QApplication before a QWidget", which previously made `import
# ai_srs.state_machine` impossible to run in isolation. `aqt.mw` is only set to
# the real main window once Anki's GUI is up, so it's the correct in-Anki test.
try:
    from aqt import mw
except Exception:  # pragma: no cover - exercised only outside Anki
    mw = None


def _setup() -> None:
    from . import log, menu, profile_runtime, reviewer_hooks

    log.register_install_hooks()
    profile_runtime.register()
    menu.register()
    reviewer_hooks.register()


if mw is not None:
    try:
        _setup()
    except Exception:  # pragma: no cover - never block Anki from booting
        import logging

        logging.getLogger("ai_srs").exception("ai_srs setup failed")

"""Non-blocking UI notices for background generation (Task 1).

Every AI generation now runs off the main thread, so the user needs a visible,
non-blocking indicator that work is happening (and, on cold start, that the
first card may take a while because the local model is loading into RAM).

These helpers are best-effort and import `aqt` lazily, so importing this module
never requires Anki/Qt (it stays importable for the test suite and standalone).
When not running inside Anki the tooltip calls are simply no-ops; the warm-up
notice still logs. A tooltip NEVER blocks input or requires dismissal — it
auto-expires after `period` ms.
"""

from __future__ import annotations

from .log import get_logger

log = get_logger()

# One-time "warming up" flag for this Anki session. The first AI generation
# after launch pays the model-load cost (10–30s for a 7–14B model), so we tell
# the user once instead of letting it look like a hang.
_warmed = False


def _tooltip(message: str, period: int = 2000) -> None:
    """Show a non-blocking Anki tooltip; no-op (and never raises) outside Anki."""
    try:
        from aqt.utils import tooltip

        tooltip(message, period=period)
    except Exception:  # pragma: no cover - not in a live Anki GUI
        pass


def warm_up_once() -> bool:
    """Show the one-time "warming up model…" notice for the first generation of
    the session. Returns True the first time (so the caller can suppress its own
    generic "Generating…" notice and avoid stacking two tooltips), False after.
    """
    global _warmed
    if _warmed:
        return False
    _warmed = True
    log.info("warming up local model — the first AI card can take up to a minute")
    _tooltip("Warming up model… the first AI card can take up to a minute", period=3000)
    return True


# One-shot flag for the "extraction backend unavailable" notice, keyed by the
# reason text. Same shape as `_warmed`: a scan that finds no usable extraction
# provider must say so clearly ONCE, not on every scan. A different reason (the
# key got set but the endpoint is now down) is a genuinely new fact and speaks
# again; re-raising the identical reason is nagging.
_extraction_notices: "set[str]" = set()


def extraction_unavailable_once(reason: str) -> bool:
    """Show the one-line "extraction skipped" notice for this session, unless
    this exact reason has already been shown. Returns True if it was shown.

    Deliberately a tooltip, not a dialog: vault scanning itself still worked and
    the user has candidates-or-not either way, so this must not require a
    dismissal. Callers that are already showing a summary dialog (the on-demand
    menu action) should put the reason in that dialog instead of calling this.
    """
    reason = (reason or "").strip()
    if not reason or reason in _extraction_notices:
        return False
    _extraction_notices.add(reason)
    log.warning("vault extraction skipped: %s", reason)
    _tooltip(f"Vault extraction skipped — {reason}", period=5000)
    return True


def reset_extraction_notices() -> None:
    """Forget which notices have been shown. For tests, and for a deliberate
    user re-check (fixing the key should let the next failure speak again)."""
    _extraction_notices.clear()


def generating_started(label: str = "Generating AI card…") -> None:
    """Announce (non-blocking) that a background generation has begun. On the
    very first call of the session the warm-up notice stands in for it."""
    if warm_up_once():
        return
    _tooltip(label, period=2000)


def generating_done(is_fallback: bool = False) -> None:
    """Announce (non-blocking) that a background generation finished."""
    _tooltip(
        "AI card ready" + (" (fallback)" if is_fallback else ""),
        period=1500,
    )

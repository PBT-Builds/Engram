"""Reviewer integration (spec §9).

Step 1 scope: when a card whose note carries the configured trigger tag is
shown, call Ollama with the card's front text and append the generated
sentence to the rendered HTML. This proves the end-to-end loop without
touching FSRS scheduling.

Step 3 adds the answer-event hook (`reviewer_did_answer_card`) that drives the
learning -> recycled promotion. Steps 4–5 extend the same hook to recycle
cycles, graduation to Legacy, and demotion — all dispatched in
`state_machine.on_card_answered` from the card's stage + grade.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import json

from . import deck_ops, subjects, ui_notify
from . import generator
from .generator import GenRequest, generate_validated
from .log import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from anki.cards import Card

log = get_logger()


def _config() -> dict:
    from . import profile_runtime

    return profile_runtime.effective_config()


def _subject_for_card(cfg: dict, card: "Card") -> "str | None":
    """Infer the card's subject (spec §7) from its home deck, using the
    config-driven subject list. Returns None when the deck isn't pipeline-
    enabled — generation then uses the default model."""
    try:
        from aqt import mw

        deck_name = deck_ops.card_home_deck_name(mw.col, card)
    except Exception:  # pragma: no cover - defensive
        return None
    return subjects.subject_of_deck(deck_name, subjects.subjects_from_config(cfg))


def _has_trigger_tag(card: "Card", trigger_tag: str) -> bool:
    if not trigger_tag:
        return False
    note = card.note()
    return any(t.lower() == trigger_tag.lower() for t in note.tags)


def _extract_target_word(card: "Card") -> str:
    """Step 1 heuristic: the first non-empty field is the target word.

    Real word discovery (spec §8.2) will read from a configured field name
    per note type.
    """
    note = card.note()
    for value in note.values():
        text = (value or "").strip()
        if text:
            return text
    return ""


# Banner generation runs OFF the main thread (Task 1: the old synchronous
# Ollama call inside this hook was the remaining UI freeze — `card_will_show`
# must return HTML immediately, so calling Ollama inline blocked rendering for
# the whole generation, 10–30s on cold start). We now return the card unchanged
# at once and inject the banner via JS when generation finishes.
#
# Per-card caches keep it cheap and safe:
#   _BANNER_CACHE   card.id -> finished banner HTML (reused for the answer side
#                   / re-renders with NO extra network call)
#   _BANNER_INFLIGHT card.ids whose generation is currently running (so the Q
#                   and A renders don't each kick off a duplicate generation)
_BANNER_CACHE: "dict[int, str]" = {}
_BANNER_INFLIGHT: "set[int]" = set()
_BANNER_CACHE_CAP = 256  # bound session growth; oldest-wins clear when exceeded
_BANNER_ELEMENT_ID = "ai-srs-banner"


def _build_banner(model: str, sentence: str) -> str:
    """The banner HTML, wrapped in an id'd div so the JS injector can dedupe."""
    return (
        f'<div id="{_BANNER_ELEMENT_ID}">'
        '<hr><div style="margin-top:1em;padding:0.6em;border:1px solid '
        '#888;border-radius:6px;font-style:italic;">'
        f"AI ({model}): {sentence}"
        "</div></div>"
    )


def _on_card_will_show(text: str, card: "Card", kind: str) -> str:
    """gui_hooks.card_will_show callback — NON-BLOCKING (Task 1).

    Returning the (possibly modified) HTML is the contract, and we ALWAYS return
    promptly: if a banner is already generated for this card we append it (no
    network); otherwise we launch background generation and return the card
    unchanged, injecting the banner via JS when it's ready. Generation never runs
    on the main thread, so a slow/cold model can't freeze the review.
    """
    try:
        # Only the live reviewer gets the banner (skip preview / card layout).
        if "review" not in kind:
            return text

        cfg = _config()
        trigger = cfg.get("trigger_tag", "ai-srs")
        if not _has_trigger_tag(card, trigger):
            return text

        cid = int(card.id)
        cached = _BANNER_CACHE.get(cid)
        if cached is not None:
            # Fast path: already generated — append synchronously, no Ollama.
            return text + cached

        word = _extract_target_word(card)
        if not word:
            return text

        _launch_banner_async(card, word, cfg)
        return text  # render now; the banner pops in when generation finishes
    except Exception as e:  # pragma: no cover - belt and suspenders
        log.exception("unexpected reviewer hook error: %s", e)
        return text


def _launch_banner_async(card: "Card", word: str, cfg: dict) -> None:
    """Generate the banner sentence off the main thread and inject it when done.
    Fires at most once per card (guarded by `_BANNER_INFLIGHT`). Degrades to a
    no-op when taskman is unavailable (no live Anki) — the banner is optional."""
    from aqt import mw

    cid = int(card.id)
    taskman = getattr(mw, "taskman", None)
    if taskman is None or cid in _BANNER_INFLIGHT:
        return

    # Subject inferred from the card's deck (spec §7); model chosen per subject,
    # falling back to default. Read here (main thread) before backgrounding.
    subject = _subject_for_card(cfg, card)
    prov, model = generator.resolve_for_subject(cfg, subject)
    retries = int(cfg.get("validation_retries", 2))
    req = GenRequest(
        word=word,
        model=model,
        subject=subject,
        temperature=float(cfg.get("temperature", 0.3)),
        words_per_sentence=int(cfg.get("words_per_sentence", 1)),
    )

    _BANNER_INFLIGHT.add(cid)
    log.info(
        "banner generating (background) word=%r subject=%r via provider=%s model=%r",
        word, subject, prov.name, model,
    )
    ui_notify.generating_started("Generating AI sentence…")

    def task():
        # Background thread: network only, NO Anki collection access. Validates
        # the sentence contains the target word (spec §5); None => no banner.
        return generate_validated(
            req, endpoint=prov.api_base, timeout=prov.timeout, retries=retries,
            api_format=prov.api_format, api_key=prov.api_key,
        )

    def on_done(future) -> None:
        # Back on the main thread (taskman guarantee).
        _BANNER_INFLIGHT.discard(cid)
        try:
            sentence = future.result()
        except Exception:  # pragma: no cover - never crash a review
            log.exception("banner background generation failed word=%r", word)
            return
        if not sentence:
            log.warning(
                "no validated sentence for word=%r; leaving card untouched", word
            )
            return
        banner = _build_banner(model, sentence)
        if len(_BANNER_CACHE) >= _BANNER_CACHE_CAP:
            _BANNER_CACHE.clear()
        _BANNER_CACHE[cid] = banner
        _inject_banner(mw, cid, banner)
        ui_notify.generating_done(is_fallback=False)

    taskman.run_in_background(task, on_done)


def _inject_banner(mw, cid: int, banner: str) -> None:
    """Inject the finished banner into the card currently on screen, but ONLY if
    the reviewer is still showing this exact card (otherwise it'll appear on the
    next render from `_BANNER_CACHE`). Idempotent via the element id."""
    try:
        reviewer = getattr(mw, "reviewer", None)
        current = getattr(reviewer, "card", None) if reviewer is not None else None
        if current is None or int(current.id) != cid:
            return
        js = (
            "if(!document.getElementById(%s)){"
            "document.body.insertAdjacentHTML('beforeend', %s);}"
            % (json.dumps(_BANNER_ELEMENT_ID), json.dumps(banner))
        )
        reviewer.web.eval(js)
    except Exception:  # pragma: no cover - injection is best effort
        log.exception("banner injection failed for card=%s", cid)


# Pre-answer card `type` captured in the will-answer filter, keyed by card id,
# so the did-answer hook can detect a learning/new -> review graduation
# (Step 9). Cleared as it's consumed.
_PRE_ANSWER_TYPE: "dict[int, int]" = {}


def _on_reviewer_will_answer(ease_tuple, reviewer, card: "Card"):
    """gui_hooks.reviewer_will_answer_card filter — fires BEFORE the answer is
    applied, so `card` is in its pre-answer state. We record the card's `type`
    (read-only) for the graduation check, then return the ease tuple UNCHANGED
    (we never alter scheduling). Fail-safe: on any error, return it unchanged.
    """
    try:
        _PRE_ANSWER_TYPE[int(card.id)] = int(card.type)
    except Exception:  # pragma: no cover - never interfere with answering
        log.exception("will-answer pre-state capture failed, ignoring")
    return ease_tuple


def _on_reviewer_did_answer(reviewer, card: "Card", ease: int) -> None:
    """gui_hooks.reviewer_did_answer_card callback.

    Fires AFTER the answer is committed (card reloaded), so the card's
    post-answer state is what we read. Two independent, fail-safe jobs:
      1. Step 9 — on-demand auto-generation for SOURCE cards in assigned decks
         (fires once, at learning->review graduation).
      2. Steps 3–5 — pipeline stage transitions for our own AI cards (keyed on
         the sidecar `current_card_id`; source/AI decks never overlap).

    Each is wrapped so a failure in one never breaks the review or the other.
    An OUTER guard wraps the whole body too, so there is provably no path from a
    reviewer answer to an unhandled raise (the no-popup rule).
    """
    try:
        try:
            pre_type = _PRE_ANSWER_TYPE.pop(int(card.id), None)
        except Exception:  # pragma: no cover - defensive
            log.exception("did-answer pre-type pop failed, ignoring")
            pre_type = None

        from aqt import mw

        from . import db

        # Job 1: Step-9 auto-generation trigger.
        try:
            from . import autogen

            cfg = _config()
            conn = db.connect()
            try:
                autogen.maybe_autogenerate(mw, conn, cfg, card, pre_type)
            finally:
                conn.close()
        except Exception:  # pragma: no cover - belt and suspenders
            log.exception("autogen trigger failed, ignoring")

        # Job 2: Steps 3–5 pipeline transitions.
        try:
            from . import state_machine

            cfg = _config()
            conn = db.connect()
            try:
                state_machine.on_card_answered(mw.col, conn, card, cfg, ease)
            finally:
                conn.close()
        except Exception:  # pragma: no cover - belt and suspenders
            log.exception("answer-event promotion check failed, ignoring")
    except Exception:  # pragma: no cover - outermost no-popup guard
        log.exception("reviewer did-answer hook failed, ignoring")


def register() -> None:
    from aqt import gui_hooks

    gui_hooks.card_will_show.append(_on_card_will_show)
    gui_hooks.reviewer_will_answer_card.append(_on_reviewer_will_answer)
    gui_hooks.reviewer_did_answer_card.append(_on_reviewer_did_answer)
    log.info("reviewer_hooks registered")

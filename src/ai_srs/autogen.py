"""On-demand auto-generation (spec §9 — Step 9).

The layer that makes the add-on usable without manual per-card creation: once
the user assigns their real decks to categories (Part 1), AI cards are generated
automatically — but ONLY at the moment a source card first graduates from
learning into review, and exactly once per source card, ever.

Trigger (all must hold):
  1. the answered card's deck is assigned to a category (`deck_category_map`);
  2. the answer just transitioned the card learning/new -> review (graduation),
     detected from the card's `type` BEFORE vs AFTER the answer (read-only);
  3. we have not already generated for this source card (sidecar
     `generated_sources` ledger).

When it fires: read the source card's content, resolve its category ->
card_type (Step 8) + model (Step 7), and generate the appropriate card into the
category's Learning bucket — in the BACKGROUND so the review is never blocked.
The original card is only ever read.

The pure predicates (`is_graduation`, `should_generate`) are stdlib-only and
unit-testable. The orchestration uses Anki only via lazy, in-function imports
and injected objects, so this module imports without Anki/Qt.
"""

from __future__ import annotations

from typing import Optional

from .log import get_logger

log = get_logger()

# anki.consts.CARD_TYPE_REV — a card in the review queue (graduated). Mirrored
# here as a plain int so this module imports without Anki; verified against
# Anki 25.09.x (anki/consts.py: CARD_TYPE_REV = 2).
CARD_TYPE_REV = 2


# ---------------- pure predicates (testable) ----------------

def is_graduation(pre_type: Optional[int], post_type: Optional[int]) -> bool:
    """True iff the answer moved the card INTO review state for the first time:
    it was not a review card before and is a review card now.

    - new/learning/relearning -> review  => True (graduation; first real interval)
    - review -> review                    => False (mature card re-reviewed)
    - anything -> learning                => False (still in learning steps)
    - pre_type unknown (None)             => False (cannot confirm; stay safe —
      avoids firing on pre-existing mature cards when the pre-answer state was
      not captured).
    """
    if pre_type is None or post_type is None:
        return False
    return pre_type != CARD_TYPE_REV and post_type == CARD_TYPE_REV


def should_generate(
    *, deck_assigned: bool, graduated: bool, already_generated: bool
) -> bool:
    """The Step-9 trigger predicate: fire only when the deck is assigned to a
    category, the card just graduated to review, and we haven't already
    generated for this source card."""
    return bool(deck_assigned and graduated and not already_generated)


# ---------------- orchestration (Anki via lazy imports) ----------------

def _read_source(col, card) -> "tuple[str, Optional[str], Optional[str]]":
    """Read the source note's (word, front, back) on the main thread.
    word = first non-empty field; front = first field; back = first non-empty
    field after the front. Best effort; never raises."""
    try:
        note = card.note()
        values = [(v or "").strip() for v in note.values()]
    except Exception:  # pragma: no cover - defensive
        log.exception("autogen: could not read source note for card %s", getattr(card, "id", "?"))
        return "", None, None
    word = next((v for v in values if v), "")
    front = values[0] if values else ""
    back = next((v for v in values[1:] if v), "")
    return word, (front or None), (back or None)


def maybe_autogenerate(mw, conn, cfg: dict, card, pre_type: Optional[int]) -> bool:
    """Evaluate the trigger for an answered source card and, if it fires, claim
    idempotency and launch background generation. Returns True if it fired.

    Runs on the main thread (from the reviewer answer hook). Only fast col reads
    happen here; the slow network generation is dispatched to a background task.
    """
    from . import db, deck_ops, subjects

    # Never auto-generate from our OWN generated cards (they live in
    # `<category>::Learning|Recycled|Legacy`, which can match a broad category
    # prefix). Source cards are the user's real notes; ours carry our tags.
    try:
        note_tags = {t.lower() for t in card.note().tags}
    except Exception:  # pragma: no cover - defensive
        note_tags = set()
    if {deck_ops.AI_CARD_TAG.lower(), deck_ops.AI_LEGACY_TAG.lower()} & note_tags:
        return False

    deck_map = cfg.get("deck_category_map", {}) or {}
    deck_name = deck_ops.card_home_deck_name(mw.col, card)
    category = subjects.category_for_deck(deck_map, deck_name)
    deck_assigned = category is not None

    graduated = is_graduation(pre_type, getattr(card, "type", None))
    source_card_id = int(card.id)
    already = db.has_generated_for_source(conn, source_card_id)

    if not should_generate(
        deck_assigned=deck_assigned, graduated=graduated, already_generated=already
    ):
        return False

    # Claim idempotency BEFORE generating so the trigger is airtight once-only
    # even if generation later fails/falls back (graduated cards won't reappear
    # for days, so there is no realistic re-fire race). A False here means it
    # was already claimed — do nothing.
    source_note_id = int(card.nid)
    if not db.mark_generated_for_source(
        conn, source_card_id, source_note_id=source_note_id, category=category
    ):
        return False

    word, src_front, src_back = _read_source(mw.col, card)
    log.info(
        "autogen FIRING: source_card=%d deck=%r category=%r word=%r",
        source_card_id, deck_name, category, word,
    )
    _launch_background(mw, cfg, category, word, src_front, src_back, source_note_id)
    return True


def _launch_background(
    mw, cfg: dict, category: str, word: str,
    src_front: Optional[str], src_back: Optional[str], source_note_id: int,
) -> None:
    """Show a non-blocking notice and run generation off the main thread; create
    the card back on the main thread when it's ready. Never blocks the review.
    """
    from . import generator, strategies, subjects, ui_notify

    card_type = subjects.card_type_for_subject(cfg, category)
    prov, model = generator.resolve_for_subject(cfg, category)
    temperature = float(cfg.get("temperature", 0.3))
    retries = int(cfg.get("validation_retries", 2))
    ctx = strategies.GenContext(
        word=word, subject=category, source_front=src_front, source_back=src_back
    )

    ui_notify.generating_started()

    def task():
        # Background thread: network only, NO Anki collection access.
        content = strategies.generate_card(
            card_type, ctx, model=model, endpoint=prov.api_base,
            timeout=prov.timeout, temperature=temperature, retries=retries,
            api_format=prov.api_format, api_key=prov.api_key,
        )
        if content is not None:
            return content, False
        return strategies.fallback_card(card_type, ctx), True

    def on_done(future) -> None:
        # Back on the main thread (taskman guarantees) — safe to touch col.
        try:
            content, is_fallback = future.result()
            _finish(mw.col, category, word, content, source_note_id)
        except Exception:  # pragma: no cover - never crash a review
            log.exception("autogen: background generation/finish failed")
            return
        ui_notify.generating_done(is_fallback)

    mw.taskman.run_in_background(task, on_done)


def _finish(col, category: str, word: str, content, source_note_id: int) -> None:
    """Create the generated AI card in the category's Learning bucket and track
    it in the sidecar. Runs on the main thread."""
    from . import db, deck_ops

    new_card_id = deck_ops.create_ai_card(
        col, category, deck_ops.LEARNING_SUFFIX, word, content.front, content.back
    )
    conn = db.connect()
    try:
        word_id = db.upsert_word(conn, word, category, source_note_id=source_note_id)
        db.set_stage(conn, word_id, db.STAGE_LEARNING)
        db.set_current_card_id(conn, word_id, new_card_id)
    finally:
        conn.close()
    log.info(
        "autogen created card=%d in %s::Learning for word=%r (source_note=%d)",
        new_card_id, category, word, source_note_id,
    )

"""Deck and card operations (spec §2).

Owns: ensuring `Subject::Learning|Recycled|Legacy` decks exist; creating
fresh AI cards in the right deck; retiring (suspending) old AI cards on
promotion/demotion.

Crucially: a promotion/demotion is *never* a card move — we create a fresh
card so FSRS history doesn't follow the new sentence (spec §2, last bullet).

Anki imports are lazy / behind TYPE_CHECKING so this module stays importable
outside Anki for tooling, consistent with the rest of the package.

Step 6 adds `create_vault_note` — an ORDINARY Basic note built from a
reviewed vault candidate. Deliberately NOT tagged `AI_CARD_TAG`, so
auto-generation keeps treating it as the user's own source material.

Step 3: ensure the Recycled deck, create an AI card, retire (suspend) an old
AI card. Step 4 adds `copy_original_to_legacy` — duplicating the user's
ORIGINAL note into `Subject::Legacy` (traditional, not an AI sentence). That
copy is strictly read-only against the source note (spec §2 — Original is
sacred): it reads fields/tags and writes a brand-new note; it never flushes,
moves, suspends, or reschedules the original.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .log import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from anki.collection import Collection

log = get_logger()

LEARNING_SUFFIX = "Learning"
RECYCLED_SUFFIX = "Recycled"
LEGACY_SUFFIX = "Legacy"

# Marker tag on every AI-generated card so they're identifiable. Deliberately
# NOT the Step-1 generation trigger tag (config `trigger_tag`, default
# "ai-srs"): the generated sentence is baked into the note here, so we must
# not also trip the reviewer's on-the-fly card_will_show generation.
AI_CARD_TAG = "ai-srs-generated"

# Marker tag on Legacy copies (a traditional copy of the original note).
AI_LEGACY_TAG = "ai-srs-legacy"


def subject_deck_name(subject: str, suffix: str) -> str:
    """`Japanese`, `Learning` -> `Japanese::Learning` (spec §2 / §7)."""
    return f"{subject}::{suffix}"


def ensure_deck(col: "Collection", name: str) -> int:
    """Return the deck id for `name`, creating the deck if it doesn't exist.

    We only ever ensure our own AI decks (Learning/Recycled/Legacy). Original
    is never created or touched here.
    """
    return int(col.decks.id(name, create=True))


def _resolve_ai_notetype(col: "Collection"):
    """Pick a notetype for AI cards: prefer the built-in "Basic", else the
    first available notetype in the collection.
    """
    nt = col.models.by_name("Basic")
    if nt is not None:
        return nt
    all_nt = col.models.all()
    if not all_nt:
        raise RuntimeError("no notetypes available to create an AI card")
    return all_nt[0]


def create_ai_card(
    col: "Collection",
    subject: str,
    suffix: str,
    word: str,
    front: str,
    back: str,
) -> int:
    """Create a fresh AI note+card with the given `front`/`back` in
    `Subject::<suffix>`. The Front/Back content is built by the per-subject
    card-type strategy (spec §8); this function is format-agnostic. `word` is
    used for logging/identity. Tagged `AI_CARD_TAG`. Returns the new card's id.

    This is a brand-new card with no scheduling history, by design (spec §2:
    "moving" a word is create-fresh + retire-old, never a card move).
    """
    deck_id = ensure_deck(col, subject_deck_name(subject, suffix))
    notetype = _resolve_ai_notetype(col)
    note = col.new_note(notetype)
    note.fields[0] = front
    if len(note.fields) >= 2:
        note.fields[1] = back
    note.add_tag(AI_CARD_TAG)
    col.add_note(note, deck_id)
    cards = note.cards()
    if not cards:
        raise RuntimeError(f"add_note produced no cards for word={word!r}")
    new_card_id = int(cards[0].id)
    log.info(
        "created AI card id=%d deck=%r word=%r",
        new_card_id, subject_deck_name(subject, suffix), word,
    )
    return new_card_id


def create_vault_note(
    col: "Collection",
    deck_name: str,
    front: str,
    back: str,
    *,
    tag: str = "engram-vault",
) -> int:
    """Create an ordinary Basic note in `deck_name` from a reviewed vault card.

    Unlike `create_ai_card` this takes a FULL deck name (the review dialog
    resolves and may override it) and does NOT apply `AI_CARD_TAG`. That
    distinction is the whole point: a vault card is source material, so it must
    look to the rest of the add-on exactly like a note the user typed — and stay
    eligible for auto-generation, which skips anything carrying AI_CARD_TAG.

    The deck is created if it doesn't exist (a category whose source material
    arrives from the vault before it has a deck). Returns the new note's id.
    """
    deck_id = ensure_deck(col, deck_name)
    notetype = _resolve_ai_notetype(col)
    note = col.new_note(notetype)
    note.fields[0] = front
    if len(note.fields) >= 2:
        note.fields[1] = back
    if tag:
        note.add_tag(tag)
    col.add_note(note, deck_id)
    log.info(
        "created vault note id=%s deck=%r front=%r",
        note.id, deck_name, front[:60],
    )
    return int(note.id)


def copy_original_to_legacy(
    col: "Collection",
    source_note_id: int,
    subject: str,
    exclude_tags: "tuple[str, ...]" = (),
) -> int:
    """Duplicate the ORIGINAL note (`source_note_id`) into `Subject::Legacy`
    as a fresh, traditionally-scheduled note (spec §2 — Legacy = a copy of the
    original card, NOT an AI sentence). Returns the new card's id.

    Read-only against the source: we read its notetype, fields and tags, then
    build and add a NEW note. The source note is never flushed, moved,
    suspended, or rescheduled. The new note's cards are brand-new (no
    scheduling history follows the copy).

    `exclude_tags` (case-insensitive) are dropped from the copied tags — used
    to strip the generation `trigger_tag` so a Legacy copy can never trip the
    reviewer's on-the-fly card_will_show generation.
    """
    src = col.get_note(int(source_note_id))  # read-only fetch; raises if gone

    notetype = src.note_type()
    new = col.new_note(notetype)
    for i in range(len(new.fields)):
        if i < len(src.fields):
            new.fields[i] = src.fields[i]

    excluded = {t.lower() for t in exclude_tags}
    for tag in src.tags:
        if tag.lower() not in excluded:
            new.add_tag(tag)
    new.add_tag(AI_LEGACY_TAG)

    deck_id = ensure_deck(col, subject_deck_name(subject, LEGACY_SUFFIX))
    col.add_note(new, deck_id)
    cards = new.cards()
    if not cards:
        raise RuntimeError(
            f"add_note produced no cards copying note {source_note_id}"
        )
    new_card_id = int(cards[0].id)
    log.info(
        "copied original note=%d -> legacy card=%d deck=%r",
        int(source_note_id), new_card_id,
        subject_deck_name(subject, LEGACY_SUFFIX),
    )
    return new_card_id


def retire_card(col: "Collection", card_id: int) -> None:
    """Retire an old AI card by suspending it (recoverable; preferred over
    delete during development — spec §2 retires the old card on a swap).
    """
    col.sched.suspend_cards([int(card_id)])
    log.info("retired (suspended) AI card id=%d", int(card_id))


def card_home_deck_name(col: "Collection", card) -> str:
    """Home deck name for a card, resolving filtered-deck cards back to their
    original deck (`odid`). Read-only.
    """
    did = card.odid or card.did
    return col.decks.name(did)

"""Reviewing vault candidates and importing the accepted ones (step 6).

`vault_ingest` stops at "here are candidate cards". This module owns everything
between that and real notes in the user's collection:

  1. **Destination** (`resolve_destination_deck`) — which real deck a category's
     cards land in, derived from the `deck_category_map` the user already built
     in *Setup categories…*.
  2. **Review state** (`CandidateReview`, `ReviewSession`) — accept / reject /
     edit per candidate, and which of them an import would actually write.
  3. **Duplicates** (`find_duplicate_notes`, `mark_duplicates`) — candidates
     whose front already exists in the collection, so re-scanning a note you
     already imported from doesn't quietly re-add everything.
  4. **Import** (`import_reviews`) — the accepted candidates as ordinary Basic
     notes, in one undo step.

The split is deliberate: parts 1–3 are stdlib-only and unit-testable, part 4 is
the only piece that needs a live collection. The Qt dialog (`vault_dialog.py`)
holds NO decision logic — it edits a `ReviewSession` and calls `import_reviews`,
so what gets imported is testable without driving a UI.

Imported notes are tagged `engram-vault` — deliberately NOT `deck_ops.
AI_CARD_TAG`, which auto-generation uses to recognize and skip its own output.
Vault cards are source material and MUST stay eligible for the practice
pipeline, exactly like a hand-typed note.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import deck_ops
from .log import get_logger

log = get_logger()

# Marker tag on every note imported from the vault. Lets the user find
# (`tag:engram-vault`) or undo a whole import in the browser later. See the
# module docstring for why this is not AI_CARD_TAG.
VAULT_TAG = "engram-vault"

# Suffix used when a category has no real deck assigned to it. Matches the
# spec's naming for a subject's source deck (§2: `Subject::Original`).
ORIGINAL_SUFFIX = "Original"

# Engram's own generated decks. A category's cards must never be imported into
# one of these even if a stale `deck_category_map` entry points at it —
# Learning/Recycled/Legacy hold generated practice cards, not source material.
_GENERATED_SUFFIXES = (
    deck_ops.LEARNING_SUFFIX,
    deck_ops.RECYCLED_SUFFIX,
    deck_ops.LEGACY_SUFFIX,
)


# --------------------------------------------------------------------------
# destination deck
# --------------------------------------------------------------------------

def decks_for_category(cfg: dict, category: str) -> "list[str]":
    """Every real deck assigned to `category`, best destination first.

    `deck_category_map` runs deck → category and is many-to-one, so this reverse
    lookup can legitimately find several decks. Order is shortest-then-
    alphabetical: the shortest name is the least specific one (`Physics::Core`
    over `Physics::Core::Ch1`), which is the better home for a card that came
    from a whole week's notes rather than one chapter.

    Engram's own generated decks are excluded — see `_GENERATED_SUFFIXES`.
    """
    if not category:
        return []
    deck_map = cfg.get("deck_category_map", {}) or {}
    if not isinstance(deck_map, dict):
        return []
    out = []
    for deck, cat in deck_map.items():
        if not isinstance(deck, str) or not isinstance(cat, str):
            continue
        if cat.strip() != category.strip():
            continue
        deck = deck.strip()
        if not deck or deck.split("::")[-1] in _GENERATED_SUFFIXES:
            continue
        out.append(deck)
    return sorted(set(out), key=lambda d: (len(d), d))


def resolve_destination_deck(cfg: dict, category: Optional[str]) -> str:
    """The deck a category's accepted cards import into.

    The deck the user already assigned to that category in *Setup categories…*,
    or `<Category>::Original` when nothing is assigned — a deck that may not
    exist yet and will be created on import, which is the right behaviour for a
    category whose source material is arriving from the vault first.

    Only ever a DEFAULT: the review dialog shows this and lets the user change
    it before importing.
    """
    if not category:
        return ""
    assigned = decks_for_category(cfg, category)
    if assigned:
        return assigned[0]
    return deck_ops.subject_deck_name(category, ORIGINAL_SUFFIX)


# --------------------------------------------------------------------------
# review state
# --------------------------------------------------------------------------

@dataclass
class CandidateReview:
    """One candidate card plus the user's decision about it.

    Mutable by design — the dialog edits these in place. `front`/`back` are the
    CURRENT (possibly edited) text; `original_front`/`original_back` keep what
    the model proposed so an edit is visible and the import log can say what
    changed.
    """
    front: str
    back: str
    source_path: str = ""
    category: Optional[str] = None
    accepted: bool = True
    duplicate_of: Optional[int] = None   # note id whose front already matches
    original_front: str = ""
    original_back: str = ""

    def __post_init__(self) -> None:
        if not self.original_front:
            self.original_front = self.front
        if not self.original_back:
            self.original_back = self.back

    @classmethod
    def from_candidate(cls, candidate) -> "CandidateReview":
        """Wrap a `vault_ingest.Candidate`, accepted by default."""
        return cls(
            front=candidate.front,
            back=candidate.back,
            source_path=candidate.source_path,
            category=candidate.category,
        )

    @property
    def is_duplicate(self) -> bool:
        return self.duplicate_of is not None

    @property
    def edited(self) -> bool:
        """True once the user has changed either side of the card."""
        return (
            self.front.strip() != self.original_front.strip()
            or self.back.strip() != self.original_back.strip()
        )

    @property
    def importable(self) -> bool:
        """Accepted AND still a usable card. An edit that empties a side is a
        rejection in effect — importing a blank-fronted note would create a
        card the user can never answer."""
        return bool(self.accepted and self.front.strip() and self.back.strip())


@dataclass
class ReviewSession:
    """Every candidate from one scan, plus where each category's cards go.

    Holds the whole decision state for the review dialog. Nothing here touches
    Anki, so "what would this import write" is answerable in a test.
    """
    reviews: "list[CandidateReview]" = field(default_factory=list)
    destinations: dict = field(default_factory=dict)   # category -> deck name

    @classmethod
    def from_candidates(cls, candidates, cfg: dict) -> "ReviewSession":
        """Build a session from `vault_ingest.Candidate`s, defaulting each
        category's destination from `deck_category_map`."""
        reviews = [CandidateReview.from_candidate(c) for c in candidates]
        session = cls(reviews=reviews)
        for category in session.categories:
            session.destinations[category] = resolve_destination_deck(cfg, category)
        return session

    @property
    def categories(self) -> "list[str]":
        """Categories present in this batch, sorted. A candidate with no
        category cannot be imported (nowhere to put it) and is excluded."""
        return sorted({r.category for r in self.reviews if r.category})

    def destination_for(self, review: CandidateReview) -> str:
        """The deck `review` would import into, or "" if it has none."""
        if not review.category:
            return ""
        return (self.destinations.get(review.category) or "").strip()

    def accept_all(self, *, include_duplicates: bool = False) -> int:
        """Accept every candidate; returns how many changed. Duplicates stay
        rejected unless explicitly included — the whole point of flagging them
        is that a blanket "accept all" shouldn't re-import them."""
        changed = 0
        for r in self.reviews:
            if r.is_duplicate and not include_duplicates:
                continue
            if not r.accepted:
                r.accepted = True
                changed += 1
        return changed

    def reject_all(self) -> int:
        changed = 0
        for r in self.reviews:
            if r.accepted:
                r.accepted = False
                changed += 1
        return changed

    def plan(self) -> "list[tuple]":
        """`(review, deck_name)` for everything an import would actually write.

        The single source of truth for "what gets imported": accepted, non-empty
        on both sides, and with a resolved destination deck. A candidate whose
        category has no deck is dropped here rather than at import time, so the
        dialog can show the same count the import will produce.
        """
        out = []
        for r in self.reviews:
            if not r.importable:
                continue
            deck = self.destination_for(r)
            if not deck:
                continue
            out.append((r, deck))
        return out

    def counts(self) -> dict:
        """Summary counts for the dialog header and the post-import report."""
        planned = self.plan()
        return {
            "total": len(self.reviews),
            "accepted": sum(1 for r in self.reviews if r.accepted),
            "rejected": sum(1 for r in self.reviews if not r.accepted),
            "duplicates": sum(1 for r in self.reviews if r.is_duplicate),
            "edited": sum(1 for r in self.reviews if r.edited),
            "importable": len(planned),
            "no_destination": sum(
                1 for r in self.reviews
                if r.importable and not self.destination_for(r)
            ),
        }

    def source_paths(self) -> "list[str]":
        """Every vault note represented in this batch, sorted."""
        return sorted({r.source_path for r in self.reviews if r.source_path})


# --------------------------------------------------------------------------
# duplicates
# --------------------------------------------------------------------------

def mark_duplicates(session: ReviewSession, dupes: dict) -> int:
    """Apply a `front text -> existing note id` map to a session.

    A flagged candidate is set to REJECTED, so the default action on a re-scan
    is "change nothing". The user can still flip one back on — the flag is
    information, not a veto. Returns how many were flagged.
    """
    flagged = 0
    for review in session.reviews:
        nid = (dupes or {}).get(review.front.strip())
        if nid is None:
            continue
        review.duplicate_of = int(nid)
        review.accepted = False
        flagged += 1
    if flagged:
        log.info("vault import: %d candidate(s) already in the collection", flagged)
    return flagged


def find_duplicate_notes(col, fronts) -> dict:
    """`front text -> existing note id` for fronts already in the collection.

    Uses Anki's own duplicate rule (first-field checksum via the `dupe:` search
    node) against the Basic notetype vault cards are created with, so what we
    call a duplicate is what the Anki browser calls one.

    Best-effort: any failure returns fewer entries, never an exception. A
    collection where dupe detection doesn't work should still be importable —
    the user just loses the pre-warning.
    """
    out: dict = {}
    try:
        from anki.collection import SearchNode

        notetype = deck_ops._resolve_ai_notetype(col)
        mid = int(notetype["id"])
    except Exception as e:  # noqa: BLE001 — advisory feature, never fatal
        log.warning("vault import: duplicate check unavailable: %s", e)
        return out

    for front in fronts:
        text = (front or "").strip()
        if not text or text in out:
            continue
        try:
            search = col.build_search_string(
                SearchNode(dupe=SearchNode.Dupe(notetype_id=mid, first_field=text))
            )
            nids = col.find_notes(search)
        except Exception as e:  # noqa: BLE001
            log.warning("vault import: duplicate check failed for %r: %s", text[:60], e)
            continue
        if nids:
            out[text] = int(nids[0])
    return out


# --------------------------------------------------------------------------
# import
# --------------------------------------------------------------------------

@dataclass
class ImportReport:
    """What an import actually did — shown to the user and used to update the
    ingestion ledger."""
    imported: int = 0
    failed: int = 0
    by_deck: dict = field(default_factory=dict)          # deck name -> count
    by_source: dict = field(default_factory=dict)        # vault path -> count
    errors: "list[str]" = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.failed == 0


def import_reviews(col, session: ReviewSession, *, tag: str = VAULT_TAG) -> ImportReport:
    """Write every accepted candidate into its destination deck as a Basic note.

    One undo entry for the whole batch (`add_custom_undo_entry` +
    `merge_undo_entries`), so a mis-aimed import is a single Ctrl+Z rather than
    forty. If the undo API is unavailable the import still runs — it just
    undoes one note at a time.

    A single failing note is logged and counted, not raised: the other accepted
    cards should still land. Returns the report; the caller updates the vault
    ledger from `by_source`.
    """
    report = ImportReport()
    planned = session.plan()
    if not planned:
        return report

    undo_target = None
    try:
        undo_target = col.add_custom_undo_entry(f"Import {len(planned)} card(s) from vault")
    except Exception as e:  # noqa: BLE001 — undo merging is a nicety
        log.warning("vault import: could not open an undo entry: %s", e)

    for review, deck_name in planned:
        try:
            deck_ops.create_vault_note(
                col, deck_name, review.front.strip(), review.back.strip(), tag=tag
            )
        except Exception as e:  # noqa: BLE001 — one bad note must not stop the rest
            log.exception("vault import: failed to create note in %r", deck_name)
            report.failed += 1
            report.errors.append(f"{review.front[:60]!r} → {deck_name}: {e}")
            continue
        report.imported += 1
        report.by_deck[deck_name] = report.by_deck.get(deck_name, 0) + 1
        if review.source_path:
            report.by_source[review.source_path] = (
                report.by_source.get(review.source_path, 0) + 1
            )

    if undo_target is not None:
        try:
            col.merge_undo_entries(undo_target)
        except Exception as e:  # noqa: BLE001
            log.warning("vault import: could not merge undo entries: %s", e)

    log.info(
        "vault import: %d note(s) created, %d failed, decks=%s",
        report.imported, report.failed, report.by_deck,
    )
    return report


def record_ingested(conn, notes, report: ImportReport) -> int:
    """Mark every reviewed note as ingested, with its imported-card count.

    Every note that was REVIEWED is marked, not just the ones that produced an
    import. Rejecting a note's candidates is a decision, and re-offering the
    same rejected cards on every future scan would make the ledger useless. A
    genuinely re-edited note comes back anyway — its mtime moves.

    Called only after the user commits the import; cancelling the dialog leaves
    the ledger untouched so nothing is silently marked as done.
    """
    from . import vault_ingest

    marked = 0
    for note in notes:
        try:
            vault_ingest.mark_ingested(
                conn, note, cards_imported=report.by_source.get(note.rel_path, 0)
            )
            marked += 1
        except Exception:  # noqa: BLE001 — a ledger write must not lose the import
            log.exception("vault import: could not mark %s as ingested", note.rel_path)
    return marked

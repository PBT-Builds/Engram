"""Populate the sidecar DB from an Original deck (spec §2, §8.2).

Read-only against Anki's collection: we enumerate notes that have a card in
the given deck, pull the configured target-word field, and upsert one row
per (word, subject) into the sidecar. We never edit, move, suspend, or
reschedule anything in Original (spec §2 — the Original deck is sacred).

Idempotent: re-running over the same deck does not create duplicates,
courtesy of UNIQUE(word, subject) + the upsert path in db.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterator, Optional

from . import db
from .log import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from anki.collection import Collection

log = get_logger()


@dataclass
class PopulateReport:
    scanned: int = 0
    upserted: int = 0
    skipped_empty: int = 0
    skipped_missing_field: int = 0


def _quote_deck(name: str) -> str:
    # Anki search uses backslash escapes inside double-quoted literals.
    return name.replace("\\", "\\\\").replace('"', '\\"')


def _strip_html(value: str) -> str:
    # Lazy import: anki.utils is only available inside Anki.
    try:
        from anki.utils import strip_html  # type: ignore

        return strip_html(value)
    except Exception:  # pragma: no cover - tooling/standalone path
        return value


def _resolve_field_index(note, word_field: Optional[str]) -> int:
    """Return the field index for the configured `word_field` on this note's
    notetype, or 0 if no override is configured. Raises LookupError if the
    field name is configured but missing on this notetype.
    """
    if not word_field:
        return 0
    nt = note.note_type()
    for i, fld in enumerate(nt["flds"]):
        if fld["name"] == word_field:
            return i
    raise LookupError(word_field)


def _iter_words(
    col: "Collection",
    deck_name: str,
    word_field: Optional[str],
    report: PopulateReport,
) -> Iterator[tuple[str, int]]:
    """Yield (word, source_note_id) pairs for every note in the deck."""
    query = f'deck:"{_quote_deck(deck_name)}"'
    note_ids = col.find_notes(query)
    # Cache field index per notetype id so we don't re-scan flds per note.
    idx_cache: dict[int, Optional[int]] = {}

    for nid in note_ids:
        report.scanned += 1
        note = col.get_note(nid)
        mid = note.mid
        if mid not in idx_cache:
            try:
                idx_cache[mid] = _resolve_field_index(note, word_field)
            except LookupError:
                idx_cache[mid] = None
        field_idx = idx_cache[mid]
        if field_idx is None:
            report.skipped_missing_field += 1
            continue
        try:
            raw = note.fields[field_idx]
        except IndexError:
            report.skipped_missing_field += 1
            continue
        word = _strip_html(raw or "").strip()
        if not word:
            report.skipped_empty += 1
            continue
        yield word, int(nid)


def populate_from_deck(
    col: "Collection",
    subject: str,
    deck_name: str,
    word_field: Optional[str] = None,
) -> PopulateReport:
    """Walk every note that has a card in `deck_name` and upsert one sidecar
    row per (word, subject). Read-only against Anki's collection.
    """
    if not subject or not subject.strip():
        raise ValueError("subject is required")
    if not deck_name or not deck_name.strip():
        raise ValueError("deck_name is required")

    subject = subject.strip()
    deck_name = deck_name.strip()
    report = PopulateReport()

    conn = db.connect()
    try:
        items = (
            (word, subject, source_note_id)
            for word, source_note_id in _iter_words(col, deck_name, word_field, report)
        )
        report.upserted = db.bulk_upsert_words(conn, items)
    finally:
        conn.close()

    log.info(
        "populate subject=%r deck=%r scanned=%d upserted=%d "
        "skipped_empty=%d skipped_missing_field=%d",
        subject, deck_name, report.scanned, report.upserted,
        report.skipped_empty, report.skipped_missing_field,
    )
    return report

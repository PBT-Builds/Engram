"""Sidecar SQLite database (spec §3).

Pipeline state is keyed on *words*, which are not native Anki objects. We
keep our own DB inside the add-on's `user_files/` so FSRS scheduling state
stays cleanly separate (spec §3, §9).

This module is deliberately Anki-agnostic — it takes an `addon_dir`, opens a
SQLite file, and exposes CRUD. Code that reads from Anki's collection lives
in populate.py so db.py is unit-testable outside Anki.
"""

from __future__ import annotations

import os
import sqlite3
import time
from typing import Any, Iterable, Optional

STAGE_LEARNING = "learning"
STAGE_RECYCLED = "recycled"
STAGE_LEGACY = "legacy"
VALID_STAGES = frozenset({STAGE_LEARNING, STAGE_RECYCLED, STAGE_LEGACY})

SCHEMA = """
CREATE TABLE IF NOT EXISTS words (
    id              INTEGER PRIMARY KEY,
    word            TEXT NOT NULL,
    subject         TEXT NOT NULL,
    source_note_id  INTEGER,
    stage           TEXT NOT NULL,
    recycle_count   INTEGER NOT NULL DEFAULT 0,
    fail_streak     INTEGER NOT NULL DEFAULT 0,
    current_card_id INTEGER,
    created_at      INTEGER,
    updated_at      INTEGER,
    UNIQUE(word, subject)
);
CREATE INDEX IF NOT EXISTS ix_words_subject_stage ON words(subject, stage);

-- Idempotency ledger for Step-9 on-demand auto-generation: one row per SOURCE
-- card we have already generated an AI card for, so the trigger fires exactly
-- once per source card, ever (even though the card keeps coming up for review).
CREATE TABLE IF NOT EXISTS generated_sources (
    source_card_id  INTEGER PRIMARY KEY,
    source_note_id  INTEGER,
    category        TEXT,
    generated_at    INTEGER
);

-- Obsidian vault ingestion: one row per vault file we have already extracted
-- candidate cards from. `last_ingested_mtime` is the file's modification time
-- AT THE MOMENT WE INGESTED IT, so a later scan re-processes only notes that
-- actually changed — a weekly vault is not re-sent to the LLM every run.
CREATE TABLE IF NOT EXISTS vault_files (
    path                TEXT PRIMARY KEY,   -- vault-relative, '/'-separated
    last_ingested_mtime REAL NOT NULL,
    last_ingested_at    INTEGER,
    cards_imported      INTEGER NOT NULL DEFAULT 0
);

-- Small key/value store for the DB's own bookkeeping (e.g. which collection it
-- belongs to — see profile_store).
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# Columns that internal setters are allowed to write to. Keeps the dynamic
# UPDATE in _update_field from ever being driven by untrusted input.
_UPDATABLE_COLUMNS = frozenset(
    {"stage", "recycle_count", "fail_streak", "current_card_id"}
)


def default_addon_dir() -> str:
    """The add-on's own folder (the directory containing this module)."""
    return os.path.dirname(os.path.realpath(__file__))


def db_path(addon_dir: Optional[str] = None) -> str:
    base = addon_dir or default_addon_dir()
    user_files = os.path.join(base, "user_files")
    os.makedirs(user_files, exist_ok=True)
    return os.path.join(user_files, "pipeline.sqlite")


def connect(addon_dir: Optional[str] = None) -> sqlite3.Connection:
    """Open the sidecar DB.

    With an explicit `addon_dir` (tests, scripts) this is that folder's
    `user_files/pipeline.sqlite`. With none, inside Anki it is the CURRENT
    PROFILE's DB (`user_files/profiles/<profile>/pipeline.sqlite`, see
    profile_store) so card IDs never cross collections; outside Anki it falls
    back to the add-on's own `user_files/pipeline.sqlite`.
    """
    if addon_dir is None:
        from . import profile_runtime

        path = profile_runtime.current_db_path()
        if path is not None:
            return connect_path(path)
    return connect_path(db_path(addon_dir))


def connect_path(path: str) -> sqlite3.Connection:
    """Open (creating if needed) a sidecar DB at an explicit file path."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    with conn:
        conn.executescript(SCHEMA)
    return conn


def _now() -> int:
    return int(time.time())


def _row(row: Optional[sqlite3.Row]) -> Optional[dict]:
    return dict(row) if row is not None else None


# ---------- create / upsert ----------

_UPSERT_SQL = """
INSERT INTO words (
    word, subject, source_note_id, stage,
    recycle_count, fail_streak, current_card_id,
    created_at, updated_at
)
VALUES (?, ?, ?, ?, 0, 0, NULL, ?, ?)
ON CONFLICT(word, subject) DO UPDATE SET
    source_note_id = COALESCE(words.source_note_id, excluded.source_note_id),
    updated_at     = excluded.updated_at
"""


def upsert_word(
    conn: sqlite3.Connection,
    word: str,
    subject: str,
    source_note_id: Optional[int] = None,
) -> int:
    """Insert a new word in `learning` (recycle_count=0, fail_streak=0) or, if
    (word, subject) already exists, refresh metadata only — preserving the
    existing stage / counters / current_card_id and only filling in
    source_note_id if it was previously NULL. Bumps updated_at.

    Returns the row id.
    """
    if not word or not subject:
        raise ValueError("word and subject are required")
    now = _now()
    with conn:
        conn.execute(
            _UPSERT_SQL,
            (word, subject, source_note_id, STAGE_LEARNING, now, now),
        )
        r = conn.execute(
            "SELECT id FROM words WHERE word = ? AND subject = ?",
            (word, subject),
        ).fetchone()
    return int(r["id"])


def bulk_upsert_words(
    conn: sqlite3.Connection,
    items: Iterable[tuple[str, str, Optional[int]]],
) -> int:
    """Upsert many (word, subject, source_note_id) rows in ONE transaction.

    Items with empty word/subject are silently skipped. Returns the number
    of rows actually written.
    """
    now = _now()
    written = 0
    with conn:
        for word, subject, source_note_id in items:
            if not word or not subject:
                continue
            conn.execute(
                _UPSERT_SQL,
                (word, subject, source_note_id, STAGE_LEARNING, now, now),
            )
            written += 1
    return written


# ---------- read ----------

def get_word(
    conn: sqlite3.Connection, word: str, subject: str
) -> Optional[dict]:
    r = conn.execute(
        "SELECT * FROM words WHERE word = ? AND subject = ?",
        (word, subject),
    ).fetchone()
    return _row(r)


def get_word_by_id(conn: sqlite3.Connection, word_id: int) -> Optional[dict]:
    r = conn.execute(
        "SELECT * FROM words WHERE id = ?", (word_id,)
    ).fetchone()
    return _row(r)


def get_word_by_current_card(
    conn: sqlite3.Connection, card_id: int
) -> Optional[dict]:
    """Find the word whose live AI card is `card_id`, or None.

    This is how the reviewer's answer-event hook maps an answered card back to
    its pipeline word (the sidecar is the source of truth for that link).
    """
    r = conn.execute(
        "SELECT * FROM words WHERE current_card_id = ?", (int(card_id),)
    ).fetchone()
    return _row(r)


def list_by_subject(conn: sqlite3.Connection, subject: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM words WHERE subject = ? ORDER BY word",
        (subject,),
    ).fetchall()
    return [dict(r) for r in rows]


def list_by_stage(
    conn: sqlite3.Connection, subject: str, stage: str
) -> list[dict]:
    if stage not in VALID_STAGES:
        raise ValueError(f"invalid stage: {stage!r}")
    rows = conn.execute(
        "SELECT * FROM words WHERE subject = ? AND stage = ? ORDER BY word",
        (subject, stage),
    ).fetchall()
    return [dict(r) for r in rows]


def count_by_subject(conn: sqlite3.Connection, subject: str) -> int:
    r = conn.execute(
        "SELECT COUNT(*) AS n FROM words WHERE subject = ?", (subject,)
    ).fetchone()
    return int(r["n"])


# ---------- update ----------

def _update_field(
    conn: sqlite3.Connection, word_id: int, column: str, value: Any
) -> None:
    if column not in _UPDATABLE_COLUMNS:
        raise ValueError(f"column not updatable: {column!r}")
    with conn:
        cur = conn.execute(
            f"UPDATE words SET {column} = ?, updated_at = ? WHERE id = ?",
            (value, _now(), word_id),
        )
        if cur.rowcount == 0:
            raise KeyError(f"word id {word_id} not found")


def set_stage(conn: sqlite3.Connection, word_id: int, stage: str) -> None:
    if stage not in VALID_STAGES:
        raise ValueError(f"invalid stage: {stage!r}")
    _update_field(conn, word_id, "stage", stage)


def set_recycle_count(
    conn: sqlite3.Connection, word_id: int, count: int
) -> None:
    if count < 0:
        raise ValueError("recycle_count must be >= 0")
    _update_field(conn, word_id, "recycle_count", int(count))


def set_fail_streak(
    conn: sqlite3.Connection, word_id: int, streak: int
) -> None:
    if streak < 0:
        raise ValueError("fail_streak must be >= 0")
    _update_field(conn, word_id, "fail_streak", int(streak))


def set_current_card_id(
    conn: sqlite3.Connection, word_id: int, card_id: Optional[int]
) -> None:
    _update_field(
        conn, word_id, "current_card_id",
        None if card_id is None else int(card_id),
    )


# ---------- transitions (atomic multi-field updates) ----------

def promote_word(
    conn: sqlite3.Connection,
    word_id: int,
    *,
    stage: str,
    current_card_id: Optional[int],
) -> None:
    """Apply a promotion in a SINGLE transaction so the row can't be left
    half-updated: sets `stage`, repoints `current_card_id`, and resets
    `fail_streak` to 0. `recycle_count` is deliberately left unchanged
    (spec §4: learning -> recycled leaves recycle_count alone).
    """
    if stage not in VALID_STAGES:
        raise ValueError(f"invalid stage: {stage!r}")
    card = None if current_card_id is None else int(current_card_id)
    with conn:
        cur = conn.execute(
            "UPDATE words SET stage = ?, current_card_id = ?, "
            "fail_streak = 0, updated_at = ? WHERE id = ?",
            (stage, card, _now(), word_id),
        )
        if cur.rowcount == 0:
            raise KeyError(f"word id {word_id} not found")


def cycle_word(
    conn: sqlite3.Connection,
    word_id: int,
    *,
    new_card_id: int,
    recycle_count: int,
) -> None:
    """A successful recycle cycle, in ONE transaction (spec §4 row 2): stays in
    `recycled`, repoints `current_card_id` to the freshly generated card, sets
    the new `recycle_count`, and resets `fail_streak` to 0.
    """
    if recycle_count < 0:
        raise ValueError("recycle_count must be >= 0")
    with conn:
        cur = conn.execute(
            "UPDATE words SET stage = ?, current_card_id = ?, "
            "recycle_count = ?, fail_streak = 0, updated_at = ? WHERE id = ?",
            (STAGE_RECYCLED, int(new_card_id), int(recycle_count), _now(), word_id),
        )
        if cur.rowcount == 0:
            raise KeyError(f"word id {word_id} not found")


def graduate_word(
    conn: sqlite3.Connection,
    word_id: int,
    *,
    recycle_count: int,
) -> None:
    """Graduate to `legacy`, in ONE transaction (spec §4 row 3): stage=legacy,
    `current_card_id` cleared to NULL (spec §3 — legacy rows have no live AI
    card), records the final `recycle_count`, and resets `fail_streak` to 0.
    """
    if recycle_count < 0:
        raise ValueError("recycle_count must be >= 0")
    with conn:
        cur = conn.execute(
            "UPDATE words SET stage = ?, current_card_id = NULL, "
            "recycle_count = ?, fail_streak = 0, updated_at = ? WHERE id = ?",
            (STAGE_LEGACY, int(recycle_count), _now(), word_id),
        )
        if cur.rowcount == 0:
            raise KeyError(f"word id {word_id} not found")


def increment_fail_streak(conn: sqlite3.Connection, word_id: int) -> int:
    """Atomically bump `fail_streak` by one and return the new value. Used on a
    failed review in the recycled stage (spec §4 row 4); demotion fires when
    the returned streak reaches `demotion_fails`.
    """
    with conn:
        cur = conn.execute(
            "UPDATE words SET fail_streak = fail_streak + 1, updated_at = ? "
            "WHERE id = ?",
            (_now(), word_id),
        )
        if cur.rowcount == 0:
            raise KeyError(f"word id {word_id} not found")
        r = conn.execute(
            "SELECT fail_streak FROM words WHERE id = ?", (word_id,)
        ).fetchone()
    return int(r["fail_streak"])


# ---------- Step-9 auto-generation idempotency ledger ----------

def has_generated_for_source(
    conn: sqlite3.Connection, source_card_id: int
) -> bool:
    """True if we have already auto-generated an AI card for this SOURCE card."""
    r = conn.execute(
        "SELECT 1 FROM generated_sources WHERE source_card_id = ?",
        (int(source_card_id),),
    ).fetchone()
    return r is not None


def mark_generated_for_source(
    conn: sqlite3.Connection,
    source_card_id: int,
    *,
    source_note_id: Optional[int] = None,
    category: Optional[str] = None,
) -> bool:
    """Atomically claim a source card as generated. Returns True if this call
    newly claimed it, False if it was already claimed — so the caller can treat
    a False as "someone already handled this; do nothing" (airtight once-only).
    """
    with conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO generated_sources "
            "(source_card_id, source_note_id, category, generated_at) "
            "VALUES (?, ?, ?, ?)",
            (
                int(source_card_id),
                None if source_note_id is None else int(source_note_id),
                category,
                _now(),
            ),
        )
    return cur.rowcount > 0


# ---------- Obsidian vault ingestion ledger ----------

def get_vault_file(conn: sqlite3.Connection, path: str) -> Optional[dict]:
    r = conn.execute(
        "SELECT * FROM vault_files WHERE path = ?", (path,)
    ).fetchone()
    return _row(r)


def last_ingested_mtime(
    conn: sqlite3.Connection, path: str
) -> Optional[float]:
    """The mtime this file had when we last ingested it, or None if never.

    The caller compares it against the file's CURRENT mtime: unchanged (or
    older) means skip, newer means re-extract.
    """
    r = conn.execute(
        "SELECT last_ingested_mtime FROM vault_files WHERE path = ?", (path,)
    ).fetchone()
    return None if r is None else float(r["last_ingested_mtime"])


def mark_vault_file_ingested(
    conn: sqlite3.Connection,
    path: str,
    mtime: float,
    *,
    cards_imported: int = 0,
) -> None:
    """Record that `path` was ingested at file-mtime `mtime`.

    `cards_imported` ACCUMULATES across runs — a note revisited after an edit
    adds to its total rather than resetting it, so the number reflects how many
    real Anki notes this vault file has produced overall.
    """
    with conn:
        conn.execute(
            "INSERT INTO vault_files "
            "(path, last_ingested_mtime, last_ingested_at, cards_imported) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET "
            "  last_ingested_mtime = excluded.last_ingested_mtime, "
            "  last_ingested_at    = excluded.last_ingested_at, "
            "  cards_imported      = vault_files.cards_imported "
            "                        + excluded.cards_imported",
            (path, float(mtime), _now(), int(cards_imported)),
        )


def list_vault_files(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM vault_files ORDER BY path"
    ).fetchall()
    return [dict(r) for r in rows]


def forget_vault_file(conn: sqlite3.Connection, path: str) -> bool:
    """Drop a file's ingestion record so the next scan treats it as new.
    Returns True if a row was removed."""
    with conn:
        cur = conn.execute("DELETE FROM vault_files WHERE path = ?", (path,))
    return cur.rowcount > 0


# ---------- delete ----------

def delete_word(conn: sqlite3.Connection, word_id: int) -> bool:
    """Return True if a row was deleted."""
    with conn:
        cur = conn.execute("DELETE FROM words WHERE id = ?", (word_id,))
    return cur.rowcount > 0

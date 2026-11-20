"""Vault candidate review + import (step 6), entirely offline.

This is the test suite for everything the review dialog *decides*, which is
deliberately none of it: `vault_dialog.py` only edits a `ReviewSession` and
calls `import_reviews`, so accept / reject / edit state and "what actually gets
written" are all testable here without driving Qt.

  - **Destination** (`decks_for_category`, `resolve_destination_deck`) — the
    reverse `deck_category_map` lookup, its shortest-first ordering, the
    carve-out that stops a stale mapping from importing source material into
    Engram's own generated decks, and the `<Category>::Original` fallback.
  - **Review state** (`CandidateReview`) — accepted-by-default, `edited` after
    a change, and `importable` refusing a card whose side was edited empty.
  - **Session** (`ReviewSession`) — `plan()` as the single source of truth for
    what an import writes, `counts()` (what the dialog's counter shows),
    and the duplicate carve-out in `accept_all`.
  - **Duplicates** — `mark_duplicates` flags AND rejects; `find_duplicate_notes`
    returns a usable map and degrades to `{}` rather than raising when the
    collection can't answer.
  - **Import** (`import_reviews`) — only planned rows written, correct deck per
    category, the `engram-vault` tag (NOT `AI_CARD_TAG`, which would make
    auto-generation skip these), one merged undo entry, and a single failing
    note counted rather than aborting the batch.
  - **Ledger** (`record_ingested`) — every reviewed note marked at the scan's
    mtime, with its imported-card count, against a real sidecar DB.

What is NOT covered here, and needs manual testing in Anki: the Qt dialog
itself — checkbox/cell wiring, the destination combo boxes, and the
`QueryOp` threading in `menu._import_from_vault_action`.

Standalone: `python3 tests/test_vault_import.py` (no Anki / Qt / network).
"""

import os
import shutil
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ai_srs import deck_ops, db, vault_import, vault_ingest  # noqa: E402

_passed = 0
_failed = 0


def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"PASS  {name}")
    else:
        _failed += 1
        print(f"FAIL  {name}: {detail}")


# --------------------- fakes ---------------------

class _FakeNote:
    def __init__(self, nid, nfields=2):
        self.id = nid
        self.fields = [""] * nfields
        self.tags = []

    def add_tag(self, tag):
        self.tags.append(tag)


class _FakeDecks:
    def __init__(self):
        self.created = []

    def id(self, name, create=True):
        if name not in self.created:
            self.created.append(name)
        return 1000 + self.created.index(name)


class _FakeModels:
    def __init__(self, nfields=2):
        self.nfields = nfields

    def by_name(self, name):
        return {"id": 1, "name": name} if name == "Basic" else None

    def all(self):
        return [{"id": 1, "name": "Basic"}]


class _FakeCol:
    """Just enough collection for `create_vault_note` + the undo API."""

    def __init__(self, *, fail_on=(), nfields=2, undo=True):
        self.decks = _FakeDecks()
        self.models = _FakeModels(nfields)
        self.added = []            # (deck_id, front, back, tags)
        self.undo_opened = []
        self.undo_merged = []
        self._next_id = 1
        self._fail_on = set(fail_on)
        self._undo = undo
        self._nfields = nfields

    def new_note(self, notetype):
        note = _FakeNote(self._next_id, self._nfields)
        self._next_id += 1
        return note

    def add_note(self, note, deck_id):
        if note.fields[0] in self._fail_on:
            raise RuntimeError("simulated add_note failure")
        self.added.append(
            (deck_id, note.fields[0], note.fields[1] if len(note.fields) > 1 else "",
             list(note.tags))
        )

    def add_custom_undo_entry(self, label):
        if not self._undo:
            raise RuntimeError("no undo API")
        self.undo_opened.append(label)
        return len(self.undo_opened)

    def merge_undo_entries(self, target):
        if not self._undo:
            raise RuntimeError("no undo API")
        self.undo_merged.append(target)

    # used only by find_duplicate_notes
    def build_search_string(self, node):
        return f"search:{node}"

    def find_notes(self, search):
        return []

    def decks_all(self):
        return self.decks.created


def _cand(front, back="B", path="Weekly/Physics/w12.md", category="Physics"):
    return vault_ingest.Candidate(
        front=front, back=back, source_path=path, category=category
    )


def _session(*candidates, cfg=None):
    return vault_import.ReviewSession.from_candidates(list(candidates), cfg or {})


def _fronts_added(col):
    return [front for _deck, front, _back, _tags in col.added]


def _decks_added(col):
    """Deck NAMES (not ids) each note landed in, in write order."""
    return [col.decks.created[deck_id - 1000] for deck_id, *_ in col.added]


# --------------------- destination deck ---------------------

def test_decks_for_category_reverse_lookup():
    cfg = {"deck_category_map": {"Physics::Core": "Physics", "History": "History"}}
    out = vault_import.decks_for_category(cfg, "Physics")
    check("reverse lookup finds the assigned deck", out == ["Physics::Core"], out)
    check(
        "unassigned category finds nothing",
        vault_import.decks_for_category(cfg, "Biology") == [],
    )
    check("blank category finds nothing", vault_import.decks_for_category(cfg, "") == [])


def test_decks_for_category_orders_least_specific_first():
    cfg = {
        "deck_category_map": {
            "Physics::Core::Ch1": "Physics",
            "Physics::Core": "Physics",
            "Physics::Extra": "Physics",
        }
    }
    out = vault_import.decks_for_category(cfg, "Physics")
    check(
        "shortest name first, then alphabetical",
        out == ["Physics::Core", "Physics::Extra", "Physics::Core::Ch1"],
        out,
    )
    check(
        "resolve picks the least specific",
        vault_import.resolve_destination_deck(cfg, "Physics") == "Physics::Core",
    )


def test_generated_decks_are_never_a_destination():
    """A stale mapping pointing at Learning/Recycled/Legacy must not make vault
    source material land among generated practice cards."""
    for suffix in ("Learning", "Recycled", "Legacy"):
        cfg = {"deck_category_map": {f"Physics::{suffix}": "Physics"}}
        out = vault_import.decks_for_category(cfg, "Physics")
        check(f"{suffix} deck excluded as a destination", out == [], out)
        check(
            f"{suffix} mapping falls back to ::Original",
            vault_import.resolve_destination_deck(cfg, "Physics")
            == "Physics::Original",
        )


def test_resolve_destination_falls_back_to_original():
    check(
        "no assignment → <Category>::Original",
        vault_import.resolve_destination_deck({}, "Physics") == "Physics::Original",
    )
    check(
        "no category → no deck",
        vault_import.resolve_destination_deck({}, None) == "",
    )
    check(
        "matches the spec's Original suffix",
        vault_import.ORIGINAL_SUFFIX == deck_ops.LEARNING_SUFFIX.replace(
            "Learning", "Original"
        ),
    )


def test_malformed_deck_map_degrades():
    for bad in ({"deck_category_map": None}, {"deck_category_map": []},
                {"deck_category_map": "Physics"}):
        out = vault_import.decks_for_category(bad, "Physics")
        check(f"malformed map {bad['deck_category_map']!r} → []", out == [], out)
    mixed = {"deck_category_map": {"Physics::Core": "Physics", 7: "Physics",
                                   "Other": None, "  ": "Physics"}}
    out = vault_import.decks_for_category(mixed, "Physics")
    check("non-string entries skipped, good one kept", out == ["Physics::Core"], out)


def test_category_whitespace_is_tolerated():
    cfg = {"deck_category_map": {"Physics::Core": " Physics "}}
    out = vault_import.decks_for_category(cfg, "Physics")
    check("surrounding whitespace doesn't break the match", out == ["Physics::Core"], out)


# --------------------- CandidateReview state ---------------------

def test_candidate_is_accepted_by_default():
    r = vault_import.CandidateReview.from_candidate(_cand("Q?"))
    check("accepted by default", r.accepted is True)
    check("not a duplicate by default", r.is_duplicate is False)
    check("not edited by default", r.edited is False)
    check("importable by default", r.importable is True)
    check("carries the source path", r.source_path == "Weekly/Physics/w12.md")
    check("carries the category", r.category == "Physics")


def test_editing_a_side_is_detected():
    r = vault_import.CandidateReview.from_candidate(_cand("Q?", "A"))
    r.front = "Better Q?"
    check("front edit detected", r.edited is True)
    check("original front retained", r.original_front == "Q?", r.original_front)
    r2 = vault_import.CandidateReview.from_candidate(_cand("Q?", "A"))
    r2.back = "Better A"
    check("back edit detected", r2.edited is True)
    r3 = vault_import.CandidateReview.from_candidate(_cand("Q?", "A"))
    r3.front = "  Q?  "
    check("whitespace-only change is not an edit", r3.edited is False)


def test_rejected_or_emptied_cards_are_not_importable():
    r = vault_import.CandidateReview.from_candidate(_cand("Q?"))
    r.accepted = False
    check("rejected → not importable", r.importable is False)

    for side in ("front", "back"):
        r = vault_import.CandidateReview.from_candidate(_cand("Q?", "A"))
        setattr(r, side, "   ")
        check(
            f"accepted but {side} edited empty → not importable",
            r.importable is False,
            f"{side}={getattr(r, side)!r}",
        )


# --------------------- ReviewSession ---------------------

def test_session_defaults_destinations_per_category():
    cfg = {"deck_category_map": {"Physics::Core": "Physics"}}
    s = _session(
        _cand("A?", category="Physics"),
        _cand("B?", category="CS"),
        cfg=cfg,
    )
    check("categories sorted", s.categories == ["CS", "Physics"], s.categories)
    check("assigned deck used", s.destinations["Physics"] == "Physics::Core")
    check("unassigned falls back", s.destinations["CS"] == "CS::Original")
    check(
        "destination_for resolves per review",
        s.destination_for(s.reviews[0]) == "Physics::Core",
    )


def test_uncategorised_candidate_has_no_destination():
    s = _session(_cand("A?", category=None))
    check("no category → not in categories", s.categories == [], s.categories)
    check("no destination", s.destination_for(s.reviews[0]) == "")
    check("dropped from the plan", s.plan() == [], s.plan())
    check(
        "counted as accepted-but-homeless",
        s.counts()["no_destination"] == 1,
        s.counts(),
    )


def test_plan_is_what_gets_imported():
    s = _session(_cand("A?"), _cand("B?"), _cand("C?"))
    s.reviews[1].accepted = False
    s.reviews[2].back = "  "
    planned = [r.front for r, _deck in s.plan()]
    check("only accepted + non-empty survive", planned == ["A?"], planned)
    check("plan carries the deck", s.plan()[0][1] == "Physics::Original")


def test_accept_all_leaves_duplicates_alone():
    s = _session(_cand("A?"), _cand("B?"))
    vault_import.mark_duplicates(s, {"B?": 42})
    s.reject_all()
    changed = s.accept_all()
    check("accept_all re-accepted only the non-duplicate", changed == 1, changed)
    check("duplicate stays rejected", s.reviews[1].accepted is False)
    check("plan excludes the duplicate", [r.front for r, _ in s.plan()] == ["A?"])

    changed = s.accept_all(include_duplicates=True)
    check("explicit include re-accepts it", changed == 1, changed)
    check("duplicate now accepted", s.reviews[1].accepted is True)


def test_reject_all_reports_what_changed():
    s = _session(_cand("A?"), _cand("B?"))
    s.reviews[0].accepted = False
    check("reject_all only counts real changes", s.reject_all() == 1)
    check("everything rejected", all(not r.accepted for r in s.reviews))
    check("second reject_all changes nothing", s.reject_all() == 0)


def test_counts_match_the_dialog_counter():
    s = _session(_cand("A?"), _cand("B?"), _cand("C?"), _cand("D?"))
    vault_import.mark_duplicates(s, {"D?": 7})       # → rejected
    s.reviews[1].accepted = False
    s.reviews[2].front = "C edited?"
    c = s.counts()
    check("total", c["total"] == 4, c)
    check("accepted", c["accepted"] == 2, c)
    check("rejected", c["rejected"] == 2, c)
    check("duplicates", c["duplicates"] == 1, c)
    check("edited", c["edited"] == 1, c)
    check("importable", c["importable"] == 2, c)
    check("no_destination", c["no_destination"] == 0, c)


def test_source_paths_are_deduped():
    s = _session(
        _cand("A?", path="w12.md"),
        _cand("B?", path="w12.md"),
        _cand("C?", path="w11.md"),
    )
    check("deduped + sorted", s.source_paths() == ["w11.md", "w12.md"], s.source_paths())


def test_editing_the_destination_reroutes_the_plan():
    """What the dialog's editable deck box does."""
    s = _session(_cand("A?"))
    s.destinations["Physics"] = "Physics::FromObsidian"
    check(
        "plan follows the overridden destination",
        s.plan()[0][1] == "Physics::FromObsidian",
        s.plan(),
    )
    s.destinations["Physics"] = "   "
    check("a blanked destination drops the card from the plan", s.plan() == [], s.plan())


# --------------------- duplicates ---------------------

def test_mark_duplicates_flags_and_rejects():
    s = _session(_cand("A?"), _cand("B?"))
    flagged = vault_import.mark_duplicates(s, {"B?": 99})
    check("one flagged", flagged == 1, flagged)
    check("records the existing note id", s.reviews[1].duplicate_of == 99)
    check("and rejects it by default", s.reviews[1].accepted is False)
    check("the other is untouched", s.reviews[0].accepted is True)
    check("empty map flags nothing", vault_import.mark_duplicates(s, {}) == 0)
    check("None map flags nothing", vault_import.mark_duplicates(s, None) == 0)


def test_mark_duplicates_matches_on_stripped_front():
    s = _session(_cand("  A?  "))
    check("stripped comparison", vault_import.mark_duplicates(s, {"A?": 5}) == 1)


def test_find_duplicate_notes_builds_a_front_to_nid_map():
    """With a collection that answers, the map is keyed on the stripped front."""
    fake_anki = types.ModuleType("anki")
    fake_collection = types.ModuleType("anki.collection")

    class SearchNode:
        class Dupe:
            def __init__(self, notetype_id, first_field):
                self.notetype_id = notetype_id
                self.first_field = first_field

        def __init__(self, dupe=None):
            self.dupe = dupe

    fake_collection.SearchNode = SearchNode
    fake_anki.collection = fake_collection
    saved = {k: sys.modules.get(k) for k in ("anki", "anki.collection")}
    sys.modules["anki"] = fake_anki
    sys.modules["anki.collection"] = fake_collection

    class _Col(_FakeCol):
        def find_notes(self, search):
            return [321] if search == "dupe:known front" else []

        def build_search_string(self, node):
            return f"dupe:{node.dupe.first_field}"

    try:
        col = _Col()
        out = vault_import.find_duplicate_notes(
            col, ["known front", "unknown front", "  ", "known front"]
        )
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    check("known front mapped to its note id", out == {"known front": 321}, out)


def test_find_duplicate_notes_never_raises():
    """A collection that can't do dupe detection costs the user the warning,
    not the import."""
    class _Broken(_FakeCol):
        def build_search_string(self, node):
            raise RuntimeError("search unavailable")

    out = vault_import.find_duplicate_notes(_Broken(), ["A?"])
    check("degrades to an empty map", out == {}, out)

    class _NoNotetype(_FakeCol):
        def __init__(self):
            super().__init__()
            self.models = types.SimpleNamespace(
                by_name=lambda n: None, all=lambda: []
            )

    out = vault_import.find_duplicate_notes(_NoNotetype(), ["A?"])
    check("no notetype → empty map, no raise", out == {}, out)


# --------------------- import ---------------------

def test_import_writes_only_the_plan():
    s = _session(_cand("A?"), _cand("B?"), _cand("C?"))
    s.reviews[1].accepted = False
    col = _FakeCol()
    report = vault_import.import_reviews(col, s)
    check("only accepted written", _fronts_added(col) == ["A?", "C?"], _fronts_added(col))
    check("report counts them", report.imported == 2, report.imported)
    check("nothing failed", report.failed == 0 and report.ok)
    check(
        "grouped by deck",
        report.by_deck == {"Physics::Original": 2},
        report.by_deck,
    )
    check(
        "grouped by source note",
        report.by_source == {"Weekly/Physics/w12.md": 2},
        report.by_source,
    )


def test_import_uses_the_edited_text():
    s = _session(_cand("A?", "A"))
    s.reviews[0].front = "  Edited front?  "
    s.reviews[0].back = "  Edited back  "
    col = _FakeCol()
    vault_import.import_reviews(col, s)
    check(
        "edited + stripped text is what lands",
        col.added[0][1] == "Edited front?" and col.added[0][2] == "Edited back",
        col.added,
    )


def test_import_routes_each_category_to_its_own_deck():
    cfg = {"deck_category_map": {"Physics::Core": "Physics", "CS::Notes": "CS"}}
    s = _session(
        _cand("P?", category="Physics"),
        _cand("C?", category="CS"),
        cfg=cfg,
    )
    col = _FakeCol()
    vault_import.import_reviews(col, s)
    check(
        "each card in its category's deck",
        _decks_added(col) == ["Physics::Core", "CS::Notes"],
        _decks_added(col),
    )
    check("both decks ensured", set(col.decks.created) == {"Physics::Core", "CS::Notes"})


def test_imported_notes_carry_the_vault_tag_not_the_ai_tag():
    """The whole reason vault notes are source material: AI_CARD_TAG would make
    auto-generation skip them, so these must never carry it."""
    s = _session(_cand("A?"))
    col = _FakeCol()
    vault_import.import_reviews(col, s)
    tags = col.added[0][3]
    check("tagged engram-vault", tags == [vault_import.VAULT_TAG], tags)
    check("VAULT_TAG value", vault_import.VAULT_TAG == "engram-vault")
    check("and NOT the AI card tag", deck_ops.AI_CARD_TAG not in tags, tags)


def test_import_is_one_undo_entry():
    s = _session(_cand("A?"), _cand("B?"))
    col = _FakeCol()
    vault_import.import_reviews(col, s)
    check("one undo entry opened", len(col.undo_opened) == 1, col.undo_opened)
    check("and merged once", len(col.undo_merged) == 1, col.undo_merged)
    check("labelled with the count", "2 card(s)" in col.undo_opened[0], col.undo_opened)


def test_import_works_without_the_undo_api():
    s = _session(_cand("A?"))
    col = _FakeCol(undo=False)
    report = vault_import.import_reviews(col, s)
    check("import still happened", report.imported == 1, report.imported)
    check("card written", _fronts_added(col) == ["A?"], col.added)


def test_one_bad_note_does_not_stop_the_batch():
    s = _session(_cand("A?"), _cand("BAD?"), _cand("C?"))
    col = _FakeCol(fail_on={"BAD?"})
    report = vault_import.import_reviews(col, s)
    check("the good ones landed", _fronts_added(col) == ["A?", "C?"], _fronts_added(col))
    check("imported count excludes the failure", report.imported == 2, report.imported)
    check("failure counted", report.failed == 1, report.failed)
    check("report is not ok", report.ok is False)
    check("error text names the card", "BAD?" in report.errors[0], report.errors)
    check("undo still merged", len(col.undo_merged) == 1, col.undo_merged)


def test_importing_nothing_touches_nothing():
    s = _session(_cand("A?"))
    s.reject_all()
    col = _FakeCol()
    report = vault_import.import_reviews(col, s)
    check("no notes added", col.added == [], col.added)
    check("no deck created", col.decks.created == [], col.decks.created)
    check("no undo entry opened", col.undo_opened == [], col.undo_opened)
    check("empty report", report.imported == 0 and report.failed == 0)


def test_single_field_notetype_still_imports():
    """`create_vault_note` guards `fields[1]`; a one-field notetype must not
    raise, it just loses the back."""
    s = _session(_cand("A?", "B"))
    col = _FakeCol(nfields=1)
    report = vault_import.import_reviews(col, s)
    check("still imported", report.imported == 1, report.imported)
    check("front written", col.added[0][1] == "A?", col.added)


# --------------------- ledger ---------------------

class _Ledger:
    def __init__(self):
        self.addon_dir = tempfile.mkdtemp(prefix="engram-ledger-")
        self.conn = db.connect(self.addon_dir)

    def close(self):
        self.conn.close()
        shutil.rmtree(self.addon_dir, ignore_errors=True)


def _note(rel, mtime=1000.0, category="Physics"):
    return vault_ingest.VaultNote(
        rel_path=rel, abs_path="/tmp/" + rel, mtime=mtime, category=category
    )


def test_record_ingested_marks_every_reviewed_note():
    """Including notes whose candidates were all rejected — otherwise a
    re-scan would re-offer the same rejected cards forever."""
    led = _Ledger()
    try:
        notes = [_note("w12.md", 111.0), _note("w11.md", 222.0)]
        report = vault_import.ImportReport(
            imported=3, by_source={"w12.md": 3}
        )
        marked = vault_import.record_ingested(led.conn, notes, report)
        rows = {r["path"]: dict(r) for r in db.list_vault_files(led.conn)}
        check("both marked", marked == 2, marked)
        check("rejected note recorded too", set(rows) == {"w11.md", "w12.md"}, list(rows))
        check(
            "card count recorded per source",
            rows["w12.md"]["cards_imported"] == 3, rows["w12.md"],
        )
        check(
            "a note that produced nothing records 0",
            rows["w11.md"]["cards_imported"] == 0, rows["w11.md"],
        )
        check(
            "the SCAN's mtime is what's stored",
            rows["w12.md"]["last_ingested_mtime"] == 111.0, rows["w12.md"],
        )
    finally:
        led.close()


def test_a_ledger_failure_does_not_lose_the_import():
    class _BadConn:
        def execute(self, *a, **k):
            raise RuntimeError("db gone")

        def commit(self):
            raise RuntimeError("db gone")

    marked = vault_import.record_ingested(
        _BadConn(), [_note("w12.md")], vault_import.ImportReport(imported=1)
    )
    check("reports nothing marked, raises nothing", marked == 0, marked)


# --------------------- end to end ---------------------

def test_candidates_through_review_to_notes():
    """The whole step-6 seam: extraction output → session → the user's
    accept/edit/reject → real notes in the right decks + a marked ledger."""
    led = _Ledger()
    try:
        cfg = {"deck_category_map": {"Physics::Core": "Physics"}}
        candidates = [
            _cand("What is the first law?", "dU = Q - W"),
            _cand("Email the professor?", "TODO"),         # user rejects
            _cand("What is entropy?", "S"),                # user edits
            _cand("What is the first law?", "dup"),        # already imported
        ]
        session = vault_import.ReviewSession.from_candidates(candidates, cfg)
        vault_import.mark_duplicates(session, {"What is the first law?": 500})

        # mark_duplicates matches on front text, so BOTH first-law copies flag
        # — the pre-existing note AND the second one in this very batch. The
        # user re-accepts one of them the way the checkbox would, and leaves the
        # in-batch repeat rejected.
        session.reviews[0].accepted = True
        session.reviews[1].accepted = False
        session.reviews[2].back = "A measure of disorder"

        counts = session.counts()
        check("counter shows 2 importable", counts["importable"] == 2, counts)
        check("counter shows the edit", counts["edited"] == 1, counts)
        check("counter shows both duplicate flags", counts["duplicates"] == 2, counts)

        col = _FakeCol()
        report = vault_import.import_reviews(col, session)
        check(
            "exactly the accepted cards landed",
            _fronts_added(col) == ["What is the first law?", "What is entropy?"],
            _fronts_added(col),
        )
        check(
            "the in-batch repeat was not written twice",
            _fronts_added(col).count("What is the first law?") == 1,
            _fronts_added(col),
        )
        check(
            "the edited back is what landed",
            col.added[1][2] == "A measure of disorder", col.added[1],
        )
        check("rejected card never written", "Email the professor?" not in _fronts_added(col))
        check("all into the assigned deck", set(_decks_added(col)) == {"Physics::Core"})

        notes = [_note("Weekly/Physics/w12.md", 999.0)]
        vault_import.record_ingested(led.conn, notes, report)
        rows = [dict(r) for r in db.list_vault_files(led.conn)]
        check("ledger updated once", len(rows) == 1, rows)
        check("with the imported count", rows[0]["cards_imported"] == 2, rows[0])
    finally:
        led.close()


def test_cancelling_the_dialog_leaves_the_ledger_untouched():
    """`menu._finish_vault_import` is the only caller of `record_ingested`, and
    it runs only after the dialog returns True. Asserted here as the contract:
    building and even editing a session writes nothing."""
    led = _Ledger()
    try:
        s = _session(_cand("A?"))
        s.reviews[0].front = "edited"
        s.accept_all()
        rows = list(db.list_vault_files(led.conn))
        check("no ledger rows from reviewing alone", rows == [], rows)
    finally:
        led.close()


if __name__ == "__main__":
    for fn in list(globals().values()):
        if callable(fn) and getattr(fn, "__name__", "").startswith("test_"):
            fn()
    print(f"\n{_passed}/{_passed + _failed} passed")
    sys.exit(1 if _failed else 0)

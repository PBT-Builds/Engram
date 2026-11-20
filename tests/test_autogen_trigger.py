"""Tests for Step 9 — on-demand auto-generation trigger (spec §9).

Runs standalone (no Anki, no Ollama, no QApplication):
    python3 tests/test_autogen_trigger.py
or under pytest.
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ai_srs import autogen, db, subjects  # noqa: E402

NEW, LRN, REV, RELRN = 0, 1, 2, 3  # anki CardType values


# ---------------- should_generate: fires only when all true ----------------

def test_fires_only_when_all_conditions_true():
    assert autogen.should_generate(
        deck_assigned=True, graduated=True, already_generated=False
    ) is True


def test_does_not_fire_when_deck_unassigned():
    assert autogen.should_generate(
        deck_assigned=False, graduated=True, already_generated=False
    ) is False


def test_does_not_fire_when_not_graduated():
    # e.g. a Good answer while still in learning steps.
    assert autogen.should_generate(
        deck_assigned=True, graduated=False, already_generated=False
    ) is False


def test_does_not_fire_when_already_generated():
    assert autogen.should_generate(
        deck_assigned=True, graduated=True, already_generated=True
    ) is False


# ---------------- is_graduation: learning->review only ----------------

def test_graduation_new_to_review():
    assert autogen.is_graduation(NEW, REV) is True


def test_graduation_learning_to_review():
    assert autogen.is_graduation(LRN, REV) is True


def test_no_graduation_good_while_learning():
    # answered Good but still in learning steps (stays learning)
    assert autogen.is_graduation(LRN, LRN) is False
    assert autogen.is_graduation(NEW, LRN) is False


def test_no_graduation_mature_card_reviewed():
    # already a review card, reviewed again -> NOT a graduation
    assert autogen.is_graduation(REV, REV) is False


def test_no_graduation_relearn_back_to_review():
    # a lapsed card re-graduating: is_graduation is True by type, but the
    # idempotency ledger (already-generated) prevents a re-fire — that gate is
    # tested separately. Here we just document the type transition.
    assert autogen.is_graduation(RELRN, REV) is True


def test_no_graduation_when_pre_state_unknown():
    # If we never captured the pre-answer type, stay safe and don't fire.
    assert autogen.is_graduation(None, REV) is False


# ---------------- category_for_deck: assignment resolution ----------------

def test_category_for_deck_exact_and_prefix():
    m = {"Japanese::Core": "Japanese", "History": "History"}
    assert subjects.category_for_deck(m, "Japanese::Core") == "Japanese"
    assert subjects.category_for_deck(m, "History::WW2::Battles") == "History"


def test_category_for_deck_unassigned_is_none():
    m = {"Japanese::Core": "Japanese"}
    assert subjects.category_for_deck(m, "Chemistry::Organic") is None
    assert subjects.category_for_deck({}, "Anything") is None


def test_category_for_deck_longest_prefix_wins():
    m = {"Lang": "Broad", "Lang::Japanese": "Japanese"}
    assert subjects.category_for_deck(m, "Lang::Japanese::Core") == "Japanese"


# ---------------- idempotency ledger: fires exactly once ----------------

def test_generated_sources_ledger_is_idempotent():
    conn = db.connect(tempfile.mkdtemp())
    src = 123456789
    assert db.has_generated_for_source(conn, src) is False
    # first claim succeeds...
    assert db.mark_generated_for_source(conn, src, source_note_id=42, category="Japanese") is True
    assert db.has_generated_for_source(conn, src) is True
    # ...subsequent claims are no-ops (already generated)
    assert db.mark_generated_for_source(conn, src, source_note_id=42, category="Japanese") is False
    # a different source card is independent
    assert db.mark_generated_for_source(conn, 999, category="History") is True


def test_card_reviewed_many_times_generates_once():
    """Simulate the predicate over a card's lifetime: graduate once, then 50
    normal reviews. should_generate is True exactly once."""
    conn = db.connect(tempfile.mkdtemp())
    src = 555

    def tick(pre_type, post_type):
        graduated = autogen.is_graduation(pre_type, post_type)
        already = db.has_generated_for_source(conn, src)
        fire = autogen.should_generate(
            deck_assigned=True, graduated=graduated, already_generated=already
        )
        if fire:
            db.mark_generated_for_source(conn, src, category="Japanese")
        return fire

    fires = []
    fires.append(tick(NEW, LRN))   # first answer, still learning -> no
    fires.append(tick(LRN, REV))   # graduates -> YES (once)
    for _ in range(50):
        fires.append(tick(REV, REV))  # 50 normal reviews -> no
    fires.append(tick(RELRN, REV))    # lapse + re-graduate -> already done -> no
    assert sum(fires) == 1, fires
    assert fires[1] is True, fires


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)

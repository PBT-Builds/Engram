"""Regression tests for the reset-on-pass invariant (spec §4/§5).

Demotion is on CONSECUTIVE failures, so ANY pass must reset fail_streak to 0 —
including the pass that triggers a recycle cycle, not only promotion/demotion.

Runs standalone (no Anki, no Ollama, no QApplication):
    python3 tests/test_streak_reset.py
or under pytest:
    pytest tests/test_streak_reset.py
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# Importing the package must NOT require a QApplication (importability fix).
from ai_srs import db, deck_ops, strategies  # noqa: E402
from ai_srs import state_machine as sm  # noqa: E402

CFG = {"learned_retrievability": 0.90, "recycle_graduation": 3, "demotion_fails": 3}


# ---- stubs for the Anki/Ollama side effects (deck_ops + generation) ----

class _Stubs:
    """Replaces the card-creating / retiring / generating side effects so the
    transition logic can be exercised with only the sidecar DB."""

    def __init__(self):
        self.next_card = 9000
        self.created = []
        self.retired = []
        self.legacy = []

    def install(self):
        self._orig = (
            deck_ops.create_ai_card,
            deck_ops.retire_card,
            deck_ops.copy_original_to_legacy,
            sm._card_content,
        )
        deck_ops.create_ai_card = self._create
        deck_ops.retire_card = lambda col, cid: self.retired.append(int(cid))
        deck_ops.copy_original_to_legacy = self._legacy
        # Stub the generation chokepoint to always return valid card content.
        sm._card_content = lambda col, cfg, word, subject, src: (
            strategies.CardContent(front=f"S({word})", back="b"), False
        )

    def restore(self):
        (deck_ops.create_ai_card, deck_ops.retire_card,
         deck_ops.copy_original_to_legacy, sm._card_content) = self._orig

    def _create(self, col, subject, suffix, word, front, back):
        self.next_card += 1
        self.created.append((suffix, word, self.next_card))
        return self.next_card

    def _legacy(self, col, source_note_id, subject, exclude_tags=()):
        self.next_card += 1
        self.legacy.append((int(source_note_id), self.next_card))
        return self.next_card


class _FakeStats:
    def __init__(self, r):
        self.fsrs_retrievability = r


class _FakeCol:
    """Minimal stand-in for anki Collection: only card_stats_data is read."""
    def __init__(self, retrievability=1.0):
        self._r = retrievability

    def card_stats_data(self, card_id):
        return _FakeStats(self._r)


class _FakeCard:
    def __init__(self, card_id):
        self.id = card_id


def _new_conn():
    return db.connect(tempfile.mkdtemp())


def _seed(conn, *, stage, rc, fs, card, src=None, word="w", subject="J"):
    wid = db.upsert_word(conn, word, subject, source_note_id=src)
    db.set_stage(conn, wid, stage)
    db.set_recycle_count(conn, wid, rc)
    db.set_fail_streak(conn, wid, fs)
    db.set_current_card_id(conn, wid, card)
    return db.get_word_by_id(conn, wid)


# ----------------------------- tests -----------------------------

def test_force_pass_cycle_resets_fail_streak():
    """The reported bug: recycled | rc=1 | fs=2  --Force PASS-->  rc=2 | fs=0."""
    s = _Stubs(); s.install()
    try:
        conn = _new_conn()
        row = _seed(conn, stage="recycled", rc=1, fs=2, card=500, word="neko")
        result = sm.force_pass(_FakeCol(), conn, row, CFG)
        after = db.get_word_by_id(conn, row["id"])
        assert result == "cycled", result
        assert after["recycle_count"] == 2, after
        assert after["fail_streak"] == 0, f"fail_streak not reset: {after}"
    finally:
        s.restore()


def test_on_card_answered_cycle_resets_fail_streak():
    """Same as above but via the REAL reviewer-hook entry point (non-lapse)."""
    s = _Stubs(); s.install()
    try:
        conn = _new_conn()
        _seed(conn, stage="recycled", rc=1, fs=2, card=500, word="neko")
        result = sm.on_card_answered(_FakeCol(1.0), conn, _FakeCard(500), CFG, ease=3)
        after = db.get_word(conn, "neko", "J")
        assert result == "cycled", result
        assert after["recycle_count"] == 2 and after["fail_streak"] == 0, after
    finally:
        s.restore()


def test_force_pass_and_hook_share_core():
    """force_pass / force_fail and the hook must funnel through ONE core, not
    parallel copies."""
    import inspect
    assert "_apply_pass" in inspect.getsource(sm.force_pass)
    assert "_apply_fail" in inspect.getsource(sm.force_fail)
    assert "_apply_pass" in inspect.getsource(sm.on_card_answered)
    assert "_apply_fail" in inspect.getsource(sm.on_card_answered)


def test_promotion_pass_resets():
    s = _Stubs(); s.install()
    try:
        conn = _new_conn()
        row = _seed(conn, stage="learning", rc=0, fs=2, card=510, word="a")
        assert sm.force_pass(_FakeCol(), conn, row, CFG) == "promoted"
        assert db.get_word_by_id(conn, row["id"])["fail_streak"] == 0
    finally:
        s.restore()


def test_graduation_pass_resets():
    s = _Stubs(); s.install()
    try:
        conn = _new_conn()
        row = _seed(conn, stage="recycled", rc=2, fs=2, card=520, src=77, word="b")
        assert sm.force_pass(_FakeCol(), conn, row, CFG) == "graduated"
        g = db.get_word_by_id(conn, row["id"])
        assert g["stage"] == "legacy" and g["fail_streak"] == 0, g
    finally:
        s.restore()


def test_consecutive_fail_then_pass_resets():
    """Fails accumulate; a pass clears the streak (consecutive, not cumulative)."""
    s = _Stubs(); s.install()
    try:
        conn = _new_conn()
        row = _seed(conn, stage="recycled", rc=0, fs=0, card=530, word="c")
        sm.force_fail(None, conn, db.get_word_by_id(conn, row["id"]), CFG)
        sm.force_fail(None, conn, db.get_word_by_id(conn, row["id"]), CFG)
        assert db.get_word_by_id(conn, row["id"])["fail_streak"] == 2
        sm.force_pass(_FakeCol(), conn, db.get_word_by_id(conn, row["id"]), CFG)
        assert db.get_word_by_id(conn, row["id"])["fail_streak"] == 0
    finally:
        s.restore()


def test_corrective_guard_forces_reset_even_if_helper_regresses():
    """If a future change made cycle_word forget the reset, the corrective
    post-condition guard still guarantees fail_streak == 0 after a pass."""
    s = _Stubs(); s.install()
    orig_cycle = db.cycle_word
    # Sabotage: update recycle_count but NOT fail_streak.
    db.cycle_word = lambda conn, wid, **k: db.set_recycle_count(conn, wid, k["recycle_count"])
    try:
        conn = _new_conn()
        row = _seed(conn, stage="recycled", rc=1, fs=2, card=540, word="d")
        sm.force_pass(_FakeCol(), conn, row, CFG)
        after = db.get_word_by_id(conn, row["id"])
        assert after["recycle_count"] == 2 and after["fail_streak"] == 0, after
    finally:
        db.cycle_word = orig_cycle
        s.restore()


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

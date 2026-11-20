"""Tests for Step 8 — per-subject card-type strategies (spec §8).

Runs standalone (no Anki, no Ollama, no QApplication):
    python3 tests/test_card_strategies.py
or under pytest.
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ai_srs import db, deck_ops, strategies, subjects  # noqa: E402
from ai_srs import state_machine as sm  # noqa: E402

LANG = strategies.CARD_TYPE_LANGUAGE
KNOW = strategies.CARD_TYPE_KNOWLEDGE


# ---- a stub for generator.generate_raw, scripted per attempt ----

class _RawSpy:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.prompts = []

    def __call__(self, model, prompt, endpoint, timeout=60, temperature=0.3, **kwargs):
        self.prompts.append(prompt)
        return self.outputs[min(len(self.prompts) - 1, len(self.outputs) - 1)]


def _patch_raw(spy):
    orig = strategies.generate_raw
    strategies.generate_raw = spy
    return orig


def _gen(card_type, ctx, spy, **kw):
    orig = _patch_raw(spy)
    try:
        return strategies.generate_card(
            card_type, ctx, model="m", endpoint="x", **kw
        )
    finally:
        strategies.generate_raw = orig


# ----------------------- Strategy A: LANGUAGE -----------------------

def test_language_produces_sentence_translation_gloss():
    ctx = strategies.GenContext(word="猫", subject="Japanese")
    raw = '{"sentence": "私は猫が好きです。", "translation": "I like cats.", "gloss": "cat"}'
    content = _gen(LANG, ctx, _RawSpy([raw]))
    assert content is not None
    assert "猫" in content.front and "私は猫が好きです" in content.front, content.front
    assert "I like cats." in content.back and "cat" in content.back, content.back


def test_language_rejects_sentence_missing_word_then_falls_back():
    ctx = strategies.GenContext(
        word="猫", subject="Japanese", source_front="猫 (original)", source_back="cat"
    )
    # Every attempt returns a sentence WITHOUT the target word -> None.
    raw = '{"sentence": "I like dogs.", "translation": "x", "gloss": "y"}'
    content = _gen(LANG, ctx, _RawSpy([raw]), retries=2)
    assert content is None
    fb = strategies.fallback_card(LANG, ctx)
    assert fb.front == "猫 (original)" and fb.back == "cat", fb


def test_language_prompt_pins_sentence_language():
    # Phase 1: qwen2.5:7b wrote Chinese (我家有一只可爱的猫。) for Japanese 猫 on
    # 5/5 runs — a kanji word shared with Chinese pulled the model into
    # Chinese. Naming the language for the SENTENCE itself fixed it (18/18).
    s = strategies.LanguageStrategy()
    ctx = strategies.GenContext(word="猫", subject="Japanese")
    for emphasize in (False, True):
        p = s.build_prompt(ctx, emphasize=emphasize)
        assert "written entirely in Japanese" in p, p
        assert "not in any other language" in p, p


def test_language_rejects_missing_fields():
    ctx = strategies.GenContext(word="Hund", subject="German")
    raw = '{"sentence": "Der Hund bellt.", "translation": "", "gloss": "dog"}'  # empty translation
    assert _gen(LANG, ctx, _RawSpy([raw])) is None


# ----------------------- Strategy B: KNOWLEDGE -----------------------

def test_knowledge_produces_non_identical_qa():
    ctx = strategies.GenContext(
        word="WW2 end", subject="History",
        source_front="What year did WW2 end?", source_back="1945",
    )
    raw = '{"question": "World War II ended in which year?", "answer": "1945"}'
    content = _gen(KNOW, ctx, _RawSpy([raw]))
    assert content is not None
    assert content.front == "World War II ended in which year?", content.front
    assert content.back == "1945", content.back
    assert content.front != ctx.source_front  # not a verbatim copy


def test_knowledge_rejects_verbatim_copy_then_falls_back():
    ctx = strategies.GenContext(
        word="WW2 end", subject="History",
        source_front="What year did WW2 end?", source_back="1945",
    )
    # Identical (modulo whitespace/case) question must be rejected every attempt.
    raw = '{"question": "what year did WW2 end?", "answer": "1945"}'
    content = _gen(KNOW, ctx, _RawSpy([raw]), retries=1)
    assert content is None
    fb = strategies.fallback_card(KNOW, ctx)
    assert fb.front == "What year did WW2 end?" and fb.back == "1945", fb


def test_knowledge_rejects_empty():
    ctx = strategies.GenContext(word="x", subject="History",
                                source_front="Q?", source_back="A")
    assert _gen(KNOW, ctx, _RawSpy(['{"question": "", "answer": "A"}'])) is None


# ----------------------- dispatcher routing -----------------------

def test_dispatcher_routes_language_vs_knowledge_by_card_type():
    lang_ctx = strategies.GenContext(word="猫", subject="Japanese")
    know_ctx = strategies.GenContext(word="k", subject="History",
                                     source_front="Q?", source_back="A")
    lang_spy = _RawSpy(['{"sentence":"猫だ。","translation":"It is a cat.","gloss":"cat"}'])
    know_spy = _RawSpy(['{"question":"Rephrased Q?","answer":"A"}'])
    _gen(LANG, lang_ctx, lang_spy)
    _gen(KNOW, know_ctx, know_spy)
    assert "vocabulary flashcard" in lang_spy.prompts[0]
    assert "same underlying concept" in know_spy.prompts[0].lower()


def test_unknown_card_type_defaults_to_language():
    ctx = strategies.GenContext(word="猫", subject="Japanese")
    spy = _RawSpy(['{"sentence":"猫だ。","translation":"It is a cat.","gloss":"cat"}'])
    content = _gen("nonsense", ctx, spy)
    assert content is not None
    assert "vocabulary flashcard" in spy.prompts[0]  # language prompt used


def test_card_type_for_subject_config():
    cfg = {
        "card_types_by_subject": {"Japanese": "language", "History": "knowledge"},
        "default_card_type": "language",
    }
    assert subjects.card_type_for_subject(cfg, "Japanese") == "language"
    assert subjects.card_type_for_subject(cfg, "History") == "knowledge"
    assert subjects.card_type_for_subject(cfg, "Unlisted") == "language"  # default
    # invalid value -> default
    bad = {"card_types_by_subject": {"X": "bogus"}, "default_card_type": "knowledge"}
    assert subjects.card_type_for_subject(bad, "X") == "knowledge"


# ------------- integration: _card_content routes + falls back -------------

class _FakeNote:
    def __init__(self, fields):
        self._fields = fields

    def values(self):
        return list(self._fields)


class _FakeCol:
    def __init__(self, note=None):
        self._note = note

    def get_note(self, nid):
        if self._note is None:
            raise KeyError(nid)
        return self._note


def test_card_content_knowledge_falls_back_to_original_qa():
    """_card_content with a knowledge subject, generation failing, must fall
    back to the ORIGINAL question/answer (read from the source note)."""
    cfg = {
        "card_types_by_subject": {"History": "knowledge"},
        "default_card_type": "language",
        "models_by_subject": {"default": "m"},
        "validation_retries": 1,
    }
    col = _FakeCol(_FakeNote(["What year did WW2 end?", "1945"]))
    orig = strategies.generate_card
    strategies.generate_card = lambda *a, **k: None  # force failure
    try:
        content, is_fallback = sm._card_content(col, cfg, "WW2", "History", source_note_id=7)
        assert is_fallback is True
        assert content.front == "What year did WW2 end?", content.front
        assert content.back == "1945", content.back
    finally:
        strategies.generate_card = orig


def test_card_content_transition_still_proceeds_with_fallback():
    """A recycle cycle whose knowledge generation fails must still transition,
    creating a card with fallback (original Q/A) content."""
    created = []
    cfg = {
        "card_types_by_subject": {"History": "knowledge"},
        "models_by_subject": {"default": "m"},
        "recycle_graduation": 3, "validation_retries": 1,
    }
    orig_gen = strategies.generate_card
    orig_create = deck_ops.create_ai_card
    orig_retire = deck_ops.retire_card
    strategies.generate_card = lambda *a, **k: None
    deck_ops.create_ai_card = lambda col, subj, suf, word, front, back: (
        created.append((suf, front, back)) or 7777
    )
    deck_ops.retire_card = lambda col, cid: None
    try:
        conn = db.connect(tempfile.mkdtemp())
        wid = db.upsert_word(conn, "WW2", "History", source_note_id=7)
        db.set_stage(conn, wid, "recycled")
        db.set_recycle_count(conn, wid, 0)
        db.set_current_card_id(conn, wid, 500)
        row = db.get_word_by_id(conn, wid)
        col = _FakeCol(_FakeNote(["What year did WW2 end?", "1945"]))
        result = sm.force_pass(col, conn, row, cfg)
        after = db.get_word_by_id(conn, wid)
        assert result == "cycled", result
        assert after["recycle_count"] == 1, after
        assert created and created[-1][0] == "Recycled", created
        assert created[-1][1] == "What year did WW2 end?", created  # fallback front
        assert created[-1][2] == "1945", created                    # fallback back
    finally:
        strategies.generate_card = orig_gen
        deck_ops.create_ai_card = orig_create
        deck_ops.retire_card = orig_retire


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

"""Tests for Step 6 — validation + retry + fallback (spec §5).

Runs standalone (no Anki, no Ollama, no QApplication):
    python3 tests/test_validation_fallback.py
or under pytest.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ai_srs import generator  # noqa: E402
from ai_srs import validation  # noqa: E402


# ----------------------- (a)/(b) validation: latin -----------------------

def test_latin_contains_pass():
    # "lerne" is a conjugation of the lemma "lernen" -> stem match.
    assert validation.contains_word("Ich lerne jeden Tag Deutsch.", "lernen")


def test_latin_missing_fails():
    assert not validation.contains_word("Ich spreche Englisch.", "lernen")


def test_latin_word_boundary_short_word_not_inside_larger():
    # short word must match as a whole word, not inside "category".
    assert not validation.contains_word("category theory", "cat")
    assert validation.contains_word("the cat sleeps", "cat")


def test_latin_accent_aware_nfkc():
    # decomposed "Tür" (T u + combining diaeresis) must match precomposed "Tür".
    assert validation.contains_word("Die Tür ist offen.", "Tür")


# ----------------------- (c) validation: japanese -----------------------

def test_japanese_exact():
    assert validation.contains_word("猫が好きです。", "猫")


def test_japanese_conjugated_form():
    # target lemma 食べる; sentence has conjugated 食べます -> stem 食べ matches.
    assert validation.contains_word("りんごを食べます。", "食べる")


def test_japanese_missing_fails():
    assert not validation.contains_word("犬が好きです。", "猫")


def test_empty_word_false():
    assert not validation.contains_word("anything", "")


def test_other_script_substring():
    assert validation.contains_word("Я люблю кошки", "кошки")
    assert not validation.contains_word("Я люблю собаки", "кошки")


# --------------- generate_validated: retry + emphasize ---------------

class _GenSpy:
    """Stub generator.generate_sentence; records calls + emphasize flags."""
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []  # list of emphasize flags

    def __call__(self, req, endpoint, timeout=60, emphasize=False, **kwargs):
        self.calls.append(emphasize)
        return self.outputs[len(self.calls) - 1]


def _patch_gen(spy):
    orig = generator.generate_sentence
    generator.generate_sentence = spy
    return orig


def test_retries_exhaust_returns_none():
    spy = _GenSpy(["no target here", "still nothing", "nope"])
    orig = _patch_gen(spy)
    try:
        req = generator.GenRequest(word="lernen", model="m")
        out = generator.generate_validated(req, endpoint="x", retries=2)
        assert out is None, out
        assert len(spy.calls) == 3, spy.calls          # retries + 1 attempts
        assert spy.calls == [False, True, True], spy.calls  # emphasize on retries
    finally:
        generator.generate_sentence = orig


def test_retry_succeeds_on_second_attempt():
    spy = _GenSpy(["missing", "Ich lerne Deutsch."])
    orig = _patch_gen(spy)
    try:
        req = generator.GenRequest(word="lernen", model="m")
        out = generator.generate_validated(req, endpoint="x", retries=2)
        assert out == "Ich lerne Deutsch.", out
        assert len(spy.calls) == 2, spy.calls
    finally:
        generator.generate_sentence = orig


# NOTE: the card-content fallback path (generation fails -> original/placeholder
# content, transition still proceeds) moved to the per-strategy layer in Step 8
# and is covered by tests/test_card_strategies.py.


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

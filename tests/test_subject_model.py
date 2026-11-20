"""Tests for Step 7 — subject layer + per-subject model selection (spec §7).

Runs standalone (no Anki, no Ollama, no QApplication):
    python3 tests/test_subject_model.py
or under pytest.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ai_srs import state_machine as sm  # noqa: E402
from ai_srs import strategies  # noqa: E402
from ai_srs import subjects  # noqa: E402

CFG = {
    "models_by_subject": {
        "Japanese": "qwen2.5:7b",
        "German": "llama3.1:8b",
        "default": "llama3.1:8b",
    },
    "subjects": ["Japanese", "German"],
    "validation_retries": 2,
}


# ----------------------- model_for_subject -----------------------

def test_known_subject_selects_its_model():
    assert subjects.model_for_subject(CFG, "Japanese") == "qwen2.5:7b"
    assert subjects.model_for_subject(CFG, "German") == "llama3.1:8b"


def test_unknown_subject_falls_back_to_default():
    assert subjects.model_for_subject(CFG, "Klingon") == "llama3.1:8b"


def test_none_subject_falls_back_to_default():
    assert subjects.model_for_subject(CFG, None) == "llama3.1:8b"


def test_default_of_last_resort_when_no_default_key():
    assert subjects.model_for_subject({"models_by_subject": {}}, "X") == subjects.DEFAULT_MODEL


# ----------------------- subject_of_deck -----------------------

def test_subject_of_deck_matches_parent_and_nested():
    subs = subjects.subjects_from_config(CFG)
    assert subjects.subject_of_deck("Japanese", subs) == "Japanese"
    assert subjects.subject_of_deck("Japanese::Vocab::Core", subs) == "Japanese"


def test_subject_of_deck_none_for_unconfigured():
    subs = subjects.subjects_from_config(CFG)
    assert subjects.subject_of_deck("Chemistry::Organic", subs) is None


def test_subject_of_deck_longest_match_wins():
    subs = ["Lang", "Lang::Japanese"]
    assert subjects.subject_of_deck("Lang::Japanese::Core", subs) == "Lang::Japanese"


def test_subjects_from_config_backcompat_uses_model_keys():
    cfg = {"models_by_subject": {"Japanese": "m", "German": "m", "default": "m"}}
    assert sorted(subjects.subjects_from_config(cfg)) == ["German", "Japanese"]


# ------------- subject threads through to generation -------------
#
# Step 8: model selection is threaded from `_card_content` into
# `strategies.generate_card(model=...)`. These capture that call.

def _capture_generate_card(word, subject):
    """Run sm._card_content with strategies.generate_card stubbed to capture the
    model + subject it receives. Returns the captured dict."""
    captured = {}

    def fake(card_type, ctx, *, model, endpoint, timeout=60, temperature=0.3, retries=2, **kwargs):
        captured.update(model=model, subject=ctx.subject, card_type=card_type)
        return strategies.CardContent(front="f", back="b")

    orig = strategies.generate_card
    strategies.generate_card = fake
    try:
        sm._card_content(None, CFG, word, subject, source_note_id=None)
    finally:
        strategies.generate_card = orig
    return captured


def test_subject_threads_to_generation_request_model():
    cap = _capture_generate_card("ねこ", "Japanese")
    assert cap["model"] == "qwen2.5:7b", cap
    assert cap["subject"] == "Japanese", cap


def test_unknown_subject_threads_default_model_to_request():
    cap = _capture_generate_card("atom", "Chemistry")
    assert cap["model"] == "llama3.1:8b", cap
    assert cap["subject"] == "Chemistry", cap


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

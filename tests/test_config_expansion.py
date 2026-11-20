"""Tests for Task 2 — config-driven subject/model/card_type expansion.

Covers, with NO Anki / Ollama / Qt:
  - a newly-added subject resolves to its configured model + card_type;
  - an unknown subject falls through to the defaults (model + card_type);
  - the deck → category → model + card_type chain resolves end to end;
  - the seeded config.json carries the recommended defaults (languages →
    qwen2.5:7b, knowledge → phi4, default → qwen2.5:7b) and they resolve.

Runs standalone (`python3 tests/test_config_expansion.py`) or under pytest.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ai_srs import subjects  # noqa: E402

_CONFIG_PATH = os.path.join(
    os.path.dirname(__file__), "..", "src", "ai_srs", "config.json"
)


def _load_shipped_config() -> dict:
    with open(_CONFIG_PATH, encoding="utf-8") as fh:
        return json.load(fh)


# A config with a brand-new subject added the way the Setup dialog / a hand-edit
# would add it — no code change, just extra dict keys.
CFG = {
    "models_by_subject": {
        "Japanese": "qwen2.5:7b",
        "History": "phi4",
        "default": "qwen2.5:7b",
    },
    "card_types_by_subject": {
        "Japanese": "language",
        "History": "knowledge",
    },
    "default_card_type": "language",
    "subjects": ["Japanese", "History"],
    "deck_category_map": {
        "JP::Core": "Japanese",
        "World History": "History",
    },
}


# ---------------- newly-added subject resolves to its config ----------------

def test_added_language_subject_resolves_model_and_card_type():
    assert subjects.model_for_subject(CFG, "Japanese") == "qwen2.5:7b"
    assert subjects.card_type_for_subject(CFG, "Japanese") == "language"


def test_added_knowledge_subject_resolves_model_and_card_type():
    # A knowledge subject added purely via config keys — no code change.
    assert subjects.model_for_subject(CFG, "History") == "phi4"
    assert subjects.card_type_for_subject(CFG, "History") == "knowledge"


def test_brand_new_subject_with_only_keys_added_resolves():
    # Simulate the dialog adding "Physics" → phi4 / knowledge to an existing cfg.
    cfg = {
        "models_by_subject": dict(CFG["models_by_subject"], Physics="phi4"),
        "card_types_by_subject": dict(
            CFG["card_types_by_subject"], Physics="knowledge"
        ),
        "default_card_type": "language",
    }
    assert subjects.model_for_subject(cfg, "Physics") == "phi4"
    assert subjects.card_type_for_subject(cfg, "Physics") == "knowledge"


# ---------------- unknown subject falls to the defaults ----------------

def test_unknown_subject_falls_to_default_model_and_card_type():
    assert subjects.model_for_subject(CFG, "Klingon") == "qwen2.5:7b"  # default key
    assert subjects.card_type_for_subject(CFG, "Klingon") == "language"  # default_card_type


def test_none_subject_falls_to_default():
    assert subjects.model_for_subject(CFG, None) == "qwen2.5:7b"
    assert subjects.card_type_for_subject(CFG, None) == "language"


# ---------------- deck → category → model + card_type, end to end ----------------

def _resolve_deck(cfg: dict, deck_name: str):
    """The full chain a graduating source card walks: deck → category (Step 9)
    → model (Step 7) + card_type (Step 8)."""
    category = subjects.category_for_deck(cfg.get("deck_category_map", {}), deck_name)
    if category is None:
        return None
    return (
        category,
        subjects.model_for_subject(cfg, category),
        subjects.card_type_for_subject(cfg, category),
    )


def test_deck_to_model_and_card_type_chain_language():
    assert _resolve_deck(CFG, "JP::Core") == ("Japanese", "qwen2.5:7b", "language")
    # nested under the mapped prefix still resolves
    assert _resolve_deck(CFG, "JP::Core::Verbs") == ("Japanese", "qwen2.5:7b", "language")


def test_deck_to_model_and_card_type_chain_knowledge():
    assert _resolve_deck(CFG, "World History") == ("History", "phi4", "knowledge")


def test_unassigned_deck_resolves_to_none():
    assert _resolve_deck(CFG, "Some::Untracked::Deck") is None


# ---------------- the shipped config.json carries the seeds ----------------

def test_shipped_config_seeds_languages_to_qwen():
    cfg = _load_shipped_config()
    for subj in ("Japanese", "Chinese", "Korean", "German", "Spanish", "French", "English"):
        assert subjects.model_for_subject(cfg, subj) == "qwen2.5:7b", subj
        assert subjects.card_type_for_subject(cfg, subj) == "language", subj


def test_shipped_config_needs_only_one_model():
    """Phase 2: the README quickstart pulls ONE model (qwen2.5:7b). Knowledge
    subjects used to default to phi4, so a stranger assigning a History deck got
    fallback cards from the first card on. Every shipped subject now resolves to
    qwen2.5:7b; phi4 stays a suggested upgrade in the Setup dialog."""
    cfg = _load_shipped_config()
    models = {v if isinstance(v, str) else v.get("model")
              for v in cfg["models_by_subject"].values()}
    assert models == {"qwen2.5:7b"}, models
    for subj in ("History", "Chemistry", "Physics", "Biology"):
        assert subjects.model_for_subject(cfg, subj) == "qwen2.5:7b", subj
        assert subjects.card_type_for_subject(cfg, subj) == "knowledge", subj


def test_shipped_config_default_is_qwen():
    cfg = _load_shipped_config()
    assert cfg["models_by_subject"]["default"] == "qwen2.5:7b"
    assert subjects.model_for_subject(cfg, "AnythingUnlisted") == "qwen2.5:7b"
    assert subjects.card_type_for_subject(cfg, "AnythingUnlisted") == "language"


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

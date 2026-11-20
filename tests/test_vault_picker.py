"""Vault picker: category -> destination-deck pre-fill.

Phase 1 found that typing a category character by character froze the deck at
the first keystroke ("Statistics" -> "S::Original"): the sync only filled a
BLANK deck box, so once it had pre-filled one itself it could never update it.
The rule these tests pin down: the deck follows the category until the user
edits the deck box themselves, and a deck the user typed is never overwritten.

Runs headless (offscreen Qt), no Anki main window:
    python tests/test_vault_picker.py
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from aqt.qt import QApplication, Qt  # noqa: E402

_app = QApplication.instance() or QApplication([])

from ai_srs import vault_picker, vault_select  # noqa: E402


def _deck_for(category: str) -> str:
    return f"{category}::Original" if category else ""


def _dialog(category_map=None):
    paths = ["Japanese/Grammar/Verbs.md", "Physics/Kinematics.md", "Notes.md"]
    tree = vault_select.VaultTree(
        root=vault_select.build_tree_from_paths(paths), files=tuple(paths)
    )
    return vault_picker.VaultPickerDialog(
        None, tree, categories=["History"], deck_names=["History::Original"],
        category_map=category_map or {}, deck_for_category=_deck_for,
    )


def _type(combo, text: str) -> None:
    """Simulate typing: one currentTextChanged per keystroke."""
    for i in range(1, len(text) + 1):
        combo.setCurrentText(text[:i])


def test_deck_follows_category_typed_keystroke_by_keystroke():
    d = _dialog()
    _type(d._category, "Statistics")
    assert d._deck.currentText() == "Statistics::Original", d._deck.currentText()


def test_deck_follows_category_changed_after_prefill():
    d = _dialog()
    d._category.setCurrentText("History")
    d._category.setCurrentText("Physics")
    assert d._deck.currentText() == "Physics::Original", d._deck.currentText()


def test_user_typed_deck_is_never_overwritten():
    d = _dialog()
    d._category.setCurrentText("History")
    _type(d._deck, "My Deck")
    _type(d._category, "Physics")
    assert d._deck.currentText() == "My Deck", d._deck.currentText()


def test_clearing_category_clears_prefilled_deck():
    d = _dialog()
    _type(d._category, "Bio")
    d._category.setCurrentText("")
    assert d._deck.currentText() == "", d._deck.currentText()


def test_user_blanking_deck_resumes_following():
    d = _dialog()
    _type(d._deck, "X")
    d._deck.setCurrentText("")
    _type(d._category, "Chem")
    assert d._deck.currentText() == "Chem::Original", d._deck.currentText()


def test_auto_filled_category_fills_deck():
    d = _dialog(category_map={"Physics": "Physics"})
    for item in d._items:
        if item.data(0, vault_picker._PATH_ROLE) == "Physics":
            item.setCheckState(0, Qt.CheckState.Checked)
    assert d._category.currentText() == "Physics"
    assert d._deck.currentText() == "Physics::Original", d._deck.currentText()


def _check(d, rel_path: str, state=Qt.CheckState.Checked) -> None:
    for item in d._items:
        if item.data(0, vault_picker._PATH_ROLE) == rel_path:
            item.setCheckState(0, state)
            return
    raise AssertionError(f"no tree item {rel_path}")


def test_conflicting_mapped_folders_blank_an_auto_filled_category():
    # Phase 1: a History folder then a Japanese folder kept "History", so all
    # the Japanese notes would have been routed to History::Original.
    d = _dialog(category_map={"Physics": "Physics", "Japanese": "Japanese"})
    _check(d, "Physics")
    assert d._category.currentText() == "Physics"
    _check(d, "Japanese")
    assert d._category.currentText() == "", d._category.currentText()
    assert d._deck.currentText() == "", d._deck.currentText()
    assert not d._ok.isEnabled(), "Continue must be disabled with no category"


def test_unchecking_back_to_one_mapped_folder_refills():
    d = _dialog(category_map={"Physics": "Physics", "Japanese": "Japanese"})
    _check(d, "Physics")
    _check(d, "Japanese")
    _check(d, "Japanese", Qt.CheckState.Unchecked)
    assert d._category.currentText() == "Physics", d._category.currentText()


def test_conflict_never_blanks_a_typed_category():
    d = _dialog(category_map={"Physics": "Physics", "Japanese": "Japanese"})
    _type(d._category, "Mine")
    _check(d, "Physics")
    _check(d, "Japanese")
    assert d._category.currentText() == "Mine", d._category.currentText()


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

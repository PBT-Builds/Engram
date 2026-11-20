"""Phase 2, item 7: two UI fixes whose decisions are testable without Qt.

1. Force PASS/FAIL result dialog: for transitions that regenerate in the
   background (result "… (async)"), the "After:" row was read before the
   background commit landed, so it showed the OLD stage/card. It now says the
   change is still in progress instead of showing stale numbers.
2. Review dialog: a selected row rendered white-on-white in Anki's dark theme.
   The dialog now sets the selection colours explicitly: the palette's
   highlight, with black or white text chosen by contrast.

Standalone: `python tests/test_ui_text.py`.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ai_srs import menu, vault_dialog_style  # noqa: E402

_passed = 0
_failed = 0


def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"PASS  {name}")
    else:
        _failed += 1
        print(f"FAIL  {name}  {detail}")


BEFORE = {"stage": "recycled", "recycle_count": 1, "fail_streak": 2, "current_card_id": 10}
AFTER_STALE = dict(BEFORE)
AFTER_SYNC = {"stage": "recycled", "recycle_count": 1, "fail_streak": 3, "current_card_id": 10}


def test_async_result_has_no_stale_after_row():
    text = menu.force_result_text("Japanese", "犬", False, "demoted (async)", BEFORE, AFTER_STALE)
    check("async: no stale After numbers", "After:  stage=" not in text, text)
    check("async: says it is still running", "background" in text, text)
    check("async: Before row kept", "Before: stage=recycled" in text, text)


def test_sync_result_keeps_after_row():
    text = menu.force_result_text("Japanese", "犬", False, "fail-registered", BEFORE, AFTER_SYNC)
    check("sync: After row shown", "After:  stage=recycled recycle_count=1 fail_streak=3" in text, text)


def test_contrast_text_colour():
    check("dark highlight -> white text", vault_dialog_style.contrast_text((40, 80, 160)) == "#ffffff")
    check("light highlight -> black text", vault_dialog_style.contrast_text((220, 230, 255)) == "#000000")


def test_selection_stylesheet():
    css = vault_dialog_style.selection_stylesheet((220, 230, 255))
    check("stylesheet sets selected background", "item:selected" in css and "#dce6ff" in css, css)
    check("stylesheet sets a contrasting text colour", "color: #000000" in css, css)


if __name__ == "__main__":
    for fn in list(globals().values()):
        if callable(fn) and getattr(fn, "__name__", "").startswith("test_"):
            try:
                fn()
            except Exception as e:
                _failed += 1
                print(f"FAIL  {fn.__name__} raised {type(e).__name__}: {e}")
    print(f"\n{_passed}/{_passed + _failed} passed")
    sys.exit(1 if _failed else 0)

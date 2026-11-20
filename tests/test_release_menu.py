"""Release polish, testable without Qt.

1. The testing items (Generate test sentence, Populate sidecar, Create Learning
   card, Dev / test) are hidden unless `show_dev_menu` is true. They are still
   wired to their handlers when shown.
2. Test connection shows an optional provider with no API key (claude, out of
   the box) as "not configured (optional)" instead of "NOT reachable ✗".

Standalone: `python tests/test_release_menu.py`.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ai_srs import generator, menu  # noqa: E402

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


DEV_LABELS = [
    "Generate test sentence…",
    "Populate sidecar from deck…",
    "Create Learning card for word…",
    "Dev / test",
]
RELEASE_LABELS = [
    "Getting started…",
    "Test connection",
    "Setup categories…",
    "Import from vault…",
    "Choose vault folder…",
]


def test_dev_menu_hidden_by_default():
    labels = menu.menu_labels({})
    check("release items shown", all(l in labels for l in RELEASE_LABELS), labels)
    check("testing items hidden by default", not any(l in labels for l in DEV_LABELS), labels)
    labels = menu.menu_labels({"show_dev_menu": False})
    check("testing items hidden when false", not any(l in labels for l in DEV_LABELS), labels)


def test_dev_menu_shown_when_enabled():
    labels = menu.menu_labels({"show_dev_menu": True})
    check("testing items shown when true", all(l in labels for l in DEV_LABELS), labels)
    check("release items still shown", all(l in labels for l in RELEASE_LABELS), labels)


def test_only_a_real_true_enables_it():
    check("string 'false' does not enable", not menu.dev_menu_enabled({"show_dev_menu": "false"}))
    check("missing key does not enable", not menu.dev_menu_enabled({}))
    check("true enables", menu.dev_menu_enabled({"show_dev_menu": True}))


def test_dev_items_keep_their_handlers():
    spec = menu.menu_spec(True)
    by_label = {e[0]: e for e in spec if e is not None}
    check("Generate test sentence wired", by_label["Generate test sentence…"][1] is menu._generate_action)
    check("Populate sidecar wired", by_label["Populate sidecar from deck…"][1] is menu._populate_action)
    check("Create Learning card wired",
          by_label["Create Learning card for word…"][1] is menu._enter_learning_action)
    sub = by_label["Dev / test"][1]
    check("Dev / test is a submenu with 4 items", isinstance(sub, list) and
          len([e for e in sub if e is not None]) == 4, sub)


def _status(name, fmt, key, reachable):
    prov = generator.ProviderConfig(api_base="http://x", api_format=fmt, api_key=key,
                                    timeout=5, name=name)
    return generator.ProviderStatus(provider=prov, reachable=reachable, models=[], detail="")


def test_keyless_optional_provider_is_not_an_error():
    st = _status("claude", "anthropic", "", False)
    label = menu.connection_status_label(st, default="local")
    check("keyless claude -> not configured (optional)", label == "not configured (optional)", label)
    check("no ✗ for it", "✗" not in label, label)


def test_real_failures_still_say_not_reachable():
    check("local down -> NOT reachable",
          menu.connection_status_label(_status("local", "openai", "", False), "local") == "NOT reachable ✗")
    check("claude with key but failing -> NOT reachable",
          menu.connection_status_label(_status("claude", "anthropic", "k", False), "local")
          == "NOT reachable ✗")
    check("keyless default provider is still an error",
          menu.connection_status_label(_status("claude", "anthropic", "", False), "claude")
          == "NOT reachable ✗")
    check("reachable -> reachable ✓",
          menu.connection_status_label(_status("local", "openai", "", True), "local") == "reachable ✓")


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

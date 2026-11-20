"""Extraction model fallback (Phase 2, item 5).

With the shipped config and `extraction_provider: local`, extraction used to
stop at "Provider 'local' has no model configured", because only
`extraction_model` and the provider's own `model` were consulted. Decision:
fall back to the note category's `models_by_subject` entry, then the
`default` entry — but only to entries that actually route to the extraction
provider (a bare string means the default provider; a dict names its own).
The hardcoded last-resort model is NOT used: an empty config still says
"no model configured" rather than guessing.

Standalone: `python tests/test_extraction_model_fallback.py` (no network).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ai_srs import vault_ingest  # noqa: E402

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


def _cfg(**over):
    cfg = {
        "providers": {
            "local": {"api_format": "openai", "api_base": "http://localhost:11434/v1"},
            "claude": {"api_format": "anthropic", "api_base": "https://api.anthropic.com/v1",
                       "api_key": "sk-test", "model": "claude-haiku-4-5-20251001"},
        },
        "default_provider": "local",
        "extraction_provider": "local",
        "extraction_model": "",
        "models_by_subject": {"Physics": "phi4", "Japanese": "qwen2.5:7b",
                              "default": "qwen2.5:7b",
                              "History": {"provider": "claude", "model": "claude-x"}},
        "vault_path": ".",
    }
    cfg.update(over)
    return cfg


def _model(cfg, category):
    return vault_ingest.check_extraction_available(cfg, probe=False, category=category)


def test_category_model_is_used():
    a = _model(_cfg(), "Physics")
    check("local + Physics -> phi4", a.available and a.model == "phi4", a)


def test_default_entry_when_category_unknown():
    a = _model(_cfg(), "Statistics")
    check("unknown category -> default entry", a.available and a.model == "qwen2.5:7b", a)


def test_no_category_uses_default_entry():
    a = _model(_cfg(), None)
    check("no category -> default entry", a.available and a.model == "qwen2.5:7b", a)


def test_entry_for_another_provider_is_skipped():
    a = _model(_cfg(), "History")
    check("History routes to claude, so local falls to default",
          a.available and a.model == "qwen2.5:7b", a)


def test_explicit_extraction_model_still_wins():
    a = _model(_cfg(extraction_model="llama3:8b"), "Physics")
    check("extraction_model beats the category model", a.model == "llama3:8b", a)


def test_provider_model_still_wins():
    cfg = _cfg()
    cfg["providers"]["local"]["model"] = "mistral"
    a = _model(cfg, "Physics")
    check("provider's own model beats the category model", a.model == "mistral", a)


def test_bare_string_entries_do_not_apply_to_non_default_provider():
    cfg = _cfg(extraction_provider="claude")
    cfg["providers"]["claude"]["model"] = ""
    a = _model(cfg, "Physics")
    check("claude without a model does not borrow local 'phi4'", not a.available, a)
    check("reason still names extraction_model", "extraction_model" in a.reason, a.reason)


def test_dict_entry_for_the_extraction_provider_applies():
    cfg = _cfg(extraction_provider="claude")
    cfg["providers"]["claude"]["model"] = ""
    a = _model(cfg, "History")
    check("claude extraction uses History's claude model", a.available and a.model == "claude-x", a)


def test_empty_models_map_still_reports_no_model():
    a = _model(_cfg(models_by_subject={}), "Physics")
    check("no configured entries -> unavailable (no hardcoded guess)", not a.available, a)


def test_shipped_config_local_extraction_works():
    import json
    path = os.path.join(os.path.dirname(__file__), "..", "src", "ai_srs", "config.json")
    with open(path, encoding="utf-8") as f:
        shipped = json.load(f)
    shipped["extraction_provider"] = "local"
    a = vault_ingest.check_extraction_available(shipped, probe=False, category="Physics")
    check("shipped config + local + Physics -> a model (was: 'no model configured')",
          a.available and a.model == shipped["models_by_subject"]["Physics"], a)


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

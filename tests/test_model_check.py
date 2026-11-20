"""Configured-but-not-installed models (Phase 2, item 4).

A stranger with the shipped config and only some models pulled used to find
out only via silent fallback cards. `model_check` works out which models the
config will actually USE per provider (assigned categories, the default, vault
extraction) and which of those the server doesn't have, so Test connection and
Getting started can say "model X is configured but not installed: run
`ollama pull X`". Unused shipped entries (e.g. phi4 for an unassigned History)
are deliberately not reported — that would be noise.

Standalone: `python tests/test_model_check.py` (pure, no network).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ai_srs import model_check  # noqa: E402

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
                       "api_key": "k", "model": "claude-haiku-4-5-20251001"},
        },
        "default_provider": "local",
        "models_by_subject": {"Japanese": "qwen2.5:7b", "History": "phi4",
                              "Physics": "phi4", "default": "qwen2.5:7b",
                              "Biology": {"provider": "claude", "model": "claude-sonnet-x"}},
        "deck_category_map": {},
        "vault_category_map": {},
        "extraction_provider": "claude",
        "extraction_model": "",
    }
    cfg.update(over)
    return cfg


# ---------- name matching ----------

def test_installed_matching():
    inst = ["qwen2.5:7b", "phi4:latest", "Llama3:8B"]
    check("exact tag", model_check.is_installed("qwen2.5:7b", inst))
    check("bare name matches :latest", model_check.is_installed("phi4", inst))
    check(":latest matches bare name", model_check.is_installed("phi4:latest", ["phi4"]))
    check("case-insensitive", model_check.is_installed("llama3:8b", inst))
    check("different tag is not installed", not model_check.is_installed("qwen2.5:14b", inst))
    check("bare name does not match another tag", not model_check.is_installed("qwen2.5", inst))


# ---------- which models are in use ----------

def test_fresh_install_uses_only_default():
    used = model_check.models_in_use(_cfg())
    check("fresh install: local uses only the default model",
          used.get("local") == {"qwen2.5:7b": ["default"]}, used)


def test_assigned_categories_are_included():
    cfg = _cfg(deck_category_map={"Japanese::Core": "Japanese", "Hist": "History"})
    used = model_check.models_in_use(cfg)
    check("History assigned -> phi4 in use", used["local"].get("phi4") == ["History"], used)
    check("Japanese and default share qwen", sorted(used["local"]["qwen2.5:7b"]) == ["Japanese", "default"],
          used)


def test_vault_categories_are_included():
    used = model_check.models_in_use(_cfg(vault_category_map={"Physics notes": "Physics"}))
    check("vault-mapped Physics -> phi4", "phi4" in used["local"], used)


def test_category_with_provider_dict():
    used = model_check.models_in_use(_cfg(deck_category_map={"Bio": "Biology"}))
    check("Biology routed to claude", used.get("claude", {}).get("claude-sonnet-x") == ["Biology"], used)


def test_extraction_model_is_included():
    used = model_check.models_in_use(_cfg())
    check("claude extraction model listed",
          used["claude"].get("claude-haiku-4-5-20251001") == ["vault extraction"], used)
    used = model_check.models_in_use(_cfg(extraction_provider="local", extraction_model="qwen3:8b"))
    check("local extraction model listed", used["local"].get("qwen3:8b") == ["vault extraction"], used)


# ---------- missing ----------

def test_missing_models_for_provider():
    cfg = _cfg(deck_category_map={"Hist": "History"})
    missing = model_check.missing_models(cfg, "local", ["qwen2.5:7b"])
    check("phi4 missing, used by History", missing == [("phi4", ["History"])], missing)


def test_nothing_missing():
    missing = model_check.missing_models(_cfg(), "local", ["qwen2.5:7b", "gemma"])
    check("all installed -> []", missing == [], missing)


def test_empty_model_list_means_unknown():
    check("an empty server list reports nothing (can't tell)",
          model_check.missing_models(_cfg(), "local", []) == [])


def test_message_for_local_has_pull_command():
    lines = model_check.missing_lines("local", "openai", [("phi4", ["History", "Physics"])])
    text = "\n".join(lines)
    check("names the model and users", "phi4" in text and "History, Physics" in text, text)
    check("gives the pull command", "ollama pull phi4" in text, text)


def test_message_for_cloud_has_no_pull_command():
    lines = model_check.missing_lines("claude", "anthropic", [("claude-x", ["Biology"])])
    text = "\n".join(lines)
    check("cloud: no ollama advice", "ollama" not in text and "claude-x" in text, text)


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

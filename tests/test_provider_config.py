"""Provider / endpoint config resolution + fresh-install (no-config) path.

Covers the packaging/flexibility pass:
  - `generator.resolve_provider` turns config into a ProviderConfig consistently
    (defaults, explicit keys, legacy `ollama_endpoint` back-compat, bad format).
  - `generate_raw` builds the right request + parses the right field for both
    wire formats ("openai" chat-completions and "ollama" native), and never
    lets an odd response shape escape.
  - A fresh install with EMPTY config never crashes: resolution yields sane
    defaults, generation against a dead endpoint returns None (caller falls
    back), and the menu's "nothing configured" guidance predicate is correct.

Standalone: `python3 tests/test_provider_config.py` (no Anki / Qt / network —
the one network test points at a closed port and asserts graceful failure).
"""

import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ai_srs import generator, menu, strategies, subjects  # noqa: E402

_passed = 0
_failed = 0


def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"PASS  {name}")
    else:
        _failed += 1
        print(f"FAIL  {name}: {detail}")


def _with_urlopen(fake):
    """Swap urllib.request.urlopen; return a restore() callable. No real
    network — the sandbox blocks sockets and we don't need them."""
    orig = urllib.request.urlopen
    urllib.request.urlopen = fake
    return lambda: setattr(urllib.request, "urlopen", orig)


def _refuse(req, timeout=None):
    raise urllib.error.URLError(ConnectionRefusedError("refused"))


# --------------------- resolve_provider ---------------------

def test_defaults_empty_config():
    prov = generator.resolve_provider({})
    check("empty config → openai format", prov.api_format == "openai", prov.api_format)
    check(
        "empty config → default api_base /v1",
        prov.api_base == "http://localhost:11434/v1",
        prov.api_base,
    )
    check("empty config → blank api_key", prov.api_key == "", repr(prov.api_key))
    check("empty config → default timeout 60", prov.timeout == 60, prov.timeout)


def test_explicit_keys():
    cfg = {
        "api_base": "https://api.example.com/v1",
        "api_format": "openai",
        "api_key": "sk-abc",
        "request_timeout_seconds": 30,
    }
    prov = generator.resolve_provider(cfg)
    check("explicit api_base honored", prov.api_base == "https://api.example.com/v1")
    check("explicit api_key honored", prov.api_key == "sk-abc")
    check("explicit timeout honored", prov.timeout == 30, prov.timeout)


def test_legacy_ollama_endpoint_back_compat():
    # Older installs / the meta.json written before this change only had
    # ollama_endpoint. openai format must derive the /v1 base from it.
    prov = generator.resolve_provider({"ollama_endpoint": "http://localhost:11434"})
    check(
        "legacy endpoint → /v1 appended for openai",
        prov.api_base == "http://localhost:11434/v1",
        prov.api_base,
    )
    # ...and the native format must keep it as-is (no /v1).
    prov2 = generator.resolve_provider(
        {"ollama_endpoint": "http://localhost:11434", "api_format": "ollama"}
    )
    check(
        "legacy endpoint kept as-is for ollama format",
        prov2.api_base == "http://localhost:11434",
        prov2.api_base,
    )


def test_unknown_format_defaults_to_openai():
    prov = generator.resolve_provider({"api_format": "banana"})
    check("unknown api_format → openai", prov.api_format == "openai", prov.api_format)


def test_bad_timeout_defaults():
    prov = generator.resolve_provider({"request_timeout_seconds": "not-an-int"})
    check("non-int timeout → 60", prov.timeout == 60, prov.timeout)


# --------------------- request shape per format ---------------------

def test_openai_request_shape():
    req = generator._build_request(
        "qwen2.5:7b", "hello", "http://localhost:11434/v1", 0.3, "openai", "sk-x"
    )
    check(
        "openai URL is /chat/completions",
        req.full_url == "http://localhost:11434/v1/chat/completions",
        req.full_url,
    )
    payload = json.loads(req.data.decode("utf-8"))
    check("openai payload has messages", "messages" in payload, payload)
    check(
        "openai messages user role",
        payload["messages"][0] == {"role": "user", "content": "hello"},
        payload["messages"],
    )
    # urllib normalizes header keys to Title-Case.
    check(
        "openai sends Authorization when key set",
        req.get_header("Authorization") == "Bearer sk-x",
        dict(req.header_items()),
    )


def test_openai_no_auth_header_when_blank():
    req = generator._build_request(
        "m", "hi", "http://localhost:11434/v1", 0.3, "openai", ""
    )
    check(
        "no Authorization header for blank key",
        req.get_header("Authorization") is None,
        dict(req.header_items()),
    )


def test_ollama_request_shape():
    req = generator._build_request(
        "m", "hello", "http://localhost:11434", 0.3, "ollama", ""
    )
    check(
        "ollama URL is /api/generate",
        req.full_url == "http://localhost:11434/api/generate",
        req.full_url,
    )
    payload = json.loads(req.data.decode("utf-8"))
    check("ollama payload has prompt", payload.get("prompt") == "hello", payload)


# --------------------- response parsing per format ---------------------

def test_extract_text_openai():
    parsed = {"choices": [{"message": {"content": "  a sentence  "}}]}
    check(
        "openai extract strips content",
        generator._extract_text(parsed, "openai") == "a sentence",
    )


def test_extract_text_openai_empty_choices():
    check(
        "openai empty choices → ''",
        generator._extract_text({"choices": []}, "openai") == "",
    )


def test_extract_text_ollama():
    check(
        "ollama extract reads response",
        generator._extract_text({"response": "x"}, "ollama") == "x",
    )


# --------------------- fresh install / no-config path ---------------------

def test_generate_card_dead_endpoint_returns_none():
    # Connection refused (no server). generate_card must NEVER raise; it returns
    # None so the caller falls back. This is the fresh-install path where
    # nothing is reachable yet.
    ctx = strategies.GenContext(word="cat", subject="English")
    restore = _with_urlopen(_refuse)
    try:
        out = strategies.generate_card(
            "language", ctx, model="m",
            endpoint="http://localhost:11434/v1",
            timeout=1, retries=0, api_format="openai",
        )
    finally:
        restore()
    check("dead endpoint → generate_card returns None", out is None, out)
    fb = strategies.fallback_card("language", ctx)
    check("fallback card is produced", fb is not None and bool(fb.front))


def test_test_connection_unreachable_is_graceful():
    prov = generator.resolve_provider({"api_base": "http://localhost:11434/v1"})
    restore = _with_urlopen(_refuse)
    try:
        reachable, models, detail = generator.test_connection(prov, timeout=1)
    finally:
        restore()
    check("unreachable → reachable False", reachable is False, reachable)
    check("unreachable → empty models", models == [], models)
    check("unreachable → has guidance text", bool(detail), detail)


def test_nothing_configured_predicate():
    check("fresh empty config → nothing configured", menu._nothing_configured({}) is True)
    check(
        "config with a subject → configured",
        menu._nothing_configured({"subjects": ["Japanese"]}) is False,
    )
    check(
        "config with a deck mapping → configured",
        menu._nothing_configured({"deck_category_map": {"X": "Japanese"}}) is False,
    )


if __name__ == "__main__":
    for fn in list(globals().values()):
        if callable(fn) and getattr(fn, "__name__", "").startswith("test_"):
            fn()
    print(f"\n{_passed}/{_passed + _failed} passed")
    sys.exit(1 if _failed else 0)

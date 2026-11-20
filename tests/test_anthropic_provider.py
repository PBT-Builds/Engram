"""Anthropic provider: wire adapter, `providers:` config, per-category routing.

Mirrors the Ollama/OpenAI coverage in `test_provider_config.py`, one layer at a
time:

  - **Request shape** — `POST /v1/messages`, `x-api-key` (not Bearer), the
    required `anthropic-version` header, and `max_tokens` in the payload.
  - **Response parsing** — `content` is a LIST of typed blocks; text blocks are
    concatenated and non-text blocks (thinking) ignored. An unexpected shape
    becomes a GenerationError so the caller's retry/fallback engages.
  - **Config schema** — the `providers:` map, `default_provider`, per-provider
    timeouts, the api-key env fallback (and the placeholder rule), and the
    back-compat paths (no `providers:` block at all; a pre-`providers:` install
    whose customized top-level `api_base` must survive the upgrade).
  - **Per-category selection** — `models_by_subject` entries as a bare string
    (default provider) or `{provider, model}`, and the shared
    `resolve_for_subject` chokepoint.
  - **Fallback on failure** — a mocked 401 from the Anthropic endpoint must
    leave `generate_card` returning None so the caller falls back to the
    original card, exactly as it does for a dead local server.

Standalone: `python3 tests/test_anthropic_provider.py` (no Anki / Qt / network —
every HTTP call is mocked).
"""

import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ai_srs import generator, strategies, subjects  # noqa: E402

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


# --------------------- mock plumbing ---------------------

class _FakeResp:
    """Minimal stand-in for the object urlopen returns."""

    def __init__(self, body: str):
        self._body = body.encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _with_urlopen(fake):
    """Swap urllib.request.urlopen; return a restore() callable."""
    orig = urllib.request.urlopen
    urllib.request.urlopen = fake
    return lambda: setattr(urllib.request, "urlopen", orig)


def _capturing(body: str, sink: list):
    """A fake urlopen that records the Request it was handed and replies `body`."""
    def fake(req, timeout=None):
        sink.append(req)
        return _FakeResp(body)
    return fake


def _unauthorized(req, timeout=None):
    raise urllib.error.HTTPError(
        req.full_url, 401, "Unauthorized", {}, None
    )


def _anthropic_body(text: str) -> str:
    return json.dumps({
        "id": "msg_01",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-4-5-20251001",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 20},
    })


def _with_env(name, value):
    """Set/clear an env var; return a restore() callable."""
    had = name in os.environ
    old = os.environ.get(name)

    def restore():
        if had:
            os.environ[name] = old
        else:
            os.environ.pop(name, None)

    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value
    return restore


# A representative multi-provider config.
CFG = {
    "providers": {
        "local": {
            "api_format": "openai",
            "api_base": "http://localhost:11434/v1",
            "api_key": "",
        },
        "claude": {
            "api_format": "anthropic",
            "api_base": "https://api.anthropic.com/v1",
            "api_key": "sk-ant-test",
            "model": "claude-haiku-4-5-20251001",
        },
    },
    "default_provider": "local",
}


# --------------------- request shape ---------------------

def test_anthropic_url_is_messages():
    req = generator._build_request(
        "claude-haiku-4-5-20251001", "hello",
        "https://api.anthropic.com/v1", 0.3, "anthropic", "sk-ant-x",
    )
    check(
        "anthropic URL is /v1/messages",
        req.full_url == "https://api.anthropic.com/v1/messages",
        req.full_url,
    )


def test_anthropic_auth_is_x_api_key_not_bearer():
    req = generator._build_request(
        "m", "hi", "https://api.anthropic.com/v1", 0.3, "anthropic", "sk-ant-x"
    )
    # urllib normalizes header keys to Title-Case.
    check(
        "anthropic sends x-api-key",
        req.get_header("X-api-key") == "sk-ant-x",
        dict(req.header_items()),
    )
    check(
        "anthropic does NOT send Authorization",
        req.get_header("Authorization") is None,
        dict(req.header_items()),
    )


def test_anthropic_version_header_required():
    req = generator._build_request(
        "m", "hi", "https://api.anthropic.com/v1", 0.3, "anthropic", "sk-ant-x"
    )
    check(
        "anthropic-version header present",
        req.get_header("Anthropic-version") == generator.ANTHROPIC_VERSION,
        dict(req.header_items()),
    )


def test_anthropic_version_sent_even_without_key():
    req = generator._build_request(
        "m", "hi", "https://api.anthropic.com/v1", 0.3, "anthropic", ""
    )
    check(
        "no x-api-key header for blank key",
        req.get_header("X-api-key") is None,
        dict(req.header_items()),
    )
    check(
        "anthropic-version still sent for blank key",
        req.get_header("Anthropic-version") == generator.ANTHROPIC_VERSION,
        dict(req.header_items()),
    )


def test_anthropic_payload_shape():
    req = generator._build_request(
        "claude-haiku-4-5-20251001", "hello",
        "https://api.anthropic.com/v1", 0.7, "anthropic", "k",
    )
    payload = json.loads(req.data.decode("utf-8"))
    check("anthropic payload model", payload.get("model") == "claude-haiku-4-5-20251001", payload)
    check(
        "anthropic payload messages",
        payload.get("messages") == [{"role": "user", "content": "hello"}],
        payload,
    )
    check(
        "anthropic payload carries required max_tokens",
        payload.get("max_tokens") == generator.ANTHROPIC_MAX_TOKENS,
        payload,
    )
    check("anthropic payload temperature", payload.get("temperature") == 0.7, payload)
    check(
        "anthropic payload has no openai-only keys",
        "choices" not in payload and "stream" not in payload,
        payload,
    )


# --------------------- response parsing ---------------------

def test_extract_text_anthropic_block_list():
    parsed = {"content": [{"type": "text", "text": "  a sentence  "}]}
    check(
        "anthropic extract reads content[].text",
        generator._extract_text(parsed, "anthropic") == "a sentence",
    )


def test_extract_text_anthropic_ignores_non_text_blocks():
    parsed = {"content": [
        {"type": "thinking", "thinking": "hmm"},
        {"type": "text", "text": "one "},
        {"type": "text", "text": "two"},
    ]}
    check(
        "anthropic joins text blocks and skips thinking",
        generator._extract_text(parsed, "anthropic") == "one two",
        generator._extract_text(parsed, "anthropic"),
    )


def test_extract_text_anthropic_empty_content():
    check(
        "anthropic empty content → ''",
        generator._extract_text({"content": []}, "anthropic") == "",
    )


def test_extract_text_anthropic_bad_shape_raises_generation_error():
    # An error envelope has no `content` list at all. The dispatcher must turn
    # any shape surprise into GenerationError so retry/fallback engages instead
    # of an AttributeError escaping into the reviewer.
    parsed = {"type": "error", "error": {"type": "invalid_request_error"}}
    out = generator._extract_text(parsed, "anthropic")
    check("anthropic error envelope → empty text", out == "", out)

    raised = None
    try:
        generator._extract_text({"content": 17}, "anthropic")
    except generator.GenerationError as e:
        raised = e
    check("anthropic non-iterable content → GenerationError", raised is not None)


def test_generate_raw_anthropic_end_to_end():
    sink = []
    restore = _with_urlopen(_capturing(_anthropic_body("Ein Satz."), sink))
    try:
        out = generator.generate_raw(
            "claude-haiku-4-5-20251001", "prompt",
            "https://api.anthropic.com/v1", timeout=5,
            api_format="anthropic", api_key="sk-ant-x",
        )
    finally:
        restore()
    check("generate_raw anthropic returns block text", out == "Ein Satz.", out)
    check("generate_raw anthropic hit /v1/messages", sink[0].full_url.endswith("/v1/messages"))


# --------------------- providers: config schema ---------------------

def test_providers_from_config_parses_both():
    provs = generator.providers_from_config(CFG)
    check("both providers parsed", set(provs) == {"local", "claude"}, list(provs))
    check("claude format", provs["claude"].api_format == "anthropic")
    check("claude base", provs["claude"].api_base == "https://api.anthropic.com/v1")
    check("claude default model", provs["claude"].model == "claude-haiku-4-5-20251001")
    check("local format", provs["local"].api_format == "openai")
    check("provider carries its own name", provs["claude"].name == "claude")


def test_no_providers_block_falls_back_to_legacy_single_provider():
    provs = generator.providers_from_config({"api_base": "http://localhost:1234/v1"})
    check("legacy config → exactly one provider", list(provs) == ["local"], list(provs))
    check(
        "legacy config → its api_base",
        provs["local"].api_base == "http://localhost:1234/v1",
        provs["local"].api_base,
    )


def test_malformed_provider_entry_ignored():
    provs = generator.providers_from_config({
        "providers": {"claude": {"api_format": "anthropic"}, "junk": "not-a-dict"}
    })
    check("non-dict provider entry dropped", set(provs) == {"claude"}, list(provs))
    check(
        "anthropic entry gets the vendor default base",
        provs["claude"].api_base == generator.ANTHROPIC_API_BASE,
        provs["claude"].api_base,
    )


def test_default_provider_selection():
    check("default_provider honored", generator.default_provider_name(CFG) == "local")
    cfg2 = dict(CFG, default_provider="claude")
    check("default_provider switchable", generator.default_provider_name(cfg2) == "claude")
    cfg3 = dict(CFG, default_provider="nonexistent")
    check(
        "unknown default_provider → local",
        generator.default_provider_name(cfg3) == "local",
    )


def test_resolve_provider_by_name():
    prov = generator.resolve_provider(CFG, "claude")
    check("named lookup returns claude", prov.name == "claude" and prov.api_format == "anthropic")
    prov2 = generator.resolve_provider(CFG)
    check("unnamed lookup returns default", prov2.name == "local")


def test_resolve_provider_unknown_name_falls_back():
    prov = generator.resolve_provider(CFG, "does-not-exist")
    check(
        "unknown provider name falls back to default (never raises)",
        prov.name == "local",
        prov.name,
    )


def test_per_provider_timeout_override_and_inherit():
    cfg = {
        "request_timeout_seconds": 60,
        "providers": {
            "local": {"api_base": "http://x/v1"},
            "claude": {"api_format": "anthropic", "request_timeout_seconds": 20},
        },
    }
    provs = generator.providers_from_config(cfg)
    check("provider inherits top-level timeout", provs["local"].timeout == 60, provs["local"].timeout)
    check("provider overrides timeout", provs["claude"].timeout == 20, provs["claude"].timeout)


# --------------------- api key resolution ---------------------

def test_api_key_from_config_wins():
    prov = generator.resolve_provider(CFG, "claude")
    check("config api_key used", prov.api_key == "sk-ant-test", prov.api_key)


def test_api_key_env_fallback_for_anthropic():
    cfg = {"providers": {"claude": {"api_format": "anthropic", "api_key": ""}}}
    restore = _with_env("ANTHROPIC_API_KEY", "sk-ant-from-env")
    try:
        prov = generator.resolve_provider(cfg, "claude")
    finally:
        restore()
    check("blank api_key → ANTHROPIC_API_KEY", prov.api_key == "sk-ant-from-env", prov.api_key)


def test_placeholder_api_key_treated_as_unset():
    cfg = {"providers": {"claude": {
        "api_format": "anthropic", "api_key": "<from Anthropic Console>",
    }}}
    restore = _with_env("ANTHROPIC_API_KEY", "sk-ant-real")
    try:
        prov = generator.resolve_provider(cfg, "claude")
    finally:
        restore()
    check(
        "placeholder key ignored in favour of env",
        prov.api_key == "sk-ant-real", prov.api_key,
    )


def test_no_env_fallback_for_openai_format():
    # A local Ollama must never pick up a stray OPENAI_API_KEY and start
    # attaching an Authorization header to localhost requests.
    cfg = {"providers": {"local": {"api_format": "openai", "api_key": ""}}}
    restore = _with_env("OPENAI_API_KEY", "sk-should-not-be-used")
    try:
        prov = generator.resolve_provider(cfg, "local")
    finally:
        restore()
    check("openai format does not read OPENAI_API_KEY", prov.api_key == "", prov.api_key)


def test_explicit_api_key_env_honored_for_any_format():
    cfg = {"providers": {"cloud": {
        "api_format": "openai", "api_key": "", "api_key_env": "MY_CUSTOM_KEY",
    }}}
    restore = _with_env("MY_CUSTOM_KEY", "sk-custom")
    try:
        prov = generator.resolve_provider(cfg, "cloud")
    finally:
        restore()
    check("explicit api_key_env honored", prov.api_key == "sk-custom", prov.api_key)


def test_missing_env_var_is_blank_not_a_crash():
    cfg = {"providers": {"claude": {"api_format": "anthropic", "api_key": ""}}}
    restore = _with_env("ANTHROPIC_API_KEY", None)
    try:
        prov = generator.resolve_provider(cfg, "claude")
    finally:
        restore()
    check("absent env var → blank key, no raise", prov.api_key == "", prov.api_key)


# --------------------- back-compat upgrade path ---------------------

def test_customized_legacy_api_base_survives_upgrade():
    # An install that predates `providers:` keeps its whole old config in
    # meta.json. A CUSTOMIZED top-level api_base (here LM Studio's port) is real
    # user intent and must not be silently replaced by the shipped local entry.
    cfg = dict(CFG, api_base="http://localhost:1234/v1")
    prov = generator.resolve_provider(cfg)
    check(
        "customized legacy api_base overrides providers.local",
        prov.api_base == "http://localhost:1234/v1",
        prov.api_base,
    )
    check(
        "the non-default provider is untouched by the fold",
        generator.resolve_provider(cfg, "claude").api_base
        == "https://api.anthropic.com/v1",
    )


def test_default_legacy_api_base_does_not_override():
    # Left at the old shipped default → carries no intent → providers: wins.
    cfg = dict(CFG, api_base="http://localhost:11434/v1")
    cfg["providers"] = dict(CFG["providers"])
    cfg["providers"]["local"] = {"api_format": "openai", "api_base": "http://elsewhere/v1"}
    prov = generator.resolve_provider(cfg)
    check(
        "uncustomized legacy api_base ignored",
        prov.api_base == "http://elsewhere/v1",
        prov.api_base,
    )


# --------------------- per-category provider selection ---------------------

def test_bare_string_entry_means_default_provider():
    cfg = dict(CFG, models_by_subject={"Japanese": "qwen2.5:7b"})
    name, model = subjects.provider_and_model_for_subject(cfg, "Japanese")
    check("bare string → no provider named", name is None, name)
    check("bare string → the model", model == "qwen2.5:7b", model)
    prov, resolved = generator.resolve_for_subject(cfg, "Japanese")
    check("bare string routes to default provider", prov.name == "local", prov.name)
    check("bare string keeps its model", resolved == "qwen2.5:7b", resolved)


def test_dict_entry_selects_provider_and_model():
    cfg = dict(CFG, models_by_subject={
        "History": {"provider": "claude", "model": "claude-haiku-4-5-20251001"},
    })
    name, model = subjects.provider_and_model_for_subject(cfg, "History")
    check("dict entry → provider", name == "claude", name)
    prov, resolved = generator.resolve_for_subject(cfg, "History")
    check("dict entry routes to claude", prov.name == "claude", prov.name)
    check("dict entry api_format is anthropic", prov.api_format == "anthropic")
    check("dict entry model", resolved == "claude-haiku-4-5-20251001", resolved)


def test_dict_entry_without_model_uses_provider_default():
    cfg = dict(CFG, models_by_subject={"History": {"provider": "claude"}})
    prov, resolved = generator.resolve_for_subject(cfg, "History")
    check("provider named, model omitted → provider's own model",
          resolved == "claude-haiku-4-5-20251001", resolved)
    check("still routed to claude", prov.name == "claude")


def test_malformed_entry_falls_back_safely():
    cfg = dict(CFG, models_by_subject={"Broken": ["not", "valid"]})
    name, model = subjects.provider_and_model_for_subject(cfg, "Broken")
    check("malformed entry → no provider", name is None, name)
    check("malformed entry → default model", model == subjects.DEFAULT_MODEL, model)


def test_model_for_subject_still_returns_a_string_for_dict_entries():
    # Existing callers (and the Setup dialog's overview text) call this and
    # concatenate the result — it must never hand back a dict.
    cfg = dict(CFG, models_by_subject={
        "History": {"provider": "claude", "model": "claude-haiku-4-5-20251001"},
    })
    got = subjects.model_for_subject(cfg, "History")
    check("model_for_subject unwraps dict entries",
          got == "claude-haiku-4-5-20251001", repr(got))


# --------------------- generation through the anthropic path ---------------------

def test_generate_card_via_anthropic_provider():
    sink = []
    body = _anthropic_body(json.dumps({
        "sentence": "Der Hund schläft.",
        "translation": "The dog is sleeping.",
        "gloss": "dog",
    }))
    ctx = strategies.GenContext(word="Hund", subject="German")
    restore = _with_urlopen(_capturing(body, sink))
    try:
        out = strategies.generate_card(
            "language", ctx, model="claude-haiku-4-5-20251001",
            endpoint="https://api.anthropic.com/v1", timeout=5, retries=0,
            api_format="anthropic", api_key="sk-ant-x",
        )
    finally:
        restore()
    check("anthropic generation produces a card", out is not None and "Hund" in out.front, out)
    check("request went to the Messages API", sink[0].full_url.endswith("/v1/messages"))
    check("request carried x-api-key", sink[0].get_header("X-api-key") == "sk-ant-x")


def test_anthropic_failure_falls_back_to_original_card():
    # THE fallback guarantee, on the cloud path: a 401 (bad/expired key) must
    # behave exactly like a dead local server — generate_card returns None and
    # never raises, so the caller shows the original card instead of a broken
    # review. Same contract as test_provider_config's dead-endpoint test.
    ctx = strategies.GenContext(
        word="Hund", subject="German",
        source_front="Hund", source_back="dog",
    )
    restore = _with_urlopen(_unauthorized)
    try:
        out = strategies.generate_card(
            "language", ctx, model="claude-haiku-4-5-20251001",
            endpoint="https://api.anthropic.com/v1", timeout=1, retries=1,
            api_format="anthropic", api_key="sk-ant-bad",
        )
    finally:
        restore()
    check("401 → generate_card returns None (never raises)", out is None, out)
    fb = strategies.fallback_card("language", ctx)
    check("fallback card is produced", fb is not None and bool(fb.front), fb)


def test_anthropic_rate_limit_also_falls_back():
    def _rate_limited(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, None)

    ctx = strategies.GenContext(word="Hund", subject="German")
    restore = _with_urlopen(_rate_limited)
    try:
        out = strategies.generate_card(
            "language", ctx, model="m", endpoint="https://api.anthropic.com/v1",
            timeout=1, retries=0, api_format="anthropic", api_key="k",
        )
    finally:
        restore()
    check("429 → generate_card returns None", out is None, out)


# --------------------- Test connection, per provider ---------------------

def test_all_providers_reports_one_status_each_default_first():
    def _refuse(req, timeout=None):
        raise urllib.error.URLError(ConnectionRefusedError("refused"))

    restore = _with_urlopen(_refuse)
    try:
        statuses = generator.test_all_providers(CFG, timeout=1)
    finally:
        restore()
    check("one status per provider", len(statuses) == 2, len(statuses))
    check("default provider reported first", statuses[0].name == "local", statuses[0].name)
    check("other provider also reported", statuses[1].name == "claude", statuses[1].name)
    check(
        "each status carries its own resolved provider",
        statuses[1].provider.api_format == "anthropic",
    )


def test_missing_api_key_reported_without_a_network_call():
    calls = []

    def _record(req, timeout=None):
        calls.append(req)
        raise urllib.error.URLError(ConnectionRefusedError("refused"))

    cfg = {"providers": {"claude": {"api_format": "anthropic", "api_key": ""}}}
    env_restore = _with_env("ANTHROPIC_API_KEY", None)
    restore = _with_urlopen(_record)
    try:
        reachable, models, detail = generator.test_connection(
            generator.resolve_provider(cfg, "claude"), timeout=1
        )
    finally:
        restore()
        env_restore()
    check("no key → not reachable", reachable is False, reachable)
    check("no key → says so", "API key" in detail, detail)
    check("no key → no pointless request made", calls == [], calls)


def test_cloud_provider_does_not_fall_back_to_a_root_ping():
    # A root ping against api.anthropic.com proves nothing, so an unreachable
    # cloud provider must report NOT reachable rather than "server reachable".
    pings = []

    def _root_ok(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        pings.append(url)
        if url.endswith("/models"):
            raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)
        return _FakeResp("ok")

    restore = _with_urlopen(_root_ok)
    try:
        reachable, models, detail = generator.test_connection(
            generator.resolve_provider(CFG, "claude"), timeout=1
        )
    finally:
        restore()
    check("cloud 401 → NOT reachable", reachable is False, reachable)
    check("cloud probe never pinged the bare root",
          all(u.endswith("/models") for u in pings), pings)
    check("cloud hint mentions the key", "key" in detail.lower(), detail)


def test_local_provider_still_falls_back_to_root_ping():
    def _only_root(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        if url.endswith("/models"):
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return _FakeResp("Ollama is running")

    restore = _with_urlopen(_only_root)
    try:
        reachable, models, detail = generator.test_connection(
            generator.resolve_provider(CFG, "local"), timeout=1
        )
    finally:
        restore()
    check("local server with no model route → still reachable", reachable is True, detail)


def test_one_unreachable_provider_does_not_hide_the_other():
    def _mixed(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        if "anthropic.com" in url:
            return _FakeResp(json.dumps(
                {"data": [{"id": "claude-haiku-4-5-20251001"}]}
            ))
        raise urllib.error.URLError(ConnectionRefusedError("refused"))

    restore = _with_urlopen(_mixed)
    try:
        statuses = generator.test_all_providers(CFG, timeout=1)
    finally:
        restore()
    by_name = {s.name: s for s in statuses}
    check("dead local reported unreachable", by_name["local"].reachable is False)
    check("live claude still reported reachable", by_name["claude"].reachable is True)
    check(
        "live claude lists its models",
        by_name["claude"].models == ["claude-haiku-4-5-20251001"],
        by_name["claude"].models,
    )


# --------------------- shipped config ---------------------

def _shipped_config():
    path = os.path.join(
        os.path.dirname(__file__), "..", "src", "ai_srs", "config.json"
    )
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def test_shipped_config_has_providers():
    cfg = _shipped_config()
    provs = cfg.get("providers", {})
    check("shipped config has providers", set(provs) >= {"local", "claude"}, list(provs))
    check(
        "shipped claude entry uses the anthropic format",
        provs.get("claude", {}).get("api_format") == "anthropic",
    )
    check(
        "shipped config ships NO api key",
        provs.get("claude", {}).get("api_key") == "",
        provs.get("claude", {}).get("api_key"),
    )
    check("shipped default_provider is local", cfg.get("default_provider") == "local")


def test_shipped_config_default_still_local_ollama():
    # The multi-provider change must not move an existing user off their local
    # model: the shipped default resolves exactly as before.
    prov = generator.resolve_provider(_shipped_config())
    check("shipped default provider is local", prov.name == "local", prov.name)
    check(
        "shipped default base unchanged",
        prov.api_base == "http://localhost:11434/v1",
        prov.api_base,
    )
    check("shipped default format unchanged", prov.api_format == "openai", prov.api_format)
    check("shipped default sends no key", prov.api_key == "", prov.api_key)


if __name__ == "__main__":
    for fn in list(globals().values()):
        if callable(fn) and getattr(fn, "__name__", "").startswith("test_"):
            fn()
    print(f"\n{_passed}/{_passed + _failed} passed")
    sys.exit(1 if _failed else 0)

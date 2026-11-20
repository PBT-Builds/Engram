"""Graceful degradation when the extraction backend is missing or unreachable.

The guarantee under test: a dead LLM skips EXTRACTION and nothing else. Vault
scanning still works, nothing raises, and the user gets one clear line saying
why — once, not on every scan.

Covers:
  - `generator.missing_credential` / `credential_hint` — the offline half of the
    key check, and that `test_connection` still short-circuits on it.
  - `vault_ingest.check_extraction_available` — the three ways a backend is
    unusable (no model, no API key, nothing answering), each with its own
    reason, plus the happy path and the probe-failure path.
  - `extract_candidates` under a dead backend — returns [], never raises, and
    (the point of the offline pre-flight) makes NO network call when the config
    alone rules the provider out.
  - `scan_vault` is unaffected by any of it.
  - `ui_notify.extraction_unavailable_once` — says a given reason once per
    session, but a NEW reason still speaks.

Standalone: `python tests/test_extraction_fallback.py` (no Anki, no Qt, no
network — the transport is monkeypatched and the vault is a temp dir).
"""

import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ai_srs import db, generator, ui_notify, vault_ingest  # noqa: E402

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


# --------------------- fixtures ---------------------

def _cfg(**overrides):
    """A config with both a local and a cloud provider, like the shipped one."""
    cfg = {
        "providers": {
            "local": {
                "api_format": "openai",
                "api_base": "http://localhost:11434/v1",
                "api_key": "",
                "model": "phi4",
            },
            "claude": {
                "api_format": "anthropic",
                "api_base": "https://api.anthropic.com/v1",
                "api_key": "",
                "api_key_env": "ENGRAM_TEST_KEY_THAT_IS_UNSET",
                "model": "claude-haiku-4-5-20251001",
            },
        },
        "default_provider": "local",
        "extraction_provider": "claude",
        "extraction_model": "",
        "vault_path": "",
        "vault_category_map": {},
        "max_cards_per_note": 15,
    }
    cfg.update(overrides)
    return cfg


def _with_key(cfg, key="sk-ant-test"):
    cfg["providers"]["claude"]["api_key"] = key
    return cfg


class _Transport:
    """Swaps `urllib.request.urlopen` for a stub and counts the calls, so a test
    can assert that NO request was made."""

    def __init__(self, handler):
        self.handler = handler
        self.calls = 0
        self._real = None

    def __enter__(self):
        self._real = urllib.request.urlopen

        def fake(req, timeout=None):
            self.calls += 1
            return self.handler(req)

        urllib.request.urlopen = fake
        return self

    def __exit__(self, *exc):
        urllib.request.urlopen = self._real
        return False


def _refuse(req):
    raise urllib.error.URLError("connection refused")


class _Resp:
    def __init__(self, body):
        self.body = body.encode("utf-8")

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _note(tmpdir, rel="Weekly/Physics/week-12.md", text="# Physics\n\nWork = force x distance.\n"):
    path = os.path.join(tmpdir, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return vault_ingest.VaultNote(
        rel_path=rel, abs_path=path, mtime=os.path.getmtime(path), category="Physics"
    )


# --------------------- generator helpers ---------------------

def test_missing_credential():
    cfg = _cfg()
    claude = generator.resolve_provider(cfg, "claude")
    local = generator.resolve_provider(cfg, "local")

    check("anthropic with no key is missing a credential",
          generator.missing_credential(claude))
    check("a local openai provider needs no credential",
          not generator.missing_credential(local))

    with_key = generator.resolve_provider(_with_key(_cfg()), "claude")
    check("anthropic with a key is not missing a credential",
          not generator.missing_credential(with_key))

    hint = generator.credential_hint(claude)
    check("the hint names the provider", "'claude'" in hint, hint)
    check("the hint names the env var", "ANTHROPIC_API_KEY" in hint, hint)


def test_test_connection_short_circuits_without_a_key():
    """The no-key path must not make a request that can only 401."""
    cfg = _cfg()
    prov = generator.resolve_provider(cfg, "claude")
    with _Transport(_refuse) as t:
        reachable, models, detail = generator.test_connection(prov, timeout=1)
    check("keyless probe reports unreachable", reachable is False)
    check("keyless probe lists no models", models == [])
    check("keyless probe made NO request", t.calls == 0, t.calls)
    check("keyless probe explains the key", "API key" in detail, detail)


# --------------------- availability check ---------------------

def test_available_when_key_and_model_present():
    cfg = _with_key(_cfg())

    def models_ok(req):
        return _Resp('{"data": [{"id": "claude-haiku-4-5-20251001"}]}')

    with _Transport(models_ok):
        avail = vault_ingest.check_extraction_available(cfg)
    check("reachable provider is available", avail.available, avail.reason)
    check("available has no reason", avail.reason == "", avail.reason)
    check("reports the provider", avail.provider == "claude", avail.provider)
    check("reports the model",
          avail.model == "claude-haiku-4-5-20251001", avail.model)
    check("truthy when available", bool(avail))


def test_unavailable_without_api_key():
    cfg = _cfg()
    with _Transport(_refuse) as t:
        avail = vault_ingest.check_extraction_available(cfg)
    check("no key -> unavailable", not avail.available)
    check("no key -> falsy", not avail)
    check("no key reason mentions the key", "API key" in avail.reason, avail.reason)
    check("no key reason names the provider", "claude" in avail.reason, avail.reason)
    check("no key makes NO request", t.calls == 0, t.calls)


def test_unavailable_without_a_model():
    cfg = _with_key(_cfg())
    cfg["providers"]["claude"]["model"] = ""
    with _Transport(_refuse) as t:
        avail = vault_ingest.check_extraction_available(cfg)
    check("no model -> unavailable", not avail.available)
    check("no model reason mentions extraction_model",
          "extraction_model" in avail.reason, avail.reason)
    check("no model makes NO request", t.calls == 0, t.calls)


def test_extraction_model_overrides_the_provider_model():
    cfg = _with_key(_cfg(extraction_model="claude-opus-5"))
    avail = vault_ingest.check_extraction_available(cfg, probe=False)
    check("extraction_model wins", avail.model == "claude-opus-5", avail.model)
    check("available with probe off", avail.available, avail.reason)


def test_unavailable_when_local_provider_is_down():
    """The 'Ollama isn't running' case: key not required, nothing answers."""
    cfg = _cfg(extraction_provider="local")
    with _Transport(_refuse) as t:
        avail = vault_ingest.check_extraction_available(cfg, timeout=1)
    check("dead local provider -> unavailable", not avail.available)
    check("dead local names the provider", "local" in avail.reason, avail.reason)
    check("dead local suggests the server",
          "server" in avail.reason.lower() or "ollama" in avail.reason.lower(),
          avail.reason)
    check("dead local actually probed", t.calls > 0, t.calls)


def test_probe_can_be_skipped():
    cfg = _cfg(extraction_provider="local")
    with _Transport(_refuse) as t:
        avail = vault_ingest.check_extraction_available(cfg, probe=False)
    check("probe=False skips the network", t.calls == 0, t.calls)
    check("probe=False trusts the config", avail.available, avail.reason)


def test_probe_exception_is_reported_not_raised():
    """A probe that blows up in an unexpected way is still just 'unavailable'."""
    real = generator.test_connection
    generator.test_connection = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        avail = vault_ingest.check_extraction_available(_cfg(extraction_provider="local"))
    finally:
        generator.test_connection = real
    check("probe crash -> unavailable", not avail.available)
    check("probe crash reports the error", "boom" in avail.reason, avail.reason)


def test_unknown_extraction_provider_falls_back_to_default():
    """A typo in `extraction_provider` must not be a hard failure — it resolves
    to the default provider, exactly as generation does."""
    cfg = _cfg(extraction_provider="cluade")
    avail = vault_ingest.check_extraction_available(cfg, probe=False)
    check("unknown provider falls back to default",
          avail.provider == "local", avail.provider)


# --------------------- extraction under a dead backend ---------------------

def test_extract_returns_nothing_and_never_raises():
    tmp = tempfile.mkdtemp(prefix="engram-fallback-")
    try:
        note = _note(tmp)
        cfg = _cfg()  # no key
        with _Transport(_refuse) as t:
            out = vault_ingest.extract_candidates(note, cfg)
        check("dead backend yields no candidates", out == [], out)
        check("dead backend made NO request (offline pre-flight)",
              t.calls == 0, t.calls)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_extract_still_tries_when_config_is_usable():
    """The offline pre-flight must not become a second gate — a configured
    provider that happens to be down should still be attempted (and fail
    gracefully), or a transient outage would look like a config error."""
    tmp = tempfile.mkdtemp(prefix="engram-fallback-")
    try:
        note = _note(tmp)
        cfg = _with_key(_cfg())
        with _Transport(_refuse) as t:
            out = vault_ingest.extract_candidates(note, cfg, retries=0)
        check("transient outage yields no candidates", out == [], out)
        check("transient outage DID attempt a request", t.calls > 0, t.calls)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_scanning_is_unaffected_by_a_dead_backend():
    """The whole point: vault scanning must not depend on the LLM at all."""
    tmp = tempfile.mkdtemp(prefix="engram-fallback-")
    addon = tempfile.mkdtemp(prefix="engram-addon-")
    try:
        _note(tmp)
        conn = db.connect(addon)
        cfg = _cfg(
            vault_path=tmp,
            vault_category_map={"Weekly/Physics": "Physics"},
        )
        vcfg = vault_ingest.resolve_vault_config(cfg)
        with _Transport(_refuse):
            notes = vault_ingest.scan_vault(vcfg, conn)
        check("scan works with no provider at all", len(notes) == 1, notes)
        check("scan resolved the category",
              notes and notes[0].category == "Physics", notes)
        conn.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(addon, ignore_errors=True)


# --------------------- the one-line, once-per-session notice ---------------------

def test_notice_shows_once_per_reason():
    ui_notify.reset_extraction_notices()
    reason = "No API key configured for provider 'claude'."
    check("first time speaks", ui_notify.extraction_unavailable_once(reason) is True)
    check("second time stays quiet",
          ui_notify.extraction_unavailable_once(reason) is False)
    check("third time stays quiet",
          ui_notify.extraction_unavailable_once(reason) is False)


def test_a_new_reason_still_speaks():
    """Fixing the key and then hitting a dead endpoint is a NEW fact, not a
    repeat — suppressing it would hide the second problem behind the first."""
    ui_notify.reset_extraction_notices()
    ui_notify.extraction_unavailable_once("No API key configured.")
    check("a different reason is not suppressed",
          ui_notify.extraction_unavailable_once("Provider 'local' is not reachable.")
          is True)


def test_empty_reason_says_nothing():
    ui_notify.reset_extraction_notices()
    check("empty reason is a no-op", ui_notify.extraction_unavailable_once("") is False)
    check("blank reason is a no-op", ui_notify.extraction_unavailable_once("   ") is False)


def test_reset_lets_a_reason_speak_again():
    ui_notify.reset_extraction_notices()
    reason = "Provider 'local' is not reachable."
    ui_notify.extraction_unavailable_once(reason)
    ui_notify.reset_extraction_notices()
    check("reset clears the memory",
          ui_notify.extraction_unavailable_once(reason) is True)


if __name__ == "__main__":
    for fn in list(globals().values()):
        if callable(fn) and getattr(fn, "__name__", "").startswith("test_"):
            fn()
    print(f"\n{_passed}/{_passed + _failed} passed")
    sys.exit(1 if _failed else 0)

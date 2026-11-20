"""LLM generation client (spec §5) — provider-flexible.

Take a target word, call a local (or remote) LLM over HTTP, return a single
generated sentence. `generate_validated` (Step 6) layers validation + retry on
top: generate, check the sentence actually contains the target word, and retry
with an emphasized prompt up to N times; the caller handles the fallback when
all attempts fail. Pure transport + orchestration: no Anki, no Qt.

Two independent concepts, deliberately kept separate:

  - **provider** — WHICH server we talk to (`api_base` + credentials + a default
    model). "local" (an Ollama/LM Studio on this machine) and "claude" (the
    Anthropic API) are different providers.
  - **api_format** — the WIRE SHAPE that server speaks: how a request is built
    and how the reply is read. Several providers can share one format.

Supported `api_format`s:

  - "openai" (default): the OpenAI-compatible `/v1/chat/completions` endpoint,
    which Ollama (>=0.1.24), LM Studio, llama.cpp's server, Jan, vLLM, and the
    OpenAI/Together/Groq/etc. cloud APIs all speak. This is the default so users
    aren't tied to one runtime; `api_base` defaults to Ollama's
    `http://localhost:11434/v1`.
  - "ollama": Ollama's native `/api/generate`. Kept as a selectable fallback so
    nothing about the previously-working Ollama path is lost.

Because the two are separate, pointing Engram at a *new* server that speaks an
existing format is a config entry (a new `providers:` block), not new code. Only
a genuinely new wire shape needs a new `_Adapter` subclass below.

All generation funnels through `generate_raw`, the single HTTP primitive, so the
format dispatch lives in exactly one place. `resolve_provider(cfg)` is the one
place that turns config into a `ProviderConfig`; every call site uses it so the
endpoint/format/key are resolved identically everywhere. stdlib only — every
call is just JSON over urllib; no `openai`/`anthropic` package, no deps.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from typing import Optional

from . import validation
from .log import get_logger

log = get_logger()

# Provider defaults. The default backend is an OpenAI-compatible endpoint
# pointed at a local Ollama (its `/v1` shim), so a stock Ollama install works
# out of the box AND swapping in LM Studio / llama.cpp / a cloud API is a
# config-only change.
DEFAULT_API_FORMAT = "openai"
DEFAULT_API_BASE = "http://localhost:11434/v1"
DEFAULT_OLLAMA_ENDPOINT = "http://localhost:11434"
DEFAULT_PROVIDER_NAME = "local"

# Anthropic Messages API specifics.
ANTHROPIC_API_BASE = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"
# `max_tokens` is REQUIRED by the Messages API (the other two formats have no
# equivalent, so it isn't part of the shared generate_raw signature). 4096 is a
# generous ceiling for what Engram asks for — one JSON flashcard, or a list of
# candidate cards from one vault note — while capping the worst case of a model
# that runs away. Raise it here if extraction ever truncates.
ANTHROPIC_MAX_TOKENS = 4096

# Formats whose credential may come from the environment when config leaves
# `api_key` blank. Deliberately NOT "openai": that format is what a local
# Ollama / LM Studio speaks, and silently attaching a stray OPENAI_API_KEY to a
# localhost request would be a surprise. Any provider can still opt in
# explicitly with `api_key_env`.
_ENV_KEY_BY_FORMAT = {"anthropic": "ANTHROPIC_API_KEY"}


class GenerationError(RuntimeError):
    """Raised when the LLM returns no usable text."""


# --------------------------------------------------------------------------
# api_format adapters — the wire shape, and NOTHING about which server.
# --------------------------------------------------------------------------
# Each adapter owns exactly four things for its format: how to build a
# generation request, how to pull the text back out of the reply, where the
# model list lives, and how to authenticate. An adapter never reads config and
# never decides which endpoint to talk to — that is the provider's job. Keeping
# the split means "support a new server" is usually a config entry, and only a
# new wire protocol costs a subclass.


def _json_request(url: str, payload: dict, headers: dict) -> "urllib.request.Request":
    data = json.dumps(payload).encode("utf-8")
    return urllib.request.Request(url, data=data, headers=headers)


class _Adapter:
    """Base wire-format adapter. Subclasses implement the four hooks."""

    name = ""
    # Whether a bare GET on the server root is a meaningful liveness probe.
    # True for self-hosted servers (Ollama answers "Ollama is running"); False
    # for cloud APIs, whose root is a 403/404 and tells us nothing useful.
    supports_root_ping = True
    # Whether a credential is mandatory. Lets Test connection say "no API key
    # configured" instead of making a request that can only 401.
    requires_api_key = False

    def auth_headers(self, api_key: str) -> dict:
        """Headers that carry credentials, if any."""
        return {}

    def unreachable_hint(self) -> str:
        """What to tell the user when nothing answered."""
        return (
            "Not reachable. Is your local LLM server running at this address? "
            "For Ollama, start it and run `ollama serve`."
        )

    def build_request(
        self, model: str, prompt: str, api_base: str,
        temperature: float, api_key: str, json_mode: bool = False,
    ) -> "urllib.request.Request":
        """`json_mode` asks the server to constrain its reply to valid JSON,
        where the wire format has a switch for it."""
        raise NotImplementedError

    def extract_text(self, parsed: dict) -> str:
        """Pull the generated text out of an already-parsed JSON response."""
        raise NotImplementedError

    def models_url(self, api_base: str) -> str:
        """Where this format lists available models."""
        raise NotImplementedError

    def parse_models(self, parsed: dict) -> list:
        """Model names out of a parsed model-list response."""
        raise NotImplementedError


class _OpenAIAdapter(_Adapter):
    """OpenAI-compatible `/v1/chat/completions` — the portable default."""

    name = "openai"

    def auth_headers(self, api_key: str) -> dict:
        return {"Authorization": f"Bearer {api_key}"} if api_key else {}

    def build_request(self, model, prompt, api_base, temperature, api_key,
                      json_mode=False):
        headers = {"Content-Type": "application/json"}
        headers.update(self.auth_headers(api_key))
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "stream": False,
        }
        if json_mode:
            # Ollama's /v1, vLLM, llama.cpp and OpenAI accept this; a server
            # that doesn't answers 400 and generate_raw retries without it.
            payload["response_format"] = {"type": "json_object"}
        return _json_request(
            api_base.rstrip("/") + "/chat/completions", payload, headers
        )

    def extract_text(self, parsed):
        choices = parsed.get("choices") or []
        if not choices:
            return ""
        return (choices[0].get("message", {}).get("content") or "").strip()

    def models_url(self, api_base):
        return api_base.rstrip("/") + "/models"

    def parse_models(self, parsed):
        return [m.get("id") for m in parsed.get("data", []) if m.get("id")]


class _OllamaAdapter(_Adapter):
    """Ollama's native `/api/generate`. Kept for back-compat with setups that
    were working before the OpenAI-compatible default landed."""

    name = "ollama"

    def build_request(self, model, prompt, api_base, temperature, api_key,
                      json_mode=False):
        headers = {"Content-Type": "application/json"}
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature},
        }
        if json_mode:
            payload["format"] = "json"
        return _json_request(
            api_base.rstrip("/") + "/api/generate", payload, headers
        )

    def extract_text(self, parsed):
        return (parsed.get("response") or "").strip()

    def models_url(self, api_base):
        return api_base.rstrip("/") + "/api/tags"

    def parse_models(self, parsed):
        return [m.get("name") for m in parsed.get("models", []) if m.get("name")]


class _AnthropicAdapter(_Adapter):
    """Anthropic's Messages API. Differs from the OpenAI-compatible shape in
    four ways, all of which live here and nowhere else:

      - the route is `POST /v1/messages`, not `/v1/chat/completions`
      - auth is the `x-api-key` header, not `Authorization: Bearer`
      - an `anthropic-version` header is required
      - the reply's `content` is a LIST of typed blocks, not
        `choices[0].message.content` — text blocks are concatenated and any
        non-text block (e.g. thinking) is ignored

    `max_tokens` is required by this API and has no equivalent in the other two
    formats, so it is not threaded through the shared `generate_raw` signature —
    see ANTHROPIC_MAX_TOKENS.
    """

    name = "anthropic"
    # api.anthropic.com has no meaningful unauthenticated root; probing it tells
    # us nothing, so Test connection uses the model list instead.
    supports_root_ping = False
    requires_api_key = True

    def unreachable_hint(self) -> str:
        return (
            "Not reachable. Check that the API key is valid and that this "
            "machine has internet access. Keys are created at "
            "console.anthropic.com."
        )

    def auth_headers(self, api_key: str) -> dict:
        headers = {"anthropic-version": ANTHROPIC_VERSION}
        if api_key:
            headers["x-api-key"] = api_key
        return headers

    def build_request(self, model, prompt, api_base, temperature, api_key,
                      json_mode=False):
        # The Messages API has no JSON-mode switch; parsing does the work.
        headers = {"Content-Type": "application/json"}
        headers.update(self.auth_headers(api_key))
        payload = {
            "model": model,
            "max_tokens": ANTHROPIC_MAX_TOKENS,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        return _json_request(
            api_base.rstrip("/") + "/messages", payload, headers
        )

    def extract_text(self, parsed):
        blocks = parsed.get("content") or []
        if isinstance(blocks, str):  # defensive: never seen, cheap to survive
            return blocks.strip()
        parts = [
            b.get("text") or ""
            for b in blocks
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        return "".join(parts).strip()

    def models_url(self, api_base):
        return api_base.rstrip("/") + "/models"

    def parse_models(self, parsed):
        return [m.get("id") for m in parsed.get("data", []) if m.get("id")]


_ADAPTERS: "dict[str, _Adapter]" = {
    _OpenAIAdapter.name: _OpenAIAdapter(),
    _OllamaAdapter.name: _OllamaAdapter(),
    _AnthropicAdapter.name: _AnthropicAdapter(),
}

VALID_API_FORMATS = tuple(_ADAPTERS)


def _adapter_for(api_format: str) -> _Adapter:
    """The adapter for `api_format`, defaulting to the portable format when the
    value is unrecognized (a config typo must never crash generation)."""
    adapter = _ADAPTERS.get(api_format)
    if adapter is None:
        log.warning(
            "unknown api_format %r; using %s", api_format, DEFAULT_API_FORMAT
        )
        return _ADAPTERS[DEFAULT_API_FORMAT]
    return adapter


@dataclass(frozen=True)
class ProviderConfig:
    """Resolved LLM backend settings — the single source of truth for WHICH
    server we call and HOW. Built from config by `resolve_provider`.

    `name` is the provider's config key ("local", "claude", …) and is used for
    logging and per-provider status reporting. `model` is that provider's
    default model, used when a subject/task doesn't name one of its own.
    """
    api_base: str
    api_format: str
    api_key: str
    timeout: int
    name: str = DEFAULT_PROVIDER_NAME
    model: str = ""


def _normalize_format(value) -> str:
    """Lower-cased, validated `api_format`. An unrecognized value is logged and
    treated as the default rather than crashing."""
    fmt = str(value or DEFAULT_API_FORMAT).strip().lower()
    if fmt not in VALID_API_FORMATS:
        log.warning("unknown api_format %r; using %s", fmt, DEFAULT_API_FORMAT)
        fmt = DEFAULT_API_FORMAT
    return fmt


def _default_base_for(fmt: str) -> str:
    if fmt == "anthropic":
        return ANTHROPIC_API_BASE
    if fmt == "ollama":
        return DEFAULT_OLLAMA_ENDPOINT
    return DEFAULT_API_BASE


def _timeout_from(entry: dict, fallback: int = 60) -> int:
    try:
        return int(entry.get("request_timeout_seconds", fallback))
    except (TypeError, ValueError):
        return fallback


def _is_placeholder(value: str) -> bool:
    """True for `<from Anthropic Console>`-style stand-ins in the shipped config
    and the docs, so an unedited config reads as "no key set" instead of sending
    a nonsense credential."""
    return value.startswith("<") and value.endswith(">")


def _resolve_api_key(fmt: str, entry: dict) -> str:
    """The provider's credential. `api_key` in config wins; when it is blank or
    still a placeholder, fall back to an environment variable so the key can be
    kept off disk (config.json is plainly readable in Anki's add-on config and
    is written to meta.json).

    The variable is either explicit (`api_key_env`) or the vendor default for
    formats that always need a credential — see `_ENV_KEY_BY_FORMAT`.
    """
    raw = str(entry.get("api_key") or "").strip()
    if raw and not _is_placeholder(raw):
        return raw
    env_name = (
        str(entry.get("api_key_env") or "").strip()
        or _ENV_KEY_BY_FORMAT.get(fmt, "")
    )
    if env_name:
        return os.environ.get(env_name, "").strip()
    return ""


def _provider_from_entry(name: str, entry: dict, cfg: dict) -> ProviderConfig:
    """Build one provider from a `providers:` entry. `request_timeout_seconds`
    may be overridden per provider (a cloud call and a cold local model want
    very different budgets) and otherwise inherits the top-level value."""
    entry = entry or {}
    fmt = _normalize_format(entry.get("api_format"))
    base = str(entry.get("api_base") or "").strip().rstrip("/") or _default_base_for(fmt)
    return ProviderConfig(
        api_base=base,
        api_format=fmt,
        api_key=_resolve_api_key(fmt, entry),
        timeout=_timeout_from(entry, _timeout_from(cfg)),
        name=name,
        model=str(entry.get("model") or "").strip(),
    )


def _legacy_provider(cfg: dict) -> ProviderConfig:
    """The pre-`providers:` config shape — top-level `api_base`/`api_format`/
    `api_key`, or older still a bare `ollama_endpoint`. Preserved exactly so an
    existing install keeps working with no config edit.

    Precedence for the base URL:
      1. `api_base` if set (the canonical key).
      2. else derive from the legacy `ollama_endpoint` — appending `/v1` for the
         OpenAI-compatible format.
      3. else the format-appropriate default.
    """
    fmt = _normalize_format(cfg.get("api_format"))
    base = str(cfg.get("api_base") or "").strip().rstrip("/")
    if not base:
        legacy = str(cfg.get("ollama_endpoint") or "").strip()
        if legacy:
            base = legacy.rstrip("/")
            if fmt == "openai" and not base.endswith("/v1"):
                base = base + "/v1"
        else:
            base = _default_base_for(fmt)
    return ProviderConfig(
        api_base=base,
        api_format=fmt,
        api_key=_resolve_api_key(fmt, cfg),
        timeout=_timeout_from(cfg),
        name=DEFAULT_PROVIDER_NAME,
        model=str(cfg.get("model") or "").strip(),
    )


def providers_from_config(cfg: dict) -> "dict[str, ProviderConfig]":
    """Every configured provider, keyed by its config name.

    Reads the `providers:` map. When it is absent, empty, or entirely malformed
    (a config written before multi-provider support), synthesizes the single
    legacy provider from the top-level keys — so there is ALWAYS at least one
    entry and no call site has to handle "no providers". Never raises.
    """
    raw = cfg.get("providers")
    out: "dict[str, ProviderConfig]" = {}
    if isinstance(raw, dict):
        for name, entry in raw.items():
            if not isinstance(name, str) or not name.strip():
                continue
            if not isinstance(entry, dict):
                log.warning("providers[%r] is not an object; ignoring", name)
                continue
            out[name.strip()] = _provider_from_entry(name.strip(), entry, cfg)
    if not out:
        legacy = _legacy_provider(cfg)
        return {legacy.name: legacy}
    return _apply_legacy_overrides(out, cfg)


def _apply_legacy_overrides(
    provs: "dict[str, ProviderConfig]", cfg: dict
) -> "dict[str, ProviderConfig]":
    """Fold a pre-`providers:` endpoint customization onto the default provider.

    Anki writes the WHOLE config dict to meta.json, so an install that predates
    `providers:` still carries top-level `api_base`/`api_key`/`ollama_endpoint`.
    A value left at the old shipped default carries no user intent and is
    ignored; a customized one (LM Studio's port, a cloud key) is that user's real
    backend and must survive the upgrade rather than being silently replaced by
    the shipped `providers.local`.

    Only ever touches the DEFAULT provider, and logs when it does so the user can
    move the setting into the providers block and drop the stale key.
    """
    legacy = _legacy_provider(cfg)
    changes = {}
    if legacy.api_base not in (DEFAULT_API_BASE, DEFAULT_OLLAMA_ENDPOINT):
        changes["api_base"] = legacy.api_base
        changes["api_format"] = legacy.api_format
    if legacy.api_key:
        changes["api_key"] = legacy.api_key
    if not changes:
        return provs
    name = _default_name(provs, cfg)
    log.warning(
        "config carries a customized top-level api_base/api_key from before "
        "`providers:` existed; applying it to provider %r. Move the setting "
        "into the providers block and clear the top-level key.", name,
    )
    merged = dict(provs)
    merged[name] = replace(provs[name], **changes)
    return merged


def _default_name(provs: dict, cfg: dict) -> str:
    want = str(cfg.get("default_provider") or "").strip()
    if want and want in provs:
        return want
    if want:
        log.warning("default_provider %r is not configured; ignoring", want)
    if DEFAULT_PROVIDER_NAME in provs:
        return DEFAULT_PROVIDER_NAME
    return next(iter(provs))


def default_provider_name(cfg: dict) -> str:
    """The provider used when nothing names one: `default_provider` if it points
    at a real entry, else "local", else whichever is configured first."""
    return _default_name(providers_from_config(cfg), cfg)


def resolve_provider(cfg: dict, name: Optional[str] = None) -> ProviderConfig:
    """Turn the add-on config into a `ProviderConfig` — the one place endpoint /
    format / key resolution happens, so every call site agrees.

    `name` selects a specific entry from `providers:` (e.g. the provider a
    subject or the extraction task asks for). An unknown name is logged and
    falls back to the default provider rather than failing the generation.
    Never raises.
    """
    provs = providers_from_config(cfg)
    if name:
        prov = provs.get(name)
        if prov is not None:
            return prov
        log.warning(
            "provider %r is not configured; falling back to the default", name
        )
    return provs[_default_name(provs, cfg)]


def resolve_for_subject(
    cfg: dict, subject: Optional[str]
) -> "tuple[ProviderConfig, str]":
    """Resolve `(provider, model)` for a subject / category.

    The single chokepoint every generation call site uses, so per-category
    provider selection behaves identically everywhere. The model comes from the
    subject's `models_by_subject` entry; when that entry names a provider but no
    model, the provider's own default `model` is used; failing that, the
    hardcoded default. Never raises.
    """
    from . import subjects  # local import keeps module import order trivial

    name, model = subjects.provider_and_model_for_subject(cfg, subject)
    prov = resolve_provider(cfg, name)
    return prov, (model or prov.model or subjects.DEFAULT_MODEL)


@dataclass(frozen=True)
class GenRequest:
    word: str
    model: str
    subject: Optional[str] = None
    temperature: float = 0.3
    words_per_sentence: int = 1


def _build_prompt(req: GenRequest, emphasize: bool = False) -> str:
    # Intentionally minimal for Step 1. Few-shot examples + per-language
    # variants land in Step 8.
    subj = f" ({req.subject})" if req.subject else ""
    prompt = (
        f"Write one short, natural sentence{subj} that uses the target word "
        f"\"{req.word}\" exactly once. Output ONLY the sentence, no quotes, "
        f"no translation, no commentary."
    )
    if emphasize:
        # Retry nudge: the previous attempt failed validation (the word was
        # missing). Be more forceful about including it verbatim.
        prompt += (
            f" IMPORTANT: the sentence MUST contain the exact word "
            f"\"{req.word}\". A sentence without it is invalid — include it."
        )
    return prompt


def _build_request(
    model: str,
    prompt: str,
    api_base: str,
    temperature: float,
    api_format: str,
    api_key: str,
    json_mode: bool = False,
) -> "urllib.request.Request":
    """Build the HTTP request for the selected wire format. A thin dispatcher —
    the per-format knowledge lives in the `_Adapter` subclasses above."""
    return _adapter_for(api_format).build_request(
        model, prompt, api_base, temperature, api_key, json_mode=json_mode
    )


# api_bases whose server rejected JSON mode with HTTP 400 this session. We stop
# asking them for it, so JSON mode can never turn a working setup into a
# failing one; an unsupported server costs one extra request, once.
_JSON_MODE_UNSUPPORTED: "set[str]" = set()


def reset_json_mode_memory() -> None:
    _JSON_MODE_UNSUPPORTED.clear()


# ---- cold start ----
# A self-hosted server (Ollama) loads a model into memory on its first request,
# which for a 7B model can take longer than `request_timeout_seconds`. So the
# first generation per (server, model) per session gets FIRST_REQUEST_TIMEOUT —
# but only after a PROBE_TIMEOUT check that the server answers at all (Ollama
# answers its model list while a model is loading). A dead server therefore
# fails at the probe, no slower than before. The long timeout is spent once per
# (server, model): later calls, including retries after a failed first call,
# use the normal timeout. Cloud APIs (requires_api_key) don't load models.
FIRST_REQUEST_TIMEOUT = 180
PROBE_TIMEOUT = 5
_COLD_START_DONE: "set[tuple[str, str]]" = set()


def reset_warm_state() -> None:
    _COLD_START_DONE.clear()


def _cold_start_timeout(model, endpoint, api_format, api_key, timeout) -> int:
    adapter = _adapter_for(api_format)
    key = (endpoint, model)
    if adapter.requires_api_key or key in _COLD_START_DONE:
        return timeout
    _COLD_START_DONE.add(key)
    probe = urllib.request.Request(
        adapter.models_url(endpoint), headers=adapter.auth_headers(api_key)
    )
    try:
        with urllib.request.urlopen(probe, timeout=PROBE_TIMEOUT) as resp:
            resp.read()
    except urllib.error.HTTPError:
        pass  # it answered; a 404 on the model list still means "alive"
    except Exception as e:
        # Not marked done: once the server is back, the first real call
        # still deserves the cold-start allowance.
        _COLD_START_DONE.discard(key)
        raise GenerationError(f"LLM server not reachable at {endpoint}: {e}") from e
    first = max(int(timeout), FIRST_REQUEST_TIMEOUT)
    log.info("first request to model=%s at %s this session: timeout %ss "
             "(model may need to load)", model, endpoint, first)
    return first


def _extract_text(parsed: dict, api_format: str) -> str:
    """Pull the generated text out of a parsed response for the given format.
    Raises GenerationError on an unexpected shape (so the caller's retry /
    fallback engages instead of an AttributeError escaping)."""
    try:
        return _adapter_for(api_format).extract_text(parsed)
    except Exception as e:
        raise GenerationError(
            f"unexpected response shape ({api_format}): {type(e).__name__}: {e}"
        ) from e


def generate_raw(
    model: str,
    prompt: str,
    endpoint: str,
    timeout: int = 60,
    temperature: float = 0.3,
    *,
    api_format: str = DEFAULT_API_FORMAT,
    api_key: str = "",
    json_mode: bool = False,
) -> str:
    """Call the configured LLM backend with an arbitrary prompt and return the
    raw response text. The transport primitive shared by the plain-sentence path
    (`generate_sentence`) and the structured per-strategy path (Step 8,
    `strategies.py`). `endpoint` is the resolved `api_base`; `api_format`
    selects the wire protocol (see module docstring). Raises GenerationError on
    transport failure / non-JSON / empty body.

    `json_mode=True` asks the server for a JSON-only reply (see the adapters).
    If the server rejects that with HTTP 400, the call is retried once without
    it and the endpoint is remembered as not supporting it.
    """
    timeout = _cold_start_timeout(model, endpoint, api_format, api_key, timeout)
    use_json = bool(json_mode) and endpoint not in _JSON_MODE_UNSUPPORTED
    request = _build_request(
        model, prompt, endpoint, temperature, api_format, api_key,
        json_mode=use_json,
    )
    # `timeout` is a HARD bound on the whole socket exchange (connect AND read),
    # so a stalled local model can never hang the caller indefinitely — it fails
    # fast and the caller falls back. We catch EVERYTHING and re-raise as a
    # single GenerationError: besides URLError, a read-phase timeout surfaces as
    # socket.timeout/TimeoutError (an OSError, NOT a URLError) and would
    # otherwise escape this function and the strategy layer.
    try:
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if not (use_json and e.code == 400):
                raise
            _JSON_MODE_UNSUPPORTED.add(endpoint)
            log.warning("JSON mode rejected by %s (HTTP 400); retrying without "
                        "it for this session", endpoint)
            request = _build_request(
                model, prompt, endpoint, temperature, api_format, api_key,
                json_mode=False,
            )
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                body = resp.read().decode("utf-8")
    except urllib.error.URLError as e:
        raise GenerationError(f"LLM request failed: {e}") from e
    except Exception as e:  # socket.timeout on read, decode errors, anything
        raise GenerationError(
            f"LLM request error: {type(e).__name__}: {e}"
        ) from e

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as e:
        raise GenerationError(f"LLM returned non-JSON: {body[:200]}") from e

    text = _extract_text(parsed, api_format)
    if not text:
        raise GenerationError(f"LLM returned empty response: {parsed!r}")
    return text


def generate_sentence(
    req: GenRequest,
    endpoint: str,
    timeout: int = 60,
    emphasize: bool = False,
    *,
    api_format: str = DEFAULT_API_FORMAT,
    api_key: str = "",
) -> str:
    """Generate a single plain sentence for a target word. Validation (does the
    sentence contain the target word?) is the caller's responsibility — see
    `validation.py` / `generate_validated`.
    """
    text = generate_raw(
        req.model,
        _build_prompt(req, emphasize=emphasize),
        endpoint,
        timeout=timeout,
        temperature=req.temperature,
        api_format=api_format,
        api_key=api_key,
    )
    log.info("generated %d chars for word=%r model=%s", len(text), req.word, req.model)
    return text


def generate_validated(
    req: GenRequest,
    endpoint: str,
    timeout: int = 60,
    retries: int = 2,
    *,
    api_format: str = DEFAULT_API_FORMAT,
    api_key: str = "",
) -> Optional[str]:
    """Generate a sentence and validate that it contains the target word
    (spec §5). Retry up to `retries` times (so `retries + 1` total attempts),
    emphasizing the prompt on each retry.

    Returns the first validated sentence, or None if every attempt fails
    (transport error, empty response, or validation miss). The caller is
    responsible for the fallback — this function never raises for those cases.
    """
    attempts = max(1, retries + 1)
    for i in range(attempts):
        try:
            sentence = generate_sentence(
                req, endpoint=endpoint, timeout=timeout, emphasize=(i > 0),
                api_format=api_format, api_key=api_key,
            )
        except Exception as e:  # never let a generation failure escape (spec §5)
            log.warning(
                "generation error attempt %d/%d word=%r: %s",
                i + 1, attempts, req.word, e,
            )
            continue
        if validation.contains_word(sentence, req.word):
            if i > 0:
                log.info(
                    "validation passed on retry %d word=%r", i, req.word
                )
            return sentence
        log.warning(
            "validation FAILED attempt %d/%d word=%r sentence=%r",
            i + 1, attempts, req.word, sentence,
        )
    log.warning(
        "all %d generation attempts failed validation for word=%r; "
        "caller will fall back", attempts, req.word,
    )
    return None


def ping(endpoint: str, timeout: int = 5) -> bool:
    """Return True if the server root responds. `endpoint` may be the api_base
    (a trailing `/v1` is stripped so we hit the server root)."""
    root = endpoint.rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    try:
        with urllib.request.urlopen(root + "/", timeout=timeout):
            return True
    except Exception as e:  # URLError, socket.timeout, anything — never raise
        log.warning("ping failed: %s", e)
        return False


def list_models(prov: ProviderConfig, timeout: int = 5) -> list:
    """Best-effort list of model names the backend exposes. The URL, auth, and
    response shape all come from the format's adapter (OpenAI-compatible servers
    answer `GET /v1/models` → `{"data": [{"id": ...}]}`; Ollama-native answers
    `GET /api/tags` → `{"models": [{"name": ...}]}`). Returns [] on any failure
    (server down, no such route, parse error) — never raises."""
    adapter = _adapter_for(prov.api_format)
    try:
        url = adapter.models_url(prov.api_base)
        headers = adapter.auth_headers(prov.api_key)
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            parsed = json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001 — best-effort, never raise
        log.warning("list_models failed: %s", e)
        return []
    try:
        return adapter.parse_models(parsed)
    except Exception:  # noqa: BLE001
        return []


def missing_credential(prov: ProviderConfig) -> bool:
    """True when this provider's wire format mandates an API key and none was
    resolved (neither `api_key` in config nor the environment variable it
    names). The offline half of `test_connection`'s first check, exposed so a
    caller can rule a provider out without making a request that can only 401.
    """
    return _adapter_for(prov.api_format).requires_api_key and not prov.api_key


def credential_hint(prov: ProviderConfig) -> str:
    """One line telling the user how to give `prov` a key. Shared by
    `test_connection` and the vault ingestion pre-flight so both say the same
    thing about the same problem."""
    return (
        f"No API key configured for provider {prov.name!r}. Set its `api_key` "
        f"in the add-on config, or put the key in the environment variable it "
        f"names (`api_key_env`, default ANTHROPIC_API_KEY for Claude)."
    )


def unreachable_hint(prov: ProviderConfig) -> str:
    """One line telling the user why nothing answered, phrased for this
    provider's wire format (local server down vs. cloud endpoint/key)."""
    return _adapter_for(prov.api_format).unreachable_hint()


def test_connection(prov: ProviderConfig, timeout: int = 5):
    """Probe ONE provider for the *Test connection* menu item.

    Returns `(reachable: bool, models: list[str], detail: str)`. Tries to list
    models first (proves the LLM API answers and tells the user what's pulled);
    for self-hosted formats it falls back to a root ping so "server up but no
    models / different API surface" still reports reachable. A cloud format that
    has no key short-circuits with clear guidance rather than making a request
    that can only 401. Never raises.
    """
    adapter = _adapter_for(prov.api_format)
    if missing_credential(prov):
        return False, [], credential_hint(prov)
    models = list_models(prov, timeout=timeout)
    if models:
        return True, models, f"Reachable — {len(models)} model(s) available."
    if adapter.supports_root_ping and ping(prov.api_base, timeout=timeout):
        return (
            True,
            [],
            "Server reachable, but no models were listed. Pull a model "
            "(e.g. `ollama pull qwen2.5:7b`) or check the endpoint.",
        )
    return False, [], adapter.unreachable_hint()


@dataclass(frozen=True)
class ProviderStatus:
    """One provider's *Test connection* result, for the per-provider report."""
    provider: ProviderConfig
    reachable: bool
    models: list
    detail: str

    @property
    def name(self) -> str:
        return self.provider.name


def test_all_providers(cfg: dict, timeout: int = 5) -> "list[ProviderStatus]":
    """Probe EVERY configured provider and return one status each.

    The default provider is reported first (it is the one most setups actually
    use), the rest follow in config order. Each probe is independently
    fail-safe, so one unreachable backend never hides the others' results.
    """
    provs = providers_from_config(cfg)
    default = _default_name(provs, cfg)
    order = [default] + [n for n in provs if n != default]
    out = []
    for name in order:
        prov = provs[name]
        try:
            reachable, models, detail = test_connection(prov, timeout=timeout)
        except Exception as e:  # noqa: BLE001 — a probe must never raise
            log.warning("test_connection failed for provider %r: %s", name, e)
            reachable, models, detail = False, [], f"Probe failed: {e}"
        out.append(
            ProviderStatus(
                provider=prov, reachable=reachable, models=models, detail=detail
            )
        )
    return out

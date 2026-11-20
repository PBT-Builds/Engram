"""Subject resolution + per-subject model selection (spec §7).

A subject is a parent deck (Anki decks are hierarchical with `::`):
`Japanese::Vocab::Core` belongs to subject `Japanese`. Which deck subtrees are
pipeline-enabled (and therefore which subject — and model — they map to) is
config-driven (`subjects` in config.json). Decks not under any configured
subject stay pure traditional Anki (spec §7) — `subject_of_deck` returns None.

Pure / importable: stdlib only, no Anki, no Qt, no network — fully unit-
testable in isolation (the Anki bits, like reading a card's deck name, live in
the call sites).
"""

from __future__ import annotations

from typing import Optional

from .log import get_logger

log = get_logger()

DEFAULT_MODEL = "qwen2.5:7b"

CARD_TYPE_LANGUAGE = "language"
CARD_TYPE_KNOWLEDGE = "knowledge"
VALID_CARD_TYPES = frozenset({CARD_TYPE_LANGUAGE, CARD_TYPE_KNOWLEDGE})
DEFAULT_CARD_TYPE = CARD_TYPE_LANGUAGE


def subjects_from_config(cfg: dict) -> list[str]:
    """The list of configured subject parent decks.

    Prefers the explicit `subjects` list. For back-compat with older configs
    that only had `models_by_subject`, falls back to its keys (minus
    `default`), so existing setups keep working without a config edit.
    """
    subs = cfg.get("subjects")
    if isinstance(subs, list):
        return [s for s in subs if isinstance(s, str) and s.strip()]
    models = cfg.get("models_by_subject", {}) or {}
    return [k for k in models.keys() if k != "default"]


def subject_of_deck(deck_name: str, subjects: list[str]) -> Optional[str]:
    """Return the configured subject (parent deck) that owns `deck_name`, or
    None if the deck isn't under any configured subject.

    A subject `S` owns deck `S` itself and anything nested under `S::`. When
    multiple configured subjects match (e.g. nested subjects), the longest
    (most specific) one wins.
    """
    best: Optional[str] = None
    for subj in subjects:
        if deck_name == subj or deck_name.startswith(subj + "::"):
            if best is None or len(subj) > len(best):
                best = subj
    return best


def category_for_deck(
    deck_category_map: dict, deck_name: str
) -> Optional[str]:
    """Resolve a real (Original) deck to its assigned category (Step 9, Part 1).

    `deck_category_map` maps a deck name OR a parent-deck prefix to a category
    name. A key matches if `deck_name == key` or `deck_name` is nested under it
    (`deck_name.startswith(key + "::")`). The longest (most specific) matching
    key wins. Returns None when the deck is unassigned — such decks are ignored
    entirely by auto-generation (stay pure traditional Anki).

    A "category" is just a subject name, so it feeds straight into
    `model_for_subject` / `card_type_for_subject`.
    """
    best_key: Optional[str] = None
    best_category: Optional[str] = None
    for key, category in (deck_category_map or {}).items():
        if not isinstance(key, str) or not isinstance(category, str):
            continue
        if deck_name == key or deck_name.startswith(key + "::"):
            if best_key is None or len(key) > len(best_key):
                best_key, best_category = key, category
    return best_category


def _models_entry(cfg: dict, subject: Optional[str]):
    """The raw `models_by_subject` entry for a subject, falling back to the
    `default` entry. May be a bare model string or a `{provider, model}` dict."""
    models = cfg.get("models_by_subject", {}) or {}
    if subject and subject in models:
        return models[subject]
    return models.get("default", DEFAULT_MODEL)


def provider_and_model_for_subject(
    cfg: dict, subject: Optional[str]
) -> "tuple[Optional[str], str]":
    """Resolve a subject to `(provider_name, model)`.

    `models_by_subject` accepts two shapes, and both stay valid forever:

      "Japanese": "qwen2.5:7b"                                  → local default
      "History":  {"provider": "claude", "model": "claude-…"}   → named provider

    A bare string means "whichever provider is the default" and returns None for
    the provider, which `generator.resolve_provider` reads as "use the default".
    An entry may also name a provider without a model (`{"provider": "claude"}`),
    in which case the model is left empty and the caller falls back to that
    provider's own `model` — this is how a category can say "use Claude" without
    repeating the model id.

    Malformed entries are logged and treated as "default provider, default
    model" so a bad config edit can never break card creation.
    """
    entry = _models_entry(cfg, subject)
    if isinstance(entry, str):
        return None, entry.strip()
    if isinstance(entry, dict):
        provider = str(entry.get("provider") or "").strip() or None
        return provider, str(entry.get("model") or "").strip()
    log.warning(
        "invalid models_by_subject entry for subject %r: %r; using %s",
        subject, entry, DEFAULT_MODEL,
    )
    return None, DEFAULT_MODEL


def model_for_subject(cfg: dict, subject: Optional[str]) -> str:
    """Pick the model for a subject from `models_by_subject`, falling back to
    `default` (then a hardcoded default) when the subject isn't listed.

    Returns the model name only — see `provider_and_model_for_subject` when the
    caller also needs to know WHICH backend to send it to. An entry that names a
    provider but no model resolves to the hardcoded default here, because this
    function has no provider to ask.

    NOTE: for a local backend the chosen model must be pulled (`ollama pull
    <model>`) or generation will fail and the §5 fallback kicks in.
    """
    _, model = provider_and_model_for_subject(cfg, subject)
    return model or DEFAULT_MODEL


def card_type_for_subject(cfg: dict, subject: Optional[str]) -> str:
    """The card-type strategy for a subject (spec §8): "language" | "knowledge".

    Reads `card_types_by_subject[subject]`, falling back to `default_card_type`
    (then the hardcoded default). Unknown/invalid values are logged and treated
    as the default so a typo never breaks card creation.
    """
    default = cfg.get("default_card_type", DEFAULT_CARD_TYPE)
    if default not in VALID_CARD_TYPES:
        log.warning("invalid default_card_type %r; using %s", default, DEFAULT_CARD_TYPE)
        default = DEFAULT_CARD_TYPE
    types = cfg.get("card_types_by_subject", {}) or {}
    if subject and subject in types:
        value = types[subject]
        if value in VALID_CARD_TYPES:
            return value
        log.warning(
            "invalid card_type %r for subject %r; using %s",
            value, subject, default,
        )
    return default

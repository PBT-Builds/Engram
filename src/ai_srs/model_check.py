"""Which configured models are actually installed on the server.

Answers "model X is configured but not installed: run `ollama pull X`" for
Test connection and Getting started. Only models the config will really use
are checked — the default model, every category assigned to a deck or a vault
folder, and vault extraction — so the shipped `phi4` entry for an unassigned
subject is not reported as a problem.

Pure: works on a config dict and a list of installed model names.
"""

from __future__ import annotations

from typing import Optional

from . import generator, subjects


def _norm(name: str) -> str:
    name = (name or "").strip().lower()
    return name[: -len(":latest")] if name.endswith(":latest") else name


def is_installed(model: str, installed) -> bool:
    """Ollama-style matching: case-insensitive; `phi4` == `phi4:latest`."""
    want = _norm(model)
    return any(_norm(m) == want for m in (installed or ()))


def _add(used: dict, provider: str, model: str, user: str) -> None:
    if not model:
        return
    users = used.setdefault(provider, {}).setdefault(model, [])
    if user not in users:
        users.append(user)


def models_in_use(cfg: dict) -> "dict[str, dict[str, list]]":
    """{provider name: {model: [who uses it]}} for everything the config uses."""
    default_name = generator.default_provider_name(cfg)
    used: dict = {}

    def add_subject(subject: Optional[str], label: str) -> None:
        provider, model = subjects.provider_and_model_for_subject(cfg, subject)
        prov_name = provider or default_name
        if not model:
            model = generator.resolve_provider(cfg, prov_name).model
        _add(used, prov_name, model, label)

    add_subject(None, "default")
    categories = []
    for mapping in (cfg.get("deck_category_map"), cfg.get("vault_category_map")):
        if isinstance(mapping, dict):
            for cat in mapping.values():
                if isinstance(cat, str) and cat and cat not in categories:
                    categories.append(cat)
    for cat in categories:
        add_subject(cat, cat)

    # Vault extraction.
    from . import vault_ingest

    vcfg = vault_ingest.resolve_vault_config(cfg)
    prov = generator.resolve_provider(cfg, vcfg.extraction_provider)
    model = vault_ingest.extraction_model_for(cfg, prov, None)
    if vcfg.extraction_model or prov.model:
        _add(used, prov.name, model, "vault extraction")
    return used


def missing_models(cfg: dict, provider_name: str, installed) -> "list[tuple[str, list]]":
    """[(model, users)] used with this provider but not in `installed`.
    An empty `installed` list means "unknown" (the server didn't list models)
    and reports nothing rather than everything."""
    if not installed:
        return []
    out = []
    for model, users in models_in_use(cfg).get(provider_name, {}).items():
        if not is_installed(model, installed):
            out.append((model, users))
    return out


def missing_lines(provider_name: str, api_format: str, missing) -> "list[str]":
    """User-facing lines for a provider block."""
    lines = []
    cloud = api_format == "anthropic"
    for model, users in missing:
        who = ", ".join(users)
        if cloud:
            lines.append(f"⚠ Model {model!r} is configured (used by {who}) but this "
                         f"provider doesn't offer it. Check the model name.")
        else:
            lines.append(f"⚠ Model {model!r} is configured (used by {who}) but not "
                         f"installed. Run:  ollama pull {model}")
    return lines

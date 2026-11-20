"""Obsidian vault ingestion — vault notes in, candidate flashcards out.

Engram reads an Obsidian vault as a plain folder of `.md` files (no Obsidian
plugin, no Obsidian running) and turns changed notes into *candidate* cards for
the user to review before anything is written to their collection.

Where this sits in the pipeline: vault cards are NOT the AI practice cards the
Learning → Recycled → Legacy machinery produces. Those are generated *from*
existing Anki notes. Vault cards are new SOURCE material — the thing the user
would otherwise hand-type into Anki. Once accepted and imported as ordinary
notes they are indistinguishable from hand-made ones and flow into the existing
pipeline normally when they graduate. So this module deliberately stops at
"here are candidates"; it never touches decks or the words table.

Two halves:

  1. **Scan** (`scan_vault`) — walk the vault, honour include globs, resolve each
     note's category, and skip anything whose mtime hasn't moved since the last
     ingest (tracked per file in the sidecar `vault_files` table). A weekly vault
     is therefore never re-sent to the LLM wholesale.
  2. **Extract** (`extract_candidates`) — send one note's cleaned text to the
     configured extraction provider and parse a JSON list of `{front, back}`
     back out, defensively.

Before either half runs against the network, `check_extraction_available` says
whether the configured backend can do anything at all (no key, no model, or
nothing answering). Scanning never depends on it — a dead LLM skips extraction
and nothing else, the same way a failed generation falls back to the original
card instead of breaking the review.

Pure / importable: stdlib only, no Anki and no Qt. The sidecar connection is
injected, and the only network call goes through `generator.generate_raw`, so
everything here is unit-testable against a temp directory and canned responses.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Iterator, Optional

from . import db, generator, jsonparse
from .log import get_logger

log = get_logger()

# Cost control: the hard ceiling on candidates produced from ONE note in ONE
# run. The prompt asks for at most this many AND the parser truncates, so a
# rambling model can't turn a long weekly master doc into an unreviewable pile
# (or a surprise API bill).
DEFAULT_MAX_CARDS_PER_NOTE = 15

DEFAULT_INCLUDE_GLOBS = ("**/*.md",)

# Vault subdirectories that never contain study notes. Pruned during the walk so
# we don't stat thousands of irrelevant files.
SKIP_DIRS = frozenset({".obsidian", ".trash", ".git", ".stfolder", "node_modules"})

MARKDOWN_SUFFIXES = (".md", ".markdown")


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class VaultConfig:
    """Resolved vault settings — the one place vault config is interpreted."""
    path: str
    include_globs: tuple
    category_map: dict
    extraction_provider: Optional[str]
    extraction_model: str
    max_cards_per_note: int

    @property
    def configured(self) -> bool:
        return bool(self.path)


def resolve_vault_config(cfg: dict) -> VaultConfig:
    """Turn the add-on config into a `VaultConfig`. Never raises — a malformed
    value is logged and replaced with its default so a bad config edit can't
    break the menu action."""
    path = str(cfg.get("vault_path") or "").strip()
    if path:
        path = os.path.expanduser(path)

    raw_globs = cfg.get("vault_include_globs")
    if isinstance(raw_globs, str):
        raw_globs = [raw_globs]
    if isinstance(raw_globs, list):
        globs = tuple(g for g in raw_globs if isinstance(g, str) and g.strip())
    else:
        globs = ()
    if not globs:
        globs = DEFAULT_INCLUDE_GLOBS

    raw_map = cfg.get("vault_category_map")
    category_map = raw_map if isinstance(raw_map, dict) else {}
    if raw_map is not None and not isinstance(raw_map, dict):
        log.warning("vault_category_map is not an object; ignoring")

    provider = str(cfg.get("extraction_provider") or "").strip() or None

    try:
        max_cards = int(cfg.get("max_cards_per_note", DEFAULT_MAX_CARDS_PER_NOTE))
    except (TypeError, ValueError):
        log.warning(
            "invalid max_cards_per_note %r; using %d",
            cfg.get("max_cards_per_note"), DEFAULT_MAX_CARDS_PER_NOTE,
        )
        max_cards = DEFAULT_MAX_CARDS_PER_NOTE
    if max_cards < 1:
        log.warning("max_cards_per_note %d < 1; using 1", max_cards)
        max_cards = 1

    return VaultConfig(
        path=path,
        include_globs=globs,
        category_map=category_map,
        extraction_provider=provider,
        extraction_model=str(cfg.get("extraction_model") or "").strip(),
        max_cards_per_note=max_cards,
    )


# --------------------------------------------------------------------------
# path matching
# --------------------------------------------------------------------------

def _glob_to_regex(pattern: str) -> "re.Pattern":
    """Translate a forward-slash glob into a regex.

    Deliberately NOT `fnmatch`, whose `*` also matches `/` — that would make
    `Weekly/*.md` silently include `Weekly/2024/deep/note.md`. Here `*` and `?`
    stop at a separator and only `**` crosses directories, matching what people
    expect from Obsidian / gitignore-style patterns.
    """
    out = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern.startswith("**/", i):
                out.append("(?:[^/]+/)*")   # zero or more directories
                i += 3
                continue
            if pattern.startswith("**", i):
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("".join(out) + r"\Z")


def matches_globs(rel_path: str, patterns) -> bool:
    """True if the vault-relative path matches any include glob. An empty
    pattern list means "everything" (the scan already filters to markdown)."""
    if not patterns:
        return True
    return any(_glob_to_regex(p).match(rel_path) for p in patterns)


def category_for_note(vault_category_map: dict, rel_path: str) -> Optional[str]:
    """Resolve a vault-relative path to its Engram category.

    Keys are folder prefixes (`"Weekly/Physics"`) or whole files
    (`"Inbox/Reading.md"`). A key matches when the path equals it or is nested
    under it; the longest (most specific) key wins — same rule
    `subjects.category_for_deck` uses for decks, so the two behave alike.

    None means the note is unmapped: it has no destination deck, so ingestion
    skips it, exactly as unassigned decks are ignored by auto-generation.
    """
    best_key: Optional[str] = None
    best_category: Optional[str] = None
    for key, category in (vault_category_map or {}).items():
        if not isinstance(key, str) or not isinstance(category, str):
            continue
        k = key.replace("\\", "/").strip("/")
        if not k:
            continue
        if rel_path == k or rel_path.startswith(k + "/"):
            if best_key is None or len(k) > len(best_key):
                best_key, best_category = k, category
    return best_category


# --------------------------------------------------------------------------
# scanning
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class VaultNote:
    """One vault file that a scan says is worth (re-)processing."""
    rel_path: str          # vault-relative, '/'-separated — the sidecar key
    abs_path: str
    mtime: float
    category: Optional[str]   # None = unmapped, no destination deck


def _iter_markdown_files(root: str, patterns) -> Iterator[tuple]:
    """Yield `(rel_path, abs_path)` for every markdown file under `root` that
    matches the include globs. Skips Obsidian/VCS internals."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
        )
        for filename in sorted(filenames):
            if not filename.lower().endswith(MARKDOWN_SUFFIXES):
                continue
            abs_path = os.path.join(dirpath, filename)
            rel = os.path.relpath(abs_path, root).replace(os.sep, "/")
            if matches_globs(rel, patterns):
                yield rel, abs_path


def scan_vault(vcfg: VaultConfig, conn, *, force: bool = False) -> "list[VaultNote]":
    """Every vault note that changed since it was last ingested.

    A note is returned when it has never been ingested, or its mtime is newer
    than the one recorded in the sidecar. `force=True` returns everything
    matching the globs, ignoring the ledger (a "re-import the whole vault"
    escape hatch).

    Notes with no category mapping are still returned — with `category=None` —
    so the caller can show them as skipped rather than have them vanish
    silently. Never raises: a missing vault path or an unreadable file is
    logged and yields fewer results, not an exception.
    """
    if not vcfg.configured:
        log.info("vault ingestion: no vault_path configured")
        return []
    root = vcfg.path
    if not os.path.isdir(root):
        log.warning("vault ingestion: vault_path %r is not a directory", root)
        return []

    notes = []
    skipped_unchanged = 0
    for rel, abs_path in _iter_markdown_files(root, vcfg.include_globs):
        try:
            mtime = os.path.getmtime(abs_path)
        except OSError as e:  # vanished mid-walk, permissions, …
            log.warning("vault ingestion: cannot stat %s: %s", rel, e)
            continue
        if not force:
            seen = db.last_ingested_mtime(conn, rel)
            if seen is not None and mtime <= seen:
                skipped_unchanged += 1
                continue
        notes.append(
            VaultNote(
                rel_path=rel,
                abs_path=abs_path,
                mtime=mtime,
                category=category_for_note(vcfg.category_map, rel),
            )
        )
    notes.sort(key=lambda n: n.rel_path)
    unmapped = sum(1 for n in notes if n.category is None)
    log.info(
        "vault scan: %d changed, %d unchanged, %d of the changed unmapped",
        len(notes), skipped_unchanged, unmapped,
    )
    return notes


def mapped_notes(notes) -> "list[VaultNote]":
    """Only the notes that have a destination category — the ones extraction
    can actually run on."""
    return [n for n in notes if n.category]


def mark_ingested(conn, note: VaultNote, *, cards_imported: int = 0) -> None:
    """Record a note as ingested at the mtime the SCAN observed.

    Deliberately the scan's mtime, not a fresh `getmtime`: if the user edits the
    file while a run is in flight, the later edit stays newer than the recorded
    value and is picked up next scan instead of being lost.
    """
    db.mark_vault_file_ingested(
        conn, note.rel_path, note.mtime, cards_imported=cards_imported
    )


# --------------------------------------------------------------------------
# extraction backend availability
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ExtractionAvailability:
    """Whether the configured extraction backend can actually be used, and if
    not, the single line to show the user."""
    available: bool
    reason: str          # "" when available
    provider: str
    model: str

    def __bool__(self) -> bool:
        return self.available


def extraction_model_for(cfg: dict, prov, category: Optional[str]) -> str:
    """The model extraction uses for a note in `category`, or "".

    Order: `extraction_model` → the extraction provider's own `model` → the
    category's `models_by_subject` entry → the `default` entry. A subject
    entry is used only if it routes to the extraction provider (a bare string
    means the default provider; a dict names its own), so a local model name
    is never sent to Claude or vice versa. The hardcoded last-resort model is
    deliberately not used: an empty config reports "no model" instead of
    guessing.
    """
    from . import subjects

    vcfg = resolve_vault_config(cfg)
    if vcfg.extraction_model:
        return vcfg.extraction_model
    if prov.model:
        return prov.model
    models = cfg.get("models_by_subject") or {}
    if not isinstance(models, dict):
        return ""
    default_name = generator.default_provider_name(cfg)
    for key in ([category] if category else []) + ["default"]:
        if key not in models:
            continue
        provider, model = subjects.provider_and_model_for_subject(
            cfg, key if key != "default" else None)
        if model and (provider or default_name) == prov.name:
            return model
    return ""


def preflight_extraction(cfg: dict, *, category: Optional[str] = None,
                         timeout: int = 5) -> "ExtractionAvailability":
    """The once-per-scan check (with a probe) used by the menu, plus a log
    line when extraction will be skipped — the dialog shows the reason, and
    the log must have it too for diagnosis after the fact."""
    avail = check_extraction_available(cfg, probe=True, timeout=timeout, category=category)
    if not avail.available:
        log.warning("vault extraction skipped (provider=%s model=%s category=%s): %s",
                    avail.provider, avail.model or "-", category or "-", avail.reason)
    return avail


def check_extraction_available(
    cfg: dict, *, probe: bool = True, timeout: int = 5,
    category: Optional[str] = None,
) -> ExtractionAvailability:
    """Can `extraction_provider` actually do any work right now?

    Three ways it cannot, each with its own one-line reason: no model resolved,
    a cloud format with no API key, and — only when `probe` is set — a provider
    that simply does not answer (local Ollama not running, endpoint wrong, key
    rejected).

    Called ONCE per scan, not once per note: without it a vault of 40 changed
    notes against a stopped Ollama means 40 doomed requests and 40 timeouts
    before the user learns anything. Mirrors the generation pipeline's contract
    — a dead backend degrades the AI step and nothing else. Never raises: a
    probe that blows up is reported as unavailable, never propagated.
    """
    vcfg = resolve_vault_config(cfg)
    prov = generator.resolve_provider(cfg, vcfg.extraction_provider)
    model = extraction_model_for(cfg, prov, category)

    def _no(reason: str) -> ExtractionAvailability:
        return ExtractionAvailability(False, reason, prov.name, model)

    if not model:
        return _no(
            f"Provider {prov.name!r} has no model configured — set "
            f"`extraction_model`, a `model` on that provider, or a model for "
            f"this category in Setup categories."
        )
    if generator.missing_credential(prov):
        return _no(generator.credential_hint(prov))
    if probe:
        try:
            reachable, _models, detail = generator.test_connection(
                prov, timeout=timeout
            )
        except Exception as e:  # noqa: BLE001 — a probe must never escape
            log.warning("extraction pre-flight probe failed: %s", e)
            return _no(f"Provider {prov.name!r} could not be probed: {e}")
        if not reachable:
            return _no(f"Provider {prov.name!r} is not reachable. {detail}")

    return ExtractionAvailability(True, "", prov.name, model)


# --------------------------------------------------------------------------
# markdown → plain text
# --------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"\A---\r?\n.*?\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_FENCE_RE = re.compile(r"```.*?```|~~~.*?~~~", re.DOTALL)
_EMBED_RE = re.compile(r"!\[\[[^\]]*\]\]")
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MDLINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_BLANKS_RE = re.compile(r"\n{3,}")


def _unwrap_wikilink(match: "re.Match") -> str:
    """`[[target|display]]` → `display`; `[[target]]` → `target`.

    A `#heading` suffix on a bare link is kept as-is — `[[Optics#Snell]]` reads
    fine as "Optics#Snell" and dropping it would lose the actual referent.
    """
    inner = match.group(1)
    if "|" in inner:
        return inner.split("|", 1)[1].strip()
    return inner.strip()


def strip_markdown(text: str) -> str:
    """Strip Obsidian-specific syntax, keeping the prose.

    Removes YAML frontmatter and `![[embeds]]`, unwraps `[[wikilinks]]` to their
    display text, drops image markup, and reduces `[text](url)` to `text`.
    Fenced code blocks are protected and restored verbatim — a snippet
    containing `[x](y)` is code, not a link, and study notes for programming
    subjects are full of them.

    The point is that the model never sees link syntax it might turn into a
    flashcard, and we don't pay tokens for URLs. Never raises.
    """
    if not text:
        return ""

    fences = []

    def _stash(match):
        fences.append(match.group(0))
        return f"\x00FENCE{len(fences) - 1}\x00"

    body = _FENCE_RE.sub(_stash, text)
    body = _FRONTMATTER_RE.sub("", body)
    body = _EMBED_RE.sub("", body)
    body = _WIKILINK_RE.sub(_unwrap_wikilink, body)
    body = _IMAGE_RE.sub("", body)
    body = _MDLINK_RE.sub(r"\1", body)

    for i, fence in enumerate(fences):
        body = body.replace(f"\x00FENCE{i}\x00", fence)

    return _BLANKS_RE.sub("\n\n", body).strip()


def read_note_text(note: VaultNote) -> str:
    """The note's cleaned text, or "" if it can't be read. Never raises."""
    try:
        with open(note.abs_path, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read()
    except OSError as e:
        log.warning("vault ingestion: cannot read %s: %s", note.rel_path, e)
        return ""
    return strip_markdown(raw)


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    """One proposed flashcard, pending the user's review. Not an Anki note yet."""
    front: str
    back: str
    source_path: str = ""
    category: Optional[str] = None


# Loose sanity bounds. Extraction quality is not locally verifiable (same honest
# limit as the knowledge card strategy), so validation only rejects the
# obviously-useless: an empty side, or a wall of text no one would review. The
# minimum is 1, not 2 — a single-character answer ("7", "π") is a perfectly good
# card back, and only emptiness is a real defect.
_MIN_SIDE_LEN = 1
_MAX_SIDE_LEN = 1000


def build_extraction_prompt(
    note_text: str,
    *,
    category: Optional[str] = None,
    max_cards: int = DEFAULT_MAX_CARDS_PER_NOTE,
    emphasize: bool = False,
) -> str:
    """The prompt that turns one note's prose into candidate flashcards.

    Asks for a JSON OBJECT `{"cards": [...]}` rather than a bare array, because
    the servers' JSON mode only produces objects; the parser still accepts a
    bare array from a model that ignores this. `max_cards` is stated in the prompt as well
    as enforced by the parser — telling the model the budget produces a better
    selection than silently truncating a longer list.
    """
    subject = f" about {category}" if category else ""
    prompt = (
        f"You are extracting spaced-repetition flashcards from a study note"
        f"{subject}.\n\n"
        f"Read the note below and write at most {max_cards} flashcards covering "
        "the most important, self-contained facts in it. Rules:\n"
        "- Each card must be answerable on its own, without having read the "
        "note. Never write \"according to the note\" or refer to it.\n"
        "- One fact per card. Prefer a specific question over a broad one.\n"
        "- Equations and formulas: make ONE card per equation. Ask what it "
        "calculates or relates, and put the equation on the back together with "
        "what each symbol means (e.g. \"U = mgh, where m is mass, g is the "
        "acceleration due to gravity and h is height\"). Do not make a "
        "separate card for a single symbol unless the note explains that "
        "symbol as a concept of its own.\n"
        "- Skip meta-content: to-do items, scheduling, personal notes, and "
        "anything that is not a durable fact worth memorizing.\n"
        "- If the note contains nothing worth memorizing, return an empty "
        "\"cards\" list.\n\n"
        "Respond with ONLY a JSON object (no markdown, no commentary) with one "
        "key, \"cards\": an array of objects with exactly two string keys, "
        "\"front\" (the question) and \"back\" (the answer).\n"
        'Example: {"cards": [{"front": "...", "back": "..."}, {"front": "...", "back": "..."}]}\n\n'
        "--- NOTE ---\n"
        f"{note_text}\n"
        "--- END NOTE ---"
    )
    if emphasize:
        prompt += (
            "\n\nIMPORTANT: your previous response was not valid JSON. Output "
            "the JSON object and NOTHING else — no explanation, no code fence."
        )
    return prompt


def _extract_json_array(raw: str) -> Optional[list]:
    """The list of card objects in a model response, defensively.

    Accepts the envelope the prompt asks for (`{"cards": [...]}`, required by
    JSON mode, which only returns objects), a bare array, other common
    envelopes (`flashcards`, `items`, `results`), and a lone card object.
    Code fences and surrounding prose are tolerated (see `jsonparse`).
    Returns None when nothing usable is there.
    """

    def _coerce(obj):
        if isinstance(obj, list):
            return obj
        if isinstance(obj, dict):
            for key in ("cards", "flashcards", "items", "results"):
                inner = obj.get(key)
                if isinstance(inner, list):
                    return inner
            if "front" in obj or "back" in obj:
                return [obj]
        return None

    return _coerce(jsonparse.extract_value(raw))


def _clean_side(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def parse_candidates(
    raw: str,
    *,
    max_cards: int = DEFAULT_MAX_CARDS_PER_NOTE,
    source_path: str = "",
    category: Optional[str] = None,
) -> "list[Candidate]":
    """Parse a model response into candidates, dropping anything unusable.

    Returns [] for a malformed response — which is indistinguishable from "this
    note had nothing worth carding", and both mean the same thing to the caller:
    show no candidates for this note. Individual bad entries are dropped without
    discarding the good ones. Enforces `max_cards` as a hard ceiling regardless
    of what the model returned. Never raises.
    """
    items = _extract_json_array(raw)
    if items is None:
        log.warning(
            "vault extraction: no JSON array in response for %s: %r",
            source_path or "<note>", (raw or "")[:200],
        )
        return []

    out = []
    dropped = 0
    for item in items:
        if not isinstance(item, dict):
            dropped += 1
            continue
        front = _clean_side(item.get("front"))
        back = _clean_side(item.get("back"))
        if (
            len(front) < _MIN_SIDE_LEN
            or len(back) < _MIN_SIDE_LEN
            or len(front) > _MAX_SIDE_LEN
            or len(back) > _MAX_SIDE_LEN
        ):
            dropped += 1
            continue
        out.append(
            Candidate(
                front=front, back=back,
                source_path=source_path, category=category,
            )
        )
        if len(out) >= max_cards:
            break

    if dropped:
        log.warning(
            "vault extraction: dropped %d unusable candidate(s) from %s",
            dropped, source_path or "<note>",
        )
    if len(items) > len(out) + dropped:
        log.info(
            "vault extraction: capped at max_cards_per_note=%d for %s "
            "(model returned %d)", max_cards, source_path or "<note>", len(items),
        )
    return out


def extract_candidates(
    note: VaultNote,
    cfg: dict,
    *,
    note_text: Optional[str] = None,
    retries: int = 1,
) -> "list[Candidate]":
    """Extract candidate cards from one vault note.

    Resolves the extraction provider/model from config (`extraction_provider`,
    `extraction_model`), sends the note's cleaned text, and parses the reply.
    Retries once by default with an emphasized prompt when the response isn't
    parseable — the same retry-then-give-up shape the card strategies use.

    Returns [] on any failure. NEVER raises: an unreachable provider, a bad key,
    a rate limit, or garbage output all degrade to "no candidates from this
    note", leaving the user's collection untouched — the vault equivalent of the
    fallback-to-original-card guarantee. Callers that want to TELL the user why
    they got nothing should call `check_extraction_available` once before the
    loop; from in here, "backend is dead" and "nothing worth carding" are the
    same empty list.
    """
    vcfg = resolve_vault_config(cfg)
    text = read_note_text(note) if note_text is None else note_text
    if not text.strip():
        log.info("vault extraction: %s is empty after cleanup", note.rel_path)
        return []

    # Offline pre-flight only (probe=False): the caller runs the probing check
    # once per scan, so re-probing here would cost one round trip per note.
    # This still catches the two config-only failures cheaply.
    avail = check_extraction_available(cfg, probe=False, category=note.category)
    if not avail.available:
        log.warning("vault extraction skipped for %s: %s", note.rel_path, avail.reason)
        return []

    prov = generator.resolve_provider(cfg, vcfg.extraction_provider)
    model = avail.model

    attempts = max(1, retries + 1)
    for i in range(attempts):
        prompt = build_extraction_prompt(
            text,
            category=note.category,
            max_cards=vcfg.max_cards_per_note,
            emphasize=(i > 0),
        )
        try:
            raw = generator.generate_raw(
                model, prompt, prov.api_base,
                timeout=prov.timeout,
                temperature=float(cfg.get("temperature", 0.3)),
                api_format=prov.api_format,
                api_key=prov.api_key,
                json_mode=True,
            )
        except Exception as e:  # noqa: BLE001 — transport must never escape
            log.warning(
                "vault extraction: attempt %d/%d failed for %s: %s",
                i + 1, attempts, note.rel_path, e,
            )
            continue
        candidates = parse_candidates(
            raw,
            max_cards=vcfg.max_cards_per_note,
            source_path=note.rel_path,
            category=note.category,
        )
        if candidates:
            log.info(
                "vault extraction: %d candidate(s) from %s via provider=%s model=%s",
                len(candidates), note.rel_path, prov.name, model,
            )
            return candidates
        # An empty result is legitimate ("nothing worth carding here"), so only
        # retry while attempts remain — and accept the empty answer at the end.
        log.info(
            "vault extraction: no usable candidates on attempt %d/%d for %s",
            i + 1, attempts, note.rel_path,
        )
    return []

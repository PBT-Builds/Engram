"""Per-subject card-type strategies (spec §8).

Two explicit, NON-interleaved strategies behind a dispatcher keyed on a
subject's `card_type`:

  - LANGUAGE (Strategy A): new-vocab cards. Structured generation of an example
    sentence + its English translation + the word's English gloss. Strict
    validation: the sentence must contain the target word (reuses the Step-6
    script-aware `validation.contains_word`), and translation/gloss must be
    non-empty.

  - KNOWLEDGE (Strategy B): history/chem/physics/etc. Given the ORIGINAL card's
    question + answer, generate a NEW question/answer that tests the SAME
    concept, rephrased (different angle / direction / direct application) — not
    new material, not a verbatim copy. Validation is necessarily LOOSE: we
    cannot verify conceptual correctness with a local model (see the honest
    note on KnowledgeStrategy.parse_and_validate). The fallback is the safety
    net.

Pure / importable: stdlib + JSON only, no Anki, no Qt, no network. The Anki
reads (original note's Q/A) happen in the call site and arrive here as plain
strings on `GenContext`. The model name and HTTP transport are injected.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional

from . import jsonparse, validation
from .generator import generate_raw
from .log import get_logger

log = get_logger()

CARD_TYPE_LANGUAGE = "language"
CARD_TYPE_KNOWLEDGE = "knowledge"

# A visible separator between the two halves of a Front/Back field. HTML so it
# renders as a line break in Anki; harmless plain text elsewhere.
_SEP = "<br><br>"

# Loose length bounds for knowledge Q/A sanity checks (characters).
_KNOWLEDGE_MIN_LEN = 3
_KNOWLEDGE_MAX_LEN = 600


@dataclass
class GenContext:
    """Everything a strategy needs, already extracted from Anki (so strategies
    stay Anki-free). `source_front`/`source_back` are the ORIGINAL card's
    Front/Back (used as knowledge input and as fallback content)."""
    word: str
    subject: Optional[str] = None
    source_front: Optional[str] = None
    source_back: Optional[str] = None


@dataclass
class CardContent:
    """The resolved Front/Back for a generated AI card."""
    front: str
    back: str


# ---------------- defensive JSON parsing ----------------

def _extract_json(raw: str) -> Optional[dict]:
    """Parse a JSON object out of a model response, defensively: code fences
    stripped, the first object that decodes wins (see `jsonparse`). Returns a
    dict or None."""
    return jsonparse.extract_object(raw)


def _clean(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _normalize_cmp(s: str) -> str:
    """Whitespace/case-normalized form for the verbatim-copy check."""
    return re.sub(r"\s+", " ", (s or "")).strip().casefold()


# ---------------- Strategy A: LANGUAGE ----------------

class LanguageStrategy:
    name = CARD_TYPE_LANGUAGE

    def build_prompt(self, ctx: GenContext, emphasize: bool = False) -> str:
        lang = ctx.subject or "the target language"
        prompt = (
            f"You are creating a vocabulary flashcard for the word "
            f"\"{ctx.word}\" in {lang}. Respond with ONLY a JSON object (no "
            "markdown, no commentary) with exactly these keys: "
            "\"sentence\" — a short, natural example sentence written entirely "
            f"in {lang} (not in any other language, even if the word is also "
            f"used in one) that uses the word \"{ctx.word}\"; \"translation\" — the "
            "English translation of that sentence; \"gloss\" — the English "
            "meaning of the word itself. "
            'Example: {"sentence": "...", "translation": "...", "gloss": "..."}'
        )
        if emphasize:
            prompt += (
                f" IMPORTANT: \"sentence\" MUST contain the exact word "
                f"\"{ctx.word}\", and \"translation\" and \"gloss\" must be "
                "non-empty. Output JSON only."
            )
        return prompt

    def parse_and_validate(
        self, raw: str, ctx: GenContext
    ) -> Optional[CardContent]:
        obj = _extract_json(raw)
        if obj is None:
            log.warning("language: unparseable JSON response: %r", raw[:200])
            return None
        sentence = _clean(obj.get("sentence"))
        translation = _clean(obj.get("translation"))
        gloss = _clean(obj.get("gloss"))
        if not sentence or not translation or not gloss:
            log.warning(
                "language: missing field(s) (sentence/translation/gloss empty)"
            )
            return None
        # Strict (reuses Step-6 script-aware primitive): the example sentence
        # must actually contain the target word.
        if not validation.contains_word(sentence, ctx.word):
            log.warning(
                "language: sentence missing target word %r: %r",
                ctx.word, sentence,
            )
            return None
        # Translation and gloss are for an English reader. A local model
        # sometimes writes them in Chinese for a Japanese sentence (measured
        # 1-3 in 60); reject so the retry / fallback engages.
        if not validation.mostly_latin(translation) or not validation.mostly_latin(gloss):
            log.warning(
                "language: translation/gloss not in English: %r / %r",
                translation, gloss,
            )
            return None
        front = f"{ctx.word}{_SEP}{sentence}"
        back = f"{translation}{_SEP}<i>{ctx.word}</i>: {gloss}"
        return CardContent(front=front, back=back)

    def fallback(self, ctx: GenContext) -> CardContent:
        # Prefer the original card's content; else a clear placeholder.
        if ctx.source_front:
            return CardContent(
                front=ctx.source_front, back=ctx.source_back or ""
            )
        return CardContent(
            front=ctx.word,
            back=f"⚠️ AI card unavailable for “{ctx.word}”",
        )


# ---------------- Strategy B: KNOWLEDGE ----------------

class KnowledgeStrategy:
    name = CARD_TYPE_KNOWLEDGE

    def build_prompt(self, ctx: GenContext, emphasize: bool = False) -> str:
        question = ctx.source_front or ""
        answer = ctx.source_back or ""
        prompt = (
            "You are creating a study flashcard that tests the SAME underlying "
            "concept as an existing card, but phrased differently — asked from "
            "another angle, from the other direction, or as a direct "
            "application of the same fact. Do NOT introduce new material the "
            "original did not cover, and do NOT copy the original wording "
            "verbatim.\n"
            f"Original question: {question}\n"
            f"Original answer: {answer}\n"
            "Respond with ONLY a JSON object (no markdown, no commentary) with "
            "keys \"question\" and \"answer\". "
            'Example: {"question": "...", "answer": "..."}'
        )
        if emphasize:
            prompt += (
                " IMPORTANT: rephrase — the new question must NOT be identical "
                "to the original, and both fields must be non-empty. Output "
                "JSON only."
            )
        return prompt

    def parse_and_validate(
        self, raw: str, ctx: GenContext
    ) -> Optional[CardContent]:
        # HONEST NOTE: these are CHEAP SANITY CHECKS ONLY. We cannot verify with
        # a local model that the new Q/A is conceptually correct or that it
        # really tests the same concept — semantic correctness is NOT
        # guaranteed here. The retry-then-fallback (to the original Q/A) is the
        # actual safety net; this just rejects obviously-broken output.
        obj = _extract_json(raw)
        if obj is None:
            log.warning("knowledge: unparseable JSON response: %r", raw[:200])
            return None
        question = _clean(obj.get("question"))
        answer = _clean(obj.get("answer"))
        if not question or not answer:
            log.warning("knowledge: empty question/answer")
            return None
        if not (_KNOWLEDGE_MIN_LEN <= len(question) <= _KNOWLEDGE_MAX_LEN):
            log.warning("knowledge: question length out of bounds (%d)", len(question))
            return None
        if not (_KNOWLEDGE_MIN_LEN <= len(answer) <= _KNOWLEDGE_MAX_LEN):
            log.warning("knowledge: answer length out of bounds (%d)", len(answer))
            return None
        # Reject pure copies (character-identical after whitespace/case
        # normalization). This does NOT catch paraphrase-level copying.
        if _normalize_cmp(question) == _normalize_cmp(ctx.source_front or ""):
            log.warning("knowledge: question is a verbatim copy of the original")
            return None
        return CardContent(front=question, back=answer)

    def fallback(self, ctx: GenContext) -> CardContent:
        # Fall back to the ORIGINAL question/answer unchanged.
        if ctx.source_front:
            return CardContent(
                front=ctx.source_front, back=ctx.source_back or ""
            )
        return CardContent(
            front=f"⚠️ AI card unavailable for “{ctx.word}”", back=""
        )


# ---------------- dispatcher + shared retry loop ----------------

_STRATEGIES = {
    CARD_TYPE_LANGUAGE: LanguageStrategy(),
    CARD_TYPE_KNOWLEDGE: KnowledgeStrategy(),
}


def _strategy_for(card_type: Optional[str]):
    strat = _STRATEGIES.get(card_type)
    if strat is None:
        log.warning(
            "unknown card_type %r; defaulting to %s", card_type, CARD_TYPE_LANGUAGE
        )
        return _STRATEGIES[CARD_TYPE_LANGUAGE]
    return strat


def generate_card(
    card_type: Optional[str],
    ctx: GenContext,
    *,
    model: str,
    endpoint: str,
    timeout: int = 60,
    temperature: float = 0.3,
    retries: int = 2,
    api_format: str = "openai",
    api_key: str = "",
) -> Optional[CardContent]:
    """Generate + validate a card via the strategy for `card_type`, retrying up
    to `retries` (emphasized prompt on retry). Returns CardContent or None when
    every attempt fails (caller falls back via `fallback_card`). Never raises
    for generation/validation failures. `endpoint` is the resolved api_base;
    `api_format`/`api_key` select the backend (see `generator`).
    """
    strat = _strategy_for(card_type)
    attempts = max(1, retries + 1)
    for i in range(attempts):
        prompt = strat.build_prompt(ctx, emphasize=(i > 0))
        # Generation AND parsing/validation are wrapped together: a transport
        # error, timeout, or a crash inside parse_and_validate must never escape
        # this function — it falls through to another attempt and ultimately to
        # the caller's fallback (spec §5/§8). generate_card NEVER raises.
        try:
            raw = generate_raw(
                model, prompt, endpoint, timeout=timeout,
                temperature=temperature, api_format=api_format, api_key=api_key,
                json_mode=True,
            )
            content = strat.parse_and_validate(raw, ctx)
        except Exception as e:
            log.warning(
                "%s gen/parse error attempt %d/%d word=%r: %s",
                strat.name, i + 1, attempts, ctx.word, e,
            )
            continue
        if content is not None:
            if i > 0:
                log.info("%s validated on retry %d word=%r", strat.name, i, ctx.word)
            return content
        log.warning(
            "%s validation failed attempt %d/%d word=%r",
            strat.name, i + 1, attempts, ctx.word,
        )
    log.warning(
        "%s: all %d attempts failed for word=%r; caller will fall back",
        strat.name, attempts, ctx.word,
    )
    return None


def fallback_card(card_type: Optional[str], ctx: GenContext) -> CardContent:
    """The strategy-appropriate fallback content (spec §8): language falls back
    to the original note content / placeholder; knowledge falls back to the
    original question/answer."""
    return _strategy_for(card_type).fallback(ctx)

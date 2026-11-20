"""Generation validation (spec §5).

Verify that a generated sentence actually contains the target word before it's
shown on a card. Weak local models often drop the target word, return a near-
empty string, or emit the word only in a conjugated/compound form — naive
lowercased substring matching both misses real hits (conjugation) and is the
wrong tool across scripts.

Stdlib only (no morphological analyzer / no new deps). Script is detected
per-call from the target word via Unicode ranges, so we don't hardcode a
language. Matching is necessarily HEURISTIC — see the per-script notes below.
The retry+fallback layer in the generation call sites is what covers the
inevitable misses (a missed match just means a retry, then a safe fallback,
never a broken card).

This module is a pure predicate library: no Anki, no Qt, no network — fully
unit-testable in isolation.
"""

from __future__ import annotations

import re
import unicodedata

# --- Unicode ranges for script detection (checked on the NFKC'd word) ---
_HIRAGANA = (0x3040, 0x309F)
_KATAKANA = (0x30A0, 0x30FF)
_CJK = (0x4E00, 0x9FFF)
_CJK_EXT_A = (0x3400, 0x4DBF)

# Common Latin (German/English/…) inflectional endings, longest first, used to
# derive a crude stem for stem-aware matching. Deliberately small & heuristic.
_LATIN_SUFFIXES = (
    "ungen", "ung", "ende", "end", "ten", "test", "est", "ing", "ed",
    "en", "er", "es", "em", "st", "te", "ly", "s", "e", "n", "t",
)


def _norm(s: str) -> str:
    """NFKC-normalize (folds compatibility forms, half/full-width, etc.) and
    casefold (aggressive, accent-preserving lowercasing)."""
    return unicodedata.normalize("NFKC", s or "").casefold()


def _has_japanese(s: str) -> bool:
    for ch in s:
        o = ord(ch)
        if (_HIRAGANA[0] <= o <= _HIRAGANA[1]
                or _KATAKANA[0] <= o <= _KATAKANA[1]
                or _CJK[0] <= o <= _CJK[1]
                or _CJK_EXT_A[0] <= o <= _CJK_EXT_A[1]):
            return True
    return False


def _has_latin(s: str) -> bool:
    for ch in s:
        if ch.isalpha():
            try:
                if unicodedata.name(ch).startswith("LATIN"):
                    return True
            except ValueError:  # unnamed char
                continue
    return False


def _match_japanese(sentence_n: str, word_n: str) -> bool:
    """Heuristic JA match. Without a morphological analyzer we cannot resolve
    readings (kana<->kanji) or reliably lemmatize, so we:
      1. accept an exact surface-form substring, and
      2. accept the word with up to its last 1–2 chars dropped (okurigana /
         light conjugation, e.g. 食べる -> 食べ), requiring a stem of >= 2 chars.
    Misses (e.g. 飲む in 飲んで, or a kanji word written in kana) are expected and
    are caught by the retry + fallback layer, not here.
    """
    if word_n in sentence_n:
        return True
    n = len(word_n)
    if n >= 3:
        for drop in (1, 2):
            stem = word_n[:n - drop]
            if len(stem) >= 2 and stem in sentence_n:
                return True
    return False


def _latin_stem(w: str) -> str:
    for suf in _LATIN_SUFFIXES:
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            return w[:-len(suf)]
    return w


def _match_latin(sentence_n: str, word_n: str) -> bool:
    """Case-insensitive, accent-aware (via NFKC), word-boundary aware Latin
    match. Short words must match as a whole word so they don't match inside a
    larger word; longer words also match a stem + suffix so conjugations /
    plurals are accepted in both directions (heuristic).
    """
    # `\w`-based boundaries are Unicode-aware for `str` patterns, so accented
    # letters count as word chars.
    whole = r"(?<!\w)" + re.escape(word_n) + r"(?!\w)"
    if re.search(whole, sentence_n):
        return True
    stem = _latin_stem(word_n)
    if len(stem) >= 4:
        stemmed = r"(?<!\w)" + re.escape(stem) + r"\w*"
        if re.search(stemmed, sentence_n):
            return True
    return False


def contains_word(sentence: str, word: str) -> bool:
    """Return True if `sentence` plausibly contains the target `word`,
    script-aware. Empty word -> False.
    """
    s = _norm(sentence)
    w = _norm(word)
    if not w:
        return False
    if _has_japanese(w):
        return _match_japanese(s, w)
    if _has_latin(w):
        return _match_latin(s, w)
    # Other scripts (Cyrillic, etc.): normalized substring is the safe default.
    return w in s


def mostly_latin(text: str) -> bool:
    """True if at least half of the letters in `text` are Latin script.

    Used to check that a language card's translation and gloss are English:
    a local model sometimes "translates" a Japanese sentence into Chinese.
    Quoting the target word inside an English translation ("a dog (犬)")
    still passes. Empty / letter-less text -> False.
    """
    import unicodedata

    letters = latin = 0
    for ch in text or "":
        if not unicodedata.category(ch).startswith("L"):
            continue
        letters += 1
        if unicodedata.name(ch, "").startswith("LATIN"):
            latin += 1
    return letters > 0 and latin * 2 >= letters

"""The language card's translation and gloss must be English.

Measured in Phase 2: qwen2.5:7b sometimes writes the "translation" of a
Japanese sentence in Chinese (3/60 runs before JSON mode, 1/60 after), e.g.
`我在公园里看到了正在散步的狗。`. The card is then useless to an English
speaker. Such a reply now fails validation, so the retry (and ultimately the
fallback) engages. A translation that merely quotes the word in its own
script — "The cat (猫) is sleeping." — is still fine.

Standalone: `python tests/test_translation_language.py`.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ai_srs import strategies, validation  # noqa: E402

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


CTX = strategies.GenContext(word="犬", subject="Japanese", source_front="犬", source_back="dog")


def _parse(translation, gloss="dog"):
    raw = json.dumps({"sentence": "公園で犬を見ました。", "translation": translation, "gloss": gloss},
                     ensure_ascii=False)
    return strategies.LanguageStrategy().parse_and_validate(raw, CTX)


def test_english_translation_passes():
    check("English translation accepted", _parse("I saw a dog in the park.") is not None)


def test_chinese_translation_rejected():
    check("Chinese translation rejected", _parse("我在公园里看到了正在散步的狗。") is None)


def test_japanese_translation_rejected():
    check("untranslated (Japanese) translation rejected", _parse("公園で犬を見ました。") is None)


def test_korean_translation_rejected():
    check("Hangul translation rejected", _parse("나는 공원에서 개를 봤다.") is None)


def test_quoting_the_word_is_fine():
    check("English with the word quoted is accepted",
          _parse("I saw a dog (犬) in the park.") is not None)


def test_gloss_must_be_english_too():
    check("CJK gloss rejected", _parse("I saw a dog.", gloss="狗") is None)
    check("gloss quoting the word accepted", _parse("I saw a dog.", gloss="犬 — dog") is not None)


def test_mostly_english_helper():
    check("helper: plain English", validation.mostly_latin("The dog has come home."))
    check("helper: accents are fine", validation.mostly_latin("Café crème, s'il vous plaît."))
    check("helper: Chinese", not validation.mostly_latin("我在公园里看到一只狗。"))
    check("helper: empty is not English", not validation.mostly_latin(""))


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

"""Tolerating broken JSON from local models (Phase 2, item 2).

In Phase 1, qwen2.5:7b returned unparseable JSON on ~1 in 9 generations
(e.g. `"gloss": "犬": dog`). Two defences, both tested here:

  1. Ask the server for JSON: OpenAI-compatible `response_format: json_object`,
     Ollama-native `format: "json"`. Anthropic has no such switch and is left
     alone. A server that rejects the field (HTTP 400) is retried once without
     it and remembered for the session, so JSON mode can never make a working
     setup fail.
  2. Parse forgivingly: strip code fences, then take the first JSON value that
     decodes, instead of a greedy regex over the whole reply.

Standalone: `python tests/test_json_robustness.py` (no network — urlopen is
monkeypatched).
"""

import io
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ai_srs import generator, jsonparse, strategies, vault_ingest  # noqa: E402

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


# ---------------- forgiving parser ----------------

def test_plain_object():
    check("plain object", jsonparse.extract_object('{"a": 1}') == {"a": 1})


def test_code_fence():
    raw = 'Here you go:\n```json\n{"sentence": "猫がいる。", "gloss": "cat"}\n```\nEnjoy!'
    check("fenced object", jsonparse.extract_object(raw) == {"sentence": "猫がいる。", "gloss": "cat"})


def test_fence_without_language_tag():
    check("bare ``` fence", jsonparse.extract_object('```\n{"a": 2}\n```') == {"a": 2})


def test_prose_with_braces_after_object():
    """The old greedy `\\{.*\\}` regex spanned to the LAST brace and failed."""
    raw = '{"question": "Q?", "answer": "A"} Note: {this} is just prose.'
    check("first object wins over trailing braces",
          jsonparse.extract_object(raw) == {"question": "Q?", "answer": "A"})


def test_two_objects_takes_first_valid():
    raw = 'first {"broken": } then {"ok": true}'
    check("skips an invalid candidate, takes the next", jsonparse.extract_object(raw) == {"ok": True})


def test_nested_object():
    raw = 'x {"cards": [{"front": "a", "back": "b"}]} y'
    check("nested object intact",
          jsonparse.extract_object(raw) == {"cards": [{"front": "a", "back": "b"}]})


def test_garbage_is_none():
    for raw in ("", None, "no json here", '"gloss": "犬": dog', "{unterminated"):
        check(f"garbage {raw!r} -> None", jsonparse.extract_object(raw) is None)


def test_array_and_any():
    check("extract_array", jsonparse.extract_array('prose [1, 2] more') == [1, 2])
    check("extract_value takes whichever comes first",
          jsonparse.extract_value('x [{"front": "a"}] {"b": 1}') == [{"front": "a"}])
    check("extract_object skips a leading array",
          jsonparse.extract_object('[1] {"b": 1}') == {"b": 1})


def test_strategies_use_forgiving_parser():
    raw = '```json\n{"question": "Q?", "answer": "A"}\n``` and {junk}'
    check("strategies._extract_json uses it",
          strategies._extract_json(raw) == {"question": "Q?", "answer": "A"})


def test_vault_array_parser_uses_it():
    raw = 'Sure!\n```json\n{"cards": [{"front": "F", "back": "B"}]}\n```\n(1 card) [end]'
    check("vault parser: envelope inside a fence with trailing brackets",
          vault_ingest._extract_json_array(raw) == [{"front": "F", "back": "B"}])


# ---------------- JSON mode on the wire ----------------

def _payload(req):
    return json.loads(req.data.decode("utf-8"))


def test_openai_json_mode_field():
    req = generator._build_request("m", "p", "http://h/v1", 0.3, "openai", "", json_mode=True)
    check("openai: response_format json_object",
          _payload(req).get("response_format") == {"type": "json_object"}, _payload(req))
    req = generator._build_request("m", "p", "http://h/v1", 0.3, "openai", "")
    check("openai: no response_format by default", "response_format" not in _payload(req))


def test_ollama_json_mode_field():
    req = generator._build_request("m", "p", "http://h", 0.3, "ollama", "", json_mode=True)
    check("ollama native: format json", _payload(req).get("format") == "json", _payload(req))


def test_anthropic_ignores_json_mode():
    req = generator._build_request("m", "p", "https://api.anthropic.com/v1", 0.3,
                                   "anthropic", "k", json_mode=True)
    p = _payload(req)
    check("anthropic: no json-mode field sent",
          "response_format" not in p and "format" not in p, p)


class _Resp:
    def __init__(self, body):
        self._b = body.encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _openai_body(text):
    return json.dumps({"choices": [{"message": {"content": text}}]})


def _with_urlopen(fake):
    """Install `fake` as urlopen. The cold-start reachability probe (a GET of
    the model list, see generator._cold_start_timeout) is answered here, so
    `fake` only ever sees generation requests."""
    orig = urllib.request.urlopen

    def wrapper(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        if getattr(req, "data", None) is None and (
                url.endswith("/models") or url.endswith("/api/tags")):
            return _ProbeResp()
        return fake(req, timeout=timeout)

    urllib.request.urlopen = wrapper
    return lambda: setattr(urllib.request, "urlopen", orig)


class _ProbeResp:
    def read(self):
        return b'{"data": []}'

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_http_400_retries_without_json_mode_and_remembers():
    generator.reset_json_mode_memory()
    sent = []

    def fake(req, timeout=None):
        p = _payload(req)
        sent.append(p)
        if "response_format" in p:
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {},
                                         io.BytesIO(b'{"error": "response_format not supported"}'))
        return _Resp(_openai_body('{"a": 1}'))

    restore = _with_urlopen(fake)
    try:
        out = generator.generate_raw("m", "p", "http://lmstudio/v1", json_mode=True)
        out2 = generator.generate_raw("m", "p", "http://lmstudio/v1", json_mode=True)
    finally:
        restore()
    check("400 with json mode -> retried without it, succeeded", out == '{"a": 1}', out)
    check("first call: 2 requests (with, then without)", len(sent) == 3
          and "response_format" in sent[0] and "response_format" not in sent[1], sent)
    check("second call: json mode remembered as unsupported (1 request, no field)",
          "response_format" not in sent[2])


def test_http_400_without_json_mode_is_not_retried():
    generator.reset_json_mode_memory()
    calls = []

    def fake(req, timeout=None):
        calls.append(1)
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(b"bad"))

    restore = _with_urlopen(fake)
    try:
        try:
            generator.generate_raw("m", "p", "http://h/v1")
            raised = False
        except generator.GenerationError:
            raised = True
    finally:
        restore()
    check("plain 400 still a GenerationError", raised)
    check("plain 400 not retried", len(calls) == 1, calls)


def test_other_errors_not_retried_without_json_mode():
    generator.reset_json_mode_memory()
    calls = []

    def fake(req, timeout=None):
        calls.append(_payload(req))
        raise urllib.error.HTTPError(req.full_url, 500, "boom", {}, io.BytesIO(b"x"))

    restore = _with_urlopen(fake)
    try:
        try:
            generator.generate_raw("m", "p", "http://h2/v1", json_mode=True)
        except generator.GenerationError:
            pass
    finally:
        restore()
    check("500 with json mode -> one request only", len(calls) == 1, calls)


# ---------------- callers ask for JSON mode ----------------

def test_generate_card_requests_json_mode():
    seen = []

    def spy(model, prompt, endpoint, timeout=60, temperature=0.3, **kw):
        seen.append(kw.get("json_mode"))
        return '{"question": "New Q?", "answer": "Answer"}'

    orig = strategies.generate_raw
    strategies.generate_raw = spy
    try:
        strategies.generate_card(
            "knowledge",
            strategies.GenContext(word="w", source_front="Old Q?", source_back="A"),
            model="m", endpoint="x")
    finally:
        strategies.generate_raw = orig
    check("generate_card asks for JSON mode", seen == [True], seen)


def test_extraction_prompt_asks_for_envelope_object():
    prompt = vault_ingest.build_extraction_prompt("text", category="Physics")
    check("extraction prompt asks for a {\"cards\": [...]} object",
          '"cards"' in prompt and "JSON object" in prompt, prompt[-400:])


def test_extract_candidates_requests_json_mode():
    seen = []

    def fake(req, timeout=None):
        seen.append(_payload(req))
        return _Resp(_openai_body('{"cards": [{"front": "F?", "back": "B"}]}'))

    cfg = {"providers": {"local": {"api_format": "openai", "api_base": "http://h/v1",
                                   "model": "m"}},
           "extraction_provider": "local", "extraction_model": "m", "vault_path": "."}
    note = vault_ingest.VaultNote(rel_path="a.md", abs_path="", mtime=0.0, category="Physics")
    generator.reset_json_mode_memory()
    restore = _with_urlopen(fake)
    try:
        out = vault_ingest.extract_candidates(note, cfg, note_text="Some physics.")
    finally:
        restore()
    check("extraction parsed the envelope", len(out) == 1, out)
    check("extraction asked for JSON mode",
          seen and seen[0].get("response_format") == {"type": "json_object"}, seen)


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

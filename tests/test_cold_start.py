"""First request to a cold local model (Phase 2, item 3).

Loading a 7B model can take longer than `request_timeout_seconds` (60 s), so in
Phase 1 the first card of a session timed out once. Rule under test:

  - The first generation per (server, model) in a session gets a longer timeout
    (generator.FIRST_REQUEST_TIMEOUT), but only after a quick reachability
    probe (PROBE_TIMEOUT) of the server's model list succeeds.
  - Probe fails -> GenerationError at once, no generation request: a dead
    backend fails no slower than before.
  - The long timeout is spent at most ONCE per (server, model): after the first
    attempt, success or not, normal timeouts apply (retries can't stack it).
  - Cloud APIs (Anthropic) don't load models: no probe, no long timeout.

Standalone: `python tests/test_cold_start.py` (urlopen is monkeypatched).
"""

import json
import os
import socket
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ai_srs import generator  # noqa: E402

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


class _Resp:
    def __init__(self, body):
        self._b = body.encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _ok_body():
    return json.dumps({"choices": [{"message": {"content": "hello"}}]})


class _Net:
    """Fake network: records (url, timeout); probe and generate behaviour set per test."""

    def __init__(self, probe_ok=True, gen=lambda n: _Resp(_ok_body())):
        self.calls = []
        self.probe_ok = probe_ok
        self.gen = gen
        self.gens = 0

    def __call__(self, req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        self.calls.append((url, timeout))
        if url.endswith("/models") or url.endswith("/api/tags"):
            if not self.probe_ok:
                raise urllib.error.URLError("refused")
            return _Resp(json.dumps({"data": [], "models": []}))
        self.gens += 1
        return self.gen(self.gens)


def _run(net, fn):
    orig = urllib.request.urlopen
    urllib.request.urlopen = net
    try:
        return fn()
    finally:
        urllib.request.urlopen = orig


def _gen(model="m", base="http://h/v1", fmt="openai", timeout=60):
    return generator.generate_raw(model, "p", base, timeout=timeout, api_format=fmt)


def test_first_call_probes_then_uses_long_timeout():
    generator.reset_warm_state()
    net = _Net()
    _run(net, _gen)
    check("first call: probe first", net.calls[0][0] == "http://h/v1/models", net.calls)
    check("probe uses the short timeout", net.calls[0][1] == generator.PROBE_TIMEOUT, net.calls)
    check("generation uses the long first-call timeout",
          net.calls[1][1] == generator.FIRST_REQUEST_TIMEOUT, net.calls)


def test_second_call_is_normal():
    generator.reset_warm_state()
    net = _Net()
    _run(net, _gen)
    net.calls.clear()
    _run(net, _gen)
    check("second call: no probe, normal timeout", net.calls == [("http://h/v1/chat/completions", 60)],
          net.calls)


def test_dead_backend_fails_fast_without_generating():
    generator.reset_warm_state()
    net = _Net(probe_ok=False)
    try:
        _run(net, _gen)
        raised = False
    except generator.GenerationError:
        raised = True
    check("probe failure -> GenerationError", raised)
    check("no generation request sent", net.gens == 0, net.calls)


def test_long_timeout_spent_once_even_on_failure():
    generator.reset_warm_state()

    def gen(n):
        raise socket.timeout("timed out")

    net = _Net(gen=gen)
    for _ in range(3):  # like generate_card's 3 attempts
        try:
            _run(net, _gen)
        except generator.GenerationError:
            pass
    timeouts = [t for (u, t) in net.calls if u.endswith("/chat/completions")]
    check("only the first attempt gets the long timeout",
          timeouts == [generator.FIRST_REQUEST_TIMEOUT, 60, 60], timeouts)
    probes = [u for (u, t) in net.calls if u.endswith("/models")]
    check("probed once", len(probes) == 1, probes)


def test_per_model_and_per_server():
    generator.reset_warm_state()
    net = _Net()
    _run(net, lambda: _gen(model="a"))
    _run(net, lambda: _gen(model="b"))
    _run(net, lambda: _gen(model="a", base="http://other/v1"))
    long_ = [u for (u, t) in net.calls if t == generator.FIRST_REQUEST_TIMEOUT]
    check("each (server, model) gets its own first call", len(long_) == 3, net.calls)


def test_never_shortens_a_longer_configured_timeout():
    generator.reset_warm_state()
    net = _Net()
    _run(net, lambda: _gen(timeout=600))
    gen_t = [t for (u, t) in net.calls if u.endswith("/chat/completions")]
    check("configured 600s beats the first-call 180s", gen_t == [600], gen_t)


def test_ollama_native_probe_url():
    generator.reset_warm_state()
    net = _Net(gen=lambda n: _Resp(json.dumps({"response": "hi"})))
    _run(net, lambda: _gen(base="http://h:11434", fmt="ollama"))
    check("ollama native probes /api/tags", net.calls[0][0] == "http://h:11434/api/tags", net.calls)


def test_anthropic_no_probe_no_long_timeout():
    generator.reset_warm_state()

    def gen(n):
        return _Resp(json.dumps({"content": [{"type": "text", "text": "hi"}]}))

    net = _Net(gen=gen)
    _run(net, lambda: _gen(base="https://api.anthropic.com/v1", fmt="anthropic"))
    check("anthropic: one request, normal timeout",
          net.calls == [("https://api.anthropic.com/v1/messages", 60)], net.calls)


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

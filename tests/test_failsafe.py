"""Fail-safe regression tests (the no-popup / no-freeze rule).

The add-on's hard rule: NO generation failure may surface to the user during a
review — every generation/transition path must log and fall back, never raise,
and logging must never reach stderr inside Anki (Anki turns stderr into its
"Anki hit an issue" dialog).

These assert:
  1. `generator.generate_raw` wraps EVERY transport failure — connection error,
     a timeout during the response READ (socket.timeout, an OSError that is NOT
     a URLError — the bug that was escaping), and a non-JSON body — into a
     single `GenerationError`, so nothing else escapes the transport layer.
  2. `strategies.generate_card` returns None (never raises) for those failures.
  3. A transition driven through the real generation path (no stubbing of the
     chokepoint) COMPLETES NORMALLY with fallback content when the network is
     dead — it does not propagate the exception.
  4. Logging routes to a FILE (never a stderr StreamHandler) when running inside
     Anki, and never propagates to the root logger — so a logged exception can
     never trip Anki's error dialog.

Runs standalone (no Anki, no Ollama, no QApplication):
    python3 tests/test_failsafe.py
or under pytest.
"""

from __future__ import annotations

import logging
import os
import socket
import sys
import tempfile
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ai_srs import db, deck_ops, generator, log, strategies  # noqa: E402
from ai_srs import state_machine as sm  # noqa: E402
from ai_srs.generator import GenerationError  # noqa: E402


# ----------------------------- helpers -----------------------------

class _FakeResp:
    """Stand-in for the object returned by urllib.request.urlopen, usable as a
    context manager. `read()` either returns a body or raises (to simulate a
    read-phase timeout)."""

    def __init__(self, body: "str | None" = None, read_exc: "BaseException | None" = None):
        self._body = body
        self._read_exc = read_exc

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        if self._read_exc is not None:
            raise self._read_exc
        return (self._body or "").encode("utf-8")


def _with_urlopen(fake):
    """Swap urllib.request.urlopen (what generator.generate_raw calls); return a
    restore() callable."""
    orig = urllib.request.urlopen
    urllib.request.urlopen = fake
    return lambda: setattr(urllib.request, "urlopen", orig)


def _ctx():
    return strategies.GenContext(word="neko", subject="Japanese")


# -------- 1. generate_raw wraps every transport failure --------

def test_generate_raw_wraps_connection_error():
    def fake(req, timeout=None):
        raise urllib.error.URLError(ConnectionRefusedError("refused"))

    restore = _with_urlopen(fake)
    try:
        try:
            generator.generate_raw("m", "p", "http://localhost:1", timeout=1)
            assert False, "expected GenerationError"
        except GenerationError:
            pass
        except Exception as e:  # must NOT escape as anything else
            assert False, f"escaped as {type(e).__name__}: {e}"
    finally:
        restore()


def test_generate_raw_wraps_read_timeout():
    """Regression: a timeout during resp.read() raises socket.timeout (an
    OSError, NOT a URLError). It must be wrapped, not escape."""
    def fake(req, timeout=None):
        return _FakeResp(read_exc=socket.timeout("timed out"))

    restore = _with_urlopen(fake)
    try:
        try:
            generator.generate_raw("m", "p", "http://localhost:11434", timeout=1)
            assert False, "expected GenerationError"
        except GenerationError:
            pass
        except Exception as e:
            assert False, f"read-timeout escaped as {type(e).__name__}: {e}"
    finally:
        restore()


def test_generate_raw_wraps_bad_json():
    def fake(req, timeout=None):
        return _FakeResp(body="not json at all {{{")

    restore = _with_urlopen(fake)
    try:
        try:
            generator.generate_raw("m", "p", "http://localhost:11434", timeout=1)
            assert False, "expected GenerationError"
        except GenerationError:
            pass
    finally:
        restore()


# -------- 2. generate_card returns None (never raises) --------

def test_generate_card_returns_none_on_network_error():
    def fake(req, timeout=None):
        raise urllib.error.URLError("down")

    restore = _with_urlopen(fake)
    try:
        content = strategies.generate_card(
            "language", _ctx(), model="m",
            endpoint="http://localhost:11434", timeout=1, retries=1,
        )
        assert content is None, content
    finally:
        restore()


def test_generate_card_returns_none_on_read_timeout():
    def fake(req, timeout=None):
        return _FakeResp(read_exc=socket.timeout("timed out"))

    restore = _with_urlopen(fake)
    try:
        content = strategies.generate_card(
            "language", _ctx(), model="m",
            endpoint="http://localhost:11434", timeout=1, retries=1,
        )
        assert content is None, content
    finally:
        restore()


# -------- 3. a transition completes (no raise) when the network is dead --------

def test_transition_proceeds_with_fallback_on_dead_network():
    """force_pass on a recycled word, driving the REAL generation path with the
    network dead, must NOT raise — it must fall back and still cycle."""
    created = []
    orig_create, orig_retire = deck_ops.create_ai_card, deck_ops.retire_card
    deck_ops.create_ai_card = lambda col, subj, suf, word, front, back: (
        created.append((suf, front, back)) or 8888
    )
    deck_ops.retire_card = lambda col, cid: None

    def fake(req, timeout=None):
        raise urllib.error.URLError("dead endpoint")

    restore = _with_urlopen(fake)
    try:
        conn = db.connect(tempfile.mkdtemp())
        wid = db.upsert_word(conn, "neko", "Japanese")
        db.set_stage(conn, wid, "recycled")
        db.set_recycle_count(conn, wid, 0)
        db.set_current_card_id(conn, wid, 500)
        row = db.get_word_by_id(conn, wid)
        cfg = {
            "recycle_graduation": 3, "validation_retries": 1,
            "request_timeout_seconds": 1,
            "models_by_subject": {"default": "m"},
            "card_types_by_subject": {"Japanese": "language"},
        }
        # The whole point: this call must return normally, never raise.
        result = sm.force_pass(object(), conn, row, cfg)
        after = db.get_word_by_id(conn, wid)
        assert result == "cycled", result
        assert after["recycle_count"] == 1, after
        assert created, "fallback card was not created"
    finally:
        restore()
        deck_ops.create_ai_card, deck_ops.retire_card = orig_create, orig_retire


# -------- 4. logging never reaches stderr inside Anki --------

def _reset_logger():
    logger = logging.getLogger(log._LOGGER_NAME)
    saved = list(logger.handlers)
    logger.handlers.clear()
    return logger, saved


def _restore_logger(logger, saved):
    for h in list(logger.handlers):
        try:
            h.close()
        except Exception:
            pass
    logger.handlers.clear()
    for h in saved:
        logger.addHandler(h)


def test_logging_in_anki_uses_file_not_stderr():
    tmp = os.path.join(tempfile.mkdtemp(), "ai_srs.log")
    logger, saved = _reset_logger()
    orig_in_anki, orig_path = log._in_anki, log._log_file_path
    log._in_anki = lambda: True
    log._log_file_path = lambda: tmp
    try:
        l = log.get_logger()
        assert l.propagate is False, "logger must not propagate to root (stderr)"
        assert any(isinstance(h, logging.FileHandler) for h in l.handlers), \
            [type(h).__name__ for h in l.handlers]
        # No PLAIN stderr StreamHandler (FileHandler is a StreamHandler subclass,
        # so check the exact type).
        assert not any(type(h) is logging.StreamHandler for h in l.handlers), \
            "in-Anki logging must not write to stderr"
    finally:
        log._in_anki, log._log_file_path = orig_in_anki, orig_path
        _restore_logger(logger, saved)


def test_logging_in_anki_never_stderr_when_log_dir_unavailable():
    # Seen in Phase 1 on Windows: user_files/ could not be created, so the old
    # code fell back to a stderr StreamHandler inside Anki and every INFO line
    # ("menu registered") became an "Anki encountered a problem" dialog.
    logger, saved = _reset_logger()
    orig_in_anki, orig_path = log._in_anki, log._log_file_path
    log._in_anki = lambda: True
    log._log_file_path = lambda: None
    try:
        l = log.get_logger()
        assert not any(type(h) is logging.StreamHandler for h in l.handlers), \
            "in-Anki logging must not fall back to stderr"
        l.info("must not raise")
    finally:
        log._in_anki, log._log_file_path = orig_in_anki, orig_path
        _restore_logger(logger, saved)


def test_logging_in_anki_never_stderr_when_file_open_fails():
    logger, saved = _reset_logger()
    orig_in_anki, orig_path = log._in_anki, log._log_file_path
    log._in_anki = lambda: True
    # A directory path: FileHandler() raises on open.
    log._log_file_path = lambda: tempfile.mkdtemp()
    try:
        l = log.get_logger()
        assert not any(type(h) is logging.StreamHandler for h in l.handlers), \
            "in-Anki logging must not fall back to stderr"
        l.info("must not raise")
    finally:
        log._in_anki, log._log_file_path = orig_in_anki, orig_path
        _restore_logger(logger, saved)


def test_logging_standalone_uses_stderr_and_never_propagates():
    logger, saved = _reset_logger()
    orig_in_anki = log._in_anki
    log._in_anki = lambda: False
    try:
        l = log.get_logger()
        assert any(type(h) is logging.StreamHandler for h in l.handlers)
        assert l.propagate is False
    finally:
        log._in_anki = orig_in_anki
        _restore_logger(logger, saved)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)

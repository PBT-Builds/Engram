"""Log lines carry a timestamp and the profile; skipped extraction is logged
(Phase 2, item 6).

Post-hoc diagnosis in Phase 1 was hard because `ai_srs.log` had no
timestamps, the log is shared by every profile, and the "Extraction skipped —
…" reason appeared in the dialog but never in the log.

Standalone: `python tests/test_logging.py`.
"""

import logging
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ai_srs import log as log_mod, vault_ingest  # noqa: E402

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


def _format(msg, profile_name=None):
    record = logging.LogRecord("ai_srs", logging.INFO, __file__, 1, msg, None, None)
    f = log_mod.ProfileFilter(lambda: profile_name)
    f.filter(record)
    return log_mod.make_formatter().format(record)


def test_line_has_timestamp_level_and_profile():
    line = _format("hello", "EngramTest")
    check("timestamp, prefix, level, profile, message",
          re.match(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \[engram\] INFO \[EngramTest\] hello$", line),
          line)


def test_no_profile_shows_dash():
    line = _format("x", None)
    check("no profile -> [-]", "[-] x" in line, line)


def test_profile_lookup_failure_is_harmless():
    def boom():
        raise RuntimeError("no mw")
    record = logging.LogRecord("ai_srs", logging.INFO, __file__, 1, "m", None, None)
    ok = log_mod.ProfileFilter(boom).filter(record)
    check("filter never drops a record", ok is True)
    check("failed lookup -> '-'", record.profile == "-", getattr(record, "profile", None))


def test_logger_handler_uses_the_formatter():
    logger = log_mod.get_logger()
    h = logger.handlers[0]
    check("handler formatter includes asctime",
          "%(asctime)s" in h.formatter._fmt, h.formatter._fmt)
    check("handler has the profile filter",
          any(isinstance(f, log_mod.ProfileFilter) for f in h.filters))


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def test_skipped_extraction_is_logged():
    cap = _Capture()
    logger = log_mod.get_logger()
    logger.addHandler(cap)
    try:
        cfg = {"providers": {"claude": {"api_format": "anthropic", "api_key": "",
                                        "model": "m"}},
               "extraction_provider": "claude", "vault_path": "."}
        avail = vault_ingest.preflight_extraction(cfg, category="Physics")
    finally:
        logger.removeHandler(cap)
    msgs = [r.getMessage() for r in cap.records if r.levelno >= logging.WARNING]
    check("unavailable", not avail.available)
    check("the skip reason is logged",
          any("extraction skipped" in m and avail.reason in m for m in msgs), msgs)


def test_release_lets_user_files_be_renamed():
    """Found in the Phase 2 clean-install test: Anki's add-on installer
    renames the add-on's user_files/ to back it up during an update. On
    Windows a folder can't be renamed while a file in it is open, and our log
    FileHandler keeps ai_srs.log open, so every in-app update failed with
    'Access is denied'. release_file_handlers() (run on Anki's
    addon_manager_will_install_addon hook) closes it; reattach_file_handlers()
    (on ..._did_install_addon) brings logging back."""
    import shutil
    import tempfile

    root = tempfile.mkdtemp(prefix="engram-uf-")
    uf = os.path.join(root, "user_files")
    os.makedirs(uf)
    logger = log_mod.get_logger()
    fh = logging.FileHandler(os.path.join(uf, "ai_srs.log"), encoding="utf-8")
    logger.addHandler(fh)
    logger.info("hello")
    try:
        if os.name == "nt":
            try:
                os.rename(uf, uf + "_backup")
                locked = False
                os.rename(uf + "_backup", uf)
            except PermissionError:
                locked = True
            check("precondition (Windows): open log blocks the rename", locked)
        log_mod.release_file_handlers()
        check("no FileHandler left after release",
              not any(isinstance(h, logging.FileHandler) for h in logger.handlers),
              logger.handlers)
        try:
            os.rename(uf, uf + "_backup")
            ok = True
        except PermissionError:
            ok = False
        check("user_files can be renamed after release", ok)
        logger.info("still safe to log while released")  # must not raise
    finally:
        for h in list(logger.handlers):
            if h is fh:
                logger.removeHandler(h)
        fh.close()
        log_mod.reattach_file_handlers()
        shutil.rmtree(root, ignore_errors=True)
    check("reattach restores a working handler", len(log_mod.get_logger().handlers) >= 1)


def test_install_hooks_only_react_to_engram():
    calls = []
    orig_rel, orig_re = log_mod.release_file_handlers, log_mod.reattach_file_handlers
    log_mod.release_file_handlers = lambda: calls.append("release")
    log_mod.reattach_file_handlers = lambda: calls.append("reattach")
    try:
        log_mod.on_will_install(None, "some_other_addon")
        log_mod.on_will_install(None, log_mod.PACKAGE)
        log_mod.on_did_install(None, log_mod.PACKAGE)
    finally:
        log_mod.release_file_handlers, log_mod.reattach_file_handlers = orig_rel, orig_re
    check("only our own package triggers release/reattach",
          calls == ["release", "reattach"], calls)


def test_delete_hook_releases_the_log():
    """Found walking the README in the clean install: Tools > Add-ons >
    Delete failed on Windows with WinError 32 because ai_srs.log was open.
    Anki fires addons_dialog_will_delete_addons(dialog, ids) right before it
    trashes the folders."""
    calls = []
    orig = log_mod.release_file_handlers
    log_mod.release_file_handlers = lambda: calls.append("release")
    try:
        log_mod.on_will_delete(None, ["some_other_addon"])
        log_mod.on_will_delete(None, ["x", log_mod.PACKAGE])
    finally:
        log_mod.release_file_handlers = orig
    check("delete of our own package releases the log", calls == ["release"], calls)


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

"""Per-profile isolation of the sidecar DB and collection-specific settings.

The guarantee under test: pipeline state (card/note IDs) written while one Anki
profile is open can never be applied to another profile's collection, and the
one-time migration of the old shared `user_files/pipeline.sqlite` never deletes
anything and only hands it to a profile whose collection it plausibly belongs to.

Covers `profile_store` (pure — no Anki): folder layout and name safety, the
settings split/merge, the collection fingerprint guard (incl. a legitimately
changed fingerprint), profile-rename adoption, and the legacy migration rules.

Standalone: `python tests/test_profile_store.py` (no Anki, temp dirs only).
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ai_srs import db, profile_store as ps  # noqa: E402

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


def raises(fn, exc=Exception):
    try:
        fn()
    except exc:
        return True
    except Exception:
        return False
    return False


# ---------- helpers ----------

def _uf():
    return tempfile.mkdtemp(prefix="engram_uf_")


def _make_legacy(user_files, *, card_ids=(), note_ids=(), vault_rows=0):
    """A pre-Phase-2 shared DB at user_files/pipeline.sqlite."""
    conn = db.connect_path(os.path.join(user_files, "pipeline.sqlite"))
    for i, cid in enumerate(card_ids):
        conn.execute(
            "INSERT INTO generated_sources (source_card_id, source_note_id, category)"
            " VALUES (?, ?, 'Japanese')", (cid, None))
    for i, nid in enumerate(note_ids):
        conn.execute(
            "INSERT INTO words (word, subject, source_note_id, stage) VALUES (?, 'J', ?, 'learning')",
            (f"w{i}", nid))
    for i in range(vault_rows):
        conn.execute(
            "INSERT INTO vault_files (path, last_ingested_mtime) VALUES (?, 1.0)", (f"n{i}.md",))
    conn.commit()
    conn.close()


def _exists(ids_present):
    present = set(ids_present)
    return lambda ids: {i for i in ids if i in present}


def _ensure(user_files, name, *, fp=111, live=("A", "B"), cards=(), notes=(), legacy=None):
    return ps.ensure_store(
        user_files, name,
        fingerprint=fp,
        live_profiles=list(live),
        existing_cards=_exists(cards),
        existing_notes=_exists(notes),
        legacy_settings=legacy or {},
    )


def _count(path, table):
    c = sqlite3.connect(path)
    try:
        return c.execute(f"select count(*) from {table}").fetchone()[0]
    finally:
        c.close()


# ---------- layout / names ----------

def test_profile_dir_layout():
    uf = _uf()
    check("profile dir is user_files/profiles/<name>",
          ps.profile_dir(uf, "EngramTest") == os.path.join(uf, "profiles", "EngramTest"))


def test_profile_dir_rejects_unsafe_names():
    uf = _uf()
    for bad in ("", "..", ".", "a/b", "a\\b", None):
        check(f"unsafe profile name {bad!r} rejected",
              raises(lambda: ps.profile_dir(uf, bad), ps.ProfileStoreError))


# ---------- settings split / merge ----------

def test_split_moves_only_per_profile_keys():
    cfg = {"deck_category_map": {"D": "J"}, "vault_path": "C:/v", "temperature": 0.3,
           "providers": {"local": {}}}
    g, p = ps.split_config(cfg)
    check("split: global keeps temperature/providers",
          g == {"temperature": 0.3, "providers": {"local": {}}}, g)
    check("split: profile gets deck map + vault path",
          p == {"deck_category_map": {"D": "J"}, "vault_path": "C:/v"}, p)


def test_merge_profile_overrides_and_defaults():
    g = {"temperature": 0.3, "deck_category_map": {"STALE": "X"}}
    eff = ps.merge_config(g, {"vault_path": "C:/v"})
    check("merge: global key kept", eff["temperature"] == 0.3)
    check("merge: global per-profile key is ignored (never shared)",
          eff["deck_category_map"] == {}, eff["deck_category_map"])
    check("merge: profile value wins", eff["vault_path"] == "C:/v")
    check("merge: missing per-profile keys get defaults",
          eff["vault_include_globs"] == ["**/*.md"] and eff["vault_category_map"] == {})


def test_merge_does_not_alias_defaults():
    a = ps.merge_config({}, {})
    a["deck_category_map"]["X"] = "Y"
    b = ps.merge_config({}, {})
    check("merge: defaults are fresh copies", b["deck_category_map"] == {})


def test_settings_roundtrip_and_missing_file():
    uf = _uf()
    pdir = ps.profile_dir(uf, "A")
    check("load_settings on a missing folder -> {}", ps.load_settings(pdir) == {})
    ps.save_settings(pdir, {"vault_path": "C:/v", "temperature": 9})
    check("save_settings keeps only per-profile keys",
          ps.load_settings(pdir) == {"vault_path": "C:/v"})


def test_corrupt_settings_file_is_not_fatal():
    uf = _uf()
    pdir = ps.profile_dir(uf, "A")
    os.makedirs(pdir)
    with open(os.path.join(pdir, "settings.json"), "w") as f:
        f.write("{not json")
    check("corrupt settings.json -> {} (no raise)", ps.load_settings(pdir) == {})


# ---------- fresh profile ----------

def test_fresh_profile_without_legacy():
    uf = _uf()
    res = _ensure(uf, "A")
    check("fresh: action", res.action == "created", res.action)
    check("fresh: DB in profile folder",
          res.db_path == os.path.join(uf, "profiles", "A", "pipeline.sqlite")
          and os.path.exists(res.db_path))
    conn = db.connect_path(res.db_path)
    check("fresh: fingerprint recorded", ps.read_fingerprint(conn) == 111)
    conn.close()


def test_two_profiles_get_separate_dbs():
    uf = _uf()
    a = _ensure(uf, "A", fp=1)
    b = _ensure(uf, "B", fp=2)
    ca = db.connect_path(a.db_path)
    ca.execute("INSERT INTO generated_sources (source_card_id) VALUES (42)")
    ca.commit()
    ca.close()
    check("profiles: separate files", a.db_path != b.db_path)
    check("profiles: B does not see A's rows", _count(b.db_path, "generated_sources") == 0)


def test_reopen_same_profile():
    uf = _uf()
    _ensure(uf, "A")
    res = _ensure(uf, "A")
    check("reopen: action opened", res.action == "opened", res.action)


# ---------- fingerprint guard ----------

def test_fingerprint_mismatch_with_foreign_ids_refuses():
    uf = _uf()
    res = _ensure(uf, "A", fp=1)
    c = db.connect_path(res.db_path)
    c.execute("INSERT INTO generated_sources (source_card_id) VALUES (500), (501)")
    c.commit()
    c.close()
    check("mismatch + IDs not in this collection -> ProfileStoreError",
          raises(lambda: _ensure(uf, "A", fp=2, cards=()), ps.ProfileStoreError))


def test_fingerprint_changed_but_ids_match_is_accepted():
    """col.crt can legitimately change (e.g. scheduler settings); if the DB's
    card IDs really are in this collection, it is the same collection."""
    uf = _uf()
    res = _ensure(uf, "A", fp=1)
    c = db.connect_path(res.db_path)
    c.execute("INSERT INTO generated_sources (source_card_id) VALUES (500), (501)")
    c.commit()
    c.close()
    res2 = _ensure(uf, "A", fp=2, cards=(500, 501))
    check("mismatch + IDs present -> accepted", res2.action == "fingerprint-updated", res2.action)
    c = db.connect_path(res2.db_path)
    check("fingerprint updated", ps.read_fingerprint(c) == 2)
    c.close()


def test_fingerprint_changed_with_no_ids_is_accepted():
    uf = _uf()
    _ensure(uf, "A", fp=1)
    res = _ensure(uf, "A", fp=2)
    check("mismatch + no card IDs at all -> accepted (nothing to misapply)",
          res.action == "fingerprint-updated", res.action)


def test_missing_fingerprint_is_written():
    uf = _uf()
    pdir = ps.profile_dir(uf, "A")
    os.makedirs(pdir)
    db.connect_path(os.path.join(pdir, "pipeline.sqlite")).close()
    res = _ensure(uf, "A", fp=7)
    c = db.connect_path(res.db_path)
    check("no fingerprint yet -> written", ps.read_fingerprint(c) == 7)
    c.close()


# ---------- rename adoption ----------

def test_renamed_profile_adopts_orphan_folder():
    uf = _uf()
    _ensure(uf, "Old", fp=9, live=("Old",))
    res = _ensure(uf, "New", fp=9, live=("New",))
    check("rename: adopted", res.action == "adopted", res.action)
    check("rename: folder moved",
          os.path.isdir(ps.profile_dir(uf, "New")) and not os.path.exists(ps.profile_dir(uf, "Old")))


def test_live_profile_folder_is_never_adopted():
    uf = _uf()
    _ensure(uf, "A", fp=9, live=("A", "B"))
    res = _ensure(uf, "B", fp=9, live=("A", "B"))
    check("a folder of a profile that still exists is not taken",
          res.action == "created" and os.path.isdir(ps.profile_dir(uf, "A")), res.action)


def test_orphan_with_other_fingerprint_not_adopted():
    uf = _uf()
    _ensure(uf, "Old", fp=1, live=("Old",))
    res = _ensure(uf, "New", fp=2, live=("New",))
    check("orphan with a different fingerprint stays put",
          res.action == "created" and os.path.isdir(ps.profile_dir(uf, "Old")), res.action)


# ---------- legacy migration ----------

def test_legacy_with_matching_ids_is_migrated():
    uf = _uf()
    _make_legacy(uf, card_ids=(10, 11, 12), note_ids=(20,))
    legacy_cfg = {"deck_category_map": {"Japanese::Original": "Japanese"},
                  "vault_path": "C:/v", "temperature": 0.4}
    res = _ensure(uf, "A", cards=(10, 11, 12), notes=(20,), legacy=legacy_cfg)
    check("legacy: migrated", res.action == "migrated", res.action)
    check("legacy: rows copied", _count(res.db_path, "generated_sources") == 3
          and _count(res.db_path, "words") == 1)
    check("legacy: original renamed, not deleted",
          not os.path.exists(os.path.join(uf, "pipeline.sqlite"))
          and os.path.exists(os.path.join(uf, "pipeline.sqlite.migrated-to-A")))
    check("legacy: migrated settings returned (per-profile keys only)",
          res.migrated_settings == {"deck_category_map": {"Japanese::Original": "Japanese"},
                                    "vault_path": "C:/v"}, res.migrated_settings)
    check("legacy: settings.json written",
          ps.load_settings(ps.profile_dir(uf, "A"))["vault_path"] == "C:/v")
    c = db.connect_path(res.db_path)
    check("legacy: fingerprint stamped", ps.read_fingerprint(c) == 111)
    c.close()


def test_legacy_majority_rule():
    uf = _uf()
    _make_legacy(uf, card_ids=(1, 2, 3, 4))
    res = _ensure(uf, "A", cards=(1, 2))  # exactly half
    check("legacy: >=50% of IDs present -> claimed", res.action == "migrated", res.action)

    uf2 = _uf()
    _make_legacy(uf2, card_ids=(1, 2, 3, 4))
    res2 = _ensure(uf2, "A", cards=(1,))  # 25%
    check("legacy: <50% -> not claimed", res2.action == "created", res2.action)


def test_legacy_with_foreign_ids_is_left_for_another_profile():
    uf = _uf()
    _make_legacy(uf, card_ids=(10, 11))
    legacy_cfg = {"deck_category_map": {"D": "J"}}
    res = _ensure(uf, "A", cards=(), legacy=legacy_cfg)
    check("foreign legacy: profile A gets a fresh DB", res.action == "created", res.action)
    check("foreign legacy: legacy file untouched",
          os.path.exists(os.path.join(uf, "pipeline.sqlite")))
    check("foreign legacy: no settings migrated", res.migrated_settings is None)
    check("foreign legacy: A's DB is empty", _count(res.db_path, "generated_sources") == 0)
    res_b = _ensure(uf, "B", fp=222, cards=(10, 11), legacy=legacy_cfg)
    check("foreign legacy: the owning profile claims it later", res_b.action == "migrated")
    check("foreign legacy: owner gets the settings",
          res_b.migrated_settings == {"deck_category_map": {"D": "J"}})


def test_legacy_without_ids_goes_to_first_profile():
    uf = _uf()
    _make_legacy(uf, vault_rows=3)
    res = _ensure(uf, "A", legacy={"vault_path": "C:/v"})
    check("ID-less legacy: claimed by the first profile", res.action == "migrated")
    check("ID-less legacy: ledger rows copied", _count(res.db_path, "vault_files") == 3)
    res_b = _ensure(uf, "B", fp=5)
    check("ID-less legacy: second profile starts fresh",
          res_b.action == "created" and res_b.migrated_settings is None)


def test_legacy_settings_without_db_go_to_first_profile():
    uf = _uf()
    res = _ensure(uf, "A", legacy={"vault_category_map": {"HI": "History"}})
    check("settings-only legacy: migrated", res.action == "migrated", res.action)
    check("settings-only legacy: settings moved",
          res.migrated_settings == {"vault_category_map": {"HI": "History"}})
    res_b = _ensure(uf, "B", fp=5, legacy={"vault_category_map": {"HI": "History"}})
    check("settings-only legacy: claimed only once", res_b.migrated_settings is None)


def test_empty_legacy_settings_are_not_a_claim():
    uf = _uf()
    res = _ensure(uf, "A", legacy={"deck_category_map": {}, "vault_path": "",
                                   "vault_include_globs": ["**/*.md"], "vault_category_map": {}})
    check("default-valued legacy keys do not count as legacy state",
          res.action == "created" and res.migrated_settings is None, res.action)


def test_migration_survives_locked_legacy_rename():
    """On Windows the rename can fail if something holds the file; the marker
    must still stop a second profile from claiming it."""
    uf = _uf()
    _make_legacy(uf, card_ids=(1,))
    orig = os.replace
    try:
        def boom(src, dst):
            if src.endswith("pipeline.sqlite") and "migrated-to" in dst:
                raise PermissionError("locked")
            return orig(src, dst)
        ps.os.replace = boom
        res = _ensure(uf, "A", cards=(1,))
    finally:
        ps.os.replace = orig
    check("locked rename: still migrated", res.action == "migrated", res.action)
    res_b = _ensure(uf, "B", fp=3, cards=(1,))
    check("locked rename: marker prevents a second claim", res_b.action == "created", res_b.action)


def test_existing_profile_settings_not_overwritten_by_migration():
    uf = _uf()
    pdir = ps.profile_dir(uf, "A")
    ps.save_settings(pdir, {"vault_path": "C:/mine"})
    _make_legacy(uf, card_ids=(1,))
    res = _ensure(uf, "A", cards=(1,), legacy={"vault_path": "C:/old"})
    check("profile that already has settings keeps them",
          ps.load_settings(pdir)["vault_path"] == "C:/mine")


def test_legacy_db_is_not_modified_when_inspected():
    """Deciding whether to claim the old DB must only READ it: no schema
    changes, so a legacy file left for another profile stays byte-identical."""
    import hashlib
    uf = _uf()
    _make_legacy(uf, card_ids=(10, 11))
    legacy = os.path.join(uf, "pipeline.sqlite")
    # Simulate a pre-Phase-2 DB: no `meta` table.
    c = sqlite3.connect(legacy)
    c.execute("DROP TABLE meta")
    c.commit()
    c.close()
    before = hashlib.sha256(open(legacy, "rb").read()).hexdigest()
    _ensure(uf, "A", cards=())  # not claimed
    after = hashlib.sha256(open(legacy, "rb").read()).hexdigest()
    check("unclaimed legacy DB is byte-identical after inspection", before == after)


def test_runtime_user_files_is_a_resolved_path():
    """The dev install is a directory symlink (addons21/ai_srs -> src/ai_srs).
    On Windows, creating a NEW folder through that symlink fails with
    FileExistsError, which broke the first in-Anki run. Paths we create
    folders under must be resolved."""
    from ai_srs import profile_runtime
    uf = profile_runtime.user_files_dir()
    check("user_files_dir is realpath-resolved", os.path.realpath(uf) == uf, uf)
    check("db.default_addon_dir is realpath-resolved",
          os.path.realpath(db.default_addon_dir()) == db.default_addon_dir())


def test_same_fingerprint_but_foreign_ids_refuses():
    """Found in the clean-install test: col.crt is the creation DAY, so two
    collections made on the same day share it. A matching fingerprint must
    not be enough when NONE of the DB's IDs exist in the open collection."""
    uf = _uf()
    res = _ensure(uf, "A", fp=7)
    c = db.connect_path(res.db_path)
    c.execute("INSERT INTO generated_sources (source_card_id) VALUES (900), (901)")
    c.commit()
    c.close()
    check("same fingerprint + no IDs present -> refused",
          raises(lambda: _ensure(uf, "A", fp=7, cards=()), ps.ProfileStoreError))


def test_same_fingerprint_with_some_ids_present_opens():
    """The user may delete cards Engram tracks; with the fingerprint matching,
    one surviving ID is enough (no 50% rule here)."""
    uf = _uf()
    res = _ensure(uf, "A", fp=7)
    c = db.connect_path(res.db_path)
    c.execute("INSERT INTO generated_sources (source_card_id) VALUES (1), (2), (3), (4)")
    c.commit()
    c.close()
    res2 = _ensure(uf, "A", fp=7, cards=(4,))
    check("same fingerprint + 1 of 4 IDs present -> opened", res2.action == "opened", res2.action)


def test_orphan_with_same_fingerprint_but_foreign_ids_not_adopted():
    """Same-day crt again: an orphaned folder from a DIFFERENT collection made
    the same day must not be adopted when none of its IDs exist here."""
    uf = _uf()
    res = _ensure(uf, "Old", fp=9, live=("Old",))
    c = db.connect_path(res.db_path)
    c.execute("INSERT INTO generated_sources (source_card_id) VALUES (900)")
    c.commit()
    c.close()
    res2 = _ensure(uf, "New", fp=9, live=("New",), cards=())
    check("foreign orphan with same fingerprint stays put",
          res2.action == "created" and os.path.isdir(ps.profile_dir(uf, "Old")), res2.action)


if __name__ == "__main__":
    for fn in list(globals().values()):
        if callable(fn) and getattr(fn, "__name__", "").startswith("test_"):
            try:
                fn()
            except Exception as e:  # a crash is a failure, not an abort
                _failed += 1
                print(f"FAIL  {fn.__name__} raised {type(e).__name__}: {e}")
    print(f"\n{_passed}/{_passed + _failed} passed")
    sys.exit(1 if _failed else 0)

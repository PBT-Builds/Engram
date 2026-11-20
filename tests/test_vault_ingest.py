"""Obsidian vault ingestion: scanning, mtime tracking, and extraction parsing.

Covers steps 4 and 5 of the vault feature, entirely offline:

  - **Config** (`resolve_vault_config`) — defaults, `~` expansion, and the
    malformed-value paths that must degrade instead of raising.
  - **Glob matching** — `*` must NOT cross a directory separator (the trap
    `fnmatch` falls into); `**` must.
  - **Category resolution** — folder prefix, longest-key-wins, unmapped → None.
  - **Scanning** against a real temp directory of fake `.md` files: which files
    are seen, which are pruned, and — the point of the sidecar ledger — that an
    unchanged note is skipped on the second run while a touched one comes back.
  - **Markdown cleanup** — frontmatter, embeds, wikilinks, links, and the
    code-fence protection that keeps `[x](y)` inside a snippet intact.
  - **Extraction** — prompt contents, then parsing against canned LLM responses:
    well-formed, fenced, prose-wrapped, single-object, envelope, truncated,
    outright garbage, and partially-bad arrays. Plus the transport failure path,
    which must yield no candidates and never raise.

Standalone: `python3 tests/test_vault_ingest.py` (no Anki / Qt / network; the
sidecar is a real SQLite file in a temp dir, the LLM is mocked).
"""

import json
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ai_srs import db, vault_ingest  # noqa: E402

_passed = 0
_failed = 0


def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"PASS  {name}")
    else:
        _failed += 1
        print(f"FAIL  {name}: {detail}")


# --------------------- fixtures ---------------------

class _Vault:
    """A throwaway vault directory plus its own sidecar DB."""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="engram-vault-")
        self.addon_dir = tempfile.mkdtemp(prefix="engram-addon-")
        self.conn = db.connect(self.addon_dir)

    def write(self, rel, text="# Note\n\nSome content.\n", mtime=None):
        path = os.path.join(self.root, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def close(self):
        self.conn.close()
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.addon_dir, ignore_errors=True)


class _FakeResp:
    def __init__(self, body):
        self._body = body.encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


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


def _openai_body(text):
    return json.dumps({"choices": [{"message": {"content": text}}]})


CFG = {
    "vault_path": "/nonexistent",
    "vault_category_map": {"Weekly/Physics": "Physics"},
    "extraction_provider": "local",
    "extraction_model": "phi4",
    "max_cards_per_note": 15,
    "providers": {"local": {"api_format": "openai", "api_base": "http://x/v1"}},
}


# --------------------- config ---------------------

def test_config_defaults():
    vcfg = vault_ingest.resolve_vault_config({})
    check("no vault_path → not configured", vcfg.configured is False)
    check("default include globs", vcfg.include_globs == ("**/*.md",), vcfg.include_globs)
    check("default category map empty", vcfg.category_map == {}, vcfg.category_map)
    check(
        "default max_cards_per_note is 15",
        vcfg.max_cards_per_note == 15, vcfg.max_cards_per_note,
    )
    check("no extraction provider named", vcfg.extraction_provider is None)


def test_config_expands_user_home():
    vcfg = vault_ingest.resolve_vault_config({"vault_path": "~/Vault"})
    check("~ expanded", "~" not in vcfg.path, vcfg.path)
    check("path non-empty", vcfg.configured is True)


def test_config_string_glob_accepted():
    vcfg = vault_ingest.resolve_vault_config({"vault_include_globs": "Weekly/*.md"})
    check("bare string glob wrapped in a tuple",
          vcfg.include_globs == ("Weekly/*.md",), vcfg.include_globs)


def test_config_malformed_values_degrade():
    vcfg = vault_ingest.resolve_vault_config({
        "vault_include_globs": 17,
        "vault_category_map": "not-a-dict",
        "max_cards_per_note": "lots",
    })
    check("bad globs → defaults", vcfg.include_globs == ("**/*.md",), vcfg.include_globs)
    check("bad category map → empty", vcfg.category_map == {}, vcfg.category_map)
    check("bad max_cards → default", vcfg.max_cards_per_note == 15, vcfg.max_cards_per_note)


def test_config_zero_max_cards_clamped():
    vcfg = vault_ingest.resolve_vault_config({"max_cards_per_note": 0})
    check("max_cards < 1 clamped to 1", vcfg.max_cards_per_note == 1, vcfg.max_cards_per_note)


# --------------------- glob matching ---------------------

def test_star_does_not_cross_directories():
    check(
        "Weekly/*.md matches a direct child",
        vault_ingest.matches_globs("Weekly/note.md", ["Weekly/*.md"]),
    )
    check(
        "Weekly/*.md does NOT match a nested file",
        not vault_ingest.matches_globs("Weekly/2024/note.md", ["Weekly/*.md"]),
    )


def test_double_star_crosses_directories():
    check(
        "**/*.md matches at the root",
        vault_ingest.matches_globs("note.md", ["**/*.md"]),
    )
    check(
        "**/*.md matches nested",
        vault_ingest.matches_globs("a/b/c/note.md", ["**/*.md"]),
    )
    check(
        "Weekly/**/*.md matches nested under Weekly",
        vault_ingest.matches_globs("Weekly/2024/note.md", ["Weekly/**/*.md"]),
    )
    check(
        "Weekly/**/*.md does not match another tree",
        not vault_ingest.matches_globs("Daily/2024/note.md", ["Weekly/**/*.md"]),
    )


def test_multiple_globs_are_or_ed():
    pats = ["Weekly/*.md", "Inbox/*.md"]
    check("first glob matches", vault_ingest.matches_globs("Weekly/a.md", pats))
    check("second glob matches", vault_ingest.matches_globs("Inbox/b.md", pats))
    check("neither matches", not vault_ingest.matches_globs("Archive/c.md", pats))


# --------------------- category resolution ---------------------

def test_category_folder_prefix():
    m = {"Weekly/Physics": "Physics", "Weekly/CS": "ComputerScience"}
    check(
        "folder prefix resolves",
        vault_ingest.category_for_note(m, "Weekly/Physics/week-3.md") == "Physics",
    )
    check(
        "sibling folder resolves separately",
        vault_ingest.category_for_note(m, "Weekly/CS/week-3.md") == "ComputerScience",
    )
    check(
        "unmapped folder → None",
        vault_ingest.category_for_note(m, "Journal/2026.md") is None,
    )


def test_category_longest_key_wins():
    m = {"Weekly": "General", "Weekly/Physics": "Physics"}
    check(
        "most specific key wins",
        vault_ingest.category_for_note(m, "Weekly/Physics/a.md") == "Physics",
    )
    check(
        "less specific key still covers its own subtree",
        vault_ingest.category_for_note(m, "Weekly/Other/a.md") == "General",
    )


def test_category_exact_file_key():
    m = {"Inbox/Reading.md": "History"}
    check(
        "a whole-file key matches exactly",
        vault_ingest.category_for_note(m, "Inbox/Reading.md") == "History",
    )
    check(
        "and does not match a sibling",
        vault_ingest.category_for_note(m, "Inbox/Other.md") is None,
    )


def test_category_map_ignores_junk_entries():
    m = {"Weekly": "Physics", 17: "Nope", "Bad": None}
    check(
        "non-string keys/values ignored, good one still works",
        vault_ingest.category_for_note(m, "Weekly/a.md") == "Physics",
    )


# --------------------- scanning ---------------------

def test_scan_finds_markdown_and_attaches_category():
    v = _Vault()
    try:
        v.write("Weekly/Physics/week-1.md")
        v.write("Weekly/Physics/week-2.md")
        v.write("Journal/private.md")
        v.write("Weekly/Physics/image.png", "not markdown")
        vcfg = vault_ingest.resolve_vault_config({
            "vault_path": v.root,
            "vault_category_map": {"Weekly/Physics": "Physics"},
        })
        notes = vault_ingest.scan_vault(vcfg, v.conn)
        paths = [n.rel_path for n in notes]
        check("finds all markdown files", len(notes) == 3, paths)
        check("non-markdown ignored", "Weekly/Physics/image.png" not in paths, paths)
        check("results sorted by path", paths == sorted(paths), paths)
        by_path = {n.rel_path: n for n in notes}
        check(
            "mapped note carries its category",
            by_path["Weekly/Physics/week-1.md"].category == "Physics",
        )
        check(
            "unmapped note has category None",
            by_path["Journal/private.md"].category is None,
        )
        check(
            "mapped_notes filters to routable ones",
            len(vault_ingest.mapped_notes(notes)) == 2,
        )
    finally:
        v.close()


def test_scan_prunes_obsidian_internals():
    v = _Vault()
    try:
        v.write(".obsidian/workspace.md")
        v.write(".trash/deleted.md")
        v.write("Real/note.md")
        vcfg = vault_ingest.resolve_vault_config({"vault_path": v.root})
        notes = vault_ingest.scan_vault(vcfg, v.conn)
        paths = [n.rel_path for n in notes]
        check("only the real note is scanned", paths == ["Real/note.md"], paths)
    finally:
        v.close()


def test_scan_honours_include_globs():
    v = _Vault()
    try:
        v.write("Weekly/a.md")
        v.write("Weekly/2024/b.md")
        v.write("Archive/c.md")
        vcfg = vault_ingest.resolve_vault_config({
            "vault_path": v.root, "vault_include_globs": ["Weekly/*.md"],
        })
        paths = [n.rel_path for n in vault_ingest.scan_vault(vcfg, v.conn)]
        check("only direct children of Weekly", paths == ["Weekly/a.md"], paths)
    finally:
        v.close()


def test_unchanged_files_skipped_on_second_scan():
    # The whole point of the sidecar ledger: a weekly vault must not be re-sent
    # to the LLM in full on every run.
    v = _Vault()
    try:
        v.write("Weekly/Physics/week-1.md")
        v.write("Weekly/Physics/week-2.md")
        vcfg = vault_ingest.resolve_vault_config({
            "vault_path": v.root,
            "vault_category_map": {"Weekly/Physics": "Physics"},
        })
        first = vault_ingest.scan_vault(vcfg, v.conn)
        check("first scan sees both", len(first) == 2, [n.rel_path for n in first])
        for note in first:
            vault_ingest.mark_ingested(v.conn, note, cards_imported=3)

        second = vault_ingest.scan_vault(vcfg, v.conn)
        check("second scan sees nothing changed", second == [], [n.rel_path for n in second])
    finally:
        v.close()


def test_touched_file_reappears():
    v = _Vault()
    try:
        v.write("Weekly/a.md")
        v.write("Weekly/b.md")
        vcfg = vault_ingest.resolve_vault_config({"vault_path": v.root})
        for note in vault_ingest.scan_vault(vcfg, v.conn):
            vault_ingest.mark_ingested(v.conn, note)

        # Edit one note, leave the other alone.
        later = time.time() + 60
        v.write("Weekly/a.md", "# Edited\n\nNew content.\n", mtime=later)

        changed = vault_ingest.scan_vault(vcfg, v.conn)
        check(
            "only the edited note comes back",
            [n.rel_path for n in changed] == ["Weekly/a.md"],
            [n.rel_path for n in changed],
        )
    finally:
        v.close()


def test_force_ignores_the_ledger():
    v = _Vault()
    try:
        v.write("Weekly/a.md")
        vcfg = vault_ingest.resolve_vault_config({"vault_path": v.root})
        for note in vault_ingest.scan_vault(vcfg, v.conn):
            vault_ingest.mark_ingested(v.conn, note)
        check("normally skipped", vault_ingest.scan_vault(vcfg, v.conn) == [])
        forced = vault_ingest.scan_vault(vcfg, v.conn, force=True)
        check("force=True returns it anyway", len(forced) == 1, forced)
    finally:
        v.close()


def test_mark_ingested_records_scan_mtime_and_accumulates_counts():
    v = _Vault()
    try:
        v.write("Weekly/a.md")
        vcfg = vault_ingest.resolve_vault_config({"vault_path": v.root})
        note = vault_ingest.scan_vault(vcfg, v.conn)[0]
        vault_ingest.mark_ingested(v.conn, note, cards_imported=4)
        row = db.get_vault_file(v.conn, "Weekly/a.md")
        check("row written", row is not None)
        check(
            "records the mtime the SCAN observed",
            abs(row["last_ingested_mtime"] - note.mtime) < 1e-6,
            (row["last_ingested_mtime"], note.mtime),
        )
        check("card count stored", row["cards_imported"] == 4, row["cards_imported"])

        later = time.time() + 60
        v.write("Weekly/a.md", "# Edited\n", mtime=later)
        note2 = vault_ingest.scan_vault(vcfg, v.conn)[0]
        vault_ingest.mark_ingested(v.conn, note2, cards_imported=3)
        row2 = db.get_vault_file(v.conn, "Weekly/a.md")
        check(
            "counts accumulate across runs",
            row2["cards_imported"] == 7, row2["cards_imported"],
        )
    finally:
        v.close()


def test_forget_vault_file_makes_it_new_again():
    v = _Vault()
    try:
        v.write("Weekly/a.md")
        vcfg = vault_ingest.resolve_vault_config({"vault_path": v.root})
        note = vault_ingest.scan_vault(vcfg, v.conn)[0]
        vault_ingest.mark_ingested(v.conn, note)
        check("forget removes the row", db.forget_vault_file(v.conn, "Weekly/a.md") is True)
        check("note is seen again", len(vault_ingest.scan_vault(vcfg, v.conn)) == 1)
        check(
            "forgetting an unknown path is False, not an error",
            db.forget_vault_file(v.conn, "nope.md") is False,
        )
    finally:
        v.close()


def test_scan_missing_vault_path_is_graceful():
    v = _Vault()
    try:
        vcfg = vault_ingest.resolve_vault_config({"vault_path": os.path.join(v.root, "nope")})
        check("missing directory → [] (no raise)", vault_ingest.scan_vault(vcfg, v.conn) == [])
        unset = vault_ingest.resolve_vault_config({})
        check("unconfigured vault → []", vault_ingest.scan_vault(unset, v.conn) == [])
    finally:
        v.close()


# --------------------- markdown cleanup ---------------------

def test_strip_frontmatter():
    text = "---\ntags: [physics]\ndate: 2026-01-01\n---\n\n# Optics\n\nLight bends.\n"
    out = vault_ingest.strip_markdown(text)
    check("frontmatter removed", "tags:" not in out, out)
    check("body kept", "Light bends." in out, out)


def test_strip_wikilinks_and_embeds():
    text = "See [[Snell's Law]] and [[Optics#Refraction|refraction]].\n\n![[diagram.png]]\n"
    out = vault_ingest.strip_markdown(text)
    check("bare wikilink unwrapped to target", "Snell's Law" in out, out)
    check("piped wikilink unwrapped to display text", "refraction" in out, out)
    check("no wikilink syntax remains", "[[" not in out and "]]" not in out, out)
    check("embed removed entirely", "diagram.png" not in out, out)


def test_strip_markdown_links_and_images():
    text = "Read [the paper](https://example.com/a.pdf).\n\n![alt](img.png)\n"
    out = vault_ingest.strip_markdown(text)
    check("link text kept", "the paper" in out, out)
    check("URL dropped", "example.com" not in out, out)
    check("image markup dropped", "img.png" not in out and "alt" not in out, out)


def test_code_fences_are_protected():
    text = (
        "Intro with [[a link]].\n\n"
        "```python\n"
        "url = requests.get('[x](y)')  # not a markdown link\n"
        "wiki = '[[not a wikilink]]'\n"
        "```\n\n"
        "Outro.\n"
    )
    out = vault_ingest.strip_markdown(text)
    check("prose wikilink still unwrapped", "[[a link]]" not in out, out)
    check("code fence preserved verbatim", "'[x](y)'" in out, out)
    check("wikilink inside code preserved", "'[[not a wikilink]]'" in out, out)


def test_strip_collapses_blank_lines_and_trims():
    out = vault_ingest.strip_markdown("\n\n\nA\n\n\n\n\nB\n\n\n")
    check("leading/trailing whitespace trimmed", out == "A\n\nB", repr(out))


def test_strip_empty_input():
    check("empty string safe", vault_ingest.strip_markdown("") == "")
    check("None-ish safe", vault_ingest.strip_markdown(None) == "")


def test_read_note_text_missing_file_is_graceful():
    note = vault_ingest.VaultNote(
        rel_path="gone.md", abs_path="/nonexistent/gone.md",
        mtime=0.0, category=None,
    )
    check("unreadable file → '' (no raise)", vault_ingest.read_note_text(note) == "")


# --------------------- extraction prompt ---------------------

def test_prompt_states_the_cap_and_carries_the_note():
    prompt = vault_ingest.build_extraction_prompt(
        "Light bends at an interface.", category="Physics", max_cards=15,
    )
    check("cap stated to the model", "at most 15" in prompt, prompt[:200])
    check("category mentioned", "Physics" in prompt, prompt[:200])
    check("note text included", "Light bends at an interface." in prompt)
    check("asks for a JSON object with a cards array", "JSON object" in prompt and "\"cards\"" in prompt)
    check("names both keys", '"front"' in prompt and '"back"' in prompt)


def test_prompt_emphasize_adds_a_json_nudge():
    plain = vault_ingest.build_extraction_prompt("x", max_cards=5)
    nudged = vault_ingest.build_extraction_prompt("x", max_cards=5, emphasize=True)
    check("emphasize adds guidance", len(nudged) > len(plain))
    check("emphasize mentions valid JSON", "valid JSON" in nudged, nudged[-200:])


# --------------------- extraction parsing: well-formed ---------------------

GOOD = json.dumps([
    {"front": "What is Snell's law?", "back": "n1 sin(t1) = n2 sin(t2)"},
    {"front": "What is refraction?", "back": "Bending of light at an interface."},
])


def test_parse_well_formed_array():
    out = vault_ingest.parse_candidates(GOOD, source_path="a.md", category="Physics")
    check("two candidates parsed", len(out) == 2, out)
    check("front captured", out[0].front == "What is Snell's law?", out[0].front)
    check("back captured", out[0].back.startswith("n1 sin"), out[0].back)
    check("source path attached", out[0].source_path == "a.md")
    check("category attached", out[0].category == "Physics")


def test_parse_code_fenced_array():
    raw = "Here you go:\n\n```json\n" + GOOD + "\n```\n"
    out = vault_ingest.parse_candidates(raw)
    check("fenced JSON recovered", len(out) == 2, out)


def test_parse_prose_wrapped_array():
    raw = "Sure! " + GOOD + " Let me know if you want more."
    out = vault_ingest.parse_candidates(raw)
    check("prose-wrapped JSON recovered", len(out) == 2, out)


def test_parse_single_object_unwrapped():
    raw = json.dumps({"front": "Q", "back": "A"})
    out = vault_ingest.parse_candidates(raw)
    check("lone object accepted as one card", len(out) == 1, out)


def test_parse_envelope_object():
    raw = json.dumps({"cards": [{"front": "Q", "back": "A"}]})
    out = vault_ingest.parse_candidates(raw)
    check("{'cards': [...]} envelope unwrapped", len(out) == 1, out)


def test_parse_empty_array_is_a_valid_answer():
    out = vault_ingest.parse_candidates("[]")
    check("empty array → no candidates, no error", out == [], out)


# --------------------- extraction parsing: malformed ---------------------

def test_parse_garbage_returns_empty():
    for raw in [
        "I'm sorry, I can't help with that.",
        "",
        "null",
        "<html><body>502 Bad Gateway</body></html>",
    ]:
        out = vault_ingest.parse_candidates(raw)
        check(f"garbage → [] ({raw[:24]!r})", out == [], out)


def test_parse_truncated_json_returns_empty():
    truncated = '[{"front": "What is Snell\'s law?", "back": "n1 sin(t1'
    out = vault_ingest.parse_candidates(truncated)
    check("truncated JSON → []", out == [], out)


def test_parse_drops_bad_entries_but_keeps_good_ones():
    raw = json.dumps([
        {"front": "Good Q", "back": "Good A"},
        {"front": "", "back": "no question"},
        {"back": "missing front"},
        "not an object",
        {"front": "x", "back": "y" * 5000},
        {"front": "Another Q", "back": "Another A"},
    ])
    out = vault_ingest.parse_candidates(raw)
    check("only the usable entries survive", len(out) == 2, [c.front for c in out])
    check("order preserved", out[0].front == "Good Q" and out[1].front == "Another Q")


def test_parse_wrong_types_are_dropped():
    raw = json.dumps([{"front": 42, "back": ["a", "b"]}, {"front": "Q", "back": "A"}])
    out = vault_ingest.parse_candidates(raw)
    check("non-string sides dropped", len(out) == 1 and out[0].front == "Q", out)


def test_parse_enforces_max_cards_cap():
    many = json.dumps([{"front": f"Q{i}", "back": f"A{i}"} for i in range(40)])
    out = vault_ingest.parse_candidates(many, max_cards=15)
    check("hard cap applied regardless of model output", len(out) == 15, len(out))
    check("cap keeps the first N", out[0].front == "Q0" and out[14].front == "Q14")
    tight = vault_ingest.parse_candidates(many, max_cards=3)
    check("cap is configurable", len(tight) == 3, len(tight))


# --------------------- extraction end to end ---------------------

def _note(tmpdir, category="Physics"):
    path = os.path.join(tmpdir, "note.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("# Optics\n\nLight bends at an interface.\n")
    return vault_ingest.VaultNote(
        rel_path="Weekly/Physics/note.md", abs_path=path,
        mtime=os.path.getmtime(path), category=category,
    )


def test_extract_candidates_happy_path():
    tmp = tempfile.mkdtemp(prefix="engram-note-")
    sink = []

    def fake(req, timeout=None):
        sink.append(req)
        return _FakeResp(_openai_body(GOOD))

    restore = _with_urlopen(fake)
    try:
        out = vault_ingest.extract_candidates(_note(tmp), CFG)
    finally:
        restore()
        shutil.rmtree(tmp, ignore_errors=True)
    check("candidates extracted", len(out) == 2, out)
    check("source path carried through", out[0].source_path == "Weekly/Physics/note.md")
    check("category carried through", out[0].category == "Physics")
    check("one request made", len(sink) == 1, len(sink))
    sent = json.loads(sink[0].data.decode("utf-8"))["messages"][0]["content"]
    check("note text reached the model", "Light bends at an interface." in sent)


def test_extract_retries_once_on_malformed_then_succeeds():
    tmp = tempfile.mkdtemp(prefix="engram-note-")
    bodies = [_openai_body("sorry, no JSON here"), _openai_body(GOOD)]
    sent = []

    def fake(req, timeout=None):
        sent.append(json.loads(req.data.decode("utf-8"))["messages"][0]["content"])
        return _FakeResp(bodies[len(sent) - 1])

    restore = _with_urlopen(fake)
    try:
        out = vault_ingest.extract_candidates(_note(tmp), CFG)
    finally:
        restore()
        shutil.rmtree(tmp, ignore_errors=True)
    check("retry recovered the extraction", len(out) == 2, out)
    check("two attempts made", len(sent) == 2, len(sent))
    check("retry used the emphasized prompt", "valid JSON" in sent[1], sent[1][-160:])


def test_extract_gives_up_after_retries_without_raising():
    tmp = tempfile.mkdtemp(prefix="engram-note-")
    restore = _with_urlopen(lambda req, timeout=None: _FakeResp(_openai_body("nope")))
    try:
        out = vault_ingest.extract_candidates(_note(tmp), CFG)
    finally:
        restore()
        shutil.rmtree(tmp, ignore_errors=True)
    check("persistent garbage → [] (never raises)", out == [], out)


def test_extract_transport_failure_yields_no_candidates():
    # The vault-side equivalent of fallback-to-original-card: a dead or
    # unauthorized provider must leave the collection untouched, not raise.
    tmp = tempfile.mkdtemp(prefix="engram-note-")

    def _refuse(req, timeout=None):
        raise urllib.error.URLError(ConnectionRefusedError("refused"))

    restore = _with_urlopen(_refuse)
    try:
        out = vault_ingest.extract_candidates(_note(tmp), CFG)
    finally:
        restore()
        shutil.rmtree(tmp, ignore_errors=True)
    check("dead provider → [] (never raises)", out == [], out)


def test_extract_unauthorized_cloud_provider_yields_no_candidates():
    tmp = tempfile.mkdtemp(prefix="engram-note-")
    cfg = dict(CFG, extraction_provider="claude", extraction_model="", providers={
        "local": {"api_format": "openai", "api_base": "http://x/v1"},
        "claude": {
            "api_format": "anthropic", "api_key": "sk-bad",
            "model": "claude-haiku-4-5-20251001",
        },
    })

    def _unauthorized(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    restore = _with_urlopen(_unauthorized)
    try:
        out = vault_ingest.extract_candidates(_note(tmp), cfg)
    finally:
        restore()
        shutil.rmtree(tmp, ignore_errors=True)
    check("401 from the cloud provider → [] (never raises)", out == [], out)


def test_extract_routes_to_the_configured_provider():
    tmp = tempfile.mkdtemp(prefix="engram-note-")
    cfg = dict(CFG, extraction_provider="claude", extraction_model="", providers={
        "local": {"api_format": "openai", "api_base": "http://x/v1"},
        "claude": {
            "api_format": "anthropic", "api_key": "sk-ant-x",
            "model": "claude-haiku-4-5-20251001",
        },
    })
    sink = []

    def fake(req, timeout=None):
        sink.append(req)
        return _FakeResp(json.dumps({"content": [{"type": "text", "text": GOOD}]}))

    restore = _with_urlopen(fake)
    try:
        out = vault_ingest.extract_candidates(_note(tmp), cfg)
    finally:
        restore()
        shutil.rmtree(tmp, ignore_errors=True)
    check("extraction ran on the named provider", len(out) == 2, out)
    check("hit the Messages API", sink[0].full_url.endswith("/v1/messages"), sink[0].full_url)
    check("used the provider's default model",
          json.loads(sink[0].data.decode("utf-8"))["model"] == "claude-haiku-4-5-20251001")


def test_extract_empty_note_makes_no_request():
    tmp = tempfile.mkdtemp(prefix="engram-note-")
    path = os.path.join(tmp, "empty.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("---\ntags: [x]\n---\n\n")
    note = vault_ingest.VaultNote(
        rel_path="empty.md", abs_path=path,
        mtime=os.path.getmtime(path), category="Physics",
    )
    calls = []

    def fake(req, timeout=None):
        calls.append(req)
        return _FakeResp(_openai_body(GOOD))

    restore = _with_urlopen(fake)
    try:
        out = vault_ingest.extract_candidates(note, CFG)
    finally:
        restore()
        shutil.rmtree(tmp, ignore_errors=True)
    check("empty note → no candidates", out == [], out)
    check("empty note → no tokens spent", calls == [], calls)


def test_extract_without_a_model_is_reported_not_attempted():
    tmp = tempfile.mkdtemp(prefix="engram-note-")
    cfg = dict(CFG, extraction_model="", providers={
        "local": {"api_format": "openai", "api_base": "http://x/v1"},
    })
    calls = []

    def fake(req, timeout=None):
        calls.append(req)
        return _FakeResp(_openai_body(GOOD))

    restore = _with_urlopen(fake)
    try:
        out = vault_ingest.extract_candidates(_note(tmp), cfg)
    finally:
        restore()
        shutil.rmtree(tmp, ignore_errors=True)
    check("no model configured → []", out == [], out)
    check("no model configured → no request", calls == [], calls)


# --------------------- shipped config ---------------------

def test_shipped_config_has_vault_keys():
    """Global vault keys ship in config.json; the per-profile ones (path, globs,
    category map) ship as profile_store defaults and must NOT be in config.json,
    or Anki's config editor would show keys that no longer do anything."""
    from ai_srs import profile_store

    path = os.path.join(
        os.path.dirname(__file__), "..", "src", "ai_srs", "config.json"
    )
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    for key in ("extraction_provider", "max_cards_per_note"):
        check(f"shipped config has {key}", key in cfg, list(cfg))
    for key in ("vault_path", "vault_include_globs", "vault_category_map",
                "deck_category_map"):
        check(f"{key} is per-profile, not in config.json",
              key not in cfg and key in profile_store.PER_PROFILE_DEFAULTS)
    defaults = profile_store.PER_PROFILE_DEFAULTS
    check("ships no personal vault path", defaults["vault_path"] == "")
    check("ships an empty category map", defaults["vault_category_map"] == {})
    check("ships the agreed cap", cfg["max_cards_per_note"] == 15)


def test_prompt_keeps_an_equation_on_one_card():
    """Phase 2 item 8: extraction split `U = mgh` into one card for the
    formula plus one per symbol. The prompt now asks for one holistic card per
    equation (symbols explained on the back) and separate symbol cards only
    when the note treats the symbol as a concept of its own."""
    prompt = vault_ingest.build_extraction_prompt("U = mgh", category="Physics")
    check("prompt: one card per equation", "ONE card per equation" in prompt, prompt)
    check("prompt: symbols explained on the back", "each symbol" in prompt, prompt)
    check("prompt: no per-symbol cards by default",
          "separate card for a single symbol" in prompt, prompt)


if __name__ == "__main__":
    for fn in list(globals().values()):
        if callable(fn) and getattr(fn, "__name__", "").startswith("test_"):
            fn()
    print(f"\n{_passed}/{_passed + _failed} passed")
    sys.exit(1 if _failed else 0)

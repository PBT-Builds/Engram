"""The interactive vault picker's logic (tree, selection, routing), offline.

Everything the picker *decides*, which `vault_picker.py` deliberately holds none
of — it renders a tree from here and reports checked paths back.

  - **Tree building** — nesting flat rel_paths, folders-before-files ordering,
    and a real walk that honours `SKIP_DIRS`, `vault_include_globs`, and the
    file cap.
  - **Selection** — the signed-off definition: a checked folder means every
    markdown file at or below it, **recursively**, resolved to a concrete
    snapshot. Plus `minimal_roots` collapsing redundant checks.
  - **Routing** — `selection_to_vault_config`, the seam that lets the existing
    `scan_vault` do all the work: narrowed globs plus a category overlay. This
    is where the "an exact path is a valid glob" claim is proven against the
    real matcher, and where the empty-selection guard is locked down.
  - **Pre-fill** — `vault_category_map` demoted to a default, and the
    known-category list the combo box offers.
  - **Remember** — the opt-in config write, merged not replaced.

Standalone: `python3 tests/test_vault_select.py` (no Anki / Qt / network).
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ai_srs import vault_ingest, vault_select  # noqa: E402

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

SAMPLE = [
    "Inbox/reading.md",
    "Weekly/CS/algorithms.md",
    "Weekly/Physics/labs/lab-3.md",
    "Weekly/Physics/week-11.md",
    "Weekly/Physics/week-12.md",
    "index.md",
]


def _tree(paths=None):
    paths = SAMPLE if paths is None else paths
    return vault_select.VaultTree(
        vault_select.build_tree_from_paths(paths), tuple(paths), False
    )


class _Vault:
    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="engram-picker-")

    def write(self, rel, text="# note\n"):
        path = os.path.join(self.root, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def cfg(self, **over):
        base = {"vault_path": self.root}
        base.update(over)
        return vault_ingest.resolve_vault_config(base)

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)


def _names(nodes):
    return [n.name for n in nodes]


def _find(node, rel):
    if node.rel_path == rel:
        return node
    for child in node.children:
        hit = _find(child, rel)
        if hit is not None:
            return hit
    return None


# --------------------- tree building ---------------------

def test_paths_nest_into_a_tree():
    root = vault_select.build_tree_from_paths(SAMPLE)
    check("root is a synthetic dir", root.is_dir and root.rel_path == "")
    weekly = _find(root, "Weekly")
    check("Weekly is a folder", weekly is not None and weekly.is_dir)
    physics = _find(root, "Weekly/Physics")
    check("nested folder found", physics is not None and physics.is_dir)
    labs = _find(root, "Weekly/Physics/labs")
    check("doubly-nested folder found", labs is not None and labs.is_dir)
    leaf = _find(root, "Weekly/Physics/week-11.md")
    check("file leaf is not a dir", leaf is not None and not leaf.is_dir)
    check("leaf keeps its bare name", leaf.name == "week-11.md", leaf.name)


def test_folders_sort_before_files():
    root = vault_select.build_tree_from_paths(SAMPLE)
    check(
        "top level: folders then files",
        _names(root.children) == ["Inbox", "Weekly", "index.md"],
        _names(root.children),
    )
    physics = _find(root, "Weekly/Physics")
    check(
        "within a folder too",
        _names(physics.children) == ["labs", "week-11.md", "week-12.md"],
        _names(physics.children),
    )


def test_iter_files_returns_every_leaf():
    root = vault_select.build_tree_from_paths(SAMPLE)
    check("all files, no folders", sorted(root.iter_files()) == sorted(SAMPLE))
    physics = _find(root, "Weekly/Physics")
    check(
        "a subtree yields only its own",
        sorted(physics.iter_files()) == [
            "Weekly/Physics/labs/lab-3.md",
            "Weekly/Physics/week-11.md",
            "Weekly/Physics/week-12.md",
        ],
        sorted(physics.iter_files()),
    )


def test_backslashes_and_empty_input():
    root = vault_select.build_tree_from_paths(["Weekly\\Physics\\w.md"])
    check("windows separators normalised", _find(root, "Weekly/Physics/w.md") is not None)
    empty = vault_select.build_tree_from_paths([])
    check("empty input → empty tree", empty.children == (), empty.children)


# --------------------- tree from disk ---------------------

def test_build_tree_walks_the_real_vault():
    v = _Vault()
    try:
        for rel in SAMPLE:
            v.write(rel)
        v.write(".obsidian/workspace.md")     # pruned
        v.write(".trash/deleted.md")          # pruned
        v.write("Weekly/notes.txt")           # not markdown
        tree = vault_select.build_tree(v.cfg())
        check("finds every real note", sorted(tree.files) == sorted(SAMPLE), tree.files)
        check("file_count matches", tree.file_count == len(SAMPLE), tree.file_count)
        check("not truncated", tree.truncated is False)
        check("no .obsidian entry", not any(".obsidian" in f for f in tree.files))
        check("no .trash entry", not any(".trash" in f for f in tree.files))
        check("no .txt entry", not any(f.endswith(".txt") for f in tree.files))
    finally:
        v.close()


def test_build_tree_honours_include_globs():
    v = _Vault()
    try:
        for rel in SAMPLE:
            v.write(rel)
        tree = vault_select.build_tree(
            v.cfg(vault_include_globs=["Weekly/**/*.md"])
        )
        check(
            "only the glob's matches are offered",
            sorted(tree.files) == [
                "Weekly/CS/algorithms.md",
                "Weekly/Physics/labs/lab-3.md",
                "Weekly/Physics/week-11.md",
                "Weekly/Physics/week-12.md",
            ],
            tree.files,
        )
        check("index.md excluded", "index.md" not in tree.files)
    finally:
        v.close()


def test_build_tree_is_capped():
    v = _Vault()
    saved = vault_select.MAX_TREE_FILES
    vault_select.MAX_TREE_FILES = 3
    try:
        for i in range(10):
            v.write(f"Notes/n{i:02d}.md")
        tree = vault_select.build_tree(v.cfg())
        check("capped at the limit", tree.file_count == 3, tree.file_count)
        check("and says so", tree.truncated is True)
    finally:
        vault_select.MAX_TREE_FILES = saved
        v.close()


def test_build_tree_never_raises():
    empty = vault_select.build_tree(vault_ingest.resolve_vault_config({}))
    check("unconfigured → empty tree", empty.is_empty, empty.files)
    missing = vault_select.build_tree(
        vault_ingest.resolve_vault_config({"vault_path": "/no/such/vault/here"})
    )
    check("missing dir → empty tree", missing.is_empty, missing.files)


def test_picker_and_scan_see_the_same_files():
    """The picker must never offer a file the scan would skip, or hide one it
    would find — they share the walk deliberately."""
    v = _Vault()
    try:
        for rel in SAMPLE:
            v.write(rel)
        v.write(".obsidian/x.md")
        vcfg = v.cfg()
        picker = sorted(vault_select.build_tree(vcfg).files)
        scan = sorted(
            rel for rel, _abs in vault_ingest._iter_markdown_files(
                vcfg.path, vcfg.include_globs
            )
        )
        check("identical file sets", picker == scan, (picker, scan))
    finally:
        v.close()


# --------------------- selection resolution ---------------------

def test_checking_a_folder_is_recursive():
    got = vault_select.resolve_selection(SAMPLE, ["Weekly/Physics"])
    check(
        "every file at or below, any depth",
        got == (
            "Weekly/Physics/labs/lab-3.md",
            "Weekly/Physics/week-11.md",
            "Weekly/Physics/week-12.md",
        ),
        got,
    )
    check("nested subfolder included", "Weekly/Physics/labs/lab-3.md" in got)
    check("sibling folder excluded", "Weekly/CS/algorithms.md" not in got)


def test_checking_a_parent_takes_everything_under_it():
    got = vault_select.resolve_selection(SAMPLE, ["Weekly"])
    check("all four Weekly notes", len(got) == 4, got)
    check("Inbox untouched", "Inbox/reading.md" not in got)
    check("root file untouched", "index.md" not in got)


def test_checking_individual_files():
    got = vault_select.resolve_selection(SAMPLE, ["index.md", "Inbox/reading.md"])
    check("exactly those two", got == ("Inbox/reading.md", "index.md"), got)


def test_mixed_folder_and_file_selection():
    got = vault_select.resolve_selection(SAMPLE, ["Weekly/CS", "index.md"])
    check("union of both", got == ("Weekly/CS/algorithms.md", "index.md"), got)


def test_selection_edge_cases():
    check("nothing checked → nothing", vault_select.resolve_selection(SAMPLE, []) == ())
    check("None checked → nothing", vault_select.resolve_selection(SAMPLE, None) == ())
    check(
        "blank entries ignored",
        vault_select.resolve_selection(SAMPLE, ["", "   ", None]) == (),
    )
    check(
        "a prefix that is not a folder boundary does not match",
        vault_select.resolve_selection(SAMPLE, ["Week"]) == (),
        "Week must not capture Weekly/…",
    )
    check(
        "duplicate checks don't duplicate files",
        vault_select.resolve_selection(SAMPLE, ["Weekly/CS", "Weekly/CS/algorithms.md"])
        == ("Weekly/CS/algorithms.md",),
    )
    check(
        "trailing slashes tolerated",
        vault_select.resolve_selection(SAMPLE, ["Weekly/CS/"])
        == ("Weekly/CS/algorithms.md",),
    )


def test_minimal_roots_collapses_redundancy():
    check(
        "child dropped when parent is checked",
        vault_select.minimal_roots(["Weekly", "Weekly/Physics"]) == ("Weekly",),
    )
    check(
        "file dropped when its folder is checked",
        vault_select.minimal_roots(["Weekly/CS", "Weekly/CS/algorithms.md"])
        == ("Weekly/CS",),
    )
    check(
        "independent roots both kept",
        vault_select.minimal_roots(["Inbox", "Weekly/CS"]) == ("Inbox", "Weekly/CS"),
    )
    check(
        "a name that merely shares a prefix is kept",
        vault_select.minimal_roots(["Week", "Weekly"]) == ("Week", "Weekly"),
    )


# --------------------- ImportSelection ---------------------

def test_make_selection_resolves_and_minimises():
    sel = vault_select.make_selection(
        _tree(), ["Weekly/Physics", "Weekly/Physics/labs"], "Physics", "Physics::Core"
    )
    check("roots collapsed", sel.roots == ("Weekly/Physics",), sel.roots)
    check("three files", sel.file_count == 3, sel.rel_paths)
    check("category kept", sel.category == "Physics")
    check("deck kept", sel.deck == "Physics::Core")
    check("valid", sel.valid is True)
    check("force defaults off", sel.force is False)
    check("remember defaults off", sel.remember is False)


def test_selection_validity():
    tree = _tree()
    check(
        "no files → invalid",
        vault_select.make_selection(tree, [], "Physics").valid is False,
    )
    check(
        "no category → invalid",
        vault_select.make_selection(tree, ["Weekly"], "  ").valid is False,
    )
    check(
        "files + category → valid even with no deck",
        vault_select.make_selection(tree, ["Weekly"], "Physics").valid is True,
    )


def test_category_map_is_keyed_on_roots_not_files():
    """A 300-file folder must not produce a 300-entry map — `category_for_note`
    scans the whole map per note."""
    sel = vault_select.make_selection(_tree(), ["Weekly"], "Physics")
    check("four files selected", sel.file_count == 4, sel.rel_paths)
    check("but one map entry", sel.category_map() == {"Weekly": "Physics"}, sel.category_map())


def test_category_map_falls_back_to_files():
    sel = vault_select.ImportSelection(
        rel_paths=("a.md", "b.md"), roots=(), category="X"
    )
    check(
        "no roots → per-file keys",
        sel.category_map() == {"a.md": "X", "b.md": "X"},
        sel.category_map(),
    )


# --------------------- routing (the seam) ---------------------

def test_selection_narrows_the_scan_config():
    vcfg = vault_ingest.resolve_vault_config(
        {"vault_path": "/v", "vault_include_globs": ["**/*.md"]}
    )
    sel = vault_select.make_selection(_tree(), ["Weekly/CS"], "ComputerScience")
    derived = vault_select.selection_to_vault_config(vcfg, sel)
    check(
        "globs become the selected files",
        derived.include_globs == ("Weekly/CS/algorithms.md",),
        derived.include_globs,
    )
    check(
        "category map is the overlay",
        derived.category_map == {"Weekly/CS": "ComputerScience"},
        derived.category_map,
    )
    check("vault path preserved", derived.path == vcfg.path)
    check("max_cards preserved", derived.max_cards_per_note == vcfg.max_cards_per_note)
    check("extraction provider preserved", derived.extraction_provider == vcfg.extraction_provider)
    check("original config untouched", vcfg.include_globs == ("**/*.md",))


def test_an_exact_path_is_a_valid_glob():
    """The claim the whole seam rests on, checked against the real matcher."""
    for path in SAMPLE:
        check(
            f"exact path matches itself: {path}",
            vault_ingest.matches_globs(path, (path,)),
        )
    check(
        "and matches nothing else",
        not vault_ingest.matches_globs("Weekly/Physics/week-12.md",
                                       ("Weekly/Physics/week-11.md",)),
    )
    check(
        "a bracket in a filename is literal, not a character class",
        vault_ingest.matches_globs("Notes/[draft] plan.md", ("Notes/[draft] plan.md",)),
    )


def test_derived_config_selects_exactly_the_picked_files():
    """End-to-end against the real matcher: the derived globs admit every picked
    file and nothing else in the vault."""
    sel = vault_select.make_selection(_tree(), ["Weekly/Physics"], "Physics")
    derived = vault_select.selection_to_vault_config(
        vault_ingest.resolve_vault_config({"vault_path": "/v"}), sel
    )
    admitted = [p for p in SAMPLE if vault_ingest.matches_globs(p, derived.include_globs)]
    check("admits exactly the selection", tuple(admitted) == sel.rel_paths, admitted)


def test_every_selected_note_gets_the_picked_category():
    """The point of the overlay: a selection can never be dropped as unmapped."""
    sel = vault_select.make_selection(_tree(), ["Weekly"], "Physics")
    derived = vault_select.selection_to_vault_config(
        vault_ingest.resolve_vault_config({"vault_path": "/v"}), sel
    )
    cats = [
        vault_ingest.category_for_note(derived.category_map, p) for p in sel.rel_paths
    ]
    check("all mapped", all(c == "Physics" for c in cats), cats)
    check("none unmapped", None not in cats, cats)


def test_overlay_ignores_the_configured_map():
    """An ad-hoc pick overrides `vault_category_map` for that import."""
    vcfg = vault_ingest.resolve_vault_config({
        "vault_path": "/v",
        "vault_category_map": {"Weekly/Physics": "OldCategory"},
    })
    sel = vault_select.make_selection(_tree(), ["Weekly/Physics"], "NewCategory")
    derived = vault_select.selection_to_vault_config(vcfg, sel)
    got = vault_ingest.category_for_note(
        derived.category_map, "Weekly/Physics/week-11.md"
    )
    check("the picked category wins", got == "NewCategory", got)
    check("old key is gone from the overlay", "OldCategory" not in derived.category_map.values())


def test_empty_selection_is_refused_not_silently_widened():
    """An empty `include_globs` means 'match everything' to `matches_globs`, so
    a bad selection must raise rather than extract from the whole vault."""
    vcfg = vault_ingest.resolve_vault_config({"vault_path": "/v"})
    for bad in (
        vault_select.ImportSelection(),
        vault_select.ImportSelection(rel_paths=("a.md",), category="  "),
        vault_select.ImportSelection(rel_paths=(), category="Physics"),
    ):
        try:
            vault_select.selection_to_vault_config(vcfg, bad)
        except ValueError:
            check("invalid selection raises ValueError", True)
        else:
            check("invalid selection raises ValueError", False, "no raise!")
    check(
        "and the danger is real: empty globs match everything",
        vault_ingest.matches_globs("anything/at/all.md", ()),
    )


# --------------------- destination ---------------------

class _Session:
    def __init__(self, categories):
        self._categories = list(categories)
        self.destinations = {c: f"{c}::Original" for c in categories}

    @property
    def categories(self):
        return self._categories


def test_picked_deck_overrides_the_session_destination():
    session = _Session(["Physics"])
    sel = vault_select.make_selection(_tree(), ["Weekly"], "Physics", "MyDeck::Notes")
    changed = vault_select.apply_destination(session, sel)
    check("one category retargeted", changed == 1, changed)
    check("deck applied", session.destinations["Physics"] == "MyDeck::Notes")


def test_no_deck_leaves_the_session_alone():
    session = _Session(["Physics"])
    sel = vault_select.make_selection(_tree(), ["Weekly"], "Physics", "   ")
    check("nothing changed", vault_select.apply_destination(session, sel) == 0)
    check(
        "existing default preserved",
        session.destinations["Physics"] == "Physics::Original",
    )


# --------------------- pre-fill ---------------------

def test_prefill_reads_the_configured_map():
    cmap = {"Weekly/Physics": "Physics", "Weekly/CS": "ComputerScience"}
    check(
        "exact folder key",
        vault_select.prefill_category(cmap, ["Weekly/Physics"]) == "Physics",
    )
    check(
        "nested under a mapped folder",
        vault_select.prefill_category(cmap, ["Weekly/Physics/labs"]) == "Physics",
    )
    check(
        "unmapped folder → blank, not an error",
        vault_select.prefill_category(cmap, ["Inbox"]) == "",
    )
    check(
        "conflicting roots → blank (no honest single answer)",
        vault_select.prefill_category(cmap, ["Weekly/Physics", "Weekly/CS"]) == "",
    )
    check(
        "agreeing roots → that category",
        vault_select.prefill_category(cmap, ["Weekly/Physics", "Weekly/Physics/labs"])
        == "Physics",
    )
    check("empty map → blank", vault_select.prefill_category({}, ["Weekly"]) == "")
    check("no roots → blank", vault_select.prefill_category(cmap, []) == "")


def test_known_categories_gathers_every_source():
    cfg = {
        "models_by_subject": {"Japanese": "qwen2.5:7b", "default": "qwen2.5:7b"},
        "card_types_by_subject": {"Physics": "knowledge"},
        "subjects": ["History"],
        "deck_category_map": {"Chem::Core": "Chemistry"},
        "vault_category_map": {"Weekly/Bio": "Biology"},
    }
    got = vault_select.known_categories(cfg)
    check(
        "all five, sorted, no 'default'",
        got == ["Biology", "Chemistry", "History", "Japanese", "Physics"],
        got,
    )
    check("malformed config → empty, no raise", vault_select.known_categories({}) == [])
    check(
        "wrong types ignored",
        vault_select.known_categories(
            {"models_by_subject": "nope", "subjects": 7}
        ) == [],
    )


def test_category_is_configured():
    cfg = {"models_by_subject": {"Physics": "phi4"}}
    check("known category", vault_select.category_is_configured(cfg, "Physics") is True)
    check("new category", vault_select.category_is_configured(cfg, "Chemistry") is False)
    check("blank", vault_select.category_is_configured(cfg, "") is False)
    check("'default' is not a category", vault_select.category_is_configured(
        {"models_by_subject": {"default": "x"}}, "default") is False)


def test_describe_extraction_names_provider_and_model():
    class _Prov:
        name = "claude"
        model = "claude-haiku-4-5-20251001"

    vcfg = vault_ingest.resolve_vault_config({"vault_path": "/v"})
    line = vault_select.describe_extraction(vcfg, _Prov())
    check("names the provider", "claude" in line, line)
    check("names the provider's model", "claude-haiku-4-5-20251001" in line, line)

    override = vault_ingest.resolve_vault_config(
        {"vault_path": "/v", "extraction_model": "some-other-model"}
    )
    line = vault_select.describe_extraction(override, _Prov())
    check("extraction_model wins when set", "some-other-model" in line, line)


# --------------------- remember ---------------------

def test_remember_is_opt_in():
    sel = vault_select.make_selection(_tree(), ["Weekly/Physics"], "Physics")
    check("off by default → nothing persisted", sel.remember_entries() == {})
    check("and merge returns None", vault_select.merge_remembered({}, sel) is None)


def test_remember_merges_into_the_existing_map():
    sel = vault_select.make_selection(
        _tree(), ["Weekly/Physics"], "Physics", remember=True
    )
    check(
        "entry built from the roots",
        sel.remember_entries() == {"Weekly/Physics": "Physics"},
        sel.remember_entries(),
    )
    cfg = {"vault_category_map": {"Weekly/CS": "ComputerScience"}}
    merged = vault_select.merge_remembered(cfg, sel)
    check(
        "existing entries kept",
        merged == {"Weekly/CS": "ComputerScience", "Weekly/Physics": "Physics"},
        merged,
    )
    check("config not mutated in place", cfg["vault_category_map"] == {"Weekly/CS": "ComputerScience"})


def test_remember_overwrites_a_stale_entry_for_the_same_folder():
    sel = vault_select.make_selection(
        _tree(), ["Weekly/Physics"], "NewName", remember=True
    )
    merged = vault_select.merge_remembered(
        {"vault_category_map": {"Weekly/Physics": "OldName"}}, sel
    )
    check("re-pointed", merged == {"Weekly/Physics": "NewName"}, merged)


def test_remember_survives_a_malformed_existing_map():
    sel = vault_select.make_selection(_tree(), ["Weekly"], "Physics", remember=True)
    merged = vault_select.merge_remembered({"vault_category_map": "not a dict"}, sel)
    check("replaced with a clean map", merged == {"Weekly": "Physics"}, merged)


# --------------------- the pipeline is untouched ---------------------

def test_existing_pipeline_functions_are_unchanged():
    """Guard against this feature quietly reaching into the scan/extract path:
    the picker adds a config-shaped seam and nothing else."""
    import inspect

    src = inspect.getsource(vault_ingest.scan_vault)
    check("scan_vault does not know about selections", "selection" not in src.lower())
    src = inspect.getsource(vault_ingest.extract_candidates)
    check("extract_candidates does not know about selections", "selection" not in src.lower())
    check(
        "vault_select does not import vault_import (no circular dependency)",
        "vault_import" not in inspect.getsource(vault_select),
    )


if __name__ == "__main__":
    for fn in list(globals().values()):
        if callable(fn) and getattr(fn, "__name__", "").startswith("test_"):
            fn()
    print(f"\n{_passed}/{_passed + _failed} passed")
    sys.exit(1 if _failed else 0)

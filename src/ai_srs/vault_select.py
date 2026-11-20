"""Choosing WHAT to import from the vault, before anything is scanned.

The front end of vault ingestion: browse the vault as a tree, pick folders or
individual files, and name the category + destination deck for that one import
— instead of pre-editing `vault_category_map` for every folder.

Everything downstream is untouched. The picker's whole output is an
`ImportSelection`, which `selection_to_vault_config` turns into a *derived*
`VaultConfig`:

    include_globs = the selected files      (an exact path IS a valid glob)
    category_map  = {picked root: category} (overlays the configured map)

`scan_vault(derived, conn, force=...)` then runs exactly as it does today, and
so does extraction, the review dialog, and the import. No scanning, extraction,
or import code needed changing to support this — the narrowing is expressed
entirely in the config object those functions already take.

Two consequences worth stating, because they are the point of the feature:

- **A selection can never be dropped as "unmapped".** `mapped_notes()` filters
  out `category is None`; the overlay gives every selected file the picked
  category, so an explicit choice is always honoured.
- **`vault_category_map` becomes an optional default, not a prerequisite.** It
  pre-fills the picker's category box and is otherwise ignored for that import.

Pure / importable: stdlib only, no Anki and no Qt. The Qt dialog
(`vault_picker.py`) renders a tree from here and reports back which paths were
checked; it holds no decision logic, exactly as `vault_dialog.py` holds none.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Iterator, Optional

from . import vault_ingest
from .log import get_logger

log = get_logger()

# Upper bound on how many files the picker will render. A pathological vault
# (or a `**/*.md` glob over a synced folder of thousands of notes) should report
# that it was truncated, not freeze the UI building tree items. Well above any
# realistic hand-maintained vault.
MAX_TREE_FILES = 5000


# --------------------------------------------------------------------------
# the tree
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class TreeNode:
    """One folder or markdown file in the picker's tree.

    `rel_path` is vault-relative and '/'-separated — the same key the scan and
    the sidecar ledger use, so a node maps to a scanned note with no
    translation. The synthetic root has `rel_path == ""`.
    """
    rel_path: str
    name: str
    is_dir: bool
    children: tuple = ()

    def iter_files(self) -> Iterator[str]:
        """Every file rel_path at or below this node, depth-first."""
        if not self.is_dir:
            yield self.rel_path
            return
        for child in self.children:
            yield from child.iter_files()


@dataclass(frozen=True)
class VaultTree:
    root: TreeNode
    files: tuple = ()
    truncated: bool = False      # hit MAX_TREE_FILES

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def is_empty(self) -> bool:
        return not self.files


def _insert(tree: dict, parts) -> None:
    """Insert a split path into a nested `{name: dict | None}` map. `None` marks
    a leaf (a file)."""
    head = parts[0]
    if len(parts) == 1:
        tree[head] = None
        return
    sub = tree.get(head)
    if not isinstance(sub, dict):
        sub = {}
        tree[head] = sub
    _insert(sub, parts[1:])


def _to_nodes(tree: dict, prefix: str) -> tuple:
    """Folders first, then files, each alphabetical and case-insensitive —
    matching Obsidian's own file explorer so the tree reads the way the user's
    vault looks."""
    dirs, files = [], []
    for name, sub in tree.items():
        rel = f"{prefix}/{name}" if prefix else name
        if isinstance(sub, dict):
            dirs.append(TreeNode(rel, name, True, _to_nodes(sub, rel)))
        else:
            files.append(TreeNode(rel, name, False, ()))
    dirs.sort(key=lambda n: n.name.lower())
    files.sort(key=lambda n: n.name.lower())
    return tuple(dirs + files)


def build_tree_from_paths(paths) -> TreeNode:
    """Nest a flat list of vault-relative paths into a tree. Pure — no disk."""
    tree: dict = {}
    for path in paths:
        parts = [p for p in str(path).replace("\\", "/").split("/") if p]
        if parts:
            _insert(tree, parts)
    return TreeNode("", "", True, _to_nodes(tree, ""))


def build_tree(vcfg) -> VaultTree:
    """Walk the vault and build the picker's tree.

    Deliberately reuses `vault_ingest._iter_markdown_files`, the *same* walk the
    scan performs, so the picker can never offer a file the scan would not see
    (or hide one it would). `SKIP_DIRS` pruning and `vault_include_globs` apply
    here for exactly that reason.

    Never raises: an unconfigured or missing vault path yields an empty tree,
    which the caller reports rather than crashing the menu action.
    """
    empty = VaultTree(TreeNode("", "", True, ()), (), False)
    if not getattr(vcfg, "configured", False):
        log.info("vault picker: no vault_path configured")
        return empty
    if not os.path.isdir(vcfg.path):
        log.warning("vault picker: vault_path %r is not a directory", vcfg.path)
        return empty

    paths, truncated = [], False
    try:
        for rel, _abs in vault_ingest._iter_markdown_files(
            vcfg.path, vcfg.include_globs
        ):
            if len(paths) >= MAX_TREE_FILES:
                truncated = True
                log.warning(
                    "vault picker: more than %d files match; tree truncated",
                    MAX_TREE_FILES,
                )
                break
            paths.append(rel)
    except OSError as e:  # unreadable directory mid-walk
        log.warning("vault picker: walk failed: %s", e)

    return VaultTree(build_tree_from_paths(paths), tuple(paths), truncated)


# --------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------

def _normalise(paths) -> "set[str]":
    out = set()
    for p in paths or ():
        if not isinstance(p, str):
            continue
        p = p.replace("\\", "/").strip("/").strip()
        if p:
            out.add(p)
    return out


def minimal_roots(checked) -> tuple:
    """Drop any checked path already covered by another checked path.

    Checking `Weekly` and `Weekly/Physics` is the same selection as checking
    `Weekly`; keeping both would write two redundant category-map entries and
    two redundant "remember" rows.
    """
    norm = sorted(_normalise(checked))
    return tuple(
        c for c in norm
        if not any(c != o and c.startswith(o + "/") for o in norm)
    )


def resolve_selection(files, checked) -> tuple:
    """Expand checked folders/files into the concrete files they cover.

    **A checked folder means every markdown file at or below it, at any depth**
    — the signed-off definition. It is resolved against `files` (already pruned
    by `SKIP_DIRS` and filtered by `vault_include_globs`), so those two rules
    apply without being restated here.

    This is a SNAPSHOT: the answer is the file set as it stands right now, which
    is what makes it exactly testable. Scanning happens seconds later, so a
    snapshot and a live prefix are not observably different.

    Recursion matches `category_for_note`, which already treats a map key as
    covering anything nested under it. Direct-children-only would make the
    picker and the config map disagree about what one folder name means.
    """
    marks = _normalise(checked)
    if not marks:
        return ()
    out = [
        f for f in _normalise(files)
        if f in marks or any(f.startswith(m + "/") for m in marks)
    ]
    return tuple(sorted(out))


@dataclass(frozen=True)
class ImportSelection:
    """One import's worth of decisions, made in the picker.

    `rel_paths` is the resolved snapshot (what gets scanned); `roots` is what
    the user actually checked (what gets written to the category overlay, and
    optionally remembered). Keeping both is what lets a 300-file folder
    selection produce a ONE-entry category map instead of 300.
    """
    rel_paths: tuple = ()
    roots: tuple = ()
    category: str = ""
    deck: str = ""
    force: bool = False
    remember: bool = False

    @property
    def valid(self) -> bool:
        """A selection can only be acted on with files AND a category — the
        category is what gives the cards a destination at all."""
        return bool(self.rel_paths and self.category.strip())

    @property
    def file_count(self) -> int:
        return len(self.rel_paths)

    def category_map(self) -> dict:
        """The overlay handed to `scan_vault` via the derived config.

        Keyed on the checked roots (small) rather than every resolved file,
        because `category_for_note` scans the whole map per note — a per-file
        map would make that quadratic on a large folder.
        """
        cat = self.category.strip()
        keys = self.roots or self.rel_paths
        return {k: cat for k in keys if k}

    def remember_entries(self) -> dict:
        """The `vault_category_map` rows to persist, or `{}` when the user did
        not tick *Remember*. Same keys as the overlay, so what is remembered is
        exactly what was used."""
        return dict(self.category_map()) if self.remember else {}


def make_selection(
    tree, checked, category: str, deck: str = "",
    *, force: bool = False, remember: bool = False,
) -> ImportSelection:
    """Build an `ImportSelection` from what the dialog reports as checked."""
    files = tree.files if isinstance(tree, VaultTree) else tuple(tree)
    return ImportSelection(
        rel_paths=resolve_selection(files, checked),
        roots=minimal_roots(checked),
        category=(category or "").strip(),
        deck=(deck or "").strip(),
        force=bool(force),
        remember=bool(remember),
    )


def selection_to_vault_config(vcfg, selection: ImportSelection):
    """Derive the `VaultConfig` that scans exactly this selection.

    Two substitutions, and nothing else:

    - `include_globs` becomes the selected paths. An exact rel_path is a valid
      glob under `_glob_to_regex` (only `*` and `?` are special; everything else
      is `re.escape`d), so this narrows the walk with no new matching code.
    - `category_map` becomes the picked overlay, so the configured map has no
      say in this import and nothing can be dropped as unmapped.

    Raises `ValueError` on an invalid selection **on purpose**: an empty
    `include_globs` means "match everything" to `matches_globs`, so silently
    passing one through would extract from the entire vault — an expensive
    bug. The caller checks `selection.valid` first; the dialog cannot produce
    an invalid one (OK stays disabled).
    """
    if not selection.valid:
        raise ValueError(
            "refusing to build a scan config from an empty selection "
            "(empty include_globs would match the whole vault)"
        )
    return replace(
        vcfg,
        include_globs=tuple(selection.rel_paths),
        category_map=selection.category_map(),
    )


def apply_destination(session, selection: ImportSelection) -> int:
    """Point every category in a review session at the picked deck.

    The picked deck is the user's explicit answer to "where do these go", so it
    overrides `resolve_destination_deck`'s reverse lookup. Applied to the
    session rather than inside it, so `ReviewSession` keeps working unchanged
    for any caller that has no picker. Returns how many categories were set.

    The review dialog still shows this as an editable box — it is a default the
    user chose, not a lock.
    """
    deck = (selection.deck or "").strip()
    if not deck:
        return 0
    count = 0
    for category in list(getattr(session, "categories", []) or []):
        session.destinations[category] = deck
        count += 1
    return count


# --------------------------------------------------------------------------
# pre-fill helpers
# --------------------------------------------------------------------------

def prefill_category(category_map: dict, roots) -> str:
    """The category to pre-select, read from the existing `vault_category_map`.

    This is the entire remaining role of that config key: a default, so a
    returning user whose folders are already mapped picks nothing. Empty when
    the roots disagree (no single honest answer) or none is mapped.
    """
    found = {
        vault_ingest.category_for_note(category_map or {}, r)
        for r in _normalise(roots)
    }
    found.discard(None)
    return found.pop() if len(found) == 1 else ""


def known_categories(cfg: dict) -> "list[str]":
    """Every category the config knows about, from any of the four maps that
    can define one. Offered in the picker's category box."""
    cats = set()
    for key in ("models_by_subject", "card_types_by_subject"):
        value = cfg.get(key)
        if isinstance(value, dict):
            cats |= {k for k in value if isinstance(k, str)}
    subs = cfg.get("subjects")
    if isinstance(subs, list):
        cats |= {s for s in subs if isinstance(s, str)}
    deck_map = cfg.get("deck_category_map")
    if isinstance(deck_map, dict):
        cats |= {v for v in deck_map.values() if isinstance(v, str)}
    vault_map = cfg.get("vault_category_map")
    if isinstance(vault_map, dict):
        cats |= {v for v in vault_map.values() if isinstance(v, str)}
    return sorted(c for c in cats if c and c != "default")


def category_is_configured(cfg: dict, category: str) -> bool:
    """True when the category already has generation settings.

    A brand-new one still imports fine — `models_by_subject` falls through to
    `default` and `card_types_by_subject` to `default_card_type`. This only
    decides whether the post-import summary bothers to mention *Setup
    categories…*, where its model and card type would be set.
    """
    category = (category or "").strip()
    return bool(category) and category in set(known_categories(cfg))


def describe_extraction(vcfg, prov) -> str:
    """The one-line 'this is what will do the extracting' footer.

    Shown because extraction routing is global (`extraction_provider`), not
    per-category — so the picker's category choice does NOT change it, and a
    user picking an ad-hoc category should not have to guess that.
    """
    model = (getattr(vcfg, "extraction_model", "") or "").strip()
    model = model or getattr(prov, "model", "") or "the category's model"
    return f"Extracting with:  {getattr(prov, 'name', '?')} / {model}"


def merge_remembered(cfg: dict, selection: ImportSelection) -> Optional[dict]:
    """Fold a remembered selection into `vault_category_map`, or None if there
    is nothing to persist.

    Returns the NEW map rather than writing it — the write goes through
    `menu._write_config` like every other config change, and only after the
    import is committed, matching the rule the ingestion ledger already
    follows.
    """
    entries = selection.remember_entries()
    if not entries:
        return None
    current = cfg.get("vault_category_map")
    merged = dict(current) if isinstance(current, dict) else {}
    merged.update(entries)
    return merged

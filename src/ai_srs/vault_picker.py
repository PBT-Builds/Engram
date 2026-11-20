"""The vault folder/file picker dialog — Qt only, no decisions.

Step 1 of *Import from vault…*: browse the vault, check folders or individual
files, and name the category + destination deck for this one import.

**This module holds no logic worth testing and none worth duplicating.** What a
checked folder covers, what the selection resolves to, and how it becomes a scan
config all live in `vault_select.py`, which is stdlib-only and unit-tested. This
file renders a tree, reports which paths are checked, and hands back an
`ImportSelection` — the same relationship `vault_dialog.py` has to
`vault_import.py`.

Qt is imported at module scope because this *is* the UI; `menu.py` imports it
lazily from inside the action, so the package stays importable outside Anki.

Styled to match *Setup categories…*: editable combo boxes in a `QFormLayout`,
a hint label, and a standard button box. The one new widget type in the codebase
is the tree, which is the only shape that can express "import this folder".
"""

from __future__ import annotations

from aqt.qt import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    Qt,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from . import vault_select
from .log import get_logger

log = get_logger()

_PATH_ROLE = Qt.ItemDataRole.UserRole
_ISDIR_ROLE = Qt.ItemDataRole.UserRole + 1


class VaultPickerDialog(QDialog):
    """Pick what to import, and where it goes.

    The tree's check states are the only state held here; everything else is
    read straight back out on accept via `vault_select.make_selection`.
    """

    def __init__(
        self, parent, tree, *, categories=(), deck_names=(), category_map=None,
        prefill_category="", deck_for_category=None, extraction_line="",
    ):
        super().__init__(parent)
        self._tree_model = tree
        self._deck_for_category = deck_for_category
        self._category_map = category_map or {}
        # While True, checking a folder may auto-fill the category from
        # `vault_category_map`. The moment the user types or picks one
        # themselves it goes False and we stop overwriting their choice.
        self._category_auto = not (prefill_category or "").strip()
        # Same idea for the deck: it follows the category until the user edits
        # the deck box themselves (blanking it hands control back).
        self._deck_auto = True
        self._loading = True

        self.setWindowTitle("Import from vault — choose notes")
        self.resize(760, 640)
        layout = QVBoxLayout(self)

        intro = QLabel(
            f"{tree.file_count} note(s) in your vault. Check the folders or "
            "files to import from — checking a folder includes everything "
            "inside it, at any depth."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        if tree.truncated:
            warn = QLabel(
                f"Only the first {vault_select.MAX_TREE_FILES} files are shown. "
                "Narrow `vault_include_globs` in the add-on config to see the rest."
            )
            warn.setWordWrap(True)
            layout.addWidget(warn)

        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filter by name or folder…")
        self._filter.setClearButtonEnabled(True)
        self._filter.textChanged.connect(self._apply_filter)
        layout.addWidget(self._filter)

        layout.addWidget(self._build_tree(tree))
        layout.addLayout(
            self._build_form(categories, deck_names, prefill_category)
        )

        self._force = QCheckBox("Re-scan even if unchanged")
        self._force.setToolTip(
            "Notes are normally skipped when their modification time hasn't "
            "moved since the last import. Tick this to re-extract from the "
            "selected notes anyway."
        )
        layout.addWidget(self._force)

        self._remember = QCheckBox(
            "Remember this folder → category in the add-on config"
        )
        self._remember.setToolTip(
            "Writes a `vault_category_map` entry so this folder pre-fills its "
            "category next time. Optional — imports work without it."
        )
        layout.addWidget(self._remember)

        if extraction_line:
            footer = QLabel(
                f"{extraction_line}\n"
                "(the same backend for every category — set by "
                "`extraction_provider` in the add-on config)"
            )
            footer.setWordWrap(True)
            layout.addWidget(footer)

        self._counter = QLabel("")
        layout.addWidget(self._counter)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._ok = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._ok.setText("Continue")
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)

        self._loading = False
        self._sync_deck_for_category(self._category.currentText())
        self._refresh()

    # ---------------- construction ----------------

    def _build_tree(self, tree) -> QTreeWidget:
        widget = QTreeWidget(self)
        widget.setHeaderLabels(["Vault"])
        widget.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        widget.setUniformRowHeights(True)

        self._items = []
        for node in tree.root.children:
            widget.addTopLevelItem(self._make_item(node))
        # One level open: enough to see the vault's shape without rendering a
        # deep vault fully expanded.
        widget.expandToDepth(0)

        widget.itemChanged.connect(self._on_item_changed)
        self._tree = widget
        return widget

    def _make_item(self, node) -> QTreeWidgetItem:
        item = QTreeWidgetItem([node.name])
        item.setData(0, _PATH_ROLE, node.rel_path)
        item.setData(0, _ISDIR_ROLE, node.is_dir)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(0, Qt.CheckState.Unchecked)
        if node.is_dir:
            item.setToolTip(0, f"{node.rel_path}/ — everything inside, any depth")
        else:
            item.setToolTip(0, node.rel_path)
        for child in node.children:
            item.addChild(self._make_item(child))
        self._items.append(item)
        return item

    def _build_form(self, categories, deck_names, prefill) -> QFormLayout:
        form = QFormLayout()

        self._category = QComboBox()
        self._category.setEditable(True)
        self._category.addItem("")
        self._category.addItems([c for c in categories])
        self._category.setCurrentText(prefill)
        self._category.setToolTip(
            "Routing label for this import: it names the destination deck and "
            "gives the extraction prompt its subject. Pick an existing category "
            "or type a new one — no config editing needed."
        )
        self._category.currentTextChanged.connect(self._on_category_changed)
        form.addRow("Category:", self._category)

        self._deck = QComboBox()
        self._deck.setEditable(True)
        self._deck.addItem("")
        self._deck.addItems(list(deck_names or []))
        self._deck.setToolTip(
            "Deck the accepted cards are created in. Pre-filled from your "
            "deck→category assignments; type any name to override. A deck that "
            "doesn't exist yet is created on import."
        )
        self._deck.currentTextChanged.connect(self._on_deck_changed)
        form.addRow("Destination deck:", self._deck)

        return form

    # ---------------- events ----------------

    def _on_item_changed(self, item, column) -> None:
        """Cascade a check down to descendants and recompute ancestors.

        This is the tri-state bookkeeping, and it is the only real logic in the
        file: a folder's state is derived from its children, never stored.
        """
        if self._loading:
            return
        self._loading = True
        try:
            state = item.checkState(0)
            if state != Qt.CheckState.PartiallyChecked:
                self._set_subtree(item, state)
            parent = item.parent()
            while parent is not None:
                self._recompute(parent)
                parent = parent.parent()
        finally:
            self._loading = False
        self._maybe_prefill_category()
        self._refresh()

    def _set_subtree(self, item, state) -> None:
        for i in range(item.childCount()):
            child = item.child(i)
            child.setCheckState(0, state)
            self._set_subtree(child, state)

    def _recompute(self, item) -> None:
        states = {item.child(i).checkState(0) for i in range(item.childCount())}
        if not states or states == {Qt.CheckState.Unchecked}:
            item.setCheckState(0, Qt.CheckState.Unchecked)
        elif states == {Qt.CheckState.Checked}:
            item.setCheckState(0, Qt.CheckState.Checked)
        else:
            item.setCheckState(0, Qt.CheckState.PartiallyChecked)

    def _on_category_changed(self, text: str) -> None:
        # A change we did not make ourselves is the user's own choice, and ends
        # auto-filling for the rest of the dialog.
        if not self._loading:
            self._category_auto = False
        self._sync_deck_for_category(text)
        self._refresh()

    def _on_deck_changed(self, text: str) -> None:
        # Only an edit we did not make ourselves is the user's: a typed deck
        # stops the pre-fill; blanking the box lets the category drive it again.
        if not self._loading:
            self._deck_auto = not text.strip()
        self._refresh()

    def _maybe_prefill_category(self) -> None:
        """Fill the category from `vault_category_map` for what is now checked.

        This is the whole remaining job of that config key: a returning user
        whose folders are already mapped checks a folder and the category
        appears, so they pick nothing. Never overwrites a category the user
        chose, and goes blank when the checked roots disagree — a value we
        auto-filled earlier must not linger and route the other folder's
        notes into it.
        """
        if not self._category_auto:
            return
        roots = vault_select.minimal_roots(self._checked_paths())
        guess = vault_select.prefill_category(self._category_map, roots)
        if guess == self._category.currentText().strip():
            return
        prev, self._loading = self._loading, True
        try:
            self._category.setCurrentText(guess)
        finally:
            self._loading = prev
        self._sync_deck_for_category(guess)

    def _sync_deck_for_category(self, category: str) -> None:
        """Pre-fill the deck from the category, the way *Setup categories…*
        pre-fills model and card type. Keeps following the category as it is
        typed, but never overwrites a deck the user typed."""
        if self._deck_for_category is None or not self._deck_auto:
            return
        category = (category or "").strip()
        try:
            deck = self._deck_for_category(category) if category else ""
        except Exception as e:  # noqa: BLE001 — a pre-fill must never block the dialog
            log.warning("vault picker: deck pre-fill failed: %s", e)
            return
        if (deck or "") != self._deck.currentText():
            self._loading, prev = True, self._loading
            try:
                self._deck.setCurrentText(deck or "")
            finally:
                self._loading = prev

    def _apply_filter(self, text: str) -> None:
        """Hide non-matching rows. A folder stays visible when any descendant
        matches, so filtering never hides the path to a hit.

        Check state is untouched by filtering — a hidden row stays selected,
        which is why the counter reads off the model rather than the view.
        """
        needle = (text or "").strip().lower()
        for i in range(self._tree.topLevelItemCount()):
            self._filter_item(self._tree.topLevelItem(i), needle)

    def _filter_item(self, item, needle: str) -> bool:
        path = (item.data(0, _PATH_ROLE) or "").lower()
        match = not needle or needle in path
        visible = match
        for i in range(item.childCount()):
            if self._filter_item(item.child(i), needle):
                visible = True
        item.setHidden(not visible)
        if needle and visible and item.childCount():
            item.setExpanded(True)
        return visible

    # ---------------- state ----------------

    def _checked_paths(self) -> "list[str]":
        """Every fully-checked node. Partially-checked folders are excluded and
        their checked children reported instead, so `minimal_roots` receives an
        already-sensible set."""
        return [
            item.data(0, _PATH_ROLE)
            for item in self._items
            if item.checkState(0) == Qt.CheckState.Checked
        ]

    def selection(self):
        return vault_select.make_selection(
            self._tree_model,
            self._checked_paths(),
            self._category.currentText(),
            self._deck.currentText(),
            force=self._force.isChecked(),
            remember=self._remember.isChecked(),
        )

    def _refresh(self) -> None:
        selection = self.selection()
        parts = [f"{selection.file_count} note(s) selected"]
        if selection.file_count and not selection.category.strip():
            parts.append("choose or type a category to continue")
        self._counter.setText(".  ".join(parts) + ".")
        if self._ok is not None:
            self._ok.setEnabled(selection.valid)


def run_picker_dialog(parent, tree, **kwargs):
    """Show the picker. Returns an `ImportSelection`, or None if cancelled.

    Returning the selection rather than acting on it keeps the scan and the
    extraction spend out of the dialog's event handling, where an exception
    would be swallowed by Qt — the same split `run_review_dialog` uses.
    """
    dialog = VaultPickerDialog(parent, tree, **kwargs)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        log.info("vault picker: cancelled")
        return None
    selection = dialog.selection()
    log.info(
        "vault picker: %d file(s), roots=%s, category=%r, deck=%r, "
        "force=%s, remember=%s",
        selection.file_count, list(selection.roots), selection.category,
        selection.deck, selection.force, selection.remember,
    )
    return selection

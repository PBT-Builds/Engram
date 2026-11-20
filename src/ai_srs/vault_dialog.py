"""The vault candidate review dialog (step 6) — Qt only, no decisions.

Shows what extraction proposed and lets the user accept, edit, or reject each
card before anything is written, then hands the (possibly edited) selection back
to `vault_import`.

**This module holds no logic worth testing and no logic worth duplicating.**
Every decision — which cards import, where they go, what counts as a duplicate
— lives in `vault_import.ReviewSession`, which is stdlib-only and unit-tested.
This file edits that object in response to clicks and nothing more, so a change
in import behaviour is made (and verified) in one place, not two.

Unlike the rest of the package this module imports Qt at module scope, because
it *is* the UI and cannot do anything useful without it. It is therefore
imported lazily, from inside `menu.py`'s action, which keeps the package as a
whole importable outside Anki.

Styled to match the existing *Setup categories…* dialog: a summary label, a
`QFormLayout` of editable combo boxes, then the working area and a standard
button box.
"""

from __future__ import annotations

from aqt.qt import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    Qt,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from . import vault_dialog_style
from .log import get_logger

log = get_logger()

_COL_ACCEPT = 0
_COL_FRONT = 1
_COL_BACK = 2
_COL_SOURCE = 3

# Enough height that a two-line question is readable without opening the cell.
_ROW_HEIGHT = 56
_MAX_ROW_HEIGHT = 140


class VaultReviewDialog(QDialog):
    """Accept / edit / reject the candidates in a `ReviewSession`.

    Edits are written straight back into the session as they happen, so the
    "will import" counter is always the truth and `accept()` has nothing left to
    collect. Rejecting is unchecking; editing is typing in the cell.
    """

    def __init__(self, parent, session, deck_names=(), header_lines=()):
        super().__init__(parent)
        self.session = session
        self._deck_names = list(deck_names or [])
        self._loading = True          # suppress itemChanged during table build

        self.setWindowTitle("Import from vault")
        self.resize(900, 600)
        layout = QVBoxLayout(self)

        if header_lines:
            summary = QLabel("\n".join(header_lines))
            summary.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            summary.setWordWrap(True)
            layout.addWidget(summary)

        layout.addLayout(self._build_destination_form())
        layout.addWidget(self._build_table())
        layout.addLayout(self._build_bulk_actions())

        self._counter = QLabel("")
        layout.addWidget(self._counter)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._import_button = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._import_button.setText("Import accepted")
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)

        self._loading = False
        self._refresh_counter()

    # ---------------- construction ----------------

    def _build_destination_form(self) -> QFormLayout:
        """One editable deck box per category in the batch.

        Pre-filled from `deck_category_map` and editable, so the common case is
        zero clicks and an unusual one is still possible without editing config.
        """
        form = QFormLayout()
        self._destination_boxes = {}
        for category in self.session.categories:
            box = QComboBox()
            box.setEditable(True)
            current = self.session.destinations.get(category, "")
            names = list(self._deck_names)
            if current and current not in names:
                names.insert(0, current)
            box.addItems(names)
            box.setCurrentText(current)
            box.setToolTip(
                f"Deck that accepted {category} cards are created in. Resolved "
                "from your deck→category assignments; edit to override. A deck "
                "that doesn't exist yet will be created."
            )
            box.currentTextChanged.connect(
                lambda text, c=category: self._on_destination_changed(c, text)
            )
            form.addRow(f"{category}:", box)
            self._destination_boxes[category] = box
        if not self._destination_boxes:
            form.addRow(QLabel("No candidates with a mapped category."))
        return form

    def _build_table(self) -> QTableWidget:
        table = QTableWidget(len(self.session.reviews), 4, self)
        table.setHorizontalHeaderLabels(["Import", "Front", "Back", "Source"])
        table.setWordWrap(True)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.verticalHeader().setVisible(False)
        # Explicit selected-row colours: in Anki's dark theme the default left
        # white text on a light highlight (unreadable). See vault_dialog_style.
        hl = table.palette().highlight().color()
        table.setStyleSheet(
            vault_dialog_style.selection_stylesheet((hl.red(), hl.green(), hl.blue()))
        )

        header = table.horizontalHeader()
        header.setSectionResizeMode(_COL_ACCEPT, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(_COL_FRONT, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(_COL_BACK, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(_COL_SOURCE, QHeaderView.ResizeMode.ResizeToContents)

        for row, review in enumerate(self.session.reviews):
            accept = QTableWidgetItem()
            accept.setFlags(
                Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
            )
            accept.setCheckState(
                Qt.CheckState.Checked if review.accepted else Qt.CheckState.Unchecked
            )
            table.setItem(row, _COL_ACCEPT, accept)

            table.setItem(row, _COL_FRONT, QTableWidgetItem(review.front))
            table.setItem(row, _COL_BACK, QTableWidgetItem(review.back))

            source = QTableWidgetItem(self._source_label(review))
            source.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            if review.is_duplicate:
                source.setToolTip(
                    "A note with this Front already exists in your collection "
                    f"(note id {review.duplicate_of}). Unchecked by default — "
                    "check it if you want a second copy anyway."
                )
            table.setItem(row, _COL_SOURCE, source)

            table.setRowHeight(row, _ROW_HEIGHT)

        table.resizeRowsToContents()
        for row in range(table.rowCount()):
            table.setRowHeight(row, min(table.rowHeight(row), _MAX_ROW_HEIGHT))

        table.itemChanged.connect(self._on_item_changed)
        self._table = table
        return table

    def _build_bulk_actions(self) -> QHBoxLayout:
        row = QHBoxLayout()
        accept_all = QPushButton("Accept all")
        accept_all.setToolTip(
            "Accept every candidate except the ones already in your collection."
        )
        accept_all.clicked.connect(lambda: self._set_all(True))
        row.addWidget(accept_all)

        reject_all = QPushButton("Reject all")
        reject_all.clicked.connect(lambda: self._set_all(False))
        row.addWidget(reject_all)

        row.addStretch(1)
        return row

    @staticmethod
    def _source_label(review) -> str:
        label = review.source_path or "?"
        if review.is_duplicate:
            label += "   [duplicate]"
        return label

    # ---------------- events ----------------

    def _on_item_changed(self, item) -> None:
        """Mirror a cell edit into the session. The session, not the table, is
        what the import reads."""
        if self._loading:
            return
        row = item.row()
        if row < 0 or row >= len(self.session.reviews):
            return
        review = self.session.reviews[row]
        column = item.column()
        if column == _COL_ACCEPT:
            review.accepted = item.checkState() == Qt.CheckState.Checked
        elif column == _COL_FRONT:
            review.front = item.text()
        elif column == _COL_BACK:
            review.back = item.text()
        self._refresh_counter()

    def _on_destination_changed(self, category: str, text: str) -> None:
        self.session.destinations[category] = text.strip()
        self._refresh_counter()

    def _set_all(self, accepted: bool) -> None:
        """Bulk accept/reject via the session, then re-sync the checkboxes —
        so the duplicate carve-out in `accept_all` applies here too instead of
        being re-implemented against the table."""
        if accepted:
            self.session.accept_all()
        else:
            self.session.reject_all()
        self._loading = True
        try:
            for row, review in enumerate(self.session.reviews):
                item = self._table.item(row, _COL_ACCEPT)
                if item is not None:
                    item.setCheckState(
                        Qt.CheckState.Checked if review.accepted
                        else Qt.CheckState.Unchecked
                    )
        finally:
            self._loading = False
        self._refresh_counter()

    def _refresh_counter(self) -> None:
        counts = self.session.counts()
        parts = [f"{counts['importable']} of {counts['total']} will be imported"]
        if counts["duplicates"]:
            parts.append(f"{counts['duplicates']} already in collection")
        if counts["edited"]:
            parts.append(f"{counts['edited']} edited")
        if counts["no_destination"]:
            parts.append(
                f"{counts['no_destination']} accepted but have no destination deck"
            )
        self._counter.setText(".  ".join(parts) + ".")
        if self._import_button is not None:
            self._import_button.setEnabled(counts["importable"] > 0)


def run_review_dialog(parent, session, deck_names=(), header_lines=()) -> bool:
    """Show the review dialog. Returns True if the user chose to import.

    On True the `session` carries the user's edits and selections; the caller
    performs the actual import via `vault_import.import_reviews`. Splitting it
    this way keeps the collection write out of the dialog's event handling,
    where an exception would be swallowed by Qt.
    """
    dialog = VaultReviewDialog(parent, session, deck_names, header_lines)
    accepted = dialog.exec() == QDialog.DialogCode.Accepted
    log.info(
        "vault review dialog closed: %s (%s)",
        "import" if accepted else "cancelled", session.counts(),
    )
    return accepted

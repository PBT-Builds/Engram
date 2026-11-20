"""Selection colours for the review dialog's table (pure, no Qt).

In Anki's dark theme a selected row rendered white text on a light highlight
(invisible). The dialog now sets the selected-item colours explicitly: the
palette's own highlight colour, with black or white text picked by contrast,
so it reads in both themes.
"""

from __future__ import annotations


def _luminance(rgb) -> float:
    def chan(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (chan(v) for v in rgb[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_text(rgb) -> str:
    """'#000000' or '#ffffff', whichever contrasts more with `rgb` (WCAG)."""
    lum = _luminance(rgb)
    on_black = (lum + 0.05) / 0.05
    on_white = 1.05 / (lum + 0.05)
    return "#000000" if on_black >= on_white else "#ffffff"


def selection_stylesheet(highlight_rgb) -> str:
    bg = "#%02x%02x%02x" % tuple(int(v) for v in highlight_rgb[:3])
    return (
        f"QTableView::item:selected {{ background-color: {bg}; "
        f"color: {contrast_text(highlight_rgb)}; }}"
    )

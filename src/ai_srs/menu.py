"""Tools menu integration (spec §8.1 — Step 1; §8.2 — Step 2).

Adds an `Engram` submenu under Tools with diagnostic actions:

1. *Test connection* — probes every configured provider and reports each.
2. *Generate test sentence…* — prompts for a word, calls Ollama, shows the
   result. Proves the end-to-end generation loop.
3. *Populate sidecar from deck…* — prompts for a subject + Original deck,
   scans notes read-only, and upserts one sidecar row per word. Step 2
   dev/test affordance; will move into the proper pipeline later.
4. *Create Learning card for word…* — pipeline entry (Step 3 dev affordance):
   generates a sentence, creates a tracked AI card in `Subject::Learning`.
   Answering it to FSRS retrievability ≥ threshold promotes it to Recycled.
5. *Import from vault…* — scans the configured Obsidian vault for changed
   notes, extracts candidate flashcards from each, and opens the review dialog
   (`vault_dialog`) so accepted cards can be imported as ordinary notes.

Dev/test affordances for Steps 4–5 (drive transitions without waiting for FSRS):
6. *Set recycle_count for word…* / *Set fail_streak for word…* — poke a
   tracked word's counters directly.
7. *Force PASS on current card…* / *Force FAIL on current card…* — run the
   real PASS / FAIL transition for the word's current stage (same code the
   reviewer hook runs), bypassing the FSRS gate.
"""

from __future__ import annotations

from . import generator, subjects
from .generator import GenRequest, GenerationError, generate_sentence
from .log import get_logger

log = get_logger()

# Recommended Ollama models offered in the Setup-categories dropdown (the user
# can still type any other pulled model). qwen2.5:7b serves all languages;
# phi4 is an optional stronger reasoner for knowledge subjects (~16GB RAM).
RECOMMENDED_MODELS = ["qwen2.5:7b", "phi4"]


def _config() -> dict:
    """Effective config: global add-on config + the open profile's settings."""
    from . import profile_runtime

    return profile_runtime.effective_config()


def _write_config(cfg: dict) -> None:
    """Global keys -> Anki's add-on config; per-profile keys (deck map, vault)
    -> this profile's settings.json. See profile_store."""
    from . import profile_runtime

    profile_runtime.write_config(cfg)


def _known_categories(cfg: dict, deck_map: dict) -> list[str]:
    """Categories the user has configured anywhere: model/card-type subjects,
    the `subjects` list, and any already-assigned deck. Sorted, no blanks."""
    cats = (
        set((cfg.get("models_by_subject", {}) or {}).keys())
        | set((cfg.get("card_types_by_subject", {}) or {}).keys())
        | set(cfg.get("subjects", []) or [])
        | set(deck_map.values())
    )
    return sorted(c for c in cats if c and c != "default")


def _model_choices(cfg: dict) -> list[str]:
    """Models offered in the dropdown: the recommended defaults first, then any
    other models already configured. Editable — the user can type any pulled
    model name."""
    configured = [
        m for m in (cfg.get("models_by_subject", {}) or {}).values()
        if isinstance(m, str) and m
    ]
    out: list[str] = []
    for m in RECOMMENDED_MODELS + sorted(set(configured)):
        if m not in out:
            out.append(m)
    return out


def _setup_categories_action() -> None:
    """Step 9, Part 1 (+ Task 2): assign a real deck to a category AND configure
    that category's model + card_type — so adding a brand-new subject is fully
    doable from the UI, no hand-editing of config.json.

    Persists into `deck_category_map` (deck → category), `models_by_subject`
    (category → Ollama model), `card_types_by_subject` (category → language /
    knowledge), and the `subjects` list. A deck assigned here auto-generates AI
    cards when its cards graduate to review; unassigned decks are ignored.

    UI: editable combo boxes let you pick existing values or type new ones. The
    overview shows each current assignment with its resolved model + card_type.
    """
    from aqt import mw
    from aqt.qt import (
        QComboBox,
        QDialog,
        QDialogButtonBox,
        QFormLayout,
        QLabel,
        Qt,
        QVBoxLayout,
    )
    from aqt.utils import showInfo

    cfg = _config()
    deck_map = dict(cfg.get("deck_category_map", {}) or {})
    known = _known_categories(cfg, deck_map)

    try:
        deck_names = sorted(d.name for d in mw.col.decks.all_names_and_ids())
    except Exception:
        deck_names = []

    dialog = QDialog(mw)
    dialog.setWindowTitle("Setup categories")
    layout = QVBoxLayout(dialog)

    # Overview: each assignment with the model + card_type that category resolves
    # to (so the current config is visible without opening config.json).
    if deck_map:
        rows = []
        for d, c in sorted(deck_map.items()):
            model = subjects.model_for_subject(cfg, c)
            ctype = subjects.card_type_for_subject(cfg, c)
            rows.append(f"  • {d}  →  {c}   [{ctype}, {model}]")
        current = "\n".join(rows)
    else:
        current = "  (none yet)"
    overview = QLabel(f"Current assignments:\n{current}")
    overview.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    layout.addWidget(overview)

    form = QFormLayout()

    # Deck: editable so you can pick an existing deck or type a parent prefix.
    deck_combo = QComboBox()
    deck_combo.setEditable(True)
    deck_combo.addItem("")
    deck_combo.addItems(deck_names)
    deck_combo.setCurrentText("")
    deck_combo.setToolTip("Pick an existing deck, or type a full path / parent-deck prefix.")
    form.addRow("Real deck:", deck_combo)

    # Category: dropdown of known/assigned categories; type to create a new one.
    cat_combo = QComboBox()
    cat_combo.setEditable(True)
    cat_combo.addItem("")
    cat_combo.addItems(known)
    cat_combo.setCurrentText("")
    cat_combo.setToolTip("Choose an existing category, or type a new one to create it.")
    form.addRow("Category:", cat_combo)

    # Model: recommended models + any already configured; editable for any other.
    model_combo = QComboBox()
    model_combo.setEditable(True)
    model_combo.addItems(_model_choices(cfg))
    model_combo.setToolTip(
        "Ollama model for this category. Must be pulled (`ollama pull <model>`) "
        "or generation falls back. qwen2.5:7b works for every subject; phi4 is "
        "a stronger (14B, ~16 GB RAM) option for knowledge subjects."
    )
    form.addRow("Model:", model_combo)

    # Card type: the strategy (language vs knowledge).
    ctype_combo = QComboBox()
    ctype_combo.addItems([subjects.CARD_TYPE_LANGUAGE, subjects.CARD_TYPE_KNOWLEDGE])
    ctype_combo.setToolTip(
        "language = vocab (word + example sentence); "
        "knowledge = facts (rephrased Q/A from the source card)."
    )
    form.addRow("Card type:", ctype_combo)

    layout.addLayout(form)

    hint = QLabel(
        "Pick or type a category, then set its model + card type.\n"
        "Selecting an existing deck or category pre-fills its current values."
    )
    layout.addWidget(hint)

    def _sync_for_category(category: str) -> None:
        """Pre-fill model + card_type from the category's current/resolved config
        (recommended defaults for a brand-new one)."""
        category = category.strip()
        model_combo.setCurrentText(subjects.model_for_subject(cfg, category or None))
        ctype = subjects.card_type_for_subject(cfg, category or None)
        idx = ctype_combo.findText(ctype)
        if idx >= 0:
            ctype_combo.setCurrentIndex(idx)

    # When an already-assigned deck is chosen, pre-fill its category (+ its
    # model/card_type via the category change below).
    def _on_deck_changed(text: str) -> None:
        existing = deck_map.get(text.strip())
        if existing:
            cat_combo.setCurrentText(existing)

    deck_combo.currentTextChanged.connect(_on_deck_changed)
    cat_combo.currentTextChanged.connect(_sync_for_category)
    _sync_for_category("")  # seed with recommended defaults

    buttons = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
    )
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)

    if dialog.exec() != QDialog.DialogCode.Accepted:
        return

    deck_name = deck_combo.currentText().strip()
    category = cat_combo.currentText().strip()
    if not deck_name or not category:
        return
    model = model_combo.currentText().strip()
    card_type = ctype_combo.currentText().strip()

    # Persist all four config maps so the category is fully usable with no
    # hand-editing of config.json.
    deck_map[deck_name] = category
    cfg["deck_category_map"] = deck_map

    models = dict(cfg.get("models_by_subject", {}) or {})
    if model:
        models[category] = model
    cfg["models_by_subject"] = models

    card_types = dict(cfg.get("card_types_by_subject", {}) or {})
    if card_type in subjects.VALID_CARD_TYPES:
        card_types[category] = card_type
    cfg["card_types_by_subject"] = card_types

    subs = list(cfg.get("subjects", []) or [])
    if category not in subs:
        subs.append(category)
    cfg["subjects"] = subs

    _write_config(cfg)

    eff_model = subjects.model_for_subject(cfg, category)
    eff_ctype = subjects.card_type_for_subject(cfg, category)
    updated = "\n".join(
        f"  {d}  →  {c}   [{subjects.card_type_for_subject(cfg, c)}, "
        f"{subjects.model_for_subject(cfg, c)}]"
        for d, c in sorted(deck_map.items())
    )
    showInfo(
        f"Assigned:\n  {deck_name}  →  {category}   [{eff_ctype}, {eff_model}]\n\n"
        f"All assignments now:\n{updated}\n\n"
        f"Make sure the model is pulled:  ollama pull {eff_model}\n\n"
        "Cards in this deck will auto-generate an AI card (once each) when they "
        "first graduate from learning into review.",
        parent=mw,
    )


def _nothing_configured(cfg: dict) -> bool:
    """True for a fresh install: no decks assigned to categories AND no subjects
    configured. Used to show friendly get-started guidance instead of letting a
    new user hit silent fallbacks."""
    deck_map = cfg.get("deck_category_map", {}) or {}
    subs = subjects.subjects_from_config(cfg)
    return not deck_map and not subs


_GET_STARTED_TEXT = (
    "Welcome to Engram!\n\n"
    "This add-on watches your existing decks and, when a card first graduates "
    "from learning into review, auto-generates a fresh AI practice card in a "
    "separate deck. It never modifies your original decks.\n\n"
    "To get started:\n"
    "  1. Install a local LLM server (e.g. Ollama from ollama.com) and pull a "
    "model:\n"
    "       ollama pull qwen2.5:7b      (works for every subject)\n"
    "     Optional, for knowledge subjects on a machine with ~16 GB RAM:\n"
    "       ollama pull phi4\n"
    "  2. Tools → Engram → Test connection  — confirm it's reachable.\n"
    "  3. Tools → Engram → Setup categories…  — point a deck at a "
    "category and pick its model + card type.\n\n"
    "That's it — review as normal and AI cards appear in their own decks. See "
    "the add-on's README for full details."
)


def _setup_status_lines(cfg: dict, statuses) -> "list[str]":
    """'Your setup right now': is the default provider reachable, and is every
    model the config uses installed? Built from Test connection's statuses."""
    from . import model_check

    default = generator.default_provider_name(cfg)
    lines = []
    for st in statuses:
        if st.name != default and not model_check.models_in_use(cfg).get(st.name):
            continue
        if not st.reachable:
            if st.name != default:
                users = sorted({u for us in model_check.models_in_use(cfg)[st.name].values()
                                for u in us})
                lines.append(f"– {st.name} (only needed for {', '.join(users)}): "
                             f"not set up. {st.detail}".strip())
            else:
                lines.append(f"✗ {st.name}: not reachable. {st.detail}".strip())
            continue
        missing = model_check.missing_models(cfg, st.name, st.models)
        if missing:
            lines.extend(model_check.missing_lines(st.name, st.provider.api_format, missing))
        else:
            lines.append(f"✓ {st.name}: reachable, configured models installed.")
    return lines


def _get_started_action() -> None:
    from aqt import mw
    from aqt.utils import showInfo

    cfg = _config()
    try:
        statuses = generator.test_all_providers(cfg, timeout=5)
        status = _setup_status_lines(cfg, statuses)
    except Exception:  # never let a probe break the welcome text
        log.exception("getting started: status check failed")
        status = []
    text = _GET_STARTED_TEXT
    if status:
        text += "\n\nYour setup right now:\n" + "\n".join(status)
    showInfo(text, parent=mw, title="Engram — getting started")


def connection_status_label(st, default: str) -> str:
    """Status word for one provider in Test connection. An optional provider
    with no API key (claude, out of the box) is not a failure, so it doesn't
    get the red-flag ✗."""
    if st.reachable:
        return "reachable ✓"
    if st.name != default and generator.missing_credential(st.provider):
        return "not configured (optional)"
    return "NOT reachable ✗"


def _test_connection_action() -> None:
    """Setup affordance: probe EVERY configured provider and report each one's
    status. With a local model and a cloud provider both configured, a single
    combined verdict would be misleading — so each provider gets its own block,
    and the default provider is listed first and marked."""
    from aqt import mw
    from aqt.utils import showInfo

    from . import model_check

    cfg = _config()
    statuses = generator.test_all_providers(cfg, timeout=5)
    default = generator.default_provider_name(cfg)

    blocks = []
    for st in statuses:
        prov = st.provider
        marker = "  (default)" if st.name == default else ""
        status = connection_status_label(st, default)
        lines = [
            f"── {st.name}{marker} ──",
            f"Endpoint:  {prov.api_base}",
            f"Format:    {prov.api_format}",
            f"Status:    {status}",
            st.detail,
        ]
        if prov.model:
            lines.insert(3, f"Model:     {prov.model}")
        if st.models:
            shown = ", ".join(st.models[:20])
            more = "" if len(st.models) <= 20 else f"  (+{len(st.models) - 20} more)"
            lines.append(f"Available models: {shown}{more}")
            missing = model_check.missing_models(cfg, st.name, st.models)
            lines.extend(model_check.missing_lines(st.name, prov.api_format, missing))
        blocks.append("\n".join(lines))

    if not any(st.reachable for st in statuses):
        blocks.append(
            "No provider answered. For a local model, start your LLM server "
            "(`ollama serve`) and pull a model (`ollama pull qwen2.5:7b`). For "
            "a cloud provider, check the API key. Both are configured under "
            "`providers` in the add-on config."
        )

    showInfo("\n\n".join(blocks), parent=mw, title="Engram — Test connection")


def _populate_action() -> None:
    from aqt import mw
    from aqt.utils import getText, showInfo, showWarning

    subject, ok = getText(
        "Subject (parent deck / language tag, e.g. Japanese):", parent=mw,
    )
    if not ok or not subject.strip():
        return
    deck_name, ok = getText(
        "Original deck to scan (full path, e.g. Japanese::Vocab::Core):",
        parent=mw,
    )
    if not ok or not deck_name.strip():
        return
    cfg = _config()
    word_field = cfg.get("word_field") or None
    try:
        from . import populate as populate_mod

        report = populate_mod.populate_from_deck(
            mw.col,
            subject=subject.strip(),
            deck_name=deck_name.strip(),
            word_field=word_field,
        )
    except Exception as e:
        log.exception("populate failed")
        showWarning(f"Populate failed: {e}", parent=mw)
        return
    showInfo(
        f"Subject: {subject.strip()}\n"
        f"Deck:    {deck_name.strip()}\n"
        f"Word field: {word_field or '(first field)'}\n\n"
        f"Notes scanned:        {report.scanned}\n"
        f"Upserted into sidecar: {report.upserted}\n"
        f"Skipped (empty):      {report.skipped_empty}\n"
        f"Skipped (no field):   {report.skipped_missing_field}",
        parent=mw,
    )


def _enter_learning_action() -> None:
    from aqt import mw
    from aqt.utils import getText, showInfo, showWarning

    subject, ok = getText(
        "Subject (parent deck, e.g. Japanese):", parent=mw,
    )
    if not ok or not subject.strip():
        return
    word, ok = getText("Target word to bring into Learning:", parent=mw)
    if not ok or not word.strip():
        return
    cfg = _config()
    try:
        from . import db, state_machine

        conn = db.connect()
        try:
            card_id = state_machine.enter_learning(
                mw.col, conn, word.strip(), subject.strip(), cfg
            )
        finally:
            conn.close()
    except Exception as e:
        log.exception("enter_learning failed")
        showWarning(f"Create Learning card failed: {e}", parent=mw)
        return
    mw.reset()  # refresh deck list / due counts to show the new card
    showInfo(
        f"Subject: {subject.strip()}\n"
        f"Word:    {word.strip()}\n\n"
        f"Created tracked Learning card id={card_id} in "
        f"{subject.strip()}::Learning.\n\n"
        "Study that deck and answer the card; once FSRS retrievability ≥ "
        "the configured threshold, it promotes to Recycled.",
        parent=mw,
    )


def _prompt_subject_word(mw):
    """Prompt for subject + word. Returns (subject, word) stripped, or None if
    cancelled/empty."""
    from aqt.utils import getText

    subject, ok = getText("Subject (parent deck, e.g. Japanese):", parent=mw)
    if not ok or not subject.strip():
        return None
    word, ok = getText("Tracked word:", parent=mw)
    if not ok or not word.strip():
        return None
    return subject.strip(), word.strip()


def _set_counter_action(column: str, prompt: str) -> None:
    """Shared dev action: set recycle_count or fail_streak on a tracked word."""
    from aqt import mw
    from aqt.utils import getText, showInfo, showWarning

    sw = _prompt_subject_word(mw)
    if sw is None:
        return
    subject, word = sw
    value_str, ok = getText(prompt, parent=mw)
    if not ok:
        return
    try:
        value = int(value_str.strip())
    except ValueError:
        showWarning(f"Not an integer: {value_str!r}", parent=mw)
        return
    try:
        from . import db

        conn = db.connect()
        try:
            row = db.get_word(conn, word, subject)
            if row is None:
                showWarning(
                    f"No tracked word {word!r} in subject {subject!r}.",
                    parent=mw,
                )
                return
            if column == "recycle_count":
                db.set_recycle_count(conn, int(row["id"]), value)
            else:
                db.set_fail_streak(conn, int(row["id"]), value)
        finally:
            conn.close()
    except Exception as e:
        log.exception("set %s failed", column)
        showWarning(f"Set {column} failed: {e}", parent=mw)
        return
    showInfo(
        f"{subject} / {word}: {column} set to {value}.", parent=mw,
    )


def _force_transition_action(passed: bool) -> None:
    """Shared dev action: run the PASS or FAIL transition for a tracked word's
    current stage, bypassing the FSRS gate (same code the reviewer hook runs).
    """
    from aqt import mw
    from aqt.utils import showInfo, showWarning

    sw = _prompt_subject_word(mw)
    if sw is None:
        return
    subject, word = sw
    cfg = _config()
    try:
        from . import db, state_machine

        conn = db.connect()
        try:
            row = db.get_word(conn, word, subject)
            if row is None:
                showWarning(
                    f"No tracked word {word!r} in subject {subject!r}.",
                    parent=mw,
                )
                return
            before = dict(row)
            if passed:
                result = state_machine.force_pass(mw.col, conn, row, cfg)
            else:
                result = state_machine.force_fail(mw.col, conn, row, cfg)
            after = db.get_word_by_id(conn, int(row["id"]))
        finally:
            conn.close()
    except Exception as e:
        log.exception("force %s failed", "pass" if passed else "fail")
        showWarning(f"Force {'pass' if passed else 'fail'} failed: {e}", parent=mw)
        return
    mw.reset()  # refresh deck list / due counts
    showInfo(force_result_text(subject, word, passed, result, before, after), parent=mw)


def force_result_text(subject, word, passed, result, before, after) -> str:
    """The Force PASS/FAIL result dialog. A transition that regenerates a card
    runs in the background (result "… (async)"), so the row read right after
    it is still the OLD state — say so instead of showing stale numbers."""
    def row(r):
        return (f"stage={r['stage']} recycle_count={r['recycle_count']} "
                f"fail_streak={r['fail_streak']} card={r['current_card_id']}")

    text = (
        f"{subject} / {word}\n"
        f"Action: force {'PASS' if passed else 'FAIL'}  ->  {result}\n\n"
        f"Before: {row(before)}\n"
    )
    if str(result).endswith("(async)"):
        text += ("After:  the new card is being generated in the background; the "
                 "stage and card change when it lands (a few seconds).")
    else:
        text += f"After:  {row(after)}"
    return text


def _generate_action() -> None:
    from aqt import mw
    from aqt.utils import getText, showInfo, showWarning

    word, ok = getText("Target word to generate a sentence for:", parent=mw)
    if not ok or not word.strip():
        return
    cfg = _config()
    prov, model = generator.resolve_for_subject(cfg, None)
    req = GenRequest(
        word=word.strip(),
        model=model,
        temperature=float(cfg.get("temperature", 0.3)),
        words_per_sentence=int(cfg.get("words_per_sentence", 1)),
    )
    try:
        sentence = generate_sentence(
            req,
            endpoint=prov.api_base,
            timeout=prov.timeout,
            api_format=prov.api_format,
            api_key=prov.api_key,
        )
    except GenerationError as e:
        showWarning(
            f"Generation failed: {e}\n\n"
            f"Check Tools → Engram → Test connection "
            f"(provider {prov.name}, endpoint {prov.api_base}, model {model}).",
            parent=mw,
        )
        return
    showInfo(f"Model: {model}\nWord: {word}\n\n{sentence}", parent=mw)


# --------------------------------------------------------------------------
# Obsidian vault ingestion (steps 6 + 7)
# --------------------------------------------------------------------------

def _vault_scan_summary(notes, unmapped, avail, selection=None) -> "list[str]":
    """The lines shown above the candidate table: what the scan found, and —
    when extraction could not run — the single line saying why."""
    lines = []
    if selection is not None:
        where = ", ".join(selection.roots[:4]) or "your selection"
        if len(selection.roots) > 4:
            where += f", +{len(selection.roots) - 4} more"
        lines.append(f"From {where} → category {selection.category}.")
    lines.append(f"{len(notes)} changed note(s) with a mapped category.")
    if unmapped:
        lines.append(
            f"{unmapped} changed note(s) skipped: no `vault_category_map` entry, "
            "so there is no destination deck for them."
        )
    if not avail.available:
        lines.append(f"Extraction skipped — {avail.reason}")
    return lines


def _pick_vault_selection(mw, cfg, vcfg):
    """Step 1 of the import: browse the vault and choose what to import.

    Returns an `ImportSelection`, or None if the user cancelled or there is
    nothing to show. The picker replaces `vault_category_map` as the *required*
    input — that key is now only a pre-fill, so importing from a new folder
    needs no config editing.
    """
    from aqt.utils import showInfo

    from . import vault_import, vault_picker, vault_select

    tree = vault_select.build_tree(vcfg)
    if tree.is_empty:
        showInfo(
            f"No markdown notes found in {vcfg.path}.\n\n"
            "Check the folder (Tools → Engram → Choose vault folder…). "
            "Only `.md` files are imported.",
            parent=mw, title="Engram — Import from vault",
        )
        return None

    try:
        deck_names = sorted(d.name for d in mw.col.decks.all_names_and_ids())
    except Exception:
        deck_names = []

    prov = generator.resolve_provider(cfg, vcfg.extraction_provider)
    return vault_picker.run_picker_dialog(
        mw, tree,
        categories=vault_select.known_categories(cfg),
        deck_names=deck_names,
        # Demoted from prerequisite to pre-fill: checking a mapped folder fills
        # the category box, and the user can always override it.
        category_map=vcfg.category_map,
        deck_for_category=lambda c: vault_import.resolve_destination_deck(cfg, c),
        extraction_line=vault_select.describe_extraction(vcfg, prov),
    )


def _choose_vault_folder(mw, cfg: dict, first_time: bool = False):
    """Ask for the Obsidian vault folder and save it as this profile's
    `vault_path`. Returns the updated config, or None if cancelled."""
    import os

    from aqt.qt import QFileDialog
    from aqt.utils import showInfo

    if first_time:
        showInfo(
            "Choose your Obsidian vault folder.\n\n"
            "That is all Engram needs — you pick which folders to import from, "
            "and their category, each time you run Import from vault. You can "
            "change the folder later with Tools → Engram → Choose vault folder….",
            parent=mw, title="Engram — Import from vault",
        )
    start = str(cfg.get("vault_path") or "") or os.path.expanduser("~")
    path = QFileDialog.getExistingDirectory(mw, "Choose your Obsidian vault folder", start)
    if not path:
        return None
    new_cfg = dict(cfg)
    new_cfg["vault_path"] = os.path.normpath(path)
    _write_config(new_cfg)
    log.info("vault folder set to %s", new_cfg["vault_path"])
    return new_cfg


def _choose_vault_folder_action() -> None:
    from aqt import mw
    from aqt.utils import tooltip

    cfg = _choose_vault_folder(mw, _config())
    if cfg is not None:
        tooltip(f"Vault folder: {cfg['vault_path']}", parent=mw)


def _import_from_vault_action() -> None:
    """Step 7: pick what to import, extract candidates, review, import.

    The picker (`vault_picker`) decides WHAT is scanned and WHERE it routes; the
    rest of the pipeline — scan, extraction, review dialog, import — is
    unchanged and simply receives a derived `VaultConfig` narrowed to the
    selection.

    Everything slow (the provider probe and one extraction call per changed
    note) runs off the main thread behind a progress dialog; the dialogs, the
    import, and the ledger update happen back on the main thread where touching
    the collection is safe.

    A dead or unconfigured extraction backend does NOT abort the scan — the
    dialog still opens, reports why extraction was skipped, and shows nothing to
    import. That is the same contract the generation pipeline has: a failed LLM
    degrades the AI step and leaves everything else working.
    """
    from aqt import mw
    from aqt.operations import QueryOp
    from aqt.utils import askUser, showInfo, showWarning

    from . import db, vault_dialog, vault_ingest, vault_import, vault_select

    cfg = _config()
    vcfg = vault_ingest.resolve_vault_config(cfg)
    if not vcfg.configured:
        # The vault folder is a per-profile setting (not in the config
        # editor), so ask for it here the first time.
        cfg = _choose_vault_folder(mw, cfg, first_time=True)
        if cfg is None:
            return
        vcfg = vault_ingest.resolve_vault_config(cfg)

    selection = _pick_vault_selection(mw, cfg, vcfg)
    if selection is None:
        return
    if not selection.valid:  # the dialog disables OK, so this is belt-and-braces
        return

    # Narrow the scan to exactly what was picked, and label it with the picked
    # category. `scan_vault` itself is untouched — an exact path is a valid glob.
    scan_cfg = vault_select.selection_to_vault_config(vcfg, selection)

    # Fast, local, main-thread: which notes changed. Cheap enough to do before
    # committing the user to any spend, and its result is what we ask about.
    conn = db.connect()
    try:
        all_notes = vault_ingest.scan_vault(scan_cfg, conn, force=selection.force)
    except Exception as e:
        log.exception("vault scan failed")
        showWarning(f"Vault scan failed: {e}", parent=mw)
        return
    finally:
        conn.close()

    notes = vault_ingest.mapped_notes(all_notes)
    unmapped = len(all_notes) - len(notes)
    if not notes:
        # With the picker every selected note is mapped, so an empty result
        # means the ledger skipped them all — say so, and name the way out.
        extra = (
            "They are all unchanged since their last import.\n\n"
            "Tick “Re-scan even if unchanged” in the picker to import them again."
            if not selection.force
            else "Nothing matched the selection on disk."
        )
        showInfo(
            f"Nothing to import from the {selection.file_count} selected "
            f"note(s).\n\n{extra}",
            parent=mw, title="Engram — Import from vault",
        )
        return

    # Cost control: one extraction call per note, and the user is paying for
    # them. Say how many before making any of them.
    prov_name = vcfg.extraction_provider or generator.default_provider_name(cfg)
    listing = "\n".join(f"  • {n.rel_path}  [{n.category}]" for n in notes[:15])
    if len(notes) > 15:
        listing += f"\n  … and {len(notes) - 15} more"
    if not askUser(
        f"{len(notes)} changed note(s) will be sent to provider {prov_name!r} "
        f"— one extraction call each, up to {vcfg.max_cards_per_note} cards per "
        f"note.\n\n{listing}\n\nContinue?",
        parent=mw, title="Engram — Import from vault", defaultno=False,
    ):
        return

    def op(col):
        """Background thread. Probe once, extract per note, then look up
        duplicates. Returns everything the main thread needs."""
        avail = vault_ingest.preflight_extraction(cfg, category=selection.category)
        candidates = []
        if avail.available:
            for note in notes:
                mw.taskman.run_on_main(
                    lambda p=note.rel_path: mw.progress.update(
                        label=f"Extracting from {p}…"
                    )
                )
                candidates.extend(vault_ingest.extract_candidates(note, cfg))
        session = vault_import.ReviewSession.from_candidates(candidates, cfg)
        # The picked deck is the user's explicit answer to "where do these go",
        # so it overrides the reverse `deck_category_map` lookup. Still shown as
        # an editable box in the review dialog.
        vault_select.apply_destination(session, selection)
        dupes = vault_import.find_duplicate_notes(
            col, [r.front for r in session.reviews]
        )
        vault_import.mark_duplicates(session, dupes)
        deck_names = sorted(d.name for d in col.decks.all_names_and_ids())
        return avail, session, deck_names

    def success(result) -> None:
        avail, session, deck_names = result
        header = _vault_scan_summary(notes, unmapped, avail, selection)
        if not session.reviews:
            header.append("No candidate cards were produced.")
            showInfo("\n".join(header), parent=mw, title="Engram — Import from vault")
            return
        if not vault_dialog.run_review_dialog(mw, session, deck_names, header):
            return
        _finish_vault_import(mw, session, notes, selection)

    QueryOp(parent=mw, op=op, success=success).with_progress(
        "Extracting flashcards from your vault…"
    ).run_in_background()


def _finish_vault_import(mw, session, notes, selection=None) -> None:
    """Write the accepted cards, update the ingestion ledger, and report.

    Main thread only. The ledger is updated ONLY here — cancelling the review
    dialog leaves every note un-ingested so nothing is silently marked done.
    The optional *Remember this folder → category* write follows the same rule:
    it lands only once the import is actually committed.
    """
    from aqt.utils import showInfo, showWarning

    from . import db, vault_import, vault_select

    try:
        report = vault_import.import_reviews(mw.col, session)
    except Exception as e:
        log.exception("vault import failed")
        showWarning(f"Import failed: {e}", parent=mw)
        return

    conn = db.connect()
    try:
        vault_import.record_ingested(conn, notes, report)
    finally:
        conn.close()

    remembered = None
    if selection is not None:
        try:
            merged = vault_select.merge_remembered(_config(), selection)
            if merged is not None:
                cfg = _config()
                cfg["vault_category_map"] = merged
                _write_config(cfg)
                remembered = selection.remember_entries()
        except Exception:  # noqa: BLE001 — a preference write must not lose the import
            log.exception("vault import: could not persist vault_category_map")

    mw.reset()  # refresh deck list / due counts so the new cards show up

    by_deck = "\n".join(
        f"  {deck}:  {count}" for deck, count in sorted(report.by_deck.items())
    ) or "  (none)"
    lines = [
        f"Imported {report.imported} card(s) as Basic notes, "
        f"tagged `{vault_import.VAULT_TAG}`.",
        "",
        "Into:",
        by_deck,
        "",
        f"{len(notes)} vault note(s) marked as ingested — they will not be "
        "re-read until you edit them.",
    ]
    if remembered:
        rows = "\n".join(f"  {k}  →  {v}" for k, v in sorted(remembered.items()))
        lines += ["", "Remembered in `vault_category_map`:", rows]
    if report.failed:
        lines += ["", f"{report.failed} card(s) could not be created:"]
        lines += [f"  {e}" for e in report.errors[:10]]

    # A brand-new category imports fine (model/card_type fall through to the
    # defaults), but it has no generation settings of its own yet. Say so once,
    # here, rather than turning the import flow into a second config wizard.
    if selection is not None and not vault_select.category_is_configured(
        _config(), selection.category
    ):
        lines += [
            "",
            f"Note: “{selection.category}” has no model or card type "
            "configured yet, so it uses the defaults. Set them in "
            "Tools → Engram → Setup categories… if you want AI practice cards "
            "generated from these once they graduate.",
        ]
    showInfo("\n".join(lines), parent=mw, title="Engram — Import from vault")


def dev_menu_enabled(cfg: dict) -> bool:
    """The testing items are shown only when `show_dev_menu` is exactly true
    (documented in TESTING.md, not in the user-facing config docs)."""
    return (cfg or {}).get("show_dev_menu") is True


def menu_spec(show_dev: bool) -> list:
    """The Engram menu as data: (label, handler) entries, None for a separator.
    A handler that is a list is a submenu. `register` builds the Qt menu from
    this, so the tests check the same structure Anki shows."""
    spec = [
        ("Getting started…", _get_started_action),
        ("Test connection", _test_connection_action),
        ("Setup categories…", _setup_categories_action),
        None,
        ("Import from vault…", _import_from_vault_action),
        ("Choose vault folder…", _choose_vault_folder_action),
    ]
    if not show_dev:
        return spec
    dev = [
        ("Set recycle_count for word…",
         lambda: _set_counter_action("recycle_count", "New recycle_count:")),
        ("Set fail_streak for word…",
         lambda: _set_counter_action("fail_streak", "New fail_streak:")),
        None,
        ("Force PASS on current card…", lambda: _force_transition_action(True)),
        ("Force FAIL on current card…", lambda: _force_transition_action(False)),
    ]
    return spec + [
        None,
        ("Generate test sentence…", _generate_action),
        None,
        ("Populate sidecar from deck…", _populate_action),
        ("Create Learning card for word…", _enter_learning_action),
        None,
        ("Dev / test", dev),
    ]


def menu_labels(cfg: dict) -> "list[str]":
    """Top-level labels the menu shows for this config."""
    return [e[0] for e in menu_spec(dev_menu_enabled(cfg)) if e is not None]


def _add_entries(qmenu, entries, mw) -> None:
    from aqt.qt import QAction

    for entry in entries:
        if entry is None:
            qmenu.addSeparator()
            continue
        label, handler = entry
        if isinstance(handler, list):
            _add_entries(qmenu.addMenu(label), handler, mw)
            continue
        act = QAction(label, mw)
        act.triggered.connect(handler)
        qmenu.addAction(act)


def register() -> None:
    from aqt import mw
    from aqt.qt import QMenu

    menu = QMenu("Engram", mw)
    mw.form.menuTools.addMenu(menu)
    try:
        from . import profile_runtime

        cfg = profile_runtime._global_config()
    except Exception:  # a broken config must not cost the user the menu
        cfg = {}
    _add_entries(menu, menu_spec(dev_menu_enabled(cfg)), mw)
    log.info("menu registered")

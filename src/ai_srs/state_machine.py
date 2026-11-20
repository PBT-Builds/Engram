"""Pipeline state machine (spec §4).

States: learning, recycled, legacy. Transitions are evaluated after each
review of an AI card (hooked from reviewer_hooks). All thresholds come from
config.json (spec §6). Promotion/demotion is plain deterministic code — the
model is never consulted for state decisions (spec §1, §4).

Implemented (this file):
  - `enter_learning`: pipeline entry — generate a sentence, create the AI
    card in `Subject::Learning`, record it in the sidecar. (Dev entry point.)
  - `on_card_answered`: the answer-event dispatcher.
      learning  -> recycled       (Step 3, spec §4 row 1)
      recycled  -> recycled cycle  (Step 4, spec §4 row 2)
      recycled  -> legacy          (Step 4, spec §4 row 3)
      recycled  -> learning        (Step 5, spec §4 row 4)
  - `force_pass` / `force_fail`: dev hooks that run the SAME transitions
    without waiting for FSRS (see menu.py).

Pass / fail signals (spec §4):
  - A "successful" review uses the SAME proxy as Step 3: post-answer FSRS
    retrievability >= `learned_retrievability`. Because retrievability is ~1.0
    immediately after any non-lapse answer, in practice a "pass" = answered
    without lapsing.
  - A "fail" is an Anki lapse: `ease == 1` ("Again"). This must key off the
    grade, NOT retrievability — right after *any* answer (pass or lapse)
    elapsed time is ~0 so retrievability is ~1.0 and cannot distinguish a
    lapse. Demotion counts CONSECUTIVE fails; any pass resets the streak to 0.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from . import db, deck_ops, generator, strategies, subjects, ui_notify
from .log import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from anki.cards import Card
    from anki.collection import Collection

log = get_logger()

LEARNING = db.STAGE_LEARNING
RECYCLED = db.STAGE_RECYCLED
LEGACY = db.STAGE_LEGACY


# ---------- generation chokepoint (card-type strategies, spec §8) ----------

def _read_source_qa(col: "Collection", source_note_id) -> "tuple[Optional[str], Optional[str]]":
    """Read the ORIGINAL note's (front, back) for the knowledge strategy input
    and for fallback content. Front = first field; back = first non-empty field
    after it. Read-only; never raises (best effort)."""
    if not source_note_id or col is None:
        return None, None
    try:
        note = col.get_note(int(source_note_id))
        values = [(v or "").strip() for v in note.values()]
    except Exception:  # pragma: no cover - best effort
        log.exception("could not read source note %s", source_note_id)
        return None, None
    front = values[0] if values else ""
    back = next((v for v in values[1:] if v), "")
    return (front or None), (back or None)


def _generate_content(
    card_type: Optional[str],
    ctx: "strategies.GenContext",
    cfg: dict,
) -> "tuple[strategies.CardContent, bool]":
    """Col-FREE generation: run the card-type strategy for `ctx` and return
    (CardContent, is_fallback). Safe to call OFF the main thread — it touches
    only the network (via `strategies.generate_card`), never the Anki
    collection or the sidecar. `strategies.generate_card` never raises, so on
    hard failure we FALL BACK (strategy-appropriate) rather than abort (spec §5).
    """
    prov, model = generator.resolve_for_subject(cfg, ctx.subject)
    log.info(
        "generating word=%r subject=%r via provider=%s model=%s",
        ctx.word, ctx.subject, prov.name, model,
    )
    content = strategies.generate_card(
        card_type, ctx,
        model=model,
        endpoint=prov.api_base,
        timeout=prov.timeout,
        temperature=float(cfg.get("temperature", 0.3)),
        retries=int(cfg.get("validation_retries", 2)),
        api_format=prov.api_format,
        api_key=prov.api_key,
    )
    if content is not None:
        return content, False
    log.warning(
        "using FALLBACK card for word=%r subject=%r card_type=%s",
        ctx.word, ctx.subject, card_type,
    )
    return strategies.fallback_card(card_type, ctx), True


def _card_content(
    col: "Collection",
    cfg: dict,
    word: str,
    subject: Optional[str],
    source_note_id,
) -> "tuple[strategies.CardContent, bool]":
    """Return (CardContent, is_fallback) for a new AI card via the subject's
    card-type strategy (spec §8). The model is still chosen per subject
    (spec §7). On hard failure we FALL BACK rather than abort (spec §6).

    This reads the source note (an Anki collection access) and then generates,
    so it MUST run on the main thread. The transitions use it directly only on
    the synchronous path; the in-Anki path splits the col read from generation
    (see `_regenerate`) so the slow network call runs in the background.
    """
    card_type = subjects.card_type_for_subject(cfg, subject)
    src_front, src_back = _read_source_qa(col, source_note_id)
    ctx = strategies.GenContext(
        word=word, subject=subject,
        source_front=src_front, source_back=src_back,
    )
    log.info(
        "generating word=%r subject=%r card_type=%s", word, subject, card_type,
    )
    return _generate_content(card_type, ctx, cfg)


# ---------- non-blocking, fail-safe transition tail (spec §9) ----------

def _taskman():
    """Anki's background task manager when running inside a live Anki GUI; None
    otherwise (unit tests / standalone). When None, callers run synchronously,
    preserving the original behavior and return values for tests."""
    try:
        from aqt import mw
    except Exception:
        return None
    if mw is None:
        return None
    return getattr(mw, "taskman", None)


def _regenerate(col, conn, row: dict, cfg: dict, *, result_label: str, apply):
    """Shared NON-BLOCKING, FAIL-SAFE tail for the regenerating transitions
    (promotion, recycle cycle, demotion — each does generate + create card +
    atomic sidecar update + retire old card).

    The slow Ollama generation runs OFF the main thread (via `mw.taskman`), and
    `apply(col, conn, content, is_fallback) -> str` (which creates the card,
    commits the sidecar, and retires the old card) runs back ON the main thread.
    A slow local model can therefore never freeze the review, and any failure is
    logged and swallowed — never a dialog, never a blocked review.

    Mirrors the Step-9 autogen pattern, including opening a FRESH sidecar
    connection in the done-callback: the reviewer hook closes its `conn` as soon
    as `on_card_answered` returns, long before the background task completes, so
    `apply` must not reuse it.

    Degrades to FULLY SYNCHRONOUS execution (same `conn`, original return value)
    when taskman is unavailable — i.e. unit tests / standalone — so existing
    call/return semantics are preserved there.
    """
    word = row["word"]
    subject = row["subject"]
    source_note_id = row["source_note_id"]
    taskman = _taskman()

    if taskman is None:
        # Synchronous path (tests / standalone): identical behavior to before.
        content, is_fallback = _card_content(col, cfg, word, subject, source_note_id)
        return apply(col, conn, content, is_fallback)

    # --- In Anki: read source on the main thread, generate in the background,
    # apply on the main thread. ---
    card_type = subjects.card_type_for_subject(cfg, subject)
    src_front, src_back = _read_source_qa(col, source_note_id)  # col -> main thread
    ctx = strategies.GenContext(
        word=word, subject=subject, source_front=src_front, source_back=src_back,
    )

    def task():
        # Background thread: network only — NO collection / sidecar access.
        try:
            return _generate_content(card_type, ctx, cfg)
        except Exception:  # pragma: no cover - generation must never escape
            log.exception("background generation crashed for word=%r", word)
            return strategies.fallback_card(card_type, ctx), True

    def on_done(future) -> None:
        # Back on the main thread (taskman guarantee): safe to touch col + DB.
        try:
            content, is_fallback = future.result()
        except Exception:  # pragma: no cover - never crash a review
            log.exception("background generation failed for word=%r", word)
            return
        conn2 = None
        try:
            conn2 = db.connect()
            apply(col, conn2, content, is_fallback)
            ui_notify.generating_done(is_fallback)
        except Exception:  # pragma: no cover - never crash a review
            log.exception("transition apply failed for word=%r", word)
        finally:
            if conn2 is not None:
                conn2.close()

    log.info(
        "regenerating word=%r subject=%r in background (%s)",
        word, subject, result_label,
    )
    # Non-blocking indicator: the user keeps reviewing while this runs.
    ui_notify.generating_started()
    taskman.run_in_background(task, on_done)
    return f"{result_label} (async)"


# ---------- read-only FSRS access (spec §4, §9) ----------

def retrievability(col: "Collection", card_id: int) -> Optional[float]:
    """Current FSRS retrievability for a card, read strictly read-only via the
    scheduler's stats (spec §9 — never modify scheduling).

    Returns a float in (0, 1], or None when no FSRS memory state is available
    (e.g. a brand-new card still in learning steps, or FSRS disabled). None
    means "promotion condition not evaluable yet" — we deliberately do NOT
    substitute a different metric (spec §4 keeps retrievability as the proxy).
    """
    try:
        stats = col.card_stats_data(card_id)
    except Exception as e:  # pragma: no cover - defensive
        log.warning("card_stats_data failed for card=%d: %s", card_id, e)
        return None
    value = getattr(stats, "fsrs_retrievability", 0.0) or 0.0
    if value <= 0.0:
        return None
    return float(value)


# ---------- pipeline entry (entry to learning) ----------

def enter_learning(
    col: "Collection",
    conn,
    word: str,
    subject: str,
    cfg: dict,
) -> int:
    """Bring a word into the pipeline at the `learning` stage: generate a
    validated sentence (falling back to original content/placeholder if
    generation fails, spec §5), create a fresh AI card in `Subject::Learning`,
    and record it in the sidecar as the word's live card.

    Returns the new card id. Never raises for generation failure — it falls
    back so a review is never broken.
    """
    word_id = db.upsert_word(conn, word, subject)
    existing = db.get_word_by_id(conn, word_id)
    old_card_id = existing.get("current_card_id") if existing else None
    source_note_id = existing.get("source_note_id") if existing else None

    content, _is_fallback = _card_content(col, cfg, word, subject, source_note_id)
    new_card_id = deck_ops.create_ai_card(
        col, subject, deck_ops.LEARNING_SUFFIX, word, content.front, content.back
    )
    db.set_stage(conn, word_id, LEARNING)
    db.set_current_card_id(conn, word_id, new_card_id)

    if old_card_id:
        # Replacing a prior live card for this word — retire the old one so it
        # doesn't linger as a duplicate.
        try:
            deck_ops.retire_card(col, old_card_id)
        except Exception:  # pragma: no cover - best effort
            log.exception("failed to retire prior card id=%s", old_card_id)

    log.info(
        "entered learning word=%r subject=%r card=%d",
        word, subject, new_card_id,
    )
    return new_card_id


LAPSE_EASE = 1  # Anki: ease button 1 == "Again" == a lapse / fail.


def _passed_by_retrievability(col: "Collection", card_id: int, cfg: dict) -> bool:
    """True iff post-answer FSRS retrievability >= learned_retrievability
    (None / not-evaluable counts as NOT passed)."""
    threshold = float(cfg.get("learned_retrievability", 0.90))
    r = retrievability(col, card_id)
    return r is not None and r >= threshold


# Transition results that count as a "pass" (a non-lapse review). Demotion is
# on CONSECUTIVE fails (spec §4/§5), so EVERY pass must leave fail_streak == 0 —
# including the pass that triggers a recycle cycle, not just promotion/demotion.
_PASS_RESULTS = frozenset({"promoted", "cycled", "graduated"})


def _enforce_streak_reset(conn, word_id: int, result: str) -> str:
    """Post-condition guard for PASS transitions: a pass MUST leave
    fail_streak == 0 (spec §4/§5 — demotion is on consecutive fails, any pass
    resets the streak).

    This is *corrective*, not just detective: if a pass transition somehow ends
    with fail_streak != 0, force it to 0 here and log loudly. The atomic helpers
    (`promote_word`/`cycle_word`/`graduate_word`) already set it to 0, so in the
    normal path this is a no-op; the corrective write guarantees the invariant
    holds in Anki regardless of any environment-specific seam, while the ERROR
    log preserves the signal that something wrote the streak back.

    Non-fatal and last-write-wins inside the same transition — it can never
    break a review, and a *later, separate* failed review still increments
    normally.
    """
    if result in _PASS_RESULTS:
        row = db.get_word_by_id(conn, word_id)
        if row is not None and row["fail_streak"] != 0:
            log.error(
                "INVARIANT VIOLATED: pass transition %r left fail_streak=%s "
                "for word_id=%s — forcing it back to 0 (a pass must reset the "
                "streak)", result, row["fail_streak"], word_id,
            )
            db.set_fail_streak(conn, word_id, 0)
    return result


# ---------- shared transition core (ONE source of truth) ----------
#
# Both the real reviewer hook (`on_card_answered`) and the dev menu
# (`force_pass`/`force_fail`) funnel through `_apply_pass` / `_apply_fail`.
# There are deliberately no parallel copies of the transition logic — the only
# difference between "real" and "forced" is how the pass/fail decision is made
# (FSRS retrievability + grade vs. an explicit dev request), not what the
# transition does.

def _apply_pass(col: "Collection", conn, row: dict, cfg: dict) -> str:
    """The PASS transition for the word's current stage.

    The fail_streak==0 post-condition guard (`_enforce_streak_reset`) is applied
    inside each pass transition's COMMIT step rather than here, because the
    regenerating transitions now commit on the main thread AFTER background
    generation (the commit, not this call, is when the sidecar is written and
    therefore when the streak must be checked on the committing connection)."""
    stage = row["stage"]
    if stage == LEARNING:
        return _promote_learning_to_recycled(col, conn, row, cfg)
    if stage == RECYCLED:
        return _cycle_or_graduate(col, conn, row, cfg)
    return "noop (legacy: no further transitions)"


def _apply_fail(col: "Collection", conn, row: dict, cfg: dict) -> str:
    """The FAIL (lapse) transition. Only the recycled stage tracks fails
    (spec §4)."""
    if row["stage"] == RECYCLED:
        return _register_fail_recycled(col, conn, row, cfg)
    return "noop (only the recycled stage tracks fails)"


# ---------- answer-event evaluation ----------

def on_card_answered(
    col: "Collection",
    conn,
    card: "Card",
    cfg: dict,
    ease: Optional[int] = None,
) -> str:
    """Evaluate pipeline transitions after a card was answered, dispatching on
    the word's current stage. Driven from `reviewer_did_answer_card`, so the
    card's post-answer FSRS state is what we read. `ease` is the grade button
    (1..4); `ease == 1` is a lapse.

    Decides pass/fail, then delegates the actual transition to the shared
    `_apply_pass` / `_apply_fail` core. Returns a short status string ("noop",
    "promoted", "cycled", "graduated", "demoted", "fail-registered").
    """
    row = db.get_word_by_current_card(conn, int(card.id))
    if row is None:
        return "noop"  # not a tracked AI card (e.g. a Legacy/Original card)

    stage = row["stage"]

    if stage == LEARNING:
        # learning -> recycled on a pass (Step 3, verified). Learning has no
        # fail handling per spec §4 (demotion is only from recycled).
        if not _passed_by_retrievability(col, int(card.id), cfg):
            return "noop"
        return _apply_pass(col, conn, row, cfg)

    if stage == RECYCLED:
        if ease == LAPSE_EASE:
            return _apply_fail(col, conn, row, cfg)
        # Non-lapse: gate the "successful cycle" on the same retrievability
        # proxy as Step 3.
        if not _passed_by_retrievability(col, int(card.id), cfg):
            return "noop"
        return _apply_pass(col, conn, row, cfg)

    return "noop"  # legacy (no live AI card) or anything unexpected


# ---------- dev hooks: drive transitions without waiting for FSRS ----------

def force_pass(col: "Collection", conn, row: dict, cfg: dict) -> str:
    """Run the PASS-path transition for the word's current stage, bypassing the
    retrievability gate (as if answered successfully). For dev/testing only —
    calls the exact same `_apply_pass` core the reviewer hook uses.
    """
    return _apply_pass(col, conn, row, cfg)


def force_fail(col: "Collection", conn, row: dict, cfg: dict) -> str:
    """Run the FAIL-path transition (a lapse tick) for the word's current
    stage, calling the same `_apply_fail` core the reviewer hook uses. For
    dev/testing only.
    """
    return _apply_fail(col, conn, row, cfg)


def _promote_learning_to_recycled(
    col: "Collection",
    conn,
    row: dict,
    cfg: dict,
) -> str:
    """learning -> recycled (spec §4 row 1): generate a fresh sentence, create
    a new card in `Subject::Recycled`, retire the old learning card, and
    update the sidecar (stage=recycled, reset fail_streak, current_card_id =
    new card; recycle_count left unchanged).

    Generation runs in the background (`_regenerate`); the commit below runs on
    the main thread. The new card is created and the sidecar repointed to it
    BEFORE the old card is retired, so a mid-failure can never leave the word
    pointing at a retired card:
      - generation fails          -> falls back to placeholder content (no abort).
      - create/commit fails        -> logged + swallowed; word stays in learning,
        still pointing at the live learning card.
      - retire fails (last step)  -> sidecar already points at the healthy new
        recycled card; the old learning card just lingers un-suspended
        (recoverable, logged).

    Returns "promoted" (sync) or "promoted (async)" (in Anki, before the
    background commit lands).
    """
    word = row["word"]
    subject = row["subject"]
    word_id = int(row["id"])
    old_card_id = row["current_card_id"]

    def apply(col, conn, content, _is_fallback) -> str:
        new_card_id = deck_ops.create_ai_card(
            col, subject, deck_ops.RECYCLED_SUFFIX, word, content.front, content.back
        )
        # Atomic sidecar update: stage + current_card_id + fail_streak in ONE
        # transaction so the DB can't end up half-promoted.
        db.promote_word(conn, word_id, stage=RECYCLED, current_card_id=new_card_id)
        if old_card_id:
            try:
                deck_ops.retire_card(col, old_card_id)
            except Exception:  # pragma: no cover - best effort
                log.exception("failed to retire old card id=%s", old_card_id)
        log.info(
            "promoted learning->recycled word=%r subject=%r old_card=%s new_card=%d",
            word, subject, old_card_id, new_card_id,
        )
        return _enforce_streak_reset(conn, word_id, "promoted")

    return _regenerate(col, conn, row, cfg, result_label="promoted", apply=apply)


def _cycle_or_graduate(col: "Collection", conn, row: dict, cfg: dict) -> str:
    """A successful review in the recycled stage (spec §4 rows 2 & 3).

    One successful cycle would take recycle_count -> recycle_count + 1. If that
    reaches `recycle_graduation`, graduate to Legacy instead of cycling again;
    otherwise perform another recycle cycle.
    """
    next_count = int(row["recycle_count"]) + 1
    graduation = int(cfg.get("recycle_graduation", 3))
    if next_count >= graduation:
        return _graduate_to_legacy(col, conn, row, cfg, recycle_count=next_count)

    # --- recycle cycle: same create-fresh + retire-old + atomic repoint as
    # the Step-3 promotion, plus recycle_count += 1 (fail_streak reset to 0).
    # Generation runs in the background (`_regenerate`); the commit runs on the
    # main thread.
    word = row["word"]
    subject = row["subject"]
    word_id = int(row["id"])
    old_card_id = row["current_card_id"]

    def apply(col, conn, content, _is_fallback) -> str:
        new_card_id = deck_ops.create_ai_card(
            col, subject, deck_ops.RECYCLED_SUFFIX, word, content.front, content.back
        )
        db.cycle_word(conn, word_id, new_card_id=new_card_id, recycle_count=next_count)
        if old_card_id:
            try:
                deck_ops.retire_card(col, old_card_id)
            except Exception:  # pragma: no cover - best effort
                log.exception("failed to retire old card id=%s", old_card_id)
        log.info(
            "recycle cycle word=%r subject=%r recycle_count=%d old_card=%s new_card=%d",
            word, subject, next_count, old_card_id, new_card_id,
        )
        return _enforce_streak_reset(conn, word_id, "cycled")

    return _regenerate(col, conn, row, cfg, result_label="cycled", apply=apply)


def _graduate_to_legacy(
    col: "Collection", conn, row: dict, cfg: dict, *, recycle_count: int
) -> str:
    """recycled -> legacy (spec §2 + §4 row 3): copy the ORIGINAL note into
    `Subject::Legacy` (traditional, scheduled normally), retire the AI card,
    set stage=legacy, current_card_id=NULL. Legacy gets no further regeneration.

    Atomic ordering mirrors Step 3: create the Legacy copy first, then update
    the sidecar (stage=legacy, current_card_id=NULL), then retire the old AI
    card last. A mid-failure can never leave the word pointing at a retired
    card:
      - copy fails        -> no DB change; word stays recycled, still live.
      - sidecar fails     -> Legacy copy is a harmless extra; word still
        recycled pointing at the live AI card.
      - retire fails      -> sidecar already legacy w/ current_card_id NULL; the
        old AI card just lingers un-suspended (recoverable, logged).

    The Original note is only read & copied from — never edited/moved/suspended.
    """
    word = row["word"]
    subject = row["subject"]
    word_id = int(row["id"])
    old_card_id = row["current_card_id"]
    source_note_id = row["source_note_id"]

    if not source_note_id:
        log.warning(
            "cannot graduate word=%r: no source_note_id linked (populate the "
            "word from its Original deck first)", word
        )
        return "noop (no source note to copy — populate from Original first)"

    trigger_tag = cfg.get("trigger_tag", "ai-srs")
    try:
        deck_ops.copy_original_to_legacy(
            col, source_note_id, subject, exclude_tags=(trigger_tag,)
        )
    except Exception as e:
        log.exception("graduation copy failed word=%r note=%s", word, source_note_id)
        return f"noop (legacy copy failed: {e})"

    db.graduate_word(conn, word_id, recycle_count=recycle_count)
    if old_card_id:
        try:
            deck_ops.retire_card(col, old_card_id)
        except Exception:  # pragma: no cover - best effort
            log.exception("failed to retire old card id=%s", old_card_id)

    log.info(
        "graduated recycled->legacy word=%r subject=%r recycle_count=%d "
        "source_note=%s retired_card=%s",
        word, subject, recycle_count, source_note_id, old_card_id,
    )
    # Graduation copies the ORIGINAL note (no Ollama), so it stays synchronous;
    # it is still a PASS, so enforce the streak-reset post-condition (spec §4/§5).
    return _enforce_streak_reset(conn, word_id, "graduated")


def _register_fail_recycled(col: "Collection", conn, row: dict, cfg: dict) -> str:
    """A failed (lapsed) review in the recycled stage (spec §4 row 4).

    Bumps the CONSECUTIVE fail streak by one (a pass elsewhere resets it to 0
    via the atomic pass-path helpers). When the streak reaches `demotion_fails`,
    demote back to learning.
    """
    word_id = int(row["id"])
    streak = db.increment_fail_streak(conn, word_id)
    demotion_fails = int(cfg.get("demotion_fails", 3))
    log.info("fail registered word=%r fail_streak=%d", row["word"], streak)
    if streak >= demotion_fails:
        return _demote_to_learning(col, conn, row, cfg)
    return "fail-registered"


def _demote_to_learning(col: "Collection", conn, row: dict, cfg: dict) -> str:
    """recycled -> learning (spec §4 row 4): generate a fresh sentence, create
    a new card in `Subject::Learning`, retire the recycled card, set
    stage=learning and reset fail_streak=0. `recycle_count` is left UNCHANGED —
    the word keeps its history.

    Same create-fresh + atomic-repoint-then-retire ordering as Step 3; reuses
    `db.promote_word` (which sets stage + current_card_id + fail_streak=0 and
    leaves recycle_count alone).
    """
    word = row["word"]
    subject = row["subject"]
    word_id = int(row["id"])
    old_card_id = row["current_card_id"]

    def apply(col, conn, content, _is_fallback) -> str:
        new_card_id = deck_ops.create_ai_card(
            col, subject, deck_ops.LEARNING_SUFFIX, word, content.front, content.back
        )
        # promote_word also resets fail_streak=0 (the word restarts in learning).
        db.promote_word(conn, word_id, stage=LEARNING, current_card_id=new_card_id)
        if old_card_id:
            try:
                deck_ops.retire_card(col, old_card_id)
            except Exception:  # pragma: no cover - best effort
                log.exception("failed to retire old card id=%s", old_card_id)
        log.info(
            "demoted recycled->learning word=%r subject=%r old_card=%s new_card=%d",
            word, subject, old_card_id, new_card_id,
        )
        return "demoted"

    return _regenerate(col, conn, row, cfg, result_label="demoted", apply=apply)

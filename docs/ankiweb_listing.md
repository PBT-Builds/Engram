# AnkiWeb listing draft — Engram 1.0.0

Draft only, not published. AnkiWeb's upload form has a **title**, a
**description** (plain text with limited HTML, shown on the add-on page), a
**support page / homepage** link and image uploads. The upload page is
<https://ankiweb.net/shared/addons> (signed in), using
`dist/ai_srs-1.0.0.ankiaddon`.

---

## Title

**Engram – AI practice cards from your own decks (local, private)**

(Shorter fallback if the field is tight: *Engram – AI practice cards*)

## Short description

> When a card graduates, Engram adds a fresh AI-written practice card to a
> separate deck: a new example sentence for a word, or the same fact asked
> another way. Runs on a free local model (Ollama). Your original cards are
> never changed.

## Full description

```
Engram turns the cards you already study into fresh practice.

When a card in one of your decks graduates from learning to review, Engram writes ONE new practice card into a separate deck:
• Languages: a short new example sentence that uses the word, with an English translation and the word's meaning.
• Knowledge: the same fact, asked from a different angle or the other way round.

The AI cards get their own small cycle (Learning → Recycled → Legacy) next to your decks. Your original cards are never edited, moved, rescheduled or deleted.

PRIVATE BY DEFAULT
Engram uses a free AI model that runs on your own computer (Ollama). Your cards don't leave your machine. Engram has no telemetry, no account and no server of its own; nothing is ever sent to the author.

SETUP (about 10 minutes)
1. Install Ollama from https://ollama.com and start it.
2. In a terminal: ollama pull qwen2.5:7b   (about 4.7 GB; one model covers every subject)
3. Install this add-on and restart Anki.
4. Tools → Engram → Setup categories… → pick a deck, name its category, choose "language" or "knowledge".
5. Study as usual. New AI cards appear in <Category>::Learning.

Check everything with Tools → Engram → Test connection. It tells you if Ollama isn't running or a model isn't downloaded, and the exact command to fix it.

GOOD TO KNOW
• The first AI card after starting Anki can take up to a minute while the model loads. Later cards take a few seconds.
• If the AI fails, Engram uses a copy of your original card. Reviews are never blocked.
• Each Anki profile keeps its own Engram settings and data.
• Optional: suggest cards from your Obsidian notes (Tools → Engram → Import from vault…). You review every card before anything is added.
• Needs Anki 23.10+ (FSRS) and about 8 GB of RAM.

Source, full guide and issues: https://github.com/PBT-Builds/Engram
Copyright (C) 2026 pbt-builds. License: AGPL-3.0
```

## Support / homepage link

<https://github.com/PBT-Builds/Engram>

## Screenshots to use (in this order)

Taken on 2026-11-20 in the EngramTest profile and saved under
`docs/screenshots/` (the same files the README shows). They are in Anki's dark
theme, which followed the Windows setting, and are cropped to the window or
dialog.

1. `ai-card.png`: an AI **language** card in review (front: word + Japanese
   sentence; back: translation + gloss). It shows the product in one image.
2. `setup-categories.png`: the Setup categories dialog with a deck mapped to
   `Japanese` / `language` / `qwen2.5:7b`. It shows how little setup is needed.
3. `test-connection.png`: Test connection with `local` reachable ✓ and
   `qwen2.5:7b` listed. It shows a stranger what "working" looks like.
4. `vault-review.png`: the vault review dialog with a few candidate cards.

To retake them: a test profile only (no personal decks or notes in view), and
crop to the dialog. The vault shot uses a two-note demo vault (Congress of
Vienna, Peace of Westphalia), not a personal one.

## Publishing checklist (by hand)

1. Sign in at <https://ankiweb.net>, open *Shared add-ons → Upload*.
2. Upload `dist/ai_srs-1.0.0.ankiaddon`; paste the title, description and
   support link above; attach the screenshots.
3. Note the numeric add-on code AnkiWeb assigns, and put it in the README
   quickstart step 3 (replacing "paste Engram's code from its AnkiWeb page").
4. Create a GitHub release `v1.0.0` with the same `.ankiaddon` attached (the
   README links to the Releases page).

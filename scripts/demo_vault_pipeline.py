#!/usr/bin/env python3
"""End-to-end demo of the vault ingestion pipeline (steps 4-5), outside Anki.

Runs the real code path -- `resolve_vault_config` -> `scan_vault` (against a
real sidecar SQLite ledger) -> `read_note_text` -> `build_extraction_prompt` ->
`extract_candidates` -> `parse_candidates` -- and prints what came back.
Nothing is ever written to an Anki collection: this stops at candidates, which
is exactly where step 6 (the review dialog) will pick up.

Two modes:

  mocked (default)  The LLM call is replaced with a canned, well-formed JSON
                    response. Proves wiring, config resolution, scanning, mtime
                    tracking, and parsing. Costs nothing, needs no key, no
                    network.

  --live            Really calls the configured `extraction_provider`. Costs
                    money on a cloud provider. This is the only way to judge
                    EXTRACTION QUALITY, i.e. whether the prompt is good enough.

Usage:
    python scripts/demo_vault_pipeline.py                     # mocked, temp vault
    python scripts/demo_vault_pipeline.py --vault ~/Obsidian  # mocked, real vault
    python scripts/demo_vault_pipeline.py --live --vault ~/Obsidian
    python scripts/demo_vault_pipeline.py --live --limit 1 --show-prompt

The sidecar ledger lives in a temp dir by default so a demo run never marks
your real notes as ingested. Pass --use-addon-db to use the add-on's own
user_files/pipeline.sqlite instead.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
)

from ai_srs import db, generator, vault_ingest  # noqa: E402


# A canned reply in the exact shape the extraction prompt asks for. Used in
# mocked mode in place of the provider's response.
MOCK_RESPONSE = json.dumps(
    [
        {"front": "What does the first law of thermodynamics state?",
         "back": "The change in a closed system's internal energy equals the "
                 "heat added minus the work it does: dU = Q - W."},
        {"front": "In the equation dU = Q - W, what does W represent?",
         "back": "The work done BY the system."},
    ]
)

SAMPLE_NOTE = """---
tags: [weekly, physics]
---

# Week 12 -- Thermodynamics

The **first law** states that the change in internal energy of a closed system
equals the heat added minus the work done by the system: dU = Q - W.

See [[Week 11]] for the ideal gas law recap.

- TODO: email the professor about the lab slot
"""


def _load_config(path):
    """The add-on's shipped defaults, or a config file the user points at.

    NOTE: this reads `src/ai_srs/config.json` -- the DEFAULTS. It does NOT read
    Anki's meta.json, because this script runs outside Anki. To demo with your
    real settings, copy the values you want into a JSON file and pass --config.
    """
    if path:
        with open(os.path.expanduser(path), encoding="utf-8") as fh:
            return json.load(fh)
    default = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..", "src", "ai_srs", "config.json",
    )
    with open(default, encoding="utf-8") as fh:
        return json.load(fh)


def _temp_vault():
    """A throwaway vault with one realistic note, for a no-arguments run."""
    root = tempfile.mkdtemp(prefix="engram-demo-vault-")
    folder = os.path.join(root, "Weekly", "Physics")
    os.makedirs(folder)
    with open(os.path.join(folder, "week-12.md"), "w", encoding="utf-8") as fh:
        fh.write(SAMPLE_NOTE)
    return root


def _key_summary(api_key):
    if not api_key:
        return "NOT SET"
    return "set (%d chars, ends %s)" % (len(api_key), api_key[-4:])


def main():
    ap = argparse.ArgumentParser(
        description="Demo the vault ingestion pipeline outside Anki."
    )
    ap.add_argument("--live", action="store_true",
                    help="really call the extraction provider (costs money)")
    ap.add_argument("--vault", help="vault folder (default: a generated temp vault)")
    ap.add_argument("--config",
                    help="JSON file of add-on config (default: shipped config.json)")
    ap.add_argument("--provider", help="override extraction_provider")
    ap.add_argument("--model", help="override extraction_model")
    ap.add_argument("--limit", type=int, default=3,
                    help="max notes to extract from (default 3)")
    ap.add_argument("--force", action="store_true",
                    help="ignore the ingested-ledger and re-process everything")
    ap.add_argument("--show-prompt", action="store_true",
                    help="print the full extraction prompt")
    ap.add_argument("--show-text", action="store_true",
                    help="print the cleaned note text sent to the model")
    ap.add_argument("--use-addon-db", action="store_true",
                    help="use src/ai_srs/user_files/pipeline.sqlite, not a temp ledger")
    args = ap.parse_args()

    cfg = _load_config(args.config)

    made_vault = None
    if args.vault:
        cfg["vault_path"] = args.vault
    elif not cfg.get("vault_path"):
        made_vault = _temp_vault()
        cfg["vault_path"] = made_vault
        cfg["vault_category_map"] = {"Weekly/Physics": "Physics"}

    if args.provider:
        cfg["extraction_provider"] = args.provider
    if args.model:
        cfg["extraction_model"] = args.model

    vcfg = vault_ingest.resolve_vault_config(cfg)
    prov = generator.resolve_provider(cfg, vcfg.extraction_provider)
    model = vcfg.extraction_model or prov.model

    print("=" * 72)
    print("MODE           :",
          "LIVE (real API calls)" if args.live else "mocked (no network)")
    print("vault_path     :", vcfg.path)
    print("include_globs  :", list(vcfg.include_globs))
    print("category_map   :", vcfg.category_map or
          "{}  <- nothing mapped: every note will be skipped")
    print("max_cards/note :", vcfg.max_cards_per_note)
    print("provider       :", "%s (api_format=%s)" % (prov.name, prov.api_format))
    print("api_base       :", prov.api_base)
    print("model          :", model or "<none configured>")
    print("api key        :", _key_summary(prov.api_key))
    print("=" * 72)

    if args.live and not prov.api_key:
        print()
        print("ERROR: --live, but this provider resolved no API key.")
        print("Set it in the environment and re-run. PowerShell:")
        print('    $env:ANTHROPIC_API_KEY = "sk-ant-..."')
        print("Git Bash:")
        print('    export ANTHROPIC_API_KEY="sk-ant-..."')
        return 2
    if args.live and not model:
        print()
        print("ERROR: --live, but no model is configured for this provider.")
        print("Set `extraction_model`, or the provider's own `model`.")
        return 2

    tmp_db_dir = None
    if args.use_addon_db:
        conn = db.connect()
    else:
        tmp_db_dir = tempfile.mkdtemp(prefix="engram-demo-db-")
        conn = db.connect(tmp_db_dir)

    original_generate_raw = None
    try:
        notes = vault_ingest.scan_vault(vcfg, conn, force=args.force)
        mapped = vault_ingest.mapped_notes(notes)
        print()
        print("scan: %d changed note(s), %d with a category mapping"
              % (len(notes), len(mapped)))
        for n in notes:
            print("  - %s  [%s]" % (n.rel_path, n.category or "UNMAPPED - skipped"))
        if not mapped:
            print()
            print("Nothing to extract from. Either every note is unchanged "
                  "(use --force), or")
            print("vault_category_map does not cover your folders.")
            return 1

        if not args.live:
            original_generate_raw = generator.generate_raw
            generator.generate_raw = lambda *a, **k: MOCK_RESPONSE

        total = 0
        for note in mapped[: args.limit]:
            text = vault_ingest.read_note_text(note)
            print()
            print("-" * 72)
            print("NOTE: %s   category=%s" % (note.rel_path, note.category))
            print("-" * 72)
            if args.show_text:
                print("cleaned text sent to the model:")
                print()
                print(text)
                print()
            if args.show_prompt:
                print("full prompt:")
                print()
                print(vault_ingest.build_extraction_prompt(
                    text, category=note.category,
                    max_cards=vcfg.max_cards_per_note))
                print()

            candidates = vault_ingest.extract_candidates(note, cfg, note_text=text)
            if not candidates:
                print("  (no candidates -- see the warnings above for why)")
                continue
            for i, c in enumerate(candidates, 1):
                print("  %2d. FRONT: %s" % (i, c.front))
                print("      BACK : %s" % c.back)
            total += len(candidates)
            vault_ingest.mark_ingested(conn, note, cards_imported=0)

        print()
        print("=" * 72)
        print("%d candidate card(s) from %d note(s)."
              % (total, min(len(mapped), args.limit)))
        print("Nothing was written to any Anki collection -- candidates stop here.")
        print("=" * 72)
        return 0
    finally:
        if original_generate_raw is not None:
            generator.generate_raw = original_generate_raw
        conn.close()
        if tmp_db_dir:
            shutil.rmtree(tmp_db_dir, ignore_errors=True)
        if made_vault:
            shutil.rmtree(made_vault, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

"""Measure how often a local model's reply can't be parsed as JSON.

Drives the REAL generation code (strategies.generate_card and
vault_ingest.extract_candidates) against a live backend and records every raw
reply, so the same script gives comparable before/after numbers for a change
to the request (JSON mode) or the parser.

Per attempt it counts: transport errors, replies with no parseable JSON, parsed
but rejected by validation, and good. Per card it counts fallbacks (every
attempt failed). Nothing is written to Anki; no sidecar DB is touched.

    python scripts/measure_json_robustness.py --reps 6 --out result.json
    python scripts/measure_json_robustness.py --model qwen2.5:7b --api-base http://localhost:11434/v1

Local and free with Ollama. Takes a few minutes (one model call per attempt).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ai_srs import generator, strategies, vault_ingest  # noqa: E402

LANGUAGE_WORDS = [("猫", "cat"), ("犬", "dog"), ("水", "water"),
                  ("食べる", "to eat"), ("学校", "school"), ("友達", "friend")]
KNOWLEDGE_QA = [
    ("In what year was the Declaration of Independence signed?", "1776"),
    ("Who was the first President of the United States?", "George Washington"),
    ("Which amendment abolished slavery in the US?", "The 13th Amendment"),
    ("Which 1863 battle was the turning point of the American Civil War?", "Gettysburg"),
]
SAMPLE_NOTE = """# Newton's laws

Newton's second law: F = ma, where F is the net force (N), m the mass (kg)
and a the acceleration (m/s^2).

Momentum p = mv. Impulse J = F Δt = Δp.

Kinetic energy KE = 1/2 m v^2. Work W = F d cos θ.
The work-energy theorem says the net work done on an object equals its change
in kinetic energy.
"""


class Recorder:
    def __init__(self, real, classify):
        self.real = real
        self.classify = classify
        self.events = []

    def __call__(self, *args, **kwargs):
        t0 = time.time()
        try:
            raw = self.real(*args, **kwargs)
        except Exception as e:
            self.events.append({"kind": "transport", "error": str(e)[:200],
                                "secs": round(time.time() - t0, 1)})
            raise
        ok = self.classify(raw)
        self.events.append({"kind": "parsed" if ok else "json_fail",
                            "raw": raw[:400], "secs": round(time.time() - t0, 1)})
        return raw


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="qwen2.5:7b")
    ap.add_argument("--api-base", default="http://localhost:11434/v1")
    ap.add_argument("--api-format", default="openai")
    ap.add_argument("--reps", type=int, default=6)
    ap.add_argument("--extraction-reps", type=int, default=5)
    ap.add_argument("--temperature", type=float, default=0.3)
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    # --- card strategies ---
    rec = Recorder(strategies.generate_raw,
                   lambda raw: strategies._extract_json(raw) is not None)
    strategies.generate_raw = rec
    cards = Counter()
    per_card = []
    jobs = ([("language", strategies.GenContext(word=w, subject="Japanese",
                                                 source_front=w, source_back=g))
             for w, g in LANGUAGE_WORDS]
            + [("knowledge", strategies.GenContext(word=q, subject="History",
                                                    source_front=q, source_back=a))
               for q, a in KNOWLEDGE_QA])
    for rep in range(args.reps):
        for card_type, ctx in jobs:
            before = len(rec.events)
            content = strategies.generate_card(
                card_type, ctx, model=args.model, endpoint=args.api_base,
                timeout=args.timeout, temperature=args.temperature, retries=2,
                api_format=args.api_format,
            )
            attempts = rec.events[before:]
            # An attempt that parsed but returned no card failed validation.
            if content is None:
                for ev in attempts:
                    if ev["kind"] == "parsed":
                        ev["kind"] = "invalid"
            elif attempts and attempts[-1]["kind"] == "parsed":
                for ev in attempts[:-1]:
                    if ev["kind"] == "parsed":
                        ev["kind"] = "invalid"
            cards["fallback" if content is None else "ok"] += 1
            per_card.append({"type": card_type, "word": ctx.word,
                             "fallback": content is None,
                             "attempts": [e["kind"] for e in attempts]})
            print(f"[{rep+1}/{args.reps}] {card_type:9s} {ctx.word[:30]:30s} "
                  f"{'FALLBACK' if content is None else 'ok':8s} "
                  f"{[e['kind'] for e in attempts]}", flush=True)
    strategies.generate_raw = rec.real
    attempt_counts = Counter(e["kind"] for e in rec.events)

    # --- vault extraction ---
    xrec = Recorder(generator.generate_raw,
                    lambda raw: vault_ingest._extract_json_array(raw) is not None)
    generator.generate_raw = xrec
    cfg = {
        "providers": {"local": {"api_format": args.api_format, "api_base": args.api_base,
                                "api_key": "", "model": args.model,
                                "request_timeout_seconds": args.timeout}},
        "default_provider": "local", "extraction_provider": "local",
        "extraction_model": args.model, "temperature": args.temperature,
        "vault_path": ".", "max_cards_per_note": 15,
    }
    note = vault_ingest.VaultNote(rel_path="Physics/newton.md", abs_path="",
                                  mtime=0.0, category="Physics")
    extraction = []
    for rep in range(args.extraction_reps):
        before = len(xrec.events)
        cands = vault_ingest.extract_candidates(note, cfg, note_text=SAMPLE_NOTE)
        kinds = [e["kind"] for e in xrec.events[before:]]
        extraction.append({"candidates": len(cands), "attempts": kinds})
        print(f"[extract {rep+1}/{args.extraction_reps}] {len(cands)} candidates {kinds}",
              flush=True)
    generator.generate_raw = xrec.real
    x_counts = Counter(e["kind"] for e in xrec.events)

    n_attempts = sum(attempt_counts.values())
    summary = {
        "model": args.model, "api_base": args.api_base, "api_format": args.api_format,
        "cards": dict(cards),
        "card_fallback_rate": round(cards["fallback"] / max(1, sum(cards.values())), 3),
        "attempts": dict(attempt_counts),
        "attempt_json_fail_rate": round(attempt_counts["json_fail"] / max(1, n_attempts), 3),
        "extraction_attempts": dict(x_counts),
        "extraction_runs_with_no_candidates": sum(1 for x in extraction if not x["candidates"]),
        "json_fail_samples": [e["raw"] for e in rec.events + xrec.events
                              if e["kind"] == "json_fail"][:10],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"summary": summary, "per_card": per_card,
                       "extraction": extraction,
                       "events": rec.events + xrec.events}, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()

# Engram — Testing

Everything still worth verifying by hand, and how to do it. Configuration and
API-key locations are documented separately in
[`src/ai_srs/config.md` → *Where config actually lives*](src/ai_srs/config.md).

Scope of this document: the provider and vault-import work — provider
abstraction, Anthropic adapter, Test connection, vault scan, extraction, the review dialog, and the
menu wiring.

**Phase 1 (2026-11-19):** every step, the vault picker, and the core pipeline
(spec build steps 3–6, 8, 9) have now been run inside real Anki 25.09.4 on Windows,
in a throwaway `EngramTest` profile, and checked against the sidecar DB, `ai_srs.log`
and the collection itself (via AnkiConnect) rather than the on-screen summaries.
Four bugs were found and fixed; see §0 for the
results. §7 and §8 stay as the manual plans for re-verifying after future changes.

---

## 0. Status snapshot

| Area | Automated | Manually verified (Phase 1, Windows, Anki 25.09.4) |
|---|---|---|
| Step 1 — adapter refactor (`openai` / `ollama`) | ✅ 29 tests | ✅ Test connection lists local Ollama models |
| Step 2 — `anthropic` adapter + `providers` config | ✅ 86 tests, HTTP mocked | ✅ 6 live `POST /v1/messages` extractions via `claude-haiku-4-5-20251001`, no HTTP 400 |
| Step 3 — Test connection across providers | ✅ mocked probes | ✅ in Anki: Ollama up, local down, no key, live `GET /v1/models` (15 models) |
| Step 4 — vault scan + mtime ledger | ✅ 121 tests | ✅ ledger rows only after a committed import; re-run finds nothing |
| Step 5 — extraction prompt + parsing | ✅ canned responses | ✅ live in Anki (9 / 5 / 7 / 5 candidates from four notes) |
| Extraction fallback (no key / provider down) | ✅ 46 tests | ✅ §7.1 — message, no popup, ledger untouched |
| Step 6 — review dialog + import | ✅ 115 tests | ✅ §7.2–7.4 incl. Ctrl+Z / redo, duplicates, escape hatch |
| Step 7 — menu wiring (`Import from vault…`) | ❌ not unit-testable | ✅ dialog in front of progress, no freeze, deck list refreshes |
| Vault picker — selection + routing | ✅ 110 tests (`test_vault_select.py`) | ✅ §8.4–8.7 |
| Vault picker — the Qt dialog | ✅ 9 tests, offscreen Qt (`test_vault_picker.py`) | ✅ §8.1–8.3 tri-state + filter; 2 pre-fill bugs fixed |
| Core pipeline — autogen, Learning → Recycled → Legacy, demotion | ✅ (autogen, streak, strategies suites) | ✅ via real graduations and the Dev / test menu, with `qwen2.5:7b` |
| The add-on inside Anki | — | ✅ loads cleanly; config write round-trips via the UI (§5) |
| Phase 2 — per-profile store + migration | ✅ 62 tests | ✅ EngramTest migration (4/4/4 rows, settings moved, `action=opened` on restart); clean `Stranger` base gets its own empty store (§9) |
| Phase 2 — `.ankiaddon` install + upgrade | ✅ build self-check | ✅ installed and re-installed with Anki's own `AddonManager.install()` into a separate base; store kept. In-app update while running: lock fixed, not yet clicked through |
| Phase 2 — JSON mode, cold start, English check | ✅ 50 tests | ✅ measured live with qwen2.5:7b (malformed 5.7% → 0%); cold start not observed in Anki |
| Phase 2 — Tier 2 dialogs (Test connection model lines, review colours, Force dialog, vault folder chooser) | ✅ pure logic tested | ❌ not seen on screen (no screen access) |

### Running the automated suite

Tests are stdlib `unittest`, each file standalone — there is no `pytest` in the
venv and none is needed.

```powershell
foreach ($f in Get-ChildItem tests\test_*.py) { .\venv\Scripts\python.exe $f.FullName }
```

Last full run (Phase 2, 2026-11-19): **762/762 passing** across 22 files.
New in Phase 2: `test_profile_store.py` (62), `test_json_robustness.py` (30),
`test_cold_start.py` (12), `test_extraction_model_fallback.py` (11),
`test_model_check.py` (19), `test_logging.py` (13), `test_translation_language.py` (8),
`test_ui_text.py` (8).

The three newest files are the ones covering the work above:

- `test_vault_select.py` (110) — the interactive picker's logic: tree building
  from a real vault (honouring `SKIP_DIRS`, the include globs, and the file
  cap), recursive folder resolution, and the routing seam
  (`selection_to_vault_config`). Includes the proof that an exact path is a
  valid glob, and the guard that an empty selection **raises** rather than
  silently widening to the whole vault.

- `test_extraction_fallback.py` (46) — the no-usable-backend path: missing
  credential, missing model, unreachable provider, a probe that itself throws,
  extraction returning `[]` without raising, **scanning still working with a
  dead backend**, and the once-per-reason notice.
- `test_vault_import.py` (115) — everything the review dialog *decides*:
  destination-deck resolution, accept/reject/edit state, `plan()` (what an
  import writes), duplicate flagging, the import itself against a fake
  collection (tag, deck routing, single undo entry, one-bad-note isolation), and
  the ledger write. The **Qt dialog and the menu action are not covered** — see
  §7.

---

## 1. A note on `scripts/demo_vault_pipeline.py`

**This script did not exist in the repo** — not in the working tree, not in any
commit (`git log --all --diff-filter=A` finds no such file), and not as an
untracked file. Whatever mocked run happened previously was from a copy that is
now gone.

It has been **recreated** at `scripts/demo_vault_pipeline.py`, driving the real
code path end to end:

```
resolve_vault_config → scan_vault (real SQLite ledger) → read_note_text
  → build_extraction_prompt → extract_candidates → parse_candidates
```

It stops at candidates and **never writes to an Anki collection** — that is
exactly the seam step 6 will pick up. Its sidecar ledger goes to a temp
directory by default, so a demo run cannot mark your real notes as ingested.

### Re-run the mocked baseline first

```powershell
.\venv\Scripts\python.exe scripts\demo_vault_pipeline.py
```

With no arguments it generates a throwaway vault containing one realistic
physics note and mocks the LLM response. Verified output:

```
MODE           : mocked (no network)
provider       : claude (api_format=anthropic)
api_base       : https://api.anthropic.com/v1
model          : claude-haiku-4-5-20251001
api key        : NOT SET

scan: 1 changed note(s), 1 with a category mapping
  - Weekly/Physics/week-12.md  [Physics]
   1. FRONT: What does the first law of thermodynamics state?
      ...
2 candidate card(s) from 1 note(s).
```

Useful flags: `--vault <path>`, `--config <file.json>`, `--provider`, `--model`,
`--limit N`, `--force` (ignore the ledger), `--show-text` (cleaned note text),
`--show-prompt` (the full prompt), `--use-addon-db`.

> The script reads `src/ai_srs/config.json` — the **defaults** — not Anki's
> `meta.json`, because it runs outside Anki. To demo with your real settings,
> copy the values into a JSON file and pass `--config`.

---

## 2. The `--live` run: setting a real ANTHROPIC_API_KEY

### 2a. Get and set the key

1. Create a key at <https://console.anthropic.com> → API keys. It starts `sk-ant-`.
2. Set it **in the shell you will run the script from**. For a script run, a
   session variable is enough and is the safer choice — it does not persist to
   disk or leak into other programs.

   PowerShell:
   ```powershell
   $env:ANTHROPIC_API_KEY = "sk-ant-..."
   ```

   Git Bash:
   ```bash
   export ANTHROPIC_API_KEY="sk-ant-..."
   ```

   Only use `setx ANTHROPIC_API_KEY "sk-ant-..."` (persistent, machine-wide for
   your user) when you want **Anki itself** to see the key later — see §4.

3. Confirm the add-on resolves it, without spending anything:
   ```powershell
   .\venv\Scripts\python.exe scripts\demo_vault_pipeline.py
   ```
   The header must now read `api key : set (NN chars, ends XXXX)` instead of
   `NOT SET`. If it still says `NOT SET`, the variable is not in this process's
   environment (wrong window, or typed after the shell started).

> **Precedence reminder:** a non-blank `api_key` in the effective config beats
> the environment variable. The shipped `config.json` has `api_key: ""`, so the
> env var wins by default — but if you have previously typed a key into Anki's
> config editor, that stale key shadows the env var. Blank the field to fall
> through.

### 2b. Do the live run

Start small — one note, so a bad prompt costs one call:

```powershell
.\venv\Scripts\python.exe scripts\demo_vault_pipeline.py --live --limit 1 --vault "C:\path\to\ObsidianVault"
```

If your vault has no `vault_category_map`, every note scans as unmapped and is
skipped. Point at a mapped folder with a temporary config file:

```json
{
  "providers": { "claude": { "api_format": "anthropic", "api_base": "https://api.anthropic.com/v1", "api_key": "", "api_key_env": "ANTHROPIC_API_KEY", "model": "claude-haiku-4-5-20251001" } },
  "default_provider": "claude",
  "extraction_provider": "claude",
  "vault_path": "C:\\path\\to\\ObsidianVault",
  "vault_include_globs": ["Weekly/**/*.md"],
  "vault_category_map": { "Weekly/Physics": "Physics" },
  "max_cards_per_note": 15,
  "temperature": 0.3
}
```

```powershell
.\venv\Scripts\python.exe scripts\demo_vault_pipeline.py --live --limit 1 --config demo-config.json
```

Save that file **outside the repo** or under an ignored path if you ever put a
real key in it. With `api_key: ""` plus the env var, it holds no secret.

Then widen: `--limit 5`, then a `--force` pass over a whole mapped folder.

### 2c. What "good" output looks like

Good — the prompt is working, move on to step 6:

- **3–10 candidates from a substantial note**, not 1 and not the `max_cards`
  ceiling every time. Hitting 15 on every note means the model is padding;
  lower `max_cards_per_note` or tighten the prompt.
- **Each card is answerable standalone.** No "according to the note", no "the
  above", no "in this week's reading". This is an explicit prompt rule; a
  violation is a prompt failure, not a parsing failure.
- **One fact per card.** A front asking two things ("What is X and why does Y?")
  means the atomicity rule is not landing.
- **Meta-content skipped.** The demo note deliberately contains
  `- TODO: email the professor about the lab slot`. A card about emailing the
  professor is a **clear prompt failure**.
- **Fronts are specific questions**, not topic labels ("Thermodynamics" as a
  front is wrong; "What does dU = Q − W describe?" is right).
- **Backs are complete but short** — a sentence or two, not a copied paragraph.
- **Obsidian syntax is gone.** `[[Week 11]]`, `![[image.png]]`, and YAML
  frontmatter must never appear in a card. If they do, the bug is in
  `strip_markdown`, not the prompt — check with `--show-text`.
- **Log line present:** `vault extraction: N candidate(s) from <path> via provider=claude model=claude-haiku-4-5-20251001`.

Signals the extraction prompt needs work:

| Symptom | Likely cause | Where to look |
|---|---|---|
| `0 candidates` on a note that clearly has facts | model returned prose or an empty array | re-run with `--show-prompt`; check the log for `no usable candidates on attempt 1/2` |
| Log shows a retry then success | the model ignored "JSON only" on the first pass | tolerable, but tighten the format instruction in `build_extraction_prompt` |
| Log shows `dropped N unusable candidate(s)` | model emitted wrong-typed or empty sides | `parse_candidates` is doing its job; the prompt is producing junk |
| Cards reference the note ("as mentioned above") | standalone rule not landing | strengthen that bullet in the prompt |
| Cards about TODOs / scheduling | meta-content rule not landing | strengthen that bullet |
| Always exactly `max_cards` | model padding to the budget | say "only the facts that are genuinely worth memorizing" more forcefully |
| Truncated final card / invalid JSON on long notes | hit `ANTHROPIC_MAX_TOKENS` (4096) | raise it in `generator.py`, or lower `max_cards_per_note` |

Failures that are **not** prompt problems, and what they look like instead:

- `ERROR: --live, but this provider resolved no API key.` → §2a.
- `401` in the log → key is wrong or revoked.
- `429` in the log → rate limited; the call falls back to "no candidates" by
  design and never raises.
- `404` on `/v1/messages` → wrong `api_base` (missing or doubled `/v1`).
- Invalid model name → check the id against §3's live model list.

### 2d. Cost

Each note is one call (two if the first response fails to parse). With
`claude-haiku-4-5-20251001` and a typical weekly note, a 5-note run is a
fraction of a cent. `max_cards_per_note` caps the output side; a runaway note
is capped by `ANTHROPIC_MAX_TOKENS = 4096`.

---

## 3. Testing "Test connection" (step 3) standalone

`Test connection` is wired into `menu.py`, but its whole logic lives in
`generator.test_all_providers(cfg)` and is callable without Anki, Qt, or steps
6–7.

```powershell
.\venv\Scripts\python.exe -c "import json,sys; sys.path.insert(0,'src')
from ai_srs import generator
cfg=json.load(open('src/ai_srs/config.json',encoding='utf-8'))
for s in generator.test_all_providers(cfg, timeout=5):
    print('%-8s %-6s %s' % (s.name, 'OK' if s.reachable else 'FAIL', s.detail))
    print('         models:', ', '.join(s.models[:8]) or '(none)')"
```

### Case A — nothing configured (verified, this is real captured output)

```
local    FAIL   Not reachable. Is your local LLM server running at this address? For Ollama, start it and run `ollama serve`.
         models: (none)
claude   FAIL   No API key configured. Set this provider's `api_key` in the add-on config, or put the key in the environment variable it names (`api_key_env`, default ANTHROPIC_API_KEY for Claude).
         models: (none)
```

Two things to confirm here, both of which the above demonstrates:
- the **default provider is reported first** (`local`, per `default_provider`);
- one unreachable provider does **not** hide the other.

### Case B — Claude key set, no local server

Set `$env:ANTHROPIC_API_KEY` and re-run. Expected:

```
local    FAIL   Not reachable. Is your local LLM server running at this address? ...
claude   OK     Reachable — N model(s) available.
         models: claude-opus-4-1-..., claude-haiku-4-5-20251001, ...
```

This is the **first real network call to `api.anthropic.com`** the project has
ever made, and it verifies four things the mocked tests cannot:
`GET /v1/models` exists at that path, `x-api-key` + `anthropic-version` are
accepted, the response really is `{"data": [{"id": ...}]}`, and the model id in
`config.json` (`claude-haiku-4-5-20251001`) is actually offered. **Check that id
appears in the list** — a stale model name is a silent live-run failure.

An invalid key yields, by design, no root-ping fallback (`supports_root_ping` is
`False` for cloud):

```
claude   FAIL   Not reachable. Check that the API key is valid and that this machine has internet access. Keys are created at console.anthropic.com.
```

### Case C — local Ollama running

Start Ollama (`ollama serve`) and re-run: `local` should report
`Reachable — N model(s) available` and list what is pulled. Confirm
`qwen2.5:7b` and `phi4` are present, since `models_by_subject` references both.

### Case D — the Anki dialog itself

Deferred until the add-on is loaded in Anki (§4). Only the `showInfo` formatting
in `menu._test_connection_action` is untested by the above; the probe results
are identical.

---

## 4. Remaining manual verification for steps 1–5

Ordered roughly by risk. Items 1–4 need no Anki.

### 4.1 Live Anthropic reachability — **highest value**
§3 Case B. All 86 Anthropic tests mock the HTTP layer, so nothing has ever
confirmed the endpoint, headers, or model id against the real API.

### 4.2 A real generation call through the anthropic adapter
Distinct from `/v1/models` — it exercises `POST /v1/messages`, the `max_tokens`
requirement, and the block-list response parser:

```powershell
.\venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'src')
from ai_srs import generator
print(generator.generate_raw('claude-haiku-4-5-20251001', 'Reply with the single word: ok',
      'https://api.anthropic.com/v1', timeout=30, temperature=0.0,
      api_format='anthropic', api_key=__import__('os').environ['ANTHROPIC_API_KEY']))"
```

Expect `ok`. A `GenerationError` or a KeyError-shaped failure means the response
parser and the live shape disagree.

### 4.3 A real Obsidian vault scan
Everything in step 4 was tested against generated temp directories. Run mocked
(free) against your actual vault:

```powershell
.\venv\Scripts\python.exe scripts\demo_vault_pipeline.py --vault "C:\path\to\ObsidianVault" --limit 3
```

Confirm specifically:
- `.obsidian/`, `.trash/`, and `.git/` are pruned (they should never appear).
- The reported note count is plausible for the vault's real size.
- Non-ASCII filenames and paths with spaces resolve correctly.
- Your `vault_include_globs` select what you expect — remember `*` does **not**
  cross `/`, only `**` does, so `"Weekly/*.md"` misses `Weekly/Physics/x.md`.
- Every note you care about gets a category. Anything printed
  `[UNMAPPED - skipped]` will silently produce nothing in the real feature.
- A `~`-prefixed `vault_path` expands to `C:\Users\<you>\...` on Windows.
- If the vault is in OneDrive/Dropbox, check that sync does not churn mtimes —
  that would defeat the ledger and re-send unchanged notes to the LLM.

### 4.4 The mtime ledger across runs
Run the demo twice with `--use-addon-db` (so the ledger persists):

```powershell
.\venv\Scripts\python.exe scripts\demo_vault_pipeline.py --use-addon-db --vault "C:\path\to\Vault"
.\venv\Scripts\python.exe scripts\demo_vault_pipeline.py --use-addon-db --vault "C:\path\to\Vault"
```

The second run must report **0 changed notes**. Touch one file, re-run, and only
that one should come back. `--force` must return everything. Inspect directly:

```powershell
.\venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'src')
from ai_srs import db
c=db.connect()
for r in db.list_vault_files(c): print(dict(r))"
```

Delete `src\ai_srs\user_files\pipeline.sqlite` afterwards if you do not want the
demo's ledger entries lingering.

### 4.5 Two config traps worth confirming deliberately
- **`vault_category_map` ships `{}`.** Out of the box, ingestion finds notes and
  skips all of them. Confirm you see the "0 with a category mapping" path, so the
  behaviour is familiar before step 6 shows it in a dialog.
- **`extraction_provider` ships `"claude"` while `default_provider` ships
  `"local"`.** A fresh install therefore routes extraction at a provider with no
  key. Confirm this degrades cleanly (log warning, zero candidates, no
  exception) rather than surfacing as a crash — and decide whether the shipped
  default should instead be `local`.

### 4.6 Inside Anki (do this before step 6, not during it)
Done in Phase 1 (2026-11-19): the add-on loads cleanly in Anki 25.09.4 on Windows
(installed as a `<SYMLINKD>` into `addons21i_srs`). The steps below remain the
recipe for a fresh machine:

1. Launch Anki once (`.\venv\Scripts\anki.exe`) so `%APPDATA%\Anki2` and
   `addons21\` are created.
2. Install the add-on — build with `scripts\build_ankiaddon.sh` and use
   Tools → Add-ons → Install from file…, or copy `src\ai_srs` into
   `addons21\ai_srs`. (`install_symlink.sh` defaults to the macOS path; on
   Windows set `ANKI_ADDONS_DIR` and note symlinks need Developer Mode.)
3. Confirm **Tools → Engram** appears at all — `__init__.py` swallows setup
   exceptions to avoid blocking boot, so a broken import shows up as a *missing
   menu*, not an error dialog. Check the log if it is absent.
4. Open Tools → Add-ons → Engram → **Config**. Confirm the editor renders
   `config.md` as help and shows `providers` with a blank `api_key`.
5. Run **Test connection** and compare against §3.
6. Create meta.json — **see §5, this needs an actual edit.** Opening the
   Config screen and clicking OK unchanged writes nothing.
7. **Verify the env var reaches the Anki process.** A session-scoped
   `$env:ANTHROPIC_API_KEY` is invisible to Anki launched from the Start menu.
   Either launch Anki from the same shell, or use `setx` and restart Anki
   completely.
8. Note that **Setup categories… writes the entire merged config to
   meta.json**, including a `providers` block with `api_key: ""`. Blank still
   falls through to the environment, so this is safe — but confirm it, because
   it is the exact shape that would shadow the env var if the field were
   non-blank.

Per the project's existing manual-test discipline: clear `__pycache__`, fully
quit Anki between runs, and verify against the sidecar DB directly rather than
trusting the UI.

---

## 5. Verifying meta.json gets created

**Confirmed root cause of "meta.json is never created":** it is Anki's designed
behaviour, not a fault in Engram. `ConfigEditor.accept()` in
`aqt/addons.py` (Anki 25.9.4, line ~1721) guards the write:

```python
if new_conf != self.conf:
    self.mgr.writeConfig(self.addon, new_conf)
```

Open Config, click **OK** without typing anything, and the new config compares
equal to the old one — so `writeConfig` is never called and no file is written.
Cancel / Escape / the window X go through `reject()`, which never writes either.
And because Engram is installed by symlink rather than through Anki's installer,
the *other* thing that creates meta.json (`install()`, which writes the name and
version block) never ran. Hence: no file.

Full reasoning, plus the four causes ruled out, is in
[`src/ai_srs/config.md` → *Why meta.json may not exist yet*](src/ai_srs/config.md).

### The exact steps

1. Start from a clean state — confirm the file is absent:
   ```powershell
   dir <repo>\src\ai_srs\meta.json
   dir $env:APPDATA\Anki2\addons21\ai_srs\meta.json
   ```
   Both should say *File Not Found*. (They are the same file: the second path
   resolves through the symlink to the first.)

2. Launch Anki from a terminal so any traceback is visible:
   ```powershell
   .\venv\Scripts\anki.exe
   ```

3. **Tools → Add-ons.** Note the add-on lists as **`ai_srs`**, not `Engram` —
   that is expected with no meta.json, because `human_name()` falls back to the
   folder name. It will change to `Engram` only if you later install via
   *Install from file…*, not from a config save.

4. Select it and click **Config**. The editor must open showing the JSON with
   `providers`, `vault_path`, `extraction_provider`, etc. If instead you get
   *"Add-on has no configuration"*, `config.json` failed to parse — that is a
   different bug, report the message.

5. **Make a real edit.** The smallest unambiguous one — change:
   ```json
   "temperature": 0.3
   ```
   to
   ```json
   "temperature": 0.4
   ```
   Anything genuinely different works; whitespace and key reordering do not,
   because the comparison is between parsed dicts, not text.

6. Click **OK** (not Cancel, not Escape, not the X). `Ctrl+Return` is the
   keyboard shortcut for OK.

7. Confirm the file now exists:
   ```powershell
   dir <repo>\src\ai_srs\meta.json
   type <repo>\src\ai_srs\meta.json
   ```
   Expect a single line starting `{"config": {...}` containing
   `"temperature": 0.4`.

8. Confirm the round trip — reopen Tools → Add-ons → Config and check it still
   shows `0.4`. That proves the merge in `getConfig()` (`config.json` defaults
   overlaid with meta.json) is working, not just the write.

9. Confirm git ignores it:
   ```powershell
   git status --short
   git check-ignore -v src/ai_srs/meta.json
   ```
   `git status` must **not** list meta.json; `check-ignore` must report
   `.gitignore:25:src/ai_srs/meta.json`.

10. Optional — set `"temperature"` back to `0.3` and OK again. meta.json stays
    (it is never deleted) and now records `0.3`.

### Verified headlessly

The write path was reproduced outside the GUI against the real
`aqt.addons.AddonManager` and the real symlinked folder:

```
addonsFolder(addon) : $env:APPDATA\Anki2\addons21\ai_srs
_addonMetaPath      : $env:APPDATA\Anki2\addons21\ai_srs\meta.json
realpath of that    : <repo>\src\ai_srs\meta.json
addonConfigDefaults : OK, 23 keys
_addon_schema       : True (accept everything — no config.schema.json shipped)
getConfig()         : OK, 23 keys

open dialog, click OK with NO edits:        writeConfig called? False
change temperature to 0.4, click OK:        writeConfig called? True

--- performing the real writeConfig ---
meta.json exists now?: True
resolved to          : <repo>\src\ai_srs\meta.json
```

That test file was deleted afterwards so your manual run starts clean.

---

## 6. What is deliberately not covered by the automated suite

- **The Qt dialog itself** (`vault_dialog.py`) — checkbox and cell-edit wiring,
  the destination combo boxes, the live counter, and the enabled/disabled state
  of the *Import accepted* button. The module holds no decision logic (it edits
  a `ReviewSession` and nothing else), which is why the *decisions* are testable
  in `test_vault_import.py` — but that the clicks reach them is not.
- **The menu action** (`menu._import_from_vault_action`) — the `QueryOp`
  threading, the progress dialog, the confirmation prompt, and `mw.reset()`.
- Extraction *quality* is not locally verifiable by assertion — the same honest
  limit the knowledge-card strategy has. §2c is a judgement rubric, not a test.

All three are covered by §7 instead.

---

## 7. Steps 6–7 in real Anki — the manual plan

**Nothing here has ever run.** This is the first Engram code that *writes* to
your collection, so the ordering below goes from "cannot damage anything" to
"actually creates notes". Do §4.6 first if the add-on has never been loaded.

Two safety properties are worth knowing before you start, because they shape
what a mistake costs:

- **The ledger is only written after you commit the import.** Cancelling the
  dialog leaves every scanned note un-ingested, so a cancelled run is a true
  no-op and the same notes come back next scan.
- **The whole import is one undo entry.** `Ctrl+Z` once in the main window
  removes all the notes a single import created.

### 7.1 The graceful no-backend path (do this first — costs nothing)

The point is to confirm a dead extraction backend degrades to a message rather
than a crash or a hang.

1. Make sure `ANTHROPIC_API_KEY` is **not** visible to Anki (start Anki from a
   shell without it; `$env:ANTHROPIC_API_KEY=""` first if needed).
2. Set `vault_path` and one `vault_category_map` entry, per §2b.
3. **Tools → Engram → Import from vault…**

Expected, and each of these is a distinct claim to verify:

- The scan still finds your changed notes — a dead LLM must not break scanning.
- The confirmation prompt appears and names the provider and the note count.
- After confirming, a dialog reports **`Extraction skipped — No API key
  configured for provider 'claude'…`** and *"No candidate cards were produced."*
- **No error popup, no traceback, no freeze.** Anki's "hit an issue" dialog
  appearing at all is a bug — Engram logs to a file, never to
  stderr, because Anki shows anything on stderr as an error.
- The vault ledger is **unchanged** (`db.list_vault_files` — §4.4), so the same
  notes are offered again once a key exists.

Then repeat with `extraction_provider` set to `local` and Ollama **stopped**.
The reason line should now be the *not reachable* one, not the missing-key one.
(Since Phase 2 the shipped config resolves a local extraction model from the
category's `models_by_subject` entry, then `default`; before that it stopped at
*"Provider 'local' has no model configured"*.)

### 7.2 The dialog, with real candidates but no import

Set the key, re-run *Import from vault…*, and **click Cancel at the end**.

Check, in the dialog:

- One row per candidate, with **Front** and **Back** readable without opening
  the cell (rows are height-capped at 140px; a long card is scrollable).
- One **destination combo box per category**, pre-filled with the deck that
  category is mapped to in *Setup categories…*, or `<Category>::Original` when
  it has none. It is editable — type a deck that doesn't exist and confirm the
  counter still says the card will import (it is created at import time).
- **Unchecking** a row drops the counter by one. **Editing** a Front or Back
  cell bumps the *"N edited"* figure.
- **Edit a Back to be empty** → that row stops counting as importable even
  though it is still checked. (This is `CandidateReview.importable`; a blank
  side would create an unanswerable card.)
- **Accept all** does *not* re-check rows flagged `[duplicate]`. **Reject all**
  unchecks everything and the *Import accepted* button goes **disabled**.
- Blank a destination box entirely → the counter reports *"N accepted but have
  no destination deck"* and those rows stop being importable.

Then **Cancel** and confirm the two no-op properties: no new notes in the
browser, and the ledger has no new rows.

### 7.3 The first real import — one card

Re-run, **reject everything except one card**, and click *Import accepted*.

Verify, in this order:

1. The summary dialog reports `Imported 1 card(s)`, the deck it went to, and
   `N vault note(s) marked as ingested`.
2. **Browse → `tag:engram-vault`** finds exactly that note.
3. It is a **Basic** note, Front/Back as shown in the dialog (including any
   edit you made), in the deck the combo box named.
4. It does **not** carry `ai-srs-generated`. This matters: auto-generation skips
   anything with that tag, and a vault card is source material that must stay
   eligible for the practice pipeline.
5. `Ctrl+Z` once removes it. Redo or re-import to continue.
6. The ledger now lists **every** note that was reviewed — including ones whose
   candidates you rejected — with `cards_imported` set per note (§4.4).
7. **Re-run *Import from vault…* immediately.** It must report nothing new: the
   ledger is doing its job. Touch one note in Obsidian and only that one returns.

### 7.4 Duplicate detection

Re-run the scan with `--force`-equivalent behaviour (edit the note so its mtime
moves, or delete its ledger row) so the same candidates come back.

- The card you already imported should be labelled **`[duplicate]`**, unchecked,
  with a tooltip naming the existing note id.
- *Accept all* leaves it unchecked; checking it manually and importing creates a
  genuine second copy (that is the intended escape hatch, not a bug).

### 7.5 Things most likely to be wrong, since none of this has run

Watch specifically for:

- **The dialog opening behind the progress window**, or the progress window not
  closing — a `QueryOp` sequencing problem.
- **A UI freeze during extraction.** The extraction calls run off the main
  thread; a freeze means something slow leaked onto it.
- **A note landing in the wrong deck** when two categories are in one batch.
- **`mw.reset()` not refreshing** the deck list, so new cards seem missing until
  you switch screens.
- A category with candidates but **no combo box**, or vice versa.

Per the project's manual-test discipline: clear `__pycache__`, fully quit Anki
between runs, and verify against the sidecar DB and the browser directly rather
than trusting the summary dialog.

---

## 8. The vault picker in real Anki — manual plan

The picker is now **step 1 of *Import from vault…***, before everything in §7.
It replaces `vault_category_map` as the *required* input: only `vault_path` must
be set, and you choose folders + category per import.

`test_vault_select.py` covers what a selection *resolves to* and how it routes.
What it cannot cover — and what this section is for — is the tri-state tree
widget, the combo boxes, and the auto-prefill interaction.

### 8.1 The tree renders your vault correctly

Set only `vault_path` (you can leave `vault_category_map` empty entirely) and
open **Tools → Engram → Import from vault…**.

- The tree shows your real folder structure, **folders before files**, one level
  expanded.
- `.obsidian/`, `.trash/`, `.git/` do **not** appear.
- Non-markdown files do **not** appear.
- The count in the intro line is plausible for your vault.
- Non-ASCII folder and file names render correctly.
- If `vault_include_globs` is narrow (e.g. `["Weekly/**/*.md"]`), only matching
  files appear — this is the same walk the scan uses, so a file visible here is
  exactly a file the scan would see.

### 8.2 Checking behaviour (the tri-state logic)

This is the part with no automated coverage. Check specifically:

- Checking a **folder** checks every descendant, including nested subfolders.
- Unchecking one child turns the parent **partially checked** (a filled square),
  not unchecked.
- Unchecking the last checked child turns the parent fully unchecked.
- A partially-checked parent, clicked, goes to fully checked and cascades down.
- The counter reads *"N note(s) selected"* and matches what you checked —
  including files inside collapsed folders.
- **Continue is disabled** until both a file is checked *and* a category is set.

### 8.3 The filter box

- Typing filters rows; a folder stays visible when a descendant matches, so the
  path to a hit is never hidden.
- **Check something, then filter it out of view.** The counter must not change —
  filtering hides rows, it does not deselect them. Clear the filter and confirm
  it is still checked.

### 8.4 Category and deck pre-fill

With `vault_category_map` set to e.g. `{"Weekly/Physics": "Physics"}`:

- Checking `Weekly/Physics` **auto-fills** the category box with `Physics`, and
  the destination deck follows from your `deck_category_map` (or
  `Physics::Original`).
- **Type your own category, then check a different mapped folder.** The box must
  **not** overwrite what you typed — auto-fill stops once you choose.
- Checking two folders mapped to *different* categories leaves the box blank
  (there is no single honest answer).
- Checking an unmapped folder leaves it blank — type anything, no config needed.
  **This is the headline case for this feature: confirm it works with
  `vault_category_map` completely empty.**

### 8.5 Routing actually takes effect

Pick one folder, a **brand-new** category name, and a **new** deck name, then go
through extraction and import.

1. The review dialog's destination box shows **your typed deck**, not
   `<Category>::Original` — the picked deck overrides the reverse lookup.
2. After import, the notes are in that deck (created if it did not exist).
3. The post-import summary carries the *"has no model or card type configured
   yet"* line, pointing at *Setup categories…*.
4. Nothing was written to `vault_category_map` (you left *Remember* unticked).

### 8.6 The Remember checkbox

Repeat with **Remember this folder → category** ticked.

- The summary reports what was remembered.
- **Tools → Add-ons → Engram → Config** now shows that entry in
  `vault_category_map`, merged alongside any existing entries — not replacing
  them.
- Re-open the picker: checking that folder now auto-fills the category.
- **Cancel the review dialog on a remembered run instead of importing.** Nothing
  should be written — the config write follows the same "only after commit" rule
  as the ingestion ledger.

### 8.7 The force checkbox and the ledger

- Import from a folder, then pick the same folder again → *"Nothing to import…
  they are all unchanged"*, naming the checkbox as the way out.
- Tick **Re-scan even if unchanged** and re-pick → the same notes come back and
  are re-extracted (this **spends** API calls again; the confirmation still
  names the count first).

### 8.8 Things most likely to be wrong

- **Tri-state parent state** after unchecking a deeply-nested grandchild.
- **Auto-prefill overwriting a category you typed** — the one interaction with
  real state to get wrong.
- A **large vault** freezing the dialog on open (the cap is
  `vault_select.MAX_TREE_FILES = 5000`; over it the dialog says it truncated).
- The deck box **not following** an auto-filled category.
- Cancelling the picker leaving a progress dialog on screen.

---

## 9. Phase 2 — per-profile isolation, JSON mode, cold start

Automated: `test_profile_store.py` (layout, settings split/merge, fingerprint
guard, rename adoption, every migration rule), `test_json_robustness.py`
(forgiving parser, JSON-mode request fields, the HTTP 400 fallback),
`test_cold_start.py` (probe, long first timeout spent once, dead backend fails
fast, cloud APIs untouched).

### 9.1 Migration of the old shared DB (in Anki)

1. Quit Anki. Note what is in `src/ai_srs/user_files/` (`pipeline.sqlite`) and
   which per-profile keys `meta.json` holds (`deck_category_map`, `vault_*`).
2. Delete `src/ai_srs/__pycache__`, launch `anki.exe -p EngramTest`.
3. `ai_srs.log` must show `profile store: migrated legacy state to profile
   'EngramTest'` (when the old DB's card IDs are in EngramTest) and
   `action=migrated`.
4. On disk: `user_files/profiles/EngramTest/pipeline.sqlite` (same row counts
   as the old DB), `.../settings.json` (the deck map and vault settings),
   `user_files/pipeline.sqlite.migrated-to-EngramTest`, `user_files/legacy_migrated.json`;
   `meta.json` no longer contains the per-profile keys.
5. *Setup categories…* still shows the old deck assignments; the vault picker
   still opens on the old vault path.
6. Restart Anki: the log says `action=opened`, nothing is migrated twice.

### 9.2 Isolation

Open a second, fresh profile: it gets its own empty
`user_files/profiles/<name>/` with default settings, no deck assignments, and
its log line says `action=created`. Switching back to EngramTest (File → Switch
Profile, no restart) uses EngramTest's DB again.

### 9.3 Clean install without touching the dev setup

`%APPDATA%\Anki2\addons21\ai_srs` is the dev symlink to `src/ai_srs`; installing the
`.ankiaddon` there would make Anki replace that folder. Use a separate base folder:

1. `bash scripts/build_ankiaddon.sh` (Git Bash; in PowerShell `bash` may be WSL).
2. Quit every Anki window (Anki allows one instance per user; a second launch just
   hands over to the running one).
3. `anki.exe -b C:\EngramCleanInstall` → create a profile → Tools →
   Add-ons → Install from file → `dist/ai_srs-1.0.0.ankiaddon` → restart.
   (Headless alternative used in Phase 2: `ProfileManager(base=…).create(…)` and
   `AddonManager.install(file)` with a minimal `mw` stand-in.)
4. Follow the README quickstart literally. Expect in `user_files/ai_srs.log`:
   `profile store: … action=created`.
5. Upgrade: install the file again **from inside that running Anki**. It must
   succeed (before 1.0.0 it failed on Windows with "Access is denied" renaming
   `user_files`) and the profile's store must survive.
6. Delete: Tools → Add-ons → select Engram → Delete. It must remove the whole
   folder (before 1.0.0 it failed on Windows with WinError 32 on the open
   `ai_srs.log` and left a half-deleted folder).

Done on screen on 2026-11-20: steps 3–4 and 6 passed; the first AI card took
44 s (cold model).

### 9.4 Cold start

`ollama stop qwen2.5:7b` (unloads the model), then trigger a generation. The log
shows `first request to model=qwen2.5:7b … timeout 180s` and the card is
generated without a timeout. With the server down the generation fails at once
(`LLM server not reachable`) and falls back.

## 10. The testing menu items (`show_dev_menu`)

The release menu shows only *Getting started*, *Test connection*, *Setup
categories*, *Import from vault* and *Choose vault folder*. The testing items
(*Generate test sentence…*, *Populate sidecar from deck…*, *Create Learning
card for word…* and the *Dev / test* submenu with Set recycle_count / Set
fail_streak / Force PASS / Force FAIL) are hidden unless the add-on config has:

```json
"show_dev_menu": true
```

Add the key in **Tools → Add-ons → Engram → Config** (it is not in the shipped
`config.json`, so it doesn't appear in a user's config), then **restart Anki**:
the menu is built once at startup. Any value other than `true` (missing,
`false`, `"true"` as a string) keeps the items hidden. The decision is covered by
`tests/test_release_menu.py`.

# Engram — configuration

Restart Anki after editing.

For what is still untested and how to verify it (including a live API-key run),
see [`TESTING.md`](../../TESTING.md) at the repo root.

## Where config actually lives

This section is the precise answer to "I edited a value — which file did it land
in, and is that file in the repo?" Short version: **the file you edit is not in
this repo, and the files in this repo are never where a real API key goes.**

### Per-profile settings and data (since 1.0)

Some settings only make sense for one Anki profile, because they name that
profile's decks or its owner's vault. They are **not** in `meta.json` and not in
Anki's config editor; Engram keeps them per profile:

```
addons21/ai_srs/user_files/profiles/<profile name>/settings.json   deck_category_map, vault_path,
                                                                   vault_include_globs, vault_category_map
addons21/ai_srs/user_files/profiles/<profile name>/pipeline.sqlite the sidecar DB (card IDs, vault ledger)
```

*Setup categories…*, *Choose vault folder…* (also asked on the first *Import from vault…*) and the vault picker write these for you. Everything else
(providers, models, card types, thresholds, timeouts) stays global in
`meta.json`. A copy of a per-profile key left in `meta.json` is ignored.

Each `pipeline.sqlite` records which collection it belongs to. If the folder
ever ends up next to a different collection, Engram refuses to use it (one
warning, nothing in the collection is touched) rather than apply another
collection's card IDs. Renaming a profile in Anki keeps its data.

**Upgrading from 0.x:** the old shared `user_files/pipeline.sqlite` and the
per-profile keys in `meta.json` move, once, to the first profile whose
collection contains the cards the old DB refers to (or to the first profile
opened, if it refers to none). The old file is renamed to
`pipeline.sqlite.migrated-to-<profile>`, never deleted;
`user_files/legacy_migrated.json` records who claimed it.

### The three files, and which one is authoritative

Anki's add-on config is a two-file system, and this repo adds a third that is
documentation only:

| File | Shipped in the repo? | Written by Anki? | Role |
|---|---|---|---|
| `config.json` | yes — `src/ai_srs/config.json` | never | **Defaults.** Read via `addonConfigDefaults()`. Every key must exist here or it does not exist at all. |
| `meta.json` | no — created by Anki, outside the repo | yes, but **only when a value actually changes** | **Your real values.** Anki stores your edits under its `"config"` key. See *Why meta.json may not exist yet* below. |
| `config.md` | yes — `src/ai_srs/config.md` (this file) | never | **Help text only.** Anki renders it beside the config editor. Nothing here is ever loaded as a value. |

What the add-on actually sees is the merge, done by Anki in
`aqt/addons.py::getConfig()`:

```python
config = self.addonConfigDefaults(addon)   # config.json
meta   = self.addonMeta(addon)             # meta.json
config.update(meta.get("config", {}))      # meta.json wins
```

> **The merge is SHALLOW — one level only.** `config.update(...)` replaces
> whole top-level values. If `meta.json` carries a `providers` key, it replaces
> the *entire* shipped `providers` map — Anki does not merge `providers.claude`
> into `providers.local`. So when you edit `providers` in Anki's config editor,
> keep **every** provider entry you want in the block, not just the one you
> changed. The same trap applies to `models_by_subject`,
> `vault_category_map`, and `deck_category_map`.

### Why meta.json may not exist yet

**A missing meta.json is normal, not a fault.** Anki creates it lazily, and for
a manually-installed (copied or symlinked) add-on there are only two things that
ever create it — neither of which happens just by opening the config screen:

1. **Anki's add-on installer.** `install()` writes a meta.json carrying the
   add-on's name, mod time, and version bounds, read out of `manifest.json`. It
   runs for *Install from file…* and for AnkiWeb downloads. **It does not run
   for a manual copy or a symlink**, so a dev install never gets this one.
2. **Saving a changed config.** `ConfigEditor.accept()` in `aqt/addons.py` ends
   with:

   ```python
   if new_conf != self.conf:
       self.mgr.writeConfig(self.addon, new_conf)
   ```

   The write is **guarded by an inequality check**. Opening
   Tools → Add-ons → *Engram* → **Config** and clicking **OK** without editing
   anything compares equal, so `writeConfig` is never called and no file
   appears. Cancel, Escape, and the window's X close via `reject()`, which never
   writes either. **Only OK after a real edit creates meta.json.**

Two consequences of having no meta.json, both harmless and both useful as
signals:

- `addonMeta()` returns `{}`, so `getConfig()` is `config.json` alone. The
  add-on runs entirely on its shipped defaults — which is correct behaviour, not
  a degraded state.
- `AddonMeta.provided_name` is `None`, and `human_name()` falls back to the
  folder name. **The add-on therefore lists as `ai_srs` rather than `Engram`**
  in Tools → Add-ons until a meta.json exists. Seeing `ai_srs` there is
  confirmation of a manual install with no meta.json yet, not a broken manifest.

Things that are *not* the cause, ruled out by inspection of Anki 25.9.4:

- **Schema rejection.** `AddonManager._addon_schema()` returns `True` — a schema
  accepting everything — unless the add-on ships a `config.schema.json`. Engram
  does not ship one, so `jsonschema.validate()` cannot reject anything, and no
  key in `providers` or the `vault_*` block can be refused. (Anki does **not**
  auto-generate a schema from `config.json`; the config screen is a plain JSON
  text editor.)
- **`config.json` failing to parse.** `addonConfigDefaults()` returns `None` on
  a parse error, and `onConfig()` then shows *"Add-on has no configuration"*
  instead of the editor. If the editor opened at all, `config.json` parsed.
- **The symlink blocking the write.** Windows resolves writes through a
  directory symlink normally;
  `...\addons21\ai_srs\meta.json` resolves to
  `<repo>\src\ai_srs\meta.json` and writes land there.
- **`config.md` breaking the dialog.** `addonConfigHelp()` renders it through
  `markdown.markdown(...)`; this file renders without error.

### The exact path to meta.json on Windows

```
C:\Users\<you>\AppData\Roaming\Anki2\addons21\ai_srs\meta.json
```

How each piece is derived, from Anki's own source:

- `%APPDATA%\Anki2` — `aqt/profiles.py`: on Windows the base folder is
  `os.path.join(get_appdata(), "Anki2")`.
- `addons21` — `aqt/profiles.py::addonFolder()` → `os.path.join(self.base, "addons21")`.
  Note this sits at the **base** level, *not* inside `Anki2\<profile>\`, so it is
  shared by every profile. There is one meta.json per add-on, not one per profile.
- `ai_srs` — the add-on's folder name. For an install-from-file (`.ankiaddon`)
  or a manual copy, that is the `"package"` value in `manifest.json`, which this
  add-on sets to `ai_srs`. Installed from AnkiWeb instead, the folder would be a
  numeric add-on ID and the path would be
  `...\addons21\<numeric-id>\meta.json`.
- `meta.json` — `aqt/addons.py::_addonMetaPath()` →
  `os.path.join(self.addonsFolder(module), "meta.json")`.

Sibling files Anki puts in that same folder: `user_files/` (the sidecar
`pipeline.sqlite` and the runtime log) — also outside the repo.

### Is meta.json inside the git repo?

**No.** To be completely explicit:

- The repo is wherever you cloned it, e.g. `C:\Users\<you>\Engram`.
- meta.json is `C:\Users\<you>\AppData\Roaming\Anki2\addons21\ai_srs\meta.json`.

These are two separate directory trees. Nothing under `AppData\Roaming` is
reachable from the repo, so on a normal install a real API key placed in
meta.json is **physically outside the repository** and cannot be committed even
by accident.

**The one exception — a symlink install.** `scripts/install_symlink.sh` links
`addons21\ai_srs` → `<repo>\src\ai_srs`. With that install, "the add-on folder"
*is* the repo folder, so Anki writes:

```
<repo>\src\ai_srs\meta.json      <- INSIDE the repo
<repo>\src\ai_srs\user_files\    <- INSIDE the repo
```

That is exactly why `.gitignore` lists `src/ai_srs/meta.json` and
`src/ai_srs/user_files/` — they are dead entries on a normal install and load-
bearing on a symlink install. (That script defaults to the macOS add-ons path;
on Windows set `ANKI_ADDONS_DIR`, and note Windows symlink creation needs
Developer Mode or an elevated shell.)

### Per key: where a real value goes

Keys added by steps 1–5 of the multi-provider + vault plan. "code default" means
`src/ai_srs/config.json`, which you edit only to change what a *fresh install*
starts with — editing it does **not** change an existing install, because
meta.json already overrides it.

| Config key | Where a real value goes | Notes |
|---|---|---|
| `providers` (the whole map) | **meta.json** | Shallow merge: include every provider entry, not just the changed one. |
| `providers.<name>.api_format` | meta.json | Code default ships `local`→`openai`, `claude`→`anthropic`. |
| `providers.<name>.api_base` | meta.json | Code default per format; blank falls back to the format default. |
| `providers.<name>.model` | meta.json | Code default ships `claude-haiku-4-5-20251001` for `claude`. |
| `providers.<name>.request_timeout_seconds` | meta.json | Optional; inherits the top-level value when absent. |
| **`providers.claude.api_key`** | **environment variable `ANTHROPIC_API_KEY` (recommended), or meta.json** | See the precedence rule below. Ships blank in `config.json` and stays blank. Putting the key in meta.json stores it **in plaintext on disk** and shows it in Anki's config editor. |
| `providers.claude.api_key_env` | meta.json | Names *which* env var to read. Code default for the `anthropic` format is `ANTHROPIC_API_KEY`, so you normally never set this. |
| `providers.local.api_key` | leave blank | A localhost server needs no credential; the `openai` format deliberately does **not** read any env var (see below). |
| `default_provider` | meta.json | Code default `local`. |
| `models_by_subject` (per-subject provider override, `{"provider": …, "model": …}`) | meta.json | Also written for you by Tools → Engram → **Setup categories…**. |
| `vault_path` | **per profile** (`settings.json`) | Default `""` (ingestion disabled). `~` is expanded. |
| `vault_include_globs` | **per profile** | Default `["**/*.md"]`. |
| `vault_category_map` | **per profile** | Default `{}` — with it empty, **every** note scans as unmapped and is skipped. |
| `extraction_provider` | meta.json | Code default `claude`. |
| `extraction_model` | meta.json | Ships `""` → the provider's own `model`, else the note category's `models_by_subject` entry, else its `default` (only entries that route to the extraction provider). |
| `max_cards_per_note` | meta.json | Code default `15`. |

**No config key's real value belongs in the repo.** The only repo file that
holds config *values* is `config.json`, and its role is defaults for a fresh
install — its `api_key` is `""` and must stay that way.

### Does the real add-on read ANTHROPIC_API_KEY from the environment?

**Yes — in the real add-on, not just a demo script.** The read happens in
`generator._resolve_api_key()` (`src/ai_srs/generator.py`), which is called by
`_provider_from_entry()` and `_legacy_provider()`, i.e. by
`providers_from_config()` → `resolve_provider()`. Every place the add-on builds
an LLM request goes through that one function:

- `state_machine.py` (all four card-generation transitions)
- `autogen.py` (step-9 on-graduation auto-generation)
- `reviewer_hooks.py` (the reviewer path)
- `menu.py` (Test connection, manual generate)
- `vault_ingest.py` (vault extraction)

**Precedence — meta.json wins, environment is the fallback:**

1. If `api_key` in the effective config is a non-blank, non-placeholder string,
   **that value is used** and the environment is never consulted.
2. Otherwise (blank, missing, or still a `<placeholder>` in angle brackets —
   `_is_placeholder()` treats `<from Anthropic Console>` as unset), the value of
   the environment variable is used.
3. The variable is `api_key_env` if you set one; otherwise the vendor default
   for that format, which is `ANTHROPIC_API_KEY` for `anthropic`
   (`_ENV_KEY_BY_FORMAT`).
4. If neither is set, the key is `""`. `Test connection` reports
   *"No API key configured"* without making a request that could only 401, and
   extraction/generation degrade to their normal fallback rather than raising.

So both are supported, and **a key in meta.json shadows the environment
variable.** If you set the env var but a stale key is still sitting in
meta.json, the stale one wins — blank the `api_key` field to fall through.

The env fallback is **format-gated on purpose**: only `anthropic` gets an
automatic default variable. The `openai` format never auto-reads `OPENAI_API_KEY`,
because that format is what a local Ollama / LM Studio speaks and attaching a
stray key to a localhost request would be a surprise. Any provider can still opt
in explicitly by setting `api_key_env`.

> **Windows gotcha:** the variable must be visible **to the Anki process**.
> `$env:ANTHROPIC_API_KEY = "..."` in a PowerShell window only affects programs
> launched from that window. For Anki started from the Start menu, set a
> persistent user variable (`setx ANTHROPIC_API_KEY "sk-ant-..."`, or System
> Properties → Environment Variables) and then **fully quit and restart Anki** —
> a running process does not pick up a changed environment.

### What the shipped example shows for the key field

Verified in this repo:

- `src/ai_srs/config.json` — `providers.claude.api_key` is `""` (empty string),
  and `providers.local.api_key` is `""`. Plus
  `"api_key_env": "ANTHROPIC_API_KEY"`, which is the pointer, not a secret.
- `src/ai_srs/config.md` — the `providers` example block in *The `providers` map*
  above likewise shows `"api_key": ""`.

**No real key appears anywhere in the repo**, and the only `sk-ant-…` strings
tracked are obvious fixtures in `tests/test_anthropic_provider.py`
(`"sk-ant-test"`, `"sk-ant-x"`) and the usage hints printed by
`scripts/demo_vault_pipeline.py`.

Relative to what Anki loads: `config.json` and `config.md` ship *with the code*
and end up as siblings of meta.json in
`...\addons21\ai_srs\`. Anki reads `config.json` for defaults, renders
`config.md` as help, and reads/writes **only** `meta.json` for your actual
values. The example you are reading is therefore never the file a key lands in.

### .gitignore coverage

`.gitignore` already excludes every file that could hold a real key or real user
data, verified with `git check-ignore`:

| Pattern | Covers |
|---|---|
| `src/ai_srs/meta.json` | The config file Anki writes — only reachable in a symlink install. |
| `src/ai_srs/user_files/` | Sidecar DB + runtime log. |
| `*.sqlite`, `*.sqlite-journal`, `*.db` | Stray copies of the sidecar. |
| `*.log` | Runtime logs (which record model names and endpoints). |
| `venv/`, `__pycache__/`, `*.py[cod]` | Environment and build artifacts. |
| `dist/` | Built `.ankiaddon` packages. |

`git ls-files` confirms none of meta.json, `user_files/`, `*.sqlite`, or `*.log`
is tracked. `scripts/build_ankiaddon.sh` independently excludes `meta.json`,
`user_files/`, `*.log`, `*.sqlite`, `*.db`, and `__pycache__` from the shipped
package, so a key cannot escape via a build either.

**Bottom line:** on a normal (non-symlink) install there is nothing to worry
about — meta.json lives under `%APPDATA%\Anki2\addons21\`, entirely outside the
repository, so a real key is not in a place git can see. The `.gitignore` entries
are belt-and-braces for the symlink-install case, where the add-on folder and the
repo folder are the same directory.

## Adding subjects / categories (no code changes needed)

Subjects are **fully config-driven** — `models_by_subject`, `card_types_by_subject`,
and `deck_category_map` are plain dictionaries looked up by subject name, so you can
add as many subjects as you like with **no code changes**. Three ways, easiest first:

1. **From the UI (recommended):** Tools → Engram → **Setup categories…**. Pick
   (or type) a real deck, give it a category name, choose its **Model** and **Card
   type**, and click OK. The dialog writes all four maps for you (`deck_category_map`,
   `models_by_subject`, `card_types_by_subject`, `subjects`) and shows each existing
   assignment's current model + card type. This is the no-hand-editing path.
2. **By hand in config.json:** add the subject name as a key under `models_by_subject`
   and `card_types_by_subject`, and (if it's a parent deck) to `subjects`. Example —
   adding Korean (language) and Physics (knowledge):
   ```json
   "models_by_subject":    { "Korean": "qwen2.5:7b", "Physics": "phi4", "default": "qwen2.5:7b" },
   "card_types_by_subject": { "Korean": "language",   "Physics": "knowledge" },
   "subjects": ["Korean", "Physics"]
   ```
   Any subject NOT listed falls through to `default` (model) and `default_card_type`.
3. **For auto-generation**, also map the real source deck → category with
   Setup categories…. The deck map is per profile (`user_files/profiles/<profile>/settings.json`,
   see *Per-profile settings* above), so it is not edited in the config editor.

## Recommended models (and their `ollama pull` commands)

**Every model you reference must be pulled first** — `ollama pull <model>` — or that
subject's generation fails and the add-on falls back to the original card (spec §5).
The seeded defaults:

| Subjects | Model | Pull command | Notes |
|----------|-------|--------------|-------|
| Languages (Japanese, Chinese, Korean, German, Spanish, French, English, …) | `qwen2.5:7b` | `ollama pull qwen2.5:7b` | Strong multilingual / CJK; one model serves all languages. ~5GB RAM. |
| Knowledge (History, Chemistry, Physics, Biology, …) | `qwen2.5:7b` | `ollama pull qwen2.5:7b` | Same model as languages, so one pull covers everything. Optional upgrade: `phi4` (`ollama pull phi4`), stronger reasoning for related questions but **14B → needs ~16GB RAM**; pick it per category in Setup categories. |
| `default` (anything unlisted) | `qwen2.5:7b` | `ollama pull qwen2.5:7b` | |

> **Heavier model = longer cold-start.** `phi4` (14B) takes noticeably longer to load
> into RAM on the first call and longer per generation than `qwen2.5:7b` (7B) — often
> 20–30s on a cold start. That is exactly why generation is **non-blocking**: the
> Ollama call runs off the main thread and a tooltip (*Generating AI card…* → *AI card
> ready*) shows while it works, with a one-time *Warming up model…* notice on the first
> generation of a session. The UI stays clickable throughout. If a heavy model is too
> slow on your machine, set that subject's model to `qwen2.5:7b` (or another lighter
> pulled model) in `models_by_subject`.

> **Ollama context window (knowledge cards).** Ollama's default context is **2048
> tokens**. A knowledge card feeds the *original* question + answer into the prompt, so
> a very long source Q/A could be truncated and degrade the rephrased card. To raise it,
> create a larger-context variant of the model with a Modelfile and point the subject at
> it:
> ```
> printf 'FROM phi4\nPARAMETER num_ctx 8192\n' > Modelfile
> ollama create phi4-8k -f Modelfile
> ```
> then set that subject's model to `phi4-8k` in `models_by_subject`. (Bigger `num_ctx`
> uses more RAM and is slightly slower.) Language cards are short and unaffected.

## LLM backends (providers)

The add-on is **not locked to Ollama**, and not to a single backend at a time.
It keeps two independent concepts apart:

- a **provider** is *which server* — its URL, its credential, its default model;
- an **api_format** is *the wire shape that server speaks* — how a request is
  built and how the reply is read.

Several providers can share one format, so pointing Engram at another server
that speaks a format it already knows is a config entry, not a code change.
Everything uses stdlib HTTP only (no `openai` or `anthropic` package, no
dependencies).

Supported formats:

| `api_format` | Endpoint | Auth | Works with |
|---|---|---|---|
| `openai` (default) | `POST {api_base}/chat/completions` | `Authorization: Bearer` (omitted when blank) | Ollama ≥0.1.24, LM Studio, llama.cpp server, Jan, vLLM, OpenAI/Together/Groq |
| `ollama` | `POST {api_base}/api/generate` | none | Ollama's native API |
| `anthropic` | `POST {api_base}/messages` | `x-api-key` + `anthropic-version` | The Anthropic (Claude) API |

### The `providers` map

```json
"providers": {
  "local": {
    "api_format": "openai",
    "api_base": "http://localhost:11434/v1",
    "api_key": ""
  },
  "claude": {
    "api_format": "anthropic",
    "api_base": "https://api.anthropic.com/v1",
    "api_key": "",
    "api_key_env": "ANTHROPIC_API_KEY",
    "model": "claude-haiku-4-5-20251001"
  }
},
"default_provider": "local"
```

Per-entry keys:

- `api_format` — one of the three above. An unrecognized value falls back to
  `openai` with a warning rather than breaking generation.
- `api_base` — the server's base URL. Defaults per format: `openai` →
  `http://localhost:11434/v1`, `ollama` → `http://localhost:11434`, `anthropic`
  → `https://api.anthropic.com/v1`. In `openai`/`anthropic` mode include the
  `/v1` suffix; in `ollama` mode use the bare server root. LM Studio is
  typically `http://localhost:1234/v1`, llama.cpp's server `.../8080/v1`.
- `api_key` — the credential. Leave blank for local servers.
- `api_key_env` — an environment variable to read the key from when `api_key` is
  blank or still a `<placeholder>`. For the `anthropic` format this defaults to
  `ANTHROPIC_API_KEY`, so you can keep the key out of the config file
  entirely. **Note:** this file is readable in Tools → Add-ons → Engram → Config
  and its contents are written to `meta.json` on disk, so a key set here is
  stored in plaintext — the environment variable is the safer option. The
  fallback is deliberately *not* automatic for the `openai` format, so a stray
  `OPENAI_API_KEY` can never be attached to a localhost request.
- `model` — this provider's default model, used when a subject or the extraction
  task names the provider without naming a model.
- `request_timeout_seconds` — optional per-provider timeout, overriding the
  top-level value. A cloud call and a cold local model want different budgets.

`default_provider` names the entry used when nothing else specifies one.

### Choosing a provider per subject

`models_by_subject` accepts two shapes, and both stay valid:

```json
"models_by_subject": {
  "Japanese": "qwen2.5:7b",
  "History":  {"provider": "claude", "model": "claude-haiku-4-5-20251001"},
  "Physics":  {"provider": "claude"}
}
```

A bare string means "the default provider". A dict routes that subject to a
named provider; omit `model` to use that provider's own default. So a single
collection can run local models for languages and a cloud model for the
knowledge subjects where quality matters more.

> **Back-compat:** a config written before `providers` existed still works. With
> no `providers` map, the top-level `api_base` / `api_format` / `api_key` (or,
> older still, `ollama_endpoint`) define the single provider. And if you upgrade
> with a *customized* top-level `api_base` — say LM Studio's port — it is folded
> onto the default provider so your endpoint survives the upgrade; a warning in
> the log tells you to move the setting into `providers` and clear the old key.

Use **Tools → Engram → Test connection** to probe **every** configured provider
and see each one's status and available models. When nothing is configured yet,
**Getting started…** explains the setup steps — a fresh install guides you rather
than failing silently.

## Obsidian vault ingestion

Engram can read an Obsidian vault as a plain folder of `.md` files (no Obsidian
plugin needed, Obsidian doesn't have to be running) and propose flashcards from
notes you've changed. Extracted cards are **candidates**: nothing is written to
your collection until you review and accept them. Once imported they are
ordinary notes and flow into the normal pipeline.

**Only `vault_path` is required.** *Import from vault…* opens a picker that
browses the vault as a tree; you check folders or files and name their category
and destination deck for that import, so no config editing is needed to import
from a new folder. Everything below is a default or a limit, not a prerequisite.

- `vault_path` — the vault folder. `~` is expanded. Blank disables ingestion.
- `vault_include_globs` — which files to consider, vault-relative. Default
  `["**/*.md"]`. `*` and `?` stop at a `/`; only `**` crosses directories, so
  `"Weekly/*.md"` means direct children of `Weekly` only.
- `vault_category_map` — **optional.** Folder (or whole-file) prefix → Engram
  category, e.g. `{"Weekly/Physics": "Physics", "Weekly/CS": "ComputerScience"}`.
  The longest matching key wins, the same rule `deck_category_map` uses.

  > **This is no longer a prerequisite for importing.** *Import from vault…*
  > opens a picker where you choose the folders and their category per import,
  > so a folder with no entry here imports fine. An entry only **pre-fills** the
  > picker's category box when you check that folder, and the picker's value
  > always wins for that import. Tick *Remember this folder → category* in the
  > picker to have an entry written for you (merged into the existing map, and
  > only once the import is actually committed).
- `extraction_provider` — which `providers` entry performs extraction. This is
  independent of the per-subject generation providers, so you can extract with a
  cloud model and still generate practice cards locally.
- `extraction_model` — model for extraction. Blank uses the provider's own
  `model`; if that is blank too, the note category's `models_by_subject` model,
  then `models_by_subject.default` (only entries that route to the extraction
  provider — a bare string means the default provider).
- `max_cards_per_note` — hard ceiling on candidates from one note in one run
  (default 15). The number is both stated in the prompt and enforced when
  parsing, so a long weekly document can't produce an unreviewable pile or a
  surprise API bill.

Files already ingested are tracked in the sidecar database by their modification
time, so a scan only re-processes notes that actually changed.

## Keys

- `providers`, `default_provider` — the LLM backends (see above).
- `models_by_subject` — model per subject; `default` is used when no subject matches. Each entry is either a bare model name (uses `default_provider`) or `{"provider": ..., "model": ...}` to route that subject to a named provider — see *Choosing a provider per subject* above. The model is chosen per generation call from the word's subject. **Every local model listed must be pulled (`ollama pull <model>`)** or generation falls back to the original card (spec §5). Add an entry whose key exactly matches your subject name (the subject parent deck) to give that subject its own model. Supports arbitrarily many subjects — it's just a dict lookup.
- `subjects` — list of subject parent decks that are pipeline-enabled. A card whose deck is one of these (or nested under it with `::`) maps to that subject; decks not listed stay pure traditional Anki. Used to infer the subject (and thus the model) for the reviewer banner; pipeline cards already carry their subject in the sidecar.
- `card_types_by_subject` — per-subject card-type strategy (spec §8): `"language"` (new-vocab cards: target word + example sentence on the Front; English translation + word gloss on the Back; the sentence is validated to contain the word) or `"knowledge"` (history/chem/physics/etc.: generates a NEW question/answer testing the same concept as the original card, rephrased — needs a populated `source_note_id` to read the original Q/A; validation is loose and falls back to the original Q/A). A subject with no entry uses `default_card_type`. Also supports arbitrarily many subjects.
- `default_card_type` — `"language"` or `"knowledge"`; used for subjects not listed in `card_types_by_subject`.
- `deck_category_map` — Step-9 on-demand auto-generation (Tools → Engram → *Setup categories…*). Maps a real deck name (or a parent-deck prefix) to a category (= a subject name, which provides the model and card_type). When a card in an assigned deck first graduates from learning into review, the add-on auto-generates one AI card into `<category>::Learning` — exactly once per source card, ever. Decks not listed here are ignored entirely (stay pure traditional Anki). Example: `{"Japanese::Core": "Japanese", "History": "History"}`.
- `temperature` — generation temperature passed to Ollama.
- `words_per_sentence` — target words per generated sentence (start at 1).
- `learned_retrievability` — FSRS retrievability threshold to promote `learning → recycled` (spec §4).
- `recycle_graduation` — successful recycle cycles needed to promote `recycled → legacy` (spec §4).
- `demotion_fails` — consecutive failures that trigger `recycled → learning` demotion (spec §4).
- `demotion_mode` — `consecutive` (only mode currently implemented).
- `validation_retries` — generation retries before falling back to the original card (spec §5).
- `trigger_tag` — note tag that opts a card into AI sentence rendering (Step 1 scaffold).
- `request_timeout_seconds` — HTTP timeout for an LLM call. A provider entry may override it.
- `word_field` — note field whose value is the target word during sidecar populate. `null` means use the first field (Anki's sort field). Set to a field name (e.g. `"Word"`, `"Expression"`) to override.
- `vault_path`, `vault_include_globs`, `vault_category_map`, `extraction_provider`, `extraction_model`, `max_cards_per_note` — Obsidian vault ingestion (see above).


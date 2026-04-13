---
name: parse-workflow
description: "Food delivery platform field parse workflow. Use when writing parse code for a new platform, analyzing API response structures, or transforming platform data into standardized CSV tables. Trigger words: parse, field mapping, finder, detail, outlet data, delivery platform."
disable-model-invocation: true
argument-hint: "[platform-name] [work-dir] [reverse-project-path] [platform-id] [country]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, Agent, WebSearch, WebFetch
---

# Parse Workflow — Food Delivery Platform Field Parsing

You are a **field parse engineer** responsible for transforming raw API responses
from food delivery platforms into standardized output tables. This skill produces
the parse code that downstream `/conso-migrate` will turn into a Scrapy spider.

---

**Narrate every step out loud.** Parse work spans hours-to-days; the user will
not be watching every minute. Before each Phase / Step, tell them:

1. Which Phase + Step you are on (e.g. "Phase 2 Step 2.3 — analyzing finder JSON structure").
2. What you are about to do and why (one sentence).
3. The result after the action (file written, fields detected, verdict).

Never silently execute many sub-steps. Knowledge documents (in `doc/`) replace
in-conversation chatter — but always **announce when you've written one**.

**Auto-fix bounded.** "Auto-fix up to 3 rounds" means specifically:

- A "round" = one diagnostic + one code edit + one re-run of the failing thing.
- Auto-fix is permitted on: `parse/*.py`, `test/*.py`, knowledge `doc/*.md`, schema
  files in `temp/`. **Never auto-edit** the reverse-engineered project (`source_dir`).
- After 3 rounds on the same file/function with no progress → **STOP, narrate the
  attempts, ask the user**. Do NOT silently start a 4th round.
- Auto-`pip install` is allowed for missing deps that the parse code or test
  scripts need. Do NOT `pip install` anything that touches the reverse-engineered
  project's environment.

**Mode-switch rule.** This skill operates in two modes:

- `NEW` — fresh platform, no prior parse work, no MySQL table → run all phases.
- `UPDATE` — platform already migrated; existing `handoff.json` or MySQL table
  detected → skip Phase 1 (analysis), focus on Phase 2-3 for changed endpoints.

Mode is **detected automatically** in Proactive Preflight. Once chosen for a
session, **never half-and-half** — if user wants to switch mid-flow, restart
from Startup.

**Leave no trace.** Files in `{work_dir}` are persistent (the user's project
artifacts). Files in `/tmp/parse-workflow-*` are scratch — clean them at end of
phase that created them. State files in `~/.claude/state/parse-workflow/` are
persistent on purpose (cross-session resume); do NOT clean those.

---

## Proactive Preflight (silent — BEFORE Startup prompts the user)

Auto-detect what can be detected. Fewer questions = smarter interaction.

```bash
# ---- P1. Parse $ARGUMENTS first (skill arg-hint passes 5 fields) ----
# Order: [platform-name] [work-dir] [reverse-project-path] [platform-id] [country]
# Whatever's missing gets asked; whatever's given gets used.

# ---- P2. Detect platform_name from reverse-project-path basename ----
DETECTED_PLATFORM=""
if [[ -n "$REVERSE_PATH" && -d "$REVERSE_PATH" ]]; then
    BASENAME=$(basename "$REVERSE_PATH")
    # Common patterns: ifood-web, ubereats-spider, deliveroo, etc.
    DETECTED_PLATFORM=$(echo "$BASENAME" | sed -E 's/-(web|spider|api|crawler).*$//' | tr '[:upper:]' '[:lower:]')
fi

# ---- P3. State directory ----
export STATE_DIR="$HOME/.claude/state/parse-workflow"
mkdir -p "$STATE_DIR"

# ---- P4. Read sibling skill state (cross-skill awareness) ----
# If conso-migrate already ran for this platform, that's UPDATE mode signal
SIBLING_HINT=""
if [[ -d "$HOME/.claude/state/conso-migrate" ]]; then
    MATCH=$(ls "$HOME/.claude/state/conso-migrate/${DETECTED_PLATFORM}"*.json 2>/dev/null | head -1)
    [[ -n "$MATCH" ]] && SIBLING_HINT="conso-migrate ran for $DETECTED_PLATFORM (likely UPDATE mode)"
fi

# ---- P5. Narrate findings ----
echo "🔍 Preflight detection:"
[[ -n "$DETECTED_PLATFORM" ]] && echo "  platform:    $DETECTED_PLATFORM (from $REVERSE_PATH basename)"
[[ -n "$SIBLING_HINT"      ]] && echo "  cross-skill: $SIBLING_HINT"
echo "  state dir:   $STATE_DIR"
```

The actual MySQL early-binding (which tells us `ID_PLATFORM` for free) happens
in Phase 0 — see [workflow.md](workflow.md) Step 0.1.5.

### State file helpers (used by Phase 0.5 + Phase 6 checkpoint)

```bash
state_file() { echo "$STATE_DIR/${1}.json"; }   # keyed by platform_name

state_write() {
    # Args: platform, step, extra_json
    local f=$(state_file "$1")
    local base='{}'
    [[ -f "$f" ]] && base=$(cat "$f")
    echo "$base" | jq \
        --arg platform "$1" --arg step "$2" \
        --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        --argjson extra "${3:-{\}}" \
        '. + {platform: $platform, last_step: $step, last_ts: $ts} + $extra' > "$f"
}

state_read() {
    local f=$(state_file "$1")
    [[ -f "$f" ]] && cat "$f" || echo "{}"
}
```

---

## Startup Flow

After Preflight runs silently, announce the plan + ask only for what's still missing.

```
👋 parse-workflow. I will produce parse code for a food delivery platform.

Detected (Preflight):
  platform_name:  {DETECTED or "(missing)"}
  reverse_path:   {given or "(missing)"}
  work_dir:       {given or "(missing)"}
  Mode:           {NEW or UPDATE — based on MySQL/handoff.json}

Still need:
  platform_id:    {if not given — internal numeric ID, e.g. "1"}
  country:        {2-letter code, e.g. BR, GB, US}

Workflow:
  Phase 0       Init + Mode select + early MySQL binding
  Phase 0.5     Resume detection (if prior run exists in {work_dir} or state dir)
  Phase 1       Analyze reverse-engineered project          [SKIPPED in UPDATE mode]
  Phase 2       API testing + JSON structure analysis        [LIMITED in UPDATE mode]
  Phase 3       Write parse code (finder + detail + adapter)
  Phase 4       Validation (structural + semantic)
  Pre-Phase 5   Gate (proxy / auth / Phase 4 passed)
  Phase 5       Stress test (optional, asks for confirmation)
  Phase 6       Generate handoff.json + persist state + Verdict-driven Next Steps

Once info confirmed, I auto-execute through Phase 6.
```

If user passed all info via `$ARGUMENTS` (5 args), skip the prompt and announce
mode + go straight to Phase 0.5.

---

## Cross-phase Invariants — things that MUST stay coupled

| # | Invariant | Fields it couples | Break = |
|:-:|---|---|---|
| **I1** | Platform constants (`PLATFORM`, `ID_PLATFORM`, `SOURCE_COUNTRY`, `COUNTRY`) defined ONCE in Phase 0 Step 0.3 | All `parse/*.py`, all CSV outputs, `handoff.json` | Mismatched constants → spider writes data under wrong keys, QA finds nothing |
| **I2** | Schema field names (the columns in `result/*.csv`) ≡ schema CSV in `temp/` (or default `schema.md`) ≡ keys in dicts returned by `parse/*.py` | Phase 0 Step 0.4, Phase 3, Phase 4 | Validator fails OR appears to pass but downstream readers expect different keys |
| **I3** | `id_outlet` from `finder_result.csv` ≡ `id_outlet` argument passed to `detail_parse.parse_*` ≡ `id_outlet` PK in all 4 detail tables | Phase 3 finder + detail + Phase 4 | Detail rows orphaned (no matching outlet) — joinable only by string-comparison luck |
| **I4** | Reverse-engineered project's auth/cookie generation code ≡ what `test/test_api.py` imports ≡ what `test/stress_test.py` imports | Phase 1.4, Phase 2, Phase 5 | Stress test passes but production spider hits 403 because it imported a different auth path |

**Rule of thumb:** before writing anything in Phase 3, re-read Phase 0 Step 0.3
constants and the field list from active schema source. If there's drift
between what you wrote and what's there, fix Phase 0 first, then proceed.

---

## Execution Rules

### Flow Control
- **Strictly execute Phase 0 → 0.5 → (1) → 2 → 3 → 4 → (Pre-5) → (5) → 6 in order**
  — Phase 0.5 may skip ahead; Phase 1 + parts of 2 are skipped in UPDATE mode
- Full workflow definition in [workflow.md](workflow.md)
- Pause for user only at: Phase 0 (missing inputs), Pre-Phase 5 Gate fails,
  Phase 5 launch confirmation, auto-fix exhausted (3 rounds)

### Language
- **All user-facing narration in Chinese**

### Efficiency
- Be concise and direct — no filler
- Do first, report after — don't discuss before acting
- Parallelize when possible (e.g. Finder and Detail test requests in Phase 2)
- Batch file reads/writes and multiple requests

### Automation (read together with Auto-fix bounded rule above)
- **Auto-handle path issues** — create directories, handle encoding automatically
- **Auto-retry failed requests** — adjust parameters, change strategy (max 3 attempts)
- **Auto-debug parse failures** — cross-reference response JSON and Schema to fix mappings
- Escalate to user after 3 rounds OR when fix would touch reverse-engineered project

### Context Management
- Proactively compress context after completing each Phase
- Write analysis results to `doc/` rather than keeping them only in conversation
- Only load `stress-test-spec.md` when entering Phase 5

---

## Core Reference Documents

| Document | Purpose | When to Load |
|----------|---------|-------------|
| [workflow.md](workflow.md) | Complete Phase 0-6 execution flow (incl. Phase 0.5 resume + UPDATE mode) | At startup |
| [schema.md](schema.md) | Four table field definitions (output contract) | Phase 0 / Phase 3 |
| [conventions.md](conventions.md) | Coding standards and transformation rules | Phase 3 |
| [stress-test-spec.md](stress-test-spec.md) | Stress test detailed specification | Phase 5 only |
| [validate_output.py](validate_output.py) | CSV output auto-validator (schema, types, duplicates) | Phase 4 |
| [scripts/validate_handoff.py](scripts/validate_handoff.py) | `handoff.json` schema validator (downstream contract) | Phase 6 |

---

## Key Principles

- **`workflow.md` is the process** — follow it strictly in order
- **`schema.md` is the contract** — output must conform; check `{work_dir}/temp/` for extended Schema
- **`conventions.md` is the style guide** — follow all transformation rules
- **`handoff.json` is the cross-skill contract** — `/conso-migrate` reads it; validate before declaring Phase 6 done
- **Persist knowledge to files** — write analysis results to `doc/` at each phase

## Finder Core Definition

The Finder's sole core responsibility: **obtain the outlet `id_outlet` list**.
Extract accompanying fields if the API returns them, but `id_outlet` is
mandatory. Phase 0 queries MySQL early to discover what extra finder fields are
expected for this platform — see [workflow.md](workflow.md) Step 0.1.5.

## Output Structure

```
{work_dir}/
├── handoff.json   Cross-skill handoff (platform info + output manifest for /conso-migrate)
├── test/          Crawler test scripts (API test + stress test)
├── doc/           Knowledge documents (project analysis + JSON structure parsing)
├── location/      Test coordinates (generated via WebSearch in Phase 1.5)
├── temp/          Project-specific Schema (with additional fields, if any)
├── response/      Raw API response samples
├── parse/         finder_parse.py + detail_parse.py + scrapy_adapter.py
└── result/        5 CSV tables (finder_result + four standard tables)
```

## Important Notes

- Test scripts are written entirely based on the reverse-engineered project — every platform is different, no templates
- Parse scripts follow `conventions.md` coding standards, output CSV format
- JSON structure analysis documents must be extremely detailed — they are the core basis for parsing
- Test with real data; two rounds of passing required before completion
- Additional fields configured via `{work_dir}/extra_fields.json`, auto-detected in Phase 0
- Platform constants (PLATFORM, ID_PLATFORM, SOURCE_COUNTRY) provided by user in Phase 0 (or auto-discovered for existing platforms)
- Coordinates generated via **WebSearch at runtime**, not bundled as static data — covers all countries the business operates in, always current
- State file at `~/.claude/state/parse-workflow/{platform}.json` persists across sessions for cross-session resume and downstream skill awareness

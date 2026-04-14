---
name: grid-gen
description: "Generate hexagonal grid points for ConSo finder spiders. Covers country/city scope, H3 hex generation, S3 upload, Redis push, CASS update, and push_grids.py sync. Use when the user says: grid, grids, hexgrid, point grid, finder grid, push grid, generate grid, 生成网格, 推送网格, 网格点."
disable-model-invocation: true
argument-hint: "[platform] [prefix] [distance] [mode]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, Agent
---

# ConSo Grid Generation & Push

Generate hexagonal (honeycomb) grid point maps for ConSo finder spiders, upload
to S3, push to Redis, update CASS configuration, and sync `push_grids.py`.

Grid points are the **input fuel for finder spiders** — without them, the finder
has nowhere to search and returns zero outlets. Every grid point becomes one API
request at runtime: `pop grid → build request → discover outlets nearby`.

---

**Narrate every step out loud.** Grid data flows through S3 → Redis → finder
spider → production crawl. A wrong grid distance, a mismatched Redis key, or an
accidental overwrite of a production grid silently breaks the entire finder —
**no error, just zero outlets**. Before every phase or sub-step, tell the user:

1. Which phase/step you are on (e.g. "Phase 2 — generating H3 hex grid at resolution 5").
2. What you are about to do and why (one-two sentences).
   Example: "I'll generate 3000m-spaced hex points covering all cities >100k
   population in NL — this feeds the TKW finder spider's Redis queue."
3. The result after the action (point count, file size, S3 path, Redis count).

Never silently execute a block of commands.

**Auto-fix bounded.** "Auto-fix up to 3 rounds" means specifically:

- A "round" = one diagnostic + one code edit + one re-run of the failing thing.
- Auto-fix is permitted on: `scripts/generate_grid.py` invocation arguments,
  push_grids.py CONFIGS, CASS update parameters. **Never auto-edit** the finder
  spider code or framework internals.
- After 3 rounds on the same issue with no progress → **STOP, narrate the
  attempts, ask the user**. Do NOT silently start a 4th round.
- Auto-`pip install` is allowed for missing deps (`h3`, `shapely`, `requests`).

**Mode rule.** This skill operates in two modes:

- **FULL** (default) — generate + S3 + Redis + CASS + push_grids.py sync.
  Requires being inside a spider project directory (`scrapy.cfg` present).
- **GENERATE-ONLY** — generate + S3 upload only. No Redis, no CASS, no
  push_grids.py. Use when the spider project doesn't exist yet (pre-migration)
  or when the user only needs to update S3.

Mode is **detected automatically** in Proactive Preflight (based on `scrapy.cfg`
presence). If the user explicitly requests Redis/CASS operations but there's no
spider project, STOP and explain that those phases need `dashmote_sourcing`
installed via `poetry`.

**Leave no trace.** Temp files (`/tmp/grid-gen-*.json`, scratch boundary files)
must be cleaned at end of phase. Files in `location/` are persistent project
artifacts (but NOT committed — `.gitignore`'d). State files in
`~/.claude/state/grid-gen/` are persistent on purpose; don't clean those.

---

## Proactive Preflight (silent — BEFORE Startup prompts the user)

Auto-detect what can be detected. Fewer questions = smarter interaction.

```bash
# ---- P1. Parse $ARGUMENTS (skill arg-hint: [platform] [prefix] [distance] [mode]) ----
# Whatever's given gets used; whatever's missing gets asked.

# ---- P2. Platform from git remote ----
DETECTED_PLATFORM=""
if git remote get-url origin >/dev/null 2>&1; then
    DETECTED_PLATFORM=$(basename "$(git remote get-url origin)" .git)
    # Sanity: expect 2-5 uppercase letters (ConSo ID)
    [[ "$DETECTED_PLATFORM" =~ ^[A-Z]{2,5}$ ]] || DETECTED_PLATFORM=""
fi

# ---- P3. Spider project directory → determines FULL vs GENERATE-ONLY mode ----
DETECTED_SPIDER_DIR=""
DETECTED_MODE="generate-only"
if [[ -f "./scrapy.cfg" ]]; then
    DETECTED_SPIDER_DIR="$(pwd)"
    DETECTED_MODE="full"
fi

# ---- P4. Existing grids in location/ ----
DETECTED_EXISTING_GRIDS=""
if [[ -d "./location" ]]; then
    DETECTED_EXISTING_GRIDS=$(ls -1 ./location/*_grid.json 2>/dev/null | head -10)
fi

# ---- P5. push_grids.py existence + current CONFIGS prefixes ----
DETECTED_PUSH_SCRIPT=""
DETECTED_PUSH_PREFIXES=""
if [[ -f "./scripts/push_grids.py" ]]; then
    DETECTED_PUSH_SCRIPT="$(pwd)/scripts/push_grids.py"
    # Extract existing prefix keys from CONFIGS dict
    DETECTED_PUSH_PREFIXES=$(grep -oP "'[A-Z]{2}'" ./scripts/push_grids.py \
        | tr -d "'" | sort -u | paste -sd, 2>/dev/null || echo "")
fi

# ---- P6. AWS account from STS ----
export AWS_REGION="${AWS_REGION:-eu-central-1}"
export AWS_ACCOUNT_ID=$(MSYS_NO_PATHCONV=1 aws sts get-caller-identity \
    --query Account --output text --region "$AWS_REGION" 2>/dev/null)

# ---- P7. State directory ----
export STATE_DIR="$HOME/.claude/state/grid-gen"
mkdir -p "$STATE_DIR"

# ---- P8. Cross-skill: read conso-migrate state ----
SIBLING_HINT=""
if [[ -n "$DETECTED_PLATFORM" && -d "$HOME/.claude/state/conso-migrate" ]]; then
    MATCH=$(ls "$HOME/.claude/state/conso-migrate/${DETECTED_PLATFORM}"*.json 2>/dev/null | head -1)
    if [[ -n "$MATCH" ]]; then
        SIBLING_PREFIXES=$(jq -r '.prefixes // empty' "$MATCH" 2>/dev/null)
        SIBLING_HINT="conso-migrate ran for $DETECTED_PLATFORM (prefixes: $SIBLING_PREFIXES)"
    fi
fi

# ---- P9. Skill script path ----
: "${CLAUDE_SKILL_DIR:=$HOME/.claude/skills/grid-gen}"
export GRID_GEN_SCRIPT="$CLAUDE_SKILL_DIR/scripts/generate_grid.py"
# Fallback: check repo copy
[[ -f "$GRID_GEN_SCRIPT" ]] || GRID_GEN_SCRIPT="$(pwd)/../grid-gen/scripts/generate_grid.py"
[[ -f "$GRID_GEN_SCRIPT" ]] || GRID_GEN_SCRIPT=""

# ---- P10. Narrate findings ----
echo "🔍 Preflight detection:"
[[ -n "$DETECTED_PLATFORM"       ]] && echo "  platform:        $DETECTED_PLATFORM (from git remote)"
[[ -n "$DETECTED_SPIDER_DIR"     ]] && echo "  spider_dir:      $DETECTED_SPIDER_DIR (scrapy.cfg found)"
echo "  mode:            $DETECTED_MODE ($([ "$DETECTED_MODE" = "full" ] && echo "spider project found — Redis+CASS enabled" || echo "no spider project — S3 only"))"
[[ -n "$DETECTED_EXISTING_GRIDS" ]] && echo "  existing grids:  $(echo "$DETECTED_EXISTING_GRIDS" | wc -l | tr -d ' ') file(s) in location/"
[[ -n "$DETECTED_PUSH_SCRIPT"    ]] && echo "  push_grids.py:   found (prefixes: ${DETECTED_PUSH_PREFIXES:-none})"
[[ -n "$AWS_ACCOUNT_ID"          ]] && echo "  aws account:     $AWS_ACCOUNT_ID"
[[ -n "$SIBLING_HINT"            ]] && echo "  cross-skill:     $SIBLING_HINT"
[[ -n "$GRID_GEN_SCRIPT"         ]] && echo "  gen script:      $GRID_GEN_SCRIPT"
echo "  state dir:       $STATE_DIR"
```

### State file helpers (used by Smart entry + every checkpoint)

```bash
state_file() { echo "$STATE_DIR/${1}-${2}.json"; }   # keyed by platform-prefix

state_write() {
    # Args: platform, prefix, step, extra_json
    local f=$(state_file "$1" "$2")
    local base='{}'
    [[ -f "$f" ]] && base=$(cat "$f")
    echo "$base" | jq \
        --arg platform "$1" --arg prefix "$2" \
        --arg step     "$3" --arg ts     "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        --argjson extra "${4:-{\}}" \
        '. + {platform: $platform, prefix: $prefix,
              last_step: $step, last_ts: $ts} + $extra' > "$f"
}

state_read() {
    local f=$(state_file "$1" "$2")
    [[ -f "$f" ]] && cat "$f" || echo "{}"
}
```

---

## Startup — Self-Introduction

```
👋 grid-gen. I will generate hex grid points for a ConSo finder spider.

Detected (Preflight):
  platform:       {DETECTED or "(missing — need from user)"}
  spider_dir:     {DETECTED or "(not in spider project)"}
  mode:           {FULL or GENERATE-ONLY}
  existing grids: {count or "none"}
  push_grids.py:  {found (prefixes: XX,YY) or "not found"}

Still need from user:
  PREFIX          2-letter country code (e.g. NL, RU, KZ)
  DISTANCE        grid spacing in meters (default: 3000)
  COVERAGE_MODE   A. cities-only  B. full-country

Workflow:
  Phase 0       Variable resolution (single source of truth)
  Phase 0.6     Smart entry (resume detection + S3/Redis existence check)
  Phase 1       Scope definition (cities/buffer — only if cities-only mode)
  Phase 2       Grid generation (bundled script)
  Phase 3       S3 upload (with overwrite protection)
                                                        ┐
  Pre-Phase 4   Gate (6 STOP-style checks)              │ FULL mode only
  Phase 4       Redis push (test + prod, with safety)   │ (skipped in
  Phase 5       CASS update (finder_geo_distance)       │  GENERATE-ONLY)
  Phase 6       push_grids.py sync                      ┘
  Phase 7       Verification + State + Next Steps

Confirm or override detected values, then provide missing inputs.
```

Wait for user confirmation before proceeding.

---

## Phase 0 — Variable Resolution (single source of truth)

### 0.1 — Collect remaining inputs

After Preflight + user confirmation, all of these must be resolved:

| Input | Source | Validation |
|---|---|---|
| `ID_PLATFORM` | Preflight P2 or user | 2-5 uppercase letters |
| `PREFIX` | User (or Preflight cross-skill hint) | 2-letter uppercase |
| `DISTANCE` | User (default: 3000) | Integer, one of: 500, 1000, 1500, 2000, 3000, 5000, 7000 |
| `COVERAGE_MODE` | User | `cities-only` or `full-country` |

### 0.2 — Platform S3 name mapping

```python
PLATFORM_S3_NAMES = {
    "TKW": "thuisbezorgd",
    "DLR": "deliveroo",
    "JSE": "justeat",
    "YDE": "yandex_eats",
    "DRD": "doordash",
    "UBE": "ubereats",
    "GGM": "grubhub",
    "EFD": "efood",
    "HGS": "hungerstation",
    "SPF": "shopee_food",
    "NOO": "noon",
    "SMD": "smood",
    "DLH": "deliveryhero",
    # If platform not listed → ask user
}
```

If `ID_PLATFORM` is not in the mapping, ask the user and verify:
```bash
MSYS_NO_PATHCONV=1 aws s3 ls s3://dash-dbcenter/config/ --region eu-central-1
```

### 0.3 — Derived variables

```bash
# From user / preflight (all resolved by now)
export ID_PLATFORM="..."   # e.g. TKW
export PREFIX="..."         # e.g. NL
export DISTANCE="..."       # e.g. 3000
export COVERAGE_MODE="..."  # e.g. cities-only

# Derived (Invariant I1 — single place these strings are defined)
export GRID_FILENAME="${PREFIX}_${DISTANCE}_grid.json"
export PLATFORM_S3_NAME="..."   # from mapping above
export S3_KEY="config/${PLATFORM_S3_NAME}/${GRID_FILENAME}"
export S3_URI="s3://dash-dbcenter/${S3_KEY}"
export REDIS_KEY="${ID_PLATFORM}:${PREFIX}:${DISTANCE}_grid"
export CASS_GEO_DISTANCE="${DISTANCE}_grid"
export LOCAL_GRID_PATH="$(pwd)/location/${GRID_FILENAME}"

# Execution mode (from Preflight P3)
export EXEC_MODE="..."     # "full" or "generate-only"
```

### 0.4 — Resolved table — print to user

| Variable | Value | Used in |
|---|---|---|
| `ID_PLATFORM`       | `TKW`             | Redis key, CASS, push_grids.py |
| `PREFIX`             | `NL`              | Grid filename, Redis key, CASS |
| `DISTANCE`           | `3000`            | Grid spacing, filename, CASS |
| `COVERAGE_MODE`      | `cities-only`     | Phase 1 / script `--mode` |
| `PLATFORM_S3_NAME`   | `thuisbezorgd`    | S3 config path |
| `GRID_FILENAME`      | `NL_3000_grid.json` | Local + S3 filename (I4) |
| `S3_URI`             | `s3://dash-dbcenter/config/thuisbezorgd/NL_3000_grid.json` | Phase 3 |
| `REDIS_KEY`          | `TKW:NL:3000_grid` | Phase 4 (I1) |
| `CASS_GEO_DISTANCE`  | `3000_grid`       | Phase 5 (I1) |
| `EXEC_MODE`          | `full` / `generate-only` | Phase 4-6 skip logic |
| `AWS_ACCOUNT_ID`     | (dynamic)         | All AWS calls |

STOP and confirm with user before proceeding.

### 0.6 — Smart entry: resume detection

Check state file AND live infrastructure to determine the best entry point.

```bash
LAST=$(state_read "$ID_PLATFORM" "$PREFIX")
HAS_HIST=$(echo "$LAST" | jq -r 'if . == {} then "no" else "yes" end')

if [[ "$HAS_HIST" == "yes" ]]; then
    LAST_TS=$(echo "$LAST"       | jq -r '.last_ts // ""')
    LAST_STEP=$(echo "$LAST"     | jq -r '.last_step // ""')
    LAST_DISTANCE=$(echo "$LAST" | jq -r '.distance // ""')
    LAST_POINTS=$(echo "$LAST"   | jq -r '.point_count // ""')
    LAST_VERDICT=$(echo "$LAST"  | jq -r '.verdict // ""')
    AGE_HOURS=$(python3 -c "
from datetime import datetime,timezone
t = datetime.fromisoformat('${LAST_TS}'.replace('Z','+00:00'))
print(int((datetime.now(timezone.utc) - t).total_seconds() / 3600))
" 2>/dev/null || echo 999)

    echo ""
    echo "⏮️  Found previous run for ${ID_PLATFORM}/${PREFIX}:"
    echo "    distance:    ${LAST_DISTANCE}m"
    echo "    points:      ${LAST_POINTS}"
    echo "    last step:   $LAST_STEP"
    echo "    verdict:     $LAST_VERDICT"
    echo "    age:         ${AGE_HOURS}h ago"
    echo ""

    # ---- Determine resume point based on last_step ----
    if [[ "$LAST_DISTANCE" == "$DISTANCE" ]]; then
        case "$LAST_STEP" in
            generated)
                echo "    Grid exists locally. Options:"
                echo "      1. Resume from Phase 3 (S3 upload)"
                echo "      2. Regenerate from scratch"
                echo "      3. Abort"
                ;;
            s3_uploaded)
                echo "    Grid on S3. Options:"
                echo "      1. Resume from Pre-Phase 4 Gate (Redis push)"
                echo "      2. Re-upload to S3 first"
                echo "      3. Abort"
                ;;
            redis_pushed)
                echo "    Redis already pushed. Options:"
                echo "      1. Resume from Phase 5 (CASS update)"
                echo "      2. Re-push Redis"
                echo "      3. Abort"
                ;;
            cass_updated)
                echo "    CASS already updated. Options:"
                echo "      1. Resume from Phase 6 (push_grids.py sync)"
                echo "      2. Redo CASS update"
                echo "      3. Abort"
                ;;
            completed)
                echo "    ✅ Previous run completed successfully."
                echo "    Options:"
                echo "      1. Re-run entirely (different coverage/cities)"
                echo "      2. Abort (nothing to do)"
                ;;
            *)
                echo "    Unknown last step '$LAST_STEP'. Starting fresh."
                ;;
        esac
    else
        echo "    Distance changed (was ${LAST_DISTANCE}m, now ${DISTANCE}m)."
        echo "    New generation required — starting from Phase 1."
    fi
fi

# ---- Also check live S3 ----
S3_EXISTS=$(MSYS_NO_PATHCONV=1 aws s3 ls "$S3_URI" --region "$AWS_REGION" 2>/dev/null)
if [[ -n "$S3_EXISTS" ]]; then
    echo ""
    echo "📦 Grid already exists on S3:"
    echo "    $S3_URI"
    echo "    $S3_EXISTS"
    echo "    ⚠️  Overwriting affects ALL future finder runs for ${ID_PLATFORM}/${PREFIX}."
fi
```

---

## Cross-phase Invariants — things that MUST stay coupled

| # | Invariant | Fields it couples | Break = |
|:-:|---|---|---|
| **I1** | `REDIS_KEY` = `{ID_PLATFORM}:{PREFIX}:{DISTANCE}_grid` — defined ONCE in Phase 0.3; must match push_grids.py CONFIGS `key_suffix`, finder spider `self.db_key`, and CASS `finder_geo_distance` | Phase 0.3, Phase 4, Phase 5, Phase 6 | Finder spider pops from a Redis key that has zero entries — returns zero outlets, no error |
| **I2** | `H3_RESOLUTION` ↔ `DISTANCE` mapping is deterministic — 3000m→res5, 1500m→res6, etc. Changing one without the other produces a grid whose spacing doesn't match the filename | Phase 0.3, Phase 2 (script) | Grid file says "3000" but actual spacing is 1500m — point count 4× expected |
| **I3** | `S3_KEY` = `config/{PLATFORM_S3_NAME}/{GRID_FILENAME}` — must match the `s3_key` in push_grids.py CONFIGS | Phase 0.3, Phase 3, Phase 6 | push_grids.py loads from wrong S3 path → empty grid → finder starved |
| **I4** | `GRID_FILENAME` on local disk (`location/`) ≡ S3 object name ≡ push_grids.py reference | Phase 2, Phase 3, Phase 6 | Local file `NL_3000_grid.json` but S3 has `NL_3000.json` — silent mismatch |
| **I5** | Grid JSON format `{"data": [{"lat": float, "lon": float}]}` ≡ push_grids.py `loader='standard_json'` + `fields='lat,lon'` ≡ finder spider `start_requests()` grid string parsing | Phase 2 (script), Phase 4, finder runtime | Grid generated but finder can't parse entries — `ValueError` on every pop |

**Rule of thumb:** after Phase 2 (generation), re-verify that `GRID_FILENAME`,
`S3_KEY`, `REDIS_KEY`, and `CASS_GEO_DISTANCE` all reference the same distance
value from Phase 0.3. If any has drifted, fix Phase 0 first.

---

## Phase 1 — Scope Definition (cities-only mode)

**Skip this phase entirely if `COVERAGE_MODE = full-country`** — no city
selection needed.

### 1.1 — City selection (only if cities-only mode)

```
Which cities to cover in {PREFIX}?
  1. All major cities (population > 100k)
  2. Top N cities by population
  3. Specific cities (comma-separated): Amsterdam, Rotterdam, ...
```

### 1.2 — Buffer radius override

```
City buffer radius (default: auto-scaled by population)?
  Population > 1M:   30km
  Population > 500k: 20km
  Population > 100k: 15km
  Population > 50k:  10km

  Override all to Xkm? (press Enter for auto)
```

### 1.3 — Print plan and confirm

```
=== Grid Generation Plan ===
  Platform:     {ID_PLATFORM}
  Prefix:       {PREFIX}
  Mode:         {cities-only / full-country}
  Distance:     {DISTANCE}m
  Cities:       {list or "all >100k" or "full country"}
  Exec mode:    {FULL or GENERATE-ONLY}

  Local file:   location/{GRID_FILENAME}
  S3 target:    {S3_URI}
  Redis key:    {REDIS_KEY}             {FULL mode only}
  CASS value:   {CASS_GEO_DISTANCE}     {FULL mode only}

Proceed? (Y/n)
```

---

## Phase 2 — Grid Generation (bundled script)

The generation logic is encapsulated in `scripts/generate_grid.py`. This script:
- Handles H3 v3/v4 version detection automatically
- Retries Overpass API up to 3 times with exponential backoff
- Emits structured JSON on stdout, narration on stderr
- Validates output (non-zero points, valid format)

### 2.1 — Dependencies check

```bash
pip install h3 shapely requests 2>/dev/null
```

### 2.2 — Run generation

```bash
# Build CLI args from Phase 0 + Phase 1 inputs
SCRIPT_ARGS=(
    --prefix "$PREFIX"
    --distance "$DISTANCE"
    --mode "$COVERAGE_MODE"
    --output "$LOCAL_GRID_PATH"
)

# Add city-specific args if applicable
[[ -n "$CITIES" ]]        && SCRIPT_ARGS+=(--cities "$CITIES")
[[ -n "$TOP_N" ]]         && SCRIPT_ARGS+=(--top-n "$TOP_N")
[[ -n "$MIN_POP" ]]       && SCRIPT_ARGS+=(--min-population "$MIN_POP")
[[ -n "$BUFFER_KM" ]]     && SCRIPT_ARGS+=(--buffer-km "$BUFFER_KM")

# Ensure location/ exists and is gitignored
mkdir -p location
grep -q '^location/' .gitignore 2>/dev/null || echo -e '\n# Grid files\nlocation/' >> .gitignore

# Run
GEN_OUT=$(python "$GRID_GEN_SCRIPT" "${SCRIPT_ARGS[@]}")

# Parse structured output
echo "$GEN_OUT" | jq '.'
POINT_COUNT=$(echo "$GEN_OUT" | jq -r '.point_count')
FILE_SIZE=$(echo "$GEN_OUT"   | jq -r '.file_size_bytes')
VERDICT=$(echo "$GEN_OUT"     | jq -r '.verdict')
```

**Inline error decisions for Phase 2:**

| If you see… | Likely cause | Do |
|---|---|---|
| `h3 not installed` | Missing dependency | `pip install h3` — auto-fix allowed |
| `shapely` / `requests` import error | Missing dependency | `pip install shapely requests` — auto-fix |
| Overpass returns empty `elements` | Wrong country code, or `place=city` filter too strict for this country | Re-run with `--min-population 50000` or `--min-population 20000` |
| `429 Too Many Requests` | Overpass rate-limited | Script retries automatically; if still fails, wait 60s and re-run |
| Timeout after all retries | Overpass overloaded | Ask user to retry later, or provide boundary GeoJSON manually |
| Zero points generated | Boundary data empty or H3 resolution too low | Check Overpass returned data; try different country code format |
| Point count 10× expected | Wrong H3 resolution (I2 drift) | Verify `--distance` matches intent |
| `GRID_GEN_SCRIPT` not found | Skill not installed at expected path | Search: `find ~ -name generate_grid.py 2>/dev/null`; or use the repo copy |

### 2.3 — Sanity checks

| Check | Expected | If FAIL |
|---|---|---|
| `POINT_COUNT > 0` | At least 1 point | Boundary empty — re-check Overpass data |
| Point count reasonable | See table below | 10× → wrong resolution; 0.1× → boundary too small |
| `jq '.' "$LOCAL_GRID_PATH"` succeeds | Valid JSON | Encoding issue — regenerate |
| File has `.data[]` array | Standard format (I5) | Script bug — check output |

Expected point counts per 10,000 km²:

| Distance | Points / 10k km² |
|----------|-----------------|
| 1000m | ~86,000 |
| 1500m | ~38,000 |
| 2000m | ~22,000 |
| 3000m | ~10,000 |
| 5000m | ~3,500 |

Persist checkpoint:
```bash
state_write "$ID_PLATFORM" "$PREFIX" "generated" \
    "$(jq -n --arg d "$DISTANCE" --arg n "$POINT_COUNT" \
        '{distance: $d, point_count: ($n|tonumber), phase: "2"}')"
```

---

## Phase 3 — S3 Upload

### 3.1 — Check existing S3 object

```bash
EXISTING=$(MSYS_NO_PATHCONV=1 aws s3 ls "$S3_URI" --region "$AWS_REGION" 2>/dev/null)
```

If file exists:
```
⚠️  Grid already exists on S3:
    {S3_URI}
    {EXISTING}

This grid may be in active use by the finder spider.
Overwriting affects ALL future finder runs for {ID_PLATFORM}/{PREFIX}.

Overwrite? (Y/n)
```

**Wait for explicit confirmation before overwriting.**

### 3.2 — Upload

```bash
MSYS_NO_PATHCONV=1 aws s3 cp "$LOCAL_GRID_PATH" "$S3_URI" --region "$AWS_REGION"
```

### 3.3 — Verify upload

```bash
S3_VERIFY=$(MSYS_NO_PATHCONV=1 aws s3 ls "$S3_URI" --region "$AWS_REGION" 2>/dev/null)
LOCAL_SIZE=$(wc -c < "$LOCAL_GRID_PATH" | tr -d ' ')
```

Confirm S3 object exists and size is reasonable (within 5% of local file).

**Inline error decisions for Phase 3:**

| If you see… | Likely cause | Do |
|---|---|---|
| `AccessDenied` | MFA expired or no s3:PutObject on `dash-dbcenter` | Re-MFA; check IAM policies |
| `NoSuchBucket` | Wrong bucket name | Verify: bucket is `dash-dbcenter`, NOT `dash-sourcing` |
| Upload succeeds but `s3 ls` returns empty | Wrong path or brief delay | Wait 5s, retry; check `S3_KEY` matches Phase 0 (I3) |
| Size mismatch >5% | Partial upload | Re-upload; check disk/network |

Persist checkpoint:
```bash
state_write "$ID_PLATFORM" "$PREFIX" "s3_uploaded" \
    "$(jq -n --arg uri "$S3_URI" '{s3_uri: $uri, phase: "3"}')"
```

**If `EXEC_MODE = generate-only` → skip to Phase 7 (state + next steps).**

---

## Pre-Phase 4 Gate (STOP-style, all 6 must pass)

**This gate and Phases 4-6 only run in FULL mode.**

Before pushing to Redis (which feeds production spiders), verify every assumption.

### G1 — AWS MFA valid

```bash
ACTUAL_ACCT=$(MSYS_NO_PATHCONV=1 aws sts get-caller-identity \
    --query Account --output text --region "$AWS_REGION" 2>/dev/null)
[[ "$ACTUAL_ACCT" == "$AWS_ACCOUNT_ID" ]] \
    && echo "✅ G1 MFA valid (account $AWS_ACCOUNT_ID)" \
    || { echo "❌ G1 FAIL: expected '$AWS_ACCOUNT_ID', got '$ACTUAL_ACCT'"; }
```

### G2 — Grid file exists on S3

```bash
MSYS_NO_PATHCONV=1 aws s3 ls "$S3_URI" --region "$AWS_REGION" >/dev/null 2>&1 \
    && echo "✅ G2 grid exists on S3" \
    || { echo "❌ G2 FAIL: $S3_URI not found — Phase 3 may have failed"; }
```

### G3 — Grid point count > 0

```bash
POINT_COUNT=$(python3 -c "
import json
with open('$LOCAL_GRID_PATH') as f:
    print(len(json.load(f).get('data', [])))
")
(( POINT_COUNT > 0 )) \
    && echo "✅ G3 grid has $POINT_COUNT points" \
    || { echo "❌ G3 FAIL: grid file is empty"; }
```

### G4 — REDIS_KEY matches Invariant I1

```bash
EXPECTED_KEY="${ID_PLATFORM}:${PREFIX}:${DISTANCE}_grid"
[[ "$REDIS_KEY" == "$EXPECTED_KEY" ]] \
    && echo "✅ G4 Redis key matches I1" \
    || { echo "❌ G4 FAIL: $REDIS_KEY != $EXPECTED_KEY"; }
```

### G5 — Spider project detected (scrapy.cfg)

```bash
[[ -f "./scrapy.cfg" ]] \
    && echo "✅ G5 spider project detected" \
    || { echo "❌ G5 FAIL: no scrapy.cfg — cannot run Redis/CASS phases"; }
```

### G6 — No active finder task for this platform

```bash
ACTIVE_FINDER=$(MSYS_NO_PATHCONV=1 aws ecs list-tasks --cluster conso-cluster \
    --family "conso_${ID_PLATFORM,,}_spider" --desired-status RUNNING \
    --region "$AWS_REGION" --query 'taskArns' --output text 2>/dev/null || echo "")
if [[ -n "$ACTIVE_FINDER" && "$ACTIVE_FINDER" != "None" ]]; then
    echo "⚠️  G6 WARN: active finder task detected for ${ID_PLATFORM}"
    echo "    Pushing new grid while finder is running replaces the Redis queue mid-crawl."
    echo "    Recommend: wait for current run to finish, then push."
else
    echo "✅ G6 no active finder task"
fi
```

### Gate summary

```
G1 MFA valid (correct account)       [✅ / ❌]
G2 Grid exists on S3                  [✅ / ❌]
G3 Grid has >0 points                 [✅ / ❌]
G4 Redis key matches I1               [✅ / ❌]
G5 Spider project detected            [✅ / ❌]
G6 No active finder task              [✅ / ⚠️]
```

All ✅ → proceed to Phase 4. Any ❌ → STOP, narrate, ask user.
⚠️ G6 is informational — narrate the risk, ask user to confirm before proceeding.

---

## Phase 4 — Redis Push

### 4.1 — Check existing Redis key (pre-push awareness)

Before pushing, check if the key already has data — this reveals whether we're
creating a new grid or **replacing a production grid**.

```bash
poetry run python -c "
from dashmote_sourcing.db import RedisDriver
prod = RedisDriver(test=False)
existing = prod.connection.zcard('${REDIS_KEY}')
print(f'Existing prod Redis entries: {existing}')
if existing > 0:
    print('⚠️  This key has live data. Pushing will DELETE and REPLACE it.')
    print('    If a finder spider is actively popping from this key,')
    print('    it will see the queue suddenly refilled mid-run.')
"
```

If existing count > 0, **narrate the risk and ask user to confirm** before proceeding.

### 4.2 — Push via push_grids.py (preferred)

If `scripts/push_grids.py` exists and has the CONFIGS entry for this prefix:

```bash
poetry run python scripts/push_grids.py
```

### 4.3 — Push inline (fallback — if push_grids.py missing or prefix not in CONFIGS)

```python
import json
from dashmote_sourcing.db import RedisDriver

with open(LOCAL_GRID_PATH) as f:
    points = json.load(f)["data"]

for env_name, test_flag in [("test", True), ("prod", False)]:
    redis = RedisDriver(test=test_flag)
    # SAFETY: delete + repush is atomic per-key; warn was shown in 4.1
    redis.connection.delete(REDIS_KEY)
    pipe = redis.connection.pipeline()
    for p in points:
        member = f"{p['lat']},{p['lon']}"
        pipe.zadd(REDIS_KEY, {member: 0})
    pipe.execute()
    count = redis.connection.zcard(REDIS_KEY)
    print(f"{env_name} Redis: {count} entries in {REDIS_KEY}")
```

### 4.4 — Verify Redis counts

```bash
poetry run python -c "
from dashmote_sourcing.db import RedisDriver
test = RedisDriver(test=True)
prod = RedisDriver(test=False)
key = '${REDIS_KEY}'
tc = test.connection.zcard(key)
pc = prod.connection.zcard(key)
print(f'Test Redis:  {tc}')
print(f'Prod Redis:  {pc}')
print(f'Expected:    ${POINT_COUNT}')
if tc == pc == ${POINT_COUNT}:
    print('✅ All counts match')
elif abs(tc - ${POINT_COUNT}) <= ${POINT_COUNT} * 0.001:
    print('⚠️  Counts within 0.1% — acceptable (Redis deduplicates identical coordinates)')
else:
    print('❌ COUNT MISMATCH — investigate before proceeding')
"
```

**Inline error decisions for Phase 4:**

| If you see… | Likely cause | Do |
|---|---|---|
| `ConnectionRefusedError` | Redis not reachable (VPN? SSH tunnel?) | Check `dashmote_sourcing` settings; ensure tunnel or VPN active |
| `ModuleNotFoundError: dashmote_sourcing` | Not in spider project's poetry env | `cd` to spider project root; `poetry install`; retry |
| Test count OK but prod count = 0 | Prod Redis requires SSM tunnel to EC2 | Use SSM-based push — same pattern as `conso-migrate` Phase 7.2 |
| Count mismatch >1% | Partial push (network interruption) | Delete key, re-push |

Persist checkpoint:
```bash
state_write "$ID_PLATFORM" "$PREFIX" "redis_pushed" \
    "$(jq -n --arg tc "$TEST_COUNT" --arg pc "$PROD_COUNT" \
        '{test_redis_count: ($tc|tonumber), prod_redis_count: ($pc|tonumber), phase: "4"}')"
```

---

## Phase 5 — CASS Update

### 5.1 — Locate cass_insert.py

```bash
# Dynamic discovery — search multiple possible locations
CASS_INSERT_PATH=""
for candidate in \
    "$HOME/.claude/skills/conso-migrate/cass_insert.py" \
    "$HOME/.claude/commands/conso-migrate/cass_insert.py" \
    "$(find "$HOME/.claude" -name cass_insert.py -print -quit 2>/dev/null)"; do
    if [[ -f "$candidate" ]]; then
        CASS_INSERT_PATH="$candidate"
        break
    fi
done

if [[ -z "$CASS_INSERT_PATH" ]]; then
    echo "❌ cass_insert.py not found. Search result:"
    find "$HOME/.claude" -name "cass_insert.py" 2>/dev/null || echo "  (none)"
    echo "Ask user for path or skip CASS update."
fi
```

### 5.2 — Check current CASS value (before overwriting)

```bash
# Run with --verify only (dry check) to see current value
CURRENT_GEO=$(poetry run python "$CASS_INSERT_PATH" \
    --id-platform "$ID_PLATFORM" --prefixes "$PREFIX" --verify 2>&1 \
    | grep -i "finder_geo_distance" || echo "(unknown)")
echo "Current CASS finder_geo_distance: $CURRENT_GEO"
echo "New value:                        $CASS_GEO_DISTANCE"
```

If the current value differs from the new value, warn:
```
⚠️  Changing finder_geo_distance from '{old}' to '{CASS_GEO_DISTANCE}'.
    This will affect the next finder spider run for {ID_PLATFORM}/{PREFIX}.
    Confirm? (Y/n)
```

### 5.3 — Update

```bash
poetry run python "$CASS_INSERT_PATH" \
    --id-platform "$ID_PLATFORM" \
    --prefixes "$PREFIX" \
    --finder-geo-distance "$CASS_GEO_DISTANCE" \
    --verify
```

Confirm output shows `finder_geo_distance` = `{CASS_GEO_DISTANCE}`.

**Inline error decisions for Phase 5:**

| If you see… | Likely cause | Do |
|---|---|---|
| `cass_insert.py` not found (after search) | Skill not installed | Ask user for path |
| Database connection error | VPN/tunnel not active, or RDS credentials expired | Check `dashmote_sourcing` DB settings |
| `finder_geo_distance` unchanged after update | Script bug or wrong `--prefixes` | Re-run with `--verbose`; verify prefix spelling |

Persist checkpoint:
```bash
state_write "$ID_PLATFORM" "$PREFIX" "cass_updated" \
    "$(jq -n --arg geo "$CASS_GEO_DISTANCE" '{cass_geo_distance: $geo, phase: "5"}')"
```

---

## Phase 6 — push_grids.py Sync

If `scripts/push_grids.py` exists, ensure it has the correct CONFIGS entry.
If it doesn't exist, **warn but don't create from scratch** — that's
`/conso-migrate` Phase 7's job.

### 6.1 — Check existing CONFIGS

```bash
if [[ -f "./scripts/push_grids.py" ]]; then
    grep -q "'${PREFIX}'" ./scripts/push_grids.py \
        && echo "✅ CONFIGS entry for '${PREFIX}' exists" \
        || echo "⚠️  CONFIGS entry for '${PREFIX}' missing — will add"
else
    echo "⚠️  scripts/push_grids.py not found — skip (create via /conso-migrate Phase 7)"
fi
```

### 6.2 — Add CONFIGS entry (if push_grids.py exists but prefix missing)

Add inside the `CONFIGS = {` dict:

```python
'${PREFIX}': dict(
    s3_key='config/${PLATFORM_S3_NAME}/${GRID_FILENAME}',
    loader='standard_json',
    fields='lat,lon',
    key_suffix='${DISTANCE}_grid',
),
```

After adding, verify invariants:
- **I1 check:** `key_suffix` = `${DISTANCE}_grid` matches `CASS_GEO_DISTANCE`
  and `REDIS_KEY` suffix.
- **I3 check:** `s3_key` matches `S3_KEY` from Phase 0.

### 6.3 — Verify finder spider grid_name default

```bash
FINDER_FILE=$(find . -name "conso_outlet_finder.py" -path "*/spiders/*" 2>/dev/null | head -1)
if [[ -n "$FINDER_FILE" ]]; then
    echo "Finder spider: $FINDER_FILE"
    grep -n "grid_name" "$FINDER_FILE"
fi
```

If finder uses `grid_name=prefix` (old pattern) instead of
`grid_name='{DISTANCE}_grid'`:
```
⚠️  Finder spider's grid_name default is '{PREFIX}' but REDIS_KEY uses
    '{DISTANCE}_grid'. The finder will look for key '{ID_PLATFORM}:{PREFIX}:{PREFIX}'
    instead of '{REDIS_KEY}'.

    Fix: update finder's __init__ to: grid_name=kwargs.get('grid_name', '{DISTANCE}_grid')
    Or: pass grid_name='{DISTANCE}_grid' via SpiderKeeper at runtime.
```

---

## Phase 7 — Verification + State + Next Steps

### 7.1 — End-to-end verification table

Print a summary table. In GENERATE-ONLY mode, omit Redis/CASS/push_grids rows.

```
=== Grid Generation Complete ===

| Check | Status | Value |
|-------|--------|-------|
| Grid generated         | ✅ | {POINT_COUNT} points, {DISTANCE}m spacing |
| Local file             | ✅ | location/{GRID_FILENAME} ({FILE_SIZE} KB) |
| S3 upload              | ✅ | {S3_URI} |
| Test Redis             | ✅ | {REDIS_KEY} — {TEST_COUNT} records      | ← FULL only
| Prod Redis             | ✅ | {REDIS_KEY} — {PROD_COUNT} records      | ← FULL only
| CASS finder_geo_dist   | ✅ | {CASS_GEO_DISTANCE}                     | ← FULL only
| push_grids.py synced   | ✅ | CONFIGS['{PREFIX}'].key_suffix matches   | ← FULL only
| I1 Redis↔CASS↔finder   | ✅ | all reference '{DISTANCE}_grid'          | ← FULL only
| I3 S3 path consistent  | ✅ | push_grids.py s3_key matches S3_URI      | ← FULL only
| I5 Grid format valid   | ✅ | {"data": [{"lat","lon"}]} — standard_json |
```

### 7.2 — Persist final state

```bash
FINAL_VERDICT="success"
[[ "$EXEC_MODE" == "generate-only" ]] && FINAL_VERDICT="success_generate_only"

state_write "$ID_PLATFORM" "$PREFIX" "completed" \
    "$(jq -n \
        --arg verdict      "$FINAL_VERDICT" \
        --arg distance     "$DISTANCE" \
        --arg point_count  "$POINT_COUNT" \
        --arg s3_uri       "$S3_URI" \
        --arg redis_key    "$REDIS_KEY" \
        --arg cass_geo     "$CASS_GEO_DISTANCE" \
        --arg exec_mode    "$EXEC_MODE" \
        --arg test_count   "${TEST_COUNT:-0}" \
        --arg prod_count   "${PROD_COUNT:-0}" \
        '{verdict: $verdict, distance: ($distance|tonumber),
          point_count: ($point_count|tonumber), s3_uri: $s3_uri,
          redis_key: $redis_key, cass_geo_distance: $cass_geo,
          exec_mode: $exec_mode,
          test_redis_count: ($test_count|tonumber),
          prod_redis_count: ($prod_count|tonumber)}')"

echo "📝 State persisted to $STATE_DIR/${ID_PLATFORM}-${PREFIX}.json"
```

### 7.3 — Verdict-driven Next Steps

Pick the SPECIFIC next action based on `EXEC_MODE` and outcome:

```
✅ FULL mode — all checks passed:
    Grid is live. The finder spider can discover outlets in {PREFIX}.

    → Test: trigger a sample finder run (sample=5) via SpiderKeeper
        to confirm grid works end-to-end
    → If finder not yet deployed:
        /conso-deploy to set up ECR + EventBridge + CASS activation
    → If this is a new prefix for an existing platform:
        /run-detail {ID_PLATFORM} to crawl detail data after finder completes
    → Additional prefixes:
        /grid-gen (re-invoke with different PREFIX)

✅ GENERATE-ONLY mode — grid on S3:
    Grid uploaded but not pushed to Redis or registered in CASS.

    → When the spider project is ready:
        /conso-migrate Phase 7 will handle push_grids.py + Redis + CASS
    → Or re-invoke /grid-gen from inside the spider project directory
        for FULL mode (Phase 4-6)

⚠️ Partial success (some checks failed):
    Review the verification table above.
    Fix failed items, then re-invoke /grid-gen —
    Smart entry will detect the previous attempt and offer to resume
    from the exact point of failure.

❌ Generation failed:
    Check Phase 2 error table.
    Most common: Overpass API timeout → retry later.
```

---

## Appendix: Grid Distance Guidelines

| Distance | Points / 10k km² | H3 Resolution | Use case |
|----------|-----------------|---------------|----------|
| 500m | ~345,000 | 7-8 | City-level micro coverage (rarely needed) |
| 1000m | ~86,000 | 6 | Small countries, premium platforms |
| 1500m | ~38,000 | 6 | Dense coverage |
| 2000m | ~22,000 | 5-6 | Medium coverage |
| 3000m | ~10,000 | 5 | **Standard (recommended default)** |
| 5000m | ~3,500 | 4 | Large countries, testing |
| 7000m | ~1,800 | 4 | Very sparse, initial testing only |

**Rule of thumb:** Start with 3000m. If finder returns too few outlets, decrease
to 2000m or 1500m. If grid has >100k points, increase the distance.

---

## Notes

- **Grid files are NOT committed to git.** They live in `location/` (gitignored)
  and on S3. `push_grids.py` is the committed reference for which grids exist.
- **S3 bucket is `dash-dbcenter`** (config storage), NOT `dash-sourcing` (crawl
  output). Don't confuse them.
- **Redis sorted sets use score 0** for all members — the spider pops randomly
  via `ZPOPMIN`, not by score ordering.
- **CASS `finder_geo_distance`** is a string label, not a number — it's the
  Redis key suffix (e.g. `3000_grid`), not the distance in meters.
- **State files** live in `~/.claude/state/grid-gen/{platform}-{prefix}.json`.
  Persistent across invocations (Smart entry depends on them); don't clean as
  part of leave-no-trace.
- **Windows (Git Bash):** All `aws` CLI calls that take `/`-prefixed arguments
  use `MSYS_NO_PATHCONV=1` to prevent Git Bash from converting S3 paths to
  Windows paths. The bundled `generate_grid.py` is pure Python and works on
  all platforms.
- **`generate_grid.py`** emits JSON on stdout and narration on stderr — pipe
  stdout through `jq` for structured processing, read stderr for human context.

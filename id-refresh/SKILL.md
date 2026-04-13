---
name: id-refresh
description: "Re-crawl specific outlet IDs for a sourcing spider. Pushes IDs from a CSV file to Redis, runs the detail spider (locally OR on Fargate), verifies S3 output, and triggers QA. Use when the user says: refresh IDs, re-crawl IDs, push IDs and run spider, id refresh for <platform> <country> <month>."
---

# ID Refresh — Targeted Outlet Re-crawl (Local & Fargate dual-mode)

Use this skill when the user wants to re-crawl a specific set of outlet IDs by
pushing them to Redis and running the detail spider.

This is the "targeted" counterpart to `/run-detail`:
- `/run-detail` crawls the whole queue (pulls latest IDs from MySQL).
- `/id-refresh` crawls a **specific CSV list** you supply.

Two execution modes:

| Mode | Redis | S3 bucket | Runs spider | Best for |
|---|---|---|---|---|
| **Local** | test (`db/redis/test`) | `dash-alpha-dev` | your machine (`scrapy crawl … -a local_test=True`) | small batches (<500 IDs), debugging, dry-run before prod |
| **Fargate** | prod (`db/redis/prod`) | `dash-sourcing` | ECS Fargate (shares image + Phase 3-5 with `/run-detail`) | large batches (≥500 IDs), direct-to-prod data fixes, no local setup |

---

**Narrate every step out loud.** This skill touches MySQL, Redis, and S3 —
silence means a typo could silently corrupt the prod queue or inject bad rows.
Before every step, tell the user:

1. Which step (e.g. "Step 2 — pushing 3,420 IDs to prod Redis key `TKW:NL:202603:outlet_feeds`").
2. What & why (one sentence).
3. The numeric result (IDs pushed, files verified, rows landed).

**Auto-fix confirmation rule.** If an error forces a deviation — modifying the
spider code, editing the CSV on the fly, touching Redis keys other than the one
we're working on, bypassing the metadata-completeness warning — **stop and
explain before proceeding**. Ask for explicit confirmation.

**Mode switch rule.** Once a mode is chosen in Startup, it is fixed for the
entire session. **Never half-and-half** (push to prod Redis then run spider
locally; or verify in test bucket after a Fargate run). If user wants to switch
mode mid-workflow, restart from Startup.

**Leave no trace.** Temp files created by this skill (`/tmp/id-refresh-*.json`,
local CSVs copied from the user's path) are cleaned up at the end of each phase.

---

## Proactive Preflight (run silently BEFORE the Startup prompt)

Before asking the user for inputs, **detect what can be detected**. Fewer
questions = smarter interaction. Each detection is narrated as "I found X;
confirm or override?" rather than "please provide X".

```bash
# ---- P1. Platform from git remote ----
DETECTED_PLATFORM=""
if git remote get-url origin >/dev/null 2>&1; then
    DETECTED_PLATFORM=$(basename "$(git remote get-url origin)" .git)
    # Sanity: expect 3-4 uppercase letters
    [[ "$DETECTED_PLATFORM" =~ ^[A-Z]{2,5}$ ]] || DETECTED_PLATFORM=""
fi

# ---- P2. CSV in current working directory ----
DETECTED_CSV=""
CSV_CANDIDATES=$(ls -1 *.csv 2>/dev/null)
CSV_COUNT=$(echo "$CSV_CANDIDATES" | grep -cv '^$')
if (( CSV_COUNT == 1 )); then
    DETECTED_CSV="$(pwd)/$CSV_CANDIDATES"
elif (( CSV_COUNT > 1 )); then
    DETECTED_CSV="(ambiguous — $CSV_COUNT CSVs in cwd, ask user to pick)"
fi

# ---- P3. Spider project directory ----
DETECTED_SPIDER_DIR=""
if [[ -f "./scrapy.cfg" ]]; then
    DETECTED_SPIDER_DIR="$(pwd)"
fi

# ---- P4. State directory (for Smart entry in Phase 0.6) ----
export STATE_DIR="$HOME/.claude/state/id-refresh"
mkdir -p "$STATE_DIR"

# ---- P5. Narrate findings ----
echo "🔍 Preflight detection:"
[[ -n "$DETECTED_PLATFORM"   ]] && echo "  platform:     $DETECTED_PLATFORM (from git remote)"
[[ -n "$DETECTED_CSV"        ]] && echo "  csv_path:     $DETECTED_CSV"
[[ -n "$DETECTED_SPIDER_DIR" ]] && echo "  spider_dir:   $DETECTED_SPIDER_DIR (scrapy.cfg found)"
echo "  state_dir:    $STATE_DIR"
```

Use these in Startup's prompt — show detected values as defaults instead of
asking blind.

### State file helpers (used by Smart entry + every checkpoint)

Define once; call at checkpoints (after push / verify-ids / QA trigger).
State file is `$STATE_DIR/{platform}-{country}-{month}.json`.

```bash
state_file() {
    echo "$STATE_DIR/${1}-${2}-${3}.json"
}

state_write() {
    # Usage: state_write <platform> <country> <month> <step> <extra_json>
    local f=$(state_file "$1" "$2" "$3")
    local base='{}'
    [[ -f "$f" ]] && base=$(cat "$f")
    echo "$base" | jq \
        --arg platform "$1" --arg country "$2" --arg month "$3" \
        --arg step     "$4" --arg ts      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        --argjson extra "${5:-{\}}" \
        '. + {platform: $platform, country: $country, month: $month,
              last_step: $step, last_ts: $ts} + $extra' > "$f"
}

state_read() {
    local f=$(state_file "$1" "$2" "$3")
    [[ -f "$f" ]] && cat "$f" || echo "{}"
}
```

---

## Startup — Self-Introduction & Mode Selection

On invocation, announce the flow AND collect required inputs + auto-recommend
mode. **Pre-fill defaults from Proactive Preflight above** — don't ask blind.

```
👋 id-refresh. Workflow:

  Phase 0     — Resolve variables (mode-aware: local vs fargate)
  Step 1      — Ensure IDs exist in MySQL (+ metadata completeness check)
  Step 2      — Push IDs to Redis (race-checked for Fargate mode)
  Pre-launch  — 6 STOP-style gates
  Step 3      — Run detail spider
                • Mode Local   → scrapy crawl locally
                • Mode Fargate → reuse /run-detail Phase 3-5
  Step 4      — Verify S3 output (bucket per mode)
  Phase 7     — Post-run data verification (ID-level MySQL cross-check)
  Step 5      — Trigger QA (required)
  Phase 8     — Next steps

Required inputs:

  id_platform        e.g. TKW, EPL, LMN (3-4 uppercase letters)
  country            e.g. NL, DE, US    (2-3 uppercase letters)
  output_month       YYYYMM,            NOT future
  csv_path           path to CSV with 'id_outlet' column
  engineer_name      for QA trigger; ask later if not known

After receiving csv_path I will count rows and auto-recommend:
  <500 IDs  → Local mode    (faster, no prod risk, test bucket output)
  ≥500 IDs  → Fargate mode  (parallel to prod cron, writes prod MySQL+S3)

The user can override the auto-recommendation.
```

Wait for all required inputs. Do not touch AWS before Phase 0 completes.

---

## Phase 0 — Variable Resolution (single source of truth)

Resolve every variable once here. All subsequent steps MUST reference this
table — never re-derive.

### 0.1 — Portable paths (no hardcoded `C:/Users/...`)

```bash
# Where the skill itself lives (portable — works in repo, in ~/.claude/skills,
# or wherever the user has it). Claude Code sets CLAUDE_SKILL_DIR; if not, we
# locate the script relative to this SKILL.md's bash invocations.
: "${CLAUDE_SKILL_DIR:=$HOME/.claude/skills/id-refresh}"
export ID_REFRESH_SCRIPT="$CLAUDE_SKILL_DIR/scripts/id_refresh.py"

# Locate the sibling trigger-qa skill — check both `skills` and `commands` layouts
TRIGGER_QA_SCRIPT=""
for base in "$HOME/.claude/skills/trigger-qa" "$HOME/.claude/commands/trigger-qa"; do
    if [[ -f "$base/scripts/trigger_qa_pipeline.py" ]]; then
        TRIGGER_QA_SCRIPT="$base/scripts/trigger_qa_pipeline.py"
        break
    fi
done
export TRIGGER_QA_SCRIPT   # empty if not installed — Step 5 will handle
```

### 0.2 — Count CSV rows → auto-recommend mode

```bash
CSV_ROWS=$(tail -n +2 "$CSV_PATH" | grep -cv '^[[:space:]]*$')
echo "CSV has $CSV_ROWS rows"

if [[ -z "$MODE" ]]; then
    if (( CSV_ROWS < 500 )); then
        DEFAULT_MODE="local"
    else
        DEFAULT_MODE="fargate"
    fi
    echo "Auto-recommended mode: $DEFAULT_MODE"
    echo "Override? (local / fargate / <Enter> to accept):"
    # Wait for user; if empty, MODE=$DEFAULT_MODE
fi
```

### 0.3 — Mode-dependent variable table

After mode is chosen, resolve:

| Variable | Local mode | Fargate mode |
|---|---|---|
| `REDIS_SECRET` | `db/redis/test` | `db/redis/prod` |
| `S3_BUCKET` | `dash-alpha-dev` | `dash-sourcing` |
| `S3_PREFIX_TMPL` | `sourcing/{platform}/{month}/` | `{platform}/{month}/` |
| Spider invocation | `scrapy crawl conso_outlet_detail -a local_test=True …` (on your machine) | `aws ecs run-task …` (via run-detail Phase 3-5) |
| `id_refresh` arg | `False` | `False` |
| `recrawl` arg | `False` | `False` |
| `local_test` arg | `True` | **NOT passed** |

The script (`id_refresh.py`) honors these via `--mode local|fargate`.

### 0.4 — Spider project directory (Local mode only)

```bash
if [[ "$MODE" == "local" ]]; then
    # Priority: user-provided → current dir if it has scrapy.cfg → ask
    if [[ -z "$SPIDER_PROJECT_DIR" ]]; then
        if [[ -f "./scrapy.cfg" ]]; then
            SPIDER_PROJECT_DIR="$(pwd)"
        else
            echo "Spider project dir not found in current directory."
            echo "Please provide the path to the repo (where scrapy.cfg lives)."
            exit 1
        fi
    fi
    [[ -f "$SPIDER_PROJECT_DIR/scrapy.cfg" ]] \
        || { echo "❌ $SPIDER_PROJECT_DIR has no scrapy.cfg"; exit 1; }
    echo "✅ SPIDER_PROJECT_DIR = $SPIDER_PROJECT_DIR"
fi
```

### 0.5 — Resolved table — print back to user before Step 1

```
=== Resolved variables ===
  Mode:              {local | fargate}
  ID_PLATFORM:       TKW
  COUNTRY:           NL               (regex-validated: ^[A-Z]{2,3}$)
  OUTPUT_MONTH:      202603           (future-date guard: passed)
  CSV_PATH:          /path/to/id.csv  (3420 rows)
  MEAL_FIX:          False
  REDIS_SECRET:      db/redis/prod    (← Fargate)
  REDIS_KEY:         TKW:NL:202603:outlet_feeds
  S3_BUCKET:         dash-sourcing    (← Fargate)
  SPIDER_PROJECT_DIR: (N/A — Fargate mode)
  TRIGGER_QA_SCRIPT: /home/user/.claude/skills/trigger-qa/scripts/trigger_qa_pipeline.py
```

STOP here and wait for user confirmation. Any ambiguity = ask, don't guess.

### 0.6 — Smart entry: check recent history for this (platform/country/month)

Before running anything, check if THIS exact refresh was attempted recently.
Avoids blind re-runs; lets us offer resume / retry-unlanded / view-last-result.

```bash
LAST=$(state_read "$ID_PLATFORM" "$COUNTRY" "$OUTPUT_MONTH")
HAS_HISTORY=$(echo "$LAST" | jq -r 'if . == {} then "no" else "yes" end')

if [[ "$HAS_HISTORY" == "yes" ]]; then
    LAST_STEP=$(echo "$LAST" | jq -r '.last_step // "unknown"')
    LAST_TS=$(echo "$LAST"   | jq -r '.last_ts   // "unknown"')
    AGE_HOURS=$(python3 -c "
from datetime import datetime,timezone
t = datetime.fromisoformat('${LAST_TS}'.replace('Z','+00:00'))
print(int((datetime.now(timezone.utc) - t).total_seconds() / 3600))
" 2>/dev/null || echo "??")

    LAST_VERDICT=$(echo "$LAST" | jq -r '.verdict // "none"')
    NOT_LANDED=$(echo "$LAST"   | jq -r '.not_landed_count // 0')

    echo ""
    echo "⏮️  Found previous attempt ${AGE_HOURS}h ago:"
    echo "    last_step:   $LAST_STEP"
    echo "    verdict:     $LAST_VERDICT"
    [[ "$NOT_LANDED" != "0" ]] && echo "    not landed:  $NOT_LANDED IDs"
    echo ""

    # Narrate options based on last state
    case "$LAST_VERDICT" in
      "✅ data landed")
        echo "This refresh already succeeded. Options:"
        echo "  1. View last results (skip straight to Phase 7 / Phase 8)"
        echo "  2. Re-run from scratch (only if data is re-suspect)"
        ;;
      "⚠️  partial"*)
        echo "Last run was PARTIAL. Options:"
        echo "  1. Retry just the ${NOT_LANDED} not-landed IDs (I'll extract them to a new CSV)"
        echo "  2. Re-run everything"
        echo "  3. View diagnostic (why they didn't land?)"
        ;;
      "❌ LOW LANDING RATE"*)
        echo "Last run FAILED verification. Options:"
        echo "  1. Investigate the failure (view spider logs)"
        echo "  2. Re-run from scratch (after you've fixed the issue)"
        ;;
      *)
        # Mid-workflow (pushed but not verified, etc.)
        echo "Last run stopped at: $LAST_STEP"
        echo "Options:"
        echo "  1. Resume from that step"
        echo "  2. Restart from Step 1"
        ;;
    esac
fi
```

Only after Smart entry answer → proceed to the actual Step 1. If user picks
"retry not-landed", extract sample + full list via a small helper:

```bash
# Create a retry CSV from the stored not_landed list (if sample > 20, query MySQL
# for the full list using last CSV as reference)
echo "id_outlet" > /tmp/id-refresh-retry.csv
echo "$LAST" | jq -r '.not_landed_sample[]' >> /tmp/id-refresh-retry.csv
echo "Retry CSV written to /tmp/id-refresh-retry.csv (N=$(wc -l < /tmp/id-refresh-retry.csv))"
# Narrate to user; then switch $CSV_PATH to this file and re-enter Step 1.
```

---

## Cross-phase Invariants — things that MUST stay coupled

| # | Invariant | Fields it couples | Break = |
|:-:|---|---|---|
| **I1** | `COUNTRY` ≡ MySQL table name ≡ Redis key country segment ≡ S3 partition prefix | Steps 1, 2, 4, 7 | Queries hit wrong table / wrong partition / empty results |
| **I2** | `id_outlet` in CSV ≡ `id_outlet` PK in MySQL ≡ Redis SADD value ≡ row key in S3 parquet | Steps 1-4, 7 | Orphan IDs: in Redis but not MySQL → silently skipped by spider |
| **I3** | `MODE=local` ⇔ `REDIS_SECRET=db/redis/test` ⇔ `S3_BUCKET=dash-alpha-dev` ⇔ spider has `-a local_test=True` | Phase 0, Steps 2/3/4 | Cross-contamination: push to test Redis, spider reads prod → empty run |
| **I4** | `MODE=fargate` ⇔ `REDIS_SECRET=db/redis/prod` ⇔ `S3_BUCKET=dash-sourcing` ⇔ spider runs WITHOUT `local_test` | Phase 0, Steps 2/3/4 | Writes appear in wrong bucket; QA finds nothing |
| **I5** | Spider always runs with `id_refresh=False` AND `recrawl=False` (in BOTH modes) | Step 3 | `id_refresh=True` would overwrite our manually pushed Redis queue; `recrawl=True` would filter out "already crawled" IDs (defeats the purpose) |
| **I6** | `REDIS_KEY` pattern = `{platform}:{country}:{output_month}:outlet_feeds` — same as monthly cron (Fargate mode) | Step 2 | Race with prod cron → partial pushes / duplicates |
| **I7** | Metadata completeness: ≥95% of CSV IDs have non-NULL `unique_name` in MySQL | Step 1, Pre-launch Gate | Spider silently skips every ID missing metadata → S3 output dramatically smaller than expected |

**Rule of thumb:** after every step, grep output for the invariant's fields
and assert they match Phase 0. If ANY mismatch, STOP and narrate.

---

## Step 1 — Ensure IDs Exist in MySQL

### 1a. Check for missing IDs + metadata completeness

```bash
# Capture stdout (JSON) — narration went to stderr so $CHECK_MYSQL_OUT is pure JSON
CHECK_MYSQL_OUT=$(python "$ID_REFRESH_SCRIPT" check-mysql \
    --platform "$ID_PLATFORM" \
    --country  "$COUNTRY" \
    --csv-path "$CSV_PATH")
echo "$CHECK_MYSQL_OUT" | jq '.'
```

Output JSON includes:
- `ids_in_csv`
- `ids_missing_from_mysql` + `missing_ids[]`
- `metadata_null_count` — IDs in MySQL but with NULL `unique_name`
- `metadata_completeness_pct`

Save key fields for downstream sanity checks:

```bash
MISSING_COUNT=$(echo    "$CHECK_MYSQL_OUT" | jq -r '.ids_missing_from_mysql')
METADATA_NULL=$(echo    "$CHECK_MYSQL_OUT" | jq -r '.metadata_null_count')
COMPLETENESS=$(echo     "$CHECK_MYSQL_OUT" | jq -r '.metadata_completeness_pct')
```

**Decision tree:**
- `MISSING_COUNT == 0` AND `COMPLETENESS >= 95` → proceed to Step 2
- `MISSING_COUNT > 0` → **1b (ask user whether to insert)**
- `COMPLETENESS < 95` → **STOP, narrate**: these IDs will be silently
  skipped by the spider. Options:
  1. Run finder spider first to populate metadata
  2. Accept the known gap and proceed anyway
  3. Abort this refresh

**Inline error decisions:**

| If you see… | Likely cause | Do |
|---|---|---|
| `pymysql.err.OperationalError: (2003)` | VPN down / MySQL host unreachable | Reconnect VPN, retry |
| `Access denied for user` | Wrong secret / rotated creds | Check `db/mysql/general` in Secrets Manager |
| `Table 'XXX' doesn't exist` | `$COUNTRY` doesn't match a real table | Double-check spelling; some platforms use `{country}_outlets` |
| Large `missing_ids` list | CSV contains IDs from wrong platform / country | Confirm CSV source; don't blindly `insert-mysql` |

### 1b. Insert missing IDs (only if user confirms)

```bash
python "$ID_REFRESH_SCRIPT" insert-mysql \
    --platform "$ID_PLATFORM" \
    --country  "$COUNTRY" \
    --csv-path "$CSV_PATH"
```

This inserts only `id_outlet`; other metadata columns are NULL. `created_at`
and `last_refresh` are auto-filled by MySQL.

**After insert:** the newly inserted IDs have NULL `unique_name`. If the spider
cannot derive request URLs without `unique_name` (most platforms can't), it
will silently skip them. Warn the user and either:
- Run the finder spider to populate metadata first, or
- Confirm the user accepts the known skip.

---

## Step 2 — Push IDs to Redis (mode-aware, race-checked)

```bash
# Script stdout = JSON (parseable), stderr = narration.
# Capture stdout so we can assert on it immediately after.
PUSH_OUT=$(python "$ID_REFRESH_SCRIPT" push \
    --mode         "$MODE" \
    --platform     "$ID_PLATFORM" \
    --country      "$COUNTRY" \
    --output-month "$OUTPUT_MONTH" \
    --csv-path     "$CSV_PATH")
PUSHED=$(echo "$PUSH_OUT" | jq -r '.ids_pushed')
```

**Inline error decisions:**

| If script exits with… | Meaning | Do |
|---|---|---|
| `REFUSING to overwrite` | Redis key has N existing IDs (maybe cron running) | Confirm with user no conflict, re-run with `--force` appended |
| boto3 `ExpiredToken` | MFA expired mid-workflow | Re-MFA, redo from Phase 0 (variables stay valid) |
| `Invalid country code` / `is in the future` | Upstream validation caught a typo | Fix the input, restart from Startup |
| Connection timeout | VPN down (Local) or SG wrong (Fargate) | Reconnect VPN (Local) or check Fargate SG egress to Redis |

### Sanity check: SCARD must equal CSV rows (Invariant I2 re-assertion)

```bash
if [[ "$PUSHED" != "$CSV_ROWS" ]]; then
    echo "❌ SANITY FAILED: pushed $PUSHED but CSV has $CSV_ROWS rows."
    echo "   Possible causes:"
    echo "     - CSV has duplicate IDs (Redis SADD deduped them)"
    echo "     - CSV rows above first blank line were not counted"
    echo "     - Someone else is writing to this key concurrently"
    echo "   STOP — investigate before Step 3."
    exit 1
fi

# Persist checkpoint
state_write "$ID_PLATFORM" "$COUNTRY" "$OUTPUT_MONTH" "pushed" \
    "$(jq -n --argjson n "$PUSHED" --arg csv "$CSV_PATH" --arg mode "$MODE" \
           '{ids_pushed:$n, csv_path:$csv, mode:$mode}')"

echo "✅ Step 2 done: $PUSHED IDs in Redis key $REDIS_KEY"
```

**⚠️ Fargate mode special warning (Invariant I6):** the Redis key is the SAME
one the monthly cron uses. Before pushing:

```bash
# Check cron schedule — is a scheduled run imminent or in progress?
aws events list-rules --name-prefix "conso-${ID_PLATFORM}-${COUNTRY}" \
    --region eu-central-1 --query 'Rules[].{Name:Name, Sched:ScheduleExpression, State:State}'
```

If a rule's next fire-time is within the next few hours, narrate loudly:
"Monthly cron fires at {time}. Your id-refresh pushed IDs now will be mixed
with the cron's pull. Proceed only if you are the on-call / owner."

---

## Pre-launch Gate (STOP-style, all 6 must pass)

Before entering Step 3 (which consumes real resources), verify every
assumption. Any FAIL = STOP.

```
G1 Redis SCARD after push == CSV row count         [✅ / ❌]
G2 Metadata completeness ≥ 95%                     [✅ / ❌]
G3 output_month passes future-date guard            [✅ / ❌]
G4 COUNTRY matches ^[A-Z]{2,3}$                    [✅ / ❌]
G5 Mode ↔ endpoints consistent (I3 or I4)          [✅ / ❌]
G6 AWS MFA valid (sts get-caller-identity OK)      [✅ / ❌]
```

### G1 — Redis push matches CSV

```bash
REDIS_KEY="${ID_PLATFORM}:${COUNTRY}:${OUTPUT_MONTH}:outlet_feeds"
# Redis SCARD should equal CSV_ROWS (from Phase 0.2)
# The push subcommand prints `ids_pushed` — compare those two numbers.
```

### G2 — Metadata completeness (already computed in Step 1a)

```bash
# Parse check-mysql's output JSON, require metadata_completeness_pct >= 95
```

### G3 — Future-date (script validates; re-assert here)
### G4 — Country regex (script validates; re-assert here)
### G5 — Mode consistency

```bash
if [[ "$MODE" == "local" ]]; then
    [[ "$REDIS_SECRET" == "db/redis/test" && "$S3_BUCKET" == "dash-alpha-dev" ]] \
        || { echo "❌ G5 FAIL: local mode but wrong endpoints"; exit 1; }
elif [[ "$MODE" == "fargate" ]]; then
    [[ "$REDIS_SECRET" == "db/redis/prod" && "$S3_BUCKET" == "dash-sourcing" ]] \
        || { echo "❌ G5 FAIL: fargate mode but wrong endpoints"; exit 1; }
fi
```

### G6 — MFA

```bash
aws sts get-caller-identity --region eu-central-1 >/dev/null \
    && echo "✅ G6 MFA valid" \
    || { echo "❌ G6 MFA expired — run dash-mfa"; exit 1; }
```

All six must pass. On ANY failure — STOP, narrate which, ask user.

---

## Step 3 — Run the Detail Spider (mode branches here)

### Mode A — Local

Run the spider from the spider project directory. **`local_test=True` is
critical** — without it the spider connects to prod Redis (not where we pushed)
and will find an empty queue.

```bash
cd "$SPIDER_PROJECT_DIR"
scrapy crawl conso_outlet_detail \
    -a prefix="$COUNTRY" \
    -a output_month="$OUTPUT_MONTH" \
    -a id_refresh=False \
    -a recrawl=False \
    -a local_test=True \
    $([ "$MEAL_FIX" = "True" ] && echo "-a meal_fix=True")
```

- `id_refresh=False` → spider must NOT overwrite the Redis queue we just pushed (Invariant I5)
- `recrawl=False`    → spider must NOT filter out "already crawled" (defeats the purpose)
- `local_test=True`  → connect to test Redis (Invariant I3)

Run in the **foreground** so Claude can monitor progress and final stats (`item_scraped_count`).

### Mode B — Fargate (chain into run-detail's Phase 3-5)

Instead of re-implementing Fargate launch, hand off to the sibling skill:

```
Invoking run-detail's Fargate launch with id-refresh-specific overrides:

  - Mode:          id-refresh-fargate  (single container, not multi-instance)
  - id_refresh:    False              (do NOT overwrite our Redis push)
  - recrawl:       False              (do NOT filter; crawl every pushed ID)
  - local_test:    (not passed — use prod Redis)
  - sample:        0                  (production)
  - INSTANCE_COUNT: 1                  (no workers needed — our ID list is fixed)
  - meal_fix:      $MEAL_FIX
```

Execute run-detail's:
- **Phase 3** — image freshness + IMAGE_DIGEST capture
- **Phase 4** — task-def registration (digest-pinned)
- **Phase 4.5** — Pre-launch Gate (G1–G6 there, different gates than our Step 3 pre-gate)
- **Phase 5.1** — run-task with our overrides
- **Phase 5.2** — poll until STOPPED
- **Phase 5.3** — collect exit code; on 137 → run-detail's OOM auto-recovery kicks in

After Fargate run completes, return here for Step 4 + Phase 7.

**Why not multi-instance?** id-refresh operates on a fixed CSV list that fits
in one container's runtime. Multi-instance would require splitting the CSV
(unnecessary complexity); single container finishes in minutes-to-an-hour
regardless of batch size.

---

## Step 4 — Verify S3 Output (bucket per mode)

```bash
VERIFY_OUT=$(python "$ID_REFRESH_SCRIPT" verify \
    --mode         "$MODE" \
    --platform     "$ID_PLATFORM" \
    --country      "$COUNTRY" \
    --output-month "$OUTPUT_MONTH")
# Optional: --id-job YYYYMMDD to check a specific date (defaults to today)

# Total files across all 4 tables
S3_TOTAL_FILES=$(echo "$VERIFY_OUT" \
    | jq '[.tables | to_entries[] | .value.file_count // 0] | add')
echo "S3 total files (today's id_job): $S3_TOTAL_FILES"
```

### Sanity: S3 == 0 but spider "succeeded" → inconsistent

If Step 3 claimed the spider exited 0 (or Fargate task returned exit 0) but
S3_TOTAL_FILES is 0, something is wrong:

```bash
if (( S3_TOTAL_FILES == 0 )); then
    echo "❌ INCONSISTENCY: spider appeared to finish but 0 files landed in S3."
    echo "   Possible causes:"
    echo "     - Wrong mode endpoint (Fargate spider writing to prod but we're checking test bucket)"
    echo "     - S3Pipeline commented out in spider's custom_settings"
    echo "     - id_job partition mismatch (spider used yesterday's date)"
    echo "     - Task role missing s3:PutObject permission"
    echo "   STOP before Phase 7 — investigating a 100% failure saves wasted MySQL queries."
    exit 1
fi
```

**Inline error decisions:**

| If you see… | Likely cause | Do |
|---|---|---|
| `file_count: 0` on some tables but not others | Pipeline partially broken (e.g. MealPipeline error) | Inspect spider logs; may still be OK if only `outlet_information` matters for QA |
| All `file_count: 0` | Wrong bucket (mode mismatch I3/I4) or spider never wrote | See sanity check above — STOP |
| Error in JSON: `AccessDenied` | S3 bucket policy or creds missing | Check IAM role, verify mode (`aws s3 ls` manually) |

> **Caveat**: this counts FILES, not ROWS. An empty file still = 1. Phase 7
> does the row-level check.

---

## Phase 7 — Post-run Data Verification (ID-level cross-check)

**Why this phase exists — the YDE/LMN pattern.** Step 4 counts S3 files but
doesn't confirm the specific IDs from your CSV actually landed. Phase 7 checks
MySQL row-by-row.

```bash
VIDS_OUT=$(python "$ID_REFRESH_SCRIPT" verify-ids \
    --platform "$ID_PLATFORM" \
    --country  "$COUNTRY" \
    --csv-path "$CSV_PATH")
echo "$VIDS_OUT"

LANDING_RATE=$(echo "$VIDS_OUT" | jq -r '.landing_rate_pct')
VERDICT=$(echo "$VIDS_OUT"      | jq -r '.verdict')
NOT_LANDED=$(echo "$VIDS_OUT"   | jq -r '.ids_not_landed_today')
```

Output JSON:
```
{
  "ids_in_csv":           3420,
  "ids_landed_today":     3387,
  "ids_not_landed_today": 33,
  "not_landed_sample":    ["id-abc", "id-xyz", ...],   # first 20
  "landing_rate_pct":     99.0,
  "verdict":              "✅ data landed",
  "hint":                 "If these look fine in MySQL, try --lenient-match..."
}
```

### Sanity: cross-check vs Step 1 metadata_null_count

If Step 1a reported N IDs with NULL `unique_name` (they should be silently
skipped by the spider), but Phase 7 reports 100% landing, something doesn't
add up.

```bash
METADATA_NULL=$(echo "$CHECK_MYSQL_OUT" | jq -r '.metadata_null_count // 0')
IDS_IN_CSV=$(echo "$VIDS_OUT" | jq -r '.ids_in_csv')

if (( METADATA_NULL > 0 )) && (( $(echo "$LANDING_RATE == 100.0" | bc -l) )); then
    echo "🤔 SUSPICIOUS: Step 1 said $METADATA_NULL IDs had NULL metadata"
    echo "   (spider should skip those), but Phase 7 shows 100% landing."
    echo "   Either:"
    echo "     (a) metadata was back-filled between Step 1 and Step 3 (plausible if someone ran finder)"
    echo "     (b) spider has new logic that doesn't need unique_name"
    echo "     (c) the CURDATE() filter is catching yesterday's data (timezone edge case)"
    echo "   Narrate to user — do NOT claim success without explanation."
fi
```

If `NOT_LANDED > 0` and user didn't pass `--lenient-match`, narrate the hint:
"If these IDs look fine in MySQL manually, re-run verify-ids with
`--lenient-match` to rule out Excel zero-padding / case drift."

### Write checkpoint to state file

```bash
state_write "$ID_PLATFORM" "$COUNTRY" "$OUTPUT_MONTH" "verified" \
    "$(jq -n \
        --argjson rate  "$LANDING_RATE" \
        --arg     v     "$VERDICT" \
        --argjson nland "$NOT_LANDED" \
        --argjson sample "$(echo "$VIDS_OUT" | jq '.not_landed_sample')" \
        '{landing_rate_pct:$rate, verdict:$v,
          not_landed_count:$nland, not_landed_sample:$sample}')"
```

**Verdict rules (script-enforced):**
- `≥95%` → `✅ data landed` — proceed to Step 5
- `80–94%` → `⚠️ partial` — narrate which IDs didn't land, ask user (metadata
  gaps in MySQL? spider error mid-run? platform blocked?)
- `<80%` → `❌ LOW LANDING RATE` — STOP, investigate spider/pipeline BEFORE
  triggering QA. Possible causes: wrong mode endpoint, `local_test` mismatch,
  spider code bug, upstream API block.

### Cross-check table to show user

```
=== Data Verification ===
  Mode:              {MODE}
  CSV IDs pushed:    {CSV_ROWS}
  Redis SCARD:       {after push}
  S3 files today:    {sum of Step 4 file_counts}
  MySQL rows today:  {ids_landed_today}
  Landing rate:      {landing_rate_pct}%
  Verdict:           {✅ / ⚠️ / ❌}
```

---

## Step 5 — Trigger QA + Follow-up poll (required)

After Phase 7 verdict = ✅ (or user explicitly accepts ⚠️), trigger QA AND
**verify the QA actually started** — don't stop at "trigger call returned".

### 5.1 — Invoke trigger-qa

```bash
if [[ -z "$TRIGGER_QA_SCRIPT" ]]; then
    echo "⚠️  trigger-qa skill not found. Ask user to install it, or run manually:"
    echo "   /trigger-qa $ID_PLATFORM $COUNTRY $OUTPUT_MONTH"
    QA_STATUS="skipped"
else
    QA_OUTPUT=$(python "$TRIGGER_QA_SCRIPT" \
        --platform       "$ID_PLATFORM" \
        --country        "$COUNTRY" \
        --refresh        "$OUTPUT_MONTH" \
        --engineer-name  "$ENGINEER_NAME" 2>&1)
    echo "$QA_OUTPUT"

    # Extract EMR cluster ID if the script printed one
    EMR_CLUSTER=$(echo "$QA_OUTPUT" | grep -oE 'j-[A-Z0-9]{10,}' | head -1)
    LAMBDA_STATUS=$(echo "$QA_OUTPUT" | grep -oiE 'lambda.*(success|invoked|failed)' | head -1)
fi
```

Ask for `engineer_name` now if not already collected. No hardcoded enum — any
string the QA pipeline accepts is fine.

### 5.2 — Poll EMR cluster status (verify QA actually started)

If we got a cluster ID, poll until cluster reaches a stable state (or timeout).
Three exit verdicts: `triggered_running` / `triggered_stuck` / `triggered_failed`.

```bash
if [[ -n "$EMR_CLUSTER" ]]; then
    echo ""
    echo "🔬 Polling EMR cluster $EMR_CLUSTER (max 10 min)..."
    START=$(date +%s)
    TIMEOUT=600
    QA_VERDICT="triggered_stuck"

    while true; do
        STATE=$(aws emr describe-cluster --cluster-id "$EMR_CLUSTER" \
            --region eu-central-1 \
            --query 'Cluster.Status.State' --output text 2>/dev/null)
        REASON=$(aws emr describe-cluster --cluster-id "$EMR_CLUSTER" \
            --region eu-central-1 \
            --query 'Cluster.Status.StateChangeReason.Message' --output text 2>/dev/null)
        ELAPSED=$(( $(date +%s) - START ))
        echo "  [${ELAPSED}s] state=$STATE"

        case "$STATE" in
            RUNNING|WAITING)
                echo "✅ EMR cluster $STATE — QA is progressing"
                QA_VERDICT="triggered_running"
                break
                ;;
            TERMINATED)
                echo "ℹ️  EMR cluster TERMINATED (completed): $REASON"
                QA_VERDICT="triggered_running"   # completed normally
                break
                ;;
            TERMINATED_WITH_ERRORS)
                echo "❌ EMR cluster TERMINATED_WITH_ERRORS: $REASON"
                QA_VERDICT="triggered_failed"
                break
                ;;
            STARTING|BOOTSTRAPPING)
                :   # still coming up, keep polling
                ;;
            *)
                echo "?  unknown state: $STATE"
                ;;
        esac

        if (( ELAPSED > TIMEOUT )); then
            echo "⏰ still $STATE after ${TIMEOUT}s — QA likely stuck (or just slow)"
            QA_VERDICT="triggered_stuck"
            break
        fi
        sleep 20
    done

    # EMR console link for user follow-up
    echo ""
    echo "EMR console: https://console.aws.amazon.com/elasticmapreduce/home?region=eu-central-1#cluster-details:$EMR_CLUSTER"
else
    echo "⚠️  No EMR cluster ID found in trigger-qa output — cannot poll. Manual check needed."
    QA_VERDICT="triggered_unknown"
fi

# Persist QA verdict to state file
state_write "$ID_PLATFORM" "$COUNTRY" "$OUTPUT_MONTH" "qa_triggered" \
    "$(jq -n --arg v "$QA_VERDICT" --arg c "${EMR_CLUSTER:-}" \
           '{qa_verdict:$v, qa_emr_cluster:$c}')"
```

### 5.3 — Narrate final QA state back to user

Report based on `QA_VERDICT`:
- `triggered_running` → "QA is running (cluster healthy, expect ~15-30 min)"
- `triggered_stuck` → "QA cluster didn't leave STARTING after 10 min — investigate"
- `triggered_failed` → "QA cluster failed — inspect EMR console (link above)"
- `triggered_unknown` → "QA was invoked but we couldn't find a cluster ID; check manually"
- `skipped` → "trigger-qa skill not installed — run manually later"

**Inline error decisions:**

| If you see… | Likely cause | Do |
|---|---|---|
| Lambda invoke error `AccessDenied` | MFA session expired / role missing | Re-MFA, retry Step 5 |
| Cluster `stuck` state for 10+ min | EMR subnet exhaustion / bootstrap failure | Check EMR logs (console link), escalate to infra |
| `TERMINATED_WITH_ERRORS` with `Bootstrap failure` | Runtime dep missing in EMR image | Infra team — not fixable here |
| No `j-*` pattern in output | trigger-qa script may have changed signature | Update trigger-qa or parse output manually |

---

## Phase 8 — Next Steps

Phase 7 verdict dictates options:

**If ✅:**
```
✅ id-refresh complete. QA triggered.

Optional next actions:
  1. Watch QA progress:
     → (check EMR console link from Step 5)
  2. Full detail refresh for the same platform:
     → /run-detail
  3. Refresh different IDs / month / country:
     → /id-refresh again
  4. Done
```

**If ⚠️ partial:**
```
⚠️  Partial landing ({landing_rate_pct}%). Options:
  1. Retry just the missing IDs:
     → Export `not_landed_sample` + full list to new CSV, re-run
  2. Accept gap and trigger QA anyway (manual override — not recommended)
  3. Investigate: inspect spider log for "no metadata" / "IgnoreRequest"
```

**If ❌:**
```
❌ Landing rate <80%. Do NOT trigger QA yet.
Investigate:
  - Mode endpoint mismatch (I3/I4)? Re-check Phase 0.
  - Metadata gap growing? Re-run Step 1a check-mysql.
  - Spider error? Inspect logs (local terminal, or run-detail Phase 6 for Fargate).
```

---

## Script reference (id_refresh.py subcommands)

All subcommands validate country regex (`^[A-Z]{2,3}$`) and parameterize SQL —
safe against injection via CSV content or country arg.

| Subcommand | Arguments | What it does |
|---|---|---|
| `push` | `--mode --platform --country --output-month --csv-path [--force]` | Push CSV IDs to Redis (mode selects which Redis). `--force` required to overwrite a non-empty key. |
| `verify` | `--mode --platform --country --output-month [--id-job]` | Count S3 files per table under `id_job=YYYYMMDD/` partition. |
| `check-mysql` | `--platform --country --csv-path` | Find missing IDs AND metadata completeness. |
| `insert-mysql` | `--platform --country --csv-path` | Insert missing IDs with NULL metadata. |
| `verify-ids` | `--platform --country --csv-path` | Row-level: for each CSV id, check MySQL `DATE(last_refresh) = CURDATE()`. |

---

## Error Handling (symptom → root cause → fix)

| Symptom | Likely cause | Fix |
|---|---|---|
| `REFUSING to overwrite` in Step 2 | Redis key non-empty (maybe cron running) | Confirm no conflict, re-run with `--force` |
| `Invalid country code` error | Country arg failed regex (e.g. lowercase, 4+ chars) | Use 2-3 uppercase letters: `NL` not `nl` or `NLD` |
| `output_month … is in the future` | Typo (e.g. `202704` when it's 2026) | Use past or current YYYYMM |
| MySQL injection-looking errors | Legacy callers missing `--mode` | Re-run with `--mode local` or `--mode fargate` |
| Spider starts but crawls nothing | Mode/endpoint mismatch (I3/I4) OR `local_test` flag wrong | Check Phase 0 table; re-run Step 3 with correct mode |
| `No metadata found for outlet X, skipping` | ID exists in MySQL but `unique_name` is NULL | Run finder spider first OR manually populate metadata |
| `ConnectionRefusedError` on Redis (Local mode) | VPN off | Reconnect VPN, re-run push |
| `Timeout waiting for Redis` (Fargate mode) | Security group blocks Fargate ↔ Redis | Check cluster SG allows Redis port |
| Phase 7 landing_rate < 80% | Spider skipped many IDs (metadata NULL) OR wrong mode | Check metadata_null_count; re-check mode endpoints |
| `dynamic_menu FAILED: Task got bad yield` | Scrapy 2.14 / `deferToThread` compatibility warning | Harmless — only affects `rating`/`out_of_stock` |
| `pyarrow` / `pandas` version mismatch | Local env mismatch with ECR image | Run in Fargate mode instead, OR sync deps |

---

## Notes

- **Redis key format**: `{platform}:{country}:{output_month}:outlet_feeds`
  — identical to monthly cron (Invariant I6). Fargate mode shares this key.
- **Redis credentials**:
  - Local mode: `db/redis/test` in Secrets Manager (`us-east-1`, non-SSL).
  - Fargate mode: `db/redis/prod` (SSL).
- **S3 paths**:
  - Local: `s3://dash-alpha-dev/sourcing/{platform}/{output_month}/{country}_{table}/id_job={YYYYMMDD}/`
  - Fargate: `s3://dash-sourcing/{platform}/{output_month}/{country}_{table}/id_job={YYYYMMDD}/`
- **Large batches**: Local >3000 IDs can take multiple hours. Consider Fargate
  — container can run headless and you can close your laptop.
- **Race with monthly cron (Fargate only)**: The Redis key is shared. Either:
  (a) schedule id-refresh in the cron's off-window, or (b) coordinate with the
  on-call engineer. The `push` subcommand's `--force` requirement is there to
  make you pause and think.
- **Resume on spider interrupt**: Not currently automated. Remaining IDs stay
  in Redis; just re-run Step 3 (spider will continue consuming the queue).

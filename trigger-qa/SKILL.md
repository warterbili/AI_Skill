---
name: trigger-qa
description: "Trigger the Dash sourcing QA pipeline on AWS. Use when the user says: trigger QA, run QA, start QA pipeline, retrigger QA, QA for <platform> <country> <month>. Example: /trigger-qa LMN TH 202603"
disable-model-invocation: true
allowed-tools: Read, Bash, Glob, Grep
---

# Trigger QA Pipeline AWS

Use this skill when the user wants to start or re-trigger one Dash sourcing QA
run from a machine that already has AWS CLI access.

The flow:
1. Resolve all variables (auto-detect from context where possible)
2. Pre-launch Gate (no duplicate cluster, MFA OK, engineer resolved)
3. Invoke the Lambda (`dash-sourcing-pipeline-spark`)
4. Verify the EMR cluster started
5. (Optional) Wait for cluster completion
6. Persist state + recommend next action

---

**Narrate every step out loud.** This skill costs real money — each EMR cluster
is ~$5-20 depending on size, and an accidentally double-triggered QA wastes
both. Before each step, tell the user:

1. Which step (e.g. "Step 2 — polling EMR cluster `j-XXX` for completion").
2. What & why (one sentence).
3. The result (cluster ID, state, verdict).

**Auto-fix confirmation rule.** If something forces a deviation — invoking a
different Lambda, targeting a different account, overriding `--check-existing`
warning, manually constructing the EMR cluster name — **stop and explain
before proceeding**. Ask for explicit confirmation.

**Leave no trace.** The `aws lambda invoke` writes a temp file containing the
Lambda response payload — the script already cleans this up. State files
(`~/.claude/state/trigger-qa/`) are persistent on purpose; don't clean those.

---

## Proactive Preflight (silent — BEFORE Startup prompts the user)

Detect what can be detected. Fewer questions = smarter interaction.

```bash
# ---- P1. Auto-detect from sibling id-refresh state ----
# If id-refresh just ran for the same combo, its state file has platform/country/month
# in /home/.../id-refresh/ — preferred over asking the user blindly.
DETECTED_PLATFORM=""
DETECTED_COUNTRY=""
DETECTED_REFRESH=""
ID_REFRESH_STATE_DIR="$HOME/.claude/state/id-refresh"
if [[ -d "$ID_REFRESH_STATE_DIR" ]]; then
    LATEST_ID_REFRESH=$(ls -t "$ID_REFRESH_STATE_DIR"/*.json 2>/dev/null | head -1)
    if [[ -n "$LATEST_ID_REFRESH" ]]; then
        DETECTED_PLATFORM=$(jq -r '.platform // empty' "$LATEST_ID_REFRESH")
        DETECTED_COUNTRY=$(jq  -r '.country  // empty' "$LATEST_ID_REFRESH")
        DETECTED_REFRESH=$(jq  -r '.month    // empty' "$LATEST_ID_REFRESH")
        # Only suggest if recent (<2h) — older state is stale signal
        AGE_HOURS=$(python3 -c "
from datetime import datetime,timezone
import json
with open('$LATEST_ID_REFRESH') as f: t = json.load(f).get('last_ts')
if not t: print(99); exit()
dt = datetime.fromisoformat(t.replace('Z','+00:00'))
print(int((datetime.now(timezone.utc) - dt).total_seconds() / 3600))
" 2>/dev/null || echo 99)
        if (( AGE_HOURS > 2 )); then
            DETECTED_PLATFORM=""; DETECTED_COUNTRY=""; DETECTED_REFRESH=""
        fi
    fi
fi

# ---- P2. Engineer name from git config (fallback for user input) ----
DETECTED_ENGINEER=$(git config user.name 2>/dev/null | awk '{print $1}')

# ---- P3. AWS account from STS (replaces hardcoded 593453040104) ----
export AWS_REGION="${AWS_REGION:-eu-central-1}"
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity \
    --query Account --output text --region "$AWS_REGION" 2>/dev/null)

# ---- P4. State directory (Smart entry uses this in Phase 0.6) ----
export STATE_DIR="$HOME/.claude/state/trigger-qa"
mkdir -p "$STATE_DIR"

# ---- P5. Narrate findings ----
echo "🔍 Preflight detection:"
[[ -n "$DETECTED_PLATFORM" ]] && echo "  platform:  $DETECTED_PLATFORM (from recent id-refresh)"
[[ -n "$DETECTED_COUNTRY"  ]] && echo "  country:   $DETECTED_COUNTRY"
[[ -n "$DETECTED_REFRESH"  ]] && echo "  refresh:   $DETECTED_REFRESH"
[[ -n "$DETECTED_ENGINEER" ]] && echo "  engineer:  $DETECTED_ENGINEER (from git config)"
[[ -n "$AWS_ACCOUNT_ID"    ]] && echo "  aws acct:  $AWS_ACCOUNT_ID"
echo "  state dir: $STATE_DIR"
```

### State file helpers (used by Smart entry + post-trigger checkpoint)

```bash
state_file() { echo "$STATE_DIR/${1}-${2}-${3}.json"; }

state_write() {
    # Args: platform, country, refresh, step, extra_json
    local f=$(state_file "$1" "$2" "$3")
    local base='{}'
    [[ -f "$f" ]] && base=$(cat "$f")
    echo "$base" | jq \
        --arg platform "$1" --arg country "$2" --arg refresh "$3" \
        --arg step     "$4" --arg ts      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        --argjson extra "${5:-{\}}" \
        '. + {platform: $platform, country: $country, refresh: $refresh,
              last_step: $step, last_ts: $ts} + $extra' > "$f"
}

state_read() {
    local f=$(state_file "$1" "$2" "$3")
    [[ -f "$f" ]] && cat "$f" || echo "{}"
}
```

---

## Startup — Self-Introduction

```
👋 trigger-qa. I will:

  Phase 0    — Resolve variables (preflight already detected what it could)
  Phase 0.6  — Smart entry (check for recent triggers — avoid duplicates)
  Pre-launch — 5 STOP-style gates
  Step 1     — Invoke Lambda, verify EMR cluster started
  Step 2     — (Optional) Wait for cluster completion
  Step 3     — Persist state file
  Step 4     — Next steps

Required inputs (Preflight may have filled some):

  id_platform   e.g. EPL, LMN, TKW (auto-detected from id-refresh: $DETECTED_PLATFORM)
  country       2-letter code      (auto-detected: $DETECTED_COUNTRY)
  refresh       YYYYMM             (auto-detected: $DETECTED_REFRESH)
  engineer_name e.g. Cam, Morri    (auto-detected: $DETECTED_ENGINEER)

Confirm or override each. If anything is "(empty)" I'll ask.
```

Wait for user confirmation. **Do not invoke Lambda before Phase 0 + Smart
entry complete** — those checks may surface "you already triggered this 5 min
ago" which prevents waste.

---

## Phase 0 — Variable Resolution (single source of truth)

### 0.1 — Portable script path

```bash
# Where this skill lives — works in ~/.claude/skills/, in repo, anywhere
: "${CLAUDE_SKILL_DIR:=$HOME/.claude/skills/trigger-qa}"
export TRIGGER_QA_SCRIPT="$CLAUDE_SKILL_DIR/scripts/trigger_qa_pipeline.py"
```

### 0.2 — Resolved variables

```bash
# From user / preflight
export ID_PLATFORM="$DETECTED_PLATFORM_OR_USER"
export COUNTRY="$DETECTED_COUNTRY_OR_USER"
export REFRESH="$DETECTED_REFRESH_OR_USER"
export ENGINEER_NAME="$DETECTED_ENGINEER_OR_USER"

# Constants (rarely overridden)
export ENV="${ENV:-dev}"
export LAYER="${LAYER:-raw}"
export TABLES="${TABLES:-outlet_information,outlet_meal,meal_option,option_relation}"
export LOAD_RAW_AS_STRINGS="${LOAD_RAW_AS_STRINGS:-0}"

# Derived (Invariant I1 — single place this string is defined)
export CLUSTER_NAME="sourcing-pipeline-${ID_PLATFORM}-${COUNTRY}-${REFRESH}"

# Lambda — function name only; AWS CLI uses caller's account
# To invoke a Lambda in another account, override with full ARN
export FUNCTION_NAME="${FUNCTION_NAME:-dash-sourcing-pipeline-spark}"
```

### 0.3 — Resolved table — print to user

| Variable | Value | Used in |
|---|---|---|
| `ID_PLATFORM`     | `EPL`             | Lambda payload, cluster name |
| `COUNTRY`         | `US`              | Lambda payload, cluster name |
| `REFRESH`         | `202603`          | Lambda payload, cluster name |
| `ENGINEER_NAME`   | `Cam`             | Resolved to Slack ID inside script |
| `ENV` / `LAYER`   | `dev` / `raw`     | Lambda payload (rarely changed) |
| `TABLES`          | `outlet_information,outlet_meal,…` | Lambda payload (Invariant I4) |
| `AWS_ACCOUNT_ID`  | (dynamic)         | Resolved from STS, NOT hardcoded |
| `AWS_REGION`      | `eu-central-1`    | Every aws call |
| `CLUSTER_NAME`    | `sourcing-pipeline-EPL-US-202603` | I1 — must match script's find-cluster |
| `FUNCTION_NAME`   | `dash-sourcing-pipeline-spark` | I2 — caller account scope |

STOP and confirm with user before proceeding.

### 0.6 — Smart entry: recent-trigger guard

```bash
LAST=$(state_read "$ID_PLATFORM" "$COUNTRY" "$REFRESH")
HAS_HIST=$(echo "$LAST" | jq -r 'if . == {} then "no" else "yes" end')

if [[ "$HAS_HIST" == "yes" ]]; then
    LAST_TS=$(echo "$LAST"      | jq -r '.last_ts // ""')
    LAST_STEP=$(echo "$LAST"    | jq -r '.last_step // ""')
    LAST_VERDICT=$(echo "$LAST" | jq -r '.verdict // ""')
    LAST_CLUSTER=$(echo "$LAST" | jq -r '.cluster_id // ""')
    AGE_MIN=$(python3 -c "
from datetime import datetime,timezone
t = datetime.fromisoformat('${LAST_TS}'.replace('Z','+00:00'))
print(int((datetime.now(timezone.utc) - t).total_seconds() / 60))
" 2>/dev/null || echo 999)

    echo ""
    echo "⏮️  Found previous trigger ${AGE_MIN} min ago:"
    echo "    last step:    $LAST_STEP"
    echo "    last verdict: $LAST_VERDICT"
    [[ -n "$LAST_CLUSTER" ]] && echo "    cluster id:   $LAST_CLUSTER"
    echo ""

    if (( AGE_MIN < 60 )); then
        echo "⚠️  This is recent — re-triggering will create a SECOND EMR cluster ($5-20 wasted)."
        echo "    Options:"
        echo "      1. View the existing cluster (skip to Step 2)"
        echo "      2. Force a new trigger (only if the previous one truly failed)"
        echo "      3. Abort"
    else
        echo "    Last trigger is old enough to safely re-run."
    fi
fi
```

If user picks **option 1** → jump straight to Step 2 (cluster polling) using
`$LAST_CLUSTER`. If **option 2** → continue normally. If **option 3** → exit.

---

## Cross-phase Invariants — things that MUST stay coupled

| # | Invariant | Fields | Break = |
|:-:|---|---|---|
| **I1** | `CLUSTER_NAME` = `sourcing-pipeline-{platform}-{country}-{refresh}` — defined ONCE in Phase 0.2; used by Lambda payload, by `find_cluster` search, by Smart entry's existing-cluster check | Phase 0.2, Step 1, Step 2 | Lambda invokes successfully but verification can't find the cluster — looks like silent failure |
| **I2** | `FUNCTION_NAME` resolved against **caller's account** unless full ARN given | Phase 0.2, Step 1 | Cross-account misfire: Lambda doesn't exist in this account, AccessDenied error |
| **I3** | `engineer_id` resolved ONCE in script (via name → mapping → fallback to direct id) — NOT re-resolved downstream | Script `resolve_engineer_id` | Different ID end up in Lambda payload vs Slack notification — confused audit trail |
| **I4** | `TABLES` list (`outlet_information`, `outlet_meal`, `meal_option`, `option_relation`) ≡ S3 paths the QA Spark job reads from | Phase 0.2, Lambda payload | QA reads non-existent S3 paths → reports "0 records" → user thinks crawl failed |

**Rule of thumb:** after Lambda invoke, check the response payload contains
the expected `cluster_name` fragment. If `find_cluster` returns None within
the verification window, **don't assume "still starting" — first re-check that
the name in the payload matches `CLUSTER_NAME`** (I1).

---

## Pre-launch Gate (STOP-style, all 5 must pass)

Before invoking Lambda, verify every assumption.

### G1 — AWS MFA valid in expected account

```bash
ACTUAL_ACCT=$(aws sts get-caller-identity --query Account --output text \
    --region "$AWS_REGION" 2>/dev/null)

[[ "$ACTUAL_ACCT" == "$AWS_ACCOUNT_ID" ]] \
    && echo "✅ G1 MFA valid (account $AWS_ACCOUNT_ID)" \
    || { echo "❌ G1 FAIL: got '$ACTUAL_ACCT', expected '$AWS_ACCOUNT_ID' — re-MFA"; exit 1; }
```

### G2 — Engineer resolves to a Slack ID

```bash
# Trial-resolve via the script's --help-style dry check (Python verifies)
ENGINEER_TEST=$(python "$TRIGGER_QA_SCRIPT" --platform "$ID_PLATFORM" \
    --country "$COUNTRY" --refresh "$REFRESH" \
    --engineer-name "$ENGINEER_NAME" --no-verify-emr 2>&1 1>/dev/null | head -1 || true)
# If "Provide --engineer-id or a known --engineer-name" appears → G2 fail
if echo "$ENGINEER_TEST" | grep -qi "engineer"; then
    echo "❌ G2 FAIL: '$ENGINEER_NAME' not in known engineer map"
    echo "    Either correct the spelling or pass --engineer-id <Uxxx> directly"
    exit 1
fi
echo "✅ G2 engineer resolves OK"
```

### G3 — `CLUSTER_NAME` matches Invariant I1

```bash
EXPECTED="sourcing-pipeline-${ID_PLATFORM}-${COUNTRY}-${REFRESH}"
[[ "$CLUSTER_NAME" == "$EXPECTED" ]] \
    && echo "✅ G3 cluster name matches I1" \
    || { echo "❌ G3 FAIL: $CLUSTER_NAME != $EXPECTED"; exit 1; }
```

### G4 — No active duplicate cluster (uses `--check-existing` flag)

```bash
# This is folded into Step 1 (script does it pre-invoke). G4 here just promises
# that we'll pass --check-existing to the script. Skip the gate as a
# standalone shell check — the script handles it.
echo "✅ G4 will use --check-existing in Step 1"
```

### G5 — `REFRESH` not in the future

```bash
CURRENT=$(date -u +%Y%m)
if (( REFRESH > CURRENT )); then
    echo "❌ G5 FAIL: refresh '$REFRESH' is in the future (current: $CURRENT)"
    exit 1
fi
echo "✅ G5 refresh ≤ current month"
```

### Gate summary

```
G1 MFA valid (correct account)        [✅ / ❌]
G2 engineer resolves to Slack ID      [✅ / ❌]
G3 CLUSTER_NAME matches I1            [✅ / ❌]
G4 (script will check duplicates)     [✅]
G5 refresh not in future              [✅ / ❌]
```

All ✅ → proceed to Step 1. Any ❌ → STOP, narrate, ask user.

---

## Step 1 — Invoke Lambda + verify cluster started

The script does invoke + verify in one call. Always pass `--check-existing`
so duplicates are surfaced (G4).

```bash
TRIGGER_OUT=$(python "$TRIGGER_QA_SCRIPT" \
    --platform      "$ID_PLATFORM" \
    --country       "$COUNTRY" \
    --refresh       "$REFRESH" \
    --engineer-name "$ENGINEER_NAME" \
    --check-existing)

# stdout is pure JSON (script narration went to stderr)
echo "$TRIGGER_OUT" | jq '.'

VERDICT=$(echo "$TRIGGER_OUT"      | jq -r '.verdict')
CLUSTER_ID=$(echo "$TRIGGER_OUT"   | jq -r '.cluster.Id      // empty')
CONSOLE_URL=$(echo "$TRIGGER_OUT"  | jq -r '.cluster_console_url // empty')
EXISTING=$(echo "$TRIGGER_OUT"     | jq -r '.existing_clusters_warning // empty')
```

### Sanity: cluster name in payload matches Phase 0 (Invariant I1)

```bash
EXPECTED_CLUSTER="$CLUSTER_NAME"
ACTUAL_CLUSTER=$(echo "$TRIGGER_OUT" | jq -r '.cluster_name_expected')

[[ "$ACTUAL_CLUSTER" == "$EXPECTED_CLUSTER" ]] \
    || echo "❌ I1 SANITY FAIL: script computed '$ACTUAL_CLUSTER' but Phase 0 said '$EXPECTED_CLUSTER'"
```

### Handling existing-cluster warning

If `$EXISTING` is non-empty, the script found a duplicate cluster running:

```bash
if [[ -n "$EXISTING" && "$EXISTING" != "null" ]]; then
    echo "⚠️  Existing cluster(s) for the same combo:"
    echo "$EXISTING" | jq .
    echo ""
    echo "You ALSO triggered a new one (Lambda was invoked before the check)."
    echo "Now both will run. Decide:"
    echo "  1. Let both finish (waste OK)"
    echo "  2. aws emr terminate-clusters --cluster-ids <one of them>"
    echo "  3. Investigate why the duplicate happened"
fi
```

**Inline error decisions:**

| If you see… | Likely cause | Do |
|---|---|---|
| `verdict: lambda_invoke_failed` | MFA expired / wrong account / Lambda doesn't exist | Re-MFA, verify FUNCTION_NAME points to right account |
| `verdict: triggered_no_cluster_observed` | Lambda OK but cluster didn't appear in 30s | Wait longer (the script's `--verify-attempts` × `--verify-interval`); manual `aws emr list-clusters --active` |
| Lambda payload contains `"errorMessage"` | Lambda function itself raised an error | Read payload — typically a parameter validation failure (refresh format, country, etc) |
| `AccessDenied` invoking Lambda | Caller IAM lacks `lambda:InvokeFunction` | Add policy or assume the right role |
| `existing_clusters_warning` present | Someone (maybe you) already triggered | See "Handling existing-cluster warning" above |

---

## Step 2 — (Optional but recommended) Wait for cluster completion

For QA gates where the next action depends on QA's outcome, pass
`--wait-for-completion` to the script. This polls EMR cluster state until
terminal (TERMINATED / TERMINATED_WITH_ERRORS) or until the timeout.

```bash
# If you want to wait synchronously for QA to finish (default 30 min timeout):
TRIGGER_OUT=$(python "$TRIGGER_QA_SCRIPT" \
    --platform      "$ID_PLATFORM" \
    --country       "$COUNTRY" \
    --refresh       "$REFRESH" \
    --engineer-name "$ENGINEER_NAME" \
    --check-existing \
    --wait-for-completion \
    --completion-timeout 1800 \
    --completion-poll-interval 30)

VERDICT=$(echo "$TRIGGER_OUT" | jq -r '.verdict')
COMPLETION=$(echo "$TRIGGER_OUT" | jq '.completion // {}')
```

Verdict mapping:

| `verdict` value | What it means | Next action |
|---|---|---|
| `completed_success` | Cluster TERMINATED cleanly | Step 3 (write state) → Step 4 → success notification |
| `completed_with_errors` | Cluster TERMINATED_WITH_ERRORS | Read `completion.final_reason`; check Spark log in EMR console; do NOT mark QA as passed |
| `completion_timeout` | Cluster still running after timeout | Either bump `--completion-timeout` or come back later (`/trigger-qa` with same args will detect via Smart entry) |
| `triggered_cluster_started` | (No `--wait-for-completion`) cluster is up but we didn't poll | OK — manual follow-up via console URL |
| `triggered_no_cluster_observed` | Lambda OK but verification didn't see cluster in time | Manual `aws emr list-clusters --active` after a few minutes |
| `lambda_invoke_failed` | Lambda returned non-2xx | See inline error table above |

**Inline error decisions for completion poll:**

| If you see… | Likely cause | Do |
|---|---|---|
| Cluster stuck in `STARTING` >10 min | EMR queue overload / subnet capacity | Wait longer or check EMR Service Limits |
| `BOOTSTRAPPING` failing repeatedly | Bootstrap action error in cluster config | Inspect cluster's "Bootstrap actions" tab in EMR console — fix is infra-side |
| `TERMINATED_WITH_ERRORS` reason mentions S3 | QA pipeline can't read from S3 (path missing or permission) | Confirm `verify` from id-refresh showed files; check task role policies |
| `TERMINATED_WITH_ERRORS` reason mentions Spark | Spark job logic error | Read step logs — usually a schema mismatch with new tables |

---

## Step 3 — Persist state file

Write the outcome so future invocations can detect "already triggered" via
Smart entry (Phase 0.6).

```bash
state_write "$ID_PLATFORM" "$COUNTRY" "$REFRESH" "triggered" \
    "$(jq -n \
        --arg verdict     "$VERDICT" \
        --arg cluster_id  "${CLUSTER_ID:-}" \
        --arg console_url "${CONSOLE_URL:-}" \
        --argjson completion "${COMPLETION:-null}" \
        '{verdict:$verdict, cluster_id:$cluster_id,
          cluster_console_url:$console_url, completion:$completion}')"

echo "📝 State persisted to $STATE_DIR/${ID_PLATFORM}-${COUNTRY}-${REFRESH}.json"
```

---

## Step 4 — Next Steps (verdict-driven)

Pick the SPECIFIC next action based on `$VERDICT`:

```
✅ completed_success           → "QA done. Data is QA-approved.
                                  Notify product / data team. No further action."

❌ completed_with_errors       → "QA found errors. Inspect logs:
                                  $CONSOLE_URL
                                  Likely culprit: see Step 2 inline table.
                                  Once fixed → /trigger-qa to re-run."

⏰ completion_timeout          → "QA still running. Either:
                                  1. /trigger-qa again later (Smart entry will reuse cluster)
                                  2. Watch in console: $CONSOLE_URL"

🆗 triggered_cluster_started   → "QA started. Cluster: $CLUSTER_ID
                                  Watch in console: $CONSOLE_URL
                                  Re-run /trigger-qa --wait-for-completion to block on result."

⚠️ triggered_no_cluster_observed → "Lambda invoked but cluster missing.
                                  Wait 1-2 min then: aws emr list-clusters --active
                                  If still missing — Lambda may have failed silently,
                                  inspect Lambda CloudWatch logs."

❌ lambda_invoke_failed        → "Re-MFA, verify FUNCTION_NAME, retry /trigger-qa."
```

If user's broader workflow was a data refresh (id-refresh → trigger-qa), point
to follow-up:

```
After completed_success:
  • Notify QA channel that {ID_PLATFORM}/{COUNTRY}/{REFRESH} is verified
  • If product team blocked on this → ping them with $CONSOLE_URL

After completed_with_errors / lambda_invoke_failed:
  • If the issue is data — go back to /run-detail or /id-refresh to re-crawl
  • If the issue is QA pipeline — escalate to data engineering team
```

---

## Script reference (`trigger_qa_pipeline.py`)

The script accepts these CLI flags (full list — most have sensible defaults):

| Flag | Default | Purpose |
|---|---|---|
| `--platform` | (required) | 2-10 uppercase code, e.g. `EPL` |
| `--country`  | (required) | 2-letter, e.g. `US` |
| `--refresh`  | (required) | YYYYMM, e.g. `202602` |
| `--engineer-name` *or* `--engineer-id` | (one required) | Resolves to Slack ID |
| `--engineer-map-file` | (none) | JSON file extending the default Slack-id mapping |
| `--env` | `dev` | Lambda payload `env` |
| `--layer` | `raw` | Lambda payload `layer` |
| `--region` | `eu-central-1` | All AWS calls |
| `--function-name` | `dash-sourcing-pipeline-spark` | Function name OR full ARN; defaults to caller's account |
| `--tables` | `outlet_information,outlet_meal,meal_option,option_relation` | Comma-sep |
| `--load-raw-as-strings` | `0` | `1` for raw-string schema-conversion path |
| `--no-verify-emr` | (off) | Skip cluster verification — risky, NOT recommended |
| `--verify-attempts` | `10` | Cluster-existence poll count |
| `--verify-interval` | `3` | Seconds between existence polls |
| `--check-existing` | (off) | **Recommended ON** — prevents duplicate triggers |
| `--wait-for-completion` | (off) | Block until cluster terminal state |
| `--completion-timeout` | `1800` (30 min) | Max wait for completion |
| `--completion-poll-interval` | `30` | Seconds between completion polls |

---

## Notes

- **The script uses local `aws` CLI** — no `boto3` dependency.
- **Account scope:** `--function-name` defaults to the bare name. AWS CLI
  resolves it against the caller's account. To target a Lambda in a different
  account, pass the full ARN.
- **A Lambda response body of `null`** is acceptable as long as
  `verdict: triggered_cluster_started` (or better) confirms the EMR cluster
  came up. The Lambda intentionally returns null on success.
- **Engineer mapping:** the built-in map covers the current team. New people
  can be added via `--engineer-map-file` without touching this script.
- **State files** live in `~/.claude/state/trigger-qa/{p}-{c}-{r}.json`.
  These persist across invocations (Smart entry depends on them); don't
  clean them as part of leave-no-trace.


---
name: conso-deploy
description: "ConSo Deployment & Scheduling Assistant. Deploy a ConSo spider project to production and configure monthly automated crawling. Covers ECR, GitHub Actions, CloudWatch, EventBridge scheduling, and CASS activation. Use when the user says: deploy, deploy to production, set up scheduling, create EventBridge rule, 部署, 上线."
disable-model-invocation: true
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, Agent
---

# ConSo Deployment & Scheduling Assistant

Deploy a ConSo spider project to production and configure monthly automated crawling.
Reference projects: look at recently deployed ConSo repos under the configured GitHub
organization (e.g. TKW, JSE, DLR) — inspect them via `gh repo view {github_org}/{REPO}`
or clone locally when a concrete example is needed. Do NOT assume any specific local path.

---

**Narrate every step out loud.** Deployment touches production — silence is dangerous.
Before executing each phase or sub-step, tell the user:

1. Which phase/step you are on (e.g. "Phase 5B.2 — binding Lambda target for NL").
2. What you are about to do and why — one or two sentences of context so the user
   understands the purpose, not just the mechanics.
   Example: "put-targets tells the EventBridge rule WHAT to invoke. Without it, the
   cron fires into the void — no error, no data."
3. The result after the action (rule ARN, image tag, task ID, assertion pass/fail).

Never silently execute a block of commands. A missed MFA, a wrong branch, a misspelled
rule name — any of them silently breaks production for days. Narration is the only
defense.

**Auto-fix confirmation rule.** If an error or unexpected state would require deviating
from this skill's prescribed path — creating undocumented AWS resources, modifying IAM
policies, touching another platform's rules, bypassing MFA, using `--force` /
`--override` flags, editing spider code outside the current repo — **stop and explain
the deviation to the user before proceeding**. Ask for explicit confirmation before
applying any such fix.

**Leave no trace.** Every throwaway file produced during deployment (e.g. `/tmp/*.json`
targets payloads in Phase 5B, scratch scripts) must be deleted once its purpose is
served. Permanent files (`.github/workflows/ECR.yml`, `Dockerfile`, `pyproject.toml`)
stay committed; everything else gets cleaned up.

---

## Startup — Self-Introduction & Parameter Collection

On invocation, announce the plan AND collect required inputs before any action.
Print the block below verbatim, then stop and wait for user to fill the inputs.

```
👋 conso-deploy. Deployment flow (I will narrate each step):

  Phase 0    — Resolve variables (single source of truth)
  Phase 1    — Pre-deployment checks (project structure, pipeline, MFA)
  Phase 2    — GitHub repo setup (GH_TOKEN, ECR.yml)
  Phase 3    — AWS infra (ECR repo, CloudWatch log group)
  Phase 4    — Push & CI build (git → ECR image)
  Phase 4.5  — Pre-launch Gate (6 STOP-style checks)
  Phase 5    — Monthly scheduling (SpiderKeeper + EventBridge, per prefix)
  Phase 6    — Verification (CASS activation + summary)

Required inputs — I will STOP if any is missing:

  ID_PLATFORM        (e.g. DLR, TKW — uppercase, 3-4 letters)
  PREFIXES           (space-separated, e.g. "NL BE DE")
  PLATFORM_NAME      (human-readable, e.g. "Deliveroo")
  GITHUB_ORG         (default: dashmote)
  DEPLOY_BRANCH      (default: feature/conso)
  CRON_DAY_OF_MONTH  (1 / 7 / 14 / 21 — when detail runs monthly, UTC)
```

After the user fills in the inputs, run Phase 0 to expand them into the full variable
table, print the resolved table back, and **wait for user OK** before Phase 1.

---

## Phase 0 — Variable Resolution (single source of truth)

Resolve every variable once here. All subsequent phases MUST reference this table —
never re-derive names on the fly. Any mismatch downstream is a bug, not a feature.

```bash
# ---- From user input (Startup) ----
export ID_PLATFORM="DLR"             # e.g. DLR
export PREFIXES="NL BE"              # space-separated; iterate in Phase 5B
export PLATFORM_NAME="Deliveroo"     # human-readable
export GITHUB_ORG="dashmote"
export DEPLOY_BRANCH="feature/conso"
export CRON_DAY_OF_MONTH=1

# ---- Derived (computed once) ----
export id_platform_lower=$(echo "$ID_PLATFORM" | tr '[:upper:]' '[:lower:]')
export CONTAINER_NAME="conso_${id_platform_lower}_spider"
export LOG_GROUP="/ecs/${CONTAINER_NAME}"
export ECR_REPO="${CONTAINER_NAME}"

# ---- Extracted from AWS (NEVER hardcode account ID) ----
export AWS_REGION="eu-central-1"
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity \
    --query Account --output text --region "$AWS_REGION")
export LAMBDA_ARN="arn:aws:lambda:${AWS_REGION}:${AWS_ACCOUNT_ID}:function:dash-sourcing-spider-scheduler"

# ---- Per-prefix (computed inside the Phase 5B loop) ----
# RULE_NAME    = "conso-${ID_PLATFORM}-${PREFIX}-detail"
# STATEMENT_ID = "$RULE_NAME"   # must match (Invariant I1)
# SOURCE_ARN   = "arn:aws:events:${AWS_REGION}:${AWS_ACCOUNT_ID}:rule/${RULE_NAME}"
```

### Resolved Table (print this back to the user BEFORE Phase 1)

| Variable | Example | Used in |
|---|---|---|
| `ID_PLATFORM` | `DLR` | project dir name, `settings.py PLATFORM`, CASS key |
| `id_platform_lower` | `dlr` | ECR repo, Docker image, log group |
| `PLATFORM_NAME` | `Deliveroo` | commit msgs, README header |
| `PREFIXES` | `NL BE` | EventBridge rule loop, CASS `--prefixes` |
| `GITHUB_ORG` | `dashmote` | all `gh` commands |
| `DEPLOY_BRANCH` | `feature/conso` | `git push`, `ECR.yml` trigger |
| `CRON_DAY_OF_MONTH` | `1` | AWS cron expression `cron(0 0 $DAY * ? *)` |
| `AWS_ACCOUNT_ID` | (dynamic, e.g. `593453040104`) | `LAMBDA_ARN`, `SOURCE_ARN` |
| `AWS_REGION` | `eu-central-1` | every `aws` call |
| `CONTAINER_NAME` | `conso_dlr_spider` | ECR / log group / target Input `container_name` |
| `LAMBDA_ARN` | (derived) | `put-targets --targets[].Arn` |

If any downstream command references a name that doesn't match this table, it's a bug.
STOP, narrate the mismatch, ask the user how to proceed.

---

## Cross-phase Invariants — things that MUST stay coupled

These are the tight couplings between phases. A mismatch on any of them produces
**silent failures** that only surface days or weeks later. After each phase that
touches an invariant, verify the fields match Phase 0's resolved table.

| # | Invariant | Fields it couples | Break = |
|:-:|---|---|---|
| **I1** | `RULE_NAME` ≡ `STATEMENT_ID` ≡ suffix of `SOURCE_ARN` | Phase 5B Step 1 / Step 2 / Step 3 | Permission attached to wrong rule → cron fires, Lambda never invoked, no error surfaces |
| **I2** | `CONTAINER_NAME` ≡ ECR repo name ≡ log group suffix ≡ target Input `container_name` + `task_name` | Phase 3.1, 3.2, 4.3, 5B.2 | ECS can't find task family; EventBridge succeeds, task launch fails silently in DLQ |
| **I3** | `DEPLOY_BRANCH` ≡ `ECR.yml` `on.push.branches` ≡ `git push` target | Phase 2.2, 4.1 | `git push` succeeds, CI never triggers, ECR image never updates |
| **I4** | `settings.py` contains `DB = PLATFORM` (**YDE/LMN incident 2026-04-13** — 13+ days silent MySQL data loss) | Phase 1.2, Phase 4.5 Gate G3 | `Crawled N pages` keeps logging, `Inserted/Updated` never appears, MySQL row count flat — NO error logs |
| **I5** | Production pipelines uncommented (`RDSPipeline`, `PreprocessPipeline`, `S3Pipeline`) AND `MongoDBPipeline` commented everywhere | Phase 1.2, Phase 4.5 Gates G1+G2 | Data writes to local MongoDB (wrong DB) or nowhere — never reaches production S3/MySQL |
| **I6** | `AWS_ACCOUNT_ID` in `LAMBDA_ARN` ≡ `AWS_ACCOUNT_ID` in `SOURCE_ARN` ≡ the account MFA authenticates to | Phase 0, 5B.2, 5B.3 | Cross-account permission → EventBridge cannot invoke Lambda |

**Rule of thumb**: after every AWS/git command, grep the output for the invariant's
fields and assert they match Phase 0. If the assertion fails, STOP and narrate —
don't "probably OK it".

---

## Phase 1 — Pre-Deployment Checks

### 1.1 Verify project structure
Confirm the project has all required ConSo files:
```
.github/workflows/ECR.yml   ← CI/CD trigger
Dockerfile                   ← Container build
scrapy.cfg                   ← Scrapy config
{ID_PLATFORM}/settings.py    ← Spider settings
{ID_PLATFORM}/items.py       ← Item definitions
{ID_PLATFORM}/spiders/conso_outlet_finder.py   ← (if finder exists)
{ID_PLATFORM}/spiders/conso_outlet_detail.py   ← detail spider
scripts/push_grids.py        ← (if finder exists)
pyproject.toml               ← dependencies
```

### 1.2 Verify pipeline state (pre-deployment)
Both spiders must be in **production mode** before deploying:

**Finder** — `RedisPipeline` active, `MongoDBPipeline` commented out:
```python
"ITEM_PIPELINES": {
    'dashmote_sourcing.pipelines.RDSPipeline': 300,
    # 'dashmote_sourcing.pipelines.MongoDBPipeline': 300,
},
```

**Detail** — `PreprocessPipeline` + `S3Pipeline` active, `MongoDBPipeline` commented out:
```python
"ITEM_PIPELINES": {
    'dashmote_sourcing.pipelines.PreprocessPipeline': 100,
    'dashmote_sourcing.pipelines.S3Pipeline': 400,
    # 'dashmote_sourcing.pipelines.MongoDBPipeline': 400,
},
```

Run:
```bash
grep -n "MongoDBPipeline" {ID_PLATFORM}/spiders/*.py
```
Every match must be commented out (`# `).

### 1.3 AWS MFA
```bash
aws sts get-caller-identity --region eu-central-1
```
If expired, run `dash-mfa` or `get_session_token.sh`.

---

## Phase 2 — GitHub Repository Setup

### 2.1 Ensure GH_TOKEN secret
```bash
gh secret list --repo {github_org}/{ID_PLATFORM}
```
If `GH_TOKEN` missing:
```bash
gh auth token | gh secret set GH_TOKEN --repo {github_org}/{ID_PLATFORM}
```

### 2.2 Generate ECR.yml (if not present)
Use the TKW pattern — dispatches to `dashmote-sourcing/platform.yml`:

```yaml
on:
  push:
    branches:
      - 'feature/conso'
  workflow_dispatch:

jobs:
  dash-ecr-trigger:
    runs-on: ubuntu-latest
    steps:
      - name: Extract repository names
        run: |
          echo "REPO_NAME=$(echo '${{ github.repository }}' | cut -d'/' -f2)" >> $GITHUB_ENV
          echo "REPO_NAME_LOWER=$(echo '${{ github.repository }}' | cut -d'/' -f2 | tr '[:upper:]' '[:lower:]')" >> $GITHUB_ENV

      - name: Set image name
        run: echo "IMAGE_NAME=conso_${REPO_NAME_LOWER}_spider" >> $GITHUB_ENV

      - name: Print variables
        run: |
          echo "IMAGE_NAME: ${IMAGE_NAME}"
          echo "REPO_NAME: ${REPO_NAME}"

      - name: Execute curl command
        run: |
          curl -L \
            -X POST \
            -H "Accept: application/vnd.github+json" \
            -H "Authorization: Bearer ${{ secrets.GH_TOKEN }}" \
            -H "X-GitHub-Api-Version: 2022-11-28" \
            https://api.github.com/repos/dashmote/dashmote-sourcing/actions/workflows/platform.yml/dispatches \
            -d "{\"ref\":\"feature/conso\",\"inputs\":{\"image_name\":\"${IMAGE_NAME}\",\"path\":\"${REPO_NAME}\"}}"
        # default: "region":"eu-central-1"
        # default: "branch":"feature/conso"
```

Write to `.github/workflows/ECR.yml`.

---

## Phase 3 — AWS Infrastructure

### 3.1 ECR Repository
```bash
aws ecr describe-repositories --repository-names conso_{id_platform_lower}_spider --region eu-central-1 2>/dev/null \
  || aws ecr create-repository --repository-name conso_{id_platform_lower}_spider --region eu-central-1
```

### 3.2 CloudWatch Log Group
Note: use `MSYS_NO_PATHCONV=1` on Windows to prevent path conversion.
```bash
MSYS_NO_PATHCONV=1 aws logs create-log-group --log-group-name '/ecs/conso_{id_platform_lower}_spider' --region eu-central-1 2>/dev/null
MSYS_NO_PATHCONV=1 aws logs put-retention-policy --log-group-name '/ecs/conso_{id_platform_lower}_spider' --retention-in-days 60 --region eu-central-1
```

---

## Phase 4 — Push & Build

### 4.1 Commit and push
```bash
git add .
git commit -m "feat: deploy {platform_name} to production"
git push origin feature/conso
```

### 4.2 Monitor CI/CD (blocking, auto-fetch logs on failure)

The push triggers a **two-hop CI chain**:
1. `ECR.yml` in the project repo (fast, ~10s — just dispatches an API call)
2. `dashmote-sourcing/platform.yml` (actual Docker build + ECR push, ~3-5 min)

Do NOT poll by hand. Use `gh run watch` to block until completion, and auto-fetch
failure logs if the build fails.

```bash
# ---- Hop 1: wait for local ECR.yml dispatcher ----
sleep 3   # give GitHub a moment to register the run
ECR_RUN_ID=$(gh run list --repo "$GITHUB_ORG/$ID_PLATFORM" --limit 1 \
    --json databaseId -q '.[0].databaseId')

gh run watch "$ECR_RUN_ID" --repo "$GITHUB_ORG/$ID_PLATFORM" --exit-status || {
    echo "❌ ECR.yml (dispatcher) failed — logs:"
    gh run view "$ECR_RUN_ID" --repo "$GITHUB_ORG/$ID_PLATFORM" --log-failed
    exit 1
}
echo "✅ ECR.yml dispatched successfully"

# ---- Hop 2: wait for platform.yml to build + push the image ----
sleep 5   # give dashmote-sourcing repo time to pick up the dispatch
BUILD_RUN_ID=$(gh run list --repo dashmote/dashmote-sourcing \
    --workflow platform.yml --limit 1 \
    --json databaseId -q '.[0].databaseId')

gh run watch "$BUILD_RUN_ID" --repo dashmote/dashmote-sourcing --exit-status || {
    echo "❌ platform.yml (build+push) failed — logs of failed job:"
    gh run view "$BUILD_RUN_ID" --repo dashmote/dashmote-sourcing --log-failed
    echo ""
    echo "Common failure causes:"
    echo "  - pyproject.toml dependency conflict → check poetry.lock"
    echo "  - Dockerfile syntax error → view full log with 'gh run view --log'"
    echo "  - ECR push permission denied → verify GH_TOKEN has not expired"
    exit 1
}
echo "✅ platform.yml completed — new image pushed to ECR"
```

**Why two hops?** `ECR.yml` only calls the GitHub API to dispatch `platform.yml`. The
actual Docker build runs in the `dashmote-sourcing` repo. Both must succeed.

**Timing:** total wall time ~4-7 min. `gh run watch` blocks the terminal — that's the
intended behavior, not a hang.

### 4.3 Verify ECR image ↔ git HEAD correlation (STOP-style)

Do NOT just look at "latest image exists" — that's the **most common silent
failure**: CI broke, nobody noticed, ECR still has last week's image, summary prints
✅ but production runs stale code.

Require **both** conditions to pass:

```bash
GIT_SHA_SHORT=$(git rev-parse --short HEAD)
GIT_SHA_FULL=$(git rev-parse HEAD)

# Pull latest image metadata
LATEST=$(aws ecr describe-images \
    --repository-name "$ECR_REPO" \
    --region "$AWS_REGION" \
    --query 'sort_by(imageDetails, &imagePushedAt)[-1].{pushed: imagePushedAt, tags: imageTags}' \
    --output json)

PUSHED_AT=$(echo "$LATEST" | jq -r '.pushed')
TAGS=$(echo "$LATEST"       | jq -r '.tags | join(",")')

echo "📦 Latest ECR image for $ECR_REPO:"
echo "   pushed: $PUSHED_AT"
echo "   tags:   $TAGS"
echo "   expect: tag contains '$GIT_SHA_SHORT' AND pushed <10 min ago"

# ---- Check 1: tag contains HEAD SHA ----
TAG_MATCH=false
if echo "$TAGS" | grep -qE "$GIT_SHA_SHORT|$GIT_SHA_FULL"; then
    TAG_MATCH=true
    echo "✅ image tag contains HEAD SHA"
else
    echo "⚠️  image tag does NOT contain HEAD SHA ($GIT_SHA_SHORT)."
    echo "    If platform.yml uses a non-SHA tag convention (e.g. 'latest'),"
    echo "    this may be OK — falling back to timestamp check only."
fi

# ---- Check 2: pushed timestamp <10 minutes ago ----
# AWS returns ISO8601 with timezone; GNU date parses it directly.
PUSHED_EPOCH=$(date -d "$PUSHED_AT" +%s 2>/dev/null)
NOW_EPOCH=$(date -u +%s)
AGE_MIN=$(( (NOW_EPOCH - PUSHED_EPOCH) / 60 ))

if (( AGE_MIN <= 10 )); then
    echo "✅ image pushed ${AGE_MIN}m ago — fresh"
else
    echo "❌ image pushed ${AGE_MIN}m ago — too old, CI did NOT produce a new image."
    echo "   STOP. Common causes:"
    echo "     - platform.yml succeeded but was a no-op (nothing changed?)"
    echo "     - Build pushed under a different tag than expected"
    echo "     - You're looking at the wrong ECR_REPO ($ECR_REPO)"
    exit 1
fi

# ---- Both pass? ----
if $TAG_MATCH || (( AGE_MIN <= 10 )); then
    echo "✅ Phase 4.3 verified: image matches HEAD and/or is fresh"
else
    echo "❌ Phase 4.3 FAILED — neither tag nor timestamp confirms this is a new build"
    exit 1
fi
```

**Why this matters:** Phase 4.2 already confirms CI succeeded. But "CI succeeded"
and "ECR has the image we expect" are different claims — a build can succeed
without actually pushing (skipped-by-filter, cache-only re-tag, etc.). Phase 4.3
is the **last chance** to catch a stale-image bug before Phase 5 schedules cron
jobs on top of old code.

---

## Phase 4.5 — Pre-launch Gate (STOP-style, all 6 must pass)

**Before touching Phase 5 (which writes schedule rules to production AWS), verify the
code that shipped is production-ready.** Any FAIL = STOP. Fix the underlying issue,
re-run the gate. Do NOT proceed to Phase 5 on "probably OK" — the failure modes here
are all silent: cron fires, AWS says success, but no data is produced.

### G1 — Production pipelines are uncommented

Both finder (`RDSPipeline`) and detail (`PreprocessPipeline` + `S3Pipeline`) must be
LIVE in the spider's `custom_settings` block.

```bash
# Show all production-pipeline references that are NOT commented out
grep -nE "RDSPipeline|PreprocessPipeline|S3Pipeline" ${ID_PLATFORM}/spiders/*.py \
  | grep -vE "^\s*#|:\s*#"
```

**PASS** if output contains at least `RDSPipeline` (finder) and both `PreprocessPipeline`
and `S3Pipeline` (detail), all uncommented.
**FAIL** if any expected pipeline line starts with `#`.

### G2 — Debug `MongoDBPipeline` is commented out (everywhere)

```bash
# Show MongoDBPipeline references that are NOT commented out
grep -nE "MongoDBPipeline" ${ID_PLATFORM}/spiders/*.py \
  | grep -vE "^\s*#|:\s*#"
```

**PASS** if NO output (every MongoDBPipeline line has `#` in front).
**FAIL** if ANY line appears uncommented — data routes to local MongoDB instead of
production S3. Couples to Invariant **I5**.

### G3 — `settings.py` contains `DB = PLATFORM` (prevents YDE/LMN-class incidents)

**Reference**: see conso-migrate Appendix A1. On 2026-04-13, YDE/LMN ran for 13+ days
with `DB = PLATFORM` missing from `settings.py`. `RDSPipeline` in the scrapyd container
reads `self.settings.get("DB")` with NO fallback → `db_name=None` → MySQL inserts
silently no-op. `Crawled N items` log grew normally; `Inserted/Updated` never appeared;
nobody noticed for 13 days.

```bash
grep -nE "^DB\s*=\s*PLATFORM\s*$" ${ID_PLATFORM}/settings.py
```

**PASS** if exactly one match.
**FAIL** otherwise — NEVER skip this gate. Couples to Invariant **I4**.

### G4 — Current git branch is `DEPLOY_BRANCH`

```bash
CURRENT_BRANCH=$(git branch --show-current)
if [[ "$CURRENT_BRANCH" == "$DEPLOY_BRANCH" ]]; then
  echo "✅ on $DEPLOY_BRANCH"
else
  echo "❌ on '$CURRENT_BRANCH' — expected '$DEPLOY_BRANCH'"
fi
```

**FAIL** = `git push origin $DEPLOY_BRANCH` from the wrong branch silently pushes the
wrong code (or fails non-fast-forward). Couples to Invariant **I3**.

### G5 — Working tree is clean

```bash
if [[ -z "$(git status --porcelain)" ]]; then
  echo "✅ clean"
else
  echo "❌ uncommitted changes:"
  git status --short
fi
```

**FAIL** = local changes are NOT in the pushed commit → the deployed ECR image does
not contain what you think it does. Rebuild after committing.

### G6 — AWS MFA session valid in target account

```bash
ACTUAL_ACCT=$(aws sts get-caller-identity \
  --query Account --output text --region "$AWS_REGION" 2>/dev/null)

if [[ "$ACTUAL_ACCT" == "$AWS_ACCOUNT_ID" ]]; then
  echo "✅ authenticated to $AWS_ACCOUNT_ID"
else
  echo "❌ authenticated to '$ACTUAL_ACCT' — expected '$AWS_ACCOUNT_ID'"
fi
```

**FAIL** = expired session OR authenticated to wrong AWS account. Run `dash-mfa` /
`get_session_token.sh` and re-verify. Couples to Invariant **I6**.

---

### Gate summary — print this before entering Phase 5

```
G1 production pipelines enabled         [✅ / ❌]
G2 MongoDBPipeline commented out        [✅ / ❌]
G3 DB = PLATFORM present                [✅ / ❌]
G4 git branch = DEPLOY_BRANCH           [✅ / ❌]
G5 working tree clean                   [✅ / ❌]
G6 AWS MFA valid (correct account)      [✅ / ❌]
```

All six MUST be ✅. If ANY is ❌ — **STOP, narrate which gate failed, ask the user how
to proceed**. Do not enter Phase 5.

---

## Phase 5 — Monthly Scheduling

ConSo uses a **two-phase monthly cycle**:

### Phase 5A — Finder Scheduling (SpiderKeeper — manual)

Finder runs are triggered manually via SpiderKeeper UI:
- URL: http://spider.getdashmote.com:1234
- Project: `ConSo_{ID_PLATFORM}`
- Spider: `conso_outlet_finder`
- Args: `prefix={PREFIX},sample=0`

**If finder .egg not yet uploaded**, build and upload:
```bash
scrapyd-deploy --build-egg output.egg
```
Then upload via SpiderKeeper API (see conso-migrate skill Phase 13).

### Phase 5B — Detail Scheduling (EventBridge — automated)

Detail runs are automated via AWS EventBridge → Lambda → ECS Fargate.

**Architecture:**
```
AWS EventBridge Rule: conso-$ID_PLATFORM-$PREFIX-detail          (per prefix)
  ↓  (cron trigger: cron(0 0 $CRON_DAY_OF_MONTH * ? *), UTC)
Lambda: dash-sourcing-spider-scheduler                           (shared)
  ↓  (receives JSON Input with container/cmd/env config)
ECS Fargate Task: $CONTAINER_NAME                                (per platform)
  ↓
scrapy runspider conso_outlet_detail.py -a prefix=$PREFIX
```

**4 AWS operations per prefix, looped.** The block below is the per-prefix loop body —
wrap it in `for PREFIX in $PREFIXES; do ... done` to provision every prefix.

```bash
# ==== Per-prefix variables (Invariant I1: all three strings MUST stay equal) ====
RULE_NAME="conso-${ID_PLATFORM}-${PREFIX}-detail"
STATEMENT_ID="$RULE_NAME"
SOURCE_ARN="arn:aws:events:${AWS_REGION}:${AWS_ACCOUNT_ID}:rule/${RULE_NAME}"
```

#### Step 1 — Create EventBridge Rule (DISABLED first)

Create the rule with a cron expression. Start as DISABLED for safety — enable only
after the target binding is verified (Step 5).

```bash
MSYS_NO_PATHCONV=1 aws events put-rule \
  --name "$RULE_NAME" \
  --schedule-expression "cron(0 0 ${CRON_DAY_OF_MONTH} * ? *)" \
  --state DISABLED \
  --region "$AWS_REGION"
```

Common cron patterns (AWS 6-field: `min hour day month week year`, all UTC):
- Monthly 1st:  `cron(0 0 1 * ? *)`
- Monthly 7th:  `cron(0 0 7 * ? *)`
- Monthly 14th: `cron(0 0 14 * ? *)`
- Monthly 21st: `cron(0 0 21 * ? *)`
- Quarterly:    `cron(0 0 1 1,4,7,10 ? *)`

**AWS cron gotchas (DO NOT skip):**
- `day-of-month` and `day-of-week` cannot BOTH be specified — the unused one must be `?`, not `*`. `cron(0 0 1 * * *)` is invalid.
- All times are **UTC**. If the business wants "02:00 CET" be aware of summer/winter shift (CET = UTC+1, CEST = UTC+2); the cron does NOT shift with DST.
- Use 24-hour hour field. `0 0` = 00:00 UTC, not noon.

#### Step 2 — Bind Lambda Target

Tell the rule WHAT to trigger. The Lambda receives a JSON payload containing the ECS
container name, command, and environment variables.

**⚠️ DO NOT use inline shell-escaped JSON for `--targets`.** A single miscount of `\"`
produces a rule that AWS accepts as syntactically valid but semantically broken — cron
fires, Lambda invokes, but the ECS task never starts. Use `jq` + file references.

```bash
# 1) Build the Lambda Input payload (the JSON the scheduler Lambda receives)
jq -n \
  --arg container   "$CONTAINER_NAME" \
  --arg prefix      "$PREFIX" \
  --arg platform    "$ID_PLATFORM" \
  --arg today       "$(date -u +%Y%m%d)" \
  '{
    container_name: $container,
    cmd:            ["python3","-m","scrapy","runspider","conso_outlet_detail.py","-a",("prefix=" + $prefix)],
    prefix:         $prefix,
    task_name:      $container,
    env: [
      {name: "LOG_STREAM", value: $prefix},
      {name: "Platform",   value: $platform},
      {name: "Time",       value: $today}
    ]
  }' > "/tmp/conso-deploy-input-${PREFIX}.json"

# 2) Build the put-targets request (Lambda ARN resolved from Phase 0 — never hardcode account ID)
jq -n \
  --arg arn   "$LAMBDA_ARN" \
  --rawfile input "/tmp/conso-deploy-input-${PREFIX}.json" \
  '[{
    Id:    "1",
    Arn:   $arn,
    Input: $input
  }]' > "/tmp/conso-deploy-targets-${PREFIX}.json"

# 3) Submit — feed from file, zero escaping
MSYS_NO_PATHCONV=1 aws events put-targets \
  --rule "$RULE_NAME" \
  --region "$AWS_REGION" \
  --targets "file:///tmp/conso-deploy-targets-${PREFIX}.json"
```

**Why this approach:**
- `Id` is `"1"` — a stable, meaningful target ID. Older docs showed `"Id": "string"` (literal); re-running would collide silently.
- `Arn` composes from `AWS_ACCOUNT_ID` (Phase 0), not the historical hardcoded `593453040104`. Works in any account.
- `Input` is built by `jq` — no `\"` escaping hell; invalid JSON fails locally, before touching AWS.
- `/tmp/conso-deploy-*.json` are throwaway; delete them at the end of Phase 5 (leave-no-trace rule at top of skill).

**Fields in the Input JSON:**
- `container_name` — must match the ECS task definition's container name (**Invariant I2**)
- `cmd` — the scrapy command that runs inside the container
- `prefix` — country code passed to the spider
- `task_name` — ECS task family name (same as container_name for ConSo)
- `env` — environment variables injected at runtime:
  - `LOG_STREAM` — Fluent Bit log routing (set to prefix)
  - `Platform` — Fluent Bit label
  - `Time` — cosmetic label, not read at runtime (kept for historical compatibility)

#### Step 3 — Grant EventBridge permission to invoke Lambda

Without this, EventBridge cannot call the Lambda function. Order matters — put-targets
(Step 2) works without permission, but the cron fire in Step 4+ will fail silently.

**Do NOT "safely ignore `ResourceConflictException`" blindly.** If the statement-id
already exists, it might be YOURS (idempotent re-run, OK to skip) OR it might be
another rule's (statement-id collision, permissions wrong). Inspect before deciding.

```bash
# Check if the statement already exists and points to OUR source ARN
EXISTING_SRC=$(aws lambda get-policy \
    --function-name dash-sourcing-spider-scheduler \
    --region "$AWS_REGION" 2>/dev/null \
  | jq -r ".Policy | fromjson | .Statement[] | select(.Sid==\"$STATEMENT_ID\") | .Condition.ArnLike.\"AWS:SourceArn\"" 2>/dev/null)

if [[ -z "$EXISTING_SRC" ]]; then
    # Fresh install — add the permission
    MSYS_NO_PATHCONV=1 aws lambda add-permission \
      --function-name dash-sourcing-spider-scheduler \
      --statement-id "$STATEMENT_ID" \
      --action lambda:InvokeFunction \
      --principal events.amazonaws.com \
      --source-arn "$SOURCE_ARN" \
      --region "$AWS_REGION"
    echo "✅ permission added for $STATEMENT_ID"
elif [[ "$EXISTING_SRC" == "$SOURCE_ARN" ]]; then
    echo "✅ permission already exists and points to correct rule — skipping"
else
    echo "❌ statement-id '$STATEMENT_ID' exists but points to '$EXISTING_SRC' (expected '$SOURCE_ARN')"
    echo "   STOP — this is a statement-id collision. Ask user before proceeding."
    exit 1
fi
```

#### Step 4 — Enable the rule (DISABLED → ENABLED)

After Steps 1–3 are verified, flip the rule live.

```bash
MSYS_NO_PATHCONV=1 aws events enable-rule \
  --name "$RULE_NAME" \
  --region "$AWS_REGION"
```

#### Step 5 — Verify the complete setup (structured assertions)

Don't just eyeball the output — parse and assert each field. The verification below
fails loudly on any mismatch (Invariants I1, I2, I6).

```bash
# Read back what AWS actually stored
RULE_STATE=$(aws events describe-rule \
    --name "$RULE_NAME" --region "$AWS_REGION" \
    --query 'State' --output text)

TARGET_JSON=$(aws events list-targets-by-rule \
    --rule "$RULE_NAME" --region "$AWS_REGION" \
    --query 'Targets[0]' --output json)

TARGET_ARN=$(echo "$TARGET_JSON"        | jq -r '.Arn')
TARGET_INPUT=$(echo "$TARGET_JSON"      | jq -r '.Input')
INPUT_CONTAINER=$(echo "$TARGET_INPUT"  | jq -r '.container_name')
INPUT_PREFIX=$(echo "$TARGET_INPUT"     | jq -r '.prefix')

# Structured assertions — any failure = STOP
[[ "$RULE_STATE"      == "ENABLED" ]]        || { echo "❌ rule state = $RULE_STATE (expected ENABLED)"; exit 1; }
[[ "$TARGET_ARN"      == "$LAMBDA_ARN" ]]    || { echo "❌ target ARN mismatch (I6): $TARGET_ARN"; exit 1; }
[[ "$INPUT_CONTAINER" == "$CONTAINER_NAME" ]] || { echo "❌ Input container_name mismatch (I2): $INPUT_CONTAINER"; exit 1; }
[[ "$INPUT_PREFIX"    == "$PREFIX" ]]        || { echo "❌ Input prefix mismatch: $INPUT_PREFIX (expected $PREFIX)"; exit 1; }

echo "✅ $RULE_NAME verified: ENABLED, bound to Lambda, Input fields match Phase 0"
```

#### Step 6 — Cleanup (leave-no-trace)

Delete the `/tmp` payload files once the rule is verified.

```bash
rm -f "/tmp/conso-deploy-input-${PREFIX}.json" "/tmp/conso-deploy-targets-${PREFIX}.json"
```

#### Alternative: Use conso_scheduler.ipynb

If preferred, the same setup can be done via the `conso_scheduler.ipynb` notebook
on the Jupyter server (http://spider.getdashmote.com:8888/). The notebook reads
from the `conso_schedule` PostgreSQL table and creates/updates all EventBridge
rules automatically. However, the CLI approach above is more transparent and
does not require access to the Jupyter server.

### Phase 5C — Manual trigger (for testing or ad-hoc runs)

Trigger an ECS task directly without waiting for the cron schedule.
Use the `api_launch&ECS_trigger.ipynb` notebook on the Jupyter server
(http://spider.getdashmote.com:8888/), which handles subnet/security-group
configuration automatically.

---

## Phase 5.5 — Deploy-time Smoke Test (STRONGLY recommended — do NOT skip)

**Why this phase exists — the 30-day silent-failure window.**
Between Phase 5 (schedule created) and the first real cron fire (24 hours to 30
days later), there is a dangerous information void. A misconfigured rule, a broken
Input JSON, a wrong Lambda ARN, a missing IAM role, an ECS task-definition
mismatch — all fail **silently**. Nothing alerts you; the data just doesn't
appear at month end, and by then the engineer who deployed has moved on.

This phase compresses that void from **30 days to 15 minutes** by firing the same
Input that cron would send (with `sample=10` to keep it cheap), then verifying
data actually lands.

Run this for ONE prefix (first in `$PREFIXES`). If it passes, plumbing is verified
for all — the EventBridge rules differ only in name/prefix, not in transport.

### 5.5.1 — Invoke Lambda with the real production Input (sample=10)

```bash
SMOKE_PREFIX=$(echo "$PREFIXES" | awk '{print $1}')   # first prefix only
echo "🔬 Smoke test: invoking Lambda for $ID_PLATFORM/$SMOKE_PREFIX with sample=10"

# Build Lambda payload — same shape as Phase 5B Step 2, plus "sample=10"
jq -n \
  --arg container "$CONTAINER_NAME" \
  --arg prefix    "$SMOKE_PREFIX" \
  --arg platform  "$ID_PLATFORM" \
  --arg today     "$(date -u +%Y%m%d)" \
  '{
    container_name: $container,
    cmd:            ["python3","-m","scrapy","runspider","conso_outlet_detail.py","-a",("prefix=" + $prefix),"-a","sample=10"],
    prefix:         $prefix,
    task_name:      $container,
    env: [
      {name: "LOG_STREAM", value: $prefix},
      {name: "Platform",   value: $platform},
      {name: "Time",       value: $today}
    ]
  }' > /tmp/conso-deploy-smoke.json

# Invoke Lambda synchronously — returns with the ECS task ARN
MSYS_NO_PATHCONV=1 aws lambda invoke \
  --function-name dash-sourcing-spider-scheduler \
  --region "$AWS_REGION" \
  --cli-binary-format raw-in-base64-out \
  --payload file:///tmp/conso-deploy-smoke.json \
  /tmp/conso-deploy-smoke-response.json

echo "Lambda response:"
cat /tmp/conso-deploy-smoke-response.json
```

**If Lambda returns FunctionError or `errorMessage`** → **STOP**. Fix Phase 5B
Step 2 (bad Input schema) or Step 3 (missing invoke permission) before retrying.

### 5.5.2 — Poll the ECS task until RUNNING

```bash
# Extract taskArn — Lambda response shape can vary; try all known fields
TASK_ARN=$(jq -r '.taskArn // .TaskArn // .tasks[0].taskArn // empty' \
    /tmp/conso-deploy-smoke-response.json)

if [[ -z "$TASK_ARN" || "$TASK_ARN" == "null" ]]; then
    echo "❌ Lambda did not return a taskArn. Full response:"
    cat /tmp/conso-deploy-smoke-response.json
    echo "Check Lambda logs:"
    echo "  aws logs tail /aws/lambda/dash-sourcing-spider-scheduler --region $AWS_REGION --since 5m"
    exit 1
fi

echo "ECS task launched: $TASK_ARN"
echo "Polling status (max 3 min)..."

for i in {1..18}; do
    STATUS=$(aws ecs describe-tasks \
        --cluster conso-cluster \
        --tasks "$TASK_ARN" \
        --region "$AWS_REGION" \
        --query 'tasks[0].lastStatus' --output text 2>/dev/null)
    echo "  [$i/18] status=$STATUS"
    case "$STATUS" in
        RUNNING) echo "✅ task RUNNING"; break ;;
        STOPPED)
            echo "❌ task STOPPED before running — check stopReason:"
            aws ecs describe-tasks --cluster conso-cluster --tasks "$TASK_ARN" \
                --region "$AWS_REGION" --query 'tasks[0].stoppedReason' --output text
            exit 1
            ;;
        *) sleep 10 ;;
    esac
done
```

### 5.5.3 — Tail CloudWatch logs in real time

```bash
# Fluent Bit routes ECS logs to CloudWatch using LOG_STREAM=$SMOKE_PREFIX
# Follow for ~3 min, then Ctrl+C once you see the spider running
MSYS_NO_PATHCONV=1 aws logs tail "$LOG_GROUP" \
  --region "$AWS_REGION" \
  --log-stream-names "$SMOKE_PREFIX" \
  --follow --since 3m
```

**Watch for these signals (in order):**

| Log line | Meaning |
|---|---|
| `Scrapy X.Y.Z started` | Container booted, spider code loaded |
| `Spider opened` | Spider entered `start_requests` |
| `Crawled N pages (at X pages/min)` | Requests succeeding, parse working |
| `Inserted N / Updated N rows` (RDSPipeline) | **MySQL write confirmed** |
| `Uploading to s3://…` (S3Pipeline) | **S3 write confirmed** |
| `Spider closed (finished)` | Clean exit after sample=10 |

**⚠️ If `Crawled N` grows but `Inserted/Updated` never appears** → you've just
re-hit the YDE/LMN incident (I4). Check Phase 4.5 G3.

### 5.5.4 — Verify data actually landed in S3

```bash
# Adjust bucket name per environment — confirm with the user on first run
SOURCING_BUCKET="{confirm-with-user-on-first-run}"

aws s3 ls "s3://$SOURCING_BUCKET/$ID_PLATFORM/" \
    --recursive --region "$AWS_REGION" \
  | awk -v today="$(date -u +%Y-%m-%d)" '$1 == today { print }' \
  | head -20

# Expect at least one file timestamped today matching the smoke test output.
# If none, check S3Pipeline logs in Phase 5.5.3 for the exact bucket/key path.
```

### 5.5.5 — Smoke Test Verdict Table

Narrate this table back to the user:

```
Step 5.5.1 — Lambda invoke returned taskArn              [✅ / ❌]
Step 5.5.2 — ECS task reached RUNNING state               [✅ / ❌]
Step 5.5.3 — Spider logged "Spider closed (finished)"     [✅ / ❌]
Step 5.5.3 — Spider logged "Inserted/Updated N rows"      [✅ / ❌]  (MySQL)
Step 5.5.3 — Spider logged "Uploading to s3://…"          [✅ / ❌]  (S3)
Step 5.5.4 — At least one file landed in S3 today         [✅ / ❌]
```

**All must be ✅ before Phase 6.** If any ❌:

| Failed step | Likely cause | Next action |
|---|---|---|
| 5.5.1 | Bad Input JSON schema OR Lambda missing invoke permission | Re-run Phase 5B Step 2/3 |
| 5.5.2 (STOPPED early) | ECS task-def not found OR ECR image broken | Check task-def exists; re-run Phase 4.3 |
| 5.5.3 no `Crawled` | Spider can't reach target API | Check secrets/network — escalate |
| 5.5.3 no `Inserted/Updated` | **YDE-class incident — `DB = PLATFORM` missing** | Re-run Phase 4.5 G3 |
| 5.5.3 no `Uploading to s3` | S3Pipeline commented out OR S3 perms missing | Re-run Phase 4.5 G1 |
| 5.5.4 no S3 files | Logs said OK but bucket is wrong | Confirm bucket name with user |

### 5.5.6 — Cleanup (leave-no-trace)

```bash
# The smoke task should auto-exit after sample=10. If it's still RUNNING, stop it:
STATUS=$(aws ecs describe-tasks --cluster conso-cluster --tasks "$TASK_ARN" \
    --region "$AWS_REGION" --query 'tasks[0].lastStatus' --output text 2>/dev/null)

if [[ "$STATUS" == "RUNNING" ]]; then
    aws ecs stop-task \
      --cluster conso-cluster --task "$TASK_ARN" \
      --reason "smoke test complete — manual stop" \
      --region "$AWS_REGION" >/dev/null
    echo "🛑 stopped smoke task"
fi

rm -f /tmp/conso-deploy-smoke.json /tmp/conso-deploy-smoke-response.json
echo "🧹 smoke test artifacts cleaned up"
```

---

## Phase 6 — Verification

### 6.1 CASS activation
Ensure `finder_is_active` and `detail_is_active` are `True`. The `cass_insert.py`
helper lives in the sibling `conso-migrate` skill — resolve its absolute path before
running (both `~/.claude/skills/conso-migrate/` and `~/.claude/commands/conso-migrate/`
have been used historically; pick whichever exists on the current machine):
```bash
# Resolve path dynamically (works regardless of which layout is installed):
CASS_SCRIPT=$(ls ~/.claude/skills/conso-migrate/cass_insert.py 2>/dev/null \
           || ls ~/.claude/commands/conso-migrate/cass_insert.py 2>/dev/null)

poetry run python "$CASS_SCRIPT" \
  --id-platform {ID_PLATFORM} --prefixes {PREFIXES} \
  --activate --verify
```

### 6.2 Summary table

| Component | Status |
|-----------|--------|
| GH_TOKEN secret | ✅ |
| ECR.yml workflow | ✅ |
| ECR repository | ✅ |
| CloudWatch log group | ✅ |
| Docker image (ECR) | ✅ |
| ECS task definition | ✅ |
| SpiderKeeper (finder) | ✅ / N/A |
| EventBridge (detail schedule) | ✅ |
| CASS activation | ✅ |

---

🎉 **{ID_PLATFORM} is deployed and scheduled!**

Monthly cycle: Finder (SpiderKeeper manual) → Detail (EventBridge cron → Lambda → ECS Fargate)

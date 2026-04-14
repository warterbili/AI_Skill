---
name: run-detail
description: "Trigger ad-hoc detail spider runs on Fargate with image freshness check, multi-instance support, and log analysis. Detects running/completed tasks automatically. Use when the user says: run detail, trigger detail, start crawl, supplement crawl, re-run detail, check detail status, check pipeline, verify build, 跑detail, 启动detail, 补数据."
---

# ConSo Detail — Ad-hoc Fargate Run & Monitoring

## Overview

This skill **launches detail spider runs on Fargate** with pre-run image validation,
multi-instance support, log analysis, and **post-run data verification**. It is
designed for supplementary data crawling — when the monthly automated run missed
data, or you need to re-crawl specific prefixes outside the regular schedule.

**Smart entry point:** The skill first checks whether a task for this platform is
already running or recently completed. The user may invoke this skill hours after
launching a task — it must pick up where things stand, not blindly start a new run.

**Workflow:**

```
Phase 0: Preflight
    ├── AWS credentials valid?
    ├── gh CLI available? (auto-detect, fallback path optional)
    ├── Detect platform from git (sanity-checked against regex)
    └── Resolve variables (account ID, subnets, SGs, Loki instance — all dynamic)

Phase 1: Smart Entry Point
    ├── Search conso-cluster for this platform's tasks (RUNNING+STOPPED+transitional)
    │   ├── RUNNING found        → ask: monitor or launch new?
    │   ├── STOPPED found        → ask: view results or launch new?
    │   ├── PROVISIONING/PENDING → wait 30s and re-check (don't duplicate)
    │   └── None found           → continue to Phase 2
    └── If monitoring/viewing → jump to Phase 5.2 / 6 / 7

Phase 2: Collect Parameters (smart defaults inferred from git/CASS/Phase 1)
    Q1 prefix → Q2 output_month (future-date guard) → Q3 recrawl → Q4 meal_fix
    → Q5 sample → Q6 instances → Q7 multi-mode (if >1)
    → Print launch plan → User confirms

Phase 3: Image Validation (pre-run check)
    ├── 3.1 Compare ECR image time vs latest commit, capture IMAGE_DIGEST
    ├── 3.2 If stale → diagnose cause (ECR.yml curl 404?)
    ├── 3.3 If fixable → fix GH_TOKEN (⚠️ personal-token warning) → rebuild
    └── 3.4 Wait for platform.yml build (explicit break, 5 min timeout)

Phase 4: Prepare Fargate Task Definition
    ├── 4.1 Fetch shared task def, pin image by DIGEST (not :latest)
    └── 4.2 Register new revision

Phase 4.5: Pre-launch Gate (STOP-style, all 6 must pass) ── NEW
    G1 image digest CURRENT  • G2 task-def registered
    G3 platform name valid   • G4 AWS MFA still valid
    G5 sample=0 re-confirmed (production) • G6 Fargate quota headroom

Phase 5: Launch & Monitor (jq-based overrides — zero placeholders)
    ├── 5.1 Launch primary task
    ├── 5.1b Launch worker tasks (if multi-instance)
    ├── 5.2 Poll ALL task statuses every 30s
    ├── 5.3 Collect exit codes when STOPPED
    │   └── exitCode=137 (OOM) → Phase 5.3b auto-recover
    └── 5.4 Stop tasks (user-initiated abort)

Phase 6: Log Analysis (file-based SSM, instance discovered by tag)
    ├── 6.1 Calculate time window
    ├── 6.2 Query Loki via SSM (base64 payload, poll for completion)
    └── 6.3 Analyze and report

Phase 7: Post-run Data Verification ── NEW
    ├── 7.1 aws s3 ls expected path — count files actually landed
    ├── 7.2 Cross-check vs log-reported item count (>20% diff = warning)
    └── 7.3 Verdict table (LogCount vs S3Count vs MySQL rowcount)

Phase 8: Next Steps ── NEW
    Chain into /trigger-qa or conso-migrate CASS activation
```

---

**Narrate every step out loud.** This skill launches **real production Fargate
tasks** that write to live MySQL/S3. Silence = danger. Before every phase or
sub-step, tell the user:

1. Which phase/step you're on (e.g. "Phase 5.1 — launching primary task for BR").
2. What you're about to do and why (one-two sentences).
   Example: "I'll register a new task-def revision with the digest we just verified
   in Phase 3.1 — this way Fargate runs the exact image you approved, not whatever
   happens to be tagged :latest."
3. The result after the action (task ARN, exit code, S3 file count, etc.).

Never silently execute a block of commands. A misrouted `Platform` env var, a
stale `:latest` tag, an expired MFA, a `sample=0` on a test run — every one of
these silently corrupts production data. Narration is the only defense.

**Auto-fix confirmation rule.** If an error or unexpected state would require
deviating from this skill's prescribed path — modifying task-defs outside this
skill, touching OTHER platforms' tasks, bypassing MFA, force-stopping tasks not
in `$TASK_IDS`, or editing the Loki query semantics — **stop and explain the
deviation to the user before proceeding**. Ask for explicit confirmation.

**Leave no trace.** Temporary files (`/tmp/task_def_*.json`,
`/tmp/run-detail-overrides-*.json`, Loki query payload) must be `rm -f`'d at the
end of the phase that created them. Don't accumulate ECS task-definition
revisions indefinitely — Phase 8 can optionally prune old ones.

---

## Cross-phase Invariants — things that MUST stay coupled

These are the tight couplings between phases. Any mismatch produces **silent
failures** that only surface when data doesn't appear in QA. After each phase
that touches an invariant, verify the fields match Phase 0's resolved table.

| # | Invariant | Fields it couples | Break = |
|:-:|---|---|---|
| **I1** | `overrides.environment[Platform]` ≡ `ID_PLATFORM` ≡ Phase 1 filter value | Phase 1.2, 5.1, 5.1b | Smart entry can't find the task I just started; I relaunch, now 2 tasks race for same Redis queue |
| **I2** | `SERVICE_NAME=conso-outlet-detail` ≡ task-def `family` ≡ Loki `service` label | Phase 4.1, 5.1 overrides, 6.2 query | Phase 6 log query returns empty — "my task ran but there are no logs" |
| **I3** | `IMAGE_DIGEST` captured in Phase 3.1 ≡ image in Phase 4.1 registered task-def ≡ image running in Fargate | Phase 3.1 → 4.1 → 5.1 | Validated one image, ran a different one (race with concurrent `:latest` push) |
| **I4** | `ID_PLATFORM` env ≡ container env ≡ S3 path prefix ≡ MySQL DB name | Phase 5.1 overrides, 7.1, 7.2 | Data writes to wrong DB/bucket; QA shows "no data" but it exists under the wrong key (YDE/LMN-class) |
| **I5** | `sample=0` confirmed by user → writes to `s3://dash-sourcing/…` (PROD); `sample>0` → writes to `s3://dash-alpha-dev/sample/…` (DEV) | Phase 2 Q5, Phase 4.5 G5, Phase 7.1 | Accidentally pushing test data to prod bucket (or worse, prod data to test) |
| **I6** | **Exactly ONE** task per `(platform, prefix, month)` has `id_refresh=True`; all workers are `id_refresh=False` | Phase 2 id_refresh table, 5.1, 5.1b | Multiple tasks try to populate Redis → queue state clash → duplicated/missed outlets |
| **I7** | `AWS_ACCOUNT_ID` resolved in Phase 0 ≡ account MFA authenticated to ≡ account owning ECR/ECS/Subnets | Phase 0, 3.1, 4.1, 5.1 | Cross-account references → silently resolve to nothing, task launch fails with cryptic error |
| **I8** | MySQL finder data exists AND is fresh for the target prefix | Phase 0.6 preflight, Phase 2 Q1 default | detail's `filter()` returns 0 outlets → task exits immediately with 0 items scraped, ~$2-5 wasted on Fargate startup |

**Rule of thumb:** after every AWS call, grep output for the invariant's fields
and assert they match Phase 0. If an assertion fails, STOP and narrate — don't
"probably OK" it.

---

## Phase 0 — Preflight

**Do these first.** Phase 1 needs AWS credentials to search for tasks.

### 0.1 AWS credentials + extract account ID

```bash
export AWS_REGION="eu-central-1"

# Verify session AND capture account ID in one shot (needed by Phase 3/4/6)
CALLER=$(aws sts get-caller-identity --region "$AWS_REGION" 2>&1)
echo "$CALLER"

if echo "$CALLER" | grep -qi "expired\|ExpiredToken\|InvalidToken"; then
    echo "❌ MFA session expired. Run:  dash-mfa  OR  bash ~/get_session_token.sh"
    echo "   Then re-verify with: aws sts get-caller-identity --region $AWS_REGION"
    exit 1
fi

export AWS_ACCOUNT_ID=$(echo "$CALLER" | jq -r '.Account' 2>/dev/null || aws sts get-caller-identity --query Account --output text --region "$AWS_REGION")
echo "✅ authenticated to account $AWS_ACCOUNT_ID"
```

### 0.2 gh CLI (auto-detect, fallback if missing)

```bash
# Try gh from current PATH first
if command -v gh >/dev/null 2>&1; then
    echo "✅ gh found at: $(command -v gh)"
else
    # Fallback: search common install paths
    for p in \
        "/c/Users/$USER/AppData/gh_cli/bin" \
        "/c/Users/$USER/AppData/Local/Programs/GitHub CLI" \
        "/c/Program Files/GitHub CLI" \
        "$HOME/.local/bin"; do
        if [[ -x "$p/gh" || -x "$p/gh.exe" ]]; then
            export PATH="$PATH:$p"
            echo "✅ gh added to PATH from: $p"
            break
        fi
    done
fi

gh --version || {
    echo "❌ gh CLI not found. Install from https://cli.github.com/ and re-run."
    exit 1
}
```

### 0.3 Detect platform (with sanity check)

```bash
REPO_NAME=$(basename "$(git remote get-url origin)" .git)
REPO="dashmote/$REPO_NAME"
BRANCH=$(git rev-parse --abbrev-ref HEAD)
ID_PLATFORM="$REPO_NAME"
ID_PLATFORM_LOWER=$(echo "$ID_PLATFORM" | tr '[:upper:]' '[:lower:]')

# Sanity check: platform codes are typically 2-5 uppercase letters (LMN, TKW, DLR, YDE, JSE…).
# A lowercase or hyphenated name likely means we're in the wrong repo.
if ! [[ "$ID_PLATFORM" =~ ^[A-Z]{2,5}$ ]]; then
    echo "⚠️  Detected platform '$ID_PLATFORM' does not match pattern ^[A-Z]{2,5}$."
    echo "    This may be: a fork (e.g. 'YDE-fork'), the wrong repo, or a non-ConSo project."
    echo "    STOP — confirm with user: 'Is this the right platform for run-detail? [y/N]'"
    # Proceed only after explicit user 'y'
fi

IMAGE_NAME="conso_${ID_PLATFORM_LOWER}_spider"
```

### 0.4 Resolve remaining AWS variables (dynamic — nothing hardcoded)

```bash
# ---- Constants (safe to hardcode — project-level, not account-level) ----
export CLUSTER="conso-cluster"
export TASK_FAMILY="conso-outlet-detail"
export SERVICE_NAME="conso-outlet-detail"      # Invariant I2: must match task family

# ---- Dynamic from AWS (change per account/VPC) ----
# Network config: read from the cluster's default CapacityProvider or tag lookup
# Subnet: look for a tag Name containing 'conso' or 'public' in the VPC the cluster lives in.
SUBNET_IDS=$(aws ec2 describe-subnets --region "$AWS_REGION" \
    --filters "Name=tag:Name,Values=*conso*,*sourcing*,*public*" \
    --query 'Subnets[].SubnetId' --output text | tr '\t' ',')

# Security group: look for one tagged for this cluster, or default to the cluster's
# own SG. If lookup fails, ask the user rather than guessing.
SG_IDS=$(aws ec2 describe-security-groups --region "$AWS_REGION" \
    --filters "Name=tag:Name,Values=*conso*,*fargate*" \
    --query 'SecurityGroups[].GroupId' --output text | tr '\t' ',')

if [[ -z "$SUBNET_IDS" || -z "$SG_IDS" ]]; then
    echo "⚠️  Could not auto-discover subnets/security groups via tags."
    echo "    Ask the user: 'What subnet IDs and SG IDs should the task use?'"
    echo "    Tip: 'aws ec2 describe-subnets' and 'aws ec2 describe-security-groups' listings help."
fi

# ---- Loki SSM instance: discover by tag, NOT hardcoded ID ----
LOKI_INSTANCE_ID=$(aws ec2 describe-instances --region "$AWS_REGION" \
    --filters "Name=tag:Name,Values=*loki*,*log*server*" "Name=instance-state-name,Values=running" \
    --query 'Reservations[].Instances[0].InstanceId' --output text 2>/dev/null | head -n1)

if [[ -z "$LOKI_INSTANCE_ID" || "$LOKI_INSTANCE_ID" == "None" ]]; then
    echo "⚠️  Loki instance not found via tag — using last-known ID as fallback."
    export LOKI_INSTANCE_ID="i-03bd3cb7dfd97a3f1"  # historical fallback; verify with user
fi
```

### 0.5 Resolved variable table — print before Phase 1

| Variable | Example | Used in |
|---|---|---|
| `ID_PLATFORM` | `DLR` | Phase 1 filter, overrides env, S3 path |
| `ID_PLATFORM_LOWER` | `dlr` | ECR repo, log group |
| `IMAGE_NAME` | `conso_dlr_spider` | Phase 3 freshness check, Phase 4 task-def |
| `REPO` | `dashmote/DLR` | Phase 3 CI monitoring |
| `BRANCH` | `feature/conso` | Phase 3 workflow dispatch |
| `AWS_ACCOUNT_ID` | (dynamic, e.g. `593453040104`) | Phase 4 image URI |
| `AWS_REGION` | `eu-central-1` | every `aws` call |
| `CLUSTER` | `conso-cluster` | Phase 1/5 task queries |
| `TASK_FAMILY` | `conso-outlet-detail` | Phase 4 task-def family |
| `SERVICE_NAME` | `conso-outlet-detail` | Phase 5 overrides, Phase 6 Loki label (I2) |
| `SUBNET_IDS` | (dynamic) | Phase 5 run-task network config |
| `SG_IDS` | (dynamic) | Phase 5 run-task network config |
| `LOKI_INSTANCE_ID` | (dynamic) | Phase 6.2 SSM target |

If any value here is `None`, `""`, or auto-discovered incorrectly — STOP, narrate
which variable, ask the user to confirm.

### 0.6 — MySQL Finder Data Preflight

> **Why this is here:** detail's `self.filter()` reads outlet IDs from MySQL.
> If the finder never wrote data (table empty/missing), or hasn't run recently
> (stale data = re-crawling outlets that no longer exist), detail will either
> exit immediately with 0 tasks or waste time on dead outlets. Catching this
> BEFORE launching a Fargate task saves ~$2-5 per wasted run and 15+ minutes
> of waiting for a task that was doomed from the start.

**This check is silent and automatic — do NOT ask the user before running it.**

```bash
# Auto-discover all prefixes for this platform, or check just the target if known
CHECK_PREFIXES="${PREFIX:-}"   # PREFIX may not be set yet — that's OK

MYSQL_STATUS=$(python ~/.claude/commands/conso-migrate/check_mysql.py \
    --platform "$ID_PLATFORM" \
    ${CHECK_PREFIXES:+--prefixes "$CHECK_PREFIXES"} \
    --json 2>/dev/null)
```

**Intelligent triage — Claude reasons about the data, not just displays it:**

For each prefix in the result, classify into one of these buckets:

| Condition | Classification | What Claude does |
|---|---|---|
| `exists=false` | ❌ **No finder data** | STOP before launch. Tell user: "No MySQL table for {prefix} — finder has never run for this country. Run finder first, or check if `DB = PLATFORM` is set in settings.py." |
| `count=0` | ❌ **Empty table** | Same as above — finder ran but wrote nothing (silent failure pattern). |
| `last_refresh` > 30 days ago | ⚠️ **Stale data** | Warn user: "Finder data for {prefix} is {N} days old (last: {date}). Detail will re-crawl outlets that may no longer exist. Consider running finder first." Let user decide — don't block. |
| `last_refresh` > 7 days ago | ℹ️ **Aging data** | Note it in the variable table but don't warn — 7 days is normal for monthly crawl cycles. |
| `last_refresh` within 7 days AND `count > 0` | ✅ **Fresh data** | Proceed silently — don't print anything extra. |

**Cross-referencing with Phase 1 (smart entry):**

If Phase 1 finds a RUNNING detail task for the same prefix, AND MySQL shows
the data is fresh — the previous task is probably fine; suggest monitoring it
instead of launching a duplicate (save money).

If Phase 1 finds a recently STOPPED task that failed, AND MySQL shows the data
is stale — the failure might be caused by stale finder data, not a spider bug.
Suggest re-running finder before re-launching detail.

**Multi-prefix awareness:**

When no specific prefix is given yet (user hasn't answered Q1), run the check
for ALL discovered prefixes. Use the result to:
1. Pre-fill the default prefix suggestion in Q1 — pick the prefix with the
   most rows AND fresh data (best chance of a successful detail run).
2. Flag any prefixes that would fail before the user picks them.
3. If ALL prefixes are ❌, tell the user upfront: "No usable finder data for
   any {ID_PLATFORM} country. Detail cannot run until finder writes data."

**Print the summary only if there's something worth saying:**

```
# Only if warnings/errors exist:
📊 MySQL Finder Status for {ID_PLATFORM}:
  ✅ NL: 42,687 rows (9h ago)
  ✅ DE: 131,150 rows (9h ago)
  ⚠️ AT: 15,773 rows (2d ago) — consider re-running finder
  ❌ PT: table does not exist — finder never ran

# If all green, just one line:
✅ MySQL finder data OK for {ID_PLATFORM} ({N} prefixes, all fresh)
```

---

## Phase 1 — Smart Entry Point

**Purpose:** Detect whether a task for this platform is already running or recently
finished. The user may come back hours later — the skill must not blindly launch
a duplicate task.

### 1.1 Search for existing tasks (all relevant desired-statuses)

```bash
# RUNNING covers: PROVISIONING, PENDING, ACTIVATING, RUNNING, DEACTIVATING, STOPPING
# (ECS groups all "not-yet-stopped" states under desired-status=RUNNING)
RUNNING=$(aws ecs list-tasks --cluster "$CLUSTER" \
    --family "$TASK_FAMILY" --desired-status RUNNING \
    --region "$AWS_REGION" --query 'taskArns[]' --output text 2>/dev/null)

# STOPPED (ECS only returns tasks stopped within the last ~1 hour)
STOPPED=$(aws ecs list-tasks --cluster "$CLUSTER" \
    --family "$TASK_FAMILY" --desired-status STOPPED \
    --region "$AWS_REGION" --query 'taskArns[]' --output text 2>/dev/null)

ALL_TASKS="$RUNNING $STOPPED"
```

If `ALL_TASKS` is empty → no tasks found, skip to Phase 2.

### 1.2 Filter for this platform's tasks (JSON Lines output — handles special chars)

Match by checking `Platform` in container environment overrides (Invariant I1 — NOT
by image name, because `describe-tasks` shows the task def's original image, not the
override). Emit **JSON Lines** (one JSON object per line) — never pipe-delimited;
the `cmd` field contains spaces, quotes, and potentially pipes.

```bash
aws ecs describe-tasks --cluster "$CLUSTER" --tasks $ALL_TASKS \
    --region "$AWS_REGION" --output json \
  | python3 -c "
import sys, json
platform = '${ID_PLATFORM}'
data = json.load(sys.stdin)
for t in data.get('tasks', []):
    for o in t.get('overrides', {}).get('containerOverrides', []):
        for e in o.get('environment', []):
            if e.get('name') == 'Platform' and e.get('value') == platform:
                exit_code = next(
                    (c.get('exitCode') for c in t.get('containers', []) if c.get('name') == 'ggm_app'),
                    None
                )
                print(json.dumps({
                    'task_id':   t['taskArn'].split('/')[-1],
                    'status':    t['lastStatus'],
                    'desired':   t.get('desiredStatus'),
                    'created':   str(t.get('createdAt', '')),
                    'stopped':   str(t.get('stoppedAt', '')),
                    'exit_code': exit_code,
                    'cmd':       o.get('command', []),
                    'prefix':    next((e['value'] for ov in t.get('overrides', {}).get('containerOverrides', [])
                                       for e in ov.get('environment', []) if e.get('name') == 'prefix'), None)
                }))
" > /tmp/run-detail-phase1.jsonl

# Pretty-print for the user (narrate) — jq handles JSON Lines natively
jq -s '.' /tmp/run-detail-phase1.jsonl
```

Each line is now safe to pipe into `jq` for querying specific fields without
worrying about `|`, spaces, or quotes in the command.

### 1.3 Decision

**Case A — RUNNING task found:**

```
Found RUNNING task for {ID_PLATFORM}:
  Task ID:    {task_id}
  Started:    {created}
  Running:    {elapsed}
  Command:    {cmd}

  1. Monitor this task (resume Phase 5.2)
  2. Launch a new task (continue to Phase 2)
```

Default 1. User picks → jump accordingly.

**Case B — STOPPED task found (within ~1 hour):**

```
Found recently completed task for {ID_PLATFORM}:
  Task ID:    {task_id}
  Stopped:    {stopped}
  Exit code:  {exit_code}
  Command:    {cmd}

  1. View results and logs (jump to Phase 6)
  2. Launch a new task (continue to Phase 2)
```

Default 1.

**Case C — No tasks found:**

```
No running or recent tasks found for {ID_PLATFORM}.
```

Continue to Phase 2.

> Note: ECS `list-tasks --desired-status STOPPED` only returns tasks stopped
> within the last ~1 hour. For older tasks, the user must provide the task ID
> manually, or check CloudWatch/Grafana.

---

## Phase 2 — Collect Parameters (smart defaults)

Auto-detected values from Phase 0.3 are already known. Before asking, **infer
smart defaults** from git / Phase 1 / local files — then the user can just say
"yes" to most questions.

### Smart default inference (do this ONCE before Q1)

```bash
# ---- Default prefix (Q1) ----
# Priority: (1) last Phase 1 task's prefix → (2) push_grids.py CONFIGS keys → (3) none
DEFAULT_PREFIX=""
if [[ -s /tmp/run-detail-phase1.jsonl ]]; then
    DEFAULT_PREFIX=$(jq -r '.prefix // empty' /tmp/run-detail-phase1.jsonl \
                     | grep -v '^$' | head -1)
fi
if [[ -z "$DEFAULT_PREFIX" && -f scripts/push_grids.py ]]; then
    DEFAULT_PREFIX=$(grep -oE "'[A-Z]{2}'" scripts/push_grids.py | head -1 | tr -d "'")
fi

# ---- Default output_month (Q2) ----
# Current UTC month — re-validated below to block future values
CURRENT_MONTH=$(date -u +%Y%m)
DEFAULT_MONTH="$CURRENT_MONTH"
```

Now ask the questions. **ALWAYS show what was inferred** so the user can override
with intent, not guess.

### Q1: prefix
```
prefix? (2-letter country code)
Inferred default: {DEFAULT_PREFIX}
Reason: {"reused from last task" | "first entry in push_grids.py CONFIGS" | "none found — REQUIRED"}
```

If `DEFAULT_PREFIX` is empty → **REQUIRED, no default** — ask explicitly; do not
silently use `BR` (historical default, very wrong for most platforms).

### Q2: output_month (with future-date guard)
```
output_month? (YYYYMM)
Default: {CURRENT_MONTH}
作用: 数据写入哪个月的 S3 路径和 Redis key。
      补过去的数据就填那个月，如 202603
```

**Validation — STOP if invalid:**
```bash
if ! [[ "$OUTPUT_MONTH" =~ ^[0-9]{6}$ ]]; then
    echo "❌ output_month must be YYYYMM (6 digits). Got: '$OUTPUT_MONTH'"; exit 1
fi
if (( OUTPUT_MONTH > CURRENT_MONTH )); then
    echo "❌ output_month '$OUTPUT_MONTH' is in the future (current: $CURRENT_MONTH)."
    echo "   STOP — future months have no data; this is almost certainly a typo."
    exit 1
fi
```

### Q3: recrawl
```
recrawl? (True / False)
Default: True
作用: True  = 增量 — 过滤掉该月已抓过的 outlet，只抓缺失的
      False = 全量 — 不过滤，重新抓所有 outlet
⚠️ 命名反直觉: recrawl=True 是"启用过滤"(增量)，不是"重新抓"
```

### Q4: meal_fix
```
meal_fix? (True / False)
Default: False
作用: True  = 跳过 outlet_information，只重新抓 meal 和 option
      False = 正常流程，抓全部数据类型
注意: meal_fix=True 时 recrawl 参数被忽略
```

**If `meal_fix == True`**, narrate back to user:
> "meal_fix=True — I will ignore the recrawl value you gave in Q3 and skip
> outlet_information. Confirm this is intentional."

### Q5: sample
```
sample? (数字, 0 = 生产全量)
Default: 0
作用: >0 进入测试模式，只抓 N 个 outlet，数据写入 dash-alpha-dev/sample/
⚠️ 生产补数据不要设 sample
```

### Q6: instances
```
几个实例? (1-5)
Default: 1
作用: 多实例共享 Redis 队列 (SPOP 原子操作，不会重复抓取)
⚠️ 上限 5。推荐 2-3，更多可能因代理 IP 和平台限流反而更慢
```

### Q7: (仅当 instances > 1) 多实例模式
```
多实例模式?
  A. 不同月份并行 — 每个实例跑不同月份，各自独立
     → 输入月份列表 (逗号分隔): 202603,202604
     → 覆盖 Q2 的 output_month，每个月份一个实例
  B. 同月份加速 — 多个 worker 共享同一个 Redis 队列
     → 使用 Q2 的 output_month，启动 N 个 worker
```

如果选 A，用户输入的月份列表覆盖 Q2 的值,实例数 = 月份数。每个月份都必须过 Q2 的
future-date 校验。
如果选 B，实例数 = Q6 的值。

### id_refresh — 自动设置，不问用户

| 场景 | id_refresh | 原因 |
|---|---|---|
| 单实例 | `True` | 从 MySQL 拉最新 ID |
| 多月份并行 — 每个 | `True` | 不同 Redis key，清空互不影响 |
| 同月份加速 — 第 1 个 | `True` | 填充 Redis 队列 |
| 同月份加速 — 第 2+ 个 | `False` | 共享队列，不能清空 |

### Launch plan confirmation

打印完整计划，等待用户确认：

```
=== Launch Plan ===
  Platform:     {ID_PLATFORM}
  Prefix:       {PREFIX}
  Total tasks:  {N} / 5 max
  Mode:         {"⚠️ PRODUCTION" if sample==0 else "TEST (→ dash-alpha-dev/sample/)"}

  Task 1: output_month=202603  recrawl=True  id_refresh=True   (primary)
  Task 2: output_month=202603  recrawl=True  id_refresh=False  (worker)

Proceed? (Y/n)
```

**PRODUCTION mode (sample=0) requires explicit user confirmation.**

---

## Phase 3 — Image Validation (pre-run check)

**Purpose:** Ensure the ECR image is up to date with the latest code before launching.
This is NOT deployment — it is a post-deployment verification step. If the image is
stale, this phase diagnoses why and fixes the issue so the Fargate task won't fail.

### 3.1 Check ECR image freshness AND capture IMAGE_DIGEST (Invariant I3)

```bash
# Get latest image's push time AND its SHA digest in one call
LATEST_IMG=$(aws ecr describe-images \
    --repository-name "${IMAGE_NAME}" \
    --region "$AWS_REGION" \
    --query 'sort_by(imageDetails, &imagePushedAt)[-1].{pushed: imagePushedAt, digest: imageDigest, tags: imageTags}' \
    --output json 2>/dev/null)

export IMAGE_TIME=$(echo "$LATEST_IMG"  | jq -r '.pushed')
export IMAGE_DIGEST=$(echo "$LATEST_IMG"| jq -r '.digest')
export IMAGE_TAGS=$(echo "$LATEST_IMG"  | jq -r '.tags | join(",")')

echo "📦 ECR image:"
echo "   pushed:  $IMAGE_TIME"
echo "   digest:  $IMAGE_DIGEST"
echo "   tags:    $IMAGE_TAGS"

COMMIT_TIME=$(git log -1 --format='%cI' HEAD)
echo "Last commit: $COMMIT_TIME"
```

Compare timestamps:

```bash
if [[ -z "$IMAGE_TIME" || "$IMAGE_TIME" == "null" ]]; then
    VERDICT="NO_IMAGE"
else
    VERDICT=$(python3 -c "
from datetime import datetime
img    = datetime.fromisoformat('${IMAGE_TIME}')
commit = datetime.fromisoformat('${COMMIT_TIME}')
print('CURRENT' if img >= commit else 'STALE')
")
fi
echo "Verdict: $VERDICT"
```

- **CURRENT** → skip to Phase 4 (`IMAGE_DIGEST` is pinned for Phase 4.1).
- **STALE** → continue to 3.2.
- **NO_IMAGE** → create ECR repo first:
  ```bash
  aws ecr create-repository --repository-name "${IMAGE_NAME}" --region "$AWS_REGION"
  ```
  Then trigger a build via Phase 3.3 → 3.4.

### 3.2 Diagnose stale image — inspect ECR.yml curl output

```bash
RUN_ID=$(gh run list --repo "$REPO" --workflow ECR.yml --limit 1 \
    --json databaseId --jq '.[0].databaseId')
gh run view "$RUN_ID" --repo "$REPO" --log 2>&1 | tail -30
```

Search the log for specific error patterns (grep, don't eyeball):

```bash
gh run view "$RUN_ID" --repo "$REPO" --log 2>&1 \
  | grep -E '"status":.*"404"|Bad credentials|HTTP/[0-9.]+ 2|HTTP/[0-9.]+ 4' \
  | head -5
```

| Log pattern | Meaning | Action |
|---|---|---|
| `"status": "404"` | GH_TOKEN expired / invalid | → 3.3 |
| `"Bad credentials"` | GH_TOKEN invalid | → 3.3 |
| `HTTP/... 204` (no `status` field) | Dispatch OK | → 3.4 |
| `HTTP/... 5xx` | GitHub API issue | wait & retry |

### 3.3 Fix GH_TOKEN and trigger rebuild

**⚠️ Warning — the command below uses YOUR personal PAT as the repo secret.**
This is a time bomb: if you rotate / revoke your token or leave the company, the
ECR.yml workflow for this repo (and every other repo set up the same way) will
silently fail on the next push. Narrate this to the user and get explicit OK
before running.

```bash
# Check who the token belongs to — narrate back to user
TOKEN_USER=$(gh api user -q .login 2>/dev/null)
echo "Current gh token belongs to: $TOKEN_USER"
echo "Setting this as GH_TOKEN in $REPO will tie the CI pipeline to that user's token."
echo "Recommended: use a dedicated service-account token (e.g. 'dashmote-ci')."
echo ""
echo "Proceed with personal token? [y/N]"
# Only proceed on explicit 'y'. Record $TOKEN_USER in the run summary so future
# debugging knows whose token is wired up.

gh secret set GH_TOKEN --repo "$REPO" --body "$(gh auth token)"
gh secret list --repo "$REPO"
gh workflow run ECR.yml --repo "$REPO" --ref "$BRANCH"
```

Wait ~10s for ECR.yml, re-inspect log to confirm no 404.

### 3.4 Wait for platform.yml build (explicit break + timeout, bug-fix)

The previous version of this loop had `# break on completed` as a comment but
**no actual `break` statement** — it ran forever. The version below is correct.

```bash
START=$(date +%s)
TIMEOUT=300   # 5 minutes

while true; do
    STATUS_LINE=$(gh run list --repo dashmote/dashmote-sourcing --workflow platform.yml \
        --limit 5 --json status,conclusion,displayTitle 2>&1 \
      | python3 -c "
import sys, json
for r in json.load(sys.stdin):
    if '${ID_PLATFORM}' in r['displayTitle']:
        print(f\"{r['status']}|{r.get('conclusion') or ''}\")
        break
else:
    print('NOT_FOUND|')
")

    STATUS="${STATUS_LINE%|*}"
    CONCL="${STATUS_LINE#*|}"
    ELAPSED=$(( $(date +%s) - START ))
    echo "[${ELAPSED}s] status=$STATUS concl=$CONCL"

    # ✅ BREAK on terminal state
    if [[ "$STATUS" == "completed" ]]; then
        if [[ "$CONCL" == "success" ]]; then
            echo "✅ platform.yml completed successfully"
            break
        else
            echo "❌ platform.yml completed with: $CONCL"
            BUILD_RUN=$(gh run list --repo dashmote/dashmote-sourcing --workflow platform.yml \
                --limit 1 --json databaseId -q '.[0].databaseId')
            gh run view "$BUILD_RUN" --repo dashmote/dashmote-sourcing --log-failed | tail -50
            exit 1
        fi
    fi

    # ✅ BREAK on timeout
    if (( ELAPSED > TIMEOUT )); then
        echo "❌ build timed out after ${TIMEOUT}s — platform.yml still $STATUS"
        echo "   Inspect: gh run list --repo dashmote/dashmote-sourcing --workflow platform.yml --limit 5"
        exit 1
    fi

    sleep 15
done
```

Re-check image and update `IMAGE_DIGEST` — Phase 4 must pin to THIS new digest:

```bash
LATEST_IMG=$(aws ecr describe-images --repository-name "${IMAGE_NAME}" --region "$AWS_REGION" \
    --query 'sort_by(imageDetails, &imagePushedAt)[-1].{pushed: imagePushedAt, digest: imageDigest, tags: imageTags}' \
    --output json)
export IMAGE_TIME=$(echo "$LATEST_IMG" | jq -r '.pushed')
export IMAGE_DIGEST=$(echo "$LATEST_IMG" | jq -r '.digest')
echo "✅ Updated IMAGE_DIGEST = $IMAGE_DIGEST"
```

---

## Phase 4 — Prepare Fargate Task Definition

**Why:** The shared task def `conso-outlet-detail` uses `ggm_app:latest`.
ECS `run-task` cannot override `image`. Must register a new revision — and we
pin the image by **digest**, not by `:latest` tag (Invariant I3).

### 4.1 Build image URI (digest-pinned, account-dynamic)

The image URI uses `AWS_ACCOUNT_ID` from Phase 0 and `IMAGE_DIGEST` from
Phase 3.1 — nothing hardcoded.

```bash
# Digest-pinned URI — immutable, race-proof
export IMAGE_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${IMAGE_NAME}@${IMAGE_DIGEST}"

# Sanity check — did Phase 3 actually populate IMAGE_DIGEST?
if [[ -z "$IMAGE_DIGEST" || "$IMAGE_DIGEST" == "null" ]]; then
    echo "❌ IMAGE_DIGEST is empty — Phase 3 did not run correctly. STOP."
    exit 1
fi

echo "🔗 Will pin task-def to: $IMAGE_URI"
```

### 4.2 Fetch shared task-def, patch image, register new revision

```bash
TMP_TD="/tmp/task_def_${ID_PLATFORM_LOWER}.json"

aws ecs describe-task-definition --task-definition "$TASK_FAMILY" \
    --region "$AWS_REGION" --output json \
  | python3 -c "
import sys, json, os
td = json.load(sys.stdin)['taskDefinition']
for c in td['containerDefinitions']:
    if c['name'] == 'ggm_app':
        c['image'] = '${IMAGE_URI}'   # digest-pinned (I3)
keep = ['family','taskRoleArn','executionRoleArn','networkMode','containerDefinitions',
        'volumes','placementConstraints','requiresCompatibilities','cpu','memory','runtimePlatform']
with open('${TMP_TD}', 'w') as f:
    json.dump({k: td[k] for k in keep if k in td}, f)
"

TASK_DEF_ARN=$(aws ecs register-task-definition \
    --cli-input-json "file://$TMP_TD" \
    --region "$AWS_REGION" \
    --query 'taskDefinition.taskDefinitionArn' --output text)
echo "✅ Registered: $TASK_DEF_ARN"

# Leave-no-trace: remove the temp file (we can re-fetch the revision via describe if needed)
rm -f "$TMP_TD"
```

---

## Phase 4.5 — Pre-launch Gate (STOP-style, all 6 must pass)

Before `run-task` fires real Fargate containers writing to production MySQL/S3,
verify every assumption from the earlier phases still holds. Any FAIL = STOP.
Do NOT enter Phase 5 on "probably OK" — the failures here are invisible until
data doesn't appear in QA.

### G1 — Image digest pinned (Invariant I3)

```bash
if [[ -z "$IMAGE_DIGEST" || "$IMAGE_DIGEST" == "null" ]]; then
    echo "❌ G1 FAIL: IMAGE_DIGEST not set — Phase 3.1 did not run correctly"
    exit 1
fi
# The task-def we just registered must contain this digest, not :latest
REGISTERED_IMG=$(aws ecs describe-task-definition --task-definition "$TASK_DEF_ARN" \
    --region "$AWS_REGION" --query 'taskDefinition.containerDefinitions[?name==`ggm_app`].image | [0]' --output text)
if [[ "$REGISTERED_IMG" != *"$IMAGE_DIGEST"* ]]; then
    echo "❌ G1 FAIL: task-def image ($REGISTERED_IMG) does not contain verified digest"
    exit 1
fi
echo "✅ G1 image digest pinned in task-def"
```

### G2 — Task-def revision is fresh (just registered, not a stale ARN from earlier session)

```bash
REV_DATE=$(aws ecs describe-task-definition --task-definition "$TASK_DEF_ARN" \
    --region "$AWS_REGION" --query 'taskDefinition.registeredAt' --output text)
AGE_MIN=$(python3 -c "from datetime import datetime,timezone; print(int((datetime.now(timezone.utc) - datetime.fromisoformat('$REV_DATE')).total_seconds() / 60))")
if (( AGE_MIN > 15 )); then
    echo "❌ G2 FAIL: task-def $TASK_DEF_ARN was registered ${AGE_MIN}m ago — re-run Phase 4"
    exit 1
fi
echo "✅ G2 task-def fresh (${AGE_MIN}m old)"
```

### G3 — Platform name passes regex

```bash
[[ "$ID_PLATFORM" =~ ^[A-Z]{2,5}$ ]] && echo "✅ G3 platform name valid: $ID_PLATFORM" \
                                     || { echo "❌ G3 FAIL: '$ID_PLATFORM'"; exit 1; }
```

### G4 — AWS MFA still valid (Phase 0 was N minutes ago)

```bash
ACTUAL_ACCT=$(aws sts get-caller-identity --query Account --output text \
    --region "$AWS_REGION" 2>/dev/null)
[[ "$ACTUAL_ACCT" == "$AWS_ACCOUNT_ID" ]] \
    && echo "✅ G4 MFA valid (account $AWS_ACCOUNT_ID)" \
    || { echo "❌ G4 FAIL: got '$ACTUAL_ACCT', expected '$AWS_ACCOUNT_ID' — re-MFA"; exit 1; }
```

### G5 — Production mode (sample=0) requires DOUBLE confirmation

```bash
if [[ "${SAMPLE:-0}" == "0" ]]; then
    echo ""
    echo "⚠️  About to run in PRODUCTION mode (sample=0):"
    echo "     Writes to prod S3: s3://dash-sourcing/$ID_PLATFORM_LOWER/$OUTPUT_MONTH/"
    echo "     Writes to prod MySQL"
    echo "     Tasks: $INSTANCE_COUNT (running for hours)"
    echo ""
    echo "Type 'PROD' (case-sensitive) to confirm, anything else aborts:"
    # Wait for user input; only proceed if exact string "PROD"
fi
echo "✅ G5 production confirmation obtained"
```

### G6 — Fargate quota headroom

```bash
# Current running tasks in this cluster
RUNNING_NOW=$(aws ecs list-tasks --cluster "$CLUSTER" --desired-status RUNNING \
    --region "$AWS_REGION" --query 'length(taskArns[])' --output text 2>/dev/null)

# Fargate On-Demand vCPU quota (service code: fargate, quota code: L-3032A538)
# Gracefully skip if service-quotas API not available in region/account
QUOTA=$(aws service-quotas get-service-quota \
    --service-code fargate --quota-code L-3032A538 \
    --region "$AWS_REGION" --query 'Quota.Value' --output text 2>/dev/null || echo "")

if [[ -n "$QUOTA" && "$QUOTA" != "None" ]]; then
    # Each detail task uses ~0.5 vCPU (512 CPU). We want to add INSTANCE_COUNT.
    PROJECTED=$(python3 -c "print(int(${RUNNING_NOW:-0}) * 0.5 + ${INSTANCE_COUNT:-1} * 0.5)")
    echo "   Projected vCPU usage after launch: $PROJECTED / $QUOTA"
    if python3 -c "import sys; sys.exit(0 if ${PROJECTED:-0} < ${QUOTA:-1} * 0.9 else 1)"; then
        echo "✅ G6 Fargate quota headroom OK"
    else
        echo "⚠️  G6 WARN: projected usage >90% of quota. Confirm with user before proceeding."
    fi
else
    echo "✅ G6 skipped (quota API unavailable)"
fi
```

### Gate summary — print before entering Phase 5

```
G1 image digest pinned           [✅ / ❌]
G2 task-def fresh                [✅ / ❌]
G3 platform name valid           [✅ / ❌]
G4 AWS MFA valid                 [✅ / ❌]
G5 sample=0 double-confirmed     [✅ / ❌]
G6 Fargate quota headroom        [✅ / ⚠️  / skipped]
```

All must be ✅ (G6 warn is OK if user confirms). On any ❌ — **STOP, narrate
which gate, do not run-task**.

---

## Phase 5 — Launch & Monitor

### 5.1 Launch primary task (jq-built overrides — no placeholders, zero escape hell)

Previous versions of this skill had `COMMAND_ARRAY` / `COMMAND_ARRAY_WITH_ID_REFRESH_FALSE`
placeholders inside inline JSON. Those were **pseudocode** — Claude had to hand-substitute
and escape, which is a major silent-failure source. The version below uses `jq` to build
the overrides and writes to a file, then passes `file://` to run-task.

```bash
# Helper function: build overrides JSON for a given id_refresh value
# Args: $1 = id_refresh ("True" or "False"), $2 = output file path
build_overrides() {
    local id_refresh="$1"
    local out="$2"

    # Build command array — include optional args only when set
    local cmd_jq='["scrapy","crawl","conso_outlet_detail",
                   "-a", ("prefix=" + $prefix),
                   "-a", ("output_month=" + $month),
                   "-a", ("recrawl=" + $recrawl),
                   "-a", ("id_refresh=" + $id_refresh)]'

    # Append meal_fix if True
    if [[ "$MEAL_FIX" == "True" ]]; then
        cmd_jq="$cmd_jq + [\"-a\", \"meal_fix=True\"]"
    fi
    # Append sample if > 0
    if [[ "${SAMPLE:-0}" -gt 0 ]]; then
        cmd_jq="$cmd_jq + [\"-a\", (\"sample=\" + (\$sample|tostring))]"
    fi

    jq -n \
        --arg prefix     "$PREFIX" \
        --arg month      "$OUTPUT_MONTH" \
        --arg recrawl    "$RECRAWL" \
        --arg id_refresh "$id_refresh" \
        --arg platform   "$ID_PLATFORM" \
        --arg service    "$SERVICE_NAME" \
        --argjson sample "${SAMPLE:-0}" \
        "{
           containerOverrides: [{
             name: \"ggm_app\",
             command: $cmd_jq,
             environment: [
               {name: \"Platform\",     value: \$platform},
               {name: \"SERVICE_NAME\", value: \$service}
             ]
           }]
         }" > "$out"
}

# Build primary-task overrides (id_refresh=True)
OV_FILE="/tmp/run-detail-overrides-1.json"
build_overrides "True" "$OV_FILE"

# Build network config — uses SUBNET_IDS / SG_IDS from Phase 0 (dynamic)
NET_CFG="awsvpcConfiguration={subnets=[${SUBNET_IDS}],securityGroups=[${SG_IDS}],assignPublicIp=ENABLED}"

# Launch
TASK_ARN=$(aws ecs run-task \
    --cluster "$CLUSTER" \
    --task-definition "$TASK_DEF_ARN" \
    --launch-type FARGATE \
    --network-configuration "$NET_CFG" \
    --overrides "file://$OV_FILE" \
    --region "$AWS_REGION" \
    --query 'tasks[0].taskArn' --output text)

echo "Task 1 launched: $TASK_ARN"
TASK_IDS=( "$(echo $TASK_ARN | awk -F/ '{print $NF}')" )
rm -f "$OV_FILE"
```

### 5.1b Launch workers (if multi-instance same-month)

Workers use `id_refresh=False` (Invariant I6 — only primary populates Redis).

```bash
# Wait for primary to be RUNNING before launching workers (Redis queue must be populated)
TASK1_ID="${TASK_IDS[0]}"
echo "Waiting for primary task to reach RUNNING (max 5 min)..."
for i in {1..30}; do
    S=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK1_ID" \
        --region "$AWS_REGION" --query 'tasks[0].lastStatus' --output text)
    echo "[$i/30] Task 1: $S"
    [[ "$S" == "RUNNING" ]] && break
    [[ "$S" == "STOPPED" ]] && { echo "❌ primary stopped before running — abort workers"; exit 1; }
    sleep 10
done
echo "Waiting 15s for Redis queue population..."
sleep 15

# Launch workers
for i in $(seq 2 "$INSTANCE_COUNT"); do
    OV_FILE="/tmp/run-detail-overrides-${i}.json"
    build_overrides "False" "$OV_FILE"

    W_ARN=$(aws ecs run-task \
        --cluster "$CLUSTER" \
        --task-definition "$TASK_DEF_ARN" \
        --launch-type FARGATE \
        --network-configuration "$NET_CFG" \
        --overrides "file://$OV_FILE" \
        --region "$AWS_REGION" \
        --query 'tasks[0].taskArn' --output text)

    echo "Task $i launched: $W_ARN"
    TASK_IDS+=( "$(echo $W_ARN | awk -F/ '{print $NF}')" )
    rm -f "$OV_FILE"
done
```

For **multi-month parallel**: launch each month as an independent primary task
(all `id_refresh=True`), no wait needed between launches. Pass a different
`OUTPUT_MONTH` to `build_overrides` per task.

### 5.2 Monitor ALL tasks

Poll every 30s. Track status of each task. Stop when all are STOPPED or timeout:

```bash
START=$(date +%s)
TIMEOUT=600   # default 10 min, adjust based on user request

while true; do
    ALL_DONE=true
    ELAPSED=$(( $(date +%s) - START ))

    for TID in "${TASK_IDS[@]}"; do
        STATUS=$(aws ecs describe-tasks --cluster conso-cluster --tasks "$TID" \
            --region eu-central-1 --query 'tasks[0].lastStatus' --output text)
        echo "[$(date +%H:%M:%S)] Task ${TID:0:8}… — $STATUS (${ELAPSED}s)"
        [ "$STATUS" != "STOPPED" ] && ALL_DONE=false
    done

    $ALL_DONE && break

    if [ $ELAPSED -gt $TIMEOUT ]; then
        echo ""
        echo "Polling timeout (${TIMEOUT}s). Some tasks still running."
        echo "Run /run-detail again later — Phase 1 will find them."
        break
    fi

    echo ""
    sleep 30
done
```

### 5.3 Collect exit status for each task

```bash
for TID in "${TASK_IDS[@]}"; do
    echo "=== Task $TID ==="
    aws ecs describe-tasks --cluster conso-cluster --tasks "$TID" \
        --region eu-central-1 --output json | \
    python -c "
import sys, json
t = json.load(sys.stdin)['tasks'][0]
print(f\"  Status:  {t['lastStatus']}\")
print(f\"  Stop:    {t.get('stopCode', 'N/A')} — {t.get('stoppedReason', 'N/A')}\")
for c in t.get('containers', []):
    ec = c.get('exitCode', 'N/A')
    ok = 'OK' if ec == 0 else 'FAILED'
    print(f\"  {c['name']:20s} exitCode={ec} [{ok}]\")
"
done
```

### 5.3b OOM Auto-Recovery (exitCode=137)

**When any task exits with code 137**, the container was killed by OOM.
Do NOT ask the user for a memory value — estimate it automatically and relaunch.

**Step 1 — Read current memory from the task definition:**

```bash
aws ecs describe-task-definition --task-definition "$TASK_DEF_ARN" \
    --region eu-central-1 --query 'taskDefinition.{cpu: cpu, memory: memory}' --output text
```

**Step 2 — Double the memory:**

| Current memory | New memory | CPU change needed? |
|---|---|---|
| 1024 MB | 2048 MB | No (512 CPU supports up to 4096) |
| 2048 MB | 4096 MB | No (512 CPU supports up to 4096) |
| 4096 MB | 8192 MB | Yes → CPU must be 1024 |
| 8192 MB | 16384 MB | Yes → CPU must be 2048 |

Fargate 合法的 CPU/memory 组合:
- 512 CPU: 1024–4096 MB
- 1024 CPU: 2048–8192 MB
- 2048 CPU: 4096–16384 MB
- 4096 CPU: 8192–30720 MB

**Step 3 — Register new task def and relaunch:**

```bash
# Modify memory (and cpu if needed) in task def JSON
aws ecs describe-task-definition --task-definition "$TASK_DEF_ARN" \
    --region eu-central-1 --output json | \
python -c "
import sys, json, os, tempfile
td = json.load(sys.stdin)['taskDefinition']
old_mem = int(td['memory'])
new_mem = old_mem * 2
# Adjust CPU if new memory exceeds current CPU tier
cpu = int(td['cpu'])
if new_mem > 4096 and cpu < 1024: cpu = 1024
if new_mem > 8192 and cpu < 2048: cpu = 2048
if new_mem > 16384 and cpu < 4096: cpu = 4096
td['memory'] = str(new_mem)
td['cpu'] = str(cpu)
keep = ['family','taskRoleArn','executionRoleArn','networkMode','containerDefinitions',
        'volumes','placementConstraints','requiresCompatibilities','cpu','memory','runtimePlatform']
tmp = os.path.join(tempfile.gettempdir(), 'task_def_oom_fix.json')
with open(tmp, 'w') as f:
    json.dump({k: td[k] for k in keep if k in td}, f)
print(f'OOM recovery: {old_mem} MB → {new_mem} MB (CPU: {cpu})')
print(tmp)
"
```

Register and relaunch with the same parameters (return to Phase 5.1).

告知用户: `OOM @ {old_mem} MB → 自动扩容到 {new_mem} MB 重新启动`

**上限**: 如果已经 16384 MB 还 OOM，停止自动恢复，报告给用户排查内存泄漏。

---

### 5.4 Stop Tasks (user-initiated abort)

**用途:** 用户在运行途中发现问题（代码需要修改、参数错误等），需要立即终止。

当用户要求停止/终止/abort 时，停掉该平台所有正在运行的任务:

```bash
for TID in "${TASK_IDS[@]}"; do
    aws ecs stop-task --cluster conso-cluster --task "$TID" \
        --reason "User-initiated abort" \
        --region eu-central-1 --query 'task.lastStatus' --output text
    echo "Stopped: $TID"
done
```

如果没有 TASK_IDS 变量（例如用户在新会话中要求停止），先走 Phase 1.1–1.2
找到该平台的 RUNNING 任务，再执行 stop。

---

## Phase 6 — Log Analysis

Entry point for: Phase 5 completion, or Phase 1 Case B (viewing old results).

### 6.1 Calculate time window

```bash
aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK_ID" \
    --region "$AWS_REGION" --output json \
  | python3 -c "
import sys, json
from datetime import datetime, timezone
t = json.load(sys.stdin)['tasks'][0]
created = t.get('createdAt', '')
stopped = t.get('stoppedAt', '')
print(f'Created: {created}')
print(f'Stopped: {stopped}')
if created:
    now = datetime.now(timezone.utc)
    dt = datetime.fromisoformat(str(created))
    minutes_ago = int((now - dt).total_seconds() / 60) + 5
    print(f'QUERY_MINUTES={minutes_ago}')
" | tee /tmp/run-detail-time.txt

export QUERY_MINUTES=$(grep -oP '(?<=QUERY_MINUTES=)\d+' /tmp/run-detail-time.txt)
```

### 6.2 Fetch spider logs from Loki (file-based SSM — no escape hell)

Spider logs go through fluent-bit → **Loki** (NOT CloudWatch). Query via SSM on
a bastion EC2. The original version of this step had an 8-layer-escaped inline
shell command that was a maintenance nightmare. Below we:

1. **Write the query script to a local file** (clean Python, no escaping).
2. **Base64-encode** it into a single safe string.
3. **SSM-send** a minimal `bash -c '<b64>|base64 -d|python3'` invocation.
4. **Poll** `get-command-invocation` until the status is terminal (no magic `sleep 8`).

```bash
# Step 1 — write the Loki query script cleanly to disk
cat > /tmp/run-detail-loki-query.py << 'PYEOF'
import json, os, sys, subprocess
platform = os.environ['ID_PLATFORM']
minutes  = int(os.environ['QUERY_MINUTES'])
import time
now = int(time.time())
start = now - minutes * 60

curl = subprocess.run(
    ["curl", "-s", "http://localhost:3100/loki/api/v1/query_range",
     "--data-urlencode", 'query={service="conso-outlet-detail"}',
     "--data-urlencode", f"start={start}",
     "--data-urlencode", f"end={now}",
     "--data-urlencode", "limit=500"],
    capture_output=True, text=True, check=False
)
data = json.loads(curl.stdout or "{}")
lines = [v[1] for r in data.get('data', {}).get('result', []) for v in r.get('values', [])]

# Keep lines mentioning our platform OR generic-important spider events
keywords = (platform, 'item_scraped', 'ERROR', 'WARNING', 'Spider opened',
            'Spider closed', 'Dumping', 'finish_reason', 'S3Pipeline',
            'RDSPipeline', 'Inserted', 'Updated')
for line in lines:
    if any(k in line for k in keywords):
        print(line)
PYEOF

# Step 2 — base64-encode (single safe string, survives nested shell escaping)
SCRIPT_B64=$(base64 -w0 /tmp/run-detail-loki-query.py 2>/dev/null \
           || base64 /tmp/run-detail-loki-query.py | tr -d '\n')

# Step 3 — SSM send-command, single-line bash with env vars injected
SSM_CMD="export ID_PLATFORM='${ID_PLATFORM}' QUERY_MINUTES='${QUERY_MINUTES}'; echo ${SCRIPT_B64} | base64 -d | python3"
CMD_ID=$(aws ssm send-command \
    --instance-ids "$LOKI_INSTANCE_ID" \
    --document-name "AWS-RunShellScript" \
    --parameters "commands=[\"$SSM_CMD\"]" \
    --region "$AWS_REGION" \
    --query 'Command.CommandId' --output text)

echo "SSM command: $CMD_ID (against $LOKI_INSTANCE_ID)"

# Step 4 — poll until terminal (NOT a magic sleep)
for i in {1..20}; do
    STATUS=$(aws ssm get-command-invocation \
        --command-id "$CMD_ID" --instance-id "$LOKI_INSTANCE_ID" \
        --region "$AWS_REGION" --query 'Status' --output text 2>/dev/null)
    echo "[$i/20] SSM status: $STATUS"
    case "$STATUS" in
        Success)  break ;;
        Failed|Cancelled|TimedOut)  echo "❌ SSM failed: $STATUS"; break ;;
        *) sleep 3 ;;
    esac
done

# Retrieve output
aws ssm get-command-invocation \
    --command-id "$CMD_ID" --instance-id "$LOKI_INSTANCE_ID" \
    --region "$AWS_REGION" \
    --query 'StandardOutputContent' --output text > /tmp/run-detail-loki.log

echo "Retrieved $(wc -l < /tmp/run-detail-loki.log) log lines"
tail -100 /tmp/run-detail-loki.log

# Leave-no-trace
rm -f /tmp/run-detail-loki-query.py
```

> **Note:** Loki returns logs from ALL concurrent `conso-outlet-detail` runs.
> The script filters by `$ID_PLATFORM`, but shared log lines (Scrapy core,
> dashmote_sourcing) from other platforms may still appear. For per-task
> isolation, Fluent Bit would need a `platform` label added to the log stream.

> **Windows Git Bash caveat:** `aws logs` commands mangle paths starting with
> `/`. Use `MSYS_NO_PATHCONV=1`:
> ```bash
> MSYS_NO_PATHCONV=1 aws logs describe-log-streams \
>     --log-group-name "/ecs/$TASK_FAMILY" --region "$AWS_REGION" ...
> ```

### 6.3 Analyze and report

**On success (exitCode=0):**

```
=== Detail Spider Run Complete ===
  Platform:      {ID_PLATFORM}
  Prefix:        {PREFIX}
  Task ID:       {TASK_ID}
  Duration:      {elapsed}s
  Exit code:     0
  Items scraped: {count}
  Image:         {IMAGE_NAME}:latest ({image_date})

=== S3 Output ===
  outlet_information:  {n} records
  outlet_meal:         {n} records
  meal_option:         {n} records
  option_relation:     {n} records
```

Extract counts from log lines matching `S3Pipeline > Successfully uploaded`.

**On failure:** Analyze logs, provide root cause + fix suggestion + relevant log lines.

### Common failures

| Error | Cause | Fix |
|---|---|---|
| `exitCode=137` / `OutOfMemoryError` | Container exceeded memory limit | Auto-recover (Phase 5.3b) |
| `'bool' has no attribute 'get'` on `filter()` | Stale ECR image | Rebuild (Phase 3) |
| `UnboundLocalError: retry_times` | Old dashmote_sourcing base | Rebuild image |
| `ExpiredTokenException` | Container AWS creds | Check task role |
| `ConnectionRefusedError` on Redis | Fargate can't reach Redis | Check security group |
| `FileNotFoundError` QA config | Missing Google Sheets | Contact Q&A team |
| `IgnoreRequest` flood | Platform blocking | Check BrightData proxy |
| `CLOSESPIDER_ERRORCOUNT` | API format changed | Update spider code |
| `ModuleNotFoundError` | Missing dependency | Add to pyproject.toml, rebuild |
| `"status": "404"` in ECR.yml | GH_TOKEN expired | Phase 3 step 3.3 |

---

## Phase 7 — Post-run Data Verification (independent, don't trust the logs alone)

**Why this phase exists — the YDE/LMN pattern.** Phase 6 reports "items scraped"
from spider logs. But the log says *the spider ran*, not *the data landed*.
Historical incidents (YDE/LMN 2026-04-13) had `Crawled 20000 pages` in logs
while **0 rows** reached MySQL, for 13 days straight, because `DB = PLATFORM`
was missing from settings.py.

This phase goes straight to **S3 and MySQL** and counts what actually arrived.
If log count and store count diverge, narrate the gap loudly.

### 7.1 — Count files in S3 (primary destination)

```bash
# Confirm bucket + path convention on first run; store as constant after.
#   Production: s3://dash-sourcing/<ID_PLATFORM_LOWER>/<OUTPUT_MONTH>/<PREFIX>/
#   Test (sample>0): s3://dash-alpha-dev/sample/<ID_PLATFORM_LOWER>/<OUTPUT_MONTH>/<PREFIX>/
if [[ "${SAMPLE:-0}" -gt 0 ]]; then
    S3_BUCKET="dash-alpha-dev"
    S3_PREFIX="sample/${ID_PLATFORM_LOWER}/${OUTPUT_MONTH}/${PREFIX}/"
else
    S3_BUCKET="dash-sourcing"   # confirm on first run
    S3_PREFIX="${ID_PLATFORM_LOWER}/${OUTPUT_MONTH}/${PREFIX}/"
fi

echo "🪣 Listing s3://${S3_BUCKET}/${S3_PREFIX}"
S3_FILES=$(aws s3 ls "s3://${S3_BUCKET}/${S3_PREFIX}" --recursive \
    --region "$AWS_REGION" 2>/dev/null)

# Count files modified today (last 24h)
TODAY=$(date -u +%Y-%m-%d)
S3_TODAY=$(echo "$S3_FILES" | awk -v d="$TODAY" '$1 == d' | wc -l)
S3_TOTAL=$(echo "$S3_FILES" | wc -l)

echo "S3 files total:        $S3_TOTAL"
echo "S3 files created today: $S3_TODAY"

if (( S3_TODAY == 0 )); then
    echo "❌ ZERO files landed in S3 today. Do NOT trust the log count."
    echo "   Check: S3Pipeline in spiders/*.py, bucket permissions on task role."
fi
```

### 7.2 — Cross-check vs log-reported scraped count

```bash
# Extract scraped/uploaded counts from the Loki log we fetched in Phase 6.2
LOG_SCRAPED=$(grep -oE "'item_scraped_count': [0-9]+" /tmp/run-detail-loki.log \
            | awk -F': ' '{print $2}' | sort -n | tail -1)
LOG_S3_UPLOADS=$(grep -c "S3Pipeline.*Successfully uploaded" /tmp/run-detail-loki.log 2>/dev/null || echo 0)
LOG_RDS_ROWS=$(grep -oE "RDSPipeline.*(Inserted|Updated) [0-9]+" /tmp/run-detail-loki.log \
            | grep -oE "[0-9]+" | paste -sd+ | bc 2>/dev/null || echo 0)

echo ""
echo "📊 Cross-check:"
echo "   Log says scraped:        ${LOG_SCRAPED:-0} items"
echo "   Log says S3 uploads:     ${LOG_S3_UPLOADS:-0} batches"
echo "   Log says MySQL rows:     ${LOG_RDS_ROWS:-0}"
echo "   S3 files actually there: $S3_TODAY"
```

### 7.3 — (Optional) MySQL row count

Gated on whether DB credentials are available in the current environment.
Requires `mysql` CLI and `DB_HOST/DB_USER/DB_PASS` env vars (same creds used
during migration). Skip if not set.

```bash
if command -v mysql >/dev/null 2>&1 \
   && [[ -n "${DB_HOST:-}" && -n "${DB_USER:-}" && -n "${DB_PASS:-}" ]]; then

    MYSQL_ROWS=$(mysql -h "$DB_HOST" -u "$DB_USER" -p"$DB_PASS" \
        -N -e "SELECT COUNT(*) FROM ${ID_PLATFORM}.outlet_information \
               WHERE DATE(created_at) = '$TODAY' \
                 AND prefix = '$PREFIX';" 2>/dev/null || echo "UNAVAILABLE")
    echo "   MySQL rows today:        $MYSQL_ROWS"
else
    echo "   MySQL check skipped (no creds)"
    MYSQL_ROWS="SKIPPED"
fi
```

### 7.4 — Verdict table

```
=== Data Verification ===
  Platform:            {ID_PLATFORM} / {PREFIX} / {OUTPUT_MONTH}
  Log scraped count:   {LOG_SCRAPED}
  Log S3 upload count: {LOG_S3_UPLOADS} batches
  Log MySQL rows:      {LOG_RDS_ROWS}
  S3 files today:      {S3_TODAY}
  MySQL rows today:    {MYSQL_ROWS}

  Verdict: {✅ DATA LANDED  /  ⚠️ PARTIAL  /  ❌ NO DATA — INVESTIGATE}
```

**Verdict rules:**
- `✅ DATA LANDED` — `S3_TODAY > 0` AND (MySQL skipped OR `MYSQL_ROWS > 0`)
  AND `abs(LOG_SCRAPED - expected) < 20%`
- `⚠️ PARTIAL` — some data landed but significant gap between log and store
  (>20% discrepancy between `LOG_SCRAPED` and `S3_TODAY`/`MYSQL_ROWS`)
- `❌ NO DATA` — `S3_TODAY == 0` AND (MySQL skipped OR `MYSQL_ROWS == 0`)
  **even if the task exited 0** — YDE/LMN pattern; STOP, narrate loudly, ask
  user whether the spider's pipeline config is correct before Phase 8.

---

## Phase 8 — Next Steps (chain into sibling skills)

Only proceed here if Phase 7 verdict was `✅ DATA LANDED`. If `⚠️ PARTIAL` or
`❌ NO DATA`, loop back to diagnosis instead.

Offer the user the logical next actions as explicit slash-command invocations.
The user can copy/paste — don't run them silently.

```
✅ Detail run complete, data verified in S3.

Next steps (pick one):

  1. Trigger QA for this run
     → /trigger-qa $ID_PLATFORM $PREFIX $OUTPUT_MONTH

  2. Re-run for a different prefix / month
     → /run-detail (back to Phase 2)

  3. Refresh specific outlet IDs that failed (targeted re-crawl)
     → /id-refresh $ID_PLATFORM $PREFIX $OUTPUT_MONTH
     (requires a CSV of id_outlet values)

  4. Activate CASS (if this platform isn't live yet)
     → see conso-migrate Phase 6 → cass_insert.py

  5. Done — nothing more to do

Choose 1-5:
```

### Optional: prune old task-def revisions for this platform

This skill registers a new task-def revision every run. Over time hundreds
accumulate in ECS. After a successful Phase 7, offer:

```bash
# List this platform's revisions older than 30 days
CUTOFF=$(date -u -d '30 days ago' +%Y-%m-%d 2>/dev/null || date -u -v-30d +%Y-%m-%d)

aws ecs list-task-definitions --family-prefix "$TASK_FAMILY" --status ACTIVE \
    --region "$AWS_REGION" --query 'taskDefinitionArns' --output json \
  | jq -r '.[]' | tail -n +11 > /tmp/run-detail-old-tds.txt

echo "Found $(wc -l < /tmp/run-detail-old-tds.txt) old revisions."
echo "Deregister them? (keeps 10 most recent) [y/N]"
# On user 'y':
# while read -r arn; do
#     aws ecs deregister-task-definition --task-definition "$arn" --region "$AWS_REGION"
# done < /tmp/run-detail-old-tds.txt
rm -f /tmp/run-detail-old-tds.txt
```

Keep at least the 10 most recent revisions for rollback.

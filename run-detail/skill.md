---
name: run-detail
description: "Trigger ad-hoc detail spider runs on Fargate with image freshness check, multi-instance support, and log analysis. Detects running/completed tasks automatically. Use when the user says: run detail, trigger detail, start crawl, supplement crawl, re-run detail, check detail status, check pipeline, verify build, 跑detail, 启动detail, 补数据."
---

# ConSo Detail — Ad-hoc Fargate Run & Monitoring

## Overview

This skill **launches detail spider runs on Fargate** with pre-run image validation,
multi-instance support, and log analysis. It is designed for supplementary data crawling
— when the monthly automated run missed data, or you need to re-crawl specific
prefixes outside the regular schedule.

**Smart entry point:** The skill first checks whether a task for this platform is
already running or recently completed. The user may invoke this skill hours after
launching a task — it must pick up where things stand, not blindly start a new run.

**Workflow:**

```
Phase 0: Preflight
    ├── AWS credentials valid?
    ├── gh CLI available?
    └── Detect platform from git

Phase 1: Smart Entry Point
    ├── Search conso-cluster for this platform's tasks
    │   ├── RUNNING found  → ask: monitor or launch new?
    │   ├── STOPPED found  → ask: view results or launch new?
    │   └── None found     → continue to Phase 2
    └── If monitoring/viewing → jump to Phase 5.2 or 6

Phase 2: Collect Parameters (ask one by one)
    Q1 prefix → Q2 output_month → Q3 recrawl → Q4 meal_fix
    → Q5 sample → Q6 instances → Q7 multi-mode (if >1)
    → Print launch plan → User confirms

Phase 3: Image Validation (pre-run check)
    ├── 3.1 Compare ECR image time vs latest commit
    ├── 3.2 If stale → diagnose cause (ECR.yml curl 404?)
    ├── 3.3 If fixable → fix GH_TOKEN → trigger rebuild → wait
    └── 3.4 Confirm image is current before launching

Phase 4: Prepare Fargate Task Definition
    ├── 4.1 Fetch shared task def, replace image
    └── 4.2 Register new revision

Phase 5: Launch & Monitor
    ├── 5.1 Launch primary task
    ├── 5.1b Launch worker tasks (if multi-instance)
    ├── 5.2 Poll ALL task statuses every 30s
    ├── 5.3 Collect exit codes when STOPPED
    │   └── exitCode=137 (OOM) → Phase 5.3b auto-recover
    └── 5.4 Stop tasks (user-initiated abort)

Phase 6: Log Analysis
    ├── 6.1 Calculate time window
    ├── 6.2 Query Loki via SSM
    └── 6.3 Analyze and report
```

---

## Phase 0 — Preflight

**Do these first.** Phase 1 needs AWS credentials to search for tasks.

### 0.1 AWS credentials

```bash
aws sts get-caller-identity --region eu-central-1
```

If `ExpiredToken` → tell user to run `dash-mfa` or `bash ~/get_session_token.sh`.
Wait for confirmation, re-verify.

### 0.2 gh CLI

```bash
export PATH="$PATH:/c/Users/admin/AppData/gh_cli/bin"
gh --version
```

This `export` must be in every new bash invocation throughout this skill.

### 0.3 Detect platform

```bash
REPO_NAME=$(basename $(git remote get-url origin) .git)
REPO="dashmote/$REPO_NAME"
BRANCH=$(git rev-parse --abbrev-ref HEAD)
ID_PLATFORM="$REPO_NAME"
ID_PLATFORM_LOWER=$(echo "$ID_PLATFORM" | tr '[:upper:]' '[:lower:]')
IMAGE_NAME="conso_${ID_PLATFORM_LOWER}_spider"
```

---

## Phase 1 — Smart Entry Point

**Purpose:** Detect whether a task for this platform is already running or recently
finished. The user may come back hours later — the skill must not blindly launch
a duplicate task.

### 1.1 Search for existing tasks

```bash
# RUNNING tasks
RUNNING=$(aws ecs list-tasks --cluster conso-cluster \
    --family conso-outlet-detail --desired-status RUNNING \
    --region eu-central-1 --query 'taskArns[]' --output text 2>/dev/null)

# STOPPED tasks (ECS only returns tasks stopped within the last ~1 hour)
STOPPED=$(aws ecs list-tasks --cluster conso-cluster \
    --family conso-outlet-detail --desired-status STOPPED \
    --region eu-central-1 --query 'taskArns[]' --output text 2>/dev/null)

ALL_TASKS="$RUNNING $STOPPED"
```

If `ALL_TASKS` is empty → no tasks found, skip to Phase 2.

### 1.2 Filter for this platform's tasks

Match by checking `Platform` in container environment overrides (NOT by image name,
because `describe-tasks` shows the task definition's original image, not the override):

```bash
# Batch describe all tasks in one call (faster than one-by-one)
aws ecs describe-tasks --cluster conso-cluster --tasks $ALL_TASKS \
    --region eu-central-1 --output json | python -c "
import sys, json

platform = '${ID_PLATFORM}'
data = json.load(sys.stdin)
for t in data.get('tasks', []):
        # Check Platform in environment overrides
    for o in t.get('overrides', {}).get('containerOverrides', []):
        for e in o.get('environment', []):
            if e.get('name') == 'Platform' and e.get('value') == platform:
                task_id = t['taskArn'].split('/')[-1]
                status = t['lastStatus']
                created = t.get('createdAt', '')
                stopped = t.get('stoppedAt', '')
                cmd = ' '.join(o.get('command', []))
                exit_code = 'N/A'
                for c in t.get('containers', []):
                    if c.get('name') == 'ggm_app':
                        exit_code = c.get('exitCode', 'N/A')
                print(f'{task_id}|{status}|{created}|{stopped}|{exit_code}|{cmd}')
"
```

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

## Phase 2 — Collect Parameters

Auto-detected values from Phase 0.3 are already known. Now ask the user for
run-specific parameters, one at a time.

### Q1: prefix
```
prefix? (2-letter country code, e.g. BR)
Default: BR
```

### Q2: output_month
```
output_month? (YYYYMM, cannot be future)
Default: current month ({YYYYMM})
作用: 数据写入哪个月的 S3 路径和 Redis key。
      补过去的数据就填那个月，如 202603
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

如果选 A，用户输入的月份列表覆盖 Q2 的值，实例数 = 月份数。
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

### 3.1 Check ECR image freshness

```bash
# ECR image push time
IMAGE_TIME=$(aws ecr describe-images \
    --repository-name "${IMAGE_NAME}" \
    --region eu-central-1 \
    --query 'sort_by(imageDetails, &imagePushedAt)[-1].imagePushedAt' \
    --output text 2>/dev/null)
echo "ECR image: $IMAGE_TIME"

# Latest commit time
COMMIT_TIME=$(git log -1 --format='%cI' HEAD)
echo "Last commit: $COMMIT_TIME"
```

Compare timestamps:
```bash
python -c "
from datetime import datetime, timezone
img = '${IMAGE_TIME}'
commit = '${COMMIT_TIME}'
if img == 'None' or not img:
    print('NO_IMAGE')
else:
    t_img = datetime.fromisoformat(img)
    t_commit = datetime.fromisoformat(commit)
    if t_img >= t_commit:
        print(f'CURRENT — image ({img}) >= commit ({commit})')
    else:
        print(f'STALE — image ({img}) < commit ({commit})')
"
```

- **CURRENT** → skip to Phase 4.
- **STALE** → continue to 3.2.
- **Image not found** → create ECR repo first:
  ```bash
  aws ecr create-repository --repository-name "${IMAGE_NAME}" --region eu-central-1
  ```

### 3.2 Diagnose stale image — inspect ECR.yml curl output

```bash
RUN_ID=$(gh run list --repo ${REPO} --workflow ECR.yml --limit 1 \
    --json databaseId --jq '.[0].databaseId')
gh run view $RUN_ID --repo ${REPO} --log 2>&1 | tail -15
```

| Output | Meaning | Action |
|---|---|---|
| `"status": "404"` | GH_TOKEN expired | → 3.3 |
| `"Bad credentials"` | GH_TOKEN invalid | → 3.3 |
| `100  79  0  0` (0 received) | Dispatch OK (HTTP 204) | → 3.4 |

### 3.3 Fix GH_TOKEN and trigger rebuild

```bash
gh secret set GH_TOKEN --repo ${REPO} --body "$(gh auth token)"
gh secret list --repo ${REPO}
gh workflow run ECR.yml --repo ${REPO} --ref ${BRANCH}
```

Wait ~10s for ECR.yml, re-inspect log to confirm no 404.

### 3.4 Wait for platform.yml build

Poll every 15s, timeout 5 min:

```bash
START=$(date +%s)
while true; do
    gh run list --repo dashmote/dashmote-sourcing --workflow platform.yml --limit 5 \
        --json status,conclusion,displayTitle 2>&1 | \
    python -c "
import sys, json
for r in json.load(sys.stdin):
    if '${ID_PLATFORM}' in r['displayTitle']:
        print(f\"{r['status']} {r.get('conclusion') or ''}\")
        break
else:
    print('NOT_FOUND')
"
    ELAPSED=$(( $(date +%s) - START ))
    echo "(${ELAPSED}s)"
    # break on completed, timeout on 300s
    sleep 15
done
```

Verify image updated:
```bash
aws ecr describe-images --repository-name "${IMAGE_NAME}" --region eu-central-1 \
    --query 'sort_by(imageDetails, &imagePushedAt)[-1].{pushed: imagePushedAt, tags: imageTags}' \
    --output table
```

---

## Phase 4 — Prepare Fargate Task Definition

**Why:** The shared task def `conso-outlet-detail` uses `ggm_app:latest`.
ECS `run-task` cannot override `image`. Must register a new revision.

### 4.1 Replace image and register

```bash
aws ecs describe-task-definition --task-definition conso-outlet-detail \
    --region eu-central-1 --output json | \
python -c "
import sys, json, os, tempfile
td = json.load(sys.stdin)['taskDefinition']
for c in td['containerDefinitions']:
    if c['name'] == 'ggm_app':
        c['image'] = '593453040104.dkr.ecr.eu-central-1.amazonaws.com/${IMAGE_NAME}:latest'
keep = ['family','taskRoleArn','executionRoleArn','networkMode','containerDefinitions',
        'volumes','placementConstraints','requiresCompatibilities','cpu','memory','runtimePlatform']
tmp = os.path.join(tempfile.gettempdir(), 'task_def_${ID_PLATFORM_LOWER}.json')
with open(tmp, 'w') as f:
    json.dump({k: td[k] for k in keep if k in td}, f)
print(tmp, flush=True)
"
```

```bash
TMP_FILE=<output from above>
TASK_DEF_ARN=$(aws ecs register-task-definition \
    --cli-input-json "file://$TMP_FILE" \
    --region eu-central-1 \
    --query 'taskDefinition.taskDefinitionArn' --output text)
echo "Registered: $TASK_DEF_ARN"
rm -f "$TMP_FILE"
```

---

## Phase 5 — Launch & Monitor

### 5.1 Launch primary task

Build command array from parameters:
```json
["scrapy", "crawl", "conso_outlet_detail",
 "-a", "prefix={PREFIX}",
 "-a", "output_month={OUTPUT_MONTH}",
 "-a", "recrawl={RECRAWL}",
 "-a", "id_refresh=True"]
```

Append optional: `"-a", "meal_fix=True"` / `"-a", "sample=N"` if set.

```bash
TASK_ARN=$(aws ecs run-task \
    --cluster conso-cluster \
    --task-definition "$TASK_DEF_ARN" \
    --launch-type FARGATE \
    --network-configuration 'awsvpcConfiguration={subnets=[subnet-31a7e658],securityGroups=[sg-fab89293],assignPublicIp=ENABLED}' \
    --overrides '{"containerOverrides":[{"name":"ggm_app","command":[COMMAND_ARRAY],"environment":[{"name":"Platform","value":"'${ID_PLATFORM}'"},{"name":"SERVICE_NAME","value":"conso-outlet-detail"}]}]}' \
    --region eu-central-1 \
    --query 'tasks[0].taskArn' --output text)
echo "Task 1 launched: $TASK_ARN"
TASK_IDS=( "$(echo $TASK_ARN | awk -F/ '{print $NF}')" )
```

### 5.1b Launch workers (if multi-instance same-month)

```bash
# Wait for primary to be RUNNING
TASK1_ID="${TASK_IDS[0]}"
while true; do
    S=$(aws ecs describe-tasks --cluster conso-cluster --tasks "$TASK1_ID" \
        --region eu-central-1 --query 'tasks[0].lastStatus' --output text)
    echo "Task 1: $S"
    [ "$S" = "RUNNING" ] && break
    sleep 10
done
echo "Waiting 15s for Redis queue population..."
sleep 15

# Launch workers with id_refresh=False (same command but swap id_refresh)
for i in $(seq 2 $INSTANCE_COUNT); do
    W_ARN=$(aws ecs run-task \
        --cluster conso-cluster \
        --task-definition "$TASK_DEF_ARN" \
        --launch-type FARGATE \
        --network-configuration 'awsvpcConfiguration={subnets=[subnet-31a7e658],securityGroups=[sg-fab89293],assignPublicIp=ENABLED}' \
        --overrides '{"containerOverrides":[{"name":"ggm_app","command":[COMMAND_ARRAY_WITH_ID_REFRESH_FALSE],"environment":[{"name":"Platform","value":"'${ID_PLATFORM}'"},{"name":"SERVICE_NAME","value":"conso-outlet-detail"}]}]}' \
        --region eu-central-1 \
        --query 'tasks[0].taskArn' --output text)
    echo "Task $i launched: $W_ARN"
    TASK_IDS+=( "$(echo $W_ARN | awk -F/ '{print $NF}')" )
done
```

For **multi-month parallel**: launch each month as an independent primary task
(all `id_refresh=True`), no wait needed between launches.

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
aws ecs describe-tasks --cluster conso-cluster --tasks "$TASK_ID" \
    --region eu-central-1 --output json | \
python -c "
import sys, json
from datetime import datetime, timezone
t = json.load(sys.stdin)['tasks'][0]
created = t.get('createdAt', '')
stopped = t.get('stoppedAt', '')
print(f'Created: {created}')
print(f'Stopped: {stopped}')
if created:
    now = datetime.now(timezone.utc)
    # AWS returns format like '2026-04-09T14:05:29+00:00'
    dt = datetime.fromisoformat(str(created))
    minutes_ago = int((now - dt).total_seconds() / 60) + 5
    print(f'Query window: {minutes_ago} minutes ago')
"
```

### 6.2 Fetch spider logs from Loki

Spider logs go through fluent-bit → **Loki** (NOT CloudWatch).
Query via SSM on EC2:

```bash
CMD_ID=$(aws ssm send-command \
    --instance-ids "i-03bd3cb7dfd97a3f1" \
    --document-name "AWS-RunShellScript" \
    --parameters 'commands=["curl -s http://localhost:3100/loki/api/v1/query_range --data-urlencode \"query={service=\\\"conso-outlet-detail\\\"}\" --data-urlencode \"start='$(date -u -d '${MINUTES} minutes ago' +%s)'\" --data-urlencode \"limit=500\" 2>&1 | python3 -c \"import sys,json; data=json.load(sys.stdin); results=data.get(\\\"data\\\",{}).get(\\\"result\\\",[]); lines=[v[1] for r in results for v in r.get(\\\"values\\\",[])] ; [print(l) for l in lines if \\\"${ID_PLATFORM}\\\" in l or \\\"item_scraped\\\" in l or \\\"ERROR\\\" in l or \\\"Spider opened\\\" in l or \\\"Spider closed\\\" in l or \\\"Dumping\\\" in l or \\\"finish_reason\\\" in l or \\\"S3Pipeline\\\" in l or \\\"WARNING\\\" in l]\" | tail -100"]' \
    --region eu-central-1 \
    --query 'Command.CommandId' --output text)
```

Retrieve:
```bash
sleep 8
aws ssm get-command-invocation \
    --command-id "$CMD_ID" \
    --instance-id "i-03bd3cb7dfd97a3f1" \
    --region eu-central-1 \
    --query 'StandardOutputContent' --output text
```

> **Note:** Loki returns logs from ALL concurrent `conso-outlet-detail` runs.
> The query filters for `{ID_PLATFORM}` but shared log lines (Scrapy core,
> dashmote_sourcing) from other platforms may still appear.

> **Windows Git Bash:** paths starting with `/` get mangled. Use `MSYS_NO_PATHCONV=1`
> for any `aws logs` commands:
> ```bash
> MSYS_NO_PATHCONV=1 aws logs describe-log-streams \
>     --log-group-name "/ecs/conso-outlet-detail" --region eu-central-1 ...
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

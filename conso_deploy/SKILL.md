---
name: conso-deploy
description: "ConSo Deployment & Scheduling Assistant. Deploy a ConSo spider project to production and configure monthly automated crawling. Covers ECR, GitHub Actions, CloudWatch, EventBridge scheduling, and CASS activation. Use when the user says: deploy, deploy to production, set up scheduling, create EventBridge rule, 部署, 上线."
---

# ConSo Deployment & Scheduling Assistant

Deploy a ConSo spider project to production and configure monthly automated crawling.
Reference projects: TKW, JSE, DLR at `C:\Users\lsd\projects\`.

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

### 4.2 Monitor CI/CD
The push triggers `ECR.yml` → `dashmote-sourcing/platform.yml`.

```bash
# Watch local ECR.yml (fast, ~10s — just dispatches)
gh run list --repo {github_org}/{ID_PLATFORM} --limit 1

# Watch platform.yml (actual build, ~3-5 min)
gh run list --repo dashmote/dashmote-sourcing --workflow platform.yml --limit 1
```

Wait for `platform.yml` to show `completed / success`.

### 4.3 Verify ECR image
```bash
aws ecr describe-images --repository-name conso_{id_platform_lower}_spider --region eu-central-1 \
  --query 'sort_by(imageDetails, &imagePushedAt)[-1].{pushed: imagePushedAt, tags: imageTags}'
```

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
AWS EventBridge Rule: conso-{ID_PLATFORM}-{PREFIX}-detail
  ↓  (cron trigger, e.g. monthly 1st at 00:00 UTC)
Lambda: dash-sourcing-spider-scheduler
  ↓  (receives JSON input with container/cmd/env config)
ECS Fargate Task: conso_{id_platform_lower}_spider
  ↓
scrapy runspider conso_outlet_detail.py -a prefix={PREFIX}
```

**There are 4 AWS operations to create a monthly schedule. Run them for EACH prefix.**

#### Step 1 — Create EventBridge Rule (DISABLED first)

Create the rule with a cron expression. Start as DISABLED for safety — enable after
verifying the target is correctly bound.

```bash
MSYS_NO_PATHCONV=1 aws events put-rule \
  --name "conso-{ID_PLATFORM}-{PREFIX}-detail" \
  --schedule-expression "cron(0 0 1 * ? *)" \
  --state DISABLED \
  --region eu-central-1
```

Common cron patterns (AWS 6-field: `min hour day month week year`):
- Monthly 1st:  `cron(0 0 1 * ? *)`
- Monthly 7th:  `cron(0 0 7 * ? *)`
- Monthly 14th: `cron(0 0 14 * ? *)`
- Monthly 21st: `cron(0 0 21 * ? *)`
- Quarterly:    `cron(0 0 1 1,4,7,10 ? *)`

**Important:** AWS cron `week` and `day` cannot both be specified — unused one must be `?`.

#### Step 2 — Bind Lambda Target

Tell the rule WHAT to trigger. The Lambda receives a JSON payload containing the
ECS container name, command, and environment variables.

```bash
MSYS_NO_PATHCONV=1 aws events put-targets \
  --rule "conso-{ID_PLATFORM}-{PREFIX}-detail" \
  --region eu-central-1 \
  --targets '[{
    "Id": "string",
    "Arn": "arn:aws:lambda:eu-central-1:593453040104:function:dash-sourcing-spider-scheduler",
    "Input": "{\"container_name\":\"conso_{id_platform_lower}_spider\",\"cmd\":[\"python3\",\"-m\",\"scrapy\",\"runspider\",\"conso_outlet_detail.py\",\"-a\",\"prefix={PREFIX}\"],\"prefix\":\"{PREFIX}\",\"task_name\":\"conso_{id_platform_lower}_spider\",\"env\":[{\"name\":\"LOG_STREAM\",\"value\":\"{PREFIX}\"},{\"name\":\"Platform\",\"value\":\"{ID_PLATFORM}\"},{\"name\":\"Time\",\"value\":\"{YYYYMMDD_today}\"}]}"
  }]'
```

**Fields in the Input JSON:**
- `container_name`: must match the container name in the ECS task definition
- `cmd`: the scrapy command that runs inside the container
- `prefix`: country code passed to the spider
- `task_name`: ECS task family name (same as container_name for ConSo)
- `env`: environment variables injected into the container at runtime
  - `LOG_STREAM`: used by Fluent Bit for log routing (set to prefix)
  - `Platform`: used by Fluent Bit labels
  - `Time`: timestamp label (set once at rule creation, cosmetic only)

#### Step 3 — Grant EventBridge permission to invoke Lambda

Without this, EventBridge cannot call the Lambda function.

```bash
MSYS_NO_PATHCONV=1 aws lambda add-permission \
  --function-name dash-sourcing-spider-scheduler \
  --statement-id "conso-{ID_PLATFORM}-{PREFIX}-detail" \
  --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn "arn:aws:events:eu-central-1:593453040104:rule/conso-{ID_PLATFORM}-{PREFIX}-detail" \
  --region eu-central-1
```

If the permission already exists (re-running), you will get `ResourceConflictException` — safe to ignore.

#### Step 4 — Enable the rule (DISABLED → ENABLED)

After verifying Steps 1-3 are correct:
```bash
MSYS_NO_PATHCONV=1 aws events enable-rule \
  --name "conso-{ID_PLATFORM}-{PREFIX}-detail" \
  --region eu-central-1
```

#### Step 5 — Verify the complete setup
```bash
# Check rule exists and is ENABLED
MSYS_NO_PATHCONV=1 aws events list-rules \
  --name-prefix "conso-{ID_PLATFORM}" --region eu-central-1 \
  --query 'Rules[].{Name:Name, Schedule:ScheduleExpression, State:State}'

# Check target is bound correctly
MSYS_NO_PATHCONV=1 aws events list-targets-by-rule \
  --rule "conso-{ID_PLATFORM}-{PREFIX}-detail" --region eu-central-1
```

Confirm:
- Rule state = `ENABLED`
- Target ARN = `arn:aws:lambda:...:dash-sourcing-spider-scheduler`
- Target Input contains correct `container_name`, `cmd`, `prefix`

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

## Phase 6 — Verification

### 6.1 CASS activation
Ensure `finder_is_active` and `detail_is_active` are `True`:
```bash
poetry run python ~/.claude/commands/conso-migrate/cass_insert.py \
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

---
name: id-refresh
description: "Re-crawl specific outlet IDs for a sourcing spider. Pushes IDs from a CSV file to Redis, runs the detail spider locally, verifies S3 output, and triggers QA. Use when the user says: refresh IDs, re-crawl IDs, push IDs and run spider, id refresh for <platform> <country> <month>."
---

# ID Refresh

Use this skill when the user wants to re-crawl a specific set of outlet IDs by pushing them to Redis and running the detail spider locally. This is useful for data fixes, targeted re-crawls, or adding missing outlets without running a full crawl.

## Required Inputs

Do not start until these are known:

- `id_platform` — e.g. `TKW`, `EPL`, `LMN`
- `country` — 2-letter country code, e.g. `NL`, `DE`, `US`
- `output_month` — in `YYYYMM` format, e.g. `202603`
- `csv_path` — path to a CSV file with an `id_outlet` column
- `engineer_name` — engineer name for QA trigger (e.g. `Cam`, `Morri`). If not known, ask the user before Step 5.

If any required value is missing, ask the user before proceeding.

## Optional Inputs

- `meal_fix` — `True` or `False` (default `False`). When `True`, skips `outlet_information` and only re-crawls meals/options.
- `spider_project_dir` — path to the spider project directory. If not provided, infer from context or ask the user.

## Defaults

Unless the user says otherwise, use:

- `meal_fix = False`
- `s3_bucket = dash-alpha-dev`
- `s3_prefix = sourcing/{id_platform}/{output_month}/`

## Workflow

### Step 1 — Ensure IDs Exist in MySQL

#### 1a. Check for missing IDs

```bash
python3 C:/Users/admin/.claude/skills/id-refresh/scripts/id_refresh.py check-mysql \
  --platform TKW \
  --country NL \
  --csv-path /path/to/id.csv
```

- If all IDs exist: proceed to Step 2.
- If missing IDs are found: report the list to the user and ask whether they want to insert them into MySQL.

#### 1b. Insert missing IDs (only if user confirms)

```bash
python3 C:/Users/admin/.claude/skills/id-refresh/scripts/id_refresh.py insert-mysql \
  --platform TKW \
  --country NL \
  --csv-path /path/to/id.csv
```

This inserts only `id_outlet` — other metadata columns (`unique_name`, `cuisine`, etc.) are set to NULL. `created_at` and `last_refresh` are auto-populated by MySQL.

**Important**: After inserting, check whether the detail spider requires additional metadata (e.g. `unique_name` for building request URLs). If the spider cannot function without this metadata and there is no API to fetch it by ID alone, inform the user:

> The missing IDs have been inserted into MySQL, but the detail spider requires metadata (e.g. `unique_name`) that cannot be automatically retrieved for these IDs. Options:
> 1. Run the finder spider to discover and populate metadata for these outlets.
> 2. Manually provide the missing metadata.
> Without this metadata, the detail spider will skip these IDs.

### Step 2 — Push IDs to Redis

Run the bundled script to push IDs:

```bash
python3 C:/Users/admin/.claude/skills/id-refresh/scripts/id_refresh.py push \
  --platform TKW \
  --country NL \
  --output-month 202603 \
  --csv-path /path/to/id.csv
```

Report: number of IDs pushed and the Redis key used.

### Step 3 — Run the Detail Spider

Run the spider from the spider project directory:

```bash
cd <spider_project_dir>
scrapy crawl conso_outlet_detail \
  -a prefix=<country> \
  -a output_month=<output_month> \
  -a id_refresh=False \
  -a recrawl=False \
  -a local_test=True
```

Key parameters explained:
- `id_refresh=False` — do NOT let the spider overwrite our manually pushed Redis queue
- `recrawl=False` — do NOT filter out outlets already crawled this month
- `local_test=True` — use test Redis (must match the Redis instance where IDs were pushed)
- Add `-a meal_fix=True` only if the user requested meal_fix mode

**Important**: Run the spider in the foreground so you can monitor progress and report the final stats.

### Step 4 — Verify S3 Output

Run the bundled script to check S3, filtered by today's `id_job` to only show data from this crawl run:

```bash
python3 C:/Users/admin/.claude/skills/id-refresh/scripts/id_refresh.py verify \
  --platform TKW \
  --country NL \
  --output-month 202603
```

To check a specific date's output:

```bash
python3 C:/Users/admin/.claude/skills/id-refresh/scripts/id_refresh.py verify \
  --platform TKW \
  --country NL \
  --output-month 202603 \
  --id-job 20260401
```

Report: for each table (`outlet_information`, `outlet_meal`, `meal_option`, `option_relation`), show file count and total size for the current `id_job` date.

### Step 5 — Trigger QA (Required)

After the spider finishes and S3 output is verified, **always** trigger the QA pipeline. Ask the user for their engineer name if not already known.

```bash
python3 C:/Users/admin/.claude/skills/trigger-qa/scripts/trigger_qa_pipeline.py \
  --platform <id_platform> \
  --country <country> \
  --refresh <output_month> \
  --engineer-name <name>
```

`C:/Users/admin/.claude/skills/trigger-qa` is the directory containing the trigger-qa skill (e.g. `C:/Users/admin/.claude/skills/trigger-qa` or the repo path `<repo_root>/trigger-qa-pipeline-aws`).

Report: Lambda invocation status, EMR cluster id/state, and console link.

## Script Usage

The bundled script `id_refresh.py` supports four subcommands:

### `check-mysql` — Check if CSV IDs exist in MySQL

```bash
python3 C:/Users/admin/.claude/skills/id-refresh/scripts/id_refresh.py check-mysql \
  --platform TKW \
  --country NL \
  --csv-path /path/to/id.csv
```

### `insert-mysql` — Insert missing IDs into MySQL

```bash
python3 C:/Users/admin/.claude/skills/id-refresh/scripts/id_refresh.py insert-mysql \
  --platform TKW \
  --country NL \
  --csv-path /path/to/id.csv
```

Only `id_outlet` is inserted. Other fields are NULL. `created_at`/`last_refresh` auto-filled by MySQL.

### `push` — Push IDs to Redis

```bash
python3 C:/Users/admin/.claude/skills/id-refresh/scripts/id_refresh.py push \
  --platform TKW \
  --country NL \
  --output-month 202603 \
  --csv-path /path/to/id.csv
```

### `verify` — Check S3 output after crawl

```bash
python3 C:/Users/admin/.claude/skills/id-refresh/scripts/id_refresh.py verify \
  --platform TKW \
  --country NL \
  --output-month 202603 \
  --id-job 20260402
```

`--id-job` defaults to today's date (YYYYMMDD). Only files under the matching `id_job=` partition are counted.

## Error Handling

- **Redis connection fails**: Check VPN/network access. Test Redis does not require SSL.
- **Spider import error**: Ensure `dashmote_sourcing` and all its dependencies are installed in the active Python environment.
- **S3 upload fails with pyarrow error**: Check `pyarrow` and `pandas` version compatibility. The spider project's `pyproject.toml` specifies the required versions.
- **`dynamic_menu FAILED: Task got bad yield`**: This is a known Scrapy 2.14 + `deferToThread` compatibility warning. It only affects `rating` and `out_of_stock` fields — core meal/option data is unaffected.
- **`No metadata found for outlet X, skipping`**: The outlet ID exists in MySQL but has NULL metadata (e.g. `unique_name`). The detail spider cannot build request URLs without it. Run the finder spider or manually populate the metadata.

## Notes

- The Redis key format is `{platform}:{country}:{output_month}:outlet_feeds` (e.g. `TKW:NL:202603:outlet_feeds`).
- Redis credentials are stored in AWS Secrets Manager: `db/redis/test`.
- The spider must be run with `-a local_test=True` to connect to the same test Redis instance.
- The spider reads metadata from MySQL to get `unique_name` (slug) for each outlet. If an ID is in Redis but not in MySQL, it will be silently skipped. Always verify IDs exist in MySQL first.
- S3 output path: `s3://dash-alpha-dev/sourcing/{platform}/{output_month}/{country}_{table}/`
- For large ID sets (200+), expect ~15-20 minutes of crawl time due to per-domain rate limiting.

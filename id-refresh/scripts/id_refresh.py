#!/usr/bin/env python3
"""Push outlet IDs to Redis and verify S3 output for targeted re-crawls.

Supports two modes:
  * local   — test Redis (db/redis/test) + bucket dash-alpha-dev (path `sourcing/{PLATFORM}/...`).
              Run spider locally with `-a local_test=True`. Small-batch / debugging.
  * fargate — prod Redis (db/redis/prod) + bucket dash-alpha-dev (path `sourcing/{PLATFORM}/...`,
              same as monthly cron). Spider runs inside Fargate container
              (launched via run-detail's Phase 3-5). Large batches, direct-to-prod data fixes.

Both modes share the same S3 bucket (`dash-alpha-dev`); they differ only in which
Redis instance is addressed. The bucket `dash-sourcing` does NOT exist — legacy
name from pre-unification docs, Incident A12 (2026-04-17).

All subcommands accept `--mode local|fargate`. Mode determines which secret the
Redis client fetches and which S3 bucket verify reads.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import List, Set

import boto3
import redis


# ---------------------------------------------------------------------------
# Output streams — narrate to stderr, JSON result to stdout
# ---------------------------------------------------------------------------

def _log(msg: str) -> None:
    """Narration / progress messages — go to stderr so stdout stays pure JSON
    that Claude (or any caller) can pipe through `jq` without interference.
    """
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# ID normalization — handle Excel zero-padding / case / whitespace drift
# ---------------------------------------------------------------------------

def normalize_id(raw: str, lenient: bool = False) -> str:
    """Canonicalize an id_outlet for cross-source comparison.

    Default (safe): strip whitespace + cast str.
    Lenient (opt-in): ALSO lstrip leading zeros + lowercase — catches the
    common "Excel zero-padded the CSV" trap where CSV has '00123' and MySQL
    has '123'. Disabled by default because some platforms legitimately use
    IDs where leading zeros or case matter.
    """
    s = str(raw).strip()
    if lenient:
        s = s.lstrip("0") or "0"   # avoid turning "0" into ""
        s = s.lower()
    return s


def normalize_ids(raw: List[str], lenient: bool = False) -> List[str]:
    return [normalize_id(x, lenient=lenient) for x in raw]


# ---------------------------------------------------------------------------
# Constants & mode configuration
# ---------------------------------------------------------------------------

SECRETS_REGION = "us-east-1"
TABLES = ["outlet_information", "outlet_meal", "meal_option", "option_relation"]

# Per-mode endpoints — any change here must stay consistent with SKILL.md
# Cross-phase Invariants table.
MODE_CONFIG = {
    "local": {
        "redis_secret": "db/redis/test",
        "s3_bucket":    "dash-alpha-dev",
        "s3_prefix":    "sourcing/{platform}/{output_month}/",
        "redis_ssl":    False,
    },
    "fargate": {
        "redis_secret": "db/redis/prod",
        "s3_bucket":    "dash-alpha-dev",
        "s3_prefix":    "sourcing/{platform}/{output_month}/",
        "redis_ssl":    True,
    },
}

# Country codes are MySQL table names → strict allowlist-by-regex to block SQL
# injection through `FROM {country}` interpolation (we cannot parameterize
# identifiers in MySQL, only values).
COUNTRY_RE = re.compile(r"^[A-Z]{2,3}$")

# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def validate_country(country: str) -> str:
    """Reject anything that isn't a 2-3 letter uppercase code."""
    if not COUNTRY_RE.match(country):
        raise SystemExit(
            f"Invalid country code {country!r}. Must match ^[A-Z]{{2,3}}$ (e.g. NL, DE, US). "
            f"This is a SQL-injection guard — the country becomes a MySQL table name."
        )
    return country


def validate_output_month(ym: str) -> str:
    """YYYYMM format; must not be in the future."""
    if not re.match(r"^[0-9]{6}$", ym):
        raise SystemExit(f"Invalid output_month {ym!r}. Expected YYYYMM (6 digits).")
    from datetime import datetime, timezone
    current = datetime.now(timezone.utc).strftime("%Y%m")
    if int(ym) > int(current):
        raise SystemExit(
            f"output_month {ym} is in the future (current UTC: {current}). "
            f"Future months have no data — this is almost certainly a typo."
        )
    return ym


def mode_cfg(mode: str) -> dict:
    if mode not in MODE_CONFIG:
        raise SystemExit(f"Invalid --mode {mode!r}. Choices: {list(MODE_CONFIG)}")
    return MODE_CONFIG[mode]


# ---------------------------------------------------------------------------
# AWS helpers
# ---------------------------------------------------------------------------

def connect_redis(mode: str) -> redis.Redis:
    """Create a Redis connection using the mode-specific secret."""
    cfg = mode_cfg(mode)
    client = boto3.client("secretsmanager", region_name=SECRETS_REGION)
    resp = client.get_secret_value(SecretId=cfg["redis_secret"])
    secret = json.loads(resp["SecretString"])

    conn = redis.Redis(
        host=secret["REDIS_HOST"],
        port=int(secret.get("REDIS_PORT", 6379)),
        password=secret["REDIS_PASSWORD"],
        ssl=cfg["redis_ssl"],
        decode_responses=True,
    )
    conn.ping()
    return conn


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

def read_ids_from_csv(csv_path: str) -> List[str]:
    """Read outlet IDs from a CSV file with an 'id_outlet' column."""
    path = Path(csv_path)
    if not path.exists():
        raise SystemExit(f"CSV file not found: {csv_path}")

    ids: List[str] = []
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if "id_outlet" not in (reader.fieldnames or []):
            raise SystemExit(
                f"CSV file must have an 'id_outlet' column. "
                f"Found columns: {reader.fieldnames}"
            )
        for row in reader:
            value = row["id_outlet"].strip()
            if value:
                ids.append(value)

    if not ids:
        raise SystemExit("CSV file contains no IDs.")
    return ids


# ---------------------------------------------------------------------------
# Subcommand: push
# ---------------------------------------------------------------------------

def cmd_push(args: argparse.Namespace) -> None:
    """Push IDs from CSV to Redis (mode-aware)."""
    validate_country(args.country)
    validate_output_month(args.output_month)

    ids = read_ids_from_csv(args.csv_path)
    db_key = f"{args.platform}:{args.country}:{args.output_month}:outlet_feeds"

    _log(f"Mode: {args.mode}  (secret={mode_cfg(args.mode)['redis_secret']})")
    _log(f"Read {len(ids)} IDs from {args.csv_path}")
    _log(f"Redis key: {db_key}")

    conn = connect_redis(args.mode)
    _log("Redis connected.")

    # Race-protection: don't silently wipe a non-empty key.
    # In Fargate mode (prod Redis), this can collide with the monthly cron.
    current = conn.scard(db_key)
    _log(f"Current IDs in Redis key: {current}")

    if current > 0 and not args.force:
        raise SystemExit(
            f"REFUSING to overwrite: key '{db_key}' already has {current} IDs.\n"
            f"  - In fargate mode, this may be the monthly cron's queue; overwriting disrupts it.\n"
            f"  - If you are sure (no cron running, no other users): re-run with --force.\n"
            f"Aborted — 0 IDs pushed."
        )

    # Clear and push in chunks
    conn.delete(db_key)
    chunk_size = 10000
    for i in range(0, len(ids), chunk_size):
        chunk = ids[i : i + chunk_size]
        conn.sadd(db_key, *chunk)
        if len(ids) > chunk_size:
            _log(f"  pushed {min(i + chunk_size, len(ids))}/{len(ids)}")

    final = conn.scard(db_key)
    _log(f"Pushed {final} IDs to Redis key [{db_key}]")

    result = {
        "action":     "push",
        "mode":       args.mode,
        "csv_path":   args.csv_path,
        "ids_in_csv": len(ids),
        "ids_pushed": final,
        "redis_key":  db_key,
        "redis_host_secret": mode_cfg(args.mode)["redis_secret"],
    }
    print(json.dumps(result, indent=2))


# ---------------------------------------------------------------------------
# Subcommand: verify
# ---------------------------------------------------------------------------

def cmd_verify(args: argparse.Namespace) -> None:
    """Verify S3 output after a crawl, filtered by id_job date."""
    from datetime import datetime

    validate_country(args.country)
    validate_output_month(args.output_month)
    cfg = mode_cfg(args.mode)

    s3_client = boto3.client("s3")
    prefix = cfg["s3_prefix"].format(
        platform=args.platform, output_month=args.output_month
    )

    id_job = args.id_job if args.id_job else datetime.now().strftime("%Y%m%d")

    results = {}

    for table in TABLES:
        table_prefix = f"{prefix}{args.country}_{table}/id_job={id_job}/"
        try:
            objects = []
            paginator = s3_client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=cfg["s3_bucket"], Prefix=table_prefix):
                objects.extend(page.get("Contents", []))

            total_size = sum(obj["Size"] for obj in objects)
            results[table] = {
                "file_count":       len(objects),
                "total_size_bytes": total_size,
                "total_size_mb":    round(total_size / (1024 * 1024), 2),
                "s3_path":          f"s3://{cfg['s3_bucket']}/{table_prefix}",
            }
        except Exception as e:
            results[table] = {"error": str(e)}

    print(json.dumps({
        "action": "verify",
        "mode":   args.mode,
        "bucket": cfg["s3_bucket"],
        "id_job": id_job,
        "tables": results,
    }, indent=2))


# ---------------------------------------------------------------------------
# Subcommand: check-mysql (now also reports metadata completeness)
# ---------------------------------------------------------------------------

def get_mysql_connection(platform: str):
    """Create a MySQL connection using credentials from Secrets Manager."""
    try:
        import pymysql
    except ImportError:
        raise SystemExit("pymysql is required. Install with: pip install pymysql")

    sm_client = boto3.client("secretsmanager", region_name=SECRETS_REGION)
    resp = sm_client.get_secret_value(SecretId="db/mysql/general")
    mysql_secret = json.loads(resp["SecretString"])

    return pymysql.connect(
        host=mysql_secret["host"],
        user=mysql_secret["user"],
        password=mysql_secret["passwd"],
        database=platform,
        charset="utf8mb4",
    )


def cmd_check_mysql(args: argparse.Namespace) -> None:
    """Check CSV IDs against MySQL. Report missing IDs AND metadata completeness."""
    validate_country(args.country)

    ids = read_ids_from_csv(args.csv_path)
    conn = get_mysql_connection(args.platform)
    cursor = conn.cursor()

    # Use parameterized query — IDs go through placeholders, not f-string interpolation.
    # Country was already validated by regex, so safe to interpolate as table name.
    placeholders = ",".join(["%s"] * len(ids))

    # 1. Find which IDs already exist
    cursor.execute(
        f"SELECT id_outlet FROM `{args.country}` WHERE id_outlet IN ({placeholders})",
        ids,
    )
    found_ids = {str(row[0]) for row in cursor.fetchall()}
    missing_ids = [i for i in ids if i not in found_ids]

    # 2. Of the found IDs, how many have NULL metadata (unique_name)?
    # This matters because the detail spider skips IDs without unique_name.
    metadata_null_count = 0
    if found_ids:
        found_list = list(found_ids)
        found_placeholders = ",".join(["%s"] * len(found_list))
        cursor.execute(
            f"""SELECT COUNT(*) FROM `{args.country}`
                WHERE id_outlet IN ({found_placeholders})
                  AND (unique_name IS NULL OR unique_name = '')""",
            found_list,
        )
        metadata_null_count = cursor.fetchone()[0]

    cursor.close()
    conn.close()

    metadata_ok_count = len(found_ids) - metadata_null_count
    completeness = round(100 * metadata_ok_count / max(len(ids), 1), 1)

    result = {
        "action":                  "check-mysql",
        "ids_in_csv":              len(ids),
        "ids_found_in_mysql":      len(found_ids),
        "ids_missing_from_mysql":  len(missing_ids),
        "missing_ids":             missing_ids,
        "metadata_null_count":     metadata_null_count,
        "metadata_ok_count":       metadata_ok_count,
        "metadata_completeness_pct": completeness,
    }

    # Narrate warnings loudly (SKILL.md Pre-launch Gate reads these)
    if metadata_null_count > 0:
        result["warning"] = (
            f"{metadata_null_count} IDs exist in MySQL but have NULL unique_name. "
            f"The detail spider will silently SKIP these — "
            f"run the finder spider or manually populate metadata first."
        )
    print(json.dumps(result, indent=2))


def cmd_insert_mysql(args: argparse.Namespace) -> None:
    """Insert missing IDs into MySQL with only id_outlet (other fields NULL)."""
    validate_country(args.country)

    ids = read_ids_from_csv(args.csv_path)
    conn = get_mysql_connection(args.platform)
    cursor = conn.cursor()

    placeholders = ",".join(["%s"] * len(ids))
    cursor.execute(
        f"SELECT id_outlet FROM `{args.country}` WHERE id_outlet IN ({placeholders})",
        ids,
    )
    found_ids = {str(row[0]) for row in cursor.fetchall()}
    missing_ids = [i for i in ids if i not in found_ids]

    if not missing_ids:
        print(json.dumps({
            "action":       "insert-mysql",
            "ids_inserted": 0,
            "message":      "All IDs already exist in MySQL. Nothing to insert.",
        }, indent=2))
        cursor.close()
        conn.close()
        return

    # Parameterized bulk insert — ids never hit SQL as interpolated strings
    insert_sql = f"INSERT INTO `{args.country}` (id_outlet) VALUES (%s)"
    cursor.executemany(insert_sql, [(mid,) for mid in missing_ids])
    conn.commit()

    cursor.close()
    conn.close()

    result = {
        "action":       "insert-mysql",
        "ids_inserted": len(missing_ids),
        "inserted_ids": missing_ids,
        "warning": (
            "Inserted IDs have NULL metadata (unique_name, cuisine, etc.). "
            "The detail spider requires metadata (e.g. unique_name) to build request URLs. "
            "If the spider cannot resolve metadata for these IDs, they will be skipped. "
            "Run the finder spider or manually populate metadata before running the detail spider."
        ),
    }
    print(json.dumps(result, indent=2))


# ---------------------------------------------------------------------------
# Subcommand: verify-ids (new — ID-level post-run check)
# ---------------------------------------------------------------------------

def cmd_verify_ids(args: argparse.Namespace) -> None:
    """For each id_outlet in CSV, check whether a row landed in MySQL today.

    This is the 'did the data actually land?' check — complements `verify`
    which only looks at S3 file counts.

    IDs are normalized before comparison (strip + str cast). With --lenient-match,
    also lstrip leading zeros + lowercase to catch Excel-zero-padding false negatives.
    """
    validate_country(args.country)

    raw_ids = read_ids_from_csv(args.csv_path)
    # Keep raw forms for user-facing output; normalize for comparison only.
    norm_by_raw = {raw: normalize_id(raw, lenient=args.lenient_match) for raw in raw_ids}

    conn = get_mysql_connection(args.platform)
    cursor = conn.cursor()

    # Query uses the RAW ids (as stored); normalization happens in post-processing
    # because we don't control MySQL's stored form.
    placeholders = ",".join(["%s"] * len(raw_ids))
    cursor.execute(
        f"""SELECT id_outlet FROM `{args.country}`
            WHERE id_outlet IN ({placeholders})
              AND DATE(last_refresh) = CURDATE()""",
        raw_ids,
    )
    mysql_ids_raw = [str(row[0]) for row in cursor.fetchall()]
    mysql_ids_norm: Set[str] = {
        normalize_id(x, lenient=args.lenient_match) for x in mysql_ids_raw
    }

    # Compare using normalized forms
    landed_today_raw = [raw for raw, norm in norm_by_raw.items() if norm in mysql_ids_norm]
    not_landed_raw  = [raw for raw in raw_ids if raw not in landed_today_raw]

    cursor.close()
    conn.close()

    result = {
        "action":               "verify-ids",
        "ids_in_csv":           len(raw_ids),
        "ids_landed_today":     len(landed_today_raw),
        "ids_not_landed_today": len(not_landed_raw),
        "not_landed_sample":    not_landed_raw[:20],   # cap the list for readability
        "landing_rate_pct":     round(100 * len(landed_today_raw) / max(len(raw_ids), 1), 1),
        "lenient_match":        args.lenient_match,
    }

    if result["landing_rate_pct"] < 80:
        result["verdict"] = "❌ LOW LANDING RATE — investigate spider/pipeline"
    elif result["landing_rate_pct"] < 95:
        result["verdict"] = "⚠️  partial — some IDs did not land today"
    else:
        result["verdict"] = "✅ data landed"

    # Hint: if strict matching showed a gap, suggest re-running with lenient to rule out
    # Excel zero-padding / case issues before investigating spider.
    if not args.lenient_match and len(not_landed_raw) > 0:
        result["hint"] = (
            "If these 'not_landed' IDs look fine in MySQL manually, try "
            "--lenient-match to rule out leading-zero / case drift from the CSV."
        )

    print(json.dumps(result, indent=2))


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def _add_mode(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--mode", required=True, choices=list(MODE_CONFIG),
        help="local = test Redis + bucket dash-alpha-dev (sourcing/{PLATFORM}/...); "
             "fargate = prod Redis + SAME bucket dash-alpha-dev (same path as monthly cron).",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ID refresh workflow: push IDs to Redis and verify S3 output."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # -- push --
    p = subparsers.add_parser("push", help="Push IDs from CSV to Redis")
    p.add_argument("--platform", required=True, help="Platform id, e.g. TKW")
    p.add_argument("--country",  required=True, help="Country code, e.g. NL")
    p.add_argument("--output-month", required=True, help="YYYYMM, e.g. 202603")
    p.add_argument("--csv-path", required=True, help="Path to CSV with id_outlet column")
    p.add_argument("--force", action="store_true",
                   help="Overwrite even if the Redis key already has IDs. Required for non-empty keys.")
    _add_mode(p)

    # -- verify --
    v = subparsers.add_parser("verify", help="Verify S3 output after crawl")
    v.add_argument("--platform", required=True)
    v.add_argument("--country",  required=True)
    v.add_argument("--output-month", required=True)
    v.add_argument("--id-job", default=None, help="Filter by id_job date (YYYYMMDD). Defaults to today.")
    _add_mode(v)

    # -- check-mysql --
    c = subparsers.add_parser(
        "check-mysql", help="Check if CSV IDs exist in MySQL + metadata completeness"
    )
    c.add_argument("--platform", required=True)
    c.add_argument("--country",  required=True)
    c.add_argument("--csv-path", required=True)

    # -- insert-mysql --
    i = subparsers.add_parser(
        "insert-mysql", help="Insert missing IDs into MySQL (id_outlet only, other fields NULL)"
    )
    i.add_argument("--platform", required=True)
    i.add_argument("--country",  required=True)
    i.add_argument("--csv-path", required=True)

    # -- verify-ids --
    vi = subparsers.add_parser(
        "verify-ids",
        help="Post-run: check MySQL for each CSV id_outlet with today's last_refresh",
    )
    vi.add_argument("--platform", required=True)
    vi.add_argument("--country",  required=True)
    vi.add_argument("--csv-path", required=True)
    vi.add_argument(
        "--lenient-match", action="store_true",
        help="Also lstrip leading zeros + lowercase when comparing IDs. "
             "Use when CSV came from Excel (which may zero-pad) or IDs have case drift.",
    )

    return parser.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()
    dispatch = {
        "push":         cmd_push,
        "verify":       cmd_verify,
        "check-mysql":  cmd_check_mysql,
        "insert-mysql": cmd_insert_mysql,
        "verify-ids":   cmd_verify_ids,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)

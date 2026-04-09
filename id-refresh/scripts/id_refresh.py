#!/usr/bin/env python3
"""Push outlet IDs to Redis and verify S3 output for targeted re-crawls."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import List

import boto3
import redis


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SECRETS_REGION = "us-east-1"
S3_BUCKET = "dash-alpha-dev"
S3_PREFIX_TEMPLATE = "sourcing/{platform}/{output_month}/"
TABLES = ["outlet_information", "outlet_meal", "meal_option", "option_relation"]


# ---------------------------------------------------------------------------
# AWS helpers
# ---------------------------------------------------------------------------

def connect_redis() -> redis.Redis:
    """Create a Redis connection (test environment)."""
    client = boto3.client("secretsmanager", region_name=SECRETS_REGION)
    resp = client.get_secret_value(SecretId="db/redis/test")
    secret = json.loads(resp["SecretString"])
    conn = redis.Redis(
        host=secret["REDIS_HOST"],
        port=int(secret.get("REDIS_PORT", 6379)),
        password=secret["REDIS_PASSWORD"],
        ssl=False,
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
    """Push IDs from CSV to Redis."""
    ids = read_ids_from_csv(args.csv_path)
    db_key = f"{args.platform}:{args.country}:{args.output_month}:outlet_feeds"

    print(f"Read {len(ids)} IDs from {args.csv_path}")
    print(f"Redis key: {db_key}")

    conn = connect_redis()
    print("Redis connected.")

    # Show current state
    current = conn.scard(db_key)
    print(f"Current IDs in Redis key: {current}")

    # Clear and push
    conn.delete(db_key)
    # Push in chunks to avoid huge SADD commands
    chunk_size = 10000
    for i in range(0, len(ids), chunk_size):
        chunk = ids[i : i + chunk_size]
        conn.sadd(db_key, *chunk)

    final = conn.scard(db_key)
    print(f"Pushed {final} IDs to Redis key [{db_key}]")

    # Output machine-readable summary
    result = {
        "action": "push",
        "csv_path": args.csv_path,
        "ids_in_csv": len(ids),
        "ids_pushed": final,
        "redis_key": db_key,
    }
    print(json.dumps(result, indent=2))


# ---------------------------------------------------------------------------
# Subcommand: verify
# ---------------------------------------------------------------------------

def cmd_verify(args: argparse.Namespace) -> None:
    """Verify S3 output after a crawl, filtered by id_job date."""
    from datetime import datetime

    s3_client = boto3.client("s3")
    prefix = S3_PREFIX_TEMPLATE.format(
        platform=args.platform, output_month=args.output_month
    )

    # Determine id_job filter: user-provided or today's date
    id_job = args.id_job if args.id_job else datetime.now().strftime("%Y%m%d")

    tables = TABLES
    results = {}

    for table in tables:
        table_prefix = f"{prefix}{args.country}_{table}/id_job={id_job}/"
        try:
            objects = []
            paginator = s3_client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=table_prefix):
                objects.extend(page.get("Contents", []))

            total_size = sum(obj["Size"] for obj in objects)
            results[table] = {
                "file_count": len(objects),
                "total_size_bytes": total_size,
                "total_size_mb": round(total_size / (1024 * 1024), 2),
                "s3_path": f"s3://{S3_BUCKET}/{table_prefix}",
            }
        except Exception as e:
            results[table] = {"error": str(e)}

    print(json.dumps({"action": "verify", "id_job": id_job, "tables": results}, indent=2))


# ---------------------------------------------------------------------------
# Subcommand: check-mysql
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
    """Check CSV IDs against MySQL. Report missing IDs."""
    ids = read_ids_from_csv(args.csv_path)
    conn = get_mysql_connection(args.platform)
    cursor = conn.cursor()

    # Find which IDs already exist
    id_list = ",".join(f"'{i}'" for i in ids)
    query = f"SELECT id_outlet FROM {args.country} WHERE id_outlet IN ({id_list})"
    cursor.execute(query)
    found_ids = {str(row[0]) for row in cursor.fetchall()}
    missing_ids = [i for i in ids if i not in found_ids]

    cursor.close()
    conn.close()

    result = {
        "action": "check-mysql",
        "ids_in_csv": len(ids),
        "ids_found_in_mysql": len(found_ids),
        "ids_missing_from_mysql": len(missing_ids),
        "missing_ids": missing_ids,
    }
    print(json.dumps(result, indent=2))


def cmd_insert_mysql(args: argparse.Namespace) -> None:
    """Insert missing IDs into MySQL with only id_outlet (other fields NULL)."""
    ids = read_ids_from_csv(args.csv_path)
    conn = get_mysql_connection(args.platform)
    cursor = conn.cursor()

    # Find which IDs already exist
    id_list = ",".join(f"'{i}'" for i in ids)
    query = f"SELECT id_outlet FROM {args.country} WHERE id_outlet IN ({id_list})"
    cursor.execute(query)
    found_ids = {str(row[0]) for row in cursor.fetchall()}
    missing_ids = [i for i in ids if i not in found_ids]

    if not missing_ids:
        print(json.dumps({
            "action": "insert-mysql",
            "ids_inserted": 0,
            "message": "All IDs already exist in MySQL. Nothing to insert.",
        }, indent=2))
        cursor.close()
        conn.close()
        return

    # Insert missing IDs (only id_outlet; created_at/last_refresh auto-filled by MySQL)
    insert_sql = f"INSERT INTO {args.country} (id_outlet) VALUES (%s)"
    for mid in missing_ids:
        cursor.execute(insert_sql, (mid,))
    conn.commit()

    cursor.close()
    conn.close()

    result = {
        "action": "insert-mysql",
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
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ID refresh workflow: push IDs to Redis and verify S3 output."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # -- push --
    push_parser = subparsers.add_parser("push", help="Push IDs from CSV to Redis")
    push_parser.add_argument("--platform", required=True, help="Platform id, e.g. TKW")
    push_parser.add_argument("--country", required=True, help="Country code, e.g. NL")
    push_parser.add_argument("--output-month", required=True, help="YYYYMM, e.g. 202603")
    push_parser.add_argument("--csv-path", required=True, help="Path to CSV with id_outlet column")

    # -- verify --
    verify_parser = subparsers.add_parser("verify", help="Verify S3 output after crawl")
    verify_parser.add_argument("--platform", required=True, help="Platform id, e.g. TKW")
    verify_parser.add_argument("--country", required=True, help="Country code, e.g. NL")
    verify_parser.add_argument("--output-month", required=True, help="YYYYMM, e.g. 202603")
    verify_parser.add_argument("--id-job", default=None, help="Filter by id_job date (YYYYMMDD). Defaults to today.")

    # -- check-mysql --
    check_parser = subparsers.add_parser(
        "check-mysql", help="Check if CSV IDs exist in MySQL metadata table"
    )
    check_parser.add_argument("--platform", required=True, help="Platform id, e.g. TKW")
    check_parser.add_argument("--country", required=True, help="Country code, e.g. NL")
    check_parser.add_argument("--csv-path", required=True, help="Path to CSV with id_outlet column")

    # -- insert-mysql --
    insert_parser = subparsers.add_parser(
        "insert-mysql", help="Insert missing IDs into MySQL (id_outlet only, other fields NULL)"
    )
    insert_parser.add_argument("--platform", required=True, help="Platform id, e.g. TKW")
    insert_parser.add_argument("--country", required=True, help="Country code, e.g. NL")
    insert_parser.add_argument("--csv-path", required=True, help="Path to CSV with id_outlet column")

    return parser.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    if args.command == "push":
        cmd_push(args)
    elif args.command == "verify":
        cmd_verify(args)
    elif args.command == "check-mysql":
        cmd_check_mysql(args)
    elif args.command == "insert-mysql":
        cmd_insert_mysql(args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)

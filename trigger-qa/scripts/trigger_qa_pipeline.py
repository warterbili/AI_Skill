#!/usr/bin/env python3
"""Trigger the Dash sourcing QA pipeline Lambda from a local machine and verify EMR."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional


DEFAULT_ENGINEER_SLACK_IDS = {
    "Morri": "U06LYCYHMLY",
    "Shuaiwei": "U01DGJUKHLL",
    "Linus": "U0A02N4N0PQ",
    "Xuyu": "U067T4EHPU6",
    "Cam": "U08HRFUBJ59",
    "Howie": "U08QFV4T97H",
    "Shudong": "U09RX5ZCSBC",
    "Acheng": "U09B6CL5W4S",
    "Xingwen": "U03J9H75SAZ",
}

DEFAULT_TABLES = [
    "outlet_information",
    "outlet_meal",
    "meal_option",
    "option_relation",
]
DEFAULT_REGION = "eu-central-1"
DEFAULT_FUNCTION_NAME = (
    "arn:aws:lambda:eu-central-1:593453040104:function:dash-sourcing-pipeline-spark"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Trigger dash-sourcing-pipeline-spark and verify the EMR cluster."
    )
    parser.add_argument("--platform", required=True, help="Platform id, e.g. EPL")
    parser.add_argument("--country", required=True, help="Country code, e.g. US")
    parser.add_argument("--refresh", required=True, help="Month in YYYYMM, e.g. 202602")
    parser.add_argument("--engineer-name", help="Known engineer name to resolve to Slack id")
    parser.add_argument("--engineer-id", help="Raw Slack user id")
    parser.add_argument(
        "--engineer-map-file",
        help="Optional JSON file mapping engineer names to Slack ids",
    )
    parser.add_argument("--env", default="dev")
    parser.add_argument("--layer", default="raw")
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--function-name", default=DEFAULT_FUNCTION_NAME)
    parser.add_argument(
        "--tables",
        default=",".join(DEFAULT_TABLES),
        help="Comma-separated table list",
    )
    parser.add_argument(
        "--load-raw-as-strings",
        type=int,
        choices=[0, 1],
        default=0,
        help="Set to 1 for the raw-string schema-conversion path. Defaults to 0.",
    )
    parser.add_argument("--no-verify-emr", action="store_true")
    parser.add_argument("--verify-attempts", type=int, default=10)
    parser.add_argument("--verify-interval", type=int, default=3)
    return parser.parse_args()


def normalize_platform(platform: str) -> str:
    value = platform.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{2,10}", value):
        raise SystemExit("Platform must be 2-10 uppercase letters or digits, e.g. EPL.")
    return value


def normalize_country(country: str) -> str:
    value = country.strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", value):
        raise SystemExit("Country must be a 2-letter code, e.g. US.")
    return value


def normalize_refresh(refresh: str) -> str:
    value = refresh.strip()
    if not re.fullmatch(r"\d{6}", value):
        raise SystemExit("Refresh must use YYYYMM format, e.g. 202602.")

    year = int(value[:4])
    month = int(value[4:])
    try:
        date(year, month, 1)
    except ValueError as exc:
        raise SystemExit(f"Refresh is not a valid calendar month: {value}") from exc
    return value


def normalize_tables(tables: str) -> List[str]:
    parsed = [table.strip() for table in tables.split(",") if table.strip()]
    if not parsed:
        raise SystemExit("At least one table must be provided.")
    return parsed


def require_positive_int(value: int, name: str) -> int:
    if value < 1:
        raise SystemExit(f"{name} must be at least 1.")
    return value


def require_aws_cli() -> None:
    if shutil.which("aws") is None:
        raise SystemExit("aws CLI is not installed or not on PATH.")


def run_aws_json(args: List[str]) -> Dict[str, Any]:
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())

    stdout = result.stdout.strip()
    return json.loads(stdout) if stdout else {}


def get_caller_identity(region: str) -> Dict[str, Any]:
    return run_aws_json(["aws", "sts", "get-caller-identity", "--region", region])


def load_engineer_map(path: Optional[str]) -> Dict[str, str]:
    engineer_map = dict(DEFAULT_ENGINEER_SLACK_IDS)
    if not path:
        return engineer_map

    map_path = Path(path).expanduser()
    try:
        raw = map_path.read_text()
    except OSError as exc:
        raise SystemExit(f"Failed to read engineer map file {map_path}: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Engineer map file is not valid JSON: {map_path}") from exc

    if not isinstance(data, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in data.items()
    ):
        raise SystemExit("Engineer map file must contain a JSON object of string keys and values.")
    engineer_map.update(data)
    return engineer_map


def resolve_engineer_id(args: argparse.Namespace, engineer_map: Dict[str, str]) -> str:
    if args.engineer_id:
        return args.engineer_id.strip()

    if args.engineer_name:
        lookup = {name.casefold(): slack_id for name, slack_id in engineer_map.items()}
        engineer_id = lookup.get(args.engineer_name.strip().casefold())
        if engineer_id:
            return engineer_id

    known = ", ".join(sorted(engineer_map))
    raise SystemExit(
        "Provide --engineer-id or a known --engineer-name. "
        f"Known names: {known}"
    )


def build_payload(args: argparse.Namespace, engineer_id: str, tables: List[str]) -> Dict[str, Any]:
    return {
        "run": True,
        "params": {
            "trigger_engineer_id": engineer_id,
            "env": args.env,
            "layer": args.layer,
            "table_list": tables,
            "refresh": args.refresh,
            "country": args.country,
            "id_platform": args.platform,
            "load_raw_as_strings": args.load_raw_as_strings,
        },
    }


def invoke_lambda(payload: Dict[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as temp_file:
        output_path = Path(temp_file.name)

    try:
        command = [
            "aws",
            "lambda",
            "invoke",
            "--region",
            args.region,
            "--function-name",
            args.function_name,
            "--invocation-type",
            "RequestResponse",
            "--cli-binary-format",
            "raw-in-base64-out",
            "--payload",
            json.dumps(payload),
            str(output_path),
        ]
        metadata = run_aws_json(command)
        payload_text = output_path.read_text().strip()
        return {"metadata": metadata, "payload_text": payload_text}
    finally:
        output_path.unlink(missing_ok=True)


def find_cluster(
    cluster_name: str, region: str, attempts: int, interval: int
) -> Optional[Dict[str, Any]]:
    command = ["aws", "emr", "list-clusters", "--active", "--region", region]
    for _ in range(attempts):
        response = run_aws_json(command)
        for cluster in response.get("Clusters", []):
            if cluster.get("Name") == cluster_name:
                return cluster
        time.sleep(interval)
    return None


def emr_console_link(region: str, cluster_id: str) -> str:
    return (
        "https://{region}.console.aws.amazon.com/elasticmapreduce/home"
        "?region={region}#cluster-details:{cluster_id}"
    ).format(region=region, cluster_id=cluster_id)


def main() -> None:
    args = parse_args()
    args.platform = normalize_platform(args.platform)
    args.country = normalize_country(args.country)
    args.refresh = normalize_refresh(args.refresh)
    args.verify_attempts = require_positive_int(args.verify_attempts, "verify-attempts")
    args.verify_interval = require_positive_int(args.verify_interval, "verify-interval")

    require_aws_cli()

    tables = normalize_tables(args.tables)
    engineer_map = load_engineer_map(args.engineer_map_file)
    engineer_id = resolve_engineer_id(args, engineer_map)
    caller = get_caller_identity(args.region)
    payload = build_payload(args, engineer_id, tables)
    lambda_result = invoke_lambda(payload, args)

    cluster_name = f"sourcing-pipeline-{args.platform}-{args.country}-{args.refresh}"
    cluster = None
    if not args.no_verify_emr:
        cluster = find_cluster(
            cluster_name=cluster_name,
            region=args.region,
            attempts=args.verify_attempts,
            interval=args.verify_interval,
        )

    result = {
        "caller_identity": caller,
        "request": {
            "platform": args.platform,
            "country": args.country,
            "refresh": args.refresh,
            "engineer_id": engineer_id,
            "engineer_name": args.engineer_name,
            "env": args.env,
            "layer": args.layer,
            "tables": tables,
            "region": args.region,
            "function_name": args.function_name,
        },
        "lambda": lambda_result["metadata"],
        "lambda_payload": lambda_result["payload_text"] or None,
        "cluster_name_expected": cluster_name,
        "cluster": cluster,
        "cluster_console_url": (
            emr_console_link(args.region, cluster["Id"]) if cluster else None
        ),
        "verification": {
            "emr_checked": not args.no_verify_emr,
            "attempts": args.verify_attempts,
            "interval_seconds": args.verify_interval,
            "cluster_found": cluster is not None,
        },
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)

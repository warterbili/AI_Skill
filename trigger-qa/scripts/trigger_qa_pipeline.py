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
# Use the function NAME only — AWS CLI resolves it against the caller's account.
# Pass a full ARN via --function-name to invoke a Lambda in a different account.
DEFAULT_FUNCTION_NAME = "dash-sourcing-pipeline-spark"

# EMR terminal states — cluster won't change after these
EMR_TERMINAL_STATES = {"TERMINATED", "TERMINATED_WITH_ERRORS"}
EMR_HEALTHY_STATES  = {"RUNNING", "WAITING"}


# ---------------------------------------------------------------------------
# Output streams — narrate to stderr, JSON to stdout
# ---------------------------------------------------------------------------

def _log(msg: str) -> None:
    """Narration goes to stderr so stdout stays pure JSON for jq parsing."""
    print(msg, file=sys.stderr, flush=True)


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
    parser.add_argument(
        "--check-existing", action="store_true",
        help="Before invoking, check for an active EMR cluster with the same name "
             "in the last 60 min. If found, include it in the output JSON as "
             "'existing_clusters_warning' so the caller can decide whether to proceed.",
    )
    parser.add_argument(
        "--wait-for-completion", action="store_true",
        help="After cluster is found, keep polling until it reaches a terminal "
             "state (TERMINATED / TERMINATED_WITH_ERRORS). Useful for CI/QA gates.",
    )
    parser.add_argument(
        "--completion-timeout", type=int, default=1800,
        help="Max seconds to wait for cluster completion (default 1800 = 30 min).",
    )
    parser.add_argument(
        "--completion-poll-interval", type=int, default=30,
        help="Seconds between completion polls (default 30).",
    )
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


def check_existing_clusters(cluster_name: str, region: str) -> List[Dict[str, Any]]:
    """Return any active EMR clusters with the same name (created in last 60 min).

    Helps prevent accidental double-triggers — running two QA pipelines for the
    same (platform, country, refresh) wastes ~$5-20 of EMR time and confuses
    downstream consumers.
    """
    cmd = ["aws", "emr", "list-clusters", "--active", "--region", region,
           "--created-after", str(int(time.time() - 3600))]
    response = run_aws_json(cmd)
    return [c for c in response.get("Clusters", []) if c.get("Name") == cluster_name]


def wait_for_cluster_completion(
    cluster_id: str, region: str, timeout: int, interval: int
) -> Dict[str, Any]:
    """Poll cluster state until terminal or timeout. Returns final state info."""
    start = time.time()
    transitions: List[Dict[str, str]] = []
    last_state: Optional[str] = None

    while True:
        elapsed = int(time.time() - start)
        try:
            resp = run_aws_json([
                "aws", "emr", "describe-cluster",
                "--cluster-id", cluster_id, "--region", region,
            ])
        except Exception as e:
            _log(f"  [{elapsed}s] describe-cluster error: {e}")
            time.sleep(interval)
            if elapsed > timeout:
                break
            continue

        cluster = resp.get("Cluster", {})
        state = cluster.get("Status", {}).get("State", "UNKNOWN")
        reason = cluster.get("Status", {}).get("StateChangeReason", {}).get("Message", "")

        if state != last_state:
            transitions.append({"state": state, "at": f"+{elapsed}s", "reason": reason})
            _log(f"  [{elapsed}s] state: {last_state or '(start)'} → {state}  {reason}")
            last_state = state

        if state in EMR_TERMINAL_STATES:
            return {
                "final_state":      state,
                "final_reason":     reason,
                "elapsed_seconds":  elapsed,
                "timed_out":        False,
                "transitions":      transitions,
            }

        if elapsed > timeout:
            _log(f"  ⏰ timeout after {timeout}s — cluster still {state}")
            return {
                "final_state":      state,
                "final_reason":     reason,
                "elapsed_seconds":  elapsed,
                "timed_out":        True,
                "transitions":      transitions,
            }

        time.sleep(interval)


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
    _log(f"Resolved engineer: {args.engineer_name or '(direct id)'} → {engineer_id}")

    caller = get_caller_identity(args.region)
    _log(f"AWS account: {caller.get('Account')}  arn: {caller.get('Arn', '?')}")

    cluster_name = f"sourcing-pipeline-{args.platform}-{args.country}-{args.refresh}"

    # ---- Pre-invoke: check for existing cluster (if requested) ----
    existing_warning: Optional[List[Dict[str, Any]]] = None
    if args.check_existing:
        _log(f"Checking for existing clusters named {cluster_name} (last 60 min)...")
        existing = check_existing_clusters(cluster_name, args.region)
        if existing:
            _log(f"⚠️  Found {len(existing)} existing cluster(s) with same name")
            existing_warning = [
                {"id": c["Id"], "state": c["Status"]["State"], "name": c["Name"]}
                for c in existing
            ]

    # ---- Invoke Lambda ----
    payload = build_payload(args, engineer_id, tables)
    _log(f"Invoking {args.function_name}...")
    lambda_result = invoke_lambda(payload, args)
    _log("Lambda returned.")

    # ---- Verify cluster started ----
    cluster: Optional[Dict[str, Any]] = None
    if not args.no_verify_emr:
        _log(f"Polling for cluster {cluster_name} (max {args.verify_attempts}×{args.verify_interval}s)...")
        cluster = find_cluster(
            cluster_name=cluster_name,
            region=args.region,
            attempts=args.verify_attempts,
            interval=args.verify_interval,
        )
        if cluster:
            _log(f"✅ Cluster found: {cluster['Id']} (state={cluster['Status']['State']})")
        else:
            _log("⚠️  Cluster not found within verification window")

    # ---- Optional: wait for cluster completion ----
    completion: Optional[Dict[str, Any]] = None
    if args.wait_for_completion and cluster:
        _log(f"Waiting for cluster {cluster['Id']} to complete (max {args.completion_timeout}s)...")
        completion = wait_for_cluster_completion(
            cluster_id=cluster["Id"],
            region=args.region,
            timeout=args.completion_timeout,
            interval=args.completion_poll_interval,
        )

    # ---- Compose result ----
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
        "existing_clusters_warning": existing_warning,
        "completion": completion,
    }

    # Top-level verdict for callers (Claude / SKILL.md / bot)
    if completion:
        if completion["final_state"] == "TERMINATED":
            result["verdict"] = "completed_success"
        elif completion["final_state"] == "TERMINATED_WITH_ERRORS":
            result["verdict"] = "completed_with_errors"
        elif completion["timed_out"]:
            result["verdict"] = "completion_timeout"
        else:
            result["verdict"] = f"unexpected_state_{completion['final_state']}"
    elif cluster:
        result["verdict"] = "triggered_cluster_started"
    elif lambda_result["metadata"].get("StatusCode") in (200, 202):
        result["verdict"] = "triggered_no_cluster_observed"
    else:
        result["verdict"] = "lambda_invoke_failed"

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)

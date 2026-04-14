"""
Check Redis grid status for a ConSo platform's finder spider.

Queries prod Redis (via SSM → Docker exec on the spider EC2 instance) to report
remaining grid counts per prefix. Cross-references with SpiderKeeper and MySQL
to give a complete finder health picture.

Used by:
  - conso-migrate  Phase 12 Step 1 (verify grids exist before finder test)
  - run-detail     Phase 0.6 (diagnose why finder is stalled)
  - ad-hoc         check grid consumption progress

Usage:
    python check_redis.py --platform TKW
    python check_redis.py --platform TKW --prefixes AT,DE
    python check_redis.py --platform TKW --test          # check test Redis instead
    python check_redis.py --platform TKW --json

Requires: boto3
"""

import json
import argparse
import sys
import time

try:
    import boto3
except ImportError:
    print("❌  boto3 not installed. Run: pip install boto3")
    sys.exit(1)


SPIDER_INSTANCE_TAG = "*spider*"
# Container names on the spider EC2 instance
SCRAPYD_CONTAINER = "dashmote-conso"
JUPYTER_CONTAINER = "dashmote-sourcing-jupyter-lab-1"


def get_spider_instance_id(region: str) -> str | None:
    ec2 = boto3.client('ec2', region_name=region)
    resp = ec2.describe_instances(
        Filters=[
            {"Name": "tag:Name", "Values": [SPIDER_INSTANCE_TAG]},
            {"Name": "instance-state-name", "Values": ["running"]},
        ]
    )
    for r in resp.get('Reservations', []):
        for i in r.get('Instances', []):
            return i['InstanceId']
    return None


def run_ssm_python(instance_id: str, region: str, python_code: str,
                   container: str = SCRAPYD_CONTAINER, timeout: int = 30) -> str:
    """Run Python code inside a Docker container via SSM and return stdout.

    Writes a temp script file on the host, pipes it into the container via
    'cat | docker exec -i ... python -', then removes the temp file.
    This avoids all shell escaping issues with inline python -c.
    """
    ssm = boto3.client('ssm', region_name=region)

    import base64
    encoded = base64.b64encode(python_code.encode()).decode()
    # Decode base64 to temp file, pipe into container, clean up
    tmp_path = "/tmp/_check_redis_tmp.py"
    cmd = (
        f"echo '{encoded}' | base64 -d > {tmp_path} && "
        f"cat {tmp_path} | sudo docker exec -i {container} python - 2>&1 && "
        f"rm -f {tmp_path}"
    )

    resp = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": [cmd]},
    )
    cmd_id = resp['Command']['CommandId']

    # Poll for result
    for _ in range(timeout):
        time.sleep(1)
        try:
            inv = ssm.get_command_invocation(
                CommandId=cmd_id, InstanceId=instance_id)
            if inv['Status'] in ('Success', 'Failed', 'TimedOut', 'Cancelled'):
                break
        except ssm.exceptions.InvocationDoesNotExist:
            continue

    if inv['Status'] == 'Success':
        return inv.get('StandardOutputContent', '').strip()
    elif inv['Status'] == 'Failed':
        stderr = inv.get('StandardErrorContent', '')
        raise RuntimeError(f"SSM command failed: {stderr[:500]}")
    else:
        raise RuntimeError(f"SSM command {inv['Status']}")


def check_grids(instance_id: str, region: str, platform: str,
                prefixes: list, key_suffix: str = '3000_grid',
                test: bool = False) -> list:
    """Check grid counts for each prefix via SSM → Docker → Python."""

    prefixes_json = json.dumps(prefixes)
    test_str = "True" if test else "False"

    code = "\n".join([
        "import json",
        "from dashmote_sourcing.db import RedisDriver",
        f"r = RedisDriver(test={test_str})",
        "conn = r.create_connection()",
        f"prefixes = {prefixes_json}",
        f"platform = '{platform}'",
        f"key_suffix = '{key_suffix}'",
        "results = []",
        "for p in prefixes:",
        "    key = platform + ':' + p + ':' + key_suffix",
        "    count = conn.zcard(key)",
        "    results.append({'prefix': p, 'key': key, 'grids_remaining': count})",
        "print(json.dumps(results))",
    ])

    output = run_ssm_python(instance_id, region, code)
    if not output:
        raise RuntimeError("SSM returned empty output — script may have failed silently")
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        raise RuntimeError(f"Cannot parse SSM output as JSON: {output[:300]}")


def main():
    parser = argparse.ArgumentParser(
        description='Check Redis grid status for a ConSo platform.'
    )
    parser.add_argument('--platform', required=True,
                        help='Platform ID (e.g. TKW, EPL, DLR)')
    parser.add_argument('--prefixes', default=None,
                        help='Comma-separated prefix list (default: auto from MySQL)')
    parser.add_argument('--key-suffix', default='3000_grid',
                        help='Grid key suffix (default: 3000_grid)')
    parser.add_argument('--test', action='store_true',
                        help='Check test Redis instead of prod')
    parser.add_argument('--region', default='eu-central-1',
                        help='AWS region (default: eu-central-1)')
    parser.add_argument('--json', action='store_true',
                        help='Output as JSON')
    args = parser.parse_args()

    platform = args.platform.upper()

    # Resolve prefixes
    if args.prefixes:
        prefixes = [p.strip().upper() for p in args.prefixes.split(',')]
    else:
        # Auto-discover from MySQL
        import subprocess, os
        check_mysql = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'check_mysql.py')
        result = subprocess.run(
            [sys.executable, check_mysql, '--platform', platform, '--json'],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode in (0, 1) and result.stdout.strip():
            mysql_data = json.loads(result.stdout)
            prefixes = [r['prefix'] for r in mysql_data if r.get('exists', False)]
        else:
            print(f"❌  Could not auto-discover prefixes. Use --prefixes.")
            sys.exit(1)

    if not prefixes:
        print(f"⚠️  No prefixes found for {platform}.")
        sys.exit(0)

    # Get Spider EC2 instance
    instance_id = get_spider_instance_id(args.region)
    if not instance_id:
        print(f"❌  Spider EC2 instance not found.")
        sys.exit(1)

    # Query Redis
    env_label = "TEST" if args.test else "PROD"
    try:
        results = check_grids(instance_id, args.region, platform, prefixes,
                              args.key_suffix, args.test)
    except Exception as e:
        print(f"❌  Failed to query {env_label} Redis: {e}")
        sys.exit(1)

    # Output
    if args.json:
        print(json.dumps(results, indent=2))
        return

    print(f"\n📊  Redis Grid Status — {platform} ({env_label})")
    print(f"    Key pattern: {platform}:{{prefix}}:{args.key_suffix}\n")

    total_grids = 0
    print(f"  {'Prefix':<8} {'Key':<35} {'Grids In Set':>13} {'Status':<20}")
    print(f"  {'─'*6:<8} {'─'*33:<35} {'─'*13:>13} {'─'*18:<20}")

    for r in results:
        count = r['grids_remaining']
        total_grids += count
        # ZCARD=0 is normal for finder round-cycling (pop_and_push_grid):
        # grids are temporarily out of the sorted set while being processed.
        # Only key-not-exist (count=0 AND key doesn't exist) is a real problem,
        # but ZCARD can't distinguish "empty set" from "key missing" — both return 0.
        # So ZCARD=0 is informational, not a warning. The real signal is
        # MySQL last_refresh (check_mysql.py / check_spiderkeeper.py).
        if count > 0:
            icon = '✅'
            status = f'{count:,} in queue'
        else:
            icon = 'ℹ️'
            status = '0 (round cycling or empty)'
        print(f"  {icon} {r['prefix']:<6} {r['key']:<35} {count:>12,}  {status}")

    print(f"\n  Total grids in sets: {total_grids:,}")
    print(f"  Note: ZCARD=0 is normal for finder — grids are popped and pushed back")
    print(f"  in a round-robin loop. Use check_mysql.py to verify actual write activity.")


if __name__ == '__main__':
    main()

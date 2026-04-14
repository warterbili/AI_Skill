"""
Manage SpiderKeeper finder jobs: status, stop, start, deploy.

Actions:
  status  — Show RUNNING finder jobs (with optional MySQL cross-check)
  stop    — Stop a finder job by prefix (via SSM → scrapyd cancel)
  start   — Start a finder job for a prefix
  deploy  — Build .egg and upload to SpiderKeeper

Usage:
    # Check status
    python manage_spiderkeeper.py status --platform TKW
    python manage_spiderkeeper.py status --platform TKW --with-mysql

    # Stop a specific prefix's finder
    python manage_spiderkeeper.py stop --platform TKW --prefix FR

    # Stop ALL finder jobs for a platform
    python manage_spiderkeeper.py stop --platform TKW --all

    # Start finder for a prefix
    python manage_spiderkeeper.py start --platform TKW --prefix AT

    # Start finder for multiple prefixes
    python manage_spiderkeeper.py start --platform TKW --prefix AT,BE,BG

    # Build .egg and upload
    python manage_spiderkeeper.py deploy --platform TKW

Requires: boto3, requests
"""

import re
import json
import argparse
import subprocess
import sys
import os
import time
from datetime import datetime

try:
    import boto3
except ImportError:
    print("❌  boto3 not installed. Run: pip install boto3")
    sys.exit(1)

try:
    import requests as req_lib
except ImportError:
    print("❌  requests not installed. Run: pip install requests")
    sys.exit(1)


SK_HOST = "http://spider.getdashmote.com:1234"
NO_PROXY = {"http": None, "https": None}
SPIDER_INSTANCE_TAG = "*spider*"


# ─────────────────── Auth & Discovery ───────────────────

def get_auth_token(region: str = 'eu-central-1') -> str:
    try:
        from dashmote_sourcing.db import SecretsManager
        secret = SecretsManager.get_secret('Conso_SpiderKeeper', region_name=region)
        return secret['Authorization']
    except ImportError:
        client = boto3.client('secretsmanager', region_name=region)
        resp = client.get_secret_value(SecretId='Conso_SpiderKeeper')
        secret = json.loads(resp['SecretString'])
        return secret['Authorization']


def find_project_id(auth: str, platform: str) -> int | None:
    resp = req_lib.get(f"{SK_HOST}/api/projects",
                       headers={"Authorization": auth}, proxies=NO_PROXY, timeout=10)
    resp.raise_for_status()
    target = f"conso_{platform}".lower()
    for p in resp.json():
        if p['project_name'].lower().replace('conso_', 'conso_') == target:
            return p['project_id']
    return None


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


# ─────────────────── Dashboard Parsing ───────────────────

def parse_running_finder_jobs(auth: str, project_id: int) -> list:
    """Parse RUNNING finder jobs from dashboard HTML."""
    resp = req_lib.get(f"{SK_HOST}/project/{project_id}/job/dashboard",
                       headers={"Authorization": auth}, proxies=NO_PROXY, timeout=15)
    resp.raise_for_status()
    html = resp.text
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL)

    jobs = []
    for row in rows:
        if 'Stop' not in row or 'conso_outlet_finder' not in row:
            continue
        cells = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row, re.DOTALL)
        clean = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]
        clean = [c for c in clean if c]
        if len(clean) < 7:
            continue

        prefix = None
        for part in clean[3].split(','):
            if part.strip().startswith('prefix='):
                prefix = part.strip().split('=')[1]
                break

        jobs.append({
            'jobexec_id': clean[0],
            'job_id': clean[1],
            'spider': clean[2],
            'prefix': prefix,
            'args': clean[3],
            'runtime': clean[5],
            'started': clean[6],
        })
    return jobs


# ─────────────────── Actions ───────────────────

def action_status(args):
    auth = get_auth_token(args.region)
    project_id = find_project_id(auth, args.platform)
    if not project_id:
        print(f"❌  Project ConSo_{args.platform} not found.")
        sys.exit(1)

    jobs = parse_running_finder_jobs(auth, project_id)
    if args.prefix:
        filter_set = set(p.strip().upper() for p in args.prefix.split(','))
        jobs = [j for j in jobs if j['prefix'] in filter_set]

    if not jobs:
        print(f"ℹ️  No RUNNING finder jobs for {args.platform}.")
        return

    print(f"\n🕷️  RUNNING Finder Jobs — {args.platform} (project_id={project_id})\n")
    print(f"  {'Prefix':<8} {'Job ID':<10} {'Runtime':<15} {'Started':<22}")
    print(f"  {'─'*6:<8} {'─'*8:<10} {'─'*13:<15} {'─'*20:<22}")
    for j in sorted(jobs, key=lambda x: x['prefix'] or ''):
        print(f"  {j['prefix'] or '?':<8} {j['job_id']:<10} {j['runtime']:<15} {j['started']:<22}")
    print(f"\n  Total: {len(jobs)} running jobs")

    if args.with_mysql:
        print(f"\n  --- MySQL Cross-Check ---")
        check_sk = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'check_spiderkeeper.py')
        prefixes = ','.join(j['prefix'] for j in jobs if j['prefix'])
        cmd = [sys.executable, check_sk, '--platform', args.platform, '--with-mysql', '--prefixes', prefixes]
        subprocess.run(cmd)


def action_stop(args):
    auth = get_auth_token(args.region)
    project_id = find_project_id(auth, args.platform)
    if not project_id:
        print(f"❌  Project ConSo_{args.platform} not found.")
        sys.exit(1)

    jobs = parse_running_finder_jobs(auth, project_id)

    if args.all:
        targets = jobs
    elif args.prefix:
        filter_set = set(p.strip().upper() for p in args.prefix.split(','))
        targets = [j for j in jobs if j['prefix'] in filter_set]
    else:
        print("❌  Specify --prefix or --all.")
        sys.exit(1)

    if not targets:
        print(f"ℹ️  No matching RUNNING finder jobs to stop.")
        return

    # Get Spider EC2 instance for SSM
    instance_id = get_spider_instance_id(args.region)
    if not instance_id:
        print(f"❌  Spider EC2 instance not found. Cannot send cancel via SSM.")
        sys.exit(1)

    ssm = boto3.client('ssm', region_name=args.region)
    sk_project_name = f"ConSo_{args.platform}"

    print(f"\n🛑  Stopping {len(targets)} finder job(s) via SSM → scrapyd cancel\n")

    for job in targets:
        prefix = job['prefix'] or '?'
        job_id = job['job_id']
        cancel_cmd = f"curl -s -X POST http://localhost:5800/cancel.json -d 'project={sk_project_name}&job={job_id}'"

        print(f"  Stopping {prefix} (job {job_id})... ", end='', flush=True)

        try:
            resp = ssm.send_command(
                InstanceIds=[instance_id],
                DocumentName="AWS-RunShellScript",
                Parameters={"commands": [cancel_cmd]},
            )
            cmd_id = resp['Command']['CommandId']

            # Wait for result
            for _ in range(10):
                time.sleep(1)
                try:
                    inv = ssm.get_command_invocation(
                        CommandId=cmd_id, InstanceId=instance_id)
                    if inv['Status'] in ('Success', 'Failed', 'TimedOut', 'Cancelled'):
                        break
                except ssm.exceptions.InvocationDoesNotExist:
                    continue

            if inv['Status'] == 'Success':
                output = inv.get('StandardOutputContent', '').strip()
                if '"ok"' in output:
                    print(f"✅ stopped")
                else:
                    print(f"⚠️ scrapyd response: {output}")
            else:
                print(f"❌ SSM status: {inv['Status']}")
                if inv.get('StandardErrorContent'):
                    print(f"     stderr: {inv['StandardErrorContent'][:200]}")

        except Exception as e:
            print(f"❌ {e}")

    # Verify
    time.sleep(2)
    remaining = parse_running_finder_jobs(auth, project_id)
    remaining_prefixes = [j['prefix'] for j in remaining if j['prefix']]
    print(f"\n  Remaining RUNNING jobs: {len(remaining)} ({', '.join(sorted(remaining_prefixes))})")


def action_start(args):
    auth = get_auth_token(args.region)
    project_id = find_project_id(auth, args.platform)
    if not project_id:
        print(f"❌  Project ConSo_{args.platform} not found.")
        sys.exit(1)

    prefixes = [p.strip().upper() for p in args.prefix.split(',')]

    # Check for already running jobs for these prefixes
    running = parse_running_finder_jobs(auth, project_id)
    running_prefixes = {j['prefix'] for j in running if j['prefix']}

    conflicts = set(prefixes) & running_prefixes
    if conflicts and not args.force:
        print(f"⚠️  Finder already RUNNING for: {', '.join(sorted(conflicts))}")
        print(f"    Use --force to start duplicates (not recommended).")
        prefixes = [p for p in prefixes if p not in conflicts]
        if not prefixes:
            print(f"    No new jobs to start.")
            return

    print(f"\n🚀  Starting finder for {len(prefixes)} prefix(es)\n")

    for prefix in prefixes:
        spider_args = f"prefix={prefix}"
        print(f"  Starting {prefix}... ", end='', flush=True)

        try:
            resp = req_lib.post(
                f"{SK_HOST}/project/{project_id}/job/add",
                headers={"Authorization": auth},
                proxies=NO_PROXY,
                data={
                    "spider_name": "conso_outlet_finder",
                    "spider_arguments": spider_args,
                    "priority": "1",
                    "daemon": "http://localhost:5800",
                    "run_type": "onetime",
                },
                timeout=10,
                allow_redirects=False,
            )
            if resp.status_code in (200, 302):
                print(f"✅ scheduled")
            else:
                print(f"❌ HTTP {resp.status_code}: {resp.text[:100]}")
        except Exception as e:
            print(f"❌ {e}")

    # Verify
    time.sleep(3)
    running = parse_running_finder_jobs(auth, project_id)
    running_prefixes = sorted(j['prefix'] for j in running if j['prefix'])
    print(f"\n  Now RUNNING: {len(running)} jobs ({', '.join(running_prefixes)})")


def action_deploy(args):
    platform = args.platform
    project_dir = args.project_dir or os.getcwd()

    auth = get_auth_token(args.region)
    project_id = find_project_id(auth, platform)
    if not project_id:
        print(f"❌  Project ConSo_{platform} not found.")
        sys.exit(1)

    egg_path = os.path.join(project_dir, 'output.egg')

    # Step 1: Build .egg
    print(f"\n📦  Building .egg from {project_dir}...")
    result = subprocess.run(
        ['scrapyd-deploy', '--build-egg', 'output.egg'],
        cwd=project_dir,
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"❌  scrapyd-deploy failed:\n{result.stderr}")
        sys.exit(1)

    if not os.path.exists(egg_path):
        print(f"❌  output.egg not found at {egg_path}")
        sys.exit(1)

    egg_size = os.path.getsize(egg_path)
    print(f"  ✅ Built output.egg ({egg_size:,} bytes)")

    # Step 2: Seed session
    print(f"  Uploading to SpiderKeeper project {project_id}...")
    session = req_lib.Session()
    session.headers['Authorization'] = auth
    session.proxies = NO_PROXY

    session.get(f"{SK_HOST}/project/{project_id}/spider/deploy", timeout=10)

    # Step 3: Upload
    with open(egg_path, 'rb') as f:
        resp = session.post(
            f"{SK_HOST}/project/{project_id}/spider/upload",
            files={"file": ("output.egg", f)},
            timeout=30,
        )

    if 'deploy success' in resp.text.lower() or resp.status_code == 200:
        print(f"  ✅ Deployed successfully")
    else:
        print(f"  ❌ Deploy response: {resp.text[:200]}")
        sys.exit(1)

    # Step 4: Verify spiders
    resp = session.get(f"{SK_HOST}/api/projects/{project_id}/spiders", timeout=10)
    spiders = resp.json()
    spider_names = [s['spider_name'] for s in spiders]
    print(f"  Registered spiders: {', '.join(spider_names)}")

    # Cleanup
    os.remove(egg_path)
    print(f"  Cleaned up output.egg")


# ─────────────────── CLI ───────────────────

def main():
    parser = argparse.ArgumentParser(description='Manage SpiderKeeper finder jobs.')
    subparsers = parser.add_subparsers(dest='action', required=True)

    # status
    p_status = subparsers.add_parser('status', help='Show RUNNING finder jobs')
    p_status.add_argument('--platform', required=True)
    p_status.add_argument('--prefix', default=None, help='Filter by prefix(es), comma-separated')
    p_status.add_argument('--with-mysql', action='store_true')
    p_status.add_argument('--region', default='eu-central-1')

    # stop
    p_stop = subparsers.add_parser('stop', help='Stop finder job(s)')
    p_stop.add_argument('--platform', required=True)
    p_stop.add_argument('--prefix', default=None, help='Prefix(es) to stop, comma-separated')
    p_stop.add_argument('--all', action='store_true', help='Stop ALL running finders')
    p_stop.add_argument('--region', default='eu-central-1')

    # start
    p_start = subparsers.add_parser('start', help='Start finder job(s)')
    p_start.add_argument('--platform', required=True)
    p_start.add_argument('--prefix', required=True, help='Prefix(es) to start, comma-separated')
    p_start.add_argument('--force', action='store_true', help='Start even if already running')
    p_start.add_argument('--region', default='eu-central-1')

    # deploy
    p_deploy = subparsers.add_parser('deploy', help='Build .egg and upload to SpiderKeeper')
    p_deploy.add_argument('--platform', required=True)
    p_deploy.add_argument('--project-dir', default=None, help='Project root (default: cwd)')
    p_deploy.add_argument('--region', default='eu-central-1')

    args = parser.parse_args()

    actions = {
        'status': action_status,
        'stop': action_stop,
        'start': action_start,
        'deploy': action_deploy,
    }
    actions[args.action](args)


if __name__ == '__main__':
    main()

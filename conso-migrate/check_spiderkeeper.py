"""
Check SpiderKeeper finder status for a ConSo platform.

Queries SpiderKeeper dashboard to find RUNNING finder jobs, then cross-references
with MySQL write timestamps to classify each prefix as healthy, stalled, zombie,
or not running.

Used by:
  - conso-migrate  Phase 13.5 (post-deploy: is the finder actually running and writing?)
  - run-detail     Phase 0.6  (MySQL stale: is the finder stuck or not running?)
  - ad-hoc         diagnose any platform's finder health

Run from any directory (does NOT depend on scrapy.cfg or project venv):

    python ~/.claude/commands/conso-migrate/check_spiderkeeper.py --platform TKW

    # With MySQL cross-check (recommended)
    python ~/.claude/commands/conso-migrate/check_spiderkeeper.py --platform TKW --with-mysql

    # JSON output for scripting
    python ~/.claude/commands/conso-migrate/check_spiderkeeper.py --platform TKW --with-mysql --json

    # Check a specific prefix only
    python ~/.claude/commands/conso-migrate/check_spiderkeeper.py --platform TKW --prefixes AT

Requires: boto3, requests (both in dashmote-sourcing)
"""

import re
import json
import argparse
import subprocess
import sys
import os
from datetime import datetime, timedelta
from collections import defaultdict

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


def get_auth_token(region: str = 'eu-central-1') -> str:
    """Get SpiderKeeper auth token from AWS Secrets Manager."""
    try:
        from dashmote_sourcing.db import SecretsManager
        secret = SecretsManager.get_secret('Conso_SpiderKeeper', region_name=region)
        return secret['Authorization']
    except ImportError:
        # Fallback: use boto3 directly
        client = boto3.client('secretsmanager', region_name=region)
        resp = client.get_secret_value(SecretId='Conso_SpiderKeeper')
        secret = json.loads(resp['SecretString'])
        return secret['Authorization']


def find_project_id(auth: str, platform: str) -> int | None:
    """Find SpiderKeeper project_id for a platform."""
    resp = req_lib.get(
        f"{SK_HOST}/api/projects",
        headers={"Authorization": auth},
        proxies={"http": None, "https": None},
        timeout=10,
    )
    resp.raise_for_status()
    projects = resp.json()

    # Match ConSo_{PLATFORM} (case-insensitive)
    target = f"ConSo_{platform}".lower()
    for p in projects:
        if p['project_name'].lower() == target:
            return p['project_id']

    # Also try Conso_ and conso_ variants
    for p in projects:
        if p['project_name'].lower().replace('conso_', 'conso_') == target:
            return p['project_id']

    return None


def get_spiders(auth: str, project_id: int) -> list:
    """List spiders in a project."""
    resp = req_lib.get(
        f"{SK_HOST}/api/projects/{project_id}/spiders",
        headers={"Authorization": auth},
        proxies={"http": None, "https": None},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def parse_dashboard_running_jobs(auth: str, project_id: int, spider_filter: str = 'conso_outlet_finder') -> list:
    """Parse SpiderKeeper dashboard HTML to extract RUNNING jobs.

    The SpiderKeeper API (/api/projects/{id}/jobs) often returns empty,
    but the dashboard HTML contains the real data. RUNNING jobs have a
    'Stop' button in their row.
    """
    resp = req_lib.get(
        f"{SK_HOST}/project/{project_id}/job/dashboard",
        headers={"Authorization": auth},
        proxies={"http": None, "https": None},
        timeout=15,
    )
    resp.raise_for_status()
    html = resp.text

    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL)

    jobs = []
    for row in rows:
        # RUNNING jobs have a Stop button
        if 'Stop' not in row:
            continue

        cells = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row, re.DOTALL)
        if not cells:
            continue

        clean = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]
        clean = [c for c in clean if c]

        if not clean or spider_filter not in ' '.join(clean):
            continue

        if len(clean) < 7:
            continue

        # Parse args to extract prefix
        args_str = clean[3]
        prefix = None
        for part in args_str.split(','):
            part = part.strip()
            if part.startswith('prefix='):
                prefix = part.split('=')[1].strip()
                break

        # Parse runtime
        runtime_str = clean[5]

        # Parse started
        started_str = clean[6]
        started = None
        try:
            started = datetime.strptime(started_str, '%Y-%m-%d %H:%M:%S')
        except ValueError:
            pass

        jobs.append({
            'jobexec_id': clean[0],
            'job_id': clean[1],
            'spider': clean[2],
            'prefix': prefix,
            'args': args_str,
            'runtime': runtime_str,
            'started': started,
            'started_str': started_str,
        })

    return jobs


def get_mysql_status(platform: str, prefixes: list = None) -> dict:
    """Call check_mysql.py --json and return results keyed by prefix."""
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'check_mysql.py')
    cmd = [sys.executable, script, '--platform', platform, '--json']
    if prefixes:
        cmd.extend(['--prefixes', ','.join(prefixes)])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode in (0, 1) and result.stdout.strip():
            data = json.loads(result.stdout)
            return {r['prefix']: r for r in data}
    except Exception:
        pass
    return {}


def classify_prefix(sk_job: dict | None, mysql_data: dict | None) -> dict:
    """Classify a prefix's health based on SpiderKeeper + MySQL data.

    Returns a dict with 'status', 'icon', 'reason'.
    """
    has_sk = sk_job is not None
    has_mysql = mysql_data is not None and mysql_data.get('exists', False)

    if not has_sk and not has_mysql:
        return {'status': 'not_running_no_data', 'icon': '❌',
                'reason': 'Finder not running, no MySQL data. Never started for this prefix.'}

    if not has_sk and has_mysql:
        count = mysql_data.get('count', 0)
        lr = mysql_data.get('last_refresh', 'N/A')
        return {'status': 'not_running_has_data', 'icon': '⚠️',
                'reason': f'Finder not running. MySQL has {count:,} rows (last: {lr}). May need restart.'}

    # has_sk is True from here
    runtime = sk_job.get('runtime', '')
    started = sk_job.get('started_str', '?')

    if not has_mysql or mysql_data.get('count', 0) == 0:
        return {'status': 'running_no_writes', 'icon': '❌',
                'reason': f'Finder RUNNING since {started} ({runtime}) but 0 MySQL writes. Silent failure — check DB=PLATFORM and tablename.'}

    # Both running and has MySQL data — check freshness
    lr_str = mysql_data.get('last_refresh', '')
    if lr_str and lr_str != 'N/A':
        try:
            lr_dt = datetime.strptime(lr_str, '%Y-%m-%d %H:%M:%S')
            age = datetime.now() - lr_dt
            age_hours = age.total_seconds() / 3600

            if age.days >= 30:
                days = age.days
                return {'status': 'zombie', 'icon': '❌',
                        'reason': f'RUNNING since {started} ({runtime}) but MySQL last write {days}d ago. Zombie process — stop and restart.'}
            elif age_hours >= 24:
                d = age.days
                h = int((age.total_seconds() % 86400) / 3600)
                return {'status': 'stalled', 'icon': '⚠️',
                        'reason': f'RUNNING since {started} ({runtime}) but MySQL last write {d}d {h}h ago. Likely stalled — check Redis grids or proxy.'}
            else:
                return {'status': 'healthy', 'icon': '✅',
                        'reason': f'RUNNING since {started} ({runtime}). MySQL writing normally.'}
        except ValueError:
            pass

    return {'status': 'unknown', 'icon': '?',
            'reason': f'RUNNING since {started} ({runtime}). Could not parse MySQL last_refresh.'}


def format_age(dt_str: str) -> str:
    """Format a datetime string to precise age."""
    if not dt_str or dt_str == 'N/A':
        return 'N/A'
    try:
        dt = datetime.strptime(dt_str, '%Y-%m-%d %H:%M:%S')
        delta = datetime.now() - dt
        total_seconds = int(delta.total_seconds())
        days = delta.days
        hours = (total_seconds % 86400) // 3600
        minutes = (total_seconds % 3600) // 60
        if days > 0:
            return f'{days}d {hours}h {minutes}m'
        elif hours > 0:
            return f'{hours}h {minutes}m'
        else:
            return f'{minutes}m'
    except ValueError:
        return '?'


def main():
    parser = argparse.ArgumentParser(
        description='Check SpiderKeeper finder status for a ConSo platform.'
    )
    parser.add_argument('--platform', required=True,
                        help='Platform ID (e.g. TKW, EPL, DLR)')
    parser.add_argument('--prefixes', default=None,
                        help='Comma-separated prefix list (default: all found)')
    parser.add_argument('--with-mysql', action='store_true',
                        help='Cross-reference with MySQL write timestamps (recommended)')
    parser.add_argument('--region', default='eu-central-1',
                        help='AWS region (default: eu-central-1)')
    parser.add_argument('--json', action='store_true',
                        help='Output as JSON (for scripting)')
    args = parser.parse_args()

    platform = args.platform.upper()

    # 1. Get auth
    try:
        auth = get_auth_token(args.region)
    except Exception as e:
        print(f"❌  Failed to get SpiderKeeper auth token: {e}")
        sys.exit(1)

    # 2. Find project
    project_id = find_project_id(auth, platform)
    if project_id is None:
        print(f"❌  Project 'ConSo_{platform}' not found in SpiderKeeper.")
        sys.exit(1)

    # 3. Get RUNNING finder jobs
    try:
        sk_jobs = parse_dashboard_running_jobs(auth, project_id)
    except Exception as e:
        print(f"❌  Failed to parse SpiderKeeper dashboard: {e}")
        sys.exit(1)

    # Filter by prefix if specified
    filter_prefixes = None
    if args.prefixes:
        filter_prefixes = set(p.strip().upper() for p in args.prefixes.split(','))
        sk_jobs = [j for j in sk_jobs if j['prefix'] in filter_prefixes]

    # Index by prefix
    sk_by_prefix = {}
    for j in sk_jobs:
        if j['prefix']:
            sk_by_prefix[j['prefix']] = j

    # 4. Get MySQL data if requested
    mysql_by_prefix = {}
    if args.with_mysql:
        mysql_prefixes = list(filter_prefixes) if filter_prefixes else None
        mysql_by_prefix = get_mysql_status(platform, mysql_prefixes)

    # 5. Build unified prefix set
    all_prefixes = sorted(set(
        list(sk_by_prefix.keys()) +
        list(mysql_by_prefix.keys()) +
        (list(filter_prefixes) if filter_prefixes else [])
    ))

    if not all_prefixes:
        print(f"⚠️  No finder jobs or MySQL data found for {platform}.")
        sys.exit(0)

    # 6. Classify each prefix
    results = []
    for prefix in all_prefixes:
        sk_job = sk_by_prefix.get(prefix)
        mysql_data = mysql_by_prefix.get(prefix)
        classification = classify_prefix(sk_job, mysql_data)

        entry = {
            'prefix': prefix,
            'sk_running': sk_job is not None,
            'sk_runtime': sk_job['runtime'] if sk_job else None,
            'sk_started': sk_job['started_str'] if sk_job else None,
            'sk_jobexec_id': sk_job['jobexec_id'] if sk_job else None,
            'mysql_exists': mysql_data.get('exists', False) if mysql_data else False,
            'mysql_count': mysql_data.get('count', 0) if mysql_data else 0,
            'mysql_last_refresh': mysql_data.get('last_refresh', None) if mysql_data else None,
            'status': classification['status'],
            'icon': classification['icon'],
            'reason': classification['reason'],
        }
        results.append(entry)

    # 7. Output
    if args.json:
        print(json.dumps(results, indent=2, default=str))
        return

    print(f"\n🕷️  SpiderKeeper Finder Status — {platform}  (project_id={project_id})")
    if args.with_mysql:
        print(f"    Mode: SpiderKeeper + MySQL cross-check")
    else:
        print(f"    Mode: SpiderKeeper only (add --with-mysql for full diagnosis)")
    print()

    if args.with_mysql:
        print(f"  {'':>2} {'Prefix':<8} {'SK Status':<12} {'Runtime':<15} {'Started':<22} {'MySQL Rows':>12} {'Last Refresh':>22} {'Refresh Age':>15}")
        print(f"  {'':>2} {'─'*6:<8} {'─'*10:<12} {'─'*13:<15} {'─'*20:<22} {'─'*12:>12} {'─'*22:>22} {'─'*15:>15}")

        for r in results:
            sk_status = 'RUNNING' if r['sk_running'] else '—'
            runtime = r['sk_runtime'] or '—'
            started = r['sk_started'] or '—'
            mysql_rows = f"{r['mysql_count']:>12,}" if r['mysql_exists'] else f"{'—':>12}"
            lr = r['mysql_last_refresh'] or '—'
            lr_age = format_age(r['mysql_last_refresh']) if r['mysql_last_refresh'] and r['mysql_last_refresh'] != 'N/A' else '—'

            print(f"  {r['icon']} {r['prefix']:<8} {sk_status:<12} {runtime:<15} {started:<22} {mysql_rows} {lr:>22} {lr_age:>15}")
    else:
        print(f"  {'':>2} {'Prefix':<8} {'SK Status':<12} {'Runtime':<15} {'Started':<22} {'JobExec':<10}")
        print(f"  {'':>2} {'─'*6:<8} {'─'*10:<12} {'─'*13:<15} {'─'*20:<22} {'─'*8:<10}")

        for r in results:
            sk_status = 'RUNNING' if r['sk_running'] else '—'
            runtime = r['sk_runtime'] or '—'
            started = r['sk_started'] or '—'
            jobexec = r['sk_jobexec_id'] or '—'
            print(f"  {r['icon']} {r['prefix']:<8} {sk_status:<12} {runtime:<15} {started:<22} {jobexec:<10}")

    # Diagnosis summary
    print()
    for r in results:
        if r['status'] not in ('healthy',):
            print(f"  {r['icon']} {r['prefix']}: {r['reason']}")

    # Totals
    healthy = sum(1 for r in results if r['icon'] == '✅')
    warning = sum(1 for r in results if r['icon'] == '⚠️')
    error = sum(1 for r in results if r['icon'] == '❌')
    print(f"\n  ✅ {healthy}  ⚠️ {warning}  ❌ {error}")

    if error > 0:
        sys.exit(1)


if __name__ == '__main__':
    main()

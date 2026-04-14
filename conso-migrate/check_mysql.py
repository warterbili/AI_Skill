"""
Check MySQL finder write status for a ConSo platform.

Reports row count, last_refresh, last created_at per country/prefix table.
Used by:
  - conso-migrate  Phase 12 Step 1 (post-finder local validation)
  - conso-migrate  Phase 13.5 (post-deploy 15-min MySQL verification)
  - run-detail     pre-flight (confirm finder data exists before detail run)
  - ad-hoc         check any platform's MySQL health

Run from any directory (does NOT depend on scrapy.cfg or project venv):

    python ~/.claude/commands/conso-migrate/check_mysql.py --platform TKW

    # Check specific countries only
    python ~/.claude/commands/conso-migrate/check_mysql.py --platform TKW --prefixes NL,DE,AT

    # Use a different S3 config key
    python ~/.claude/commands/conso-migrate/check_mysql.py --platform TKW \\
        --config-key config/thuisbezorgd/config.json

    # Verify data is growing (run twice with --since to check delta)
    python ~/.claude/commands/conso-migrate/check_mysql.py --platform TKW --since 30

Requires: boto3, pymysql (both in dashmote-sourcing or installable standalone)
"""

import json
import argparse
import sys
from datetime import datetime, timezone, timedelta

try:
    import boto3
except ImportError:
    print("❌  boto3 not installed. Run: pip install boto3")
    sys.exit(1)

try:
    import pymysql
except ImportError:
    print("❌  pymysql not installed. Run: pip install pymysql")
    sys.exit(1)


# Default S3 config keys per platform (add new platforms here)
DEFAULT_CONFIG_KEYS = {
    # Most platforms use config/{platform_name}/config.json
    # TKW currently uses ubereats config (legacy)
    'TKW': 'config/ubereats/config.json',
}
FALLBACK_CONFIG_KEY = 'config/{platform_name}/config.json'


def get_mysql_config(config_key: str, region: str = 'eu-central-1') -> dict:
    """Fetch MySQL credentials from S3."""
    s3 = boto3.client('s3', region_name=region)
    obj = s3.get_object(Bucket='dash-dbcenter', Key=config_key)
    return json.loads(obj['Body'].read())


def get_connection(config: dict, database: str) -> pymysql.Connection:
    """Create MySQL connection."""
    return pymysql.connect(
        host=config['host'],
        user=config['user'],
        password=config['passwd'],
        database=database,
        connect_timeout=10,
        read_timeout=30,
    )


def discover_prefixes(conn: pymysql.Connection, database: str) -> list:
    """Auto-discover prefix tables in the platform database.

    Filters to only tables that look like 2-letter country codes (uppercase).
    """
    cur = conn.cursor()
    cur.execute("SHOW TABLES")
    tables = [row[0] for row in cur.fetchall()]
    cur.close()
    # Filter: 2-char uppercase = likely a country prefix table
    return sorted([t for t in tables if len(t) == 2 and t.isalpha() and t.isupper()])


def check_prefix(cur, prefix: str, since_minutes: int = 0) -> dict:
    """Check a single prefix table. Returns a result dict."""
    result = {
        'prefix': prefix,
        'exists': True,
        'count': 0,
        'last_refresh': None,
        'last_created': None,
        'recent_count': None,
        'error': None,
    }
    try:
        cur.execute(
            f"SELECT COUNT(*), MAX(last_refresh), MAX(created_at) FROM `{prefix}`"
        )
        row = cur.fetchone()
        result['count'] = row[0] or 0
        result['last_refresh'] = row[1]
        result['last_created'] = row[2]

        if since_minutes > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
            # last_refresh might be naive (no tz), compare as naive
            cutoff_naive = cutoff.replace(tzinfo=None)
            cur.execute(
                f"SELECT COUNT(*) FROM `{prefix}` WHERE last_refresh >= %s",
                (cutoff_naive,)
            )
            result['recent_count'] = cur.fetchone()[0]

    except pymysql.err.ProgrammingError as e:
        if 'doesn\'t exist' in str(e) or '1146' in str(e):
            result['exists'] = False
            result['error'] = 'table does not exist'
        else:
            result['error'] = str(e)
    except Exception as e:
        result['error'] = str(e)

    return result


def format_datetime(dt) -> str:
    if dt is None:
        return 'N/A'
    return dt.strftime('%Y-%m-%d %H:%M:%S')


def age_label(dt) -> str:
    """Human-readable age of a datetime."""
    if dt is None:
        return ''
    now = datetime.now()
    delta = now - dt
    if delta.total_seconds() < 3600:
        return f'{int(delta.total_seconds() / 60)}m ago'
    elif delta.days == 0:
        return f'{int(delta.total_seconds() / 3600)}h ago'
    elif delta.days < 30:
        return f'{delta.days}d ago'
    elif delta.days < 365:
        return f'{delta.days // 30}mo ago'
    else:
        return f'{delta.days // 365}y ago'


def status_icon(result: dict, since_minutes: int) -> str:
    """Determine status icon based on result."""
    if not result['exists']:
        return '❌'
    if result['error']:
        return '⚠️'
    if result['count'] == 0:
        return '❌'
    if result['last_refresh'] is None:
        return '⚠️'

    age = datetime.now() - result['last_refresh']
    if since_minutes > 0 and result['recent_count'] == 0:
        return '❌'  # No writes in the specified window
    if age.days >= 30:
        return '❌'  # Stale > 30 days
    if age.days >= 7:
        return '⚠️'  # Stale > 7 days
    return '✅'


def main():
    parser = argparse.ArgumentParser(
        description='Check MySQL finder write status for a ConSo platform.'
    )
    parser.add_argument('--platform', required=True,
                        help='Platform ID (e.g. TKW, EPL, DLR)')
    parser.add_argument('--prefixes', default=None,
                        help='Comma-separated prefix list (default: auto-discover)')
    parser.add_argument('--config-key', default=None,
                        help='S3 key for MySQL config (default: auto per platform)')
    parser.add_argument('--region', default='eu-central-1',
                        help='AWS region (default: eu-central-1)')
    parser.add_argument('--since', type=int, default=0,
                        help='Also count rows written in last N minutes (for growth check)')
    parser.add_argument('--json', action='store_true',
                        help='Output as JSON (for scripting)')
    args = parser.parse_args()

    platform = args.platform.upper()
    platform_name = platform.lower()

    # Resolve config key
    if args.config_key:
        config_key = args.config_key
    elif platform in DEFAULT_CONFIG_KEYS:
        config_key = DEFAULT_CONFIG_KEYS[platform]
    else:
        config_key = FALLBACK_CONFIG_KEY.format(platform_name=platform_name)

    # Connect
    try:
        config = get_mysql_config(config_key, args.region)
    except Exception as e:
        print(f"❌  Failed to fetch MySQL config from s3://dash-dbcenter/{config_key}: {e}")
        sys.exit(1)

    try:
        conn = get_connection(config, platform)
    except Exception as e:
        print(f"❌  Failed to connect to MySQL database '{platform}': {e}")
        sys.exit(1)

    # Discover or parse prefixes
    if args.prefixes:
        prefixes = [p.strip().upper() for p in args.prefixes.split(',')]
    else:
        prefixes = discover_prefixes(conn, platform)
        if not prefixes:
            print(f"⚠️  No prefix tables found in database '{platform}'.")
            conn.close()
            sys.exit(0)

    # Check each prefix
    cur = conn.cursor()
    results = []
    for prefix in prefixes:
        results.append(check_prefix(cur, prefix, args.since))
    cur.close()
    conn.close()

    # Output
    if args.json:
        import json as json_mod
        out = []
        for r in results:
            out.append({
                'prefix': r['prefix'],
                'exists': r['exists'],
                'count': r['count'],
                'last_refresh': format_datetime(r['last_refresh']),
                'last_created': format_datetime(r['last_created']),
                'recent_count': r['recent_count'],
                'error': r['error'],
            })
        print(json_mod.dumps(out, indent=2))
        return

    # Table output
    print(f"\n📊  MySQL Finder Status — {platform}")
    print(f"    Config: s3://dash-dbcenter/{config_key}")
    if args.since:
        print(f"    Growth window: last {args.since} minutes")
    print()

    hdr_since = f'  {"Recent":>8}' if args.since else ''
    print(f"  {'':>2} {'Prefix':<8} {'Rows':>10} {'Last Refresh':>22} {'Age':>10}{hdr_since}  {'Notes'}")
    print(f"  {'':>2} {'─' * 6:<8} {'─' * 10:>10} {'─' * 22:>22} {'─' * 10:>10}{'  ' + '─' * 8 if args.since else ''}  {'─' * 20}")

    for r in results:
        icon = status_icon(r, args.since)
        age = age_label(r['last_refresh'])
        since_col = f'  {r["recent_count"]:>8}' if args.since and r['recent_count'] is not None else (f'  {"—":>8}' if args.since else '')

        if not r['exists']:
            print(f"  {icon} {r['prefix']:<8} {'—':>10} {'—':>22} {'—':>10}{since_col}  table does not exist")
        elif r['error']:
            print(f"  {icon} {r['prefix']:<8} {'—':>10} {'—':>22} {'—':>10}{since_col}  {r['error'][:30]}")
        else:
            lr = format_datetime(r['last_refresh'])
            print(f"  {icon} {r['prefix']:<8} {r['count']:>10,} {lr:>22} {age:>10}{since_col}")

    # Summary
    total_rows = sum(r['count'] for r in results if r['exists'])
    ok = sum(1 for r in results if status_icon(r, args.since) == '✅')
    warn = sum(1 for r in results if status_icon(r, args.since) == '⚠️')
    fail = sum(1 for r in results if status_icon(r, args.since) == '❌')

    print(f"\n  Total: {total_rows:,} rows across {len(results)} tables")
    print(f"  ✅ {ok}  ⚠️ {warn}  ❌ {fail}")

    if fail > 0:
        sys.exit(1)


if __name__ == '__main__':
    main()

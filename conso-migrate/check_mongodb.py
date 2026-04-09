"""
Inspect MongoDB collections after a local ConSo spider test run.

Reads MONGODB_URI, PLATFORM_NAME, MONGODB_ITEM_MAPPINGS, and MONGODB_UNIQUE_KEYS
directly from the project's settings.py (auto-detected via scrapy.cfg).

Run from the project root (where scrapy.cfg lives):

    poetry run python ~/.claude/commands/conso-migrate/check_mongodb.py

Override the settings module if auto-detection fails:

    poetry run python ~/.claude/commands/conso-migrate/check_mongodb.py \\
        --settings KFC.settings
"""

import sys
import json
import argparse
import configparser
import importlib

from pymongo import MongoClient
from pymongo.errors import ConnectionFailure


def load_settings(module_path: str):
    try:
        return importlib.import_module(module_path)
    except ModuleNotFoundError as e:
        print(f"❌  Cannot import settings module '{module_path}': {e}")
        print("    Make sure you are running this script from the project root with poetry run.")
        sys.exit(1)


def detect_settings_module() -> str:
    cfg = configparser.ConfigParser()
    if not cfg.read('scrapy.cfg'):
        print("❌  scrapy.cfg not found. Run from the project root, or pass --settings explicitly.")
        sys.exit(1)
    try:
        return cfg['settings']['default']
    except KeyError:
        print("❌  scrapy.cfg has no [settings] default entry.")
        sys.exit(1)


def check_collection(db, table: str, unique_keys: dict) -> None:
    col = db[table]
    count = col.count_documents({})
    print(f"\n{'─' * 60}")
    print(f"  {table}  ({count} documents)")
    print(f"{'─' * 60}")

    if count == 0:
        print("  ⚠️  Collection is empty.")
        return

    sample = col.find_one()
    sample.pop('_id', None)

    # Validate index (unique key) fields from MONGODB_UNIQUE_KEYS
    required = unique_keys.get(table, [])
    if required:
        issues = [f for f in required if sample.get(f) is None]
        if issues:
            print(f"  ❌ Index field(s) are None or missing: {issues}")
        else:
            print(f"  ✅ Index fields OK: {required}")
    else:
        print(f"  ℹ️  No unique keys configured for this collection.")

    print("\n  Sample document:")
    for line in json.dumps(sample, indent=4, default=str, ensure_ascii=False).splitlines():
        print(f"  {line}")


def main():
    parser = argparse.ArgumentParser(description='Inspect MongoDB after local spider test.')
    parser.add_argument('--settings', default=None,
                        help='Dotted settings module path, e.g. KFC.settings '
                             '(default: auto-detected from scrapy.cfg)')
    args = parser.parse_args()

    module_path = args.settings or detect_settings_module()
    settings = load_settings(module_path)

    mongo_uri      = getattr(settings, 'MONGODB_URI', 'mongodb://localhost:27017')
    platform_name  = getattr(settings, 'PLATFORM_NAME', None)
    item_mappings  = getattr(settings, 'MONGODB_ITEM_MAPPINGS', {})
    unique_keys    = getattr(settings, 'MONGODB_UNIQUE_KEYS', {})

    if not platform_name:
        print("❌  PLATFORM_NAME not found in settings. Cannot determine database name.")
        sys.exit(1)

    if not item_mappings:
        print("❌  MONGODB_ITEM_MAPPINGS not found in settings. Nothing to inspect.")
        sys.exit(1)

    try:
        client = MongoClient(mongo_uri, serverSelectionTimeoutMS=3000)
        client.admin.command('ping')
    except ConnectionFailure:
        print(f"❌  Cannot connect to MongoDB at {mongo_uri}")
        print("    Start the service and retry:")
        print("      macOS : brew services start mongodb-community")
        print("      Docker: docker start mongodb")
        print("      Linux : sudo systemctl start mongod")
        sys.exit(1)

    db = client[platform_name]
    print(f"\nSettings : {module_path}")
    print(f"Database : {platform_name}  |  URI: {mongo_uri}")
    print(f"Collections to check: {list(item_mappings.keys())}")

    for table in item_mappings:
        check_collection(db, table, unique_keys)

    print(f"\n{'─' * 60}")
    print("Inspection complete. Review the sample documents above and")
    print("confirm the data looks correct before proceeding to Step 4.")


if __name__ == '__main__':
    main()

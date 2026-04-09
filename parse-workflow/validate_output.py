#!/usr/bin/env python3
"""
Parse Workflow — CSV Output Validator

Validates CSV files in result/ against the ConSo schema definition.
Checks: column names, Must fields, Not Null constraints, type correctness,
and duplicate ID detection.

Schema version: matches schema.md as of 2026-04-01.
If schema.md is updated, sync the DEFAULT_SCHEMA dict below.

Usage:
    python validate_output.py --result-dir /path/to/result/
    python validate_output.py --result-dir /path/to/result/ --schema-dir /path/to/temp/
    python validate_output.py --result-dir /path/to/result/ --strict
"""

import argparse
import csv
import os
import sys
from datetime import datetime

# ---------------------------------------------------------------------------
# Default schema (hardcoded from schema.md)
# Each field: (name, type, not_null, must)
# type: "str", "float", "int", "bool", "datetime"
# ---------------------------------------------------------------------------

DEFAULT_SCHEMA = {
    "outlet_information": [
        ("id_outlet", "str", True, True),
        ("id_platform", "str", True, True),
        ("platform", "str", True, True),
        ("url", "str", False, True),
        ("name", "str", True, True),
        ("description", "str", False, True),
        ("address", "str", False, True),
        ("street", "str", False, False),
        ("house_number", "str", False, False),
        ("postal_code", "str", False, False),
        ("city", "str", False, False),
        ("region", "str", False, False),
        ("country", "str", False, True),
        ("source_country", "str", True, True),
        ("name_local", "str", False, False),
        ("address_local", "str", False, False),
        ("lat", "float", False, True),
        ("lon", "float", False, True),
        ("category", "str", False, True),
        ("cuisine", "str", False, True),
        ("review_nr", "int", False, True),
        ("rating", "float", False, True),
        ("price_level", "str", False, True),
        ("telephone", "str", False, True),
        ("telephone_platform", "str", False, False),
        ("delivery_cost", "float", False, False),
        ("min_order_amount", "float", False, False),
        ("banner_img_url", "str", False, False),
        ("icon_url", "str", False, False),
        ("website", "str", False, False),
        ("opening_hours_physical", "str", False, True),
        ("opening_hours", "str", False, False),
        ("delivery_available", "bool", False, False),
        ("pickup_available", "bool", False, False),
        ("promotion", "str", False, False),
        ("is_promotion", "bool", False, False),
        ("is_convenience", "bool", False, False),
        ("is_new", "bool", False, False),
        ("chain_name", "str", False, False),
        ("chain_flag", "bool", False, False),
        ("id_chain", "str", False, False),
        ("chain_url", "str", False, False),
        ("flag_close", "bool", False, False),
        ("is_active", "bool", False, False),
        ("is_test", "bool", False, False),
        ("is_popular", "bool", False, False),
        ("menu_disabled", "bool", False, False),
        ("created_at", "datetime", True, True),
        ("last_refresh", "datetime", True, True),
    ],
    "outlet_meal": [
        ("id_meal", "str", True, True),
        ("id_outlet", "str", True, True),
        ("id_platform", "str", True, True),
        ("platform", "str", True, True),
        ("position", "str", True, True),
        ("category", "str", True, True),
        ("id_category", "str", False, False),
        ("menu", "str", False, False),
        ("id_menu", "str", False, False),
        ("price", "float", False, True),
        ("image_url", "str", False, True),
        ("name", "str", False, True),
        ("description", "str", False, True),
        ("choices", "str", False, False),
        ("has_options", "bool", False, False),
        ("out_of_stock", "bool", False, False),
        ("banner_category_img_url", "str", False, False),
        ("created_at", "datetime", True, True),
        ("last_refresh", "datetime", True, True),
    ],
    "meal_option": [
        ("id_option", "str", True, True),
        ("id_meal", "str", False, True),
        ("id_outlet", "str", True, True),
        ("id_platform", "str", True, True),
        ("platform", "str", True, True),
        ("category", "str", True, True),
        ("id_category", "str", False, False),
        ("price", "float", True, True),
        ("name", "str", True, True),
        ("description", "str", False, False),
        ("is_sold_out", "bool", False, False),
        ("created_at", "datetime", True, True),
        ("last_refresh", "datetime", True, True),
    ],
    "option_relation": [
        ("id_outlet", "str", True, True),
        ("id_platform", "str", True, True),
        ("id_meal", "str", True, True),
        ("id_option", "str", True, True),
        ("platform", "str", True, True),
        ("id_category", "str", True, True),
        ("option_level", "int", False, False),
        ("id_option_parent", "str", False, False),
        ("created_at", "datetime", True, True),
        ("last_refresh", "datetime", True, True),
    ],
}

# Unique key columns for duplicate detection per table
UNIQUE_KEYS = {
    "outlet_information": ["id_outlet"],
    "outlet_meal": ["id_outlet", "id_meal"],
    "meal_option": ["id_outlet", "id_option"],
    "option_relation": ["id_outlet", "id_meal", "id_option"],
}


def load_extended_schema(schema_dir: str) -> dict:
    """Load extended schema from CSV files in schema_dir, overriding defaults."""
    schema = dict(DEFAULT_SCHEMA)
    type_map = {"str": "str", "float": "float", "int": "int", "bool": "bool", "datetime": "datetime"}

    for table_name in DEFAULT_SCHEMA:
        csv_path = os.path.join(schema_dir, f"schema_{table_name}.csv")
        if not os.path.isfile(csv_path):
            continue
        fields = []
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                field = row.get("Field", "").strip()
                ftype = type_map.get(row.get("Type", "str").strip().lower(), "str")
                not_null = row.get("Not Null", "NO").strip().upper() == "YES"
                must = row.get("Must", "NO").strip().upper() == "YES"
                if field:
                    fields.append((field, ftype, not_null, must))
        if fields:
            schema[table_name] = fields
    return schema


def is_empty(val: str) -> bool:
    """Check if a CSV cell value is empty/null."""
    return val.strip() in ("", "None", "null", "NULL", "nan", "NaN")


def check_type(val: str, expected_type: str) -> bool:
    """Check if a non-empty CSV cell matches the expected type."""
    val = val.strip()
    if is_empty(val):
        return True  # null values are handled by not_null check

    if expected_type == "str":
        return True
    elif expected_type == "float":
        try:
            float(val)
            return True
        except ValueError:
            return False
    elif expected_type == "int":
        try:
            # Allow "1.0" style ints from CSV
            f = float(val)
            return f == int(f)
        except (ValueError, OverflowError):
            return False
    elif expected_type == "bool":
        return val in ("True", "False", "true", "false", "1", "0")
    elif expected_type == "datetime":
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                datetime.strptime(val, fmt)
                return True
            except ValueError:
                continue
        return False
    return True


def validate_table(table_name: str, csv_path: str, schema_fields: list, strict: bool) -> dict:
    """
    Validate a single CSV table against its schema.
    Returns dict with 'status' ('PASS'|'WARN'|'FAIL'), 'errors', 'warnings', 'stats'.
    """
    result = {"status": "PASS", "errors": [], "warnings": [], "stats": {}}

    if not os.path.isfile(csv_path):
        result["status"] = "FAIL"
        result["errors"].append(f"File not found: {csv_path}")
        return result

    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        rows = list(reader)

    result["stats"]["row_count"] = len(rows)

    if len(rows) == 0:
        result["status"] = "FAIL"
        result["errors"].append("CSV has no data rows (headers only or empty)")
        return result

    # --- Column name check ---
    schema_names = {f[0] for f in schema_fields}
    must_names = {f[0] for f in schema_fields if f[3]}
    header_set = set(headers)

    missing_must = must_names - header_set
    missing_other = (schema_names - header_set) - must_names
    extra = header_set - schema_names

    if missing_must:
        result["status"] = "FAIL"
        result["errors"].append(f"Missing Must columns: {sorted(missing_must)}")
    if missing_other:
        result["warnings"].append(f"Missing optional columns: {sorted(missing_other)}")
    if extra:
        result["warnings"].append(f"Extra columns not in schema: {sorted(extra)}")

    # Build lookup
    field_map = {f[0]: f for f in schema_fields}  # name -> (name, type, not_null, must)

    # --- Row-level checks ---
    not_null_violations = {}  # field -> count
    type_violations = {}  # field -> [(row_idx, value)]
    duplicate_keys = set()
    seen_keys = set()

    key_cols = UNIQUE_KEYS.get(table_name, [])

    for row_idx, row in enumerate(rows, start=2):  # row 2 = first data row (1-indexed, after header)
        # Not Null check
        for fname, ftype, not_null, must in schema_fields:
            if fname not in row:
                continue
            val = row[fname]
            if not_null and is_empty(val):
                not_null_violations[fname] = not_null_violations.get(fname, 0) + 1

            # Type check
            if not is_empty(val) and not check_type(val, ftype):
                if fname not in type_violations:
                    type_violations[fname] = []
                if len(type_violations[fname]) < 3:  # limit examples
                    type_violations[fname].append((row_idx, val))

        # Duplicate key check
        if key_cols and all(c in row for c in key_cols):
            key = tuple(row[c] for c in key_cols)
            if key in seen_keys:
                duplicate_keys.add(key)
            seen_keys.add(key)

    if not_null_violations:
        result["status"] = "FAIL"
        for fname, count in not_null_violations.items():
            result["errors"].append(f"Not Null violation: '{fname}' has {count} null values")

    if type_violations:
        level = "FAIL" if strict else "WARN"
        if level == "FAIL":
            result["status"] = "FAIL"
        elif result["status"] == "PASS":
            result["status"] = "WARN"
        for fname, examples in type_violations.items():
            expected = field_map[fname][1] if fname in field_map else "?"
            ex_str = ", ".join(f"row {r}: '{v}'" for r, v in examples)
            msg = f"Type mismatch: '{fname}' expected {expected}, got: {ex_str}"
            if strict:
                result["errors"].append(msg)
            else:
                result["warnings"].append(msg)

    if duplicate_keys:
        result["status"] = "FAIL"
        key_label = "+".join(key_cols)
        result["errors"].append(
            f"Duplicate keys ({key_label}): {len(duplicate_keys)} duplicates found"
        )
        # Show up to 3 examples
        for i, dk in enumerate(sorted(duplicate_keys, key=str)):
            if i >= 3:
                result["errors"].append(f"  ... and {len(duplicate_keys) - 3} more")
                break
            result["errors"].append(f"  duplicate: {dk}")

    result["stats"]["unique_key_count"] = len(seen_keys)
    result["stats"]["duplicate_count"] = len(duplicate_keys)

    return result


def validate_finder(csv_path: str) -> dict:
    """
    Validate finder_result.csv — no formal schema, just basic checks.
    """
    result = {"status": "PASS", "errors": [], "warnings": [], "stats": {}}

    if not os.path.isfile(csv_path):
        result["status"] = "FAIL"
        result["errors"].append(f"File not found: {csv_path}")
        return result

    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        rows = list(reader)

    result["stats"]["row_count"] = len(rows)

    if len(rows) == 0:
        result["status"] = "FAIL"
        result["errors"].append("CSV has no data rows")
        return result

    if "id_outlet" not in headers:
        result["status"] = "FAIL"
        result["errors"].append("Missing required column: id_outlet")
        return result

    # Duplicate id_outlet check
    seen = set()
    duplicates = set()
    for row in rows:
        oid = row.get("id_outlet", "")
        if oid in seen:
            duplicates.add(oid)
        seen.add(oid)

    if duplicates:
        result["status"] = "WARN"
        result["warnings"].append(
            f"Duplicate id_outlet: {len(duplicates)} duplicates "
            f"(e.g. {list(duplicates)[:3]})"
        )

    result["stats"]["unique_outlets"] = len(seen)
    result["stats"]["duplicate_count"] = len(duplicates)

    return result


def print_report(table_name: str, result: dict):
    """Print validation report for one table."""
    status = result["status"]
    icon = {"PASS": "PASS", "WARN": "WARN", "FAIL": "FAIL"}[status]
    print(f"\n{'='*60}")
    print(f"  [{icon}] {table_name}.csv")
    print(f"{'='*60}")

    stats = result.get("stats", {})
    if "row_count" in stats:
        print(f"  Rows: {stats['row_count']}")
    if "unique_key_count" in stats:
        print(f"  Unique keys: {stats['unique_key_count']}")
    if "unique_outlets" in stats:
        print(f"  Unique outlets: {stats['unique_outlets']}")

    for err in result["errors"]:
        print(f"  [ERROR] {err}")
    for warn in result["warnings"]:
        print(f"  [WARN]  {warn}")

    if not result["errors"] and not result["warnings"]:
        print("  All checks passed.")


def main():
    parser = argparse.ArgumentParser(
        description="Validate parse-workflow CSV output against ConSo schema"
    )
    parser.add_argument(
        "--result-dir", required=True, help="Path to the result/ directory containing CSV files"
    )
    parser.add_argument(
        "--schema-dir", default=None, help="Path to temp/ directory with extended schema CSVs (optional)"
    )
    parser.add_argument(
        "--strict", action="store_true", help="Treat warnings as errors"
    )
    args = parser.parse_args()

    if not os.path.isdir(args.result_dir):
        print(f"Error: result directory not found: {args.result_dir}")
        sys.exit(1)

    # Load schema
    schema = load_extended_schema(args.schema_dir) if args.schema_dir else dict(DEFAULT_SCHEMA)

    all_pass = True
    has_warn = False

    # --- Validate finder_result.csv ---
    finder_path = os.path.join(args.result_dir, "finder_result.csv")
    if os.path.isfile(finder_path):
        fr = validate_finder(finder_path)
        print_report("finder_result", fr)
        if fr["status"] == "FAIL":
            all_pass = False
        if fr["status"] == "WARN":
            has_warn = True
    else:
        print(f"\n  [SKIP] finder_result.csv not found — skipping")

    # --- Validate 4 standard tables ---
    for table_name, fields in schema.items():
        csv_path = os.path.join(args.result_dir, f"{table_name}.csv")
        if not os.path.isfile(csv_path):
            print(f"\n  [SKIP] {table_name}.csv not found — skipping")
            continue
        tr = validate_table(table_name, csv_path, fields, args.strict)
        print_report(table_name, tr)
        if tr["status"] == "FAIL":
            all_pass = False
        if tr["status"] == "WARN":
            has_warn = True

    # --- Summary ---
    print(f"\n{'='*60}")
    if all_pass and not has_warn:
        print("  RESULT: ALL PASSED")
    elif all_pass and has_warn:
        print("  RESULT: PASSED WITH WARNINGS")
    else:
        print("  RESULT: FAILED — fix errors above before proceeding")
    print(f"{'='*60}")

    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()

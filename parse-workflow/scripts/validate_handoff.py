#!/usr/bin/env python3
"""
Validate {work_dir}/handoff.json against the contract /conso-migrate expects.

This is the cross-skill contract — if it's broken, /conso-migrate will fail
(or worse, succeed with wrong data). Run at end of /parse-workflow Phase 6.

Output: single JSON object on stdout — narration on stderr.
Exit 0 if valid, 1 if invalid.

Usage:
    python validate_handoff.py --handoff /path/to/handoff.json
    python validate_handoff.py --handoff /path/to/handoff.json --quiet
"""

import argparse
import json
import os
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Schema definition — mirror what /conso-migrate reads
# ---------------------------------------------------------------------------

REQUIRED_FIELDS = {
    "platform":         str,
    "id_platform":      str,
    "source_country":   str,
    "country":          str,
    "work_dir":         str,
    "source_dir":       str,
    "has_finder":       bool,
    "has_detail":       bool,
    "is_single_endpoint": bool,
    "outputs":          dict,
    "finder_fields":    list,
    "validation_passed": bool,
    "completed_at":     str,
}

REQUIRED_OUTPUT_KEYS = [
    "finder_parse",
    "detail_parse",
    "scrapy_adapter",
    "project_analysis",
    "finder_analysis",
    "detail_analysis",
    "test_api",
    "coordinates",
]


# ---------------------------------------------------------------------------
# Output streams — JSON to stdout, narration to stderr
# ---------------------------------------------------------------------------

def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate(handoff_path: str) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    handoff_p = Path(handoff_path)

    if not handoff_p.exists():
        return {
            "verdict": "missing",
            "errors": [f"handoff.json not found at {handoff_path}"],
            "warnings": [],
        }

    try:
        data = json.loads(handoff_p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return {
            "verdict": "invalid_json",
            "errors": [f"handoff.json is not valid JSON: {e}"],
            "warnings": [],
        }

    if not isinstance(data, dict):
        return {
            "verdict": "invalid",
            "errors": ["handoff.json top-level must be a JSON object"],
            "warnings": [],
        }

    # ---- Field presence + types ----
    for field, expected_type in REQUIRED_FIELDS.items():
        if field not in data:
            errors.append(f"missing field: {field}")
            continue
        actual = data[field]
        if not isinstance(actual, expected_type):
            errors.append(
                f"field '{field}' has wrong type — got {type(actual).__name__}, "
                f"expected {expected_type.__name__}"
            )

    # ---- outputs sub-keys ----
    outputs = data.get("outputs")
    if isinstance(outputs, dict):
        missing = [k for k in REQUIRED_OUTPUT_KEYS if k not in outputs]
        if missing:
            errors.append(f"outputs missing keys: {missing}")

        # Path file-existence check (relative to work_dir)
        work_dir = data.get("work_dir", "")
        if work_dir:
            for key, rel_path in outputs.items():
                if not isinstance(rel_path, str):
                    warnings.append(f"outputs.{key} is not a string: {rel_path}")
                    continue
                abs_path = Path(work_dir) / rel_path
                if not abs_path.exists():
                    warnings.append(
                        f"outputs.{key} → '{rel_path}' does not exist at {abs_path}"
                    )

    # ---- finder_fields sanity ----
    ff = data.get("finder_fields")
    if isinstance(ff, list):
        if "id_outlet" not in ff:
            errors.append("finder_fields must contain 'id_outlet'")
        non_str = [f for f in ff if not isinstance(f, str)]
        if non_str:
            errors.append(f"finder_fields contains non-string entries: {non_str}")

    # ---- validation_passed must be true to call this 'complete' ----
    if data.get("validation_passed") is False:
        warnings.append(
            "validation_passed is False — handoff is technically structured "
            "correctly but the parse outputs did not pass validation"
        )

    # ---- platform constants consistency ----
    platform = data.get("platform", "")
    id_platform = data.get("id_platform", "")
    if platform and id_platform:
        # platform is lowercase (e.g. 'ifood'), id_platform is uppercase code (e.g. 'IFD')
        # Don't enforce case mapping (varies), just sanity-check non-empty
        if not platform.replace("_", "").replace("-", "").isalnum():
            warnings.append(f"platform name has unusual chars: {platform!r}")

    # ---- Verdict ----
    if errors:
        verdict = "invalid"
    elif warnings:
        verdict = "valid_with_warnings"
    else:
        verdict = "valid"

    return {
        "verdict":  verdict,
        "errors":   errors,
        "warnings": warnings,
        "summary": {
            "platform":           data.get("platform"),
            "id_platform":        data.get("id_platform"),
            "country":            data.get("country"),
            "has_finder":         data.get("has_finder"),
            "has_detail":         data.get("has_detail"),
            "is_single_endpoint": data.get("is_single_endpoint"),
            "validation_passed":  data.get("validation_passed"),
            "field_count":        len(data.get("finder_fields", [])),
            "output_count":       len(data.get("outputs", {})),
        },
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate handoff.json against /conso-migrate's expected contract."
    )
    parser.add_argument("--handoff", required=True, help="Path to handoff.json")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress stderr narration")
    args = parser.parse_args()

    if not args.quiet:
        _log(f"Validating {args.handoff}...")

    result = validate(args.handoff)
    print(json.dumps(result, indent=2))

    if not args.quiet:
        if result["verdict"] == "valid":
            _log("✅ valid")
        elif result["verdict"] == "valid_with_warnings":
            _log(f"⚠️  valid with {len(result['warnings'])} warnings")
        else:
            _log(f"❌ {result['verdict']} — {len(result['errors'])} errors")

    sys.exit(0 if result["verdict"] in ("valid", "valid_with_warnings") else 1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)

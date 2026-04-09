---
name: parse-workflow
description: "Food delivery platform field parse workflow. Use when writing parse code for a new platform, analyzing API response structures, or transforming platform data into standardized CSV tables. Trigger words: parse, field mapping, finder, detail, outlet data, delivery platform."
disable-model-invocation: true
argument-hint: "[platform-name] [work-dir] [reverse-project-path]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, Agent
---

# Parse Workflow — Food Delivery Platform Field Parsing

You are a **field parse engineer** responsible for transforming raw API responses from food delivery platforms into standardized output tables.

---

## Startup Flow

Upon invocation, immediately enter Phase 0 and prompt the user:

```
Hello! Parse workflow started. Please provide the following information:

1. Working directory (where code and results will be output)
   Example: /home/user/IFD_fields

2. Reverse-engineered project local path (where the completed reverse-engineered code is)
   Example: /home/user/sourcing-cracked/ifood-web

3. Platform information:
   - Platform name (lowercase English, e.g. ifood, ubereats, deliveroo)
   - Platform ID (internal number, e.g. "1")
   - Target country code (e.g. BR, GB, US)
```

If the user passed information via arguments (`$ARGUMENTS`), use them directly and skip the prompt.

Once all information is collected, **run Phase 0.5 (Resume Detection) to check for prior progress, then auto-execute from the determined Phase through Phase 6 until completion.**

---

## Execution Rules

### Flow Control
- **Strictly execute Phase 0 through Phase 6 in order** — Phase 0.5 (Resume Detection) may skip ahead if prior progress is detected
- Full workflow definition in [workflow.md](workflow.md)
- Only wait for user in these scenarios: Phase 0 input + Phase 5 launch confirmation + unresolvable blockers

### Language
- **All responses in Chinese**

### Efficiency
- Be concise and direct — no filler
- Do first, report after — don't discuss before acting
- Parallelize when possible (e.g. Finder and Detail test requests in Phase 2)
- Batch file reads/writes and multiple requests

### Automation
- **Auto-fix errors** — analyze errors, modify code, re-run automatically, up to 3 rounds
- **Auto-install missing deps** — `pip install` without confirmation
- **Auto-handle path issues** — create directories, handle encoding automatically
- **Auto-retry failed requests** — adjust parameters, change strategy
- **Auto-debug parse failures** — cross-reference response JSON and Schema to fix mappings
- Only escalate to user after 3 failed rounds

### Context Management
- Proactively compress context after completing each Phase
- Write analysis results to `doc/` rather than keeping them only in conversation
- Only load `stress-test-spec.md` when entering Phase 5

---

## Core Reference Documents

| Document | Purpose | When to Load |
|----------|---------|-------------|
| [workflow.md](workflow.md) | Complete Phase 0-6 execution flow (incl. Phase 0.5 resume detection) | At startup |
| [schema.md](schema.md) | Four table field definitions (output contract) | Phase 0 / Phase 3 |
| [conventions.md](conventions.md) | Coding standards and transformation rules | Phase 3 |
| [stress-test-spec.md](stress-test-spec.md) | Stress test detailed specification | Phase 5 only |
| [validate_output.py](validate_output.py) | CSV output auto-validator (schema, types, duplicates) | Phase 4 |

---

## Key Principles

- **`workflow.md` is the process** — follow it strictly in order
- **`schema.md` is the contract** — output must conform; check `{work_dir}/temp/` for extended Schema
- **`conventions.md` is the style guide** — follow all transformation rules
- **Persist knowledge to files** — write analysis results to `doc/` at each phase

## Finder Core Definition

The Finder's sole core responsibility: **obtain the outlet id_outlet list**. Extract accompanying fields if the API returns them, but id_outlet is mandatory.

## Output Structure

```
{work_dir}/
├── handoff.json   Cross-skill handoff file (platform info + output manifest for conso-migrate)
├── test/          Crawler test scripts (API test + stress test)
├── doc/           Knowledge documents (project analysis + JSON structure parsing)
├── location/      Test coordinates
├── temp/          Project-specific Schema (with additional fields, if any)
├── response/      Raw API response samples
├── parse/         finder_parse.py + detail_parse.py + scrapy_adapter.py
└── result/        5 CSV tables (finder_result + four standard tables)
```

## Important Notes

- Test scripts are written entirely based on the reverse-engineered project — every platform is different, no templates
- Parse scripts follow `conventions.md` coding standards, output CSV format
- JSON structure analysis documents must be extremely detailed — they are the core basis for parsing
- Test with real data; two rounds of passing required before completion
- Additional fields configured via `{work_dir}/extra_fields.json`, auto-detected in Phase 0
- Platform constants (PLATFORM, ID_PLATFORM, SOURCE_COUNTRY) provided by user in Phase 0

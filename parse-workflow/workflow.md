# Standard Parse Workflow

> Step-by-step execution guide for the AI agent.
> Strictly execute Phase 0 → 0.5 → (1) → 2 → 3 → (Pre-Phase 3 Gate) → 4 → (Pre-Phase 5 Gate) → 5 → 6 in order.
> Phase 0.5 (Resume Detection) may skip ahead. Phase 1 + parts of Phase 2 are skipped in **UPDATE mode**.

---

## Mode-aware Flow Summary

| Mode | When | Phases run |
|------|------|-----------|
| **NEW** | Brand new platform — no MySQL DB, no `handoff.json`, no state file | Phase 0 → 0.5 → 1 → 2 → 3 → Pre-Phase 3 Gate → 4 → Pre-Phase 5 Gate → 5 → 6 (full flow) |
| **UPDATE** | Platform already migrated — MySQL DB exists OR `handoff.json` exists | Phase 0 → 0.5 → ~~1~~ → 2 (limited to changed endpoints — ask user which) → 3 → Pre-Phase 3 Gate → 4 → Pre-Phase 5 Gate → 5 → 6 |

Mode is detected automatically in **Phase 0 Step 0.1.7**. The user may override.

When in UPDATE mode:
- Phase 1 is **skipped** — the prior `doc/project_analysis_summary.md` is still
  valid (we trust prior work; if anti-scraping changed, the user should say so).
- Phase 2 is **scoped** — ask the user "which endpoint(s) need re-testing?"
  Then re-test only those. Other endpoints' analysis docs remain authoritative.
- Phase 3-6 run normally — re-write only the parse functions for changed endpoints.

---

## Phase 0: Initialization

### Step 0.1: Collect User Input (parse args first, ask only for missing)

The skill's `argument-hint` accepts up to 5 positional args:
`[platform-name] [work-dir] [reverse-project-path] [platform-id] [country]`

**Procedure:**

1. **Parse `$ARGUMENTS`** — assign positional args to variables in order. Any
   trailing missing slots stay empty.
2. **Run Step 0.1.5 (Proactive preflight)** — this can fill in `platform_id`
   from MySQL and may suggest `platform_name` from `reverse_path` basename.
3. **After preflight**, look at what's still missing. Ask only for THOSE in a
   single prompt, showing detected defaults in `[brackets]` so user can
   accept by hitting Enter.

Example prompt when only `platform_id` and `country` are missing:

```
Detected:
  platform_name:  ifood        (from reverse_path basename + Preflight)
  reverse_path:   /home/user/sourcing-cracked/ifood-web
  work_dir:       /home/user/IFD_fields
  platform_id:    [Preflight could not query MySQL — please provide]
  country:        [missing — please provide]

Provide platform_id (e.g. "1") and country (2-letter code, e.g. BR):
```

If `$ARGUMENTS` is empty AND preflight detected nothing, fall back to the
original 5-field prompt.

### Step 0.1.5: Proactive Preflight (MySQL early-binding + cross-skill state)

Before asking the user, do everything we can to fill in defaults silently.

**Why this exists:** historically Step 3.1.5 queries MySQL for `ID_PLATFORM`
and finder schema, but Phase 3 is way too late — by then the user has already
told us a (potentially wrong) `platform_id`. Doing the query in Phase 0 means
we can **auto-fill `platform_id`** for existing platforms.

```bash
# Set up — needs $REVERSE_PATH (from args) and $DETECTED_PLATFORM (from SKILL.md preflight)
PLATFORM_GUESS="${PLATFORM_GUESS:-$DETECTED_PLATFORM}"

# ---- A. Query MySQL for matching platform (early-binding) ----
# Don't fail the whole skill if MySQL is unreachable — narrate, fall back.
MYSQL_DISCOVERY=$(python3 << PYEOF 2>&1
import json, sys
try:
    import boto3, pymysql
    s3 = boto3.client('s3', region_name='eu-central-1')
    obj = s3.get_object(Bucket='dash-dbcenter', Key='config/general_config/config.json')
    cfg = json.loads(obj['Body'].read())
    conn = pymysql.connect(
        host=cfg['host'], user=cfg['user'], password=cfg['passwd'],
        port=3306, connect_timeout=5,
    )
    cur = conn.cursor()
    # Find the database whose name (case-insensitive) matches our platform guess
    cur.execute("SHOW DATABASES")
    dbs = [r[0] for r in cur.fetchall()]
    guess = "${PLATFORM_GUESS}".upper()
    matched = next((d for d in dbs if d.upper() == guess or d.lower() == guess.lower()), None)

    out = {"mysql_available": True, "matched_db": matched, "candidates": dbs[:50]}

    if matched:
        cur.execute(f"USE `{matched}`")
        cur.execute("SHOW TABLES")
        tables = [r[0] for r in cur.fetchall()]
        out["tables"] = tables[:20]
        if tables:
            cur.execute(f"DESCRIBE `{tables[0]}`")
            out["finder_schema"] = [
                {"field": r[0], "type": r[1], "null": r[2]}
                for r in cur.fetchall()
            ]
    conn.close()
    print(json.dumps(out))
except Exception as e:
    print(json.dumps({"mysql_available": False, "error": str(e)[:200]}))
PYEOF
)

echo "$MYSQL_DISCOVERY" > "$STATE_DIR/mysql_discovery_${PLATFORM_GUESS:-unknown}.json"

# Parse for downstream Phase 0 + Phase 3
MYSQL_OK=$(echo "$MYSQL_DISCOVERY" | jq -r '.mysql_available')
MATCHED_DB=$(echo "$MYSQL_DISCOVERY" | jq -r '.matched_db // empty')

if [[ "$MYSQL_OK" == "true" && -n "$MATCHED_DB" ]]; then
    echo "✅ MySQL: platform '$MATCHED_DB' EXISTS — UPDATE-mode candidate"
    # ID_PLATFORM is the MySQL DB name (uppercase platform code)
    # Phase 3.1.5 will skip its re-query and reuse this discovery
elif [[ "$MYSQL_OK" == "true" ]]; then
    echo "ℹ️  MySQL reachable but no DB matches '$PLATFORM_GUESS' — NEW-mode candidate"
else
    ERR=$(echo "$MYSQL_DISCOVERY" | jq -r '.error')
    echo "⚠️  MySQL unreachable ($ERR) — Phase 3.1.5 will retry; user must provide platform_id manually"
fi

# ---- B. Read sibling skill state for cross-skill awareness ----
for sibling in conso-migrate run-detail id-refresh; do
    if [[ -d "$HOME/.claude/state/$sibling" ]]; then
        match=$(ls "$HOME/.claude/state/$sibling/${PLATFORM_GUESS}"*.json 2>/dev/null | head -1)
        [[ -n "$match" ]] && echo "  cross-skill: $sibling has state for $PLATFORM_GUESS"
    fi
done
```

### Step 0.1.7: Mode Detection (NEW vs UPDATE)

```bash
MODE="NEW"
REASONS=()

# Signal 1: handoff.json exists in work_dir
if [[ -f "$WORK_DIR/handoff.json" ]]; then
    MODE="UPDATE"
    REASONS+=("handoff.json present in $WORK_DIR")
fi

# Signal 2: MySQL has the platform DB
if [[ "$MYSQL_OK" == "true" && -n "$MATCHED_DB" ]]; then
    MODE="UPDATE"
    REASONS+=("MySQL DB '$MATCHED_DB' exists")
fi

# Signal 3: state file exists
if [[ -f "$STATE_DIR/${PLATFORM_GUESS}.json" ]]; then
    LAST_STEP=$(jq -r '.last_step // "?"' "$STATE_DIR/${PLATFORM_GUESS}.json")
    REASONS+=("state file present (last_step: $LAST_STEP)")
    # state file alone doesn't force UPDATE — could be a half-finished NEW run
fi

echo ""
echo "🎯 Mode: $MODE"
for r in "${REASONS[@]}"; do echo "    • $r"; done

if [[ "$MODE" == "UPDATE" ]]; then
    echo ""
    echo "    UPDATE mode:"
    echo "    - Phase 1 (analyze project)        → SKIPPED (already done previously)"
    echo "    - Phase 2 (API testing)            → LIMITED to changed endpoints (ask user which)"
    echo "    - Phase 3-6                         → run normally"
fi

export MODE
```

If user wants to override (e.g. force NEW for a re-do), they can say so before
Step 0.2. Don't proceed silently.

### Step 0.2: Validate Paths + Create Directories

### Step 0.2: Validate Paths + Create Directories

**Reverse-engineered project path:**
- Check if it exists; **if not, notify the user** and wait for the correct path
- AI only reads this directory — never modify any files

**Working directory:**
- If it doesn't exist, **create it automatically** (`mkdir -p`) — no need to wait for user
- Automatically create the following subdirectory structure:

```
{work_dir}/
├── test/              # Crawler test scripts
├── doc/               # Knowledge documents
├── location/          # Test coordinates
├── temp/              # Project-specific Schema (with additional fields, if any)
├── response/          # Raw API response archive
├── parse/             # Parse demo scripts
└── result/            # Parse output (5 CSV tables)
```

### Step 0.3: Set Platform Constants

Based on the platform information provided by the user, define the following constants (used in subsequent parse code):

```python
PLATFORM = "ifood"           # Platform name provided by user (lowercase)
ID_PLATFORM = "1"            # Platform ID provided by user
SOURCE_COUNTRY = "BR"        # Country code provided by user
COUNTRY = "BR"               # Same as above (usually identical)
```

Record these constants at the top of `{work_dir}/doc/project_analysis_summary.md` to ensure subsequent Phases can reference them directly.

### Step 0.4: Detect Additional Fields

Check if `{work_dir}/extra_fields.json` exists to determine if there are extra fields to add to the four tables.

**File format:**
```json
[
  { "field_name": "field description", "target_table": "outlet_information" },
  { "field_name": "field description", "target_table": "outlet_meal" }
]
```

Each object contains:
- The first key is the field name, its value is the field description
- `target_table` specifies which table the field should be added to

**Processing logic:**

1. Read `{work_dir}/extra_fields.json`
2. **File doesn't exist or is an empty array** -> No additional fields; use default [schema.md](schema.md) going forward
3. **Has additional fields** -> Execute the following:
   - Read the complete field definitions for all four tables from [schema.md](schema.md)
   - Append new fields to the end of the corresponding table (before `created_at` / `last_refresh`)
   - New field attributes: `Source = platform`, `Not Null = NO`, `Must = YES`
   - Generate four updated Schema tables, written as CSV to `{work_dir}/temp/`:
     ```
     {work_dir}/temp/
     ├── schema_outlet_information.csv
     ├── schema_outlet_meal.csv
     ├── schema_meal_option.csv
     └── schema_option_relation.csv
     ```
   - CSV columns: `Field, Type, Source, Not Null, Must, Description`

> **For all subsequent Phases that reference the Schema, first check whether schema CSV files exist in `{work_dir}/temp/`. If they exist, use the temp versions (which include additional fields); otherwise, use the default [schema.md](schema.md).**

---

## Phase 0.5: Resume Detection

> **Run automatically after Phase 0 completes.** Scans `{work_dir}` for existing outputs from a previous interrupted run and determines where to resume.

### Step 0.5.1: Scan Work Directory

Check the following directories and files in `{work_dir}`:

| Check | Path | What to look for |
|-------|------|-----------------|
| CSV results | `result/*.csv` | CSV files with data rows (not just headers) |
| Parse scripts | `parse/*.py` | `finder_parse.py` and/or `detail_parse.py` |
| Analysis docs | `doc/*_analysis.md` | Finder/Detail JSON structure analysis documents |
| Response samples | `response/*.json` | Raw API response JSON files |
| Project summary | `doc/project_analysis_summary.md` | Phase 1 knowledge document |
| Test scripts | `test/test_api.py` | API test script from Phase 1 |
| Coordinates | `location/coordinates.json` | Test coordinate file |

### Step 0.5.2: Determine Resume Point

Apply the following rules **in order** (first match wins). Rows checked include
both `{work_dir}` artifacts AND `~/.claude/state/parse-workflow/{platform}.json`
(cross-session signal).

| Condition | Resume From | Rationale |
|-----------|-------------|-----------|
| `handoff.json` exists AND state file says `last_step == "completed"` | **Done** — narrate "fully complete" + offer to re-validate or jump to next skill | Workflow finished previously; nothing left to do |
| `handoff.json` exists but state file says `last_step != "completed"` | Run `validate_handoff.py` first; if passes → **Phase 6 Step 6.3** (re-prompt for next skill); else **Phase 6 Step 6.2** (regenerate handoff) | Handoff exists but was never finalized |
| `test/stress_test.py` exists AND state file says stress test passed | **Phase 6** (handoff generation) | Stress test was the last thing; proceed to handoff |
| `result/` has CSV files containing data rows for all expected tables | **Phase 4** (re-validate) | Parse completed previously; re-validate with fresh data |
| `result/` has CSVs but only `finder_result.csv` has data rows (detail tables empty/headers-only) | **Phase 4 Step 4.1** (re-run detail parse) | Finder validated but detail parse needs testing |
| `parse/` has both `finder_parse.py` AND `detail_parse.py` | **Phase 4** (test them) | Both parse scripts complete; validate output |
| `parse/` has only `finder_parse.py` (no `detail_parse.py`) | **Phase 3 Step 3.3** (write detail parse) | Finder parse done; write detail parse next |
| `parse/` has only `detail_parse.py` (no `finder_parse.py`) | **Phase 3 Step 3.2** (write finder parse) | Detail parse done but no finder — unusual; write finder |
| `doc/` has `*_analysis.md` AND `response/` has JSON files | **Phase 3** (write parse) | API tested and analyzed; write parse scripts |
| `doc/project_analysis_summary.md` AND `test/test_api.py` exist | **Phase 2** (API testing) | Project analyzed; proceed to test APIs |
| `{work_dir}` exists but has only subdirectories or minimal content | **Phase 1** (analyze project) | Workspace initialized but no real work done |
| `{work_dir}` does not exist | **Phase 0** (start fresh) | Already handled by Phase 0 directory creation |

**Cross-session shortcut:** if `state_read $PLATFORM_GUESS` returns
`{"last_step": "phase_4_validated", ...}`, that's a stronger signal than file
scanning — use it directly to resume from the next phase.

```bash
# Check state file for cross-session resume hint
LAST_STATE=$(state_read "$PLATFORM_GUESS")
LAST_STEP=$(echo "$LAST_STATE" | jq -r '.last_step // empty')
LAST_TS=$(echo   "$LAST_STATE" | jq -r '.last_ts   // empty')
if [[ -n "$LAST_STEP" ]]; then
    AGE_HOURS=$(python3 -c "
from datetime import datetime,timezone
t = datetime.fromisoformat('${LAST_TS}'.replace('Z','+00:00'))
print(int((datetime.now(timezone.utc) - t).total_seconds() / 3600))
" 2>/dev/null || echo "?")
    echo "📂 State file says: last_step='$LAST_STEP' (${AGE_HOURS}h ago)"
fi
```

### Step 0.5.3: Notify and Resume

If resuming from a phase later than Phase 1, notify the user:

```
Detected existing progress in {work_dir}:
- Found: [list what was found]
- Resuming from: Phase {N} ({phase name})

Proceeding automatically...
```

Then **skip directly to the determined Phase** and continue the normal sequential flow from there.

If no prior progress is detected (fresh start), proceed silently to Phase 1.

> **Note:** Resume detection reads existing files but never deletes them. Parse results from a resumed run will append to existing CSVs (consistent with normal append behavior).

---

## Phase 1: Analyze Reverse-Engineered Project

**Goal:** Thoroughly understand the reverse-engineered project and extract all necessary information.

### Step 1.1: Assess Reverse Engineering Completeness
- Scan the overall structure of the reverse-engineered project
- Check for complete, runnable crawler code
- Confirm all key components are present (cookie generation, signature algorithms, proxy configuration, etc.)
- If reverse engineering is incomplete, **immediately notify the user** — do not continue

### Step 1.2: Understand Anti-Scraping Flow and Bypass Methods
Deep dive into:
- What anti-scraping mechanisms does the platform use? (PerimeterX, CloudFlare, WAF, signature verification, etc.)
- How did the reverse engineering team bypass them? (pure computation, browser environment simulation, Web Unlocker, etc.)
- Cookie/Token generation flow and validity period
- IP/Session binding restrictions

### Step 1.3: Identify Finder and Detail Endpoints

**Finder endpoint** (outlet discovery/listing):
- URL, HTTP Method, Query Parameters, Request Body
- Headers (all required headers)
- Authentication method (cookie name, token header)
- Pagination mechanism (cursor, offset, page)
  - Pagination signal: How does the API indicate more pages? (next cursor token, hasMore boolean, total count + offset, URL for next page)
  - Maximum results per page (observed and documented limit)
  - Termination condition: When to stop paginating (empty results, cursor is null, offset >= total)
- Search area definition method (coordinate grid, city ID, etc.)

**Detail endpoint** (outlet details, may have multiple):
- Outlet basic info endpoint, menu/catalog endpoint, option/modifier endpoint
- URL, Method, Headers, Params for each endpoint
- Dependencies between endpoints

**Single-endpoint platform detection:**
- If the Finder endpoint already returns complete outlet details (including menu, options), mark as "single-endpoint platform"
- Subsequent Phase 2 will skip independent Detail testing and reuse Finder response data

### Step 1.4: Build Crawler Test Scripts
Based on the analysis from Steps 1.1-1.3, write test scripts in `{work_dir}/test/`:
- **Reuse** authentication/cookie generation code from the reverse-engineered project — don't reinvent the wheel
- Scripts must be independently runnable
- Include finder request function and detail request function
- Include necessary proxy, headers, and cookie configuration

> **Note:** Test scripts are written entirely based on the actual reverse-engineered project. Every platform is different — do not use templates.

**Test script must include all of the following:**

- [ ] Import and call the reverse-engineered project's auth/cookie generation code
- [ ] Proxy configuration (matching the reverse-engineered project's setup)
- [ ] All required headers (User-Agent, platform-specific headers, auth tokens)
- [ ] A `test_finder(lat, lon)` function that sends a single Finder request and returns the raw response dict
- [ ] A `test_detail(id_outlet)` function that sends Detail request(s) for one outlet and returns the merged raw response dict
- [ ] Coordinate loading from `{work_dir}/location/coordinates.json`
- [ ] Response saving to `{work_dir}/response/` with descriptive filenames
- [ ] Basic response validation (HTTP status check, non-empty body check)
- [ ] Request interval (1-3 second sleep between requests)
- [ ] Error handling with informative error messages (not bare exceptions)

### Step 1.5: Generate Test Coordinates (WebSearch-driven, not pre-bundled)

The Finder endpoint requires latitude/longitude coordinates. **Do NOT use my
own knowledge for this** — my training data is dated and biased toward popular
markets. Use real-time **WebSearch** to find current restaurant-dense areas
for the target country.

**Procedure:**

1. **First check** `{work_dir}/location/coordinates.json` — if it exists, REUSE
   it. Don't re-search every run. Resume-friendly + idempotent.

2. **If missing**, run WebSearch in 3 parallel queries:
   ```
   WebSearch: "popular food delivery areas {country} 2026"
   WebSearch: "best restaurant districts {country} major cities"
   WebSearch: "{country} food scene cities high density"
   ```
   Parse search snippets to extract candidate cities (target 4-6 distinct cities).

3. **For each candidate city, WebSearch precise coordinates:**
   ```
   WebSearch: "{city} {country} city center latitude longitude"
   WebSearch: "{food district name} {city} coordinates"   # if a specific district was named
   ```
   Take the most-cited lat/lon (often appears in Wikipedia / Google / GeoNames).

4. **Validate the generated set** before writing:
   - ✅ At least **3 distinct cities** (avoid single-city bias)
   - ✅ Each city: 1-3 coordinates, total **10-20 points**
   - ✅ All lat/lon within the country's rough geographic bounds (sanity check —
     for example, Brazil lat is between roughly -34 and 5)
   - ✅ Coordinates look like real numbers (not 0,0 or obviously rounded centroids)

5. **Write** to `{work_dir}/location/coordinates.json` with `note` field
   citing the WebSearch source / district reason.

6. **Narrate** what was selected:
   ```
   📍 Generated 12 test coordinates for {country} via WebSearch:
       - Sao Paulo (3 coords): Centro / Vila Madalena / Itaim Bibi
       - Rio de Janeiro (3 coords): Centro / Copacabana / Ipanema
       - Brasilia (2 coords): Asa Sul / Asa Norte
       - Belo Horizonte (2 coords): Savassi / Funcionários
       - Salvador (2 coords): Pituba / Barra
   ```

**Fallback when WebSearch is unavailable** (rate-limited / network blocked):

```bash
# Tiny embedded dictionary for the most common business markets — only used
# when WebSearch fails. Covers ~80% of historical platforms.
case "$COUNTRY" in
    BR) FALLBACK='[{"lat":-23.5505,"lng":-46.6333,"city":"Sao Paulo","note":"Centro"},
                   {"lat":-22.9068,"lng":-43.1729,"city":"Rio de Janeiro","note":"Centro"},
                   {"lat":-15.7942,"lng":-47.8822,"city":"Brasilia","note":"Centro"}]';;
    US) FALLBACK='[{"lat":40.7589,"lng":-73.9851,"city":"New York","note":"Times Square"},
                   {"lat":34.0522,"lng":-118.2437,"city":"Los Angeles","note":"Downtown"},
                   {"lat":41.8781,"lng":-87.6298,"city":"Chicago","note":"Loop"}]';;
    GB) FALLBACK='[{"lat":51.5074,"lng":-0.1278,"city":"London","note":"Soho"},
                   {"lat":53.4808,"lng":-2.2426,"city":"Manchester","note":"Northern Quarter"},
                   {"lat":55.9533,"lng":-3.1883,"city":"Edinburgh","note":"Old Town"}]';;
    DE) FALLBACK='[{"lat":52.5200,"lng":13.4050,"city":"Berlin","note":"Mitte"},
                   {"lat":48.1351,"lng":11.5820,"city":"Munich","note":"Altstadt"},
                   {"lat":53.5511,"lng":9.9937,"city":"Hamburg","note":"Sankt Pauli"}]';;
    FR) FALLBACK='[{"lat":48.8566,"lng":2.3522,"city":"Paris","note":"Le Marais"},
                   {"lat":45.7640,"lng":4.8357,"city":"Lyon","note":"Vieux Lyon"},
                   {"lat":43.2965,"lng":5.3698,"city":"Marseille","note":"Vieux Port"}]';;
    *)  echo "❌ WebSearch unavailable AND no fallback for country=$COUNTRY"
        echo "   Please provide coordinates manually as JSON, OR try Phase 2 with a single coordinate"
        exit 1;;
esac
echo "$FALLBACK" > "$WORK_DIR/location/coordinates.json"
echo "⚠️  Used hardcoded fallback for $COUNTRY (3 cities). WebSearch failed."
```

> All subsequent steps that need coordinates should read from
> `{work_dir}/location/coordinates.json` uniformly — do not hardcode in scripts.

**Inline error decisions:**

| If you see… | Likely cause | Do |
|---|---|---|
| WebSearch returns no useful hits for `{country}` | Country name spelling / niche market | Try synonyms (e.g. "UK" vs "United Kingdom"); ask user |
| Coordinates outside country bounds | Search returned a tourist-named place in another country | Re-search with `{city}, {country}` qualifier |
| All coordinates clustered in one city | Search bias | Force diversity: search "second largest cities {country}" |
| Phase 2 finder returns 0 outlets at every coord | Bad coordinates OR platform's coverage doesn't include those cities | Check platform's coverage map; user may know the right cities |

### Step 1.6: Knowledge Persistence
Write all findings above to `{work_dir}/doc/project_analysis_summary.md`:
- Platform constants at the top (PLATFORM, ID_PLATFORM, SOURCE_COUNTRY, COUNTRY)
- Anti-scraping mechanisms, bypass methods, endpoint list, authentication flow, key code locations
- Whether it's a single-endpoint platform
- This document serves as the foundational knowledge base for all subsequent work

---

## Phase 2: API Testing + Response Analysis

**Goal:** Verify Finder/Detail endpoints work, collect samples, and deeply analyze response structures.

> **This Phase merges Finder and Detail testing and analysis. Requests can be pipelined: extract id_outlet from Finder response and immediately issue Detail requests — no need to wait for Finder analysis to complete.**

### Step 2.1: Finder Testing
- Use the test scripts written in Phase 1
- Send **at least 3** Finder requests (using different coordinates/regions)
- Confirm each returns HTTP 200 with an outlet list in the response
- If the Finder API is paginated, test **at least 2 consecutive pages** for one coordinate to verify:
  - Pagination token/cursor is present in the response
  - Second page returns different outlets than the first
  - End-of-pagination signal is identifiable
  - Save paginated responses as `response/finder1_page1.json`, `response/finder1_page2.json`
- Save raw responses to `response/finder1.json` through `finder3.json`

### Step 2.2: Detail Testing

**Standard platform (dual-endpoint):**
- Extract 3 `id_outlet` values from Finder responses
- Send Detail requests (if multiple sub-endpoints exist, request all of them)
- Merge multiple endpoint responses for the same outlet into a single JSON
- Save to `response/detail1.json` through `detail3.json`

**Single-endpoint platform:**
- Skip Detail requests; subsequent analysis uses detail data from Finder responses
- Note in doc: "Detail data sourced from Finder response"

### Step 2.3: Finder Response Analysis

Compare the 3 Finder responses for a complete structural analysis:

1. **Comparative analysis** — Which fields are static? Which are dynamic? What JSON path contains the outlet list? Where is pagination info?

2. **Field-by-field JSON structure parsing** (must be extremely detailed):
   - Complete nested hierarchy tree, expanding every level
   - Each field: field name, data type, meaning/purpose, example value, whether it can be null
   - Array fields: describe the complete element structure
   - Annotate field mappings: which fields correspond to which columns in the Schema's four tables
   - Annotate fields requiring transformation (cents->price, enum->bool, array->semicolon_string)

3. **Field semantic validation** (cannot be skipped):
   - **Semantic matching**: Verify that each API field -> Schema field mapping truly corresponds semantically
     - Key areas prone to confusion: `description` vs `name`, `category` vs `cuisine`, price units (currency/cents), ID ownership, rating scale (0-5/0-10)
   - **Path extraction verification**: Use real responses to confirm JSON paths can retrieve values layer by layer
     - Confirm array content types, null/missing scenarios, stability across all 3 responses
   - Mark validation results as `Verified` or `Caution: {reason}`

4. **Write to knowledge document**: `{work_dir}/doc/finder_response_json_structure_analysis.md`

Document structure:
```markdown
# Finder Response JSON Structure Analysis

## Overview
- API URL / request method / number of outlets in response

## JSON Structure Overview (nested hierarchy tree)

## Field-by-Field Analysis
### Top-level fields
### Outlet list (path.to.merchants[])
### Outlet object fields
### Nested object fields

## Field Mapping Table
| JSON Path | Schema Table | Schema Field | Transform | Validation |
|-----------|-------------|-------------|-----------|------------|

## Notes
```

### Step 2.4: Detail Response Analysis

Apply the same standards as Step 2.3 for complete analysis of Detail responses.

Additional focus areas:
- Menu nesting hierarchy: category -> item -> option group -> option; which table each level's ID/name maps to
- Options vs option groups: don't confuse them
- Price units: may differ across sub-endpoints — verify each one
- Item name field: some platforms use `description` as the item name rather than description
- `id_meal` and `id_option` association relationships

Write to: `{work_dir}/doc/detail_response_json_structure_analysis.md`

> Single-endpoint platform: Analyze the detail portion within the Finder response separately; still output the detail analysis document.

### Step 2.5: Troubleshooting

| Symptom | Possible Cause | Resolution |
|---------|---------------|------------|
| 403 | Cookie/auth expired | Check auth generation, ensure fresh cookies |
| 403 after first request | IP/Session mismatch | Ensure cookie generation and request use the same proxy session |
| 401 | Auth header missing / wrong token | Check the auth-header generation in reverse-engineered code |
| 429 | Rate limiting | Increase request interval (1-3 seconds) |
| 200 but empty data | Incorrect coordinates/params | Use verified coordinates from reverse-engineered project |
| Connection timeout | Proxy issues | Check proxy configuration |
| SSL error | TLS fingerprint detection | Use curl_cffi browser fingerprinting |
| Cloudflare challenge HTML in response body | CF JS challenge | Check whether reverse project handles JS challenge; use Web Unlocker if needed |
| Captcha required | Anti-bot escalation | Cannot bypass in script — escalate to user / reverse-engineering team |
| 451 / 403 with country mention | Geographic block | Use proxy in correct country; verify proxy region |

---

## Pre-Phase 3 Gate (STOP-style, all 4 must pass)

Before writing parse code, verify that we have everything needed to write GOOD
code — not just code that runs.

```
G1 Response samples valid       — At least 3 finder + 3 detail JSONs in response/  [✅ / ❌]
G2 Schema source determined     — Either temp/schema_*.csv OR default schema.md    [✅ / ❌]
G3 JSON analysis docs complete  — Both *_analysis.md files have field mapping table [✅ / ❌]
G4 Platform constants resolved  — PLATFORM, ID_PLATFORM, SOURCE_COUNTRY in doc/    [✅ / ❌]
```

Any FAIL → STOP, narrate which gate, fix the underlying phase before proceeding.

```bash
# G1
F=$(ls "$WORK_DIR/response/finder"*.json 2>/dev/null | wc -l)
D=$(ls "$WORK_DIR/response/detail"*.json 2>/dev/null | wc -l)
[[ "$F" -ge 3 ]] && echo "✅ G1.finder ($F samples)" || echo "❌ G1.finder ($F < 3)"
[[ "$D" -ge 3 ]] && echo "✅ G1.detail ($D samples)" || echo "❌ G1.detail ($D < 3)"

# G2
if [[ -f "$WORK_DIR/temp/schema_outlet_information.csv" ]]; then
    echo "✅ G2 schema source: $WORK_DIR/temp/ (extra fields included)"
else
    echo "✅ G2 schema source: default schema.md"
fi

# G3
[[ -f "$WORK_DIR/doc/finder_response_json_structure_analysis.md" ]] \
    && echo "✅ G3.finder analysis exists" || echo "❌ G3.finder missing"
[[ -f "$WORK_DIR/doc/detail_response_json_structure_analysis.md" ]] \
    && echo "✅ G3.detail analysis exists" || echo "❌ G3.detail missing"

# G4
grep -qE "^- PLATFORM:" "$WORK_DIR/doc/project_analysis_summary.md" 2>/dev/null \
    && echo "✅ G4 platform constants in doc" || echo "❌ G4 constants missing"
```

---

## Phase 3: Write Parse Demo

**Goal:** Based on knowledge documents, write finder and detail parse scripts.

### Step 3.1: Preparation
Before writing, you must read:
- **Schema source**: First check if `schema_*.csv` files exist in `{work_dir}/temp/`. If yes, use the temp versions; if not, use default [schema.md](schema.md)
- [conventions.md](conventions.md) — coding standards and transformation rules
- `{work_dir}/doc/finder_response_json_structure_analysis.md`
- `{work_dir}/doc/detail_response_json_structure_analysis.md`
- `{work_dir}/doc/project_analysis_summary.md` — to retrieve platform constants

### Step 3.1.5: Reuse MySQL Finder Schema from Phase 0 Preflight

The MySQL finder-schema query was already executed in **Phase 0 Step 0.1.5**
(early-binding) and the result was saved to:

```
$STATE_DIR/mysql_discovery_${PLATFORM}.json
```

Reuse it here — no need to re-query:

```bash
DISCOVERY=$(cat "$STATE_DIR/mysql_discovery_${PLATFORM_GUESS:-${PLATFORM}}.json" 2>/dev/null)

if [[ -z "$DISCOVERY" ]]; then
    echo "⚠️  Phase 0 didn't run preflight (or state file missing). Re-running query inline:"
    # (Same Python block as Step 0.1.5, abbreviated; see there for full version)
    DISCOVERY=$(python3 << PYEOF
import json, boto3, pymysql
try:
    s3 = boto3.client('s3', region_name='eu-central-1')
    obj = s3.get_object(Bucket='dash-dbcenter', Key='config/general_config/config.json')
    cfg = json.loads(obj['Body'].read())
    conn = pymysql.connect(host=cfg['host'], user=cfg['user'], password=cfg['passwd'],
                            port=3306, database='${ID_PLATFORM}', connect_timeout=10)
    cur = conn.cursor()
    cur.execute('SHOW TABLES')
    tables = [r[0] for r in cur.fetchall()]
    out = {"matched_db": "${ID_PLATFORM}", "tables": tables[:20]}
    if tables:
        cur.execute(f"DESCRIBE \`{tables[0]}\`")
        out["finder_schema"] = [{"field": r[0], "type": r[1], "null": r[2]} for r in cur.fetchall()]
    conn.close()
    print(json.dumps(out))
except Exception as e:
    print(json.dumps({"error": str(e)[:200]}))
PYEOF
)
fi

# Extract finder schema fields (column names other than auto-managed dashmote columns)
FINDER_FIELDS=$(echo "$DISCOVERY" | jq -r '
    if .finder_schema then
        .finder_schema | map(.field) | map(select(. != "created_at" and . != "last_refresh"))
    else []
    end | join(",")
')

if [[ -n "$FINDER_FIELDS" ]]; then
    echo "✅ MySQL finder schema (Phase 0): $FINDER_FIELDS"
    # Persist a human-readable doc
    echo "$DISCOVERY" | jq '.finder_schema // []' > "$WORK_DIR/doc/finder_table_schema.md.tmp"
    {
        echo "# Finder Table Schema (from MySQL ${ID_PLATFORM})"
        echo ""
        echo "Required output columns of \`finder_parse.py\` (extracted in Phase 0 preflight):"
        echo ""
        echo "$DISCOVERY" | jq -r '.finder_schema[] | "- `\(.field)` (\(.type), null=\(.null))"'
    } > "$WORK_DIR/doc/finder_table_schema.md"
    rm -f "$WORK_DIR/doc/finder_table_schema.md.tmp"
else
    echo "ℹ️  No MySQL finder schema found — NEW platform."
    echo "    finder_parse.py output: id_outlet (mandatory) + any extra fields the API returns"
    echo "    that match outlet_information schema (rating, cuisine, lat, lon, …)"
fi
```

**Decision summary:**
- Platform **exists in MySQL** → finder must output exactly the columns listed
  in `finder_table_schema.md`. `id_outlet` is always the primary key.
- Platform **brand new** (no MySQL table) → minimum is `id_outlet`; opportunistically
  extract any extra fields that match `outlet_information` schema.

The discovered schema lives at `{work_dir}/doc/finder_table_schema.md` for
reference by Step 3.2.

### Step 3.2: Write finder_parse.py

Write in `{work_dir}/parse/finder_parse.py`:

```python
"""
{Platform} Finder Parser
Parse finder API response, produce outlet ID list.
"""

def parse_finder(raw_response: dict) -> list[dict]:
    """
    Finder's core responsibility: obtain outlet id_outlet list.
    Extract accompanying fields if the API returns them.
    """
    pass

def extract_pagination(raw_response: dict) -> dict | None:
    """
    Extract pagination metadata from the Finder response.
    Returns: {'has_next': bool, 'next_cursor': str|None, 'next_offset': int|None}
    or None if the API is not paginated.
    """
    pass
```

Key rules:
- Finder's **core output is `id_outlet`** — other fields are supplementary
- `extract_pagination` is a separate function — do not change `parse_finder`'s return type
- Follow all transformation rules in [conventions.md](conventions.md)
- Use `.get()` for all API field access — no bare `[]`
- Set missing fields to `None`
- Use platform constants defined in Step 0.3

### Step 3.3: Write detail_parse.py

Write four parse functions in `{work_dir}/parse/detail_parse.py`:

```python
"""
{Platform} Detail Parser
Parse detail API response, produce four standard tables.
"""

def parse_outlet_information(raw: dict, id_outlet: str) -> dict:
    """Parse outlet basic information -> outlet_information table"""
    pass

def parse_outlet_meals(raw: dict, id_outlet: str) -> list[dict]:
    """Parse menu items -> outlet_meal table"""
    pass

def parse_meal_options(raw: dict, id_outlet: str) -> list[dict]:
    """Parse option modifiers -> meal_option table"""
    pass

def parse_option_relations(raw: dict, id_outlet: str) -> list[dict]:
    """Parse option relationships -> option_relation table"""
    pass
```

Key rules:
- Cross-reference Schema to ensure all Must fields are handled
- `dashmote` fields (platform, id_platform, source_country, created_at, last_refresh) are set automatically by code
- `position` field format: `"{menu_idx}-{category_idx}-{item_idx}"` (1-based)
- If price is in cents, divide by 100
- Multi-value fields joined with `;`
- Single record parse failure should not interrupt the whole process — try-except to skip bad records

### Step 3.4: Scripts Must Support Direct Execution
Both scripts should include an `if __name__ == "__main__"` block that can directly read JSON files from `response/` for testing.

---

## Phase 4: Test Validation

**Goal:** Validate parse demo with real data, ensure correct output.

### Step 4.1: Round 1 — Existing Data
Use finder1-3.json and detail1-3.json already saved in `response/`:

1. Run `finder_parse.py`, parse finder1 through finder3.json
2. Run `detail_parse.py`, parse detail1 through detail3.json
3. Write results to **5 CSV tables** in `{work_dir}/result/`:

```
result/
├── finder_result.csv          # Finder output: id_outlet list (and accompanying fields)
├── outlet_information.csv     # Outlet basic information
├── outlet_meal.csv            # Menu items
├── meal_option.csv            # Option modifiers
└── option_relation.csv        # Option relationships
```

CSV format requirements:
- UTF-8 encoding
- First row is column headers (matching Schema field names)
- Subsequent parsed data **appended** — do not overwrite

### Step 4.2: Auto-Validate Round 1 Results

Run the bundled validation script:

```bash
python {skill_dir}/validate_output.py --result-dir {work_dir}/result/ [--schema-dir {work_dir}/temp/]
```

The script checks: column names vs schema, Must fields present, Not Null constraints, type correctness (float/int/bool/datetime), and duplicate ID detection.

- **ALL PASSED** -> proceed to Round 2
- **PASSED WITH WARNINGS** -> review warnings; proceed if acceptable
- **FAILED** -> analyze the error report, return to Phase 3 to fix parse code

### Step 4.3: Round 2 — New Data
1. Re-run test scripts to request **1-2 new sets** of finder + detail responses
2. Parse new responses with parse demo
3. **Append** results to the same 5 CSV tables
4. Check that newly appended data is normal

> Round 2's purpose is solely to confirm "new data can also be parsed normally" — no need for large volumes of requests.

### Step 4.3.5: Auto-Validate Round 2 Results

Re-run the validation script after appending Round 2 data:

```bash
python {skill_dir}/validate_output.py --result-dir {work_dir}/result/ [--schema-dir {work_dir}/temp/]
```

Confirm the newly appended data passes all checks.

### Step 4.4: Semantic Sanity Check (NEW — beyond schema)

The validator only checks structural conformance (column names, types, nulls).
It does NOT catch "this column is mostly empty" — which usually means a wrong
JSON path. Sample the actual data:

```bash
# For each output CSV, check non-null ratio per column
python3 << 'PYEOF'
import csv, json, os
from pathlib import Path

result_dir = Path(os.environ['WORK_DIR']) / "result"
report = {}

for csv_path in result_dir.glob("*.csv"):
    if csv_path.stem == "finder_result":
        continue   # finder is small / acceptable to skip
    with open(csv_path, newline='', encoding='utf-8') as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
    if not rows:
        report[csv_path.stem] = {"row_count": 0, "warning": "no rows"}
        continue

    cols = reader.fieldnames or []
    pcts = {}
    for col in cols:
        non_null = sum(1 for r in rows if r.get(col) not in (None, "", "None"))
        pct = round(100 * non_null / len(rows), 1)
        pcts[col] = pct

    suspicious = {c: p for c, p in pcts.items() if p < 50}
    report[csv_path.stem] = {
        "row_count": len(rows),
        "non_null_pct": pcts,
        "suspicious_columns_below_50pct": suspicious,
    }

print(json.dumps(report, indent=2))
PYEOF
```

**Decision tree:**

| Result | Verdict | Action |
|---|---|---|
| All non-`Must=NO` columns ≥ 80% filled | ✅ clean | Proceed to Pre-Phase 5 Gate |
| Some columns 50-79% filled | ⚠️ partial | Narrate per-column percentages; user decides whether to proceed or fix mappings in Phase 3 |
| Any `Must=YES` column < 50% | ❌ likely wrong path | STOP — wrong JSON path is the most common cause; re-check Phase 2 analysis docs |

**Sample 5 rows back to user** for visual sanity check:

```bash
for csv in "$WORK_DIR/result/"*.csv; do
    echo "=== $(basename $csv) ==="
    head -1 "$csv"
    head -6 "$csv" | tail -5
done
```

### Step 4.5: Assess Results
- **All passed (structural + semantic clean)** → Proceed to Pre-Phase 5 Gate
- **Issues found** → Identify the problem, go back to fix:
  - Field mapping error → Return to Phase 3 to modify parse code
  - Endpoint request issue → Return to Phase 2 to troubleshoot
  - Response structure change → Update knowledge documents in doc/
  - High null ratio in `Must=YES` column → Re-check JSON path in Phase 2 analysis doc

---

## Pre-Phase 5 Gate (STOP-style, all 3 must pass)

Before launching the 30-minute stress test, verify it can actually run:

```
G1 Phase 4 verdict was ✅ clean (or ⚠️ user accepted)        [✅ / ❌]
G2 Auth/cookies still fresh (re-issue a single test request)  [✅ / ❌]
G3 Proxy still reachable (proxy GET to a known URL)            [✅ / ❌]
```

```bash
# G1 — Phase 4 result
[[ -f "$WORK_DIR/.phase4_passed" ]] && echo "✅ G1" || echo "❌ G1: Phase 4 not marked passed"

# G2 — fresh auth test
python3 -c "
import sys; sys.path.insert(0, '$WORK_DIR/test')
from test_api import test_finder
import json
coord = json.load(open('$WORK_DIR/location/coordinates.json'))[0]
resp = test_finder(coord['lat'], coord['lng'])
print('✅ G2' if resp else '❌ G2: auth/test failed')
" 2>&1 | tail -1

# G3 — proxy reachable (use whatever proxy the reverse project uses)
# This is platform-specific — narrate "checking proxy via test_api flow"
```

Any FAIL → STOP, fix before launching stress test.

---

## Phase 5: Stress Test (Optional)

**Goal:** Run the complete request + parse pipeline for 30 minutes to validate stability and throughput.

> **Must pause for confirmation:**
> ```
> Phase 4 + Pre-Phase 5 Gate passed. Launch Phase 5 stress test (30 minutes)?
> Enter "yes" to start, "skip" to finish directly, or "duration N" for custom minutes.
> ```
> If user says skip → **Jump directly to Phase 6**

### Execution

1. Read [stress-test-spec.md](stress-test-spec.md) and write `{work_dir}/test/stress_test.py` following its detailed specifications
2. Run the stress test; output statistics report after the configured duration
3. Assess results according to the pass criteria in [stress-test-spec.md](stress-test-spec.md)

> Detailed stress test requirements (concurrency model, statistics format, pass criteria, etc.) are in [stress-test-spec.md](stress-test-spec.md) — not repeated here.

---

## Phase 6: Completion

### Step 6.1: Notify User

```
Parse Demo development complete!

Working directory: {work_dir}
Platform: {PLATFORM} ({COUNTRY})

Deliverables:
- test/          Test scripts
- doc/           Knowledge documents (anti-scraping analysis + JSON structure parsing)
- location/      Test coordinates
- parse/         Parse Demo (finder + detail + scrapy_adapter)
- response/      Raw response samples
- result/        Parse results (5 CSV tables)

All tests passed. Parse pipeline running normally.
```

### Step 6.1.5: Generate Scrapy-Compatible Adapter

Generate the adapter to bridge parse functions with the `conso-migrate` skill:

1. Create `{work_dir}/parse/scrapy_adapter.py` containing:
   - Imports from `finder_parse` and `detail_parse`
   - `parse_finder_to_feeditems(response_json: dict) -> list[dict]` — calls `parse_finder()`, returns dicts with keys matching `FeedItem` fields (primarily `id_outlet`)
   - `parse_detail_to_items(response_json: dict, id_outlet: str) -> tuple[dict, list[dict], list[dict], list[dict]]` — calls all four detail parse functions, returns `(outlet_dict, meal_list, option_list, relation_list)` with keys matching `OutletItem`/`MealItem`/`OptionItem`/`RelationItem` field names

2. The adapter is a **thin mapping layer** — it performs no logic beyond calling parse functions and returning their output. The actual `conso-migrate` spider will import from this module and yield Scrapy Items from the returned dicts.

3. Print a summary of what was generated and how to use it:
   ```
   Generated: {work_dir}/parse/scrapy_adapter.py

   Usage in conso-migrate spider:
     from parse.scrapy_adapter import parse_finder_to_feeditems, parse_detail_to_items
   ```

### Step 6.2: Generate Handoff File

Write `{work_dir}/handoff.json` to persist all context needed by downstream skills (e.g. `conso-migrate`):

```json
{
  "platform": "{PLATFORM}",
  "id_platform": "{ID_PLATFORM}",
  "source_country": "{SOURCE_COUNTRY}",
  "country": "{COUNTRY}",
  "work_dir": "{work_dir}",
  "source_dir": "{reverse-engineered project path}",
  "has_finder": true,
  "has_detail": true,
  "is_single_endpoint": false,
  "outputs": {
    "finder_parse": "parse/finder_parse.py",
    "detail_parse": "parse/detail_parse.py",
    "scrapy_adapter": "parse/scrapy_adapter.py",
    "project_analysis": "doc/project_analysis_summary.md",
    "finder_analysis": "doc/finder_response_json_structure_analysis.md",
    "detail_analysis": "doc/detail_response_json_structure_analysis.md",
    "test_api": "test/test_api.py",
    "coordinates": "location/coordinates.json"
  },
  "finder_fields": ["id_outlet", "...other fields from finder_result.csv headers"],
  "validation_passed": true,
  "completed_at": "2026-04-06 15:30:00"
}
```

Field notes:
- `has_finder` / `has_detail`: whether finder and detail endpoints were identified
- `is_single_endpoint`: true if detail data comes from finder response (no separate detail API)
- `finder_fields`: actual column headers from `result/finder_result.csv`
- All paths in `outputs` are relative to `work_dir`

### Step 6.2.5: Validate handoff.json (downstream contract)

Before declaring Phase 6 done, validate that `handoff.json` actually conforms
to what `/conso-migrate` expects:

```bash
python "$CLAUDE_SKILL_DIR/scripts/validate_handoff.py" \
    --handoff "$WORK_DIR/handoff.json" 2>&1 | tee /tmp/parse-handoff-verdict.json

HANDOFF_VERDICT=$(jq -r '.verdict // "unknown"' /tmp/parse-handoff-verdict.json)
HANDOFF_ERRORS=$(jq -r '.errors // [] | length' /tmp/parse-handoff-verdict.json)
```

If `HANDOFF_VERDICT != "valid"`:
- Print the validator errors
- Loop back to Step 6.2 to fix
- Do NOT proceed to Step 6.3

### Step 6.3: Persist State + Verdict-Driven Next Steps

Write the cross-session state file:

```bash
state_write "$PLATFORM" "completed" "$(jq -n \
    --arg v       "$HANDOFF_VERDICT" \
    --arg work    "$WORK_DIR" \
    --arg country "$COUNTRY" \
    --arg mode    "$MODE" \
    --argjson stress "${STRESS_PASSED:-false}" \
    --argjson semantic "${SEMANTIC_VERDICT:-\"unknown\"}" \
    '{handoff_verdict:$v, work_dir:$work, country:$country, mode:$mode,
      stress_passed:$stress, semantic_verdict:$semantic}')"
```

Now compute overall workflow verdict and recommend specific next action:

```bash
# Verdict combines Phase 4 semantic + Phase 5 stress + handoff validity
case "${SEMANTIC_VERDICT}_${STRESS_PASSED}_${HANDOFF_VERDICT}" in
    clean_*_valid)              VERDICT="complete_clean"        ;;
    partial_*_valid)            VERDICT="complete_with_warnings";;
    *_*_valid)                  VERDICT="partial_validation"    ;;
    *)                          VERDICT="failed"                ;;
esac

case "$VERDICT" in
    complete_clean)
        cat <<EOF
✅ Parse workflow complete (clean).

Output:           $WORK_DIR
Platform:         $PLATFORM ($COUNTRY)
Mode:             $MODE
handoff.json:     valid

Recommended next: launch ConSo migration with the handoff:
    /conso-migrate $WORK_DIR

Other options:
- /run-detail (after migration deploys) — sanity-test the spider on a few outlets
- Manual review of doc/*.md before migration
EOF
        ;;
    complete_with_warnings)
        cat <<EOF
⚠️  Parse workflow complete with warnings.

Phase 4 semantic check: $SEMANTIC_VERDICT (some columns < 80% filled)
handoff.json:           valid

Recommended next:
1. Review the per-column fill ratios in doc/ — confirm low-fill is expected
   (some platforms genuinely don't return certain fields)
2. If acceptable: /conso-migrate $WORK_DIR
3. If not acceptable: revisit Phase 3 / Phase 2 mappings
EOF
        ;;
    partial_validation)
        cat <<EOF
⚠️  Parse workflow incomplete — partial validation.

Stress test:    ${STRESS_PASSED}
Semantic check: $SEMANTIC_VERDICT
handoff.json:   valid (but data quality questionable)

Recommended next:
- DO NOT migrate yet. Investigate the failed validation step before continuing.
- Re-run /parse-workflow — Phase 0.5 will resume from the failed step.
EOF
        ;;
    failed)
        cat <<EOF
❌ Parse workflow failed.

handoff.json:   $HANDOFF_VERDICT
Phase 4:        $SEMANTIC_VERDICT
Phase 5:        $STRESS_PASSED

Do NOT migrate. Investigate:
- handoff.json errors: cat /tmp/parse-handoff-verdict.json
- Phase 4 semantic report: see narrative above
- Phase 5 stress test log: $WORK_DIR/test/stress_test_*.log
EOF
        ;;
esac
```

### Step 6.4: End
Workflow complete. State file persisted at `~/.claude/state/parse-workflow/$PLATFORM.json`.
Downstream skills (`/conso-migrate`, `/run-detail`) will read this for context.

Await user's next instructions — typically `/conso-migrate $WORK_DIR`.

---

## Appendix: Final Working Directory Structure

```
{work_dir}/
├── handoff.json                                # Cross-skill handoff (platform info + output manifest)
├── test/
│   ├── test_api.py                          # API endpoint test script
│   └── stress_test.py                       # Stress test script (if Phase 5 was executed)
├── doc/
│   ├── project_analysis_summary.md          # Phase 1 knowledge persistence (incl. platform constants)
│   ├── finder_response_json_structure_analysis.md  # Phase 2 Finder structure analysis
│   └── detail_response_json_structure_analysis.md  # Phase 2 Detail structure analysis
├── location/
│   └── coordinates.json                     # AI-generated test coordinates
├── temp/                                    # Project-specific Schema (with additional fields, if any)
│   ├── schema_outlet_information.csv
│   ├── schema_outlet_meal.csv
│   ├── schema_meal_option.csv
│   └── schema_option_relation.csv
├── response/
│   ├── finder1.json ... finder3.json         # Finder raw responses
│   └── detail1.json ... detail3.json         # Detail raw responses
├── parse/
│   ├── finder_parse.py                       # Finder parse script
│   ├── detail_parse.py                       # Detail parse script
│   └── scrapy_adapter.py                    # Scrapy-compatible adapter
└── result/
    ├── finder_result.csv                     # Finder output
    ├── outlet_information.csv                # Outlet information
    ├── outlet_meal.csv                       # Menu items
    ├── meal_option.csv                       # Option modifiers
    └── option_relation.csv                   # Option relationships
```

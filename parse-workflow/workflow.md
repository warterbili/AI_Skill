# Standard Parse Workflow

> Step-by-step execution guide for the AI agent.
> Strictly execute Phase 0 through Phase 6 in order. Phase 0.5 (Resume Detection) may skip ahead to a later Phase if prior progress is detected.

---

## Phase 0: Initialization

### Step 0.1: Collect User Input (single prompt)

Ask for all required information in a single prompt — **do not split into multiple questions**:

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

Wait for the user to reply with all information before continuing.

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

Apply the following rules **in order** (first match wins):

| Condition | Resume From | Rationale |
|-----------|-------------|-----------|
| `result/` has CSV files containing data rows for all expected tables | **Phase 4** (re-validate) | Parse completed previously; re-validate with fresh data |
| `result/` has CSVs but only `finder_result.csv` has data rows (detail tables empty/headers-only) | **Phase 4 Step 4.1** (re-run detail parse) | Finder validated but detail parse needs testing |
| `parse/` has both `finder_parse.py` AND `detail_parse.py` | **Phase 4** (test them) | Both parse scripts complete; validate output |
| `parse/` has only `finder_parse.py` (no `detail_parse.py`) | **Phase 3 Step 3.3** (write detail parse) | Finder parse done; write detail parse next |
| `parse/` has only `detail_parse.py` (no `finder_parse.py`) | **Phase 3 Step 3.2** (write finder parse) | Detail parse done but no finder — unusual; write finder |
| `doc/` has `*_analysis.md` AND `response/` has JSON files | **Phase 3** (write parse) | API tested and analyzed; write parse scripts |
| `doc/project_analysis_summary.md` AND `test/test_api.py` exist | **Phase 2** (API testing) | Project analyzed; proceed to test APIs |
| `{work_dir}` exists but has only subdirectories or minimal content | **Phase 1** (analyze project) | Workspace initialized but no real work done |
| `{work_dir}` does not exist | **Phase 0** (start fresh) | Already handled by Phase 0 directory creation |

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

### Step 1.5: Generate Test Coordinates

The Finder endpoint requires latitude/longitude coordinates to search for outlets. The AI autonomously generates a set of test coordinates based on the platform's country:

**Generation rules:**
- Select **major cities** in the country (high population, restaurant-dense)
- Prefer popular tourist areas, food districts, commercial centers, and other restaurant-dense locations
- Take 1-3 commonly used location coordinates per city, **10-20 coordinate points** total

**Output file:** `{work_dir}/location/coordinates.json`

```json
[
  {"lat": -23.5505, "lng": -46.6333, "city": "Sao Paulo", "note": "Centro"},
  {"lat": -22.9068, "lng": -43.1729, "city": "Rio de Janeiro", "note": "Centro"}
]
```

> All subsequent steps that need coordinates should read from this file uniformly — do not hardcode in scripts.

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
| 429 | Rate limiting | Increase request interval (1-3 seconds) |
| 200 but empty data | Incorrect coordinates/params | Use verified coordinates from reverse-engineered project |
| Connection timeout | Proxy issues | Check proxy configuration |
| SSL error | TLS fingerprint detection | Use curl_cffi browser fingerprinting |

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

### Step 3.1.5: Query Finder Table Schema from MySQL

Before writing `finder_parse.py`, query the platform's actual MySQL table to determine which fields the finder should output:

```python
import json, boto3, pymysql

s3 = boto3.client('s3', region_name='eu-central-1')
obj = s3.get_object(Bucket='dash-dbcenter', Key='config/general_config/config.json')
config = json.loads(obj['Body'].read())

conn = pymysql.connect(
    host=config['host'], user=config['user'], password=config['passwd'],
    port=3306, database='{ID_PLATFORM}', connect_timeout=10
)
cursor = conn.cursor()
cursor.execute('SHOW TABLES')
tables = [r[0] for r in cursor.fetchall()]
# Pick the first prefix table (e.g. 'UK', 'US', 'BR')
if tables:
    cursor.execute(f'DESCRIBE `{tables[0]}`')
    print(f'=== {ID_PLATFORM}.{tables[0]} finder schema ===')
    for row in cursor.fetchall():
        print(f'  {row[0]:30s} {row[1]:20s} NULL={row[2]}')
conn.close()
```

- If the platform **already exists in MySQL** → the table columns define exactly which fields `parse_finder` should output. `id_outlet` is always the primary key; other columns are the accompanying fields to extract.
- If the platform **is brand new** (no MySQL table yet) → fall back to the minimum: `id_outlet` is the only required field. Extract any additional fields that the Finder API returns and that match `outlet_information` schema fields (e.g. `rating`, `cuisine`, `lat`, `lon`).

Save the discovered schema to `{work_dir}/doc/finder_table_schema.md` for reference.

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

### Step 4.4: Assess Results
- **All passed** -> Proceed to Phase 5
- **Issues found** -> Identify the problem, go back to the corresponding Phase to fix:
  - Field mapping error -> Return to Phase 3 to modify parse code
  - Endpoint request issue -> Return to Phase 2 to troubleshoot
  - Response structure change -> Update knowledge documents in doc/

---

## Phase 5: Stress Test (Optional)

**Goal:** Run the complete request + parse pipeline for 30 minutes to validate stability and throughput.

> **Must pause for confirmation:**
> ```
> Phase 4 test validation passed. Launch Phase 5 stress test (30 minutes)?
> Enter "yes" to start, or "skip" to finish directly.
> ```
> If user says skip -> **Jump directly to Phase 6**

### Execution

1. Read [stress-test-spec.md](stress-test-spec.md) and write `{work_dir}/test/stress_test.py` following its detailed specifications
2. Run the stress test; output statistics report after 30 minutes
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

### Step 6.3: Prompt for ConSo Migration

After generating the handoff file, ask the user:

```
Parse workflow complete! Ready to proceed to ConSo migration?

Enter "yes" to launch /conso-migrate (will use parse outputs automatically)
Enter "no" to finish here
```

- **User says yes** → invoke the `conso-migrate` skill. The conso-migrate skill will detect `{work_dir}/handoff.json` and use it to skip redundant analysis and reuse parse outputs.
- **User says no** → print completion summary and end.

### Step 6.4: End
Workflow complete. Await user's next instructions.

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

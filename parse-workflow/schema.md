# Schema Definition - Four Tables

> This is the **single source of truth** for all parse output fields.
> All platforms must output data conforming to these table definitions.

---

## Field Conventions

| Convention | Rule |
|-----------|------|
| Multivalue separator | `;` (e.g. `Fried Chicken;American;Fast Food`) |
| Price | Float, unit is the platform's currency (NOT cents). If API returns cents, divide by 100 |
| Coordinates | Float, 8 decimal places (e.g. `52.90582300`) |
| Boolean | Python `True` / `False` |
| Null handling | Use `None` for missing fields, never empty string `""` |

### Not Null vs Must Semantics

These two columns have different meanings — do not confuse them:

- **Not Null**: Database-level constraint. `YES` means the platform API reliably returns this field for every record — it should never be null in production data.
- **Must**: Parse-workflow-level requirement. `YES` means the parse script must always include this field key in the output dict, even if the value is `None`. It means "this field is part of the output contract and must be attempted."

| Not Null | Must | Meaning |
|----------|------|---------|
| YES | YES | Field must be present in every record AND must not be null |
| NO | YES | Field must be present in every record, but null is acceptable |
| YES | NO | If present, must not be null (rare — typically dashmote auto-fields) |
| NO | NO | Optional — include only if the API provides it |

Example: `meal_option.id_meal` has `Not Null=NO, Must=YES` — the parse script must always attempt to extract `id_meal` and include it in every record, but some platforms lack a meal-to-option link at the API level, so null values are acceptable.
| Date | `%Y-%m-%d` format |
| Datetime | `%Y-%m-%d %H:%M:%S` format |
| opening_hours | JSON string: `[{"dayRange": "Monday", "sectionHours": [{"startTime": 630, "endTime": 1350}]}]` |

### Field Source Types

| Source | Meaning |
|--------|---------|
| `platform` | Extracted from platform API response |
| `dashmote` | Set by our system (constants or auto-generated) |

---

## Table 1: outlet_information

Stores basic outlet/restaurant information. One record per outlet.

| # | Field | Type | Source | Not Null | Must | Description |
|---|-------|------|--------|----------|------|-------------|
| 1 | id_outlet | str | platform | YES | YES | Unique outlet ID on the platform |
| 2 | id_platform | str | dashmote | YES | YES | Platform ID (assigned by us) |
| 3 | platform | str | dashmote | YES | YES | Platform name, e.g. `ubereats`, `deliveroo`, `ifood` |
| 4 | url | str | platform | NO | YES | Outlet URL on the platform |
| 5 | name | str | platform | YES | YES | Outlet name (English if available) |
| 6 | description | str | platform | NO | YES | Outlet description |
| 7 | address | str | platform | NO | YES | Full address string |
| 8 | street | str | platform | NO | NO | Street name |
| 9 | house_number | str | platform | NO | NO | House number |
| 10 | postal_code | str | platform | NO | NO | Postal code |
| 11 | city | str | platform | NO | NO | City name |
| 12 | region | str | platform | NO | NO | Region (e.g. EMEA, LATAM) |
| 13 | country | str | platform | NO | YES | Country code, e.g. `GB`, `BR` |
| 14 | source_country | str | dashmote | YES | YES | Scrape country prefix, e.g. `UK`, `BR` |
| 15 | name_local | str | platform | NO | NO | Outlet name in local language |
| 16 | address_local | str | platform | NO | NO | Full address in local language |
| 17 | lat | float | platform | NO | YES | Latitude, 8 decimal places |
| 18 | lon | float | platform | NO | YES | Longitude, 8 decimal places |
| 19 | category | str | platform | NO | YES | Raw outlet category, `;` separated |
| 20 | cuisine | str | platform | NO | YES | Cuisine types, `;` separated |
| 21 | review_nr | int | platform | NO | YES | Number of reviews/ratings |
| 22 | rating | float | platform | NO | YES | Average rating value |
| 23 | price_level | str | platform | NO | YES | Price level indicator, e.g. `$`, `$$` |
| 24 | telephone | str | platform | NO | YES | Outlet phone number |
| 25 | telephone_platform | str | platform | NO | NO | Platform-specific phone number |
| 26 | delivery_cost | float | platform | NO | NO | Delivery cost (fixed, distance-independent) |
| 27 | min_order_amount | float | platform | NO | NO | Minimum order amount for delivery |
| 28 | banner_img_url | str | platform | NO | NO | Banner image URL |
| 29 | icon_url | str | platform | NO | NO | Icon/logo image URL |
| 30 | website | str | platform | NO | NO | Outlet's own official website |
| 31 | opening_hours_physical | str | platform | NO | YES | Physical store opening hours (JSON) |
| 32 | opening_hours | str | platform | NO | NO | Platform delivery opening hours (JSON) |
| 33 | delivery_available | bool | platform | NO | NO | Whether delivery is available |
| 34 | pickup_available | bool | platform | NO | NO | Whether pickup is available |
| 35 | promotion | str | platform | NO | NO | Promotion text |
| 36 | is_promotion | bool | platform | NO | NO | Has active promotion |
| 37 | is_convenience | bool | platform | NO | NO | Is a convenience store |
| 38 | is_new | bool | platform | NO | NO | Is new on the platform |
| 39 | chain_name | str | platform | NO | NO | Chain/brand name |
| 40 | chain_flag | bool | platform | NO | NO | Is part of a chain |
| 41 | id_chain | str | platform | NO | NO | Chain unique ID |
| 42 | chain_url | str | platform | NO | NO | Chain URL on platform |
| 43 | flag_close | bool | platform | NO | NO | Outlet closed/inactive flag |
| 44 | is_active | bool | platform | NO | NO | Outlet is active (platform-specific closed indicator) |
| 45 | is_test | bool | platform | NO | NO | Is a test outlet |
| 46 | is_popular | bool | platform | NO | NO | Is popular on platform |
| 47 | menu_disabled | bool | platform | NO | NO | Menu is disabled |
| 48 | created_at | datetime | dashmote | YES | YES | Record creation date (auto) |
| 49 | last_refresh | datetime | dashmote | YES | YES | Record last update date (auto) |

---

## Table 2: outlet_meal

Stores menu items. One record per meal/product per outlet.

| # | Field | Type | Source | Not Null | Must | Description |
|---|-------|------|--------|----------|------|-------------|
| 1 | id_meal | str | platform | YES | YES | Unique meal ID from platform |
| 2 | id_outlet | str | platform | YES | YES | Parent outlet ID |
| 3 | id_platform | str | dashmote | YES | YES | Platform ID |
| 4 | platform | str | dashmote | YES | YES | Platform name |
| 5 | position | str | dashmote | YES | YES | Menu position, e.g. `1-1-1` (menu-category-item) |
| 6 | category | str | platform | YES | YES | Category name the item belongs to |
| 7 | id_category | str | platform | NO | NO | Category ID from platform |
| 8 | menu | str | platform | NO | NO | Menu name (if platform has menu concept) |
| 9 | id_menu | str | platform | NO | NO | Menu ID from platform |
| 10 | price | float | platform | NO | YES | Item price (starting price if range) |
| 11 | image_url | str | platform | NO | YES | Item image URL |
| 12 | name | str | platform | NO | YES | Item name |
| 13 | description | str | platform | NO | YES | Item description |
| 14 | choices | str | platform | NO | NO | Summary of option modifiers at meal level |
| 15 | has_options | bool | platform | NO | NO | Whether item has customization options |
| 16 | out_of_stock | bool | platform | NO | NO | Whether item is out of stock |
| 17 | banner_category_img_url | str | platform | NO | NO | Category banner image URL |
| 18 | created_at | datetime | dashmote | YES | YES | Record creation date (auto) |
| 19 | last_refresh | datetime | dashmote | YES | YES | Record last update date (auto) |

---

## Table 3: meal_option

Stores option/modifier items for meals. One record per option per meal.

| # | Field | Type | Source | Not Null | Must | Description |
|---|-------|------|--------|----------|------|-------------|
| 1 | id_option | str | platform | YES | YES | Option modifier ID |
| 2 | id_meal | str | platform | NO | YES | Parent meal ID (required if no id_category for reverse lookup) |
| 3 | id_outlet | str | platform | YES | YES | Parent outlet ID |
| 4 | id_platform | str | dashmote | YES | YES | Platform ID |
| 5 | platform | str | dashmote | YES | YES | Platform name |
| 6 | category | str | platform | YES | YES | Option group/category name |
| 7 | id_category | str | platform | NO | NO | Option category ID from platform |
| 8 | price | float | platform | YES | YES | Option price |
| 9 | name | str | platform | YES | YES | Option name |
| 10 | description | str | platform | NO | NO | Option description |
| 11 | is_sold_out | bool | platform | NO | NO | Whether option is sold out |
| 12 | created_at | datetime | dashmote | YES | YES | Record creation date (auto) |
| 13 | last_refresh | datetime | dashmote | YES | YES | Record last update date (auto) |

---

## Table 4: option_relation

Maps relationships between meals and options. One record per meal-option pair.

| # | Field | Type | Source | Not Null | Must | Description |
|---|-------|------|--------|----------|------|-------------|
| 1 | id_outlet | str | platform | YES | YES | Parent outlet ID |
| 2 | id_platform | str | dashmote | YES | YES | Platform ID |
| 3 | id_meal | str | platform | YES | YES | Parent meal ID |
| 4 | id_option | str | platform | YES | YES | Option ID |
| 5 | platform | str | dashmote | YES | YES | Platform name |
| 6 | id_category | str | platform | YES | YES | Option category ID (= meal_option.id_category) |
| 7 | option_level | int | platform | NO | NO | Popup level (1, 2, etc.) for nested options |
| 8 | id_option_parent | str | platform | NO | NO | Parent option ID (for nested options) |
| 9 | created_at | datetime | dashmote | YES | YES | Record creation date (auto) |
| 10 | last_refresh | datetime | dashmote | YES | YES | Record last update date (auto) |

---

## Table 0: Finder Output (per-platform, variable schema)

The finder table stores discovered outlet IDs and accompanying metadata. Unlike the 4 detail tables above, **the finder schema varies per platform** — each platform extracts different fields from its discovery API.

The only universal requirement is `id_outlet` (primary key). All other columns are platform-specific.

**How to determine the finder schema for an existing platform:**

Query the platform's MySQL database directly — the table is named by prefix (e.g. `UK`, `US`, `BR`):

```sql
DESCRIBE {ID_PLATFORM}.{prefix};
```

The resulting columns define exactly which fields `finder_parse.py` should output.

**For a brand-new platform (no MySQL table yet):**

Output `id_outlet` as the minimum. Additionally extract any fields the Finder API returns that match `outlet_information` fields (e.g. `rating`, `cuisine`, `lat`, `lon`) — these are useful for deduplication and QA.

**Common finder fields observed across platforms:**

| Field | Frequency | Description |
|-------|-----------|-------------|
| `id_outlet` | **always** | Primary key — the only hard requirement |
| `created_at` | always | Auto-generated timestamp |
| `last_refresh` | always | Auto-generated timestamp |
| `rating` | common | Outlet rating |
| `cuisine` | common | Cuisine types |
| `delivery_cost` | common | Delivery fee |
| `min_order_amount` | common | Minimum order |
| `lat`, `lon` | sometimes | Coordinates |
| `chain_name` | sometimes | Chain/brand name |
| `is_new` | sometimes | New outlet flag |

---

## Finder vs Detail Field Split

Not all outlet_information fields come from the detail API. The **finder** (discovery/listing) API typically provides a subset. Refer to the MySQL table structure (Table 0 above) for the actual finder fields of a given platform.
- `icon_url`, `banner_img_url`
- `flag_close`

### Detail-Only Fields (typically require a second API call)
- `description`, `address`, `street`, `postal_code`, `city`
- `telephone`, `website`
- `opening_hours`, `opening_hours_physical`
- `chain_name`, `id_chain`, `chain_url`
- All meal/option data (Tables 2-4)

> **Note:** The exact finder/detail split depends on each platform's API.
> Always document the actual split in `{work_dir}/doc/project_analysis_summary.md`.

---

## Extended Schema (Additional Fields)

If `{work_dir}/extra_fields.json` contains additional fields, Phase 0 will automatically generate an extended Schema, saved in `{work_dir}/temp/`:

**File format:**
```json
[
  { "field_name": "field description", "target_table": "outlet_information" },
  { "field_name": "field description", "target_table": "outlet_meal" }
]
```

Each object contains:
- The first key is the field name, its value is the field description
- `target_table` specifies which table the field should be added to (one of: `outlet_information`, `outlet_meal`, `meal_option`, `option_relation`)

Extended Schema output:
```
{work_dir}/temp/
├── schema_outlet_information.csv
├── schema_outlet_meal.csv
├── schema_meal_option.csv
└── schema_option_relation.csv
```

**All subsequent Phases should prefer the extended Schema in `temp/` (if it exists); otherwise use the default definitions in this file.**

Default attributes for new fields: `Source = platform`, `Not Null = NO`, `Must = YES`.

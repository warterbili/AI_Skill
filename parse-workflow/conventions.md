# Coding Conventions

> Standard rules for all parse implementations. AI agents MUST follow these.

---

## 1. General Rules

- **Language:** Python 3.10+
- **Encoding:** UTF-8 everywhere
- **Output format:** CSV (UTF-8, column headers matching schema.md field names)
- **No external dependencies** in parse code — only stdlib (`json`, `csv`, `datetime`, `re`, etc.)
  - Test scripts may use platform-specific libraries (curl_cffi, requests, etc.)

---

## 2. Field Access

```python
# ALWAYS use .get() with default None
name = data.get("name")

# NEVER use bare indexing on API data
name = data["name"]  # will crash on missing key

# Nested access — chain .get() or use helper
address = data.get("address", {}).get("street")
```

---

## 3. Common Transformations

### Price: cents to currency unit
```python
# Many APIs return price in cents (e.g. 2990 = 29.90)
price_cents = item.get("unitPrice", 0)
price = price_cents / 100 if price_cents else None
```

### Multivalue fields: join with semicolon
```python
# cuisine, category — join list into ";" separated string
cuisines = data.get("cuisines", [])
cuisine_str = ";".join([c.get("name", "") for c in cuisines]) if cuisines else None
```

### Boolean mapping from strings
```python
# "AVAILABLE" / "UNAVAILABLE" -> bool
availability = item.get("availability")
out_of_stock = availability == "UNAVAILABLE" if availability else None
# or inversely:
is_available = availability == "AVAILABLE" if availability else None
```

### Rating normalization
```python
# Some platforms use 0-10 scale, normalize to 0-5
rating_raw = data.get("rating", 0)
rating = rating_raw / 2 if rating_raw > 5 else rating_raw
```

### Coordinate precision
```python
# Always 8 decimal places
lat = round(float(data.get("latitude", 0)), 8) if data.get("latitude") is not None else None
```

### Opening hours format
```python
# Target format: JSON string
# [{"dayRange": "Monday", "sectionHours": [{"startTime": 630, "endTime": 1350}]}]
# startTime/endTime are minutes from midnight (630 = 10:30)
import json
opening_hours = json.dumps(formatted_hours, ensure_ascii=False)
```

---

## 4. Position Generation

The `position` field in `outlet_meal` encodes location in the menu hierarchy:

```python
# Format: "{menu_idx}-{category_idx}-{item_idx}" (all 1-based)
for cat_idx, category in enumerate(menu, 1):
    for item_idx, item in enumerate(category.get("items", []), 1):
        position = f"1-{cat_idx}-{item_idx}"
```

---

## 5. Dashmote Fields

These are set by us, NOT from the API:

```python
from datetime import datetime

PLATFORM = "ifood"           # lowercase platform name
ID_PLATFORM = "1"            # assigned platform ID
SOURCE_COUNTRY = "BR"        # scrape country prefix
COUNTRY = "BR"               # country code

# Auto-generated timestamps (datetime format: %Y-%m-%d %H:%M:%S)
now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
record["created_at"] = now
record["last_refresh"] = now
```

---

## 6. Null Handling

```python
# Use None, never empty string
record["telephone"] = data.get("phone") or None  # converts "" to None

# For numeric fields, 0 is valid — don't convert to None
record["price"] = float(price) if price is not None else None
record["delivery_cost"] = 0.0  # free delivery is 0, not None
```

---

## 7. ID Handling

```python
# Always convert IDs to string
record["id_outlet"] = str(data.get("id", ""))
record["id_meal"] = str(item.get("id", ""))

# If platform uses numeric IDs, still store as string
record["id_outlet"] = str(12345)  # -> "12345"
```

---

## 8. File Structure Pattern

Each platform's parse files follow the same pattern:

```python
"""
{Platform} {Finder/Detail} Parser
"""
import json
import csv
from datetime import datetime

# --- Constants ---
PLATFORM = "..."
ID_PLATFORM = "..."
SOURCE_COUNTRY = "..."
COUNTRY = "..."

# --- Parse functions ---
def parse_xxx(raw: dict) -> list[dict]:
    ...

# --- Main: test with raw samples ---
if __name__ == "__main__":
    for i in range(1, 4):
        with open(f"../response/xxx_{i}.json", "r", encoding="utf-8") as f:
            raw = json.load(f)
        results = parse_xxx(raw)
        print(f"xxx_{i}: {len(results)} records")

    # Write to CSV (append mode)
    with open(f"../result/xxx_result.csv", "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=results[0].keys())
        if f.tell() == 0:
            writer.writeheader()
        writer.writerows(results)
```

> **Note:** Parse scripts are placed in `{work_dir}/parse/`, test samples in `{work_dir}/response/`, output in `{work_dir}/result/`.

---

## 9. Error Tolerance

Parse code should be **resilient** — a single bad record should not crash the entire parse:

```python
for item in items:
    try:
        record = parse_single_item(item)
        results.append(record)
    except Exception as e:
        print(f"[WARN] Failed to parse item {item.get('id', '?')}: {e}")
        continue
```

---

## 10. Do NOT

- Don't hardcode sample data in parse functions
- Don't add fields not in schema.md
- Don't skip Must fields — set to None if unavailable, but always include the key
- Don't use `pandas` in parse code (use for analysis only)
- Don't mix finder and detail logic in one function

---

## 11. Scrapy Compatibility Guidelines

Parse functions are designed to be importable by downstream tools (e.g., the `conso-migrate` skill which generates Scrapy spiders). Follow these rules to ensure compatibility:

- **No side effects in parse functions** — no file I/O, no print statements, no global state mutation. Side effects belong only in the `if __name__ == '__main__'` block.
- **Constants at module level** — `PLATFORM`, `ID_PLATFORM`, `SOURCE_COUNTRY`, `COUNTRY` are defined at the top of the file. Parse functions may reference them freely.
- **`id_outlet` as parameter** — detail parse functions receive `id_outlet` as a parameter (not from globals or closures). This maps directly to `response.meta['id_outlet']` in a Scrapy callback.
- **Return plain dicts** — parse functions return `list[dict]` (or `dict` for single-record functions). Keys are schema field names. This makes it trivial to map into Scrapy Item classes:
  ```python
  # In a Scrapy spider callback:
  for record in parse_outlet_meals(raw, id_outlet):
      item = MealItem()
      for k, v in record.items():
          item[k] = v
      yield item
  ```
- **One function per table** — `parse_outlet_information`, `parse_outlet_meals`, `parse_meal_options`, `parse_option_relations` map 1:1 to Scrapy callbacks or sub-callbacks.

---

## 12. Non-JSON Response Handling

Some platforms return non-JSON responses. Apply these conventions:

### HTML Responses
- Use `html.parser` from stdlib (or `re` for simple extractions)
- If the page embeds JSON in a `<script>` tag (common pattern: `__NEXT_DATA__`, `window.__INITIAL_STATE__`), extract and parse that JSON first — then treat as a normal JSON response
- For pure HTML scraping, document CSS selector paths in the analysis doc the same way JSON paths are documented

### XML / SOAP Responses
- Use `xml.etree.ElementTree` from stdlib
- Document namespace prefixes in the analysis doc
- Map XML element paths to schema fields the same way JSON paths are mapped

### Protobuf Responses
- Requires `protobuf` or `betterproto` — add to test script deps (not parse deps)
- Decode to Python dict first, then apply standard JSON parse conventions
- Document the `.proto` schema or reverse-engineered message structure in `{work_dir}/doc/protobuf_schema.md`
- If proto definition is unavailable, use raw field-number decoding and document the inferred structure

### General Rule
Always convert non-JSON to a Python dict/list **as early as possible**, then apply the same parse conventions as JSON. The parse functions themselves should receive Python dicts — format-specific decoding belongs in the test/request layer, not in parse code.

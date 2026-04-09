# Stress Test Detailed Specification

> This file defines the complete requirements for the Phase 5 stress test script.
> Only read this file when the user confirms launching the stress test.

---

## Script Requirements

Write `stress_test.py` in `{work_dir}/test/`, implementing a complete **request -> parse -> write** pipeline.

### 0. CLI Interface

The stress test script must accept these arguments via `argparse`:

| Argument | Default | Description |
|----------|---------|-------------|
| `--workers` | 8 | Number of concurrent worker threads |
| `--duration` | 30 | Test duration in minutes |
| `--interval-min` | 0.5 | Minimum wait between requests (seconds) |
| `--interval-max` | 1.0 | Maximum wait between requests (seconds) |
| `--auth-refresh` | 240 | Auth credential refresh interval (seconds) |

Example: `python stress_test.py --workers 4 --duration 10`

### 1. Multi-threaded Concurrency
- Default 8 worker threads (configurable via `--workers`)
- Each worker has an independent session: generates its own auth credentials (cookie/token) and its own proxy session ID
- Thread-safe CSV writing (use locks or queues)

### 2. Continuous Running
- Default runtime: 30 minutes (configurable via `--duration`)
- Auto-stop when time is up — don't hard-kill threads; wait for current tasks to complete and exit gracefully

### 3. Request Interval
- Each worker waits randomly `--interval-min` to `--interval-max` seconds after each request (default 0.5-1.0)

### 4. Complete Pipeline
- Finder request -> `parse_finder()` to extract id_outlet list
  - If `extract_pagination()` returns `has_next=True`, follow pagination until exhausted or a per-coordinate outlet count threshold is reached (default: 200)
- For each id_outlet: Detail request -> `parse_outlet_information()` + `parse_outlet_meals()` + `parse_meal_options()` + `parse_option_relations()`
- Write results to the 5 CSV tables in `{work_dir}/result/` (append mode)

### 5. Auto Auth Refresh
- Automatically regenerate auth credentials after `--auth-refresh` seconds (default 240 = 4 minutes) or upon 403/429 responses
- Refresh logic is implemented based on actual platform behavior (varies significantly across platforms)

### 6. Error Tolerance
- Single failure does not interrupt the whole process; log the failure reason and continue execution

---

## Real-time Statistics

During script execution, output real-time statistics every **60 seconds**:

```
[05:30] -- Real-time Stats -------------------------
  Runtime: 5m30s / 30m00s
  Finder requests: 25 (success 23, failed 2)
  Detail requests: 180 (success 172, failed 8)
  Outlets processed: 172
  Parse success: 172 | Parse failed: 0
  Avg time/outlet: 1.8s
  Auth refreshes: 12
----------------------------------------------------
```

---

## Final Report

After 30 minutes, the script outputs a complete statistics report:

```
===================================================
  Stress Test Report
===================================================
  Total runtime:          {duration}m00s
  Worker count:           {workers}

  -- Request Stats --
  Total Finder requests:  XXX
  Finder success rate:    XX.X%
  Total Detail requests:  XXX
  Detail success rate:    XX.X%
  Auth refresh count:     XX

  -- Parse Stats --
  Total outlets processed: XXX
  Parse success:           XXX
  Parse failed:            XXX
  Parse success rate:      XX.X%

  -- Performance Metrics --
  Avg time/outlet:        X.Xs
  Throughput:             X.X outlets/min
  Peak throughput:        X.X outlets/min

  -- Output Files --
  finder_result.csv:         XXX records
  outlet_information.csv:    XXX records
  outlet_meal.csv:           XXX records
  meal_option.csv:           XXX records
  option_relation.csv:       XXX records
===================================================
```

---

## Pass Criteria

| Metric | Pass Threshold |
|--------|---------------|
| Runtime | Complete the full `--duration` without crashing |
| Request success rate | Finder >= 90%, Detail >= 85% |
| Parse success rate | >= 95% |
| Output data | All 5 CSV tables have new data with correct formatting |

### Failure Handling
- Low request success rate -> Check auth refresh logic, proxy stability, request intervals
- High parse failure rate -> Check parse code handling of edge cases
- Crash/hang -> Check thread safety, resource leaks, timeout handling

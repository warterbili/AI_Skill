#!/usr/bin/env python3
"""
ConSo Grid Generator — hexagonal grid point generation for finder spiders.

Usage:
    python generate_grid.py \
        --prefix NL \
        --distance 3000 \
        --mode cities-only \
        --output location/NL_3000_grid.json

    python generate_grid.py \
        --prefix RU \
        --distance 5000 \
        --mode full-country \
        --output location/RU_5000_grid.json

    python generate_grid.py \
        --prefix KZ \
        --distance 3000 \
        --mode cities-only \
        --cities "Almaty,Astana,Shymkent" \
        --output location/KZ_3000_grid.json

Output: JSON on stdout (structured result), narration on stderr.
Exit 0 on success, 1 on failure.

Dependencies: h3, shapely, requests (pip install h3 shapely requests)
"""

import argparse
import json
import math
import os
import sys
import time

# ---------------------------------------------------------------------------
# H3 version compatibility (v3 vs v4 API)
# ---------------------------------------------------------------------------

try:
    import h3
except ImportError:
    print("FATAL: h3 not installed. Run: pip install h3", file=sys.stderr)
    sys.exit(1)

H3_V4 = hasattr(h3, "polygon_to_cells")

if H3_V4:
    def _polyfill(geojson_coords, resolution):
        outer = [(lat, lng) for lng, lat in geojson_coords[0]]
        poly = h3.LatLngPoly(outer)
        return h3.polygon_to_cells(poly, resolution)

    def _hex_to_latlng(hex_id):
        return h3.cell_to_latlng(hex_id)
else:
    def _polyfill(geojson_coords, resolution):
        geojson = {"type": "Polygon", "coordinates": geojson_coords}
        return h3.polyfill(geojson, resolution, geo_json_conformant=True)

    def _hex_to_latlng(hex_id):
        return h3.h3_to_geo(hex_id)


def _log(msg):
    """Narration goes to stderr; structured output goes to stdout."""
    print(msg, file=sys.stderr)


# ---------------------------------------------------------------------------
# Distance → H3 resolution mapping
# ---------------------------------------------------------------------------

def distance_to_h3_resolution(distance_m: int) -> int:
    if distance_m >= 5000:
        return 4
    elif distance_m >= 2500:
        return 5
    elif distance_m >= 1200:
        return 6
    elif distance_m >= 500:
        return 7
    else:
        return 8


# ---------------------------------------------------------------------------
# Overpass API helpers (with retry)
# ---------------------------------------------------------------------------

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
MAX_RETRIES = 3


def _overpass_query(query: str, timeout: int = 120) -> dict:
    """POST to Overpass with retry on timeout / connection error."""
    import requests

    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.post(
                OVERPASS_URL,
                data={"data": query},
                timeout=timeout,
            )
            resp.raise_for_status()
            return resp.json()
        except (requests.Timeout, requests.ConnectionError) as exc:
            wait = 10 * (attempt + 1)
            if attempt < MAX_RETRIES - 1:
                _log(f"Overpass timeout (attempt {attempt + 1}/{MAX_RETRIES}), "
                     f"retrying in {wait}s…")
                time.sleep(wait)
            else:
                raise RuntimeError(
                    f"Overpass API failed after {MAX_RETRIES} attempts: {exc}"
                ) from exc
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 429:
                _log("Overpass 429 rate-limited, waiting 60s…")
                time.sleep(60)
            else:
                raise


def get_major_cities(country_code: str, min_population: int = 100000) -> list:
    """Return cities above *min_population* from OpenStreetMap."""
    query = f"""
    [out:json][timeout:60];
    area["ISO3166-1"="{country_code.upper()}"]->.country;
    (
      node["place"="city"](area.country);
    );
    out body;
    """
    data = _overpass_query(query, timeout=120)
    cities = []
    for el in data.get("elements", []):
        name = el.get("tags", {}).get("name", "")
        pop_str = el.get("tags", {}).get("population", "0")
        try:
            pop = int(pop_str)
        except ValueError:
            pop = 0
        if pop >= min_population:
            cities.append({
                "name": name,
                "lat": el["lat"],
                "lon": el["lon"],
                "population": pop,
            })
    return sorted(cities, key=lambda c: -c["population"])


def get_country_boundary(country_code: str):
    """Return country boundary GeoJSON from Overpass."""
    query = f"""
    [out:json][timeout:120];
    relation["ISO3166-1"="{country_code.upper()}"]\
["boundary"="administrative"]["admin_level"="2"];
    out geom;
    """
    return _overpass_query(query, timeout=180)


# ---------------------------------------------------------------------------
# Coverage polygon builders
# ---------------------------------------------------------------------------

def build_city_coverage(cities: list, buffer_km: float | None = None):
    """Circular buffers around cities, scaled by population."""
    from shapely.geometry import MultiPolygon, Point
    from shapely.ops import unary_union

    buffers = []
    for city in cities:
        if buffer_km is not None:
            radius_km = buffer_km
        else:
            pop = city["population"]
            if pop > 1_000_000:
                radius_km = 30
            elif pop > 500_000:
                radius_km = 20
            elif pop > 100_000:
                radius_km = 15
            else:
                radius_km = 10
        radius_deg = radius_km / 111.0
        buffers.append(Point(city["lon"], city["lat"]).buffer(radius_deg))

    merged = unary_union(buffers)
    if isinstance(merged, MultiPolygon):
        return list(merged.geoms)
    return [merged]


def build_country_coverage(country_code: str):
    """Full country boundary polygon(s)."""
    from shapely.geometry import MultiPolygon, Polygon

    data = get_country_boundary(country_code)
    polygons = []
    for el in data.get("elements", []):
        if el.get("type") != "relation":
            continue
        for member in el.get("members", []):
            if member.get("type") == "way" and "geometry" in member:
                coords = [(g["lon"], g["lat"]) for g in member["geometry"]]
                if len(coords) >= 4:
                    try:
                        p = Polygon(coords)
                        if p.is_valid and p.area > 0:
                            polygons.append(p)
                    except Exception:
                        pass
    if not polygons:
        raise RuntimeError(
            f"No valid boundary polygons extracted for {country_code}. "
            "Try providing a GeoJSON file manually."
        )
    # Merge and keep only significant polygons (>1% of total area)
    from shapely.ops import unary_union
    merged = unary_union(polygons)
    if isinstance(merged, MultiPolygon):
        total_area = merged.area
        return [p for p in merged.geoms if p.area > total_area * 0.01]
    return [merged]


# ---------------------------------------------------------------------------
# Hex grid generation
# ---------------------------------------------------------------------------

def generate_hex_grid(polygons: list, resolution: int) -> list:
    """Generate unique hex center points covering all *polygons*."""
    all_hexes = set()
    for polygon in polygons:
        coords = list(polygon.exterior.coords)
        geojson_coords = [[(lng, lat) for lat, lng in coords]]
        hexes = _polyfill(geojson_coords, resolution)
        all_hexes.update(hexes)

    points = []
    seen = set()
    for hex_id in all_hexes:
        lat, lon = _hex_to_latlng(hex_id)
        key = (round(lat, 6), round(lon, 6))
        if key not in seen:
            seen.add(key)
            points.append({"lat": round(lat, 8), "lon": round(lon, 8)})
    return points


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="ConSo hex grid generator")
    p.add_argument("--prefix", required=True, help="2-letter country code (e.g. NL)")
    p.add_argument("--distance", type=int, default=3000,
                   help="Grid spacing in meters (default: 3000)")
    p.add_argument("--mode", choices=["cities-only", "full-country"],
                   default="cities-only",
                   help="Coverage mode (default: cities-only)")
    p.add_argument("--cities", default=None,
                   help="Comma-separated city names (cities-only mode); "
                        "omit for all cities > --min-population")
    p.add_argument("--min-population", type=int, default=100000,
                   help="Min city population filter (default: 100000)")
    p.add_argument("--top-n", type=int, default=None,
                   help="Keep only top N cities by population")
    p.add_argument("--buffer-km", type=float, default=None,
                   help="Override auto city buffer radius (km)")
    p.add_argument("--output", required=True,
                   help="Output JSON file path (e.g. location/NL_3000_grid.json)")
    return p.parse_args()


def main():
    args = parse_args()
    prefix = args.prefix.upper()
    distance = args.distance
    resolution = distance_to_h3_resolution(distance)

    _log(f"h3 version: {'v4' if H3_V4 else 'v3'} ({h3.__version__})")
    _log(f"H3 resolution: {resolution} (distance={distance}m)")

    # ---- Build coverage polygons ----
    if args.mode == "cities-only":
        _log(f"Fetching cities for {prefix} (min pop: {args.min_population})…")
        cities = get_major_cities(prefix, min_population=args.min_population)

        if args.cities:
            # Filter to named cities
            wanted = {c.strip().lower() for c in args.cities.split(",")}
            cities = [c for c in cities if c["name"].lower() in wanted]
            if not cities:
                _log(f"ERROR: none of the requested cities found in Overpass data "
                     f"for {prefix}. Available: "
                     + ", ".join(c["name"] for c in
                                get_major_cities(prefix, min_population=0)[:20]))
                sys.exit(1)

        if args.top_n and len(cities) > args.top_n:
            cities = cities[:args.top_n]

        if not cities:
            _log(f"ERROR: no cities found for {prefix} with population >= "
                 f"{args.min_population}. Try lowering --min-population.")
            sys.exit(1)

        _log(f"Found {len(cities)} cities: "
             + ", ".join(f"{c['name']}({c['population']})" for c in cities[:10])
             + ("…" if len(cities) > 10 else ""))

        polygons = build_city_coverage(cities, buffer_km=args.buffer_km)
        coverage_desc = (f"{len(cities)} cities >={args.min_population} pop "
                         f"in {prefix}")
    else:
        _log(f"Fetching country boundary for {prefix}…")
        polygons = build_country_coverage(prefix)
        coverage_desc = f"full country boundary for {prefix}"

    _log(f"Coverage: {len(polygons)} polygon(s) — {coverage_desc}")

    # ---- Generate hex grid ----
    _log(f"Generating hex grid at resolution {resolution}…")
    points = generate_hex_grid(polygons, resolution)
    _log(f"Generated {len(points)} unique grid points")

    if not points:
        _log("ERROR: zero points generated. Check boundary data.")
        sys.exit(1)

    # ---- Save ----
    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    grid_data = {"data": points}
    with open(args.output, "w") as f:
        json.dump(grid_data, f)

    file_size = os.path.getsize(args.output)
    lats = [p["lat"] for p in points]
    lons = [p["lon"] for p in points]

    # ---- Structured result on stdout ----
    result = {
        "verdict": "generated",
        "prefix": prefix,
        "distance": distance,
        "h3_resolution": resolution,
        "point_count": len(points),
        "coverage": coverage_desc,
        "output_file": args.output,
        "file_size_bytes": file_size,
        "bounding_box": {
            "min_lat": round(min(lats), 6),
            "max_lat": round(max(lats), 6),
            "min_lon": round(min(lons), 6),
            "max_lon": round(max(lons), 6),
        },
        "h3_version": "v4" if H3_V4 else "v3",
    }
    json.dump(result, sys.stdout, indent=2)
    print()  # trailing newline

    _log(f"✅ Saved {len(points)} points to {args.output} "
         f"({file_size / 1024:.1f} KB)")


if __name__ == "__main__":
    main()

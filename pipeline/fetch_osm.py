"""Fetch OpenStreetMap context for the LiDAR block and rasterise it.

The plan (section 5.1) asks for an OSM-derived public-access mask; the first
implementation inferred walkability from LiDAR alone and recommended highways,
farm plots and the middle of the Ebro. OSM resolves all three directly:

  * positive space - parks, plazas, gardens, pitches, pedestrian areas
  * hard exclusions - water, motorways, railways, industrial and military land
  * building footprints and names, used for map labels

Output: data/output/osm_*.tif masks on the pipeline grid, plus
viewer/public/data/osm_labels.json for the viewer.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
import pyproj

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, build_grid, list_tiles, load_config, output_dir, save_json, write_raster  # noqa: E402

OVERPASS = "https://overpass-api.de/api/interpreter"

# Areas where standing to watch an eclipse is plausible and normally permitted.
POSITIVE_QUERY = """
  way["leisure"~"^(park|garden|pitch|common|recreation_ground|dog_park)$"]({bbox});
  relation["leisure"~"^(park|garden|pitch|common|recreation_ground|dog_park)$"]({bbox});
  way["landuse"~"^(grass|recreation_ground|village_green|cemetery|allotments)$"]({bbox});
  relation["landuse"~"^(grass|recreation_ground|village_green|cemetery)$"]({bbox});
  way["highway"="pedestrian"][area="yes"]({bbox});
  way["place"="square"]({bbox});
  relation["place"="square"]({bbox});
"""

# Car parks are open and usually accessible, but a supermarket car park is a
# worse recommendation than a park of equal clearance, so they are rasterised
# separately and only down-weighted rather than excluded.
PARKING_QUERY = """
  way["amenity"="parking"]["parking"!="underground"]({bbox});
  relation["amenity"="parking"]["parking"!="underground"]({bbox});
"""

# Places that must never be recommended.
NEGATIVE_QUERY = """
  way["natural"="water"]({bbox});
  relation["natural"="water"]({bbox});
  way["waterway"~"^(riverbank|river|stream|canal)$"]({bbox});
  relation["waterway"="riverbank"]({bbox});
  way["landuse"~"^(industrial|military|railway|quarry|landfill|farmland|farmyard|orchard|vineyard|forest)$"]({bbox});
  relation["landuse"~"^(industrial|military|railway|quarry|farmland|forest)$"]({bbox});
  way["highway"~"^(motorway|trunk|primary|secondary|tertiary|motorway_link|trunk_link|primary_link)$"]({bbox});
  way["railway"~"^(rail|light_rail|subway|tram)$"]({bbox});
  way["aeroway"]({bbox});
  way["natural"="wood"]({bbox});
  relation["natural"="wood"]({bbox});
"""


def overpass(query: str, retries: int = 4) -> dict:
    body = f"[out:json][timeout:180];({query});out geom;"
    data = urllib.parse.urlencode({"data": body}).encode()
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                OVERPASS, data=data,
                headers={"User-Agent": "zaragoza-eclipse-visibility/0.1 (offline pipeline)"},
            )
            with urllib.request.urlopen(req, timeout=200) as r:
                return json.loads(r.read().decode())
        except Exception as exc:  # noqa: BLE001 - retry on any transport/server error
            last = exc
            wait = 5 * (attempt + 1)
            print(f"    overpass attempt {attempt + 1} failed ({exc}); retrying in {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"Overpass failed after {retries} attempts: {last}")


def overpass_bbox(query_tpl: str, bbox: str, depth: int = 2) -> dict:
    """Run a `{bbox}`-templated query, splitting the box when the server gives up.

    Overpass answers a query for the old 7 x 7 km block without complaint but starts
    returning 504s over a block several times that size, and retrying the same oversized
    query just fails more slowly. Quartering the box turns one request the server refuses
    into four it will accept. Elements straddling a cut come back from both halves, so
    results are deduplicated on (type, id).
    """
    try:
        return overpass(query_tpl.format(bbox=bbox))
    except RuntimeError:
        if depth <= 0:
            raise
        s, w, n, e = (float(v) for v in bbox.split(","))
        ms, me = (s + n) / 2, (w + e) / 2
        quads = [f"{s},{w},{ms},{me}", f"{s},{me},{ms},{e}",
                 f"{ms},{w},{n},{me}", f"{ms},{me},{n},{e}"]
        print(f"    splitting bbox into 4 (depth {depth})")
        seen, merged = set(), []
        for q in quads:
            for el in overpass_bbox(query_tpl, q, depth - 1).get("elements", []):
                key = (el.get("type"), el.get("id"))
                if key not in seen:
                    seen.add(key)
                    merged.append(el)
        return {"elements": merged}


def _rings(el):
    """Yield coordinate rings (lon, lat) for a way or multipolygon relation."""
    if el["type"] == "way" and "geometry" in el:
        yield [(p["lon"], p["lat"]) for p in el["geometry"]]
    elif el["type"] == "relation":
        for m in el.get("members", []):
            if m.get("role") in ("outer", "") and "geometry" in m:
                yield [(p["lon"], p["lat"]) for p in m["geometry"]]


def rasterise(elements, grid, to_utm, buffer_m=0.0, line_width_m=0.0):
    """Burn OSM geometries onto the pipeline grid."""
    from rasterio.features import rasterize
    from shapely.geometry import LineString, Polygon

    shapes = []
    for el in elements:
        for ring in _rings(el):
            if len(ring) < 2:
                continue
            xs, ys = to_utm.transform([p[0] for p in ring], [p[1] for p in ring])
            coords = list(zip(xs, ys))
            closed = len(coords) >= 4 and coords[0] == coords[-1]
            try:
                if closed:
                    geom = Polygon(coords)
                    if not geom.is_valid:
                        geom = geom.buffer(0)
                elif line_width_m > 0:
                    geom = LineString(coords).buffer(line_width_m / 2)
                else:
                    geom = Polygon(coords).buffer(0) if len(coords) >= 4 else None
                if geom is None or geom.is_empty:
                    continue
                if buffer_m:
                    geom = geom.buffer(buffer_m)
                shapes.append((geom, 1))
            except Exception:
                continue

    if not shapes:
        return np.zeros((grid.height, grid.width), dtype=np.uint8)
    return rasterize(
        shapes, out_shape=(grid.height, grid.width), transform=grid.transform,
        fill=0, default_value=1, dtype=np.uint8, all_touched=True,
    )


def main():
    t0 = time.time()
    cfg = load_config()
    grid = build_grid(cfg, list_tiles(cfg))
    out_dir = output_dir(cfg)

    to_wgs = pyproj.Transformer.from_crs(grid.crs, "EPSG:4326", always_xy=True)
    to_utm = pyproj.Transformer.from_crs("EPSG:4326", grid.crs, always_xy=True)
    w, s = to_wgs.transform(grid.left, grid.bottom)
    e, n = to_wgs.transform(grid.right, grid.top)
    bbox = f"{s},{w},{n},{e}"
    print(f"Bounding box: {bbox}")

    print("Fetching positive (open public space) features...")
    pos = overpass_bbox(POSITIVE_QUERY, bbox)
    print(f"  {len(pos['elements'])} elements")

    print("Fetching negative (excluded) features...")
    neg = overpass_bbox(NEGATIVE_QUERY, bbox)
    print(f"  {len(neg['elements'])} elements")

    print("Rasterising...")
    positive = rasterise(pos["elements"], grid, to_utm)

    # Roads and rail are lines; give them a realistic corridor width so the
    # carriageway and its verge are both excluded.
    linear = [el for el in neg["elements"]
              if el.get("tags", {}).get("highway") or el.get("tags", {}).get("railway")]
    areal = [el for el in neg["elements"] if el not in linear]
    negative = rasterise(areal, grid, to_utm, buffer_m=5.0)
    negative |= rasterise(linear, grid, to_utm, line_width_m=22.0)

    print("Fetching car parks...")
    try:
        park = overpass_bbox(PARKING_QUERY, bbox)
        parking = rasterise(park["elements"], grid, to_utm)
        print(f"  {len(park['elements'])} elements, {parking.mean() * 100:.2f}% of grid")
    except RuntimeError as exc:
        print(f"  WARNING: car park query failed ({exc}); continuing without")
        parking = np.zeros((grid.height, grid.width), dtype=np.uint8)

    write_raster(out_dir / "osm_positive.tif", positive, grid, dtype="uint8", nodata=255)
    write_raster(out_dir / "osm_negative.tif", negative, grid, dtype="uint8", nodata=255)
    write_raster(out_dir / "osm_parking.tif", parking, grid, dtype="uint8", nodata=255)
    print(f"  positive: {positive.mean() * 100:.2f}% of grid")
    print(f"  negative: {negative.mean() * 100:.2f}% of grid")

    # Named features, for map labels and for describing each zone in words.
    # Overpass rate-limits aggressively; the masks above are what actually
    # constrain the recommendations, so a failure here must not lose them.
    print("Fetching named places for labels...")
    try:
        named = overpass_bbox(
            'way["name"]["leisure"~"^(park|garden|pitch)$"]({bbox});'
            'relation["name"]["leisure"="park"]({bbox});'
            'way["name"]["place"="square"]({bbox});'
            'node["name"]["place"~"^(square|neighbourhood|suburb)$"]({bbox});'
            'way["name"]["amenity"~"^(university|hospital|marketplace)$"]({bbox});',
            bbox,
        )
    except RuntimeError as exc:
        print(f"  WARNING: label query failed ({exc}); writing empty label set")
        save_json(out_dir / "osm_labels.json", {"count": 0, "labels": [], "error": str(exc)})
        print(f"Elapsed: {time.time() - t0:.1f}s")
        return
    labels = []
    for el in named["elements"]:
        name = el.get("tags", {}).get("name")
        if not name:
            continue
        if el["type"] == "node":
            lon, lat = el["lon"], el["lat"]
        else:
            pts = [p for ring in _rings(el) for p in ring]
            if not pts:
                continue
            lon = sum(p[0] for p in pts) / len(pts)
            lat = sum(p[1] for p in pts) / len(pts)
        tags = el.get("tags", {})
        labels.append({
            "name": name,
            "lonlat": [round(lon, 6), round(lat, 6)],
            "kind": tags.get("leisure") or tags.get("place") or tags.get("amenity") or "place",
        })
    save_json(out_dir / "osm_labels.json", {"count": len(labels), "labels": labels})
    print(f"  {len(labels)} named features")
    print(f"Elapsed: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()

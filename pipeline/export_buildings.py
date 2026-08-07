"""Export OSM building footprints with LiDAR-measured heights, for the 3D view.

The 3D view used to extrude the DSM as a heightmap. MapLibre meshes a heightmap, so
buildings came out as rounded mounds with the orthophoto smeared down their sides. OSM
gives clean footprint polygons instead, and the LiDAR gives a real height for each one, so
the two together produce crisp boxes standing at their measured height.

Heights come from building_height.tif (surface minus terrain, per cell) rather than from
OSM's own `height` or `building:levels` tags, which are sparsely populated here. Each
footprint takes a high percentile of the cells it covers, not the maximum: a single
chimney, aerial or parapet pixel would otherwise lift the whole block.

    python pipeline/export_buildings.py

Writes viewer/public/data/buildings.geojson. Needs network access (Overpass) and
data/output/building_height.tif from build_surfaces.py.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, build_grid, list_tiles, load_config, output_dir, read_raster  # noqa: E402
from fetch_osm import _rings, overpass  # noqa: E402

# Below this a "building" is a shed, a bin store or a digitising artefact. It carries no
# useful shadow at a 6 deg sun and only adds polygons for the browser to draw.
MIN_HEIGHT_M = 2.0
MIN_AREA_M2 = 12.0

# 90th percentile of the covered cells. The max is set by chimneys, lift overruns and
# parapet edges; the median sags on buildings with a large central courtyard or atrium.
HEIGHT_PERCENTILE = 90


def main() -> None:
    t0 = time.time()
    cfg = load_config()
    grid = build_grid(cfg, list_tiles(cfg))
    out_dir = output_dir(cfg)
    web_dir = ROOT / cfg["paths"]["viewer_public_data"]
    web_dir.mkdir(parents=True, exist_ok=True)

    import pyproj
    from shapely.geometry import Polygon, mapping

    to_utm = pyproj.Transformer.from_crs("EPSG:4326", grid.crs, always_xy=True)
    to_wgs = pyproj.Transformer.from_crs(grid.crs, "EPSG:4326", always_xy=True)

    w, s = to_wgs.transform(grid.left, grid.bottom)
    e, n = to_wgs.transform(grid.right, grid.top)
    bbox = f"{s},{w},{n},{e}"

    # Overpass rate-limits hard and this query is a big one, so the raw response is
    # cached. Re-running to tune heights or thresholds then costs nothing and does not
    # hammer a free public service.
    cache = out_dir / "osm_buildings_raw.json"
    if cache.exists():
        print(f"Using cached Overpass response ({cache.name})")
        data = json.loads(cache.read_text(encoding="utf-8"))
    else:
        print("Fetching building footprints from Overpass...")
        data = overpass(f'way["building"]({bbox});relation["building"]({bbox});')
        cache.write_text(json.dumps(data), encoding="utf-8")
    print(f"  {len(data['elements'])} raw elements")

    height, _ = read_raster(out_dir / "building_height.tif")
    height = np.where(height <= -100, np.nan, height)

    features = []
    skipped_small = skipped_low = 0
    for el in data["elements"]:
        tags = el.get("tags", {})
        for ring in _rings(el):
            if len(ring) < 4:
                continue
            xs, ys = to_utm.transform([p[0] for p in ring], [p[1] for p in ring])
            try:
                poly = Polygon(zip(xs, ys))
                if not poly.is_valid:
                    # buffer(0) repairs self-intersecting rings, but a bow-tie splits
                    # into a MultiPolygon. Keep the largest part; the slivers are
                    # digitising noise, not separate buildings.
                    poly = poly.buffer(0)
                    if poly.geom_type == "MultiPolygon":
                        poly = max(poly.geoms, key=lambda g: g.area)
            except Exception:
                continue
            if poly.is_empty or poly.geom_type != "Polygon" or poly.area < MIN_AREA_M2:
                skipped_small += 1
                continue

            # Sample the height raster over the footprint's bounding box and keep the
            # cells actually inside it. Cheaper than rasterising every polygon, and the
            # percentile is insensitive to the few extra cells a bbox would add anyway.
            minx, miny, maxx, maxy = poly.bounds
            c0 = int(np.floor((minx - grid.left) / grid.resolution))
            c1 = int(np.ceil((maxx - grid.left) / grid.resolution))
            r0 = int(np.floor((grid.top - maxy) / grid.resolution))
            r1 = int(np.ceil((grid.top - miny) / grid.resolution))
            c0, c1 = max(0, c0), min(grid.width, c1)
            r0, r1 = max(0, r0), min(grid.height, r1)
            if c1 <= c0 or r1 <= r0:
                continue
            block = height[r0:r1, c0:c1]
            vals = block[np.isfinite(block)]
            if vals.size == 0:
                skipped_low += 1
                continue
            h = float(np.percentile(vals, HEIGHT_PERCENTILE))
            if not np.isfinite(h) or h < MIN_HEIGHT_M:
                skipped_low += 1
                continue

            lon, lat = to_wgs.transform(*poly.exterior.coords.xy)
            wgs = Polygon(zip(lon, lat))
            features.append({
                "type": "Feature",
                "properties": {
                    "h": round(h, 1),
                    "name": tags.get("name"),
                },
                "geometry": mapping(wgs),
            })

    fc = {"type": "FeatureCollection", "features": features}
    dst = web_dir / "buildings.geojson"
    # Compact: this file is fetched by the browser and indentation would roughly double it.
    dst.write_text(json.dumps(fc, separators=(",", ":")), encoding="utf-8")

    hs = np.array([f["properties"]["h"] for f in features])
    print(f"  kept {len(features)} footprints "
          f"(skipped {skipped_small} tiny, {skipped_low} with no measurable height)")
    if hs.size:
        print(f"  height p50 {np.percentile(hs, 50):.1f} m  "
              f"p90 {np.percentile(hs, 90):.1f} m  max {hs.max():.1f} m")
    print(f"  wrote {dst} ({dst.stat().st_size / 1e6:.1f} MB)")
    print(f"Elapsed: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()

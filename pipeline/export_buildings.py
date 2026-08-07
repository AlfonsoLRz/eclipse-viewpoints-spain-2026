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


def stitch_rings(el):
    """Yield closed coordinate rings for a way or multipolygon relation.

    `_rings` in fetch_osm yields each relation member as its own ring, which is fine when
    it only has to burn a mask: the fragments rasterise to the same cells either way. Here
    it silently loses buildings. OSM splits a large outline across many ways, so the
    Basilica del Pilar arrives as 58 separate two-point fragments, none of them a closed
    polygon and every one of them discarded as degenerate. The building simply vanished.

    So walk the fragments and join them end to end into closed rings, matching each
    fragment's endpoints against the ring being built, reversing where the way was
    digitised in the opposite direction.
    """
    if el["type"] == "way":
        yield from _rings(el)
        return

    segs = [list(r) for r in _rings(el) if len(r) >= 2]
    while segs:
        ring = segs.pop(0)
        if ring[0] == ring[-1] and len(ring) >= 4:
            yield ring
            continue
        joined = True
        while joined and ring[0] != ring[-1]:
            joined = False
            for i, s in enumerate(segs):
                if s[0] == ring[-1]:
                    ring += s[1:]
                elif s[-1] == ring[-1]:
                    ring += s[-2::-1]
                elif s[-1] == ring[0]:
                    ring = s[:-1] + ring
                elif s[0] == ring[0]:
                    ring = s[:0:-1] + ring
                else:
                    continue
                segs.pop(i)
                joined = True
                break
        if len(ring) >= 4 and ring[0] == ring[-1]:
            yield ring

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

    # Overpass rate-limits hard and these queries are big, so the raw responses are
    # cached. Re-running to tune heights or thresholds then costs nothing and does not
    # hammer a free public service. Delete the cache files to refetch.
    def fetch(name: str, query: str) -> dict:
        cache = out_dir / f"osm_{name}_raw.json"
        if cache.exists():
            print(f"Using cached Overpass response ({cache.name})")
            return json.loads(cache.read_text(encoding="utf-8"))
        print(f"Fetching {name} from Overpass...")
        d = overpass(query)
        cache.write_text(json.dumps(d), encoding="utf-8")
        return d

    data = fetch("buildings", f'way["building"]({bbox});relation["building"]({bbox});')
    print(f"  {len(data['elements'])} raw elements")

    # Simple 3D Buildings: a mapped building can be split into parts (tower, nave, wing,
    # setback), each with its own height. Extruding only the outer footprint flattens all
    # of that into one slab, which is what makes a cathedral look like a warehouse.
    # Where parts exist they replace their parent; where they do not, the footprint stands.
    try:
        parts_data = fetch("buildingparts",
                           f'way["building:part"]({bbox});relation["building:part"]({bbox});')
        print(f"  {len(parts_data['elements'])} building:part elements")
    except RuntimeError as exc:
        print(f"  WARNING: building:part query failed ({exc}); using footprints only")
        parts_data = {"elements": []}

    height, _ = read_raster(out_dir / "building_height.tif")
    height = np.where(height <= -100, np.nan, height)

    stats = {"small": 0, "nolidar": 0}

    def to_polys(el):
        """Valid UTM polygons for one OSM element, largest part of any repaired bow-tie."""
        out = []
        for ring in stitch_rings(el):
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
                stats["small"] += 1
                continue
            out.append(poly)
        return out

    from rasterio.features import rasterize
    from rasterio.transform import from_origin

    def lidar_height(poly):
        """Measured height inside a polygon, or None where the LiDAR saw no building.

        Masked to the polygon, not to its bounding box. A bounding box is close enough
        for a whole footprint, but building parts sit shoulder to shoulder: the bbox of
        a bell tower contains most of the nave next to it, so every part would converge
        on the same height and the articulation this function exists to recover would be
        averaged straight back out.
        """
        minx, miny, maxx, maxy = poly.bounds
        c0 = max(0, int(np.floor((minx - grid.left) / grid.resolution)))
        c1 = min(grid.width, int(np.ceil((maxx - grid.left) / grid.resolution)))
        r0 = max(0, int(np.floor((grid.top - maxy) / grid.resolution)))
        r1 = min(grid.height, int(np.ceil((grid.top - miny) / grid.resolution)))
        if c1 <= c0 or r1 <= r0:
            return None
        block = height[r0:r1, c0:c1]
        win = from_origin(grid.left + c0 * grid.resolution,
                          grid.top - r0 * grid.resolution,
                          grid.resolution, grid.resolution)
        mask = rasterize([(poly, 1)], out_shape=block.shape, transform=win,
                         fill=0, dtype=np.uint8, all_touched=True).astype(bool)
        vals = block[mask & np.isfinite(block)]
        if vals.size == 0:
            return None
        h = float(np.percentile(vals, HEIGHT_PERCENTILE))
        return h if np.isfinite(h) and h >= MIN_HEIGHT_M else None

    # Parts first. Each is measured independently, so a tower and the nave beside it get
    # their own heights instead of being averaged into one slab.
    parts = []
    for el in parts_data["elements"]:
        for poly in to_polys(el):
            h = lidar_height(poly)
            if h is None:
                stats["nolidar"] += 1
                continue
            parts.append((poly, h, el.get("tags", {}).get("name")))

    # A part-mapped building would otherwise be drawn twice: once as its outer shell and
    # again as its parts, with the shell hiding the detail behind a flat lid. Drop any
    # footprint whose area is mostly covered by parts. Prepared geometries keep the
    # pairwise test affordable across ~11k footprints and ~9k parts.
    from shapely.prepared import prep
    from shapely.strtree import STRtree

    part_polys = [p for p, _, _ in parts]
    tree = STRtree(part_polys) if part_polys else None

    features = []
    for poly, h, name in parts:
        features.append((poly, h, name, "part"))

    superseded = 0
    for el in data["elements"]:
        tags = el.get("tags", {})
        for poly in to_polys(el):
            hits = []
            if tree is not None:
                covered = 0.0
                pre = prep(poly)
                for j in tree.query(poly):
                    if pre.intersects(part_polys[j]):
                        covered += poly.intersection(part_polys[j]).area
                        hits.append(j)
                if covered >= 0.6 * poly.area:
                    # Parts carry the detail, so the shell is dropped. The name usually
                    # lives on the shell rather than the parts, so hand it down or the
                    # landmark becomes anonymous.
                    if tags.get("name"):
                        for j in hits:
                            if features[j][2] is None:
                                p, h, _, k = features[j]
                                features[j] = (p, h, tags["name"], k)
                    superseded += 1
                    continue
            h = lidar_height(poly)
            if h is None:
                stats["nolidar"] += 1
                continue
            features.append((poly, h, tags.get("name"), "building"))

    out_features = []
    for poly, h, name, kind in features:
        lon, lat = to_wgs.transform(*poly.exterior.coords.xy)
        out_features.append({
            "type": "Feature",
            "properties": {"h": round(h, 1), "name": name, "kind": kind},
            "geometry": mapping(Polygon(zip(lon, lat))),
        })

    fc = {"type": "FeatureCollection", "features": out_features}
    dst = web_dir / "buildings.geojson"
    # Compact: this file is fetched by the browser and indentation would roughly double it.
    dst.write_text(json.dumps(fc, separators=(",", ":")), encoding="utf-8")

    hs = np.array([f["properties"]["h"] for f in out_features])
    n_part = sum(1 for f in out_features if f["properties"]["kind"] == "part")
    print(f"  kept {len(out_features)} extrusions "
          f"({n_part} building parts, {len(out_features) - n_part} whole footprints)")
    print(f"  {superseded} footprints superseded by their parts; "
          f"skipped {stats['small']} tiny, {stats['nolidar']} with no measurable height")
    if hs.size:
        print(f"  height p50 {np.percentile(hs, 50):.1f} m  "
              f"p90 {np.percentile(hs, 90):.1f} m  max {hs.max():.1f} m")
    print(f"  wrote {dst} ({dst.stat().st_size / 1e6:.1f} MB)")
    print(f"Elapsed: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()

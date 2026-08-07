"""Phase 4 - walkable mask, candidate zones and base ranking.

Walkability combines two sources:

  * LiDAR geometry - open, near-flat, well-sampled ground that is not a
    building, not under canopy and not water.
  * OpenStreetMap land use (pipeline/fetch_osm.py) - parks, squares, gardens,
    pitches, pedestrian areas and car parks are eligible; water, motorways,
    railways, industrial, military and farmland are excluded outright.

LiDAR geometry alone is not sufficient. An earlier version used it by itself
and recommended motorway verges, farm plots and the surface of the Ebro, all of
which are genuinely flat, open and unobstructed. OSM is what makes the output
answer "somewhere a person can actually stand" rather than "somewhere the sun
happens to reach the ground".
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
from scipy import ndimage

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    build_grid,
    list_tiles,
    load_config,
    output_dir,
    read_raster,
    save_json,
    write_raster,
)

NODATA = -9999.0

# clearance_class codes written by compute_visibility.py
CLASS_NODATA = -1
CLASS_BLOCKED = 0
CLASS_FRAGILE = 1
CLASS_ACCEPTABLE = 2
CLASS_GOOD = 3
CLASS_EXCELLENT = 4

CLASS_LABELS = {
    CLASS_BLOCKED: "blocked",
    CLASS_FRAGILE: "fragile",
    CLASS_ACCEPTABLE: "acceptable",
    CLASS_GOOD: "good",
    CLASS_EXCELLENT: "excellent",
}

# Representative clearance in degrees for each class, used for reporting.
CLASS_MIN_CLEARANCE_DEG = {
    CLASS_BLOCKED: 0.0,
    CLASS_FRAGILE: 0.0,
    CLASS_ACCEPTABLE: 0.5,
    CLASS_GOOD: 1.0,
    CLASS_EXCELLENT: 2.0,
}

OBSTACLE_LABELS = {0: "none", 1: "terrain", 2: "building", 3: "vegetation", 255: "unknown"}


def build_walkable_mask(cfg, out_dir: Path, grid):
    """Open, near-flat, well-sampled ground that is not built on or under canopy."""
    dtm, _ = read_raster(out_dir / "dtm.tif")
    dtm = np.where(dtm == NODATA, np.nan, dtm)
    building_mask, _ = read_raster(out_dir / "building_mask.tif")
    vegetation_mask, _ = read_raster(out_dir / "vegetation_mask.tif")
    canopy, _ = read_raster(out_dir / "canopy_height.tif")
    canopy = np.where(canopy == NODATA, np.nan, canopy)
    density, _ = read_raster(out_dir / "point_density.tif")

    known = ~np.isnan(dtm)

    # Slope from the DTM: steep ground is not usable standing room.
    res = grid.resolution
    gy, gx = np.gradient(np.where(known, dtm, np.nan), res)
    slope_deg = np.degrees(np.arctan(np.hypot(gy, gx)))
    slope_ok = np.nan_to_num(slope_deg, nan=90.0) <= 15.0

    # Buildings are excluded together with a small margin, because airborne
    # LiDAR resolves roofs far better than facades and footprints bleed.
    built = building_mask > 0
    built_buffered = ndimage.binary_dilation(built, iterations=3)

    # Standing directly beneath a canopy is not usable for observing a 6 deg sun.
    under_canopy = (vegetation_mask > 0) & (np.nan_to_num(canopy, nan=0.0) > 2.0)
    under_canopy = ndimage.binary_dilation(under_canopy, iterations=1)

    # Water gives few or no LiDAR returns, so river and pond surfaces show up as
    # sparse cells sitting well below the surrounding terrain. Without this the
    # Ebro itself is "flat, open, unobstructed ground" and ranks superbly.
    zcfg = cfg["zones"]
    water_density = float(zcfg.get("water_max_point_density", 2.5))
    sparse = density < water_density
    # A 101x101 median over 49 Mcells costs many minutes; a median on a 10x
    # decimated grid, then nearest-neighbour expanded, is visually identical at
    # this scale (we only need "is this cell well below its surroundings") and
    # runs in seconds.
    step = 10
    coarse = np.where(known, dtm, np.nan)[::step, ::step]
    coarse_ref = ndimage.median_filter(np.nan_to_num(coarse, nan=np.nanmedian(coarse)), size=11)
    terrain_ref = np.repeat(np.repeat(coarse_ref, step, axis=0), step, axis=1)
    terrain_ref = terrain_ref[:dtm.shape[0], :dtm.shape[1]]
    below_surroundings = (terrain_ref - np.where(known, dtm, np.inf)) > 1.5
    water = sparse & below_surroundings
    water = ndimage.binary_closing(water, iterations=5)
    water = ndimage.binary_dilation(water, iterations=int(zcfg.get("water_buffer_m", 8)))

    well_sampled = density >= float(zcfg.get("min_point_density", 3.0))

    # Restrict to genuinely urban surroundings. Distance to the NEAREST building
    # is a poor proxy - farm plots, motorway verges and gravel riverbanks all sit
    # within ~100 m of some isolated structure, which is why the first version
    # recommended highways and the middle of the river. Local building DENSITY
    # separates "a square inside the city" from "a field with a shed next to it".
    radius = int(zcfg.get("urban_context_radius_m", 250))
    built_fraction = ndimage.uniform_filter(built.astype(np.float32), size=radius)
    urban = built_fraction >= float(zcfg.get("min_built_fraction", 0.08))

    walkable = (known & slope_ok & ~built_buffered & ~under_canopy
                & well_sampled & urban & ~water)

    # OpenStreetMap land use is far more reliable than anything inferable from
    # the point cloud: it names the river, the motorways, the railway corridor,
    # the industrial estates and the farmland outright, and it marks the parks
    # and squares that are actually meant to be stood in.
    osm_pos_path = out_dir / "osm_positive.tif"
    osm_neg_path = out_dir / "osm_negative.tif"
    if osm_neg_path.exists():
        osm_negative, _ = read_raster(osm_neg_path)
        walkable &= osm_negative == 0
        print("  applied OSM exclusions (water, roads, rail, industrial, farmland)")
    else:
        print("  WARNING: osm_negative.tif missing - run pipeline/fetch_osm.py first")

    parking = np.zeros_like(building_mask)
    park_path = out_dir / "osm_parking.tif"
    if park_path.exists():
        parking, _ = read_raster(park_path)

    if osm_pos_path.exists() and bool(zcfg.get("require_osm_public_space", True)):
        osm_positive, _ = read_raster(osm_pos_path)
        # Parks and squares are irregular; allow a short spill so the paved edge
        # of a plaza is not shaved off by polygon generalisation in OSM.
        spill = int(zcfg.get("osm_positive_buffer_m", 15))
        allowed = osm_positive > 0
        if bool(zcfg.get("include_parking", True)):
            allowed |= parking > 0
        near_public = ndimage.binary_dilation(allowed, iterations=spill)
        walkable &= near_public
        print("  restricted to OSM parks, squares, gardens, pitches, pedestrian areas"
              + (" and car parks" if bool(zcfg.get("include_parking", True)) else ""))

    # Remove speckle, then close pinholes so plazas are not shredded.
    walkable = ndimage.binary_opening(walkable, iterations=int(zcfg["morphology_opening_iterations"]))
    walkable = ndimage.binary_closing(walkable, iterations=2)

    # Drop narrow corridors. A carriageway or rail line survives the tests above
    # (flat, open, well sampled, inside the city) but is never a viewing spot.
    # Opening by half the minimum useful width erases anything narrower than that
    # while leaving wide plazas untouched. This is deliberately a morphological
    # OPENING rather than reconstruction: a plaza joined to a road by a path must
    # keep the plaza and lose the road, and reconstruction would restore both.
    half_width = max(1, int(float(zcfg.get("min_usable_width_m", 25)) / (2 * res)))
    walkable = ndimage.binary_opening(walkable, iterations=half_width)

    return walkable, slope_deg, density, known, parking


def load_places(out_dir: Path) -> list:
    path = out_dir / "osm_labels.json"
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("labels", [])
    except Exception:
        return []


def nearest_place_name(lon: float, lat: float, places: list, max_km: float = 0.35):
    """Name of the closest OSM park/square, so a zone reads as a real place."""
    if not places:
        return None
    best, best_d = None, 1e9
    for p in places:
        plon, plat = p["lonlat"]
        # equirectangular approximation is ample at this scale
        dx = (plon - lon) * 111.32 * np.cos(np.radians(lat))
        dy = (plat - lat) * 110.57
        d = float(np.hypot(dx, dy))
        if d < best_d:
            best_d, best = d, p
    return best["name"] if best_d <= max_km else None


def local_prominence(dtm: np.ndarray, known: np.ndarray, radius_cells: int = 300) -> np.ndarray:
    """Height above the local mean terrain (plan section 6.4).

    A uniform filter over a 300 m radius is used instead of an exact circular
    neighbourhood; at 1 m resolution the difference is not material and the
    separable filter is orders of magnitude faster over 49 Mcells.
    """
    filled = np.where(known, dtm, np.nan)
    mean_local = ndimage.uniform_filter(np.nan_to_num(filled, nan=0.0), size=radius_cells)
    weight = ndimage.uniform_filter(known.astype(np.float32), size=radius_cells)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean_local = np.where(weight > 0.05, mean_local / np.maximum(weight, 1e-6), np.nan)
    return filled - mean_local


def main():
    t0 = time.time()
    cfg = load_config()
    tiles = list_tiles(cfg)
    grid = build_grid(cfg, tiles)
    out_dir = output_dir(cfg)

    print("Building walkable mask...")
    walkable, slope_deg, density, known, parking = build_walkable_mask(cfg, out_dir, grid)
    print(f"  walkable: {walkable.mean() * 100:.2f}% of grid")
    write_raster(out_dir / "walkable_mask.tif", walkable.astype(np.uint8), grid, dtype="uint8", nodata=255)

    clearance, _ = read_raster(out_dir / "clearance_class.tif")
    limiting, _ = read_raster(out_dir / "limiting_obstacle.tif")
    dtm, _ = read_raster(out_dir / "dtm.tif")
    dtm = np.where(dtm == NODATA, np.nan, dtm)
    canopy, _ = read_raster(out_dir / "canopy_height.tif")
    canopy = np.where(canopy == NODATA, np.nan, canopy)

    print("Computing local prominence...")
    prominence = local_prominence(dtm, known)

    min_class = {v: k for k, v in CLASS_LABELS.items()}[cfg["zones"]["min_clearance_class"]]
    usable = walkable & (clearance >= min_class)
    usable = ndimage.binary_opening(usable, iterations=1)
    print(f"  usable (walkable AND clearance >= {cfg['zones']['min_clearance_class']}): "
          f"{usable.sum():,} cells")

    print("Labelling connected components...")
    labels, n_labels = ndimage.label(usable)
    print(f"  {n_labels:,} raw components")

    cell_area = grid.resolution ** 2
    min_area = float(cfg["zones"]["min_area_m2"])
    min_cells = int(np.ceil(min_area / cell_area))

    counts = np.bincount(labels.ravel())
    max_area = float(cfg["zones"].get("max_area_m2", 200_000))
    max_cells = int(max_area / cell_area)

    # Very large components are open countryside outside the built-up area
    # rather than identifiable observation spots. They are split on a coarse
    # grid so each candidate stays a place a person can actually be directed to.
    keep_ids = []
    oversized = [i for i in range(1, len(counts)) if counts[i] > max_cells]
    if oversized:
        print(f"  splitting {len(oversized)} oversized component(s) (> {max_area:.0f} m2)")
        block = int(np.sqrt(max_cells))
        next_label = labels.max() + 1
        for lab in oversized:
            sel = labels == lab
            rows, cols = np.nonzero(sel)
            r0, c0 = rows.min(), cols.min()
            tile_r = (rows - r0) // block
            tile_c = (cols - c0) // block
            tile_id = tile_r * (tile_c.max() + 1) + tile_c
            for t in np.unique(tile_id):
                m = tile_id == t
                if m.sum() < min_cells:
                    continue
                labels[rows[m], cols[m]] = next_label
                next_label += 1
        counts = np.bincount(labels.ravel())

    keep_ids = [i for i in range(1, len(counts))
                if min_cells <= counts[i] <= max_cells]
    print(f"  {len(keep_ids):,} components within [{min_area:.0f}, {max_area:.0f}] m2")

    # Rank by size first so the export stays manageable; the plan asks for a
    # handful of distinct zones rather than thousands of markers.
    keep_ids.sort(key=lambda i: counts[i], reverse=True)
    keep_ids = keep_ids[:400]

    import pyproj

    to_wgs84 = pyproj.Transformer.from_crs(grid.crs, "EPSG:4326", always_xy=True)

    places = load_places(out_dir)

    # recomputed after the oversized-component split above relabelled cells
    objects = ndimage.find_objects(labels)
    zones = []
    print("Characterising zones...")
    for rank, lab in enumerate(keep_ids):
        sl = objects[lab - 1]
        sub = labels[sl] == lab
        n_cells = int(sub.sum())
        area_m2 = n_cells * cell_area

        sub_clear = clearance[sl][sub]
        sub_dtm = dtm[sl][sub]
        sub_prom = prominence[sl][sub]
        sub_canopy = canopy[sl][sub]
        sub_density = density[sl][sub]
        sub_limit = limiting[sl][sub]

        # Representative point: farthest from the zone edge, tie-broken by
        # clearance, so the marker sits comfortably inside open space
        # (plan section 5.3) rather than on a boundary pixel.
        dist_in = ndimage.distance_transform_edt(np.pad(sub, 1))[1:-1, 1:-1]
        score_map = dist_in + 0.5 * np.where(sub, clearance[sl].astype(float), -1e9)
        ri, ci = np.unravel_index(np.argmax(np.where(sub, score_map, -np.inf)), sub.shape)
        row = sl[0].start + ri
        col = sl[1].start + ci

        x = grid.left + (col + 0.5) * grid.resolution
        y = grid.top - (row + 0.5) * grid.resolution
        lon, lat = to_wgs84.transform(x, y)

        # The strict minimum over thousands of cells is set by the single worst
        # pixel (usually a zone-edge cell clipped by a wall), which collapses
        # almost every zone to the same class. The 10th percentile describes
        # what an observer standing well inside the zone can rely on.
        min_class_zone = int(np.percentile(sub_clear, 10))
        worst_class_zone = int(sub_clear.min())
        mean_class_zone = float(sub_clear.mean())

        # Which obstacle dominates just outside the zone edge, for the "why" text.
        edge = ndimage.binary_dilation(sub, iterations=8) & ~sub
        edge_obst = limiting[sl][edge]
        edge_obst = edge_obst[(edge_obst != 0) & (edge_obst != 255)]
        if edge_obst.size:
            vals, cnts = np.unique(edge_obst, return_counts=True)
            main_obstacle = OBSTACLE_LABELS[int(vals[np.argmax(cnts)])]
        else:
            main_obstacle = "none"

        veg_risk = float(np.clip(np.nanmean(np.nan_to_num(sub_canopy, nan=0.0) > 0.5), 0, 1))
        data_conf = float(np.clip(np.nanmean(np.minimum(sub_density, 8.0) / 8.0), 0, 1))
        parking_fraction = float((parking[sl][sub] > 0).mean())

        zones.append({
            "id": f"zone-{rank + 1:03d}",
            "representativePoint": [round(lon, 6), round(lat, 6)],
            "representativePointUTM": [round(x, 1), round(y, 1)],
            "areaM2": round(area_m2, 1),
            "minClearanceClass": CLASS_LABELS.get(min_class_zone, "blocked"),
            "minClearanceDeg": CLASS_MIN_CLEARANCE_DEG.get(min_class_zone, 0.0),
            "worstClearanceClass": CLASS_LABELS.get(worst_class_zone, "blocked"),
            "meanClearanceClass": round(mean_class_zone, 2),
            "excellentFraction": round(float((sub_clear >= CLASS_EXCELLENT).mean()), 3),
            "visibleFraction": round(float((sub_clear >= CLASS_FRAGILE).mean()), 3),
            "groundElevationM": round(float(np.nanmean(sub_dtm)), 1),
            "localProminenceM": round(float(np.nanmean(sub_prom)), 2),
            "vegetationRisk": round(veg_risk, 3),
            "dataConfidence": round(data_conf, 3),
            "mainEdgeObstacle": main_obstacle,
            "pointDensity": round(float(np.nanmean(sub_density)), 2),
            "parkingFraction": round(parking_fraction, 3),
            "surfaceKind": "car park" if parking_fraction > 0.5 else "open space",
            "placeName": nearest_place_name(lon, lat, places),
        })

    zones.sort(key=lambda z: (-z["minClearanceDeg"], -z["areaM2"]))
    for i, z in enumerate(zones):
        z["id"] = f"zone-{i + 1:03d}"

    save_json(out_dir / "candidates.json", {
        "generated_by": "pipeline/extract_zones.py",
        "crs": grid.crs,
        "cell_area_m2": cell_area,
        "min_area_m2": min_area,
        "min_clearance_class": cfg["zones"]["min_clearance_class"],
        "access_model": "lidar_derived_unverified",
        "access_note": (
            "Walkable areas are inferred from LiDAR surfaces (open, near-flat, "
            "well-sampled ground with no building or canopy above). They are NOT "
            "verified against OpenStreetMap or municipal open data, so some zones "
            "may be private land, roadway or otherwise inaccessible."
        ),
        "count": len(zones),
        "zones": zones,
    })
    print(f"Wrote {out_dir / 'candidates.json'} ({len(zones)} zones)")
    print(f"Elapsed: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()

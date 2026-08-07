"""Phase 0 - audit the LAZ tiles: CRS, bounds, classification histogram,
point density, RGB availability, and overall footprint.

Usage:
    .venv/Scripts/python.exe pipeline/inspect_laz.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import laspy
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import build_grid, list_tiles, load_config, output_dir, save_json  # noqa: E402


def inspect_tile(tile) -> dict:
    with laspy.open(tile.path) as f:
        header = f.header
        las = f.read()

    classes, counts = np.unique(las.classification, return_counts=True)
    hist = {int(c): int(n) for c, n in zip(classes, counts)}
    total = int(header.point_count)
    has_rgb = "red" in las.point_format.dimension_names

    return {
        "file": tile.path.name,
        "easting_km": tile.easting_km,
        "northing_km": tile.northing_km,
        "point_count": total,
        "point_density_per_m2": round(total / 1_000_000.0, 2),
        "point_format": header.point_format.id,
        "las_version": str(header.version),
        "bounds": {
            "minx": float(header.mins[0]),
            "miny": float(header.mins[1]),
            "minz": float(header.mins[2]),
            "maxx": float(header.maxs[0]),
            "maxy": float(header.maxs[1]),
            "maxz": float(header.maxs[2]),
        },
        "has_rgb": has_rgb,
        "classification_histogram": hist,
        "classification_pct": {k: round(100 * v / total, 2) for k, v in hist.items()},
    }


def main():
    cfg = load_config()
    tiles = list_tiles(cfg)
    grid = build_grid(cfg, tiles)
    out_dir = output_dir(cfg)

    print(f"Found {len(tiles)} LAZ tiles")
    t0 = time.time()
    reports = []
    for i, tile in enumerate(tiles):
        r = inspect_tile(tile)
        reports.append(r)
        print(f"  [{i + 1}/{len(tiles)}] {tile.path.name}: {r['point_count']:,} pts, "
              f"density {r['point_density_per_m2']} pts/m2")

    total_points = sum(r["point_count"] for r in reports)
    combined_hist: dict[int, int] = {}
    for r in reports:
        for k, v in r["classification_histogram"].items():
            combined_hist[k] = combined_hist.get(k, 0) + v

    summary = {
        "generated_by": "pipeline/inspect_laz.py",
        "crs": cfg["crs"],
        "tile_count": len(tiles),
        "total_points": total_points,
        "mean_density_per_m2": round(total_points / (grid.width * grid.height), 2),
        "grid": {
            "left": grid.left,
            "bottom": grid.bottom,
            "right": grid.right,
            "top": grid.top,
            "resolution_m": grid.resolution,
            "width_px": grid.width,
            "height_px": grid.height,
        },
        "classification_histogram_all_tiles": combined_hist,
        "classification_pct_all_tiles": {
            k: round(100 * v / total_points, 3) for k, v in combined_hist.items()
        },
        "acquisition_year": cfg["laz"]["acquisition_year"],
        "tiles": reports,
    }

    save_json(out_dir / "laz_inventory.json", summary)

    coverage = grid.to_bounds_geojson_wgs84()
    save_json(out_dir / "laz_coverage.geojson", {"type": "FeatureCollection", "features": [coverage]})

    print(f"\nTotal points: {total_points:,}")
    print("Classification breakdown (all tiles):")
    class_names = {
        1: "unclassified", 2: "ground", 3: "veg_low", 4: "veg_medium", 5: "veg_high",
        6: "building", 7: "low_point_noise", 12: "overlap", 17: "bridge",
    }
    for k in sorted(combined_hist):
        name = class_names.get(k, f"class_{k}")
        pct = 100 * combined_hist[k] / total_points
        print(f"  {k:>3} {name:<16} {combined_hist[k]:>12,} ({pct:.2f}%)")

    print(f"\nWrote {out_dir / 'laz_inventory.json'}")
    print(f"Wrote {out_dir / 'laz_coverage.geojson'}")
    print(f"Elapsed: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()

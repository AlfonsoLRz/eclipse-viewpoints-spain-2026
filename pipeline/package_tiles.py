"""Package web deliverables: XYZ raster tiles + JSON metadata.

The plan proposes PMTiles, but neither GDAL's CLI nor the pmtiles/rio-cogeo
packages are available in this environment, so tiles are written as a plain
XYZ pyramid of PNGs. That is served correctly by any static host (the same
deployment story PMTiles was chosen for) and is read natively by MapLibre.

The orthophoto mosaic covers a much larger area than the LiDAR block (roughly
664.5-692.9 km E vs 670-677 km E), so `write_rgb_tiles` masks RGB to the LAZ
footprint. The deployed viewer no longer uses those tiles: they came to 671 MB,
86% of the published site, and the IGN WMTS serves the same PNOA mosaic live.
The viewer draws the footprint as a dashed outline instead of relying on the
clip to imply it. Pass --with-rgb to build the local tiles anyway, which is what
a fully offline copy needs.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image
from rasterio.warp import Resampling, calculate_default_transform, reproject

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    ROOT, build_grid, list_tiles, load_config, output_dir, read_raster,
    save_json,
)

TILE_SIZE = 256
WEB_MERCATOR = "EPSG:3857"
NODATA = -9999.0

# clearance classes -> RGBA. Blocked stays a dark translucent grey and the good
# classes are translucent greens, so the orthophoto underneath stays legible
# (plan section 7.2 explicitly rejects opaque red/green fills).
CLEARANCE_COLORS = {
    0: (40, 44, 52, 150),      # blocked
    1: (230, 160, 40, 130),    # fragile
    2: (180, 200, 60, 120),    # acceptable
    3: (90, 190, 90, 120),     # good
    4: (30, 150, 70, 120),     # excellent
}

# Alternative colour scales. MapLibre has no per-value raster recolouring
# (raster-color is a Mapbox GL property and is absent from MapLibre 5.x), so
# each scale is baked into its own tile set and the viewer swaps sources.
RAMPS = {
    "classes": CLEARANCE_COLORS,
    "viridis": {
        0: (68, 1, 84, 165),
        1: (59, 82, 139, 140),
        2: (33, 145, 140, 130),
        3: (94, 201, 98, 125),
        4: (253, 231, 37, 120),
    },
    "traffic": {
        0: (139, 26, 26, 160),
        1: (217, 95, 2, 140),
        2: (230, 194, 41, 130),
        3: (127, 188, 65, 125),
        4: (26, 122, 51, 120),
    },
    "binary": {
        0: (18, 18, 18, 170),
        1: (46, 125, 50, 120),
        2: (46, 125, 50, 120),
        3: (46, 125, 50, 120),
        4: (46, 125, 50, 120),
    },
    # Shadow only: paint what is blocked, leave sunlit ground untouched so the
    # orthophoto reads exactly as it would on a clear evening.
    "mono": {
        0: (0, 0, 0, 175),
        1: (0, 0, 0, 0),
        2: (0, 0, 0, 0),
        3: (0, 0, 0, 0),
        4: (0, 0, 0, 0),
    },
}


def lonlat_to_tile(lon, lat, z):
    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n
    lat_r = math.radians(lat)
    y = (1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n
    return x, y


def tile_bounds_3857(x, y, z):
    """Web-mercator bounds of tile (x, y, z)."""
    origin = 20037508.342789244
    size = 2 * origin / (2 ** z)
    left = -origin + x * size
    top = origin - y * size
    return left, top - size, left + size, top


def reproject_to_3857(src_path: Path, band_indexes, resampling):
    """Load a source raster reprojected into web mercator."""
    with rasterio.open(src_path) as src:
        transform, width, height = calculate_default_transform(
            src.crs, WEB_MERCATOR, src.width, src.height, *src.bounds
        )
        dst = np.zeros((len(band_indexes), height, width), dtype=src.dtypes[0])
        for i, b in enumerate(band_indexes):
            reproject(
                source=rasterio.band(src, b),
                destination=dst[i],
                src_transform=src.transform,
                src_crs=src.crs,
                dst_transform=transform,
                dst_crs=WEB_MERCATOR,
                resampling=resampling,
            )
    return dst, transform


def sample_window(data, transform, left, bottom, right, top, size=TILE_SIZE):
    """Nearest-neighbour sample of `data` over a web-mercator bbox."""
    xs = np.linspace(left, right, size, endpoint=False) + (right - left) / (2 * size)
    ys = np.linspace(top, bottom, size, endpoint=False) - (top - bottom) / (2 * size)
    inv = ~transform
    cols = np.empty((size, size), dtype=np.int64)
    rows = np.empty((size, size), dtype=np.int64)
    gx, gy = np.meshgrid(xs, ys)
    c, r = inv * (gx, gy)
    np.floor(c, out=c)
    np.floor(r, out=r)
    cols[:] = c
    rows[:] = r
    h, w = data.shape[-2:]
    valid = (cols >= 0) & (cols < w) & (rows >= 0) & (rows < h)
    cols = np.clip(cols, 0, w - 1)
    rows = np.clip(rows, 0, h - 1)
    return rows, cols, valid


def write_rgb_tiles(cfg, grid, out_root: Path, zooms):
    """Orthophoto tiles, masked to the LiDAR footprint."""
    ortho_files = [ROOT / f for f in cfg["orthophoto"]["files"]]
    # Only tiles that actually intersect the LAZ block are worth opening.
    useful = []
    for f in ortho_files:
        with rasterio.open(f) as s:
            b = s.bounds
            if b.right > grid.left and b.left < grid.right and b.top > grid.bottom and b.bottom < grid.top:
                useful.append(f)
    print(f"  {len(useful)} of {len(ortho_files)} orthophoto tiles intersect the LiDAR block")

    srcs = [rasterio.open(f) for f in useful]
    try:
        for z in zooms:
            count = write_rgb_zoom(srcs, grid, out_root, z)
            print(f"    zoom {z}: {count} tiles")
    finally:
        for s in srcs:
            s.close()


def _laz_bounds_3857(grid):
    import pyproj

    tr = pyproj.Transformer.from_crs(grid.crs, WEB_MERCATOR, always_xy=True)
    xs, ys = [], []
    for x, y in ((grid.left, grid.bottom), (grid.left, grid.top),
                 (grid.right, grid.bottom), (grid.right, grid.top)):
        a, b = tr.transform(x, y)
        xs.append(a)
        ys.append(b)
    return min(xs), min(ys), max(xs), max(ys)


def write_rgb_zoom(srcs, grid, out_root: Path, z):
    import pyproj

    to_utm = pyproj.Transformer.from_crs(WEB_MERCATOR, grid.crs, always_xy=True)
    minx, miny, maxx, maxy = _laz_bounds_3857(grid)
    origin = 20037508.342789244
    n = 2 ** z
    size = 2 * origin / n

    x0 = max(0, int((minx + origin) / size))
    x1 = min(n - 1, int((maxx + origin) / size))
    y0 = max(0, int((origin - maxy) / size))
    y1 = min(n - 1, int((origin - miny) / size))

    written = 0
    for tx in range(x0, x1 + 1):
        for ty in range(y0, y1 + 1):
            left, bottom, right, top = tile_bounds_3857(tx, ty, z)
            xs = np.linspace(left, right, TILE_SIZE, endpoint=False) + size / (2 * TILE_SIZE)
            ys = np.linspace(top, bottom, TILE_SIZE, endpoint=False) - size / (2 * TILE_SIZE)
            gx, gy = np.meshgrid(xs, ys)
            ux, uy = to_utm.transform(gx, gy)

            # Hard clip to the LiDAR footprint - this is the whole point.
            inside = (ux >= grid.left) & (ux < grid.right) & (uy > grid.bottom) & (uy <= grid.top)
            if not inside.any():
                continue

            rgba = np.zeros((TILE_SIZE, TILE_SIZE, 4), dtype=np.uint8)
            filled = np.zeros((TILE_SIZE, TILE_SIZE), dtype=bool)

            for src in srcs:
                b = src.bounds
                hit = inside & ~filled & (ux >= b.left) & (ux < b.right) & (uy > b.bottom) & (uy <= b.top)
                if not hit.any():
                    continue
                inv = ~src.transform
                c, r = inv * (ux[hit], uy[hit])
                c = np.clip(np.floor(c).astype(np.int64), 0, src.width - 1)
                r = np.clip(np.floor(r).astype(np.int64), 0, src.height - 1)

                rmin, rmax = r.min(), r.max() + 1
                cmin, cmax = c.min(), c.max() + 1
                block = src.read(
                    indexes=[1, 2, 3],
                    window=rasterio.windows.Window(cmin, rmin, cmax - cmin, rmax - rmin),
                )
                vals = block[:, r - rmin, c - cmin]
                rgba[hit, 0] = vals[0]
                rgba[hit, 1] = vals[1]
                rgba[hit, 2] = vals[2]
                rgba[hit, 3] = 255
                filled |= hit

            if not filled.any():
                continue
            d = out_root / str(z) / str(tx)
            d.mkdir(parents=True, exist_ok=True)
            Image.fromarray(rgba, "RGBA").save(d / f"{ty}.png", optimize=True)
            written += 1
    return written


def write_class_index_tiles(grid, clearance, out_root: Path, zooms, walkable=None):
    """Write clearance CLASS INDICES rather than final colours.

    The class (0-4) goes into the red channel and alpha marks evaluated cells,
    so the viewer can apply any colour ramp on the GPU without re-tiling. Green
    and blue are left at 0; MapLibre's raster-color expression reads the red
    channel only.
    """
    import pyproj

    to_utm = pyproj.Transformer.from_crs(WEB_MERCATOR, grid.crs, always_xy=True)
    minx, miny, maxx, maxy = _laz_bounds_3857(grid)
    origin = 20037508.342789244

    total = 0
    for z in zooms:
        n = 2 ** z
        size = 2 * origin / n
        x0 = max(0, int((minx + origin) / size))
        x1 = min(n - 1, int((maxx + origin) / size))
        y0 = max(0, int((origin - maxy) / size))
        y1 = min(n - 1, int((origin - miny) / size))
        written = 0
        for tx in range(x0, x1 + 1):
            for ty in range(y0, y1 + 1):
                left, bottom, right, top = tile_bounds_3857(tx, ty, z)
                xs = np.linspace(left, right, TILE_SIZE, endpoint=False) + size / (2 * TILE_SIZE)
                ys = np.linspace(top, bottom, TILE_SIZE, endpoint=False) - size / (2 * TILE_SIZE)
                gx, gy = np.meshgrid(xs, ys)
                ux, uy = to_utm.transform(gx, gy)

                col = np.floor((ux - grid.left) / grid.resolution).astype(np.int64)
                row = np.floor((grid.top - uy) / grid.resolution).astype(np.int64)
                inside = (col >= 0) & (col < grid.width) & (row >= 0) & (row < grid.height)
                if not inside.any():
                    continue
                col = np.clip(col, 0, grid.width - 1)
                row = np.clip(row, 0, grid.height - 1)
                vals = clearance[row, col]

                drawable = inside & (vals >= 0)
                if walkable is not None:
                    drawable &= walkable[row, col] > 0
                if not drawable.any():
                    continue

                rgba = np.zeros((TILE_SIZE, TILE_SIZE, 4), dtype=np.uint8)
                # Spread 0-4 across the 8-bit range so texture filtering and
                # PNG quantisation cannot shift a class into its neighbour.
                rgba[..., 0] = np.where(drawable, np.clip(vals, 0, 4) * 60, 0)
                rgba[..., 3] = np.where(drawable, 255, 0)

                d = out_root / str(z) / str(tx)
                d.mkdir(parents=True, exist_ok=True)
                Image.fromarray(rgba, "RGBA").save(d / f"{ty}.png", optimize=True)
                written += 1
        print(f"    zoom {z}: {written} tiles")
        total += written
    return total


def write_clearance_tiles(grid, clearance, out_root: Path, zooms, walkable=None, colors=None,
                          mask_to_walkable=True, buildings=None):
    """Clearance-class overlay tiles (already on the LiDAR grid, so no extra clip).

    Where a walkable mask is supplied and `mask_to_walkable` is set, non-walkable
    cells are left fully transparent. Rooftops are sunlit and would otherwise be
    painted green even though the plan excludes them as observing positions,
    which reads as "you can watch the eclipse from here" for every roof in the
    city. The shadow-only ramp overrides this: there the point is to show the
    physical shadow everywhere, roofs included.
    """
    import pyproj

    to_utm = pyproj.Transformer.from_crs(WEB_MERCATOR, grid.crs, always_xy=True)
    minx, miny, maxx, maxy = _laz_bounds_3857(grid)
    origin = 20037508.342789244

    lut = np.zeros((6, 4), dtype=np.uint8)
    for k, v in (colors or CLEARANCE_COLORS).items():
        lut[k] = v

    total = 0
    for z in zooms:
        n = 2 ** z
        size = 2 * origin / n
        x0 = max(0, int((minx + origin) / size))
        x1 = min(n - 1, int((maxx + origin) / size))
        y0 = max(0, int((origin - maxy) / size))
        y1 = min(n - 1, int((origin - miny) / size))
        written = 0
        for tx in range(x0, x1 + 1):
            for ty in range(y0, y1 + 1):
                left, bottom, right, top = tile_bounds_3857(tx, ty, z)
                xs = np.linspace(left, right, TILE_SIZE, endpoint=False) + size / (2 * TILE_SIZE)
                ys = np.linspace(top, bottom, TILE_SIZE, endpoint=False) - size / (2 * TILE_SIZE)
                gx, gy = np.meshgrid(xs, ys)
                ux, uy = to_utm.transform(gx, gy)

                col = np.floor((ux - grid.left) / grid.resolution).astype(np.int64)
                row = np.floor((grid.top - uy) / grid.resolution).astype(np.int64)
                inside = (col >= 0) & (col < grid.width) & (row >= 0) & (row < grid.height)
                if not inside.any():
                    continue
                col = np.clip(col, 0, grid.width - 1)
                row = np.clip(row, 0, grid.height - 1)
                vals = clearance[row, col]

                rgba = np.zeros((TILE_SIZE, TILE_SIZE, 4), dtype=np.uint8)
                drawable = inside & (vals >= 0)
                # Rooftops are sunlit but are not standing positions, so they
                # stay unpainted unless this is the physical shadow view.
                if buildings is not None and mask_to_walkable:
                    drawable &= buildings[row, col] == 0
                idx = np.clip(vals, 0, 4)
                rgba[drawable] = lut[idx[drawable]]

                # Everywhere evaluated is painted, so the shadow model is
                # visible across the whole block rather than only on the few
                # per cent of ground that is walkable. Non-walkable ground is
                # drawn at reduced opacity so the places actually worth
                # standing in still stand out. Restricting the paint to
                # walkable cells (the previous behaviour) made the map look
                # broken - a handful of coloured patches in an empty city.
                if walkable is not None and mask_to_walkable:
                    faded = drawable & (walkable[row, col] == 0)
                    rgba[..., 3] = np.where(faded, (rgba[..., 3] * 0.45).astype(np.uint8),
                                            rgba[..., 3])
                # A fully transparent tile is still written. Skipping it makes
                # the tile server answer with a 404 (or, behind an SPA rewrite,
                # index.html - which the browser then fails to decode as a PNG).
                # Transparent PNGs compress to a few hundred bytes.
                d = out_root / str(z) / str(tx)
                d.mkdir(parents=True, exist_ok=True)
                Image.fromarray(rgba, "RGBA").save(d / f"{ty}.png", optimize=True)
                written += 1
        print(f"    zoom {z}: {written} tiles")
        total += written
    return total


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--with-rgb", action="store_true",
                    help="also build local orthophoto tiles (671 MB); the "
                         "deployed viewer uses the IGN WMTS instead")
    args = ap.parse_args()

    t0 = time.time()
    cfg = load_config()
    tiles = list_tiles(cfg)
    grid = build_grid(cfg, tiles)
    out_dir = output_dir(cfg)
    web_dir = ROOT / cfg["paths"]["viewer_public_data"]
    web_dir.mkdir(parents=True, exist_ok=True)

    # Zoom 18 gives ~0.45 m/px at this latitude, so the 1 m shadow model is
    # displayed at close to its native resolution instead of being smeared.
    zooms = [13, 14, 15, 16, 17, 18]

    if args.with_rgb:
        print("Writing orthophoto tiles (clipped to LiDAR footprint)...")
        write_rgb_tiles(cfg, grid, web_dir / "tiles" / "rgb", zooms)
    else:
        print("Skipping orthophoto tiles; viewer reads the IGN WMTS "
              "(pass --with-rgb for an offline copy)")

    clearance, _ = read_raster(out_dir / "clearance_class.tif")
    walkable = None
    walk_path = out_dir / "walkable_mask.tif"
    if walk_path.exists():
        walkable, _ = read_raster(walk_path)
    else:
        print("  WARNING: walkable_mask.tif missing; overlay will include rooftops")

    buildings = None
    bld_path = out_dir / "building_mask.tif"
    if bld_path.exists():
        buildings, _ = read_raster(bld_path)

    for name, colors in RAMPS.items():
        # The shadow-only ramp is a physical picture of the shadow, so it covers
        # rooftops too and gets no walkable emphasis.
        mask = name != "mono"
        print(f"Writing '{name}' overlay tiles...")
        write_clearance_tiles(grid, clearance, web_dir / "tiles" / name, zooms,
                              walkable, colors, mask_to_walkable=mask,
                              buildings=buildings)

    # Copy the JSON products the web app consumes.
    for name in ("candidates.json", "eclipse_metadata.json", "laz_coverage.geojson",
                 "osm_labels.json"):
        src = out_dir / name
        if src.exists():
            (web_dir / name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

    import pyproj

    tr = pyproj.Transformer.from_crs(grid.crs, "EPSG:4326", always_xy=True)
    w, s = tr.transform(grid.left, grid.bottom)
    e, n = tr.transform(grid.right, grid.top)

    save_json(web_dir / "layers.json", {
        "generated_by": "pipeline/package_tiles.py",
        "tile_size": TILE_SIZE,
        "minzoom": min(zooms),
        "maxzoom": max(zooms),
        "bounds_wgs84": [round(w, 6), round(s, 6), round(e, 6), round(n, 6)],
        "center": [round((w + e) / 2, 6), round((s + n) / 2, 6)],
        "lidar_year": cfg["laz"]["acquisition_year"],
        "orthophoto_date": cfg["orthophoto"]["mosaic_date"],
        "clearance_colors": {str(k): list(v) for k, v in CLEARANCE_COLORS.items()},
        "ramps": {name: {str(k): list(v) for k, v in colors.items()}
                  for name, colors in RAMPS.items()},
        "coverage_note": (
            "Visibility was computed only inside the dashed outline — the "
            "7x7 km LiDAR block. The orthophoto is served live by the IGN and "
            "continues past that edge, but nothing outside it has been analysed."
        ),
    })
    print(f"Wrote {web_dir / 'layers.json'}")
    print(f"Elapsed: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()

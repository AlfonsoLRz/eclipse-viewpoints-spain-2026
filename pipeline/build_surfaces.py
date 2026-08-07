"""Phase 2 - build DTM, building DSM, vegetation DSM and combined DSM
(nominal + conservative) from the PNOA LAZ tiles, plus point-density and
confidence rasters.

Binning strategy: each tile is read once; points are scattered into the
*global* 1 m grid (not a per-tile local grid) using sort + reduceat, which
is fast and avoids large temporary arrays. Gaps are then closed/filled with
class-dependent rules (see plan section 3.5) rather than naive interpolation.

Usage:
    .venv/Scripts/python.exe pipeline/build_surfaces.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import laspy
import numpy as np
from scipy import ndimage

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import build_grid, list_tiles, load_config, output_dir, write_raster  # noqa: E402

NODATA = -9999.0


def scatter_max(flat_idx: np.ndarray, values: np.ndarray, out: np.ndarray) -> None:
    if flat_idx.size == 0:
        return
    order = np.argsort(flat_idx, kind="stable")
    idx_sorted = flat_idx[order]
    val_sorted = values[order]
    uniq_idx, start = np.unique(idx_sorted, return_index=True)
    maxvals = np.maximum.reduceat(val_sorted, start)
    flat_out = out.reshape(-1)
    # np.maximum() propagates NaN, but `out` starts as all-NaN (no feature yet),
    # so NaN must be treated as "no value yet" here - use np.fmax instead.
    flat_out[uniq_idx] = np.fmax(flat_out[uniq_idx], maxvals)


def scatter_sum_count(flat_idx: np.ndarray, values: np.ndarray, out_sum: np.ndarray, out_count: np.ndarray) -> None:
    if flat_idx.size == 0:
        return
    order = np.argsort(flat_idx, kind="stable")
    idx_sorted = flat_idx[order]
    val_sorted = values[order]
    uniq_idx, start = np.unique(idx_sorted, return_index=True)
    sums = np.add.reduceat(val_sorted, start)
    counts = np.diff(np.append(start, idx_sorted.size))
    flat_sum = out_sum.reshape(-1)
    flat_count = out_count.reshape(-1)
    flat_sum[uniq_idx] += sums
    flat_count[uniq_idx] += counts


def close_gaps(max_layer: np.ndarray, size: int) -> np.ndarray:
    """Grey-closing to fill small pits/gaps in a max-elevation layer that
    contains NaN where no point of that class exists."""
    filled = np.where(np.isnan(max_layer), NODATA, max_layer)
    closed = ndimage.grey_closing(filled, size=size)
    closed = np.where(closed <= NODATA, np.nan, closed)
    return closed


def dilate_and_raise(layer: np.ndarray, dilation_cells: int, height_bonus: float) -> np.ndarray:
    if dilation_cells <= 0 and height_bonus == 0:
        return layer
    filled = np.where(np.isnan(layer), NODATA, layer)
    if dilation_cells > 0:
        size = 2 * dilation_cells + 1
        filled = ndimage.grey_dilation(filled, size=size)
    out = np.where(filled <= NODATA, np.nan, filled + height_bonus)
    return out


def fill_dtm_gaps(dtm: np.ndarray, max_fill_px: int) -> tuple[np.ndarray, np.ndarray]:
    """Nearest-neighbour fill limited to `max_fill_px`. Returns (filled, filled_mask)
    where filled_mask marks cells that were interpolated (lower confidence)."""
    valid = ~np.isnan(dtm)
    if valid.all():
        return dtm, np.zeros_like(dtm, dtype=bool)
    dist, (iy, ix) = ndimage.distance_transform_edt(~valid, return_indices=True)
    nearest = dtm[iy, ix]
    fillable = (~valid) & (dist <= max_fill_px)
    out = dtm.copy()
    out[fillable] = nearest[fillable]
    return out, fillable


def main():
    cfg = load_config()
    tiles = list_tiles(cfg)
    grid = build_grid(cfg, tiles)
    out_dir = output_dir(cfg)
    H, W = grid.height, grid.width
    print(f"Global grid: {W} x {H} px ({grid.resolution} m) -> {W * H:,} cells")

    ground_sum = np.zeros((H, W), dtype=np.float64)
    ground_count = np.zeros((H, W), dtype=np.int32)
    building_max = np.full((H, W), np.nan, dtype=np.float32)
    veg_max = np.full((H, W), np.nan, dtype=np.float32)
    density_count = np.zeros((H, W), dtype=np.int32)

    ground_classes = set(cfg["laz"]["classes"]["ground"])
    veg_classes = set(
        cfg["laz"]["classes"]["vegetation_low"]
        + cfg["laz"]["classes"]["vegetation_medium"]
        + cfg["laz"]["classes"]["vegetation_high"]
    )
    building_classes = set(cfg["laz"]["classes"]["building"] + cfg["laz"]["classes"]["bridge"])

    t0 = time.time()
    for i, tile in enumerate(tiles):
        with laspy.open(tile.path) as f:
            las = f.read()
        x = np.asarray(las.x, dtype=np.float64)
        y = np.asarray(las.y, dtype=np.float64)
        z = np.asarray(las.z, dtype=np.float64)
        cls = np.asarray(las.classification)

        row, col = grid.to_pixel(x, y)
        valid = (row >= 0) & (row < H) & (col >= 0) & (col < W)
        row, col, z, cls = row[valid], col[valid], z[valid], cls[valid]
        flat_idx = row * W + col

        # point density: all valid points
        order = np.argsort(flat_idx, kind="stable")
        idx_sorted = flat_idx[order]
        uniq_idx, start = np.unique(idx_sorted, return_index=True)
        counts = np.diff(np.append(start, idx_sorted.size))
        density_count.reshape(-1)[uniq_idx] += counts

        g = np.isin(cls, list(ground_classes))
        if g.any():
            scatter_sum_count(flat_idx[g], z[g], ground_sum, ground_count)

        b = np.isin(cls, list(building_classes))
        if b.any():
            scatter_max(flat_idx[b], z[b].astype(np.float32), building_max)

        v = np.isin(cls, list(veg_classes))
        if v.any():
            scatter_max(flat_idx[v], z[v].astype(np.float32), veg_max)

        print(f"  [{i + 1}/{len(tiles)}] {tile.path.name}: binned {len(z):,} pts "
              f"({time.time() - t0:.0f}s elapsed)")

    print("Building DTM...")
    with np.errstate(invalid="ignore", divide="ignore"):
        dtm_raw = np.where(ground_count > 0, ground_sum / np.maximum(ground_count, 1), np.nan).astype(np.float32)

    max_fill_px = int(round(5.0 / grid.resolution))  # only trust interpolation up to 5 m
    dtm_filled, dtm_interp_mask = fill_dtm_gaps(dtm_raw, max_fill_px)

    print("Closing small building/vegetation gaps...")
    building_nominal = close_gaps(building_max, size=3)
    veg_nominal = close_gaps(veg_max, size=3)

    cons_cfg = cfg["visibility"]["conservative_model"]
    print("Applying conservative dilation/height bonus...")
    building_conservative = dilate_and_raise(building_nominal, cons_cfg["building_dilation_cells"], 0.0)
    veg_conservative = dilate_and_raise(
        veg_nominal, cons_cfg["vegetation_dilation_cells"], cons_cfg["vegetation_height_bonus_m"]
    )

    dsm_nominal = np.fmax(np.fmax(dtm_filled, building_nominal), veg_nominal)
    dsm_conservative = np.fmax(np.fmax(dtm_filled, building_conservative), veg_conservative)

    no_data_mask = np.isnan(dtm_filled)  # areas with no ground info at all -> uncertain
    building_mask = ~np.isnan(building_nominal)
    vegetation_mask = ~np.isnan(veg_nominal)
    building_height = np.where(building_mask, building_nominal - dtm_filled, np.nan).astype(np.float32)
    canopy_height = np.where(vegetation_mask, veg_nominal - dtm_filled, np.nan).astype(np.float32)

    # Confidence: 0 = no data, 1 = low (sparse points or interpolated), 2 = good
    confidence = np.full((H, W), 2, dtype=np.uint8)
    confidence[no_data_mask] = 0
    confidence[(~no_data_mask) & (density_count < 2)] = 1
    confidence[(~no_data_mask) & dtm_interp_mask] = 1

    print("Writing rasters...")
    write_raster(out_dir / "dtm.tif", np.where(no_data_mask, NODATA, dtm_filled), grid, dtype="float32", nodata=NODATA)
    write_raster(out_dir / "dsm_buildings.tif", np.where(building_mask, building_nominal, NODATA), grid, dtype="float32", nodata=NODATA)
    write_raster(out_dir / "dsm_vegetation.tif", np.where(vegetation_mask, veg_nominal, NODATA), grid, dtype="float32", nodata=NODATA)
    write_raster(out_dir / "dsm_all_nominal.tif", np.where(no_data_mask, NODATA, dsm_nominal), grid, dtype="float32", nodata=NODATA)
    write_raster(out_dir / "dsm_all_conservative.tif", np.where(no_data_mask, NODATA, dsm_conservative), grid, dtype="float32", nodata=NODATA)
    write_raster(out_dir / "point_density.tif", density_count, grid, dtype="int32", nodata=0)
    write_raster(out_dir / "classification_confidence.tif", confidence, grid, dtype="uint8", nodata=255)
    write_raster(out_dir / "building_mask.tif", building_mask.astype(np.uint8), grid, dtype="uint8", nodata=255)
    write_raster(out_dir / "vegetation_mask.tif", vegetation_mask.astype(np.uint8), grid, dtype="uint8", nodata=255)
    write_raster(out_dir / "building_height.tif", np.where(building_mask, building_height, NODATA), grid, dtype="float32", nodata=NODATA)
    write_raster(out_dir / "canopy_height.tif", np.where(vegetation_mask, canopy_height, NODATA), grid, dtype="float32", nodata=NODATA)

    print(f"No-data (no ground info) cells: {no_data_mask.sum():,} / {H * W:,} "
          f"({100 * no_data_mask.sum() / (H * W):.2f}%)")
    print(f"Building coverage: {100 * building_mask.sum() / (H * W):.2f}%")
    print(f"Vegetation coverage: {100 * vegetation_mask.sum() / (H * W):.2f}%")
    print(f"Elapsed: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()

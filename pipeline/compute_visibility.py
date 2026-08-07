"""Phase 3 - directional solar visibility over the combined DSM.

Implements the efficient rotate-and-scan approach from plan section 4.5/4.6:
  1. Rotate the DSM so the sun azimuth aligns with the column axis.
  2. For each row, scan columns from the sun-facing edge onward, tracking a
     running max of the "tilted height" q(u) = z(u) - u * tan(effective_elev).
  3. A cell is visible at a given effective elevation iff its own tilted eye
     height is >= the running max of every cell between it and the sun.
  4. Repeat at a few effective elevations (actual, -0.5, -1, -2 degrees) to
     approximate angular clearance without exact per-pixel ray tracing.

Also determines the limiting obstacle type (building vs vegetation vs
terrain) for blocked/fragile cells by repeating the actual-elevation test
against building-only and vegetation-only surfaces.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
from scipy import ndimage

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import build_grid, list_tiles, load_config, output_dir, read_raster, write_raster  # noqa: E402

NODATA = -9999.0
EYE_HEIGHT = None  # set from config in main()

# Limiting-obstacle codes
OBSTACLE_NONE = 0
OBSTACLE_TERRAIN = 1
OBSTACLE_BUILDING = 2
OBSTACLE_VEGETATION = 3
OBSTACLE_NODATA = 255


def _sun_aligned_transform(shape: tuple[int, int], azimuth_deg: float):
    """Build an explicit affine transform that resamples a (row, col) array
    onto a (v, u) array where u increases in the direction AWAY from the sun
    (u=0 is the sun-facing edge of the bounding box) and v is perpendicular.

    Array/pixel convention: col increases east (+x), row increases south
    (i.e. -y in geographic terms). Compass azimuth: 0=N, 90=E, clockwise.

    Returns (matrix, offset, output_shape, inverse_matrix, inverse_offset)
    such that, for scipy.ndimage.affine_transform:
        input_coord = matrix @ [v_idx, u_idx] + offset          (forward)
        orig_coord  = inverse_matrix @ [row_idx, col_idx] + inverse_offset  (unused directly;
                                                                               see backward())
    """
    H, W = shape
    phi = np.radians(azimuth_deg)
    # Compass azimuth: 0=N, 90=E, measured clockwise. Unit vector pointing
    # TOWARD the sun, in geographic axes (east, north):
    east_to_sun, north_to_sun = np.sin(phi), np.cos(phi)

    # Direction AWAY from the sun is the exact negation of both components.
    east_away, north_away = -east_to_sun, -north_to_sun

    # Convert geographic (east, north) to pixel (col, row). col grows east, so
    # its component is +east; row grows SOUTH, so its component is -north.
    # Negating only one of the two components (rather than both) turns this
    # rotation into a reflection and sends shadows off in the wrong direction.
    # Unit vector along +u (away from the sun) expressed in PIXEL axes as
    # (row component, col component) - keeping a single consistent ordering
    # throughout avoids the (col,row) vs (row,col) mix-up that silently turns
    # this transform into the identity for axis-aligned azimuths.
    u_row, u_col = -north_away, east_away
    n = np.hypot(u_row, u_col)
    u_row, u_col = u_row / n, u_col / n

    # +v is +u rotated 90 degrees within the pixel plane.
    v_row, v_col = -u_col, u_row

    corners_rc = np.array([[0, 0], [0, W - 1], [H - 1, 0], [H - 1, W - 1]], dtype=float)  # (row, col)
    proj_u = corners_rc[:, 0] * u_row + corners_rc[:, 1] * u_col
    proj_v = corners_rc[:, 0] * v_row + corners_rc[:, 1] * v_col
    u_min, u_max = proj_u.min(), proj_u.max()
    v_min, v_max = proj_v.min(), proj_v.max()

    Nu = int(np.ceil(u_max - u_min)) + 1
    Nv = int(np.ceil(v_max - v_min)) + 1

    # scipy.ndimage.affine_transform maps an OUTPUT index to an INPUT index:
    #     [row, col] = matrix @ [v_idx, u_idx] + offset
    # so column 0 of `matrix` holds the v basis vector and column 1 the u basis
    # vector, each written as (row, col).
    matrix = np.array([[v_row, u_row],
                       [v_col, u_col]])
    offset = np.array([v_min * v_row + u_min * u_row,
                       v_min * v_col + u_min * u_col])

    inv_matrix = np.linalg.inv(matrix)
    inv_offset = -inv_matrix @ offset

    return matrix, offset, (Nv, Nu), inv_matrix, inv_offset


def rotate_for_azimuth(array: np.ndarray, azimuth_deg: float, fill: float):
    """Resample `array` onto sun-aligned (v, u) axes; u=0 is the sun-facing
    edge and u increases away from the sun. Returns (resampled, transform)."""
    transform = _sun_aligned_transform(array.shape, azimuth_deg)
    matrix, offset, out_shape, inv_matrix, inv_offset = transform
    src = np.where(np.isnan(array), fill, array)
    out = ndimage.affine_transform(
        src, matrix=matrix, offset=offset, output_shape=out_shape,
        order=1, mode="constant", cval=fill, prefilter=False,
    )
    return out, transform


def unrotate(array: np.ndarray, transform, out_shape: tuple[int, int]) -> np.ndarray:
    """Resample a (v, u) array back onto the original (row, col) grid."""
    _, _, _, inv_matrix, inv_offset = transform
    back = ndimage.affine_transform(
        array, matrix=inv_matrix, offset=inv_offset, output_shape=out_shape,
        order=1, mode="constant", cval=np.nan, prefilter=False,
    )
    return back



def visible_mask_rotated(z_rot: np.ndarray, valid_rot: np.ndarray, res: float, tan_alpha: float, eye_height: float) -> np.ndarray:
    """z_rot: rotated elevation surface (col 0 = sun-facing edge).
    Returns boolean 'visible' array in rotated space.

    NaN handling is critical here: np.maximum.accumulate propagates NaN to
    every subsequent element of a row, so a single invalid cell would mark the
    whole remainder of that scan line as blocked. The sun-aligned bounding box
    is padded with invalid cells at both ends of nearly every row, so any NaN
    leaking into the accumulation poisons essentially the entire raster.
    Invalid cells must therefore become -inf (an obstacle that blocks nothing)
    BEFORE the accumulation, and the observer test must be evaluated only where
    the observer cell itself is valid.
    """
    u = np.arange(z_rot.shape[1], dtype=np.float64) * res
    finite = valid_rot & np.isfinite(z_rot)
    z_safe = np.where(finite, z_rot, 0.0)  # value is irrelevant where masked out below

    # u = 0 is the Sun-facing edge and u increases AWAY from the Sun, so an
    # obstacle that can occlude an observer at u_p sits at u_i < u_p, and the
    # observer's line of sight toward the Sun RISES by tan(alpha) per metre as
    # it travels back toward the Sun. Obstacle i blocks observer p when
    #     z_i > z_p + eye + (u_p - u_i) * tan(alpha)
    # Rearranging into a form separable in i and p:
    #     z_i + u_i * tan(alpha) > (z_p + eye) + u_p * tan(alpha)
    # so the tilt is ADDED, not subtracted. Subtracting it (the natural-looking
    # "shadow marches away from the sun" form) makes flat ground occlude itself
    # beyond eye/tan(alpha) metres and marks almost everything blocked.
    q_terrain = np.where(finite, z_safe + u[np.newaxis, :] * tan_alpha, -np.inf)

    # shift right by one so a cell is compared only against obstacles strictly Sunward of it
    running_max_before = np.full_like(q_terrain, -np.inf)
    running_max_before[:, 1:] = np.maximum.accumulate(q_terrain, axis=1)[:, :-1]

    q_eye = (z_safe + eye_height) + u[np.newaxis, :] * tan_alpha
    visible = finite & (q_eye >= running_max_before)
    return visible


def main():
    cfg = load_config()
    tiles = list_tiles(cfg)
    grid = build_grid(cfg, tiles)
    out_dir = output_dir(cfg)
    H, W = grid.height, grid.width
    eye_height = float(cfg["observer"]["eye_height_m"])

    print("Loading combined conservative DSM and component surfaces...")
    dsm, _ = read_raster(out_dir / "dsm_all_conservative.tif")
    dsm = np.where(dsm == NODATA, np.nan, dsm)
    dtm, _ = read_raster(out_dir / "dtm.tif")
    dtm = np.where(dtm == NODATA, np.nan, dtm)
    building, _ = read_raster(out_dir / "dsm_buildings.tif")
    building = np.where(building == NODATA, np.nan, building)
    vegetation, _ = read_raster(out_dir / "dsm_vegetation.tif")
    vegetation = np.where(vegetation == NODATA, np.nan, vegetation)

    no_data_mask = np.isnan(dtm)
    valid = ~no_data_mask

    azimuth = float(cfg["eclipse"].get("_azimuth_deg", 284.526))
    # Prefer the value computed by compute_sun_vectors.py if present.
    import json
    meta_path = out_dir / "eclipse_metadata.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        azimuth = meta["solar_azimuth_deg"]
        elevation = meta["solar_elevation_deg"]
    else:
        elevation = float(cfg["eclipse"].get("_elevation_deg", 6.091))

    print(f"Sun azimuth={azimuth:.3f} deg, elevation={elevation:.3f} deg")

    t0 = time.time()
    print("Resampling combined DSM onto sun-aligned axes...")
    dsm_rot, transform = rotate_for_azimuth(dsm, azimuth, fill=-9999.0)
    valid_rot, _ = rotate_for_azimuth(valid.astype(np.float32), azimuth, fill=0.0)
    valid_rot = valid_rot > 0.5
    dsm_rot = np.where(dsm_rot <= -9998.0, np.nan, dsm_rot)
    print(f"  resampled shape: {dsm_rot.shape} ({time.time() - t0:.1f}s)")

    # Surfaces for isolating the limiting obstacle: ground + one obstacle class only.
    # Where the obstacle class is absent the surface falls back to bare ground; where
    # the ground itself is unknown the cell stays invalid rather than becoming NaN
    # inside the scan (see visible_mask_rotated).
    veg_only = np.where(np.isnan(vegetation), dtm, vegetation)
    bld_only = np.where(np.isnan(building), dtm, building)
    veg_only_rot, _ = rotate_for_azimuth(veg_only, azimuth, fill=-9999.0)
    veg_only_rot = np.where(veg_only_rot <= -9998.0, np.nan, veg_only_rot)
    bld_only_rot, _ = rotate_for_azimuth(bld_only, azimuth, fill=-9999.0)
    bld_only_rot = np.where(bld_only_rot <= -9998.0, np.nan, bld_only_rot)

    offsets = cfg["visibility"]["effective_elevation_offsets_deg"]
    res = grid.resolution

    print("Scanning visibility at effective elevations:", offsets)
    visible_layers = {}
    for off in offsets:
        eff_elev = max(elevation - off, 0.01)
        tan_a = np.tan(np.radians(eff_elev))
        vis_rot = visible_mask_rotated(dsm_rot, valid_rot, res, tan_a, eye_height)
        vis = unrotate(vis_rot.astype(np.float32), transform, (H, W))
        visible_layers[off] = vis >= 0.5
        print(f"  offset {off}: visible fraction (of valid cells) = "
              f"{visible_layers[off][valid].mean():.3f} ({time.time() - t0:.1f}s)")

    # Clearance class: highest offset that remains visible.
    clearance_class = np.zeros((H, W), dtype=np.int8) - 1  # -1 = blocked at actual elevation
    class_rank = sorted(offsets)  # [0.0, 0.5, 1.0, 2.0] -> class ids 0..3 map to fragile..excellent boundaries
    # class ids: 0 blocked, 1 fragile, 2 acceptable, 3 good, 4 excellent
    clearance_class[:] = 0  # blocked by default
    clearance_class[visible_layers[0.0]] = 1       # visible at actual elevation -> at least "fragile"
    if 0.5 in visible_layers:
        clearance_class[visible_layers[0.5]] = 2   # visible even 0.5 deg lower -> "acceptable"
    if 1.0 in visible_layers:
        clearance_class[visible_layers[1.0]] = 3   # "good"
    if 2.0 in visible_layers:
        clearance_class[visible_layers[2.0]] = 4   # "excellent"
    clearance_class[no_data_mask] = -1

    # Limiting obstacle at the actual elevation, only meaningful where blocked/fragile.
    tan_actual = np.tan(np.radians(elevation))
    vis_building_only_rot = visible_mask_rotated(bld_only_rot, valid_rot, res, tan_actual, eye_height)
    vis_veg_only_rot = visible_mask_rotated(veg_only_rot, valid_rot, res, tan_actual, eye_height)
    vis_building_only = unrotate(vis_building_only_rot.astype(np.float32), transform, (H, W)) >= 0.5
    vis_veg_only = unrotate(vis_veg_only_rot.astype(np.float32), transform, (H, W)) >= 0.5

    blocked_combined = ~visible_layers[0.0]
    limiting = np.full((H, W), OBSTACLE_NONE, dtype=np.uint8)
    blocked_by_building = blocked_combined & ~vis_building_only
    blocked_by_veg = blocked_combined & ~vis_veg_only & ~blocked_by_building
    blocked_by_terrain_only = blocked_combined & vis_building_only & vis_veg_only
    limiting[blocked_by_building] = OBSTACLE_BUILDING
    limiting[blocked_by_veg] = OBSTACLE_VEGETATION
    limiting[blocked_by_terrain_only] = OBSTACLE_TERRAIN
    limiting[no_data_mask] = OBSTACLE_NODATA

    print("Writing visibility rasters...")
    write_raster(out_dir / "clearance_class.tif", clearance_class, grid, dtype="int8", nodata=-1)
    write_raster(out_dir / "limiting_obstacle.tif", limiting, grid, dtype="uint8", nodata=OBSTACLE_NODATA)
    write_raster(out_dir / "visible_actual.tif", visible_layers[0.0].astype(np.uint8), grid, dtype="uint8", nodata=255)

    classes, counts = np.unique(clearance_class[valid], return_counts=True)
    names = {-1: "blocked/nodata", 0: "blocked", 1: "fragile", 2: "acceptable", 3: "good", 4: "excellent"}
    print("\nClearance class distribution (valid cells):")
    for c, n in zip(classes, counts):
        print(f"  {names.get(int(c), c):<10} {n:>10,} ({100 * n / valid.sum():.2f}%)")

    print(f"\nTotal elapsed: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()

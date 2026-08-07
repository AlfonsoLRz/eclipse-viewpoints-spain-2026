"""Phase 1 - synthetic scenes validating the directional visibility scan.

Each scene is checked against the analytical shadow length L = h / tan(alpha)
cast by an obstacle of height h when the sun sits at elevation alpha.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
from compute_visibility import rotate_for_azimuth, unrotate, visible_mask_rotated  # noqa: E402

RES = 1.0
EYE = 1.70


def scan(z: np.ndarray, azimuth: float, elevation_deg: float, valid: np.ndarray | None = None):
    """Run the real pipeline path: rotate -> scan -> unrotate.

    Returns (visible, valid_back). The sun-aligned bounding box is larger than
    the source grid, so the round trip reintroduces invalid border cells; those
    read as "not visible" and must be excluded before interpreting a result,
    otherwise they dominate any spatial statistic.
    """
    if valid is None:
        valid = np.ones_like(z, dtype=bool)
    z_rot, transform = rotate_for_azimuth(z, azimuth, fill=-9999.0)
    z_rot = np.where(z_rot <= -9998.0, np.nan, z_rot)
    valid_rot, _ = rotate_for_azimuth(valid.astype(np.float32), azimuth, fill=0.0)
    vis_rot = visible_mask_rotated(z_rot, valid_rot > 0.5, RES, np.tan(np.radians(elevation_deg)), EYE)
    vis = unrotate(vis_rot.astype(np.float32), transform, z.shape) >= 0.5
    valid_back = unrotate((valid_rot > 0.5).astype(np.float32), transform, z.shape) >= 0.5
    return vis, valid_back


def test_flat_ground_fully_visible():
    """With no obstacles every cell must see the sun."""
    z = np.zeros((200, 200), dtype=np.float32)
    vis, _ = scan(z, azimuth=270.0, elevation_deg=6.0)
    interior = vis[20:-20, 20:-20]
    assert interior.all(), f"flat ground blocked {(~interior).sum()} cells"


def test_wall_shadow_length_matches_analytical():
    """A wall of height h due west casts a shadow of length h/tan(alpha) eastward.

    The observer's eye is at EYE metres, so the shadow ends where the wall top
    drops below the eye ray: L = (h - EYE) / tan(alpha).
    """
    elevation = 6.0
    h = 20.0
    z = np.zeros((200, 600), dtype=np.float32)
    wall_col = 100
    z[:, wall_col] = h

    # azimuth 270 = sun due west, so shadows extend east (increasing column)
    vis, _ = scan(z, azimuth=270.0, elevation_deg=elevation)

    expected_len = (h - EYE) / np.tan(np.radians(elevation))
    row = 100
    shadow = ~vis[row, wall_col + 1:]
    # first visible cell east of the wall
    first_visible = int(np.argmax(~shadow)) if (~shadow).any() else len(shadow)
    measured = first_visible * RES

    tol = 5 * RES  # rotation resampling tolerance
    assert abs(measured - expected_len) <= tol, (
        f"shadow length {measured:.1f} m vs analytical {expected_len:.1f} m"
    )


def test_cells_west_of_wall_are_visible():
    """Cells on the sun side of an obstacle are never shadowed by it."""
    z = np.zeros((200, 600), dtype=np.float32)
    z[:, 300] = 25.0
    vis, _ = scan(z, azimuth=270.0, elevation_deg=6.0)
    west_side = vis[50:150, 100:299]
    assert west_side.all(), "cells between the sun and the wall must stay visible"


def test_taller_obstacle_casts_longer_shadow():
    elevation = 6.0
    lengths = []
    for h in (10.0, 30.0):
        z = np.zeros((120, 800), dtype=np.float32)
        z[:, 100] = h
        vis, _ = scan(z, azimuth=270.0, elevation_deg=elevation)
        shadow = ~vis[60, 101:]
        lengths.append(int(np.argmax(~shadow)) if (~shadow).any() else len(shadow))
    assert lengths[1] > lengths[0], f"taller obstacle did not cast a longer shadow: {lengths}"


def test_nodata_gap_does_not_blank_the_row():
    """A hole of invalid cells must not mark the rest of the scan line blocked.

    This is the regression test for the NaN-propagation bug that made 91% of
    the real raster read as 'blocked'.
    """
    z = np.zeros((200, 600), dtype=np.float32)
    valid = np.ones_like(z, dtype=bool)
    valid[:, 150:160] = False  # unknown strip
    z[~valid] = np.nan

    vis, _ = scan(z, azimuth=270.0, elevation_deg=6.0, valid=valid)

    downstream = vis[50:150, 200:550]
    frac = downstream.mean()
    assert frac > 0.95, (
        f"only {frac:.1%} of cells east of a nodata gap are visible; "
        "NaN is propagating through the scan"
    )


def test_shadow_direction_matches_azimuth():
    """A single pillar must cast its shadow directly away from the sun.

    A symmetric wall spanning every row cannot detect a transposed or mirrored
    rotation basis, so this uses an isolated pillar on a square grid and checks
    the full 2-D direction of the shadow centroid against the compass azimuth.
    """
    for az in (270.0, 284.526, 180.0, 90.0, 0.0, 315.0):
        n = 401
        z = np.zeros((n, n), dtype=np.float32)
        z[200, 200] = 40.0
        vis, valid_back = scan(z, azimuth=az, elevation_deg=6.0)
        blocked = (~vis) & valid_back
        blocked[200, 200] = False
        ys, xs = np.nonzero(blocked)
        assert len(ys) > 0, f"pillar cast no shadow at azimuth {az}"

        drow = ys.mean() - 200.0
        dcol = xs.mean() - 200.0
        norm = np.hypot(drow, dcol)
        assert norm > 1.0, f"degenerate shadow at azimuth {az}"
        drow, dcol = drow / norm, dcol / norm

        phi = np.radians(az)
        # away from sun, converted to pixel axes (col=+east, row=-north)
        exp_col = -np.sin(phi)
        exp_row = np.cos(phi)

        cos_sim = drow * exp_row + dcol * exp_col
        assert cos_sim > 0.9, (
            f"azimuth {az}: shadow points (drow={drow:.2f}, dcol={dcol:.2f}), "
            f"expected (drow={exp_row:.2f}, dcol={exp_col:.2f}), cos={cos_sim:.2f}"
        )


def test_low_sun_makes_small_obstacles_matter():
    """At 6 degrees a 3 m hedge still shadows tens of metres."""
    z = np.zeros((100, 400), dtype=np.float32)
    z[:, 50] = 3.0
    vis, _ = scan(z, azimuth=270.0, elevation_deg=6.0)
    shadow = ~vis[50, 51:]
    measured = int(np.argmax(~shadow)) if (~shadow).any() else len(shadow)
    expected = (3.0 - EYE) / np.tan(np.radians(6.0))
    assert abs(measured - expected) <= 5.0, f"{measured} vs {expected:.1f}"


def _brute_force_visible(z, azimuth_deg, elevation_deg, eye=EYE, max_steps=300):
    """Reference implementation: march toward the sun from every cell.

    Deliberately uses no rotation, so it shares no code path with the
    production scan and can arbitrate disagreements about sign conventions.
    """
    n_rows, n_cols = z.shape
    phi = np.radians(azimuth_deg)
    step_row, step_col = -np.cos(phi), np.sin(phi)  # one step TOWARD the sun
    tan_a = np.tan(np.radians(elevation_deg))

    vis = np.ones(z.shape, dtype=bool)
    for r in range(n_rows):
        for c in range(n_cols):
            z_eye = z[r, c] + eye
            for d in range(1, max_steps):
                rr = int(round(r + step_row * d))
                cc = int(round(c + step_col * d))
                if not (0 <= rr < n_rows and 0 <= cc < n_cols):
                    break
                if z[rr, cc] > z_eye + d * tan_a:
                    vis[r, c] = False
                    break
    return vis


def test_matches_brute_force_reference():
    """The rotate-and-scan result must agree with a direct ray march."""
    rng = np.random.default_rng(0)
    for az in (270.0, 284.526, 200.0):
        z = np.zeros((120, 120), dtype=np.float32)
        # a few scattered blocks of varying height
        for _ in range(8):
            r, c = rng.integers(10, 105, size=2)
            h = float(rng.uniform(5.0, 30.0))
            z[r:r + 6, c:c + 6] = h

        vis, valid_back = scan(z, azimuth=az, elevation_deg=6.0)
        ref = _brute_force_visible(z, az, 6.0)

        # compare on the interior, away from resampling edge effects
        sl = (slice(25, 95), slice(25, 95))
        agree = (vis[sl] == ref[sl]) | ~valid_back[sl]
        frac = agree.mean()
        assert frac > 0.93, f"azimuth {az}: only {frac:.1%} agreement with brute force"

"""Validate the NOAA solar-position implementation.

The decisive test is agreement with the CNIG eclipse raster's own azimuth at
maximum: that value comes from the official product and is independent of this
code, so it arbitrates any disagreement.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
from solar import parse_local, solar_position  # noqa: E402

# Zaragoza, centroid of the LiDAR block
LAT, LON = 41.66337, -0.91604
DATE = "2026-08-12"
UTC_OFFSET = 2


def test_matches_cnig_raster_at_maximum():
    """Must agree with the official eclipse raster at maximum eclipse."""
    meta_path = ROOT / "data" / "output" / "eclipse_metadata.json"
    if not meta_path.exists():
        pytest.skip("eclipse_metadata.json not generated yet")

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    dt = parse_local(DATE, meta["contact_times"]["maximum_eclipse"]["local"], UTC_OFFSET)
    az, el = solar_position(dt, LAT, LON)

    assert abs(az - meta["solar_azimuth_deg"]) < 0.5, (
        f"azimuth {az:.3f} vs CNIG {meta['solar_azimuth_deg']:.3f}"
    )
    # The raster value includes atmospheric refraction (~0.1 deg at this
    # altitude); this code returns the geometric elevation because the shadow
    # scan traces straight rays.
    assert abs(el - meta["solar_elevation_deg"]) < 0.3, (
        f"elevation {el:.3f} vs CNIG {meta['solar_elevation_deg']:.3f}"
    )


def test_azimuth_is_south_at_local_solar_noon():
    """A northern-hemisphere sun crosses due south at solar noon.

    Getting the azimuth quadrant test backwards puts it due north instead -
    which is exactly the bug this file was written to catch.
    """
    best_az, best_el = None, -99
    for minute in range(11 * 60, 16 * 60):  # scan local 11:00-16:00
        dt = parse_local(DATE, f"{minute // 60:02d}:{minute % 60:02d}:00", UTC_OFFSET)
        az, el = solar_position(dt, LAT, LON)
        if el > best_el:
            best_el, best_az = el, az

    assert 170 < best_az < 190, f"sun at culmination is at azimuth {best_az:.1f}, expected ~180"


def test_azimuth_increases_through_the_day():
    """Azimuth runs east -> south -> west, never backwards."""
    samples = []
    for hour in range(7, 21):
        dt = parse_local(DATE, f"{hour:02d}:00:00", UTC_OFFSET)
        az, el = solar_position(dt, LAT, LON)
        if el > 0:
            samples.append((hour, az))

    for (h1, a1), (h2, a2) in zip(samples, samples[1:]):
        assert a2 > a1, f"azimuth went backwards between {h1}:00 ({a1:.1f}) and {h2}:00 ({a2:.1f})"


def test_sun_rises_in_the_east_and_sets_in_the_west():
    morning = solar_position(parse_local(DATE, "08:00:00", UTC_OFFSET), LAT, LON)[0]
    evening = solar_position(parse_local(DATE, "20:00:00", UTC_OFFSET), LAT, LON)[0]
    assert 60 < morning < 110, f"morning azimuth {morning:.1f} is not eastward"
    assert 260 < evening < 310, f"evening azimuth {evening:.1f} is not westward"


def test_elevation_is_low_at_totality():
    """The whole project hinges on the sun being only ~6 deg up."""
    dt = parse_local(DATE, "20:29:39", UTC_OFFSET)
    _, el = solar_position(dt, LAT, LON)
    assert 5.0 < el < 7.0, f"elevation at maximum is {el:.2f}, expected about 6"


def test_sun_is_below_horizon_at_night():
    for hhmm in ("02:00:00", "23:30:00"):
        _, el = solar_position(parse_local(DATE, hhmm, UTC_OFFSET), LAT, LON)
        assert el < 0, f"sun above horizon at {hhmm} ({el:.1f} deg)"

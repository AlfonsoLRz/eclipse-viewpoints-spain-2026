"""Read the CNIG 10-band eclipse raster at the LAZ footprint centroid and
produce eclipse_metadata.json (contact times in UTC and local Europe/Madrid
time, solar elevation/azimuth at maximum eclipse).

See config/zaragoza.yaml `eclipse.band_meaning` for the (partially
confirmed) band mapping, and the plan document section 1.1.

Usage:
    .venv/Scripts/python.exe pipeline/compute_sun_vectors.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pyproj
import rasterio

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, build_grid, list_tiles, load_config, output_dir, save_json  # noqa: E402
from solar import parse_local, solar_position  # noqa: E402

LOCAL_UTC_OFFSET_HOURS = 2  # CEST on 2026-08-12

# The CNIG raster marks "no such contact here" with -1000, which matters for the totality
# bands: outside the path of totality there is no C2 or C3. Left unchecked, -1000 hours is
# not an error but valid datetime arithmetic. It rolls the date back six weeks, and
# strftime("%H:%M:%S") then discards the date, so a city that never sees totality reports
# a confident, plausible, entirely fictional time for it.
NODATA_HOUR = -999.0


def decimal_hour_to_times(date_str: str, decimal_hour: float) -> dict | None:
    """Contact time as UTC and local strings, or None where the raster has no value."""
    if decimal_hour is None or decimal_hour <= NODATA_HOUR:
        return None
    base = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    utc_dt = base + timedelta(hours=decimal_hour)
    local_dt = utc_dt + timedelta(hours=LOCAL_UTC_OFFSET_HOURS)
    return {
        "utc": utc_dt.strftime("%H:%M:%S"),
        "local": local_dt.strftime("%H:%M:%S"),
    }


def main():
    cfg = load_config()
    tiles = list_tiles(cfg)
    grid = build_grid(cfg, tiles)
    out_dir = output_dir(cfg)

    cx = (grid.left + grid.right) / 2.0
    cy = (grid.bottom + grid.top) / 2.0

    transformer = pyproj.Transformer.from_crs(grid.crs, "EPSG:3857", always_xy=True)
    x3857, y3857 = transformer.transform(cx, cy)

    raster_path = ROOT / cfg["eclipse"]["raster"]
    with rasterio.open(raster_path) as ds:
        row, col = ds.index(x3857, y3857)
        window = ((row, row + 1), (col, col + 1))
        bands = ds.read(window=window)[:, 0, 0]

    band_meaning = cfg["eclipse"]["band_meaning"]
    date_str = cfg["eclipse"]["date"]

    # band4/band5 share the same "unconfirmed_auxiliary_value" label, so build
    # the lookup positionally rather than via a name-keyed dict to avoid one
    # overwriting the other.
    values = {band_meaning[i + 1]: float(bands[i]) for i in range(len(bands)) if i not in (3, 4)}
    band4_raw, band5_raw = float(bands[3]), float(bands[4])

    contacts = {
        "C1_partial_begin": decimal_hour_to_times(date_str, values["c1_partial_begin_utc_decimal_hour"]),
        "C2_totality_begin": decimal_hour_to_times(date_str, values["c2_totality_begin_utc_decimal_hour"]),
        "maximum_eclipse": decimal_hour_to_times(date_str, values["maximum_eclipse_utc_decimal_hour"]),
        "C3_totality_end": decimal_hour_to_times(date_str, values["c3_totality_end_utc_decimal_hour"]),
        "C4_partial_end": decimal_hour_to_times(date_str, values["c4_partial_end_utc_decimal_hour"]),
    }

    # Whether totality happens here is decided by the presence of C2 and C3, not by the
    # obscuration band. That band is continuous, not a flag: it reads 0.9715 at Linares,
    # and rounding it to a boolean would claim totality for a 97% partial eclipse.
    is_total = contacts["C2_totality_begin"] is not None and contacts["C3_totality_end"] is not None
    totality_seconds = (
        (values["c3_totality_end_utc_decimal_hour"] - values["c2_totality_begin_utc_decimal_hour"])
        * 3600.0
    ) if is_total else None

    # Sun direction at each contact, so the viewer can show where to look at any
    # moment rather than only at maximum. Computed with the NOAA algorithm in
    # solar.py, which is checked against this raster's own azimuth at maximum by
    # tests/test_solar.py.
    to_wgs84 = pyproj.Transformer.from_crs(grid.crs, "EPSG:4326", always_xy=True)
    ref_lon, ref_lat = to_wgs84.transform(cx, cy)

    # Contacts the raster has no value for are omitted rather than given a position. The
    # viewer builds its moment selector from the keys present here, so a missing C2 simply
    # never appears as something to look at.
    sun_track = {}
    for key, times in contacts.items():
        if times is None:
            continue
        dt = parse_local(date_str, times["local"], LOCAL_UTC_OFFSET_HOURS)
        az, el = solar_position(dt, ref_lat, ref_lon)
        sun_track[key] = {
            "local": times["local"],
            "azimuth_deg": round(az, 3),
            "elevation_deg": round(el, 3),
        }

    # A few extra samples across totality +/- 2 minutes, matching the plan's
    # "totality +/- 2 min" visibility option.
    max_local = contacts["maximum_eclipse"]["local"]
    max_dt = parse_local(date_str, max_local, LOCAL_UTC_OFFSET_HOURS)
    timeline = []
    for offset_s in range(-180, 181, 30):
        dt = max_dt + timedelta(seconds=offset_s)
        az, el = solar_position(dt, ref_lat, ref_lon)
        local_dt = dt + timedelta(hours=LOCAL_UTC_OFFSET_HOURS)
        timeline.append({
            "offset_s": offset_s,
            "local": local_dt.strftime("%H:%M:%S"),
            "azimuth_deg": round(az, 3),
            "elevation_deg": round(el, 3),
        })

    metadata = {
        "generated_by": "pipeline/compute_sun_vectors.py",
        "eclipse_date": date_str,
        "timezone_local": cfg["eclipse"]["timezone_local"],
        "local_utc_offset_hours": LOCAL_UTC_OFFSET_HOURS,
        "reference_point": {"crs25830": [cx, cy], "epsg3857": [x3857, y3857]},
        "city": cfg.get("city", {}).get("name", ""),
        "city_slug": cfg.get("city", {}).get("slug", ""),
        "solar_elevation_deg": round(values["solar_elevation_deg_at_maximum"], 3),
        "solar_azimuth_deg": round(values["solar_azimuth_deg_at_maximum"], 3),
        "is_total": is_total,
        # Band 3 is a continuous obscuration fraction, not the flag its label suggests.
        # Reported as measured so a 97% partial reads as a 97% partial.
        "obscuration": round(values["inside_totality_path_flag"], 4),
        "totality_duration_seconds": round(totality_seconds, 1) if is_total else None,
        "contact_times": contacts,
        "sun_at_contacts": sun_track,
        "sun_timeline": timeline,
        "raw_unconfirmed_bands": {
            "band4": band4_raw,
            "band5": band5_raw,
        },
        "band_confidence_note": cfg["eclipse"]["band_confidence"],
        "note": (
            "Solar elevation/azimuth are taken at the instant of maximum eclipse and "
            "treated as constant through C2-C3 (~{:.0f}s of totality); the resulting "
            "angular change is well under 0.1 degrees and does not materially affect "
            "visibility classification.".format(totality_seconds)
            if is_total else
            "Solar elevation/azimuth are taken at the instant of maximum eclipse. This "
            "site is outside the path of totality ({:.2f}% obscuration at maximum), so "
            "there is no C2-C3 interval; the geometry around maximum changes by well "
            "under 0.1 degrees a minute either way.".format(
                values["inside_totality_path_flag"] * 100)
        ),
    }

    save_json(out_dir / "eclipse_metadata.json", metadata)
    print("Eclipse metadata:")
    for k, v in metadata.items():
        if k not in ("band_confidence_note", "note"):
            print(f"  {k}: {v}")
    print(f"\nWrote {out_dir / 'eclipse_metadata.json'}")


if __name__ == "__main__":
    main()

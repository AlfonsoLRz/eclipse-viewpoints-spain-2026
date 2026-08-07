"""Solar position (NOAA algorithm).

The CNIG eclipse raster supplies azimuth and elevation only at maximum, but the
viewer needs the sun's direction at every contact time so it can draw where to
look. This is the standard NOAA solar-position calculation, accurate to well
under 0.1 degrees for dates near 2026 - far tighter than the 1 m raster or the
vegetation model, which are the real sources of error here.

The implementation is validated against the raster's own value at maximum in
tests/test_solar.py, so an error in this file cannot silently disagree with the
official product.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone


def _julian_day(dt: datetime) -> float:
    """Julian day from a timezone-aware UTC datetime."""
    dt = dt.astimezone(timezone.utc)
    y, m = dt.year, dt.month
    d = dt.day + (dt.hour + dt.minute / 60 + dt.second / 3600) / 24.0
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    return math.floor(365.25 * (y + 4716)) + math.floor(30.6001 * (m + 1)) + d + b - 1524.5


def solar_position(dt: datetime, lat_deg: float, lon_deg: float) -> tuple[float, float]:
    """Return (azimuth_deg, elevation_deg) for an instant and location.

    Azimuth is compass bearing: 0 = north, 90 = east, measured clockwise.
    Elevation is the true geometric altitude above the horizon, WITHOUT
    atmospheric refraction - refraction would lift the apparent sun by roughly
    0.1 deg at 6 deg altitude, but the shadow geometry in compute_visibility.py
    uses straight rays, so the geometric value is the consistent choice.
    """
    jd = _julian_day(dt)
    t = (jd - 2451545.0) / 36525.0  # Julian centuries since J2000.0

    # Geometric mean longitude and anomaly of the sun
    l0 = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360.0
    m = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    m_rad = math.radians(m)

    # Equation of centre
    c = (math.sin(m_rad) * (1.914602 - t * (0.004817 + 0.000014 * t))
         + math.sin(2 * m_rad) * (0.019993 - 0.000101 * t)
         + math.sin(3 * m_rad) * 0.000289)

    true_long = l0 + c
    omega = 125.04 - 1934.136 * t
    app_long = true_long - 0.00569 - 0.00478 * math.sin(math.radians(omega))

    # Obliquity of the ecliptic, with nutation correction
    seconds = 21.448 - t * (46.8150 + t * (0.00059 - t * 0.001813))
    e0 = 23.0 + (26.0 + seconds / 60.0) / 60.0
    e = e0 + 0.00256 * math.cos(math.radians(omega))

    app_long_rad = math.radians(app_long)
    e_rad = math.radians(e)

    # Right ascension and declination
    ra = math.degrees(math.atan2(math.cos(e_rad) * math.sin(app_long_rad),
                                 math.cos(app_long_rad)))
    decl = math.degrees(math.asin(math.sin(e_rad) * math.sin(app_long_rad)))

    # Equation of time (minutes)
    y = math.tan(e_rad / 2) ** 2
    l0_rad = math.radians(l0)
    eot = 4 * math.degrees(
        y * math.sin(2 * l0_rad)
        - 2 * 0.016708634 * math.sin(m_rad)
        + 4 * 0.016708634 * y * math.sin(m_rad) * math.cos(2 * l0_rad)
        - 0.5 * y * y * math.sin(4 * l0_rad)
        - 1.25 * 0.016708634 ** 2 * math.sin(2 * m_rad)
    )

    utc = dt.astimezone(timezone.utc)
    minutes = utc.hour * 60 + utc.minute + utc.second / 60.0
    true_solar_time = (minutes + eot + 4 * lon_deg) % 1440

    hour_angle = true_solar_time / 4.0 - 180.0
    if hour_angle < -180:
        hour_angle += 360

    lat_rad = math.radians(lat_deg)
    decl_rad = math.radians(decl)
    ha_rad = math.radians(hour_angle)

    cos_zenith = (math.sin(lat_rad) * math.sin(decl_rad)
                  + math.cos(lat_rad) * math.cos(decl_rad) * math.cos(ha_rad))
    cos_zenith = max(-1.0, min(1.0, cos_zenith))
    zenith = math.degrees(math.acos(cos_zenith))
    elevation = 90.0 - zenith

    # Azimuth measured clockwise from north.
    #
    # The acos form of this formula returns an angle measured from SOUTH and
    # needs a hemisphere test to recover the quadrant, which is easy to get
    # backwards - doing so puts the sun due north at local noon and makes the
    # azimuth run backwards through the day. atan2 resolves the quadrant on its
    # own, so it is used here instead.
    #
    # With the hour angle H negative before local solar noon and positive after,
    #   sin(A) = -sin(H) cos(decl)
    #   cos(A) =  sin(decl) cos(lat) - cos(H) cos(decl) sin(lat)
    # where A is measured clockwise from north.
    sin_az = -math.sin(ha_rad) * math.cos(decl_rad)
    cos_az = (math.sin(decl_rad) * math.cos(lat_rad)
              - math.cos(ha_rad) * math.cos(decl_rad) * math.sin(lat_rad))
    azimuth = math.degrees(math.atan2(sin_az, cos_az))

    return azimuth % 360.0, elevation


def parse_local(date_str: str, hhmmss: str, utc_offset_hours: int) -> datetime:
    """Build a UTC datetime from a local wall-clock time."""
    naive = datetime.strptime(f"{date_str} {hhmmss}", "%Y-%m-%d %H:%M:%S")
    return (naive - timedelta(hours=utc_offset_hours)).replace(tzinfo=timezone.utc)

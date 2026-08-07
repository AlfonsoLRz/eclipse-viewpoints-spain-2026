"""Render the README figures as SVG.

These are generated rather than drawn so they cannot drift from the code they
illustrate: the sun track comes from solar.py, the contact times from the CNIG
raster metadata when it is available, and the shadow geometry uses the same
tan(alpha) relation the scan does.

    python pipeline/make_diagrams.py

Writes docs/sun-altitude.svg and docs/shadow-geometry.svg.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from solar import parse_local, solar_position  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

LAT, LON = 41.66337, -0.91604
DATE = "2026-08-12"
UTC_OFFSET = 2

# Fallback contact times (local). Overridden by the CNIG-derived metadata when
# the pipeline has been run; the two agree to the second.
CONTACTS = {
    "C1": "19:34:34",
    "C2": "20:28:56",
    "max": "20:29:39",
    "C3": "20:30:21",
    "C4": "21:21:23",
}

INK = "#0d1017"
FG = "#d8dee9"
MUTED = "#8b949e"
SUN = "#f2b134"
GOOD = "#3fb950"
BAD = "#8b1a1a"
GRID = "#2a313c"


def load_contacts() -> dict[str, str]:
    p = ROOT / "data" / "output" / "eclipse_metadata.json"
    if not p.exists():
        return CONTACTS
    m = json.loads(p.read_text(encoding="utf-8"))
    ct = m.get("contact_times", {})
    key = {
        "C1": "C1_partial_begin", "C2": "C2_totality_begin",
        "max": "maximum_eclipse", "C3": "C3_totality_end",
        "C4": "C4_partial_end",
    }
    out = {}
    for short, long in key.items():
        out[short] = ct.get(long, {}).get("local", CONTACTS[short])
    return out


def minutes(hhmmss: str) -> float:
    h, m, s = (int(x) for x in hhmmss.split(":"))
    return h * 60 + m + s / 60.0


def sun_at(mins: float) -> tuple[float, float]:
    h, m = int(mins) // 60, int(mins) % 60
    s = int(round((mins - int(mins)) * 60))
    dt = parse_local(DATE, f"{h:02d}:{m:02d}:{s:02d}", UTC_OFFSET)
    return solar_position(dt, LAT, LON)


# --------------------------------------------------------------- sun altitude

def sun_altitude_svg() -> str:
    """Altitude against local time, from late afternoon to sunset.

    The point of the figure is the vertical scale: totality happens in the last
    few degrees before the sun goes down, which is why buildings decide the
    answer and a coarse terrain model cannot.
    """
    c = load_contacts()
    W, H = 880, 420
    L, R, T, B = 62, 24, 30, 62
    pw, ph = W - L - R, H - T - B

    t0, t1 = 17 * 60.0, minutes(c["C4"]) + 8
    el_max = 48.0

    def px(t: float) -> float:
        return L + (t - t0) / (t1 - t0) * pw

    def py(el: float) -> float:
        return T + (el_max - el) / (el_max - (-6.0)) * ph

    track = []
    t = t0
    while t <= t1:
        _, el = sun_at(t)
        track.append((px(t), py(el)))
        t += 2.0
    path = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in track)

    s: list[str] = []
    s.append(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
             f'width="{W}" height="{H}" font-family="ui-sans-serif,Segoe UI,Helvetica,Arial" '
             f'role="img" aria-label="Solar altitude at Zaragoza through the eclipse of 12 August 2026">')
    s.append(f'<rect width="{W}" height="{H}" fill="{INK}"/>')

    # horizontal gridlines every 10 degrees
    for el in range(0, int(el_max) + 1, 10):
        y = py(el)
        s.append(f'<line x1="{L}" y1="{y:.1f}" x2="{L + pw}" y2="{y:.1f}" stroke="{GRID}" stroke-width="1"/>')
        s.append(f'<text x="{L - 9}" y="{y + 4:.1f}" fill="{MUTED}" font-size="11" text-anchor="end">{el}°</text>')

    # hour ticks
    for hh in range(17, 23):
        t = hh * 60.0
        if not (t0 <= t <= t1):
            continue
        x = px(t)
        s.append(f'<line x1="{x:.1f}" y1="{T}" x2="{x:.1f}" y2="{T + ph}" stroke="{GRID}" stroke-width="1"/>')
        s.append(f'<text x="{x:.1f}" y="{T + ph + 20}" fill="{MUTED}" font-size="11" '
                 f'text-anchor="middle">{hh}:00</text>')

    # ground: below the horizon line
    y0 = py(0.0)
    s.append(f'<rect x="{L}" y="{y0:.1f}" width="{pw}" height="{T + ph - y0:.1f}" fill="#161b22"/>')
    s.append(f'<line x1="{L}" y1="{y0:.1f}" x2="{L + pw}" y2="{y0:.1f}" stroke="{FG}" stroke-width="1.5"/>')
    s.append(f'<text x="{L + 8}" y="{y0 + 16:.1f}" fill="{MUTED}" font-size="11">horizon</text>')

    # the band this project lives in: 0-7 degrees
    yb, yt = py(0.0), py(7.0)
    s.append(f'<rect x="{L}" y="{yt:.1f}" width="{pw}" height="{yb - yt:.1f}" '
             f'fill="{SUN}" opacity="0.07"/>')

    # totality window
    x2, x3 = px(minutes(c["C2"])), px(minutes(c["C3"]))
    s.append(f'<rect x="{x2:.1f}" y="{T}" width="{max(x3 - x2, 2.0):.1f}" height="{ph}" '
             f'fill="{BAD}" opacity="0.55"/>')

    s.append(f'<path d="{path}" fill="none" stroke="{SUN}" stroke-width="2.5" '
             f'stroke-linejoin="round"/>')

    # contact markers
    for label in ("C1", "max", "C4"):
        t = minutes(c[label])
        _, el = sun_at(t)
        x, y = px(t), py(el)
        s.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.5" fill="{INK}" stroke="{SUN}" stroke-width="2"/>')
        txt = {"C1": "C1 partial begins", "max": "maximum", "C4": "C4 partial ends"}[label]
        # "maximum" sits just left of its marker: directly above would land on
        # the 0-7 degree band caption, and below would land on the track itself.
        dy = {"C1": -14, "max": -12, "C4": 20}[label]
        anchor = {"C1": "start", "max": "end", "C4": "end"}[label]
        ox = {"C1": 8, "max": -10, "C4": -8}[label]
        s.append(f'<text x="{x + ox:.1f}" y="{y + dy:.1f}" fill="{FG}" font-size="12" '
                 f'text-anchor="{anchor}">{txt} · {el:.1f}°</text>')

    # callout for the totality band
    s.append(f'<text x="{x2:.1f}" y="{T - 11}" fill="{BAD}" font-size="12" font-weight="600" '
             f'text-anchor="middle">totality · 84 s</text>')
    s.append(f'<text x="{L + 10}" y="{py(3.4):.1f}" fill="{SUN}" font-size="11" '
             f'text-anchor="start" opacity="0.9">the 0–7° band, where a wall or a tree decides the answer</text>')

    s.append(f'<text x="{L}" y="{H - 16}" fill="{MUTED}" font-size="11">'
             f'Zaragoza (41.663 N, 0.916 W) · local time CEST · geometric altitude from pipeline/solar.py'
             f'</text>')
    s.append("</svg>")
    return "\n".join(s)


# ------------------------------------------------------------ shadow geometry

def shadow_geometry_svg() -> str:
    """The separable horizon test the scan implements.

    Draws the sun ray grazing an obstacle and the observer sitting in the
    shadow it casts, with the comparison that decides visibility.
    """
    W, H = 880, 330
    GY = 250            # ground line
    alpha = math.radians(16.0)   # exaggerated from 6.1 so the figure is readable

    s: list[str] = []
    s.append(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
             f'width="{W}" height="{H}" font-family="ui-sans-serif,Segoe UI,Helvetica,Arial" '
             f'role="img" aria-label="Geometry of the separable horizon test used by the shadow scan">')
    s.append(f'<rect width="{W}" height="{H}" fill="{INK}"/>')

    s.append(f'<line x1="40" y1="{GY}" x2="{W - 40}" y2="{GY}" stroke="{FG}" stroke-width="1.5"/>')

    # building
    bx, bw, bh = 300.0, 74.0, 104.0
    s.append(f'<rect x="{bx}" y="{GY - bh}" width="{bw}" height="{bh}" fill="#2f3846" stroke="{MUTED}"/>')
    s.append(f'<text x="{bx + bw / 2}" y="{GY - bh - 10}" fill="{FG}" font-size="12" '
             f'text-anchor="middle">obstacle  z_i</text>')

    # grazing ray, from the building top down-sun (to the right = away from sun)
    top = (bx + bw, GY - bh)
    x_end = W - 60.0
    y_end = top[1] + (x_end - top[0]) * math.tan(alpha)
    s.append(f'<line x1="{top[0]}" y1="{top[1]}" x2="{x_end}" y2="{y_end:.1f}" '
             f'stroke="{SUN}" stroke-width="2" stroke-dasharray="6 4"/>')

    # sun, up-sun side. Sits low enough that its title clears the caption at the
    # top of the figure.
    sx, sy = 132.0, GY - bh - 52.0
    s.append(f'<circle cx="{sx}" cy="{sy}" r="17" fill="{SUN}"/>')
    for k in range(12):
        a = k * math.pi / 6
        s.append(f'<line x1="{sx + 22 * math.cos(a):.1f}" y1="{sy + 22 * math.sin(a):.1f}" '
                 f'x2="{sx + 29 * math.cos(a):.1f}" y2="{sy + 29 * math.sin(a):.1f}" '
                 f'stroke="{SUN}" stroke-width="2" opacity="0.75"/>')
    s.append(f'<text x="{sx}" y="{sy - 34}" fill="{SUN}" font-size="13" text-anchor="middle" '
             f'font-weight="600">sun · α = 6.1°</text>')
    s.append(f'<text x="{sx}" y="{sy + 46}" fill="{MUTED}" font-size="11" text-anchor="middle">'
             f'azimuth 284.5° (WNW)</text>')

    # ray from sun to building top
    s.append(f'<line x1="{sx + 24:.1f}" y1="{sy + 10:.1f}" x2="{top[0]}" y2="{top[1]}" '
             f'stroke="{SUN}" stroke-width="2" opacity="0.5"/>')

    # shadowed span on the ground
    x_sh = top[0] + bh / math.tan(alpha)
    s.append(f'<line x1="{top[0]}" y1="{GY}" x2="{x_sh:.1f}" y2="{GY}" stroke="{BAD}" stroke-width="5"/>')
    s.append(f'<text x="{(top[0] + x_sh) / 2:.1f}" y="{GY + 20:.1f}" fill="{BAD}" font-size="12" '
             f'text-anchor="middle">shadow · L = h / tan α</text>')

    # blocked observer
    ox = top[0] + 78.0
    s.append(f'<circle cx="{ox:.1f}" cy="{GY - 15}" r="6" fill="{BAD}"/>')
    s.append(f'<line x1="{ox:.1f}" y1="{GY - 9}" x2="{ox:.1f}" y2="{GY}" stroke="{BAD}" stroke-width="3"/>')
    s.append(f'<text x="{ox:.1f}" y="{GY - 30}" fill="{BAD}" font-size="12" text-anchor="middle">blocked</text>')

    # clear observer beyond the shadow, kept off the right edge
    cx2 = min(x_sh + 96.0, W - 96.0)
    s.append(f'<circle cx="{cx2:.1f}" cy="{GY - 15}" r="6" fill="{GOOD}"/>')
    s.append(f'<line x1="{cx2:.1f}" y1="{GY - 9}" x2="{cx2:.1f}" y2="{GY}" stroke="{GOOD}" stroke-width="3"/>')
    s.append(f'<text x="{cx2:.1f}" y="{GY - 30}" fill="{GOOD}" font-size="12" text-anchor="middle">sun visible</text>')

    # u axis: increases away from the sun
    ay = GY + 46
    s.append(f'<line x1="{bx}" y1="{ay}" x2="{W - 60}" y2="{ay}" stroke="{MUTED}" stroke-width="1"/>')
    s.append(f'<polygon points="{W - 60},{ay} {W - 70},{ay - 4} {W - 70},{ay + 4}" fill="{MUTED}"/>')
    s.append(f'<text x="{W - 76}" y="{ay - 8}" fill="{MUTED}" font-size="11" text-anchor="end">'
             f'u increases away from the sun</text>')

    s.append(f'<text x="40" y="{H - 14}" fill="{FG}" font-size="13">'
             f'obstacle i blocks observer p  ⇔  z_i + u_i·tan α  &gt;  (z_p + eye) + u_p·tan α</text>')
    s.append(f'<text x="40" y="34" fill="{MUTED}" font-size="12">'
             f'A running maximum of z + u·tan α decides visibility in one linear pass per row '
             f'(angle exaggerated for legibility).</text>')
    s.append("</svg>")
    return "\n".join(s)


def main() -> None:
    DOCS.mkdir(parents=True, exist_ok=True)
    for name, svg in (("sun-altitude.svg", sun_altitude_svg()),
                      ("shadow-geometry.svg", shadow_geometry_svg())):
        (DOCS / name).write_text(svg, encoding="utf-8")
        print(f"wrote {DOCS / name}")


if __name__ == "__main__":
    main()

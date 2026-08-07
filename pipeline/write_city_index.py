"""Write viewer/public/data/cities.json, the list the viewer's city picker reads.

Each city is built independently with its own PNOA_CONFIG, so nothing during a normal
pipeline run knows that the others exist. This scans the viewer data directory for cities
that are actually complete and writes a manifest from their own metadata, which means the
picker can never offer a city whose data is missing or half-copied.

Run it after building any city:

    python pipeline/write_city_index.py

Cities with totality come first, then the rest by descending obscuration, so the picker
opens on the most complete eclipse rather than on alphabetical order.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, load_config  # noqa: E402

# A city is only offered if all of these are present. Tiles are checked separately.
REQUIRED = ("eclipse_metadata.json", "layers.json", "candidates.json", "laz_coverage.geojson")
RAMPS = ("classes", "viridis", "traffic", "binary", "mono")


def main() -> None:
    cfg = load_config()
    web_root = (ROOT / cfg["paths"]["viewer_public_data"]).parent

    cities = []
    for d in sorted(p for p in web_root.iterdir() if p.is_dir()):
        missing = [f for f in REQUIRED if not (d / f).exists()]
        if missing:
            print(f"  skipping {d.name}: missing {', '.join(missing)}")
            continue
        if not all((d / "tiles" / r).is_dir() for r in RAMPS):
            print(f"  skipping {d.name}: incomplete tile set")
            continue
        meta = json.loads((d / "eclipse_metadata.json").read_text(encoding="utf-8"))
        layers = json.loads((d / "layers.json").read_text(encoding="utf-8"))
        cities.append({
            "slug": d.name,
            "name": meta.get("city") or d.name.title(),
            "isTotal": bool(meta.get("is_total")),
            "obscuration": meta.get("obscuration"),
            "center": layers.get("center"),
        })

    cities.sort(key=lambda c: (not c["isTotal"], -(c["obscuration"] or 0), c["name"]))

    dst = web_root / "cities.json"
    dst.write_text(
        json.dumps({"generated_by": "pipeline/write_city_index.py", "cities": cities},
                   indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    for c in cities:
        kind = "total" if c["isTotal"] else f"{c['obscuration'] * 100:.1f}% partial"
        print(f"  {c['slug']:10s} {c['name']:10s} {kind}")
    print(f"Wrote {dst} ({len(cities)} cities)")


if __name__ == "__main__":
    main()

"""Shared helpers for the Zaragoza eclipse-visibility offline pipeline.

Grid convention
----------------
All rasters produced by this pipeline share a single 1 m grid in
EPSG:25830, aligned to the LAZ tile boundaries:

    left   = min_easting_km  * 1000
    right  = (max_easting_km + 1) * 1000
    bottom = min_northing_km * 1000
    top    = (max_northing_km + 1) * 1000

Row 0 is the northernmost row (standard north-up raster convention).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import rasterio
import yaml
from rasterio.transform import from_origin

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "zaragoza.yaml"

TILE_NAME_RE = re.compile(r"PNOA_2023_ARA_(\d+)-(\d+)_H30_NPC02\.laz$")


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def fill_dtm_gaps(dtm: np.ndarray, max_fill_px: int) -> tuple[np.ndarray, np.ndarray]:
    """Nearest-neighbour fill limited to `max_fill_px`. Returns (filled, filled_mask)
    where filled_mask marks cells that were interpolated (lower confidence).

    Two callers want opposite things from the cap. build_surfaces.py keeps it at 5 m so
    the river and other genuine voids stay nodata: inventing ground under water would
    hand the clearance analysis a flat, unobstructed surface and it would recommend
    standing on the Ebro. package_tiles.py passes an unbounded cap when encoding terrain
    tiles, because a mesh has no such notion of honesty available to it - every cell
    needs some elevation or MapLibre renders a pit.
    """
    from scipy import ndimage

    valid = ~np.isnan(dtm)
    if valid.all():
        return dtm, np.zeros_like(dtm, dtype=bool)
    dist, (iy, ix) = ndimage.distance_transform_edt(~valid, return_indices=True)
    nearest = dtm[iy, ix]
    fillable = (~valid) & (dist <= max_fill_px)
    out = dtm.copy()
    out[fillable] = nearest[fillable]
    return out, fillable


def output_dir(cfg: dict) -> Path:
    p = ROOT / cfg["paths"]["data_output"]
    p.mkdir(parents=True, exist_ok=True)
    return p


def intermediate_dir(cfg: dict) -> Path:
    p = ROOT / cfg["paths"]["data_intermediate"]
    p.mkdir(parents=True, exist_ok=True)
    return p


def web_data_dir(cfg: dict) -> Path:
    p = ROOT / cfg["paths"]["viewer_public_data"]
    p.mkdir(parents=True, exist_ok=True)
    return p


@dataclass(frozen=True)
class TileRef:
    """PNOA tile naming convention: the easting number is the tile's WEST
    (left) edge in km, but the northing number is the tile's NORTH (top)
    edge in km - confirmed against actual LAZ header bounds (a tile named
    ..._673-4615... spans y in [4614000, 4615000)). This is asymmetric and
    easy to get wrong.
    """

    path: Path
    easting_km: int
    northing_km: int

    @property
    def left(self) -> float:
        return self.easting_km * 1000.0

    @property
    def top(self) -> float:
        return self.northing_km * 1000.0

    @property
    def bottom(self) -> float:
        return (self.northing_km - 1) * 1000.0


def list_tiles(cfg: dict) -> list[TileRef]:
    tiles = []
    for path in sorted(ROOT.glob(cfg["laz"]["glob"])):
        m = TILE_NAME_RE.search(path.name)
        if not m:
            continue
        tiles.append(TileRef(path=path, easting_km=int(m.group(1)), northing_km=int(m.group(2))))
    if not tiles:
        raise RuntimeError(f"No LAZ tiles matched {cfg['laz']['glob']} under {ROOT}")
    return tiles


@dataclass(frozen=True)
class Grid:
    """Global 1 m raster grid covering the full LAZ footprint."""

    left: float
    bottom: float
    right: float
    top: float
    resolution: float
    crs: str

    @property
    def width(self) -> int:
        return int(round((self.right - self.left) / self.resolution))

    @property
    def height(self) -> int:
        return int(round((self.top - self.bottom) / self.resolution))

    @property
    def transform(self):
        return from_origin(self.left, self.top, self.resolution, self.resolution)

    def tile_offset(self, tile: TileRef, tile_size_m: float = 1000.0) -> tuple[int, int]:
        """Return (row_offset_from_top, col_offset_from_left) for a tile's top-left corner."""
        col = int(round((tile.left - self.left) / self.resolution))
        row = int(round((self.top - tile.top) / self.resolution))
        return row, col

    def to_pixel(self, x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        col = np.floor((x - self.left) / self.resolution).astype(np.int64)
        row = np.floor((self.top - y) / self.resolution).astype(np.int64)
        return row, col

    def to_bounds_geojson_wgs84(self):
        import pyproj

        transformer = pyproj.Transformer.from_crs(self.crs, "EPSG:4326", always_xy=True)
        corners_map = [
            (self.left, self.top),
            (self.right, self.top),
            (self.right, self.bottom),
            (self.left, self.bottom),
            (self.left, self.top),
        ]
        ring = [list(transformer.transform(x, y)) for x, y in corners_map]
        return {
            "type": "Feature",
            "properties": {"name": "laz_coverage"},
            "geometry": {"type": "Polygon", "coordinates": [ring]},
        }


def build_grid(cfg: dict, tiles: Iterable[TileRef]) -> Grid:
    tiles = list(tiles)
    min_e = min(t.easting_km for t in tiles)
    max_e = max(t.easting_km for t in tiles)
    min_n = min(t.northing_km for t in tiles)
    max_n = max(t.northing_km for t in tiles)
    res = float(cfg["resolution_m"])
    return Grid(
        left=min_e * 1000.0,
        bottom=(min_n - 1) * 1000.0,
        right=(max_e + 1) * 1000.0,
        top=max_n * 1000.0,
        resolution=res,
        crs=cfg["crs"],
    )


def write_raster(path: Path, array: np.ndarray, grid: Grid, dtype=None, nodata=None):
    dtype = dtype or array.dtype
    profile = {
        "driver": "GTiff",
        "height": array.shape[0],
        "width": array.shape[1],
        "count": 1,
        "dtype": dtype,
        "crs": grid.crs,
        "transform": grid.transform,
        "nodata": nodata,
        "compress": "deflate",
        "predictor": 2,
        "tiled": True,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(array.astype(dtype), 1)


def read_raster(path: Path) -> tuple[np.ndarray, dict]:
    with rasterio.open(path) as src:
        arr = src.read(1)
        meta = src.meta.copy()
    return arr, meta


def save_json(path: Path, obj) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)

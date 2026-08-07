> [!WARNING]
> **Check the spot in person before eclipse day.** The obstacles in this model come from a
> LiDAR survey flown in 2023, three years before the eclipse. Trees grow, get pruned and
> get felled, and Zaragoza has been unusually busy with the chainsaws lately. Buildings go
> up and come down. Anything built or planted since the survey is invisible to the model,
> and a tree that has put on three years of growth may now block a sightline the map shows
> as clear. Temporary obstacles are not modelled at all, so a crane, a marquee, a stage or
> a parked lorry can take out an otherwise perfect spot on the day.
>
> Treat the map as a shortlist to go and inspect, not a guarantee.

# Zaragoza urban eclipse visibility

Where in Zaragoza can you actually see the total eclipse of 12 August 2026?

**[Open the map](https://alfonsolrz.github.io/eclipse-zaragoza-2026/)**

At maximum the sun sits only 6.1° above the horizon at azimuth 284.5° (WNW), so a single
tree or a two-storey wall can hide it from hundreds of metres away. Coarse terrain shadow
maps cannot answer this; the question is decided by buildings and vegetation.

This project answers it by ray-scanning a 1 m LiDAR surface model over a 7 × 7 km block of
the city, then publishing the result as a web map over PNOA orthophotography. It ranks
places you can actually stand: parks, squares, gardens and other open public space.

![overview](docs/overview.png)

## Why this is hard

The eclipse happens as the sun is going down. Totality lasts 84 s and arrives with the sun
barely clear of the rooftops. Everything to the left of the marked band is comfortable
observing, and none of the eclipse happens there.

![Solar altitude at Zaragoza through the eclipse of 12 August 2026](docs/sun-altitude.svg)

At 6.1° a 10 m building casts a 94 m shadow. That is why the answer is decided by
individual buildings and trees rather than by terrain, and why the model needs a 1 m
surface rather than a coarse elevation map.

![Geometry of the horizon test: a sun ray grazing a building, the shadow it casts, and an
observer inside it](docs/shadow-geometry.svg)

Standing anywhere in that shadow means no eclipse. The map is the same test run over every
square metre of the city, for the sun's exact position at each contact time. The angle
above is drawn steeper than the real 6.1°, which would be too shallow to read.

## Reading the map

The viewer shows the sun's direction as a compass dial plus a side profile of its altitude,
and draws a 600 m dashed ray from any clicked point or selected zone. That ray is the line
that has to stay clear of buildings and trees. The moment selector switches between C1, C2,
maximum, C3 and C4.

Five colour scales cover the same data: clearance classes, viridis, red to green, a plain
visible/blocked split, and a shadow-only grey. The orthophoto, an OpenStreetMap name
overlay and the dashed analysed-area outline can each be toggled independently, so the
shadow model can be read on its own or against street names.

The overlay covers all evaluated ground (~81% of the block; the rest is building rooftops).
Reachable public space is drawn solid and everything else at 45% opacity, so the shadow
model is legible citywide while the places actually worth standing in still stand out.

### Clearance classes

Clearance is the angular margin between the sun and whatever is closest to blocking it.
More clearance means more room for the model to be wrong and the spot still to work.

| Class | Clearance | Meaning |
|---|---|---|
| blocked | < 0° | the sun is behind an obstacle |
| fragile | 0 to 0.5° | visible, but a small error changes the answer |
| acceptable | 0.5 to 1° | visible |
| good | 1 to 2° | comfortable margin |
| excellent | > 2° | robust |

Zones report the 10th percentile clearance rather than the strict minimum: over thousands
of cells the minimum is set by a single edge pixel clipped by a wall, which collapses
nearly every zone into the same class.

Prefer an excellent or good zone over a fragile one. A fragile zone is one where three
years of tree growth, or a van parked in the wrong place, flips the answer.

## Coverage and the analysed area

The PNOA orthophoto mosaic (664.5 to 692.9 km E) is far larger than the LiDAR block
(670 to 677 km E, 4611 to 4618 km N), so imagery alone says nothing about where the
analysis holds.

The map pulls PNOA Máxima Actualidad imagery live from the
[IGN WMTS](https://www.ign.es/wmts/pnoa-ma) and draws the LiDAR footprint as a dashed
outline. Imagery continues past the block, so that dashed edge is what marks where
visibility was computed. Outside it the picture is real but nothing has been analysed.

The WMTS serves current imagery, which may be newer than the LiDAR and newer than the
2025-07-14 mosaic the offline analysis was run against. If the photo shows a building the
shadow model does not seem to know about, trust the photo.

## Known limitations

- Walkability combines LiDAR geometry with OSM land use. An earlier version used LiDAR
  alone and recommended motorway verges, farm plots and the surface of the Ebro, all
  genuinely flat, open and unobstructed. Two lessons held: distance to the *nearest*
  building is a bad urbanity proxy (use local building *density*), and water reads as
  ideal ground because it returns almost no LiDAR points. OSM now supplies the positive
  space (parks, squares, gardens, pitches, pedestrian areas, car parks) and the hard
  exclusions (water, motorways, railways, industrial, military, farmland). OSM is
  maintained by volunteers and is not authoritative on public access, so check before
  relying on a spot.
- Car parks are included but down-weighted. They are open and usually accessible, yet a
  supermarket apron is a worse recommendation than a garden of equal clearance. The UI
  exposes both a preference slider and a hard filter.
- Vegetation uses the conservative model (canopy widened by ~2 m and raised by 1 m), which
  errs toward calling a spot blocked rather than clear. Even so, see the warning above:
  the canopy is as it was in 2023.
- Roofs are excluded as observing positions, so the overlay leaves them unpainted even
  though they are sunlit. The shadow-only scale is the exception: it shows the physical
  shadow everywhere, roofs included.
- Facades are under-sampled by airborne LiDAR, so building footprints are dilated by 1 m
  to compensate.
- Temporary obstacles (cranes, stages, marquees, parked lorries) are not modelled.
- Totality lasts only 84 s, and the sun moves less than 0.1° over that span, so a single
  geometry at maximum is used for C2, maximum and C3 alike.

## Data sources

- PNOA LiDAR 2023 (NPC02), 49 tiles, ~8 pts/m², 388 M points.
- PNOA Máxima Actualidad orthophotography, mosaic dated 2025-07-14, 0.25 m.
- CNIG eclipse rasters (`10bands_2026_3857_COG.tiff`) for contact times and sun geometry.

Bands 1 to 2 and 6 to 10 of the eclipse raster were cross-checked against the
independently known maximum time; bands 4 to 5 could not be identified and are exposed as
raw values.

## Running it yourself

See [USAGE.md](USAGE.md) for the repository layout, the offline pipeline, how visibility is
computed and validated, and the deployment setup.

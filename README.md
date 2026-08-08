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

# Where to stand for the eclipse of 12 August 2026

Three Spanish cities are mapped street by street: **Zaragoza**, which sees 84 seconds of
totality, and **Linares** and **Jaén**, which sit just outside the path and reach 97.2%
and 96.6% obscuration.

**[Open the map](https://alfonsolrz.github.io/eclipse-viewpoints-spain-2026/)**

At maximum the sun sits only 6° above the horizon at azimuth 284.5° (WNW), so a single
tree or a two-storey wall can hide it from hundreds of metres away. Coarse terrain shadow
maps cannot answer this; the question is decided by buildings and vegetation.

Each city is answered by ray-scanning a 1 m LiDAR surface model over its own block, then
publishing the result as a web map over aerial imagery. The maps rank places you can
actually stand: parks, squares, gardens and other open public space, with a separate tier
for open ground whose access nobody has verified.

| | Block | Eclipse | Candidate zones |
|---|---|---|---|
| Zaragoza | 13 × 13 km | total, 84 s | 263 |
| Jaén | 15 × 12 km | 96.6% partial | 215 |
| Linares | 6 × 5 km | 97.2% partial | 208 |

![The Zaragoza map: the analysed block outlined in dashed amber, clearance shading over
the evaluated ground, and the ranked candidate zones](docs/overview.png)

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

Pick a city from the switcher under the title. Each is a plain URL, so a map can be
bookmarked or shared: `?city=zaragoza`, `?city=linares`, `?city=jaen`.

The viewer shows the sun's direction as a compass dial plus a side profile of its altitude,
and draws a 600 m dashed ray from any clicked point or selected zone. That ray is the line
that has to stay clear of buildings and trees. In Zaragoza the moment selector switches
between C1, C2, maximum, C3 and C4; the two partial cities have no C2 or C3, so those
moments are simply absent.

Five colour scales cover the same data: clearance classes, viridis, red to green, a plain
visible/blocked split, and a shadow-only grey. The imagery, an OpenStreetMap name overlay
and the dashed analysed-area outline can each be toggled independently, so the shadow model
can be read on its own or against street names. The 3D control in the bottom right stands
the buildings up at their measured heights.

The overlay covers all evaluated ground, which is 95% of the block in Zaragoza and 99% in
Jaén; most of the remainder is building rooftops. Reachable public space is drawn solid and
everything else at 45% opacity, so the shadow model is legible citywide while the places
actually worth standing in still stand out.

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

Aerial imagery covers the whole country, so it says nothing about where the analysis
actually holds. Only the LiDAR block has been scanned, and it is much smaller.

The map pulls PNOA imagery live from the [IGN WMTS](https://www.ign.es/wmts/pnoa-ma) and
draws each city's LiDAR footprint as a dashed outline. Imagery continues past the block, so
that dashed edge is what marks where visibility was computed. Outside it the picture is
real but nothing has been analysed.

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

- PNOA LiDAR: 2023 Aragón (NPC02) for Zaragoza, 2024 Andalucía (NPC01) for Jaén and
  Linares. 379 tiles and 3.29 billion points across the three cities.
- PNOA aerial orthophotography, served live by the IGN. The offline analysis was run
  against a mosaic dated 2025-07-14 at 0.25 m.
- CNIG eclipse rasters (`10bands_2026_3857_COG.tiff`) for contact times and sun geometry.

Bands 1 to 2 and 6 to 10 of the eclipse raster were cross-checked against the
independently known maximum time; bands 4 to 5 could not be identified and are exposed as
raw values.

## Running it yourself

See [USAGE.md](USAGE.md) for the repository layout, the offline pipeline, how visibility is
computed and validated, and the deployment setup.

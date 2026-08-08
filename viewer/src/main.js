import maplibregl from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'

// Which city to show. Each one has its own directory under data/, written by the
// pipeline with PNOA_CONFIG pointed at that city's config. Zaragoza is the default so
// existing links keep working.
const CITY = (new URLSearchParams(location.search).get('city') || 'zaragoza')
  .replace(/[^a-z0-9_-]/gi, '')

const DATA = `./data/${CITY}`

// Directory the page lives in, without a trailing slash. MapLibre needs an
// absolute path in a tile template, and on GitHub Pages the app is served from
// /<repo>/ rather than the domain root. Deriving this from `location.pathname`
// directly would break on /<repo>/index.html, which resolves to a directory
// that does not exist; `new URL('.', ...)` strips the filename first.
const BASE = `${new URL('.', location.href).pathname.replace(/\/$/, '')}/data/${CITY}`

// IGN's public WMTS for PNOA orthophotography, the same imagery the offline
// pipeline reads, served in GoogleMapsCompatible (EPSG:3857) tiles. It sends
// `Access-Control-Allow-Origin: *`, so it works from a static host.
const PNOA_WMTS = 'https://www.ign.es/wmts/pnoa-ma' +
  '?service=WMTS&request=GetTile&version=1.0.0' +
  '&format=image/jpeg&layer=OI.OrthoimageCoverage&style=default' +
  '&tilematrixset=GoogleMapsCompatible' +
  '&TileMatrix={z}&TileRow={y}&TileCol={x}'

const CLASS_ORDER = { blocked: 0, fragile: 1, acceptable: 2, good: 3, excellent: 4 }
const CLASS_DEG = { blocked: '0°', fragile: '0 to 0.5°', acceptable: '0.5 to 1°', good: '1 to 2°', excellent: '2°+' }

const state = {
  layers: null,
  meta: null,
  zones: [],
  home: null,
  pickingHome: false,
  activeZone: null,
  markers: [],
  buildings: null,
}

async function loadJSON (name) {
  const r = await fetch(`${DATA}/${name}`)
  if (!r.ok) throw new Error(`${name}: ${r.status}`)
  return r.json()
}

function fmtArea (m2) {
  if (m2 >= 10000) return `${(m2 / 10000).toFixed(1)} ha`
  return `${Math.round(m2).toLocaleString()} m²`
}

function fmtDist (m) {
  return m >= 1000 ? `${(m / 1000).toFixed(1)} km` : `${Math.round(m)} m`
}

function haversine (a, b) {
  const R = 6371000
  const dLat = (b[1] - a[1]) * Math.PI / 180
  const dLon = (b[0] - a[0]) * Math.PI / 180
  const la1 = a[1] * Math.PI / 180
  const la2 = b[1] * Math.PI / 180
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(la1) * Math.cos(la2) * Math.sin(dLon / 2) ** 2
  return 2 * R * Math.asin(Math.sqrt(h))
}

// ---------------------------------------------------------------- colour ramps
//
// MapLibre has no per-value raster recolouring (`raster-color` is a Mapbox GL
// property and is not implemented in MapLibre 5.x), so each scale is baked into
// its own tile set by pipeline/package_tiles.py and switching a scale swaps the
// visible raster layer. Legends below must match those baked colours.

const RAMPS = {
  classes: {
    label: 'Clearance classes',
    legend: [
      ['rgba(30,150,70,.75)', 'Excellent: over 2° of clearance'],
      ['rgba(90,190,90,.75)', 'Good: 1 to 2°'],
      ['rgba(180,200,60,.75)', 'Acceptable: 0.5 to 1°'],
      ['rgba(230,160,40,.8)', 'Fragile: under 0.5°'],
      ['rgba(40,44,52,.85)', 'Blocked'],
    ],
  },
  viridis: {
    label: 'Viridis (continuous)',
    legend: [
      ['rgba(253,231,37,.8)', 'Excellent: over 2°'],
      ['rgba(94,201,98,.8)', 'Good: 1 to 2°'],
      ['rgba(33,145,140,.8)', 'Acceptable: 0.5 to 1°'],
      ['rgba(59,82,139,.85)', 'Fragile: under 0.5°'],
      ['rgba(68,1,84,.9)', 'Blocked'],
    ],
  },
  traffic: {
    label: 'Red → green',
    legend: [
      ['rgba(26,122,51,.8)', 'Excellent: over 2°'],
      ['rgba(127,188,65,.8)', 'Good: 1 to 2°'],
      ['rgba(230,194,41,.8)', 'Acceptable: 0.5 to 1°'],
      ['rgba(217,95,2,.85)', 'Fragile: under 0.5°'],
      ['rgba(139,26,26,.9)', 'Blocked'],
    ],
  },
  binary: {
    label: 'Visible / blocked',
    legend: [
      ['rgba(46,125,50,.8)', 'Sun visible'],
      ['rgba(18,18,18,.9)', 'Blocked'],
    ],
  },
  mono: {
    label: 'Shadow only (grey)',
    legend: [
      ['rgba(0,0,0,.85)', 'In shadow at maximum eclipse'],
      ['transparent', 'Sunlit'],
    ],
    note: 'Shows the physical shadow everywhere, including rooftops.',
  },
}

// ---------------------------------------------------------------- ranking

function weights () {
  return {
    clear: +document.getElementById('w-clear').value / 100,
    near: +document.getElementById('w-near').value / 100,
    high: +document.getElementById('w-high').value / 100,
    area: +document.getElementById('w-area').value / 100,
    veg: +document.getElementById('w-veg').value / 100,
    park: +document.getElementById('w-park').value / 100,
  }
}

function rankZones () {
  const w = weights()
  const onlyRobust = document.getElementById('only-robust').checked

  // Hard constraints come first: a blocked or fragile spot must never outrank a
  // clear one just because it is close to home (plan section 6).
  let pool = state.zones.filter(z => CLASS_ORDER[z.minClearanceClass] >= CLASS_ORDER.acceptable)
  if (onlyRobust) pool = pool.filter(z => z.minClearanceClass === 'excellent')
  if (document.getElementById('hide-parking').checked) {
    pool = pool.filter(z => (z.parkingFraction ?? 0) < 0.5)
  }
  // Zones outside OSM-tagged public space are open, unobstructed ground that nobody has
  // confirmed you may stand on: field edges, riverside tracks, the land around Juslibol.
  // Off by default, because the default answer should be somewhere you can simply go.
  if (!document.getElementById('show-unverified').checked) {
    pool = pool.filter(z => z.accessKind !== 'unverified')
  }

  const areas = pool.map(z => z.areaM2)
  const maxArea = Math.max(1, ...areas)
  const proms = pool.map(z => z.localProminenceM)
  const minProm = Math.min(0, ...proms)
  const maxProm = Math.max(1, ...proms)

  let maxDist = 1
  if (state.home) {
    for (const z of pool) {
      z._dist = haversine(state.home, z.representativePoint)
      if (z._dist > maxDist) maxDist = z._dist
    }
  }

  const sigma = 1500 // metres; proximity decays over roughly a walkable radius

  for (const z of pool) {
    const V = (CLASS_ORDER[z.minClearanceClass] - 2) / 2      // acceptable..excellent -> 0..1
    const A = Math.sqrt(z.areaM2) / Math.sqrt(maxArea)        // sqrt keeps huge zones from dominating
    const H = (z.localProminenceM - minProm) / (maxProm - minProm || 1)
    const P = state.home ? Math.exp(-z._dist / sigma) : 0
    const R = z.vegetationRisk
    const C = z.dataConfidence
    // Car parks are open and usually accessible, but a supermarket apron is a
    // worse place to watch an eclipse than a garden of equal clearance.
    const K = 1 - (z.parkingFraction ?? 0)
    // Somewhere you can definitely stand beats somewhere you probably cannot. Without
    // this the size term alone hands the whole ranking to open country the moment
    // unverified zones are shown: a 19 ha field outscores every park in the city, and
    // the confirmed public spaces vanish from a list that is supposed to recommend them.
    const U = z.accessKind === 'unverified' ? 0 : 1

    z._score =
      w.clear * V +
      w.near * P +
      w.high * H +
      w.area * A +
      w.park * K -
      w.veg * R +
      0.15 * C +
      0.45 * U
  }

  pool.sort((a, b) => b._score - a._score)
  // How many to show is the reader's call. Each city carries a couple of hundred ranked
  // zones, but the filters bind long before the slider does in the smaller towns: with
  // confirmed-public access only, Linares has 8 and Jaén 15, so asking for 50 there
  // yields what exists rather than what was requested.
  const want = +(document.getElementById('n-zones')?.value || 12)
  return pool.slice(0, want)
}

// ---------------------------------------------------------------- rendering

function zoneWhy (z) {
  const bits = []
  if (z.minClearanceClass === 'excellent') bits.push('over 2° of sky clear above the western horizon')
  else if (z.minClearanceClass === 'good') bits.push('1 to 2° of clearance toward the sun')
  else bits.push('marginal clearance, so verify on site')

  if (z.areaM2 > 20000) bits.push('plenty of room')
  if (z.vegetationRisk < 0.05) bits.push('almost no tree cover')
  else if (z.vegetationRisk > 0.3) bits.push('trees nearby may have grown since 2023')

  if (z.mainEdgeObstacle === 'building') bits.push('bounded by buildings')
  else if (z.mainEdgeObstacle === 'vegetation') bits.push('bounded by vegetation')

  if ((z.parkingFraction ?? 0) > 0.5) bits.push('this is a car park, so check it is open and quiet')

  return bits.join(', ')
}

// Zones outside OSM public space get an explicit caveat rather than only a badge. The
// badge alone reads as a category label; someone skimming a ranked list will not infer
// from it that the top result might be a fenced field.
function accessCaveat (z) {
  if (z.accessKind !== 'unverified') return ''
  return `<div class="zone-caveat">Access not confirmed. This is open, clear ground, but it
    is not tagged as public space and may be private land, a farm track or fenced. Check
    before relying on it.</div>`
}

function renderResults () {
  const ranked = rankZones()
  const box = document.getElementById('results')
  document.getElementById('result-count').textContent = `${ranked.length}`

  if (!ranked.length) {
    box.innerHTML = '<p class="hint">No zones match these filters. Try relaxing the clearance requirement.</p>'
    clearMarkers()
    return
  }

  box.innerHTML = ranked.map((z, i) => `
    <div class="zone${state.activeZone === z.id ? ' active' : ''}" data-id="${z.id}">
      <div class="zone-head">
        <div class="zone-rank">${i + 1}</div>
        <div class="zone-title">${z.placeName ? z.placeName : `${fmtArea(z.areaM2)} ${z.surfaceKind || 'open space'}`}${
          z.accessKind === 'unverified' ? '<span class="badge-unverified">unverified access</span>' : ''}</div>
        <div class="zone-grade grade-${z.minClearanceClass}">${CLASS_DEG[z.minClearanceClass]}</div>
      </div>
      <div class="zone-lines">
        ${state.home ? `<div>${fmtDist(z._dist)} from home</div>` : ''}
        <div>${z.groundElevationM.toFixed(0)} m elevation${z.localProminenceM > 0.5 ? ` · ${z.localProminenceM.toFixed(1)} m above surroundings` : ''}</div>
        <div>${Math.round(z.excellentFraction * 100)}% of the area has 2°+ clearance</div>
        <div>${fmtArea(z.areaM2)} · ${z.surfaceKind || 'open space'}</div>
      </div>
      <div class="zone-why">${zoneWhy(z)}</div>
      ${accessCaveat(z)}
    </div>
  `).join('')

  box.querySelectorAll('.zone').forEach(el => {
    el.addEventListener('click', () => {
      const z = state.zones.find(x => x.id === el.dataset.id)
      state.activeZone = z.id
      map.flyTo({ center: z.representativePoint, zoom: 16.5 })
      showZoneDetail(z)
      renderResults()
    })
  })

  drawMarkers(ranked)
}

function clearMarkers () {
  state.markers.forEach(m => m.remove())
  state.markers = []
}

function drawMarkers (ranked) {
  clearMarkers()
  ranked.forEach((z, i) => {
    const el = document.createElement('div')
    el.style.cssText = `
      width:26px;height:26px;border-radius:50%;
      background:${i === 0 ? '#f2b134' : '#3ba85c'};
      color:${i === 0 ? '#1a1204' : '#fff'};
      display:grid;place-items:center;font:700 12px system-ui;
      border:2px solid rgba(255,255,255,.85);cursor:pointer;
      box-shadow:0 2px 8px rgba(0,0,0,.5)`
    el.textContent = String(i + 1)
    el.addEventListener('click', (e) => {
      e.stopPropagation()
      state.activeZone = z.id
      showZoneDetail(z)
      renderResults()
    })
    state.markers.push(new maplibregl.Marker({ element: el }).setLngLat(z.representativePoint).addTo(map))
  })
}

function showZoneDetail (z) {
  drawSunRay(z.representativePoint)
  const box = document.getElementById('inspect')
  const verdictClass = z.minClearanceClass === 'excellent' ? 'yes'
    : z.minClearanceClass === 'good' ? 'yes' : 'maybe'
  const verdictText = z.minClearanceClass === 'excellent' ? 'Sun visible with room to spare'
    : z.minClearanceClass === 'good' ? 'Sun visible' : 'Sun visible, but marginal'

  box.innerHTML = `
    <button class="close" aria-label="Close">×</button>
    <h3>${z.placeName || 'Observation zone'}</h3>
    <div class="verdict ${verdictClass}">${verdictText}</div>
    <div class="kv"><span>Reliable clearance</span><b>${CLASS_DEG[z.minClearanceClass]}</b></div>
    <div class="kv"><span>Worst cell in zone</span><b>${CLASS_DEG[z.worstClearanceClass] ?? '—'}</b></div>
    <div class="kv"><span>Usable area</span><b>${fmtArea(z.areaM2)}</b></div>
    <div class="kv"><span>Ground elevation</span><b>${z.groundElevationM.toFixed(1)} m</b></div>
    <div class="kv"><span>Local prominence</span><b>${z.localProminenceM.toFixed(1)} m</b></div>
    <div class="kv"><span>Limiting obstacle</span><b>${z.mainEdgeObstacle}</b></div>
    <div class="kv"><span>Vegetation risk</span><b>${Math.round(z.vegetationRisk * 100)}%</b></div>
    <div class="kv"><span>LiDAR confidence</span><b>${Math.round(z.dataConfidence * 100)}%</b></div>
    <div class="kv"><span>Surface</span><b>${z.surfaceKind || 'open space'}</b></div>
    <div class="kv"><span>Look toward</span><b>${(() => { const s2 = currentSun(); return s2 ? `${compassLabel(s2.azimuth_deg)} ${s2.azimuth_deg.toFixed(0)}°` : '—' })()}</b></div>
    <div class="kv"><span>Sun altitude</span><b>${(() => { const s2 = currentSun(); return s2 ? `${s2.elevation_deg.toFixed(1)}°` : '—' })()}</b></div>
    <div class="kv"><span>Point density</span><b>${z.pointDensity.toFixed(1)} pts/m²</b></div>
    ${state.home ? `<div class="kv"><span>Distance from home</span><b>${fmtDist(haversine(state.home, z.representativePoint))}</b></div>` : ''}
  `
  box.classList.remove('hidden')
  box.querySelector('.close').addEventListener('click', () => box.classList.add('hidden'))
}

function renderFacts () {
  const m = state.meta
  const c = m.contact_times
  // Not everywhere under this eclipse sees totality. Where C2 and C3 exist the interval
  // between them is the whole event; where they do not, quoting a totality time would be
  // inventing one, so the card leads with obscuration and the partial contacts instead.
  const rows = m.is_total
    ? `
    <div class="fact"><span>Totality begins (C2)</span><b>${c.C2_totality_begin.local}</b></div>
    <div class="fact"><span>Maximum</span><b>${c.maximum_eclipse.local}</b></div>
    <div class="fact"><span>Totality ends (C3)</span><b>${c.C3_totality_end.local}</b></div>
    <div class="fact"><span>Duration</span><b>${m.totality_duration_seconds.toFixed(0)} s</b></div>`
    : `
    <div class="fact"><span>Partial begins (C1)</span><b>${c.C1_partial_begin.local}</b></div>
    <div class="fact"><span>Maximum</span><b>${c.maximum_eclipse.local}</b></div>
    <div class="fact"><span>Partial ends (C4)</span><b>${c.C4_partial_end.local}</b></div>
    <div class="fact"><span>Sun covered at maximum</span><b>${(m.obscuration * 100).toFixed(1)}%</b></div>`

  const lead = m.is_total
    ? `The sun sits only ${m.solar_elevation_deg.toFixed(1)}° above the horizon, so even a
       low wall or a single tree can hide it from hundreds of metres away.`
    : `${m.city || 'This town'} is outside the path of totality, so the sun is never fully
       covered here. It still sits only ${m.solar_elevation_deg.toFixed(1)}° above the
       horizon at maximum, so a low wall or a single tree can hide the last sliver from
       hundreds of metres away.`

  document.getElementById('eclipse-facts').innerHTML = `
    <h2>The eclipse</h2>${rows}
    <div class="fact"><span>Sun elevation</span><b>${m.solar_elevation_deg.toFixed(1)}°</b></div>
    <div class="fact"><span>Sun azimuth</span><b>${m.solar_azimuth_deg.toFixed(1)}° (WNW)</b></div>
    <p class="hint" style="margin-top:10px;margin-bottom:0">${lead}</p>
  `
}

// City picker. The list comes from data/cities.json, written by
// pipeline/write_city_index.py from each city's own metadata, so a city only appears once
// its data is actually complete and adding a fourth needs no change here.
//
// Switching navigates rather than swapping data in place: every source, layer and tile
// pyramid on the map belongs to one city, and tearing all that down correctly is more
// fragile than a reload for something a visitor does once or twice.
async function renderCityPicker () {
  let doc
  try {
    doc = await (await fetch('./data/cities.json')).json()
  } catch {
    return
  }
  const cities = doc.cities || []
  if (cities.length < 2) return

  const nav = document.getElementById('city-picker')
  nav.innerHTML = cities.map(c => {
    const on = c.slug === CITY
    const label = c.isTotal ? 'total eclipse' : `${(c.obscuration * 100).toFixed(1)}% covered`
    return `<a class="city${on ? ' active' : ''}" href="?city=${c.slug}"
      title="${c.name}, ${label}"${on ? ' aria-current="page"' : ''}>${c.name}</a>`
  }).join('')
  nav.classList.remove('hidden')
}

// Page title and heading come from the data, so a second city does not need its own HTML.
function renderIdentity () {
  const m = state.meta
  const name = m.city || 'Eclipse'
  const kind = m.is_total ? 'Total eclipse visibility' : 'Eclipse visibility'
  document.title = `${name} eclipse visibility, 12 August 2026`
  const h1 = document.querySelector('#panel header h1')
  const sub = document.querySelector('#panel header .sub')
  if (h1) h1.textContent = kind
  if (sub) sub.textContent = `${name} · 12 August 2026`
}

function renderLegend () {
  const ramp = RAMPS[state.ramp || 'classes']
  let html = ramp.legend.map(([color, label]) => `
    <div class="legend-row">
      <div class="swatch" style="background:${color}"></div>
      <span>${label}</span>
    </div>`).join('')

  if (ramp.note) html += `<div class="legend-row"><span>${ramp.note}</span></div>`
  if (state.ramp !== 'mono') {
    html += `<div class="legend-row" style="margin-top:8px">
      <div class="swatch" style="background:rgba(30,150,70,.75)"></div>
      <span><b>Solid.</b> Reachable public space (parks, squares, car parks)</span>
    </div>
    <div class="legend-row">
      <div class="swatch" style="background:rgba(30,150,70,.34)"></div>
      <span><b>Faded.</b> The sun reaches this ground, but it is private,
      fenced, farmland or under trees</span>
    </div>
    <div class="legend-row">
      <div class="swatch" style="background:transparent"></div>
      <span><b>Unshaded.</b> Building rooftops, which are not standing positions</span>
    </div>`
  }
  document.getElementById('legend').innerHTML = html
}

// ---------------------------------------------------------------- sun indicator

function compassLabel (az) {
  const names = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
                 'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW']
  return names[Math.round(az / 22.5) % 16]
}

function sunMoments () {
  const c = state.meta.sun_at_contacts
  if (!c) return []
  return [
    ['C2_totality_begin', 'Totality begins (C2)'],
    ['maximum_eclipse', 'Maximum eclipse'],
    ['C3_totality_end', 'Totality ends (C3)'],
    ['C1_partial_begin', 'Partial begins (C1)'],
    ['C4_partial_end', 'Partial ends (C4)'],
  ].filter(([k]) => c[k]).map(([k, label]) => ({ key: k, label, ...c[k] }))
}

function renderSunCard () {
  const moments = sunMoments()
  if (!moments.length) {
    document.getElementById('sun-card').style.display = 'none'
    return
  }

  const sel = document.getElementById('sun-moment')
  sel.innerHTML = moments.map(m =>
    `<option value="${m.key}">${m.label}, ${m.local}</option>`).join('')
  sel.value = state.sunMoment || 'maximum_eclipse'
  state.sunMoment = sel.value

  drawSunDial()
  sel.addEventListener('change', () => {
    state.sunMoment = sel.value
    drawSunDial()
    drawSunRay()
  })
}

function currentSun () {
  const c = state.meta.sun_at_contacts
  if (!c) return null
  return c[state.sunMoment] || c.maximum_eclipse
}

function drawSunDial () {
  const sun = currentSun()
  if (!sun) return
  const az = sun.azimuth_deg
  const el = sun.elevation_deg

  // Compass: 0 deg at top, clockwise. The sun marker sits on the rim at its
  // bearing; the elevation is drawn as a separate side profile because a plan
  // view cannot show altitude.
  const R = 54
  const cx = 62
  const cy = 62
  const rad = (az - 90) * Math.PI / 180
  const sx = cx + R * Math.cos(rad)
  const sy = cy + R * Math.sin(rad)

  const ticks = [['N', 0], ['E', 90], ['S', 180], ['W', 270]].map(([t, a]) => {
    const r = (a - 90) * Math.PI / 180
    return `<text x="${cx + (R - 13) * Math.cos(r)}" y="${cy + (R - 13) * Math.sin(r) + 4}"
      text-anchor="middle" font-size="10" fill="#98a3b6">${t}</text>`
  }).join('')

  // Side profile: horizon line with the sun at its true altitude angle.
  const pw = 128
  const ph = 46
  const horizonY = ph - 10
  const elRad = el * Math.PI / 180
  const px = 22 + 86 * Math.cos(elRad)
  const py = horizonY - 86 * Math.sin(elRad)

  document.getElementById('sun-dial').innerHTML = `
    <div style="display:flex;gap:10px;align-items:center">
      <svg width="124" height="124" viewBox="0 0 124 124" aria-label="Sun bearing">
        <circle cx="${cx}" cy="${cy}" r="${R}" fill="#12161f" stroke="#2b3342"/>
        <circle cx="${cx}" cy="${cy}" r="3" fill="#98a3b6"/>
        ${ticks}
        <line x1="${cx}" y1="${cy}" x2="${sx}" y2="${sy}"
              stroke="#f2b134" stroke-width="2.5" stroke-linecap="round"/>
        <circle cx="${sx}" cy="${sy}" r="7" fill="#f2b134"/>
      </svg>
      <div style="flex:1">
        <svg width="${pw}" height="${ph}" viewBox="0 0 ${pw} ${ph}" aria-label="Sun altitude">
          <line x1="8" y1="${horizonY}" x2="${pw - 4}" y2="${horizonY}"
                stroke="#2b3342" stroke-width="2"/>
          <line x1="22" y1="${horizonY}" x2="${px}" y2="${py}"
                stroke="#f2b134" stroke-width="1.5" stroke-dasharray="3 2"/>
          <circle cx="${px}" cy="${py}" r="5" fill="#f2b134"/>
          <text x="8" y="${ph - 1}" font-size="9" fill="#98a3b6">horizon</text>
        </svg>
      </div>
    </div>
  `

  document.getElementById('sun-readout').innerHTML =
    `Look <b style="color:#e8edf6">${compassLabel(az)}</b> at bearing ${az.toFixed(1)}°,
     only <b style="color:#e8edf6">${el.toFixed(1)}°</b> above the horizon
     (about ${(el / 0.5).toFixed(0)} sun-widths up).`
}

// A ray on the map from the selected zone toward the sun, so it is obvious
// which direction must stay clear of buildings and trees.
function drawSunRay (lngLat) {
  const sun = currentSun()
  if (!sun) return
  const origin = lngLat || state.rayOrigin
  if (!origin) return
  state.rayOrigin = origin

  // 600 m matches the pipeline's max_ray_search_m: obstacles further away than
  // this were not considered when classifying clearance.
  const metres = 600
  const az = sun.azimuth_deg * Math.PI / 180

  // Offsets for equal GROUND distance: latitude degrees are constant length,
  // longitude degrees shrink by cos(lat). This also happens to render at the
  // correct on-screen angle, because Web Mercator stretches the northing by
  // exactly the same 1/cos(lat) factor - the two cancel. Verified: the drawn
  // line measures 284.53 deg on screen against a true bearing of 284.53 deg.
  const lat = origin[1] * Math.PI / 180
  const dLat = (metres * Math.cos(az)) / 111320
  const dLon = (metres * Math.sin(az)) / (111320 * Math.cos(lat))
  const end = [origin[0] + dLon, origin[1] + dLat]

  const data = {
    type: 'FeatureCollection',
    features: [{ type: 'Feature', geometry: { type: 'LineString', coordinates: [origin, end] } }],
  }

  if (map.getSource('sun-ray')) {
    map.getSource('sun-ray').setData(data)
  } else {
    map.addSource('sun-ray', { type: 'geojson', data })
    map.addLayer({
      id: 'sun-ray-halo',
      type: 'line',
      source: 'sun-ray',
      paint: { 'line-color': '#000', 'line-width': 6, 'line-opacity': 0.35, 'line-blur': 2 },
    })
    map.addLayer({
      id: 'sun-ray',
      type: 'line',
      source: 'sun-ray',
      paint: {
        'line-color': '#f2b134',
        'line-width': 3,
        'line-dasharray': [2, 1.2],
      },
    })
  }
}

function renderProvenance () {
  const l = state.layers
  document.getElementById('provenance').innerHTML = `
    <strong>Data sources.</strong>
    Obstacles come from PNOA LiDAR flown in ${l.lidar_year} (~8 pts/m², classes
    2/3/4/5/6). The aerial imagery is served live by the IGN, so it is usually
    newer than the LiDAR and newer than the ${l.orthophoto_date} mosaic the
    analysis was run against. Anything built or planted since the survey is
    invisible to the model.<br><br>
    <strong>Coverage.</strong> ${l.coverage_note}<br><br>
    <strong>Caveats.</strong> Vegetation uses the conservative model, with the
    canopy raised and widened, so it errs toward calling a spot blocked.
    Reachable public space comes from OpenStreetMap, which volunteers maintain
    and which is not authoritative on who may stand where. Temporary obstacles
    such as cranes, stages and parked lorries are not modelled at all.
  `
}

// ---------------------------------------------------------------- map

const map = new maplibregl.Map({
  container: 'map',
  style: {
    version: 8,
    sources: {},
    layers: [{ id: 'bg', type: 'background', paint: { 'background-color': '#0d1017' } }],
  },
  // Placeholder only. init() calls fitBounds with the block's own bounds from
  // layers.json as soon as the data loads, so this is what shows for one frame rather
  // than a hardcoded assumption about which city is being viewed.
  center: [-0.916, 41.663],
  zoom: 13,
  maxZoom: 18,
})
map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'bottom-right')
map.addControl(new maplibregl.ScaleControl({ maxWidth: 120 }), 'bottom-left')

// MapLibre enables rotate and pitch by default. On a flat shadow map they only ever
// produce a tilted plane and a map that is no longer north-up, so they stay off until
// the 3D buildings toggle turns them on together with the extrusions.
map.dragRotate.disable()
map.touchZoomRotate.disableRotation()
map.touchPitch.disable()
map.keyboard.disableRotation()

// Exposed for the automated checks in verify.mjs; harmless in production.
window.__map = map

// Redraw polygon rings as LineStrings before stroking them. A `line` layer over
// a Polygon source is the obvious way to outline it and mostly works, but
// MapLibre clips GeoJSON into tiles, and clipping a polygon inserts edges along
// the tile boundary which the line layer then strokes as well. A LineString has
// no interior to close, so it is simply cut at the seam and the join is
// invisible at every zoom.
function ringsToLines (fc) {
  const lines = []
  for (const f of fc.features || []) {
    const g = f.geometry
    if (!g) continue
    const polys = g.type === 'Polygon' ? [g.coordinates]
      : g.type === 'MultiPolygon' ? g.coordinates
        : []
    for (const rings of polys) {
      for (const ring of rings) {
        lines.push({
          type: 'Feature',
          properties: f.properties || {},
          geometry: { type: 'LineString', coordinates: ring },
        })
      }
    }
  }
  return { type: 'FeatureCollection', features: lines }
}

async function init () {
  const [layers, meta, zonesDoc, coverage] = await Promise.all([
    loadJSON('layers.json'),
    loadJSON('eclipse_metadata.json'),
    loadJSON('candidates.json'),
    loadJSON('laz_coverage.geojson'),
  ])
  state.layers = layers
  state.meta = meta
  state.zones = zonesDoc.zones

  state.ramp = document.getElementById('ramp').value || 'classes'

  renderCityPicker()
  renderIdentity()
  renderFacts()
  renderSunCard()
  renderLegend()
  renderProvenance()

  const b = layers.bounds_wgs84
  map.setMaxBounds([[b[0] - 0.03, b[1] - 0.03], [b[2] + 0.03, b[3] + 0.03]])
  map.fitBounds([[b[0], b[1]], [b[2], b[3]]], { padding: 24, duration: 0 })

  // Orthophoto comes live from the IGN WMTS rather than a baked tile set. The
  // same imagery rendered to local tiles was 671 MB, 86% of the whole site,
  // which does not belong in a git repository. The
  // trade-off is that the WMTS serves the whole country, so imagery no longer
  // stops at the analysed block; `analysis-outline` below draws that edge
  // instead of the clip doing it implicitly.
  map.addSource('rgb', {
    type: 'raster',
    tiles: [PNOA_WMTS],
    tileSize: 256,
    maxzoom: 19,
    attribution: 'Orthophoto © <a href="https://www.ign.es/">IGN</a> (PNOA)',
  })
  map.addLayer({ id: 'rgb', type: 'raster', source: 'rgb', paint: { 'raster-opacity': 1 } })

  // One raster layer per colour scale; only the selected one is visible.
  for (const name of Object.keys(RAMPS)) {
    map.addSource(`ov-${name}`, {
      type: 'raster',
      tiles: [`${BASE}/tiles/${name}/{z}/{x}/{y}.png`],
      tileSize: layers.tile_size,
      minzoom: layers.minzoom,
      maxzoom: layers.maxzoom,
      bounds: b,
    })
    map.addLayer({
      id: `ov-${name}`,
      type: 'raster',
      source: `ov-${name}`,
      layout: { visibility: name === state.ramp ? 'visible' : 'none' },
      paint: { 'raster-opacity': 0.45, 'raster-resampling': 'nearest' },
    })
  }

  // The analysed block. With the orthophoto now coming from a nationwide WMTS,
  // nothing else marks where the LiDAR, and therefore the shadow model, stops.
  // Outside this dashed edge the imagery is real but no visibility was computed.
  map.addSource('analysis-outline', {
    type: 'geojson',
    data: ringsToLines(coverage),
  })
  map.addLayer({
    id: 'analysis-outline-halo',
    type: 'line',
    source: 'analysis-outline',
    paint: { 'line-color': '#000', 'line-width': 5, 'line-opacity': 0.35, 'line-blur': 2 },
  })
  map.addLayer({
    id: 'analysis-outline',
    type: 'line',
    source: 'analysis-outline',
    paint: {
      'line-color': '#f2b134',
      'line-width': 2,
      'line-dasharray': [3, 2],
      'line-opacity': 0.95,
    },
  })

  // OSM label tiles sit on top of everything so names stay readable whatever
  // the overlay is doing. Off by default; the eclipse layers are the point.
  map.addSource('osm-labels', {
    type: 'raster',
    tiles: ['https://basemaps.cartocdn.com/rastertiles/voyager_only_labels/{z}/{x}/{y}{r}.png'],
    tileSize: 256,
    attribution: '© OpenStreetMap contributors © CARTO',
  })
  map.addLayer({
    id: 'osm-labels',
    type: 'raster',
    source: 'osm-labels',
    layout: { visibility: 'none' },
    paint: { 'raster-opacity': 0.95 },
  })

  await addPlaceLabels()
  await addBuildings()

  // Show the sun ray from the centre of the block straight away, so the
  // direction to look is visible before anything is selected.
  drawSunRay(layers.center)

  renderResults()
  wireControls()
}

// Optional 3D buildings: OSM footprints extruded to their LiDAR-measured height.
//
// The first version of this extruded the DSM as a MapLibre heightmap instead. MapLibre
// meshes a heightmap, so every building came out as a rounded mound with the orthophoto
// stretched down its sides. Footprint polygons have real corners, and building_height.tif
// supplies a measured height per footprint, so the two together give straight walls at
// the right height. Trees are no longer 3D, but the shadow overlay still shows exactly
// where they block, which is the part that decides the answer.
//
// buildings.geojson is a local build (`pipeline/export_buildings.py`) and is not
// deployed, so it is usually absent and the control removes itself.
async function addBuildings () {
  try {
    state.buildings = await loadJSON('buildings.geojson')
  } catch {
    state.buildings = null
    return
  }
  document.getElementById('view3d').classList.remove('hidden')

  map.addSource('buildings', { type: 'geojson', data: state.buildings })
  map.addLayer({
    id: 'buildings-3d',
    type: 'fill-extrusion',
    source: 'buildings',
    layout: { visibility: 'none' },
    paint: {
      // Tint by height so the skyline reads at a glance without competing with the
      // clearance ramp, whose greens and ambers mean something specific.
      'fill-extrusion-color': [
        'interpolate', ['linear'], ['get', 'h'],
        3, '#4a5468',
        12, '#6b788f',
        30, '#93a1b8',
        60, '#c2cddd',
      ],
      'fill-extrusion-height': ['get', 'h'],
      'fill-extrusion-base': 0,
      'fill-extrusion-opacity': 0.92,
      'fill-extrusion-vertical-gradient': true,
    },
  })

  // Sky is a root style property in MapLibre, set through setSky. It is not a layer:
  // `{type: 'sky'}` is a Mapbox GL construct and addLayer rejects it outright.
  map.setSky({
    'sky-color': '#1a2233',
    'horizon-color': '#2a3346',
    'fog-color': '#0d1017',
    'sky-horizon-blend': 0.6,
    'horizon-fog-blend': 0.5,
    'fog-ground-blend': 0.4,
  })
}

function set3DEnabled (on) {
  if (!state.buildings) return
  const b3 = document.getElementById('toggle-3d')
  const b2 = document.getElementById('view-2d')
  b3.setAttribute('aria-pressed', on ? 'true' : 'false')
  b2.setAttribute('aria-pressed', on ? 'false' : 'true')
  b3.classList.toggle('active', on)
  b2.classList.toggle('active', !on)
  if (map.getLayer('buildings-3d')) {
    map.setLayoutProperty('buildings-3d', 'visibility', on ? 'visible' : 'none')
  }
  if (on) {
    map.dragRotate.enable()
    map.touchZoomRotate.enableRotation()
    map.touchPitch.enable()
    map.keyboard.enableRotation()
    if (map.getPitch() === 0) map.easeTo({ pitch: 60, duration: 600 })
  } else {
    map.easeTo({ pitch: 0, bearing: 0, duration: 400 })
    map.dragRotate.disable()
    map.touchZoomRotate.disableRotation()
    map.touchPitch.disable()
    map.keyboard.disableRotation()
  }
}

// Named parks and squares from OSM, used to describe zones in words.
async function addPlaceLabels () {
  try {
    const doc = await loadJSON('osm_labels.json')
    state.places = doc.labels || []
  } catch {
    state.places = []
  }
}

function nearestPlace (lonlat, maxMetres = 350) {
  if (!state.places || !state.places.length) return null
  let best = null
  let bestD = Infinity
  for (const p of state.places) {
    const d = haversine(lonlat, p.lonlat)
    if (d < bestD) { bestD = d; best = p }
  }
  return bestD <= maxMetres ? { ...best, distance: bestD } : null
}

function wireControls () {
  const sliders = [
    ['w-clear', 'w-clear-v'], ['w-near', 'w-near-v'], ['w-high', 'w-high-v'],
    ['w-area', 'w-area-v'], ['w-veg', 'w-veg-v'], ['w-park', 'w-park-v'],
  ]
  for (const [id, out] of sliders) {
    const el = document.getElementById(id)
    el.addEventListener('input', () => {
      document.getElementById(out).textContent = el.value
      renderResults()
    })
  }

  document.getElementById('only-robust').addEventListener('change', renderResults)
  document.getElementById('hide-parking').addEventListener('change', renderResults)
  document.getElementById('show-unverified').addEventListener('change', renderResults)
  const nZones = document.getElementById('n-zones')
  nZones.addEventListener('input', () => {
    document.getElementById('n-zones-v').textContent = nZones.value
    renderResults()
  })

  const op = document.getElementById('op')
  op.addEventListener('input', () => {
    document.getElementById('op-v').textContent = op.value
    for (const name of Object.keys(RAMPS)) {
      if (map.getLayer(`ov-${name}`)) map.setPaintProperty(`ov-${name}`, 'raster-opacity', +op.value / 100)
    }
  })

  document.getElementById('show-clearance').addEventListener('change', applyLayerVisibility)
  document.getElementById('show-rgb').addEventListener('change', (e) => {
    if (map.getLayer('rgb')) {
      map.setLayoutProperty('rgb', 'visibility', e.target.checked ? 'visible' : 'none')
    }
  })
  document.getElementById('show-labels').addEventListener('change', (e) => {
    if (map.getLayer('osm-labels')) {
      map.setLayoutProperty('osm-labels', 'visibility', e.target.checked ? 'visible' : 'none')
    }
  })
  document.getElementById('show-outline').addEventListener('change', (e) => {
    const v = e.target.checked ? 'visible' : 'none'
    for (const id of ['analysis-outline', 'analysis-outline-halo']) {
      if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', v)
    }
  })

  // The 3D control lives over the map rather than in the sidebar: it changes how the
  // map is being looked at, not what is drawn on it. addBuildings unhides it, so when
  // there is no local buildings.geojson it stays hidden and nothing else is needed.
  document.getElementById('toggle-3d').addEventListener('click', () => set3DEnabled(true))
  document.getElementById('view-2d').addEventListener('click', () => set3DEnabled(false))
  document.getElementById('ramp').addEventListener('change', (e) => {
    state.ramp = e.target.value
    applyLayerVisibility()
    renderLegend()
  })

  const pick = document.getElementById('pick-home')
  pick.addEventListener('click', () => {
    state.pickingHome = !state.pickingHome
    pick.classList.toggle('armed', state.pickingHome)
    pick.textContent = state.pickingHome ? 'Click the map…' : 'Set home from map'
    map.getCanvas().style.cursor = state.pickingHome ? 'crosshair' : ''
  })

  document.getElementById('clear-home').addEventListener('click', () => {
    state.home = null
    if (state.homeMarker) { state.homeMarker.remove(); state.homeMarker = null }
    document.getElementById('home-readout').textContent = 'No home set, so zones are ranked without distance.'
    renderResults()
  })

  map.on('click', (e) => {
    if (state.pickingHome) {
      setHome([e.lngLat.lng, e.lngLat.lat])
      state.pickingHome = false
      pick.classList.remove('armed')
      pick.textContent = 'Set home from map'
      map.getCanvas().style.cursor = ''
      return
    }
    // Clicking anywhere shows which way the sun will be from that spot.
    drawSunRay([e.lngLat.lng, e.lngLat.lat])
  })
}

function applyLayerVisibility () {
  const on = document.getElementById('show-clearance').checked
  for (const name of Object.keys(RAMPS)) {
    const id = `ov-${name}`
    if (!map.getLayer(id)) continue
    map.setLayoutProperty(id, 'visibility', on && name === state.ramp ? 'visible' : 'none')
  }
}

function setHome (lngLat) {
  state.home = lngLat
  if (state.homeMarker) state.homeMarker.remove()

  const el = document.createElement('div')
  el.style.cssText = `width:18px;height:18px;border-radius:50%;background:#4a9eff;
    border:3px solid #fff;box-shadow:0 2px 8px rgba(0,0,0,.6)`
  state.homeMarker = new maplibregl.Marker({ element: el }).setLngLat(lngLat).addTo(map)

  document.getElementById('home-readout').textContent =
    `Home at ${lngLat[1].toFixed(5)}, ${lngLat[0].toFixed(5)} (kept in your browser only).`
  renderResults()
}

init().catch(err => {
  document.getElementById('results').innerHTML =
    `<p class="hint">Could not load data: ${err.message}</p>`
  console.error(err)
})

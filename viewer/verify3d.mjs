// Browser check for the optional 3D terrain view. Run with the dev server up:
//   node verify3d.mjs <screenshot-dir>
// Skips cleanly when no local terrain build is present, which is the deployed case.
import { chromium } from 'playwright'

const OUT = process.argv[2] || '.'
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1500, height: 950 } })

const errors = []
const failed = []
page.on('pageerror', e => errors.push(e.message))
page.on('console', m => { if (m.type() === 'error') errors.push(m.text()) })
page.on('response', r => { if (r.status() >= 400) failed.push(`${r.status()} ${r.url()}`) })

await page.goto('http://localhost:5173/', { waitUntil: 'networkidle' })
await page.waitForTimeout(5000)

const results = {}
results.terrainControlPresent = await page.$('#show-terrain') !== null
if (!results.terrainControlPresent) {
  console.log(JSON.stringify({ ...results, note: 'no local terrain build; controls removed' }, null, 2))
  await browser.close()
  process.exit(0)
}

// --- turn it on ------------------------------------------------------------
await page.check('#show-terrain')
await page.waitForTimeout(6000)

results.afterEnable = await page.evaluate(() => {
  const m = window.__map
  const t = m.getTerrain()
  return {
    terrainSet: !!t,
    exaggeration: t ? t.exaggeration : null,
    source: t ? t.source : null,
    pitch: Math.round(m.getPitch()),
    dragRotateEnabled: m.dragRotate.isEnabled(),
    skySet: !!m.getSky(),
    demSourceType: m.getSource('terrain-dem')?.type ?? null,
    demTileSize: m.getSource('terrain-dem')?.tileSize ?? null,
  }
})

// Elevation must actually vary, otherwise the DEM decoded flat and the mesh is a plane.
results.elevationProbe = await page.evaluate(() => {
  const m = window.__map
  const pts = [[-0.916, 41.663], [-0.90, 41.67], [-0.94, 41.65], [-0.88, 41.68]]
  const el = pts.map(p => m.queryTerrainElevation({ lng: p[0], lat: p[1] }))
    .map(v => (v == null ? null : +v.toFixed(2)))
  const real = el.filter(v => typeof v === 'number')
  return { samples: el, min: Math.min(...real), max: Math.max(...real), varies: new Set(real).size > 1 }
})

await page.screenshot({ path: `${OUT}/terrain-on.png` })

// tilt and look WNW toward the sun, which is what the feature is for
await page.evaluate(() => window.__map.easeTo({ pitch: 70, bearing: -75, zoom: 15.5, duration: 0 }))
await page.waitForTimeout(5000)
await page.screenshot({ path: `${OUT}/terrain-tilted.png` })

// exaggeration slider
await page.$eval('#exaggeration', el => { el.value = 3; el.dispatchEvent(new Event('input')) })
await page.waitForTimeout(2500)
results.exaggerationAfterSlider = await page.evaluate(() => window.__map.getTerrain()?.exaggeration)
await page.screenshot({ path: `${OUT}/terrain-exag3.png` })

// --- turn it off -----------------------------------------------------------
await page.uncheck('#show-terrain')
await page.waitForTimeout(2500)
results.afterDisable = await page.evaluate(() => {
  const m = window.__map
  return {
    terrainCleared: m.getTerrain() == null,
    pitch: Math.round(m.getPitch()),
    dragRotateEnabled: m.dragRotate.isEnabled(),
  }
})

results.consoleErrors = errors.slice(0, 10)
results.failedRequests = failed.slice(0, 10)
console.log(JSON.stringify(results, null, 2))
await browser.close()

// Browser check for the optional 3D buildings view. Run with the dev server up:
//   node verify3d.mjs <screenshot-dir>
// Skips cleanly when there is no local buildings.geojson, which is the deployed case.
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
results.controlVisible = await page.isVisible('#view3d')
if (!results.controlVisible) {
  console.log(JSON.stringify({ ...results, note: 'no local buildings build; control hidden' }, null, 2))
  await browser.close(); process.exit(0)
}
// the control must sit over the map, clear of MapLibre's own bottom-right stack
results.controlBox = await page.locator('#view3d').boundingBox()
results.mapBox = await page.locator('#map').boundingBox()

await page.click('#toggle-3d')
await page.waitForTimeout(4500)
results.afterEnable = await page.evaluate(() => {
  const m = window.__map
  return {
    pressed: document.getElementById('toggle-3d').getAttribute('aria-pressed'),
    layerVisible: m.getLayoutProperty('buildings-3d', 'visibility'),
    pitch: Math.round(m.getPitch()),
    dragRotate: m.dragRotate.isEnabled(),
    skySet: !!m.getSky(),
    featureCount: m.querySourceFeatures('buildings').length,
  }
})
await page.evaluate(() => window.__map.easeTo({ pitch: 65, bearing: -75, zoom: 16, duration: 0 }))
await page.waitForTimeout(4000)
await page.screenshot({ path: `${OUT}/b3d-tilted.png` })

// The control is a 2D/3D segmented switch, not a toggle: clicking #toggle-3d again
// keeps 3D on. Leaving 3D means clicking the 2D segment.
await page.click('#view-2d')
await page.waitForTimeout(2500)
results.afterDisable = await page.evaluate(() => ({
  pressed: document.getElementById('toggle-3d').getAttribute('aria-pressed'),
  layerVisible: window.__map.getLayoutProperty('buildings-3d', 'visibility'),
  pitch: Math.round(window.__map.getPitch()),
  dragRotate: window.__map.dragRotate.isEnabled(),
}))
results.consoleErrors = errors.slice(0, 8)
results.failedRequests = failed.slice(0, 8)
console.log(JSON.stringify(results, null, 2))
await browser.close()

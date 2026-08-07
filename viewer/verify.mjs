// Browser verification for the viewer: colour scales, layer toggles, ranking.
// Run with the dev server up:  node verify.mjs <screenshot-dir>
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

// --- ranked results are real named places -----------------------------------
results.zoneCount = await page.$$eval('.zone', els => els.length)
results.topTitles = await page.$$eval('.zone-title', els => els.slice(0, 6).map(e => e.textContent.trim()))

// --- colour scales ----------------------------------------------------------
// Each scale must actually change which raster layer is visible.
results.ramps = {}
for (const ramp of ['classes', 'viridis', 'traffic', 'binary', 'mono']) {
  await page.selectOption('#ramp', ramp)
  await page.waitForTimeout(1800)
  const visible = await page.evaluate(() => {
    const m = window.__map
    if (!m) return null
    return ['classes', 'viridis', 'traffic', 'binary', 'mono']
      .filter(n => m.getLayer(`ov-${n}`) && m.getLayoutProperty(`ov-${n}`, 'visibility') !== 'none')
  })
  const legendRows = await page.$$eval('.legend-row', els => els.length)
  results.ramps[ramp] = { visible, legendRows }
}

await page.selectOption('#ramp', 'classes')
await page.waitForTimeout(1200)

// --- RGB toggle -------------------------------------------------------------
await page.uncheck('#show-rgb')
await page.waitForTimeout(1500)
results.rgbHiddenShot = true
await page.screenshot({ path: `${OUT}/no-rgb.png` })
const rgbVis = await page.evaluate(() =>
  window.__map ? window.__map.getLayoutProperty('rgb', 'visibility') : null)
results.rgbVisibilityWhenUnchecked = rgbVis
await page.check('#show-rgb')
await page.waitForTimeout(1200)

// --- analysed-area outline --------------------------------------------------
// The orthophoto now comes from a nationwide WMTS, so this dashed polygon is
// the only thing marking where the shadow model stops. If it silently fails to
// load, the map claims coverage it does not have.
// querySourceFeatures reads what the source actually parsed and tiled. Reading
// source._data instead reports the URL string for a url-backed GeoJSON source,
// which looks like "0 features" whether or not the fetch succeeded.
results.outline = await page.evaluate(() => {
  const m = window.__map
  if (!m) return null
  return {
    layerPresent: !!m.getLayer('analysis-outline'),
    visible: m.getLayer('analysis-outline')
      ? m.getLayoutProperty('analysis-outline', 'visibility') !== 'none'
      : null,
    sourceFeatures: m.querySourceFeatures('analysis-outline').length,
    renderedFeatures: m.queryRenderedFeatures({ layers: ['analysis-outline'] }).length,
  }
})
await page.uncheck('#show-outline')
await page.waitForTimeout(600)
results.outlineHiddenWhenUnchecked = await page.evaluate(() =>
  window.__map ? window.__map.getLayoutProperty('analysis-outline', 'visibility') : null)
await page.check('#show-outline')
await page.waitForTimeout(600)

// --- OSM labels -------------------------------------------------------------
await page.check('#show-labels')
await page.waitForTimeout(2500)
const labelVis = await page.evaluate(() =>
  window.__map ? window.__map.getLayoutProperty('osm-labels', 'visibility') : null)
results.labelVisibilityWhenChecked = labelVis
await page.screenshot({ path: `${OUT}/labels.png` })

// --- car park filter --------------------------------------------------------
// Drive the "prefer parks" weight to 0 first, otherwise the default weighting
// already pushes every car park out of the top 12 and the filter has nothing
// left to remove - which looks identical to a broken filter.
await page.$eval('#w-park', el => { el.value = 0; el.dispatchEvent(new Event('input')) })
await page.waitForTimeout(600)
const before = await page.$$eval('.zone-lines', els =>
  els.filter(e => /car park/.test(e.textContent)).length)
await page.check('#hide-parking')
await page.waitForTimeout(700)
const after = await page.$$eval('.zone-lines', els =>
  els.filter(e => /car park/.test(e.textContent)).length)
results.parkingFilter = { carParksShownBefore: before, carParksShownAfter: after }
await page.uncheck('#hide-parking')
await page.$eval('#w-park', el => { el.value = 40; el.dispatchEvent(new Event('input')) })

await page.waitForTimeout(500)
await page.screenshot({ path: `${OUT}/final.png` })

results.consoleErrors = errors.slice(0, 10)
results.failedRequests = failed.slice(0, 10)

console.log(JSON.stringify(results, null, 2))
await browser.close()

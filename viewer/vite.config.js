import fs from 'node:fs'
import path from 'node:path'
import { defineConfig } from 'vite'

// Tiles that contain no data are never written, but MapLibre still requests
// them. Vite's SPA fallback answers those with index.html and HTTP 200, so the
// browser tries to decode HTML as a PNG and logs
// "InvalidStateError: The source image could not be decoded".
// Answer missing tiles with a real 404 instead, which MapLibre handles as
// "empty tile" without complaint. Static hosts with SPA rewrites need the same
// treatment - exclude /data/tiles/ from the rewrite rule.
function tileNotFound () {
  return {
    name: 'tile-404',
    configureServer (server) {
      server.middlewares.use((req, res, next) => {
        // Tiles live under /data/<city>/tiles/ now that the viewer serves more than one
        // city, so match the segment rather than a fixed prefix. Missing the match sends
        // absent tiles through Vite's SPA fallback, which answers with index.html and a
        // 200, and the browser then tries to decode HTML as a PNG.
        if (req.url && /^\/data\/[^/]+\/tiles\//.test(req.url) && req.url.endsWith('.png')) {
          const file = path.join(process.cwd(), 'public', req.url.split('?')[0])
          if (!fs.existsSync(file)) {
            res.statusCode = 404
            res.end()
            return
          }
        }
        next()
      })
    },
  }
}

export default defineConfig({
  base: './',
  plugins: [tileNotFound()],
  server: { port: 5173 },
})

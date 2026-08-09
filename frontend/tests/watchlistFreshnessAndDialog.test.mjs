import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const currentDir = path.dirname(fileURLToPath(import.meta.url))
const appSource = fs.readFileSync(path.resolve(currentDir, '../src/App.vue'), 'utf8')
const apiSource = fs.readFileSync(path.resolve(currentDir, '../src/api.ts'), 'utf8')
const watchlistSource = fs.readFileSync(path.resolve(currentDir, '../src/views/WatchlistView.vue'), 'utf8')

test('entering the watchlist clears the in-memory snapshot and requests fresh data', () => {
  assert.match(appSource, /async function refreshWatchlist/)
  assert.match(appSource, /watchlist\.value = \[\]/)
  assert.match(appSource, /if \(page === 'watchlist'\) void refreshWatchlist\(\)/)
  assert.match(appSource, /:loading="watchlistLoading"/)
  assert.match(appSource, /@refresh="refreshWatchlist"/)
  assert.match(apiSource, /watchlist: \(\) => request<WatchlistItem\[]>\('\/api\/watchlist', \{ cache: 'no-store' \}\)/)
})

test('watchlist deletion uses an in-app accessible confirmation dialog', () => {
  assert.doesNotMatch(watchlistSource, /window\.confirm/)
  assert.match(watchlistSource, /role="dialog"/)
  assert.match(watchlistSource, /aria-modal="true"/)
  assert.match(watchlistSource, /async function confirmDelete/)
  assert.match(watchlistSource, /@keydown\.esc="cancelDelete"/)
  assert.match(watchlistSource, /正在读取最新自选/)
})

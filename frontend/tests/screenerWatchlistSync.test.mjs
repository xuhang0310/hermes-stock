import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const appPath = fileURLToPath(new URL('../src/App.vue', import.meta.url))
const screenerPath = fileURLToPath(new URL('../src/views/ScreenerView.vue', import.meta.url))

test('AI screener immediately publishes the watchlist item returned by the API', async () => {
  const source = await readFile(screenerPath, 'utf8')
  const addHandler = source.match(/async function addResultToWatchlist[\s\S]*?\n}/)?.[0]

  assert.ok(addHandler, 'AI screener add handler should exist')
  assert.match(addHandler, /const addedItem = await api\.addWatchlist/)
  assert.match(addHandler, /emit\('watchlistAdded', addedItem\)/)
})

test('app-level watchlist state consumes screener additions without a page reload', async () => {
  const source = await readFile(appPath, 'utf8')
  const applyHandler = source.match(/function applyWatchlistItem[\s\S]*?\n}/)?.[0]

  assert.ok(applyHandler, 'app-level watchlist update handler should exist')
  assert.match(applyHandler, /watchlist\.value = \[/)
  assert.match(applyHandler, /existing\.id !== item\.id/)
  assert.match(source, /@watchlist-added="applyWatchlistItem"/)
})

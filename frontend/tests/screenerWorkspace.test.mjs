import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const viewPath = fileURLToPath(new URL('../src/views/ScreenerView.vue', import.meta.url))

async function readScreenerView() {
  return readFile(viewPath, 'utf8')
}

test('screener results remain actionable after a successful run', async () => {
  const source = await readScreenerView()

  assert.match(source, /const sortedResultItems = computed/)
  assert.match(source, /v-for="item in sortedResultItems"/)
  assert.match(source, /async function addResultToWatchlist/)
  assert.match(source, /await api\.addWatchlist/)
  assert.match(source, /group_name: 'AI选股'/)
  assert.match(source, /'加入自选'/)
})

test('empty results offer a concrete way to relax data-sensitive rules', async () => {
  const source = await readScreenerView()
  const relaxRules = source.match(/function relaxDataSensitiveRules[\s\S]*?\n}/)?.[0]

  assert.ok(relaxRules, 'relaxation handler should exist')
  assert.match(relaxRules, /volumeRatioMin\.value = ''/)
  assert.match(relaxRules, /aboveMa\.value = ''/)
  assert.match(relaxRules, /result\.value = null/)
  assert.match(source, /@click="relaxDataSensitiveRules"/)
})

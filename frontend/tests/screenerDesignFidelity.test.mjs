import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const viewPath = fileURLToPath(new URL('../src/views/ScreenerView.vue', import.meta.url))

test('strategy results keep the approved Top20 scoring model', async () => {
  const source = await readFile(viewPath, 'utf8')

  assert.match(source, /const resultLimit = ref\(20\)/)
  assert.match(source, /const scoreValueWeight = ref\(40\)/)
  assert.match(source, /const scoreMomentumWeight = ref\(40\)/)
  assert.match(source, /const scoreLiquidityWeight = ref\(20\)/)
  assert.match(source, /limit: resultLimit\.value/)
  assert.match(source, /function weightedContribution/)
})

test('ranking toolbar controls real result state', async () => {
  const source = await readFile(viewPath, 'utf8')

  assert.match(source, /const resultSearch = ref\(''\)/)
  assert.match(source, /const visibleResultColumns = ref<ResultColumn\[\]>/)
  assert.match(source, /sorted\.filter\(\(item\) => `\$\{item\.symbol\} \$\{item\.name\}`/)
  assert.match(source, /function toggleResultColumn/)
  assert.match(source, /class="sc-result-toolbar"/)
  assert.match(source, /class="sc-column-menu"/)
  assert.match(source, /aria-label="搜索筛选结果"/)
  assert.match(source, /v-if="isResultColumnVisible\('risks'\)"/)
})

test('ranking layout preserves selectable rows and compact semantic tags', async () => {
  const source = await readFile(viewPath, 'utf8')

  assert.match(source, /data-testid="screener-select-page"/)
  assert.match(source, /data-testid="screener-result-checkbox"/)
  assert.match(source, /function reasonTag/)
  assert.match(source, /\.sc-logic-card li i\.is-rule/)
  assert.match(source, /\.sc-result-toolbar/)
  assert.match(source, /\.sc-ranking-table td \{ padding: 13px 8px;/)
})

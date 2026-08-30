import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const viewPath = fileURLToPath(new URL('../src/views/ScreenerView.vue', import.meta.url))
const typesPath = fileURLToPath(new URL('../src/types.ts', import.meta.url))

test('screener keeps TuShare factor condition state and plumbs it through the query', async () => {
  const source = await readFile(viewPath, 'utf8')

  assert.match(source, /const factorName = ref\(''\)/)
  assert.match(source, /const factorMin = ref\(''\)/)
  assert.match(source, /const factorMax = ref\(''\)/)
  assert.match(source, /const factorOptions = ref<TushareFactorMeta\[\]>\(\[\]\)/)
  assert.match(source, /factor_name: factorName\.value \|\| undefined/)
  assert.match(source, /factor_min: toNumber\(factorMin\.value\)/)
  assert.match(source, /factor_max: toNumber\(factorMax\.value\)/)
  assert.match(source, /factorName\.value = query\.factor_name \?\? ''/)
  assert.match(source, /factorMin\.value = query\.factor_min != null \? String\(query\.factor_min\) : ''/)
})

test('factor condition shows up as a builder chip and loads catalog options', async () => {
  const source = await readFile(viewPath, 'utf8')

  assert.match(source, /addRangeChip\(chips, `因子 \$\{query\.factor_name\}`, query\.factor_min, query\.factor_max\)/)
  assert.match(source, /async function loadFactorOptions\(\)/)
  assert.match(source, /await api\.tushareFactorCatalog\(\)/)
  assert.match(source, /function factorOptionLabel\(item: TushareFactorMeta\): string/)
  assert.match(source, /loadFactorOptions\(\)\]/)
})

test('rule editor dialog exposes the paid TuShare factor section', async () => {
  const source = await readFile(viewPath, 'utf8')

  assert.match(source, /<h4>TuShare 因子（付费库）<\/h4>/)
  assert.match(source, /<select v-model="factorName" class="sc-input">/)
  assert.match(source, /:disabled="!option\.cached_rows"/)
  assert.match(source, /factorOptionLabel\(option\)/)
  assert.match(source, /v-model="factorMin" type="number"/)
  assert.match(source, /v-model="factorMax" type="number"/)
})

test('result table renders the factor value column and snapshot date only when present', async () => {
  const source = await readFile(viewPath, 'utf8')

  assert.match(source, /const showFactorColumn = computed\(\(\) => \(result\.value\?\.items \|\| \[\]\)\.some\(\(item\) => item\.factor_value != null\)\)/)
  assert.match(source, /<th v-if="showFactorColumn">因子值<\/th>/)
  assert.match(source, /<td v-if="showFactorColumn">/)
  assert.match(source, /因子值截至 \{\{ fmtDate\(result\.factor_as_of\) \}\}/)
})

test('types carry the factor condition fields', async () => {
  const source = await readFile(typesPath, 'utf8')

  assert.match(source, /factor_name\?: string/)
  assert.match(source, /factor_min\?: number/)
  assert.match(source, /factor_max\?: number/)
  assert.match(source, /factor_value\?: number \| null/)
  assert.match(source, /factor_as_of\?: string \| null/)
})

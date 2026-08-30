import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const viewPath = fileURLToPath(new URL('../src/views/FactorAnalysisView.vue', import.meta.url))
const apiPath = fileURLToPath(new URL('../src/api.ts', import.meta.url))
const typesPath = fileURLToPath(new URL('../src/types.ts', import.meta.url))

test('tushare value sync auto-chunks long windows with offset resume and live progress', async () => {
  const source = await readFile(viewPath, 'utf8')

  assert.match(source, /const syncProgress = ref\(''\)/)
  assert.match(source, /offset = res\.offset_next/)
  assert.match(source, /if \(res\.has_more\) \{/)
  assert.match(source, /同步中 \$\{offset\}\/\$\{planned\}/)
  assert.match(source, /v-else-if="syncProgress" class="fa-sync-ok"/)
  assert.match(source, /长区间自动分片续传（每片 ≤30 次请求，无需手动分段）/)
})

test('api client and types carry the chunking cursor fields', async () => {
  const apiSource = await readFile(apiPath, 'utf8')
  const typesSource = await readFile(typesPath, 'utf8')

  assert.match(apiSource, /offset\?: number \| null/)
  assert.match(typesSource, /offset_next: number/)
  assert.match(typesSource, /has_more: boolean/)
  assert.match(typesSource, /halted: boolean/)
})

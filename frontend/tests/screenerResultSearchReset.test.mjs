import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const viewPath = fileURLToPath(new URL('../src/views/ScreenerView.vue', import.meta.url))

test('closing ranking search clears its hidden filter', async () => {
  const source = await readFile(viewPath, 'utf8')
  const handler = source.match(/function toggleResultSearch\(\) \{[\s\S]*?\n\}/)?.[0]

  assert.ok(handler, 'result search toggle should have an explicit handler')
  assert.match(handler, /if \(resultSearchOpen\.value\)/)
  assert.match(handler, /resultSearch\.value = ''/)
  assert.match(handler, /resultPage\.value = 1/)
  assert.match(source, /aria-label="搜索结果" @click="toggleResultSearch"/)
})

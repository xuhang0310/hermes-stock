import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const viewPath = fileURLToPath(new URL('../src/views/KnowledgeSearchView.vue', import.meta.url))

test('knowledge search workspace fills ultrawide content without fixed outer gutters', async () => {
  const source = await readFile(viewPath, 'utf8')
  const centerStyles = source.slice(
    source.indexOf('.knowledge-center {'),
    source.indexOf('.knowledge-session-rail,', source.indexOf('.knowledge-center {'))
  )

  assert.match(centerStyles, /width:\s*100%/)
  assert.match(centerStyles, /grid-template-columns:\s*286px minmax\(0, 1fr\)/)
  assert.doesNotMatch(centerStyles, /max-width|margin:\s*0 auto/)
})

import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const viewPath = fileURLToPath(new URL('../src/components/PositionsWorkbench.vue', import.meta.url))
const stylesPath = fileURLToPath(new URL('../src/styles.css', import.meta.url))

test('position identity columns use the restrained black-gold palette', async () => {
  const [view, styles] = await Promise.all([
    readFile(viewPath, 'utf8'),
    readFile(stylesPath, 'utf8')
  ])

  assert.match(view, /wb-broker-label/)
  assert.match(view, /wb-symbol-link--\$\{kind\}/)
  assert.match(view, /xueqiuAnchor\(position, position\.symbol, 'symbol'\)/)
  assert.match(view, /xueqiuAnchor\(position, position\.name, 'name'\)/)

  const identityStyles = styles.slice(
    styles.indexOf('.wb-broker-label'),
    styles.indexOf('.required', styles.indexOf('.wb-broker-label'))
  )
  assert.match(identityStyles, /\.wb-symbol-link--symbol/)
  assert.match(identityStyles, /\.wb-symbol-link--name/)
  assert.match(identityStyles, /var\(--gold-2\)/)
  assert.doesNotMatch(identityStyles, /var\(--pink\)|#ff8fd0|242, 84, 184/)
})

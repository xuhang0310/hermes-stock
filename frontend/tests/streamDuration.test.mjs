import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

const frontendRoot = fileURLToPath(new URL('../', import.meta.url))

async function loadDurationHelpers() {
  const server = await createServer({
    root: frontendRoot,
    appType: 'custom',
    server: { middlewareMode: true, hmr: false }
  })
  try {
    return await server.ssrLoadModule('/src/utils/streamDuration.ts')
  } finally {
    await server.close()
  }
}

test('elapsed stream duration is monotonic and rounded', async () => {
  const { elapsedStreamMilliseconds } = await loadDurationHelpers()
  assert.equal(elapsedStreamMilliseconds(1000, 2450.4), 1450)
  assert.equal(elapsedStreamMilliseconds(2000, 1500), 0)
  assert.equal(elapsedStreamMilliseconds(undefined, 1500), undefined)
})

test('stream duration uses compact Chinese labels', async () => {
  const { formatStreamDuration } = await loadDurationHelpers()
  assert.equal(formatStreamDuration(450), '0.5 秒')
  assert.equal(formatStreamDuration(9400), '9.4 秒')
  assert.equal(formatStreamDuration(42_500), '43 秒')
  assert.equal(formatStreamDuration(125_000), '2 分 5 秒')
  assert.equal(formatStreamDuration(3_661_000), '1 小时 1 分')
})

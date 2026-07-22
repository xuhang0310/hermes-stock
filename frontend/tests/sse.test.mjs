import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

const frontendRoot = fileURLToPath(new URL('../', import.meta.url))

async function loadReader() {
  const server = await createServer({
    root: frontendRoot,
    appType: 'custom',
    server: { middlewareMode: true, hmr: false }
  })
  try {
    return await server.ssrLoadModule('/src/utils/sse.ts')
  } finally {
    await server.close()
  }
}

test('terminal event releases the caller even when stream cancellation stalls', async () => {
  const { readSseStream } = await loadReader()
  const events = []
  const body = new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode('data: {"type":"done"}\n\n'))
    },
    cancel() {
      return new Promise(() => {})
    }
  })

  const outcome = await Promise.race([
    readSseStream(body, (event) => events.push(event)).then(() => 'resolved'),
    new Promise((resolve) => setTimeout(() => resolve('timed-out'), 100))
  ])

  assert.equal(outcome, 'resolved')
  assert.deepEqual(events, [{ type: 'done' }])
})

test('final event is parsed when the server closes without a blank line', async () => {
  const { readSseStream } = await loadReader()
  const events = []
  const body = new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode('data: {"type":"error","content":"closed"}'))
      controller.close()
    }
  })

  await readSseStream(body, (event) => events.push(event))

  assert.deepEqual(events, [{ type: 'error', content: 'closed' }])
})

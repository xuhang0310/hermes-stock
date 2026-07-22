import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

const frontendRoot = fileURLToPath(new URL('../', import.meta.url))

async function loadApi() {
  const server = await createServer({
    root: frontendRoot,
    appType: 'custom',
    optimizeDeps: { noDiscovery: true },
    server: { middlewareMode: true, hmr: false }
  })
  try {
    return await server.ssrLoadModule('/src/api.ts')
  } finally {
    await server.close()
  }
}

test('durable Agent transport creates once and resumes from the last event id', async () => {
  const originalLocalStorage = globalThis.localStorage
  const originalWindow = globalThis.window
  globalThis.localStorage = {
    getItem: () => null,
    setItem: () => {},
    removeItem: () => {}
  }
  globalThis.window = globalThis
  const { api } = await loadApi()
  const originalFetch = globalThis.fetch
  const calls = []
  let eventRequests = 0
  globalThis.fetch = async (url, options = {}) => {
    const requestUrl = String(url)
    calls.push({ url: requestUrl, options })
    if (requestUrl === '/api/agent/runs' && options.method === 'POST') {
      return new Response(JSON.stringify({
        run_id: 'run-1',
        session_id: 9,
        engine: 'agentscope',
        status: 'queued',
        created: true
      }), { status: 202, headers: { 'Content-Type': 'application/json' } })
    }
    if (requestUrl.startsWith('/api/agent/runs/run-1/events')) {
      eventRequests += 1
      if (eventRequests === 1) {
        return new Response('id: 1\nevent: content_delta\ndata: {"type":"content_delta","seq":1,"content":"A"}\n\n')
      }
      return new Response('id: 2\nevent: done\ndata: {"type":"done","seq":2,"status":"completed"}\n\n')
    }
    if (requestUrl === '/api/agent/runs/run-1') {
      return new Response(JSON.stringify({
        run_id: 'run-1',
        session_id: 9,
        engine: 'agentscope',
        status: 'completed',
        created: true,
        last_event_seq: 2,
        attempts: 1,
        max_attempts: 3,
        cancel_requested: false
      }), { headers: { 'Content-Type': 'application/json' } })
    }
    throw new Error(`Unexpected request: ${requestUrl}`)
  }

  try {
    const events = []
    await api.streamStockChat({ question: 'test' }, (event) => events.push(event))
    assert.deepEqual(events.map((event) => event.type), ['content_delta', 'done'])
    assert.equal(calls.filter((item) => item.url === '/api/agent/runs').length, 1)
    const createCall = calls.find((item) => item.url === '/api/agent/runs')
    assert.ok(createCall.options.headers['Idempotency-Key'])
    assert.equal(JSON.parse(createCall.options.body).chat_mode, 'stock')

    const resumedCall = calls.filter((item) => item.url.includes('/events'))[1]
    assert.ok(resumedCall.url.endsWith('after_seq=1'))
    assert.equal(resumedCall.options.headers['Last-Event-ID'], '1')
  } finally {
    globalThis.fetch = originalFetch
    if (originalLocalStorage === undefined) delete globalThis.localStorage
    else globalThis.localStorage = originalLocalStorage
    if (originalWindow === undefined) delete globalThis.window
    else globalThis.window = originalWindow
  }
})

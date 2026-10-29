import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'

const source = await readFile(new URL('../src/components/UserLoginLogs.vue', import.meta.url), 'utf8')
const setup = source.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1]
const output = ts.transpileModule(`${setup}\nglobalThis.harness = { filters, items, loading, error, total, page, pageCount, applied, loadPage, search, reset, loginReason };`, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
}).outputText.split(/\r?\n/).filter((line) => !line.startsWith('import ') && !/^export\s*\{\s*\};?$/.test(line)).join('\n')

function createHarness({ username, fetch } = {}) {
  const requests = []
  const unmounted = []
  const context = {
    defineProps: () => ({ username }),
    ref: (value) => ({ value }),
    reactive: (value) => value,
    computed: (fn) => ({ get value() { return fn() } }),
    onMounted: () => {},
    onUnmounted: (fn) => unmounted.push(fn),
    api: { userLoginLogs: async (params) => {
      requests.push(params)
      return fetch ? fetch(params) : { items: [], total: 45, page: params.page, page_size: 20 }
    } },
  }
  vm.runInNewContext(output, context)
  return { ...context.harness, requests, unmounted }
}

const settle = () => new Promise((resolve) => setImmediate(resolve))

test('user shortcut scopes the initial audit request to the chosen username', async () => {
  const h = createHarness({ username: 'alice' })
  await h.loadPage(1)
  assert.equal(h.requests[0].username, 'alice')
  assert.equal(h.requests[0].page_size, 20)
  assert.equal(h.pageCount.value, 3)
})

test('failure and Beijing date filters are retained when paging', async () => {
  const h = createHarness()
  Object.assign(h.filters, { username: ' alice ', result: 'failure', start_date: '2026-10-28', end_date: '2026-10-29' })
  h.search()
  await settle()
  h.filters.username = 'unsaved-edit'
  await h.loadPage(2)
  const query = h.requests[1]
  assert.equal(query.success, false)
  assert.equal(query.username, 'alice')
  assert.equal(query.start_date, '2026-10-28')
  assert.equal(query.end_date, '2026-10-29')
  assert.equal(query.page, 2)
})

test('invalid date range is rejected before an API request', () => {
  const h = createHarness()
  Object.assign(h.filters, { start_date: '2026-10-29', end_date: '2026-10-28' })
  h.search()
  assert.equal(h.requests.length, 0)
  assert.match(h.error.value, /开始日期/)
})

test('reset removes all filters and returns to the first page', async () => {
  const h = createHarness({ username: 'alice' })
  await h.loadPage(2)
  h.filters.result = 'failure'
  h.reset()
  await settle()
  assert.equal(h.requests.at(-1).page, 1)
  assert.equal(h.requests.at(-1).username, undefined)
  assert.equal(h.requests.at(-1).success, undefined)
})

test('late response cannot replace the result from a newer query', async () => {
  let resolveFirst
  const h = createHarness({ fetch: ({ page }) => page === 1
    ? new Promise((resolve) => { resolveFirst = resolve })
    : Promise.resolve({ items: [{ id: 2 }], total: 25, page: 2 }) })
  const first = h.loadPage(1)
  await h.loadPage(2)
  resolveFirst({ items: [{ id: 1 }], total: 100, page: 1 })
  await first
  assert.equal(h.page.value, 2)
  assert.equal(h.items.value[0].id, 2)
  assert.equal(h.total.value, 25)
})

test('failure clears stale rows and an unmounted view ignores pending results', async () => {
  const h = createHarness({ fetch: async () => { throw new Error('offline') } })
  h.items.value = [{ id: 1 }]
  await h.loadPage()
  assert.equal(h.items.value.length, 0)
  assert.ok(h.error.value)
  assert.equal(h.loading.value, false)
  let resolve
  const h2 = createHarness({ fetch: () => new Promise((done) => { resolve = done }) })
  const pending = h2.loadPage()
  h2.unmounted[0]()
  resolve({ items: [{ id: 3 }], total: 1, page: 1 })
  await pending
  assert.equal(h2.items.value.length, 0)
})

test('API query serialization preserves false and encodes usernames safely', async () => {
  const apiSource = await readFile(new URL('../src/api.ts', import.meta.url), 'utf8')
  const compiled = ts.transpileModule(apiSource, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  const requests = []
  const context = {
    exports: {}, require: () => ({}), URLSearchParams, FormData,
    fetch: async (url) => { requests.push(url); return { ok: true, status: 200, json: async () => ({ items: [] }) } },
  }
  vm.runInNewContext(compiled, context)
  await context.exports.api.userLoginLogs({ username: 'bad&name', success: false, page: 2 })
  assert.equal(requests[0], '/api/users/login-logs?username=bad%26name&success=false&page=2')
})

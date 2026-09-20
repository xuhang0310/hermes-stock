import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { readFile } from 'node:fs/promises'
import vm from 'node:vm'
import ts from 'typescript'

const server = await createServer({
  root: fileURLToPath(new URL('../', import.meta.url)), appType: 'custom',
  optimizeDeps: { noDiscovery: true }, server: { middlewareMode: true, hmr: false }
})
const { industryTiles, industryQuestion } = await server.ssrLoadModule('/src/utils/industry.ts')
await server.close()

test('treemap is bounded, non-overlapping, and proportional for uneven industry sizes', () => {
  const rows = Array.from({ length: 110 }, (_, i) => ({ industry: `行业${i}`, stock_count: i + 1, amount_yi: 110 - i }))
  for (const metric of ['count', 'amount']) {
    const tiles = industryTiles(rows, metric)
    const total = rows.reduce((sum, row) => sum + (metric === 'count' ? row.stock_count : row.amount_yi), 0)
    assert.equal(tiles.length, 110)
    for (const tile of tiles) {
      assert.ok(tile.x >= 0 && tile.y >= 0 && tile.x + tile.width <= 1000.00001 && tile.y + tile.height <= 660.00001)
      const weight = metric === 'count' ? tile.industry.stock_count : tile.industry.amount_yi
      assert.ok(Math.abs(tile.width * tile.height / 660000 - weight / total) < 1e-10)
    }
    for (let i = 0; i < tiles.length; i++) for (let j = i + 1; j < tiles.length; j++) {
      const a = tiles[i], b = tiles[j]
      const intersection = Math.max(0, Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x)) *
        Math.max(0, Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y))
      assert.ok(intersection < 1e-6)
    }
  }
})

test('treemap does not invent zero or missing turnover', () => {
  assert.deepEqual(industryTiles([], 'count'), [])
  assert.deepEqual(industryTiles([{ industry: '银行', amount_yi: null }, { industry: '软件', amount_yi: 0 }], 'amount'), [])
  assert.equal(industryTiles([{ industry: '银行', stock_count: 4, amount_yi: null }], 'count')[0].width, 1000)
})

test('research prompt keeps industry, date, genuine zero and missing values distinct', () => {
  const prompt = industryQuestion({ name: '样本', symbol: '600000.SH', industry: '银行', trade_date: '2026-09-18', pct_chg: 0, amount_yi: null, turnover_rate: 1.2 })
  assert.match(prompt, /银行/)
  assert.match(prompt, /2026-09-18（日线快照，非实时）/)
  assert.match(prompt, /涨跌幅 0.00%/)
  assert.doesNotMatch(prompt, /成交额/)
  assert.match(prompt, /先核对最新行情/)
})

const viewSource = await readFile(new URL('../src/views/IndustryAnalysisView.vue', import.meta.url), 'utf8')
const setup = viewSource.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1]
const executable = ts.transpileModule(`${setup}\nObject.assign(globalThis, { harness: { refresh, loadMembers, addWatchlist, data, members, selected, error, memberError, memberLoading, adding, added } })`, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 }
}).outputText.split(/\r?\n/).filter(line => !line.startsWith('import ') && !/^export\s*\{/.test(line)).join('\n')

function harness(api, permissions = ['data:read', 'data:write']) {
  const events = []
  let unmount
  const context = vm.createContext({
    api, events, AbortController, DOMException, Error, industryTiles,
    ApiError: class extends Error {},
    defineProps: () => ({ user: { permissions } }), defineEmits: () => (...args) => events.push(args),
    ref: value => ({ value }), computed: fn => ({ get value() { return fn() } }),
    watch: () => {}, onMounted: () => {}, onBeforeUnmount: callback => { unmount = callback },
  })
  vm.runInContext(executable, context)
  return { ...context.harness, events, unmount }
}

function deferred() {
  let resolve
  const promise = new Promise(done => { resolve = done })
  return { resolve, promise }
}

test('switching industry ignores a slower previous member response', async () => {
  const first = deferred(), second = deferred()
  const state = harness({ industryMembers: ({ industry }) => industry === '银行' ? first.promise : second.promise })
  state.data.value = { as_of: '2026-09-18', snapshot_id: 'same' }
  state.selected.value = '银行'
  const old = state.loadMembers()
  state.selected.value = '软件服务'
  const current = state.loadMembers()
  second.resolve({ industry: '软件服务', items: [{ symbol: 'new' }] })
  await current
  first.resolve({ industry: '银行', items: [{ symbol: 'old' }] })
  await old
  assert.equal(state.members.value.industry, '软件服务')
  assert.equal(state.memberLoading.value, false)
})

test('failed refresh clears previous statistics instead of presenting stale data', async () => {
  const state = harness({ industryOverview: async () => { throw new Error('offline') } })
  state.data.value = { as_of: '2026-09-18' }
  state.members.value = { items: ['old'] }
  await state.refresh()
  assert.equal(state.data.value, null)
  assert.equal(state.members.value, null)
  assert.equal(state.error.value, 'offline')
})

test('watchlist respects permission and suppresses repeated submissions while pending', async () => {
  let calls = 0
  const request = deferred()
  const api = { addWatchlist: () => { calls++; return request.promise } }
  const stock = { symbol: '600000.SH', name: '样本', industry: '银行', trade_date: '2026-09-18' }
  await harness(api, ['data:read']).addWatchlist(stock)
  assert.equal(calls, 0)
  const state = harness(api)
  const pending = state.addWatchlist(stock)
  await state.addWatchlist(stock)
  assert.equal(calls, 1)
  request.resolve({ symbol: stock.symbol, id: 10 })
  await pending
  assert.equal(state.added.value.has(stock.symbol), true)
  assert.equal(state.events[0][0], 'watchlistAdded')
  await state.addWatchlist(stock)
  assert.equal(calls, 1)
  const exiting = harness(api)
  const pendingExit = exiting.addWatchlist(stock)
  exiting.unmount()
  await pendingExit
  assert.equal(exiting.events.length, 0, 'a departed view must not update a different user session')
})

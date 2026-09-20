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
const { industryTiles, industryQuestion, mergeSmallIndustries, OTHER_INDUSTRY, leaderScores } = await server.ssrLoadModule('/src/utils/industry.ts')
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

test('small industries merge into an honest aggregate bucket with weighted stats', () => {
  const stat = (industry, stock_count, avg_pct, valid_count = stock_count) => ({
    industry, stock_count, valid_count, coverage: valid_count / stock_count,
    avg_pct, median_pct: null, up_count: valid_count, down_count: 0, flat_count: 0,
    up_ratio: 1, amount_yi: stock_count, amount_count: valid_count,
    missing_count: 0, no_trade_count: 0, invalid_count: 0, leader: null,
  })
  const rows = [stat('银行', 4000, 1), stat('证券', 1000, 2), stat('微小A', 10, 5), stat('微小B', 6, -3)]
  const merged = mergeSmallIndustries(rows, 'count', 0.004)
  assert.equal(merged.length, 3)
  const other = merged.find(row => row.industry === OTHER_INDUSTRY)
  assert.ok(other, 'tail industries must collapse into the aggregate bucket')
  assert.equal(other.stock_count, 16)
  assert.equal(other.valid_count, 16)
  assert.equal(other.up_count, 16)
  assert.equal(other.amount_yi, 16)
  assert.ok(Math.abs(other.avg_pct - (5 * 10 + -3 * 6) / 16) < 1e-12, 'avg_pct is valid-count weighted, not a simple mean')
  assert.equal(other.leader, null)
  // A single tail row must not be swallowed: merging it would hide a named industry.
  assert.deepEqual(mergeSmallIndustries([stat('银行', 5000, 1), stat('独有行业', 1, 0)], 'count', 0.004).length, 2)
  // Merged rows still produce valid bounded tiles.
  const tiles = industryTiles(merged, 'count')
  assert.equal(tiles.length, 3)
  for (const tile of tiles) {
    assert.ok(tile.x >= 0 && tile.y >= 0 && tile.x + tile.width <= 1000.00001 && tile.y + tile.height <= 660.00001)
  }
})

test('research prompt keeps industry, date, genuine zero and missing values distinct', () => {
  const prompt = industryQuestion({ name: '样本', symbol: '600000.SH', industry: '银行', trade_date: '2026-09-18', pct_chg: 0, amount_yi: null, turnover_rate: 1.2 })
  assert.match(prompt, /银行/)
  assert.match(prompt, /2026-09-18（日线快照，非实时）/)
  assert.match(prompt, /涨跌幅 0.00%/)
  assert.doesNotMatch(prompt, /成交额/)
  assert.match(prompt, /先核对最新行情/)
})

test('leader scores are min-max normalized, equal-weighted, and honest about missing factors', () => {
  const member = (symbol, pct_chg, extras = {}) => ({
    symbol, name: symbol, market: 'SH', price: 10, trade_date: '2026-09-18',
    volume_ratio: 1, turnover_rate: 1, total_mv_yi: 100, pe_ttm: 10, ma_value: null,
    industry: '银行', amount_yi: 10, status: 'valid', pct_chg, ...extras,
  })
  const rows = leaderScores([
    member('600001.SH', 10, { turnover_rate: 8, amount_yi: 50, volume_ratio: 3, total_mv_yi: 300 }),
    member('600002.SH', 0, { turnover_rate: 2, amount_yi: 10, volume_ratio: 1, total_mv_yi: 100 }),
    member('600003.SH', 5, { turnover_rate: null, amount_yi: null, volume_ratio: null, total_mv_yi: null }),
    member('600004.SH', 99, { status: 'no_trade' }),
  ])
  assert.equal(rows.length, 3, 'non-trading members must not enter the leader board')
  assert.equal(rows[0].member.symbol, '600001.SH')
  const top = rows[0]
  assert.equal(top.factors.find(factor => factor.key === 'pct_chg').score, 100)
  assert.equal(rows[2].member.symbol, '600002.SH')
  assert.equal(rows[2].factors.find(factor => factor.key === 'pct_chg').score, 0)
  const missing = rows[1].factors.find(factor => factor.key === 'turnover_rate')
  assert.equal(missing.value, null)
  assert.equal(missing.score, 0)
  for (const row of rows) {
    const expected = Math.round(row.factors.reduce((sum, factor) => sum + factor.score, 0) / row.factors.length)
    assert.equal(row.composite, expected)
  }
  assert.equal(leaderScores([], 5).length, 0)
})

const viewSource = await readFile(new URL('../src/views/IndustryAnalysisView.vue', import.meta.url), 'utf8')

test('page renders dual leader boards, ranking strength bars and leader score decomposition', () => {
  assert.match(viewSource, /data-testid="industry-gainers-board"/)
  assert.match(viewSource, /data-testid="industry-laggards-board"/)
  assert.match(viewSource, /领涨主线/)
  assert.match(viewSource, /领跌方向/)
  assert.match(viewSource, /row\.leaders/)
  assert.match(viewSource, /\{\{ row\.up_count \}\}涨 \{\{ row\.down_count \}\}跌/)
  assert.match(viewSource, /ia-board-track/)
  assert.match(viewSource, /ia-row-track/)
  assert.match(viewSource, /data-testid="industry-leader-scores"/)
  assert.match(viewSource, /龙头评分拆解/)
  assert.match(viewSource, /上涨占比/)
  assert.match(viewSource, /成交额/)
  assert.match(viewSource, /不代表收益预测/)
})
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

test('refresh keeps existing content visible while the new section loads', async () => {
  const pending = deferred()
  const state = harness({ industryOverview: () => pending.promise })
  const previous = { as_of: '2026-09-17', industries: [{ industry: '银行' }] }
  state.data.value = previous
  const request = state.refresh()
  assert.equal(state.data.value, previous, 'old content must not blank out during refresh')
  assert.equal(state.error.value, '')
  pending.resolve({ as_of: '2026-09-18', industries: [{ industry: '银行' }] })
  await request
  assert.equal(state.data.value.as_of, '2026-09-18')
  assert.equal(state.error.value, '')
})

test('page uses aggregate bucket, hover preview, stale badge, PE column and mobile ranking default', () => {
  assert.match(viewSource, /mergeSmallIndustries/)
  assert.match(viewSource, /OTHER_INDUSTRY/)
  assert.match(viewSource, /data-testid="industry-hover-preview"/)
  assert.match(viewSource, /hoverStat = tile\.industry/)
  assert.match(viewSource, /早于最近收盘日/)
  assert.match(viewSource, /as_of < data\.value\.expected_date/)
  assert.match(viewSource, /value="pe_ttm"/)
  assert.match(viewSource, /item\.pe_ttm/)
  assert.match(viewSource, /window\.innerWidth < 980/)
  assert.match(viewSource, /ia-content.ia-dim|class="ia-content" :class="\{ 'ia-dim': loading \}"/)
  assert.match(viewSource, /点击固定选择/)
})

test('page chrome stays plain: no eyebrow labels, decorative numbers, arrows or separate monospace face', () => {
  assert.doesNotMatch(viewSource, /ia-eyebrow/)
  assert.doesNotMatch(viewSource, /section-number/)
  assert.doesNotMatch(viewSource, /↗/)
  assert.doesNotMatch(viewSource, /从行业，看见市场的方向/)
  assert.doesNotMatch(viewSource, /Consolas|monospace/)
  assert.match(viewSource, /ia-heading-note/)
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

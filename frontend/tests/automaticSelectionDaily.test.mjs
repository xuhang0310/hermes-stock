import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'

const pageSource = await readFile(new URL('../src/views/AutomaticSelectionView.vue', import.meta.url), 'utf8')
const appSource = await readFile(new URL('../src/App.vue', import.meta.url), 'utf8')
const apiSource = await readFile(new URL('../src/api.ts', import.meta.url), 'utf8')
const typesSource = await readFile(new URL('../src/types.ts', import.meta.url), 'utf8')

function deferred() {
  let resolve
  let reject
  const promise = new Promise((nextResolve, nextReject) => {
    resolve = nextResolve
    reject = nextReject
  })
  return { promise, resolve, reject }
}

function pick(symbol, overrides = {}) {
  return {
    symbol,
    name: `股票${symbol}`,
    industry: '银行',
    rank: 1,
    eligible_count: 3800,
    top_percent: 0.1,
    change_state: 'added',
    change_label: '新进',
    market_cap_label: '大盘',
    total_mv_yi: 500,
    close_price: 10,
    pct_chg: 1.2,
    pe_ttm: 5,
    pb: 0.6,
    avg_amount_yi: 9,
    momentum_20: 0.05,
    volatility_20: 0.01,
    headline: '估值比 92% 的股票便宜',
    dimensions: [],
    risks: [],
    price_path: [],
    performance: { since_selection: null, horizons: [] },
    ...overrides,
  }
}

function dailyPayload(tradeDate, overrides = {}) {
  return {
    strategy: { id: 1, name: '前瞻多因子基准策略', version: 1, lifecycle_state: 'official', is_official: true },
    available_dates: [
      { trade_date: '20261016', status: 'completed', status_label: '已出结果' },
      { trade_date: '20261015', status: 'completed', status_label: '已出结果' },
    ],
    trade_date: tradeDate,
    is_latest: tradeDate === '20261016',
    last_completed_trade_date: '20261016',
    run: { run_id: `run-${tradeDate}`, llm_explanation: '' },
    status: { code: 'ok', title: '结果', message: '', issues: [], fallback_trade_date: null },
    summary: null,
    picks: [pick(`${tradeDate}-A`), pick(`${tradeDate}-B`, { rank: 2 })],
    removed: [],
    funnel: null,
    method: {},
    performance: { since_selection: null, horizons: [] },
    track_brief: null,
    disclaimer: '这是按固定规则打分排序的研究结果，不是对未来涨跌的预测，也不构成投资建议。',
    limitations: '',
    ...overrides,
  }
}

const setupMatch = pageSource.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)
assert.ok(setupMatch, 'AutomaticSelectionView script setup should be present')
const harnessExposure = `
;(globalThis as Record<string, unknown>).__dailyHarness = {
  loadDaily, loadTrack, selectDate, openDate, refresh, dispatchRun, selectTab,
  openDetail, closeDetail, addToWatchlist, askStock, watchLabel, periodCells,
  activeTab, daily, track, loading, error, notice, detailSymbol, detailPick, watchlistState,
  tabs, canDispatch, canSeeAdvanced, trackLine, trackBars, picks,
  loadStrategies, selectStrategy, strategies, strategyOptions, strategyId, currentStrategyId,
}
`
const transpiled = ts.transpileModule(`${setupMatch[1]}\n${harnessExposure}`, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  reportDiagnostics: true,
})
assert.equal(transpiled.diagnostics?.length || 0, 0)
const executable = transpiled.outputText
  .split(/\r?\n/)
  .filter((line) => !line.startsWith('import ') && !/^export\s*\{\s*\};?$/.test(line))
  .join('\n')

function createHarness({ permissions = ['data:read'], api: apiOverrides = {} } = {}) {
  const ref = (value) => ({ value })
  const computed = (getter) => Object.defineProperty({}, 'value', { get: getter })
  const emitted = []
  const calls = { daily: [], track: 0, watchlist: [], dispatch: 0 }
  const api = {
    automaticSelectionDaily: async (tradeDate, strategyId) => {
      calls.daily.push(tradeDate)
      calls.dailyStrategy = strategyId
      return dailyPayload(tradeDate || '20261016')
    },
    automaticSelectionTrackRecord: async (_limit, strategyId) => {
      calls.track += 1
      calls.trackStrategy = strategyId
      return { strategy: null, periods: [], summary: [], notes: [], disclaimer: '' }
    },
    automaticSelectionDispatch: async () => {
      calls.dispatch += 1
      return { job_run_id: 'job-1', job_key: 'k' }
    },
    addWatchlist: async (payload) => {
      calls.watchlist.push(payload)
      return { id: 9, symbol: payload.symbol }
    },
    ...apiOverrides,
  }
  const context = {
    api,
    computed,
    ref,
    nextTick: (callback) => Promise.resolve().then(callback),
    onMounted: () => {},
    onBeforeUnmount: () => {},
    defineProps: () => ({ user: { permissions } }),
    defineEmits: () => (...args) => emitted.push(args),
    renderMarkdown: (value) => value,
    beijingFromDb: (value) => value,
  }
  vm.runInNewContext(executable, context)
  return { harness: context.__dailyHarness, emitted, calls }
}

test('the page is wired into the app with plain-language endpoints and types', () => {
  assert.match(apiSource, /automaticSelectionDaily: \(tradeDate\?: string \| null, strategyId\?: number \| null\) =>/)
  assert.match(apiSource, /query\.set\('trade_date', tradeDate\)/)
  assert.match(apiSource, /query\.set\('strategy_id', String\(strategyId\)\)/)
  assert.match(apiSource, /\/api\/automatic-selection\/track-record\?limit=\$\{limit\}\$\{strategyId \? `&strategy_id=\$\{strategyId\}` : ''\}/)
  assert.match(typesSource, /export type AutomaticSelectionDaily = \{/)
  assert.match(typesSource, /export type AutomaticSelectionPick = \{[\s\S]*headline: string[\s\S]*risks: AutomaticSelectionRisk\[\]/)
  assert.match(typesSource, /export type AutomaticSelectionTrackRecord = \{/)
  assert.match(appSource, /@ask-stock="openAutomaticSelectionStockAnalysis"/)
  assert.match(appSource, /@watchlist-added="applyWatchlistItem"/)
  assert.match(appSource, /该股票来自每日自动选股/)
  assert.doesNotMatch(appSource, /确定性排名、质量门禁、不可变证据与前瞻评估/)
})

test('the user-facing page speaks plain language and keeps audit jargon behind the advanced tab', () => {
  const template = pageSource.slice(pageSource.indexOf('<template>'), pageSource.indexOf('<style'))
  assert.doesNotMatch(template, /Top-N|z-score|z 值|质量门禁|合格池|研究池|落库|supersede|驾驶舱|证据检查器|服务端分页|百分位|config_hash|data_fingerprint/)
  assert.match(template, /为什么入选/)
  assert.match(template, /需要注意/)
  assert.match(template, /选出后表现/)
  assert.match(template, /查看最近一次结果/)
  assert.match(template, /<AutomaticSelectionAudit :user="user" \/>/)
  assert.match(template, /v-else-if="activeTab === 'advanced' && canSeeAdvanced"/)
  assert.doesNotMatch(template, /<h1/)
})

test('page accessibility: underline tabs with arrow keys and a focus-trapped detail drawer', () => {
  assert.match(pageSource, /class="ui-tabs auto-tabs" role="tablist"/)
  assert.match(pageSource, /@keydown="handleTabKeydown"/)
  assert.match(pageSource, /\['ArrowLeft', 'ArrowRight', 'Home', 'End'\]/)
  assert.match(pageSource, /role="tabpanel"/)
  assert.match(pageSource, /role="dialog"[\s\S]*aria-modal="true"[\s\S]*aria-labelledby="pick-drawer-title"/)
  assert.match(pageSource, /@keydown\.esc\.stop\.prevent="closeDetail\(\)"/)
  assert.match(pageSource, /@keydown\.tab="trapDrawerFocus"/)
  assert.match(pageSource, /@media \(max-width: 680px\)/)
  assert.match(pageSource, /\.pnl-up \{ color: var\(--up\)/)
})

test('tabs and actions follow permissions', () => {
  const viewer = createHarness().harness
  assert.deepEqual([...viewer.tabs.value.map((tab) => tab.key)], ['today', 'history', 'method'])
  assert.equal(viewer.canDispatch.value, false)
  const admin = createHarness({ permissions: ['all'] }).harness
  assert.deepEqual([...admin.tabs.value.map((tab) => tab.key)], ['today', 'history', 'method', 'advanced'])
  assert.equal(admin.canDispatch.value, true)
})

test('a stale date response cannot replace the newer selection', async () => {
  const requests = { '20261015': deferred(), '': deferred() }
  const { harness } = createHarness({
    api: { automaticSelectionDaily: (tradeDate) => requests[tradeDate || ''].promise },
  })
  const older = harness.loadDaily('20261015')
  const latest = harness.loadDaily('')
  requests[''].resolve(dailyPayload('20261016'))
  await latest
  requests['20261015'].resolve(dailyPayload('20261015'))
  await older
  assert.equal(harness.daily.value.trade_date, '20261016')
  assert.equal(harness.loading.value, false)

  const failing = createHarness({ api: { automaticSelectionDaily: async () => { throw new Error('读取失败') } } }).harness
  await failing.loadDaily('')
  assert.match(failing.error.value, /读取失败/)
  assert.equal(failing.loading.value, false)
})

test('choosing the latest date asks for the default view, older dates are explicit', async () => {
  const { harness, calls } = createHarness()
  await harness.loadDaily('')
  harness.selectDate('20261015')
  await Promise.resolve()
  harness.selectDate('20261016')
  await Promise.resolve()
  assert.deepEqual(calls.daily, [null, '20261015', null])

  harness.activeTab.value = 'history'
  harness.openDate('20261015')
  assert.equal(harness.activeTab.value, 'today')
})

test('detail drawer only opens for stocks on the current result and closes on reload', async () => {
  const { harness } = createHarness()
  await harness.loadDaily('')
  harness.openDetail('not-a-pick')
  assert.equal(harness.detailPick.value, null)
  harness.openDetail('20261016-B')
  assert.equal(harness.detailPick.value.rank, 2)
  await harness.loadDaily('20261015')
  assert.equal(harness.detailSymbol.value, '')
})

test('watchlist and ask-stock actions respect permissions and report state', async () => {
  const writer = createHarness({ permissions: ['data:write', 'agent:use'] })
  await writer.harness.loadDaily('')
  const target = writer.harness.picks.value[0]
  await writer.harness.addToWatchlist(target)
  assert.equal(writer.calls.watchlist.length, 1)
  assert.equal(writer.calls.watchlist[0].group_name, '自动选股')
  assert.match(writer.calls.watchlist[0].reason, /^自动选股 2026-10-16 入选：估值比 92% 的股票便宜/)
  assert.equal(writer.harness.watchLabel(target.symbol), '已加入自选')
  await writer.harness.addToWatchlist(target)
  assert.equal(writer.calls.watchlist.length, 1)
  writer.harness.askStock(target)
  assert.deepEqual(writer.emitted.map((entry) => entry[0]), ['watchlistAdded', 'askStock'])
  assert.equal(writer.emitted[1][2], '20261016')

  const reader = createHarness()
  await reader.harness.loadDaily('')
  await reader.harness.addToWatchlist(reader.harness.picks.value[0])
  reader.harness.askStock(reader.harness.picks.value[0])
  assert.equal(reader.calls.watchlist.length, 0)
  assert.equal(reader.emitted.length, 0)
})

test('recalculation is only for schedulers and tells the user what happens next', async () => {
  const viewer = createHarness()
  await viewer.harness.dispatchRun()
  assert.equal(viewer.calls.dispatch, 0)
  const admin = createHarness({ permissions: ['schedule:manage'] })
  await admin.harness.dispatchRun()
  assert.equal(admin.calls.dispatch, 1)
  assert.match(admin.harness.notice.value, /几分钟内完成/)
})

test('track record loads once, explains the brief and draws signed bars', async () => {
  const { harness, calls } = createHarness()
  harness.selectTab('history')
  await harness.loadTrack()
  assert.equal(calls.track, 1)
  await new Promise((resolve) => setTimeout(resolve, 0))
  await harness.loadTrack()
  assert.equal(calls.track, 1)
  await harness.loadTrack(true)
  assert.equal(calls.track, 2)

  assert.match(harness.trackLine.value, /还没有到期的 5 日表现/)
  harness.daily.value = dailyPayload('20261016', { track_brief: { horizon: 5, periods: 12, wins: 7, avg_excess_return: -0.004 } })
  assert.equal(harness.trackLine.value, '最近 12 期的 5 日表现：7 期跑赢合格股票平均，平均每期跑输 0.40%。')

  harness.track.value = {
    periods: [
      { run_id: 'b', trade_date: '20261016', selected_count: 30, horizons: { 5: { status: 'completed', status_label: '已到期', excess_return: -0.01 } } },
      { run_id: 'a', trade_date: '20261015', selected_count: 30, horizons: { 5: { status: 'completed', status_label: '已到期', excess_return: 0.02 } } },
    ],
    summary: [],
    notes: [],
  }
  const bars = harness.trackBars.value
  assert.deepEqual([...bars.map((bar) => bar.date)], ['20261015', '20261016'])
  assert.ok(bars[0].y < 50)
  assert.equal(bars[1].y, 50)
  const cells = harness.periodCells(harness.track.value.periods[0])
  assert.equal(cells.length, 3)
  assert.equal(cells[1].status_label, '—')
})

test('data sync status is visible in plain words, technical detail only for operators', () => {
  const template = pageSource.slice(pageSource.indexOf('<template>'), pageSource.indexOf('<style'))
  assert.match(template, /v-if="dataSync && !dataSync\.ok"[\s\S]*data-testid="automatic-selection-data-sync"/)
  assert.match(template, /数据同步：\{\{ dataSync\.title \}\}/)
  assert.match(template, /\{\{ dataSync\.next_step \}\}/)
  assert.match(template, /<details v-if="dataSync\.detail && canSeeAdvanced" class="tech-detail">/)
  assert.match(template, /<details v-if="status\.detail && canSeeAdvanced" class="tech-detail">/)
  assert.match(template, /beijingFromDb\(dataSync\.at\)/)
  assert.match(typesSource, /export type AutomaticSelectionDataSync = \{/)
  assert.match(typesSource, /data_sync: AutomaticSelectionDataSync \| null/)
})

test('switching strategy reloads the day and the track record for that strategy', async () => {
  const strategy = (id, name, state, official = false) => ({
    id, name, lifecycle_state: state, is_official: official, enabled: true,
  })
  const { harness, calls } = createHarness({
    api: {
      automaticSelectionStrategies: async () => [
        strategy(2, '模型排名前 30（挑战者）', 'challenger'),
        strategy(1, '前瞻多因子基准策略', 'official', true),
        strategy(3, '旧版本', 'retired'),
      ],
    },
  })
  await harness.loadStrategies()
  // 正式策略排第一，停用的不显示
  assert.deepEqual([...harness.strategyOptions.value.map((item) => item.id)], [1, 2])
  await harness.loadDaily('')
  assert.equal(harness.currentStrategyId.value, 1)
  harness.selectTab('history')
  harness.selectStrategy('2')
  await new Promise((resolve) => setTimeout(resolve, 0))
  assert.equal(harness.strategyId.value, 2)
  assert.equal(calls.dailyStrategy, 2)
  assert.equal(calls.trackStrategy, 2)

  const broken = createHarness({ api: { automaticSelectionStrategies: async () => { throw new Error('x') } } }).harness
  await broken.loadStrategies()
  assert.equal(broken.strategyOptions.value.length, 0)
})

test('a previous strategy history response stays discarded after switching outside history', async () => {
  const previous = deferred()
  const requested = []
  const nextTrack = { strategy: { id: 2 }, periods: [], summary: [], notes: [], disclaimer: '' }
  const { harness } = createHarness({
    api: {
      automaticSelectionTrackRecord: async (_limit, strategyId) => {
        requested.push(strategyId)
        return strategyId === 2 ? nextTrack : previous.promise
      },
    },
  })
  harness.activeTab.value = 'history'
  const pending = harness.loadTrack()
  harness.selectTab('today')
  harness.selectStrategy('2')
  previous.resolve({ ...nextTrack, strategy: { id: 1 } })
  await pending
  assert.equal(harness.track.value, null)
  harness.selectTab('history')
  await new Promise((resolve) => setTimeout(resolve, 0))
  assert.deepEqual(requested, [null, 2])
  assert.equal(harness.track.value.strategy.id, 2)
})

test('a pending previous strategy request does not block loading the new history', async () => {
  const previous = deferred()
  const requested = []
  const nextTrack = { strategy: { id: 2 }, periods: [], summary: [], notes: [], disclaimer: '' }
  const { harness } = createHarness({
    api: {
      automaticSelectionTrackRecord: async (_limit, strategyId) => {
        requested.push(strategyId)
        return strategyId === 2 ? nextTrack : previous.promise
      },
    },
  })
  harness.activeTab.value = 'history'
  const pending = harness.loadTrack()
  harness.selectTab('today')
  harness.selectStrategy('2')
  harness.selectTab('history')
  await new Promise((resolve) => setTimeout(resolve, 0))
  assert.deepEqual(requested, [null, 2])
  assert.equal(harness.track.value.strategy.id, 2)
  previous.resolve({ ...nextTrack, strategy: { id: 1 } })
  await pending
  assert.equal(harness.track.value.strategy.id, 2)
})

test('switching strategy clears previous picks even when the next request fails', async () => {
  const next = deferred()
  const { harness } = createHarness({
    api: {
      automaticSelectionDaily: async (_tradeDate, strategyId) =>
        strategyId === 2 ? next.promise : dailyPayload('20261016'),
    },
  })
  await harness.loadDaily()
  assert.equal(harness.daily.value.strategy.id, 1)
  harness.selectStrategy('2')
  assert.equal(harness.daily.value, null)
  next.reject(new Error('方案读取失败'))
  await new Promise((resolve) => setTimeout(resolve, 0))
  assert.equal(harness.daily.value, null)
  assert.equal(harness.picks.value.length, 0)
  assert.match(harness.error.value, /方案读取失败/)
})

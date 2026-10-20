import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'

// 运行审计与策略管理收进了自动选股页的“高级”标签（AutomaticSelectionAudit 组件）。
const viewSource = await readFile(
  new URL('../src/components/AutomaticSelectionAudit.vue', import.meta.url),
  'utf8'
)
const pageSource = await readFile(
  new URL('../src/views/AutomaticSelectionView.vue', import.meta.url),
  'utf8'
)
const shellSource = await readFile(new URL('../src/components/AppShell.vue', import.meta.url), 'utf8')
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

function runEvidence(runId, suffix = runId) {
  return {
    run_id: runId,
    candidates: [{ symbol: `candidate-${suffix}` }],
    candidate_total: 1,
    candidate_limit: 50,
    candidate_offset: 0,
    evaluations: [{ run_id: runId, horizon: 5 }],
  }
}

function overviewEvidence(runId) {
  return {
    latest_run: { run_id: runId },
    top_candidates: [{ symbol: `overview-${runId}` }],
    evaluations: [{ run_id: runId, horizon: 5 }],
    recent_runs: [],
    strategy_counts: {},
    official_strategy: null,
    limitations: '',
  }
}

const setupMatch = viewSource.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)
assert.ok(setupMatch, 'AutomaticSelectionAudit script setup should be present')
const harnessExposure = `
;(globalThis as Record<string, unknown>).__automaticSelectionHarness = {
  loadDashboard,
  loadRun,
  selectScope,
  previousCandidatePage,
  nextCandidatePage,
  selectCandidate,
  selectedRunId,
  runDetail,
  selectedRunDetail,
  selectedOverviewRun,
  currentRun,
  isSelectedLatestRun,
  runContextLabel,
  candidates,
  selectedCandidateSymbol,
  selectedCandidate,
  evaluations,
  candidateTotal,
  candidateLimit,
  candidateOffset,
  candidatePage,
  candidatePageCount,
  candidateScope,
  overview,
  error,
  loading,
  detailLoading,
}
`
const transpileResult = ts.transpileModule(`${setupMatch[1]}\n${harnessExposure}`, {
  compilerOptions: {
    module: ts.ModuleKind.ESNext,
    target: ts.ScriptTarget.ES2022,
  },
  reportDiagnostics: true,
})
assert.equal(
  transpileResult.diagnostics?.length || 0,
  0,
  (transpileResult.diagnostics || [])
    .map((diagnostic) => ts.flattenDiagnosticMessageText(diagnostic.messageText, '\n'))
    .join('\n')
)
const viewExecutable = transpileResult.outputText
  .split(/\r?\n/)
  .filter((line) => !line.startsWith('import ') && !/^export\s*\{\s*\};?$/.test(line))
  .join('\n')

function createViewHarness(apiOverrides = {}) {

  const ref = (value) => ({ value })
  const computed = (getter) => Object.defineProperty({}, 'value', { get: getter })
  const api = {
    automaticSelectionOverview: async () => overviewEvidence('latest'),
    automaticSelectionStrategies: async () => [],
    automaticSelectionRunDetail: async (runId) => runEvidence(runId),
    ...apiOverrides,
  }
  const context = {
    api,
    computed,
    nextTick: (callback) => Promise.resolve().then(callback),
    onBeforeUnmount: () => {},
    onMounted: () => {},
    ref,
    defineProps: () => ({ user: { permissions: [] } }),
    renderMarkdown: (value) => value,
    beijingFromDb: (value) => value,
  }
  vm.runInNewContext(viewExecutable, context)
  return context.__automaticSelectionHarness
}

test('automatic selection is mounted under quant navigation and has a durable path', () => {
  assert.match(shellSource, /\{ key: 'automaticSelection', label: '自动选股' \}/)
  assert.match(appSource, /import AutomaticSelectionView from '\.\/views\/AutomaticSelectionView\.vue'/)
  assert.match(appSource, /v-else-if="activePage === 'automaticSelection'"/)
  assert.match(appSource, /window\.history\.pushState\(\{\}, '', '\/automatic-selection'\)/)
  assert.match(appSource, /\^\\\/automatic-selection\\\/\?\$\/i/)
  assert.doesNotMatch(appSource, /window\.location\.pathname === '\/automatic-selection'/)
})

test('automatic selection API client covers research runs and human strategy governance', () => {
  assert.match(apiSource, /automaticSelectionOverview:/)
  assert.match(apiSource, /automaticSelectionStrategies:/)
  assert.match(apiSource, /automaticSelectionCreateStrategy:/)
  assert.match(apiSource, /automaticSelectionPromoteStrategy:/)
  assert.match(apiSource, /automaticSelectionRetireStrategy:/)
  assert.match(apiSource, /automaticSelectionDispatch:/)
  assert.match(apiSource, /automaticSelectionRunDetail:/)
  assert.match(apiSource, /new URLSearchParams\(\{/)
  assert.match(apiSource, /candidate_scope: candidateScope/)
  assert.match(apiSource, /candidate_limit: String\(candidateLimit\)/)
  assert.match(apiSource, /candidate_offset: String\(candidateOffset\)/)
  assert.match(apiSource, /request<ApiEnvelope<AutomaticSelectionOverview>>/)
})

test('automatic selection types preserve immutable evidence fields', () => {
  assert.match(typesSource, /export type AutomaticSelectionStrategy = \{/)
  assert.match(typesSource, /config_hash: string/)
  assert.match(typesSource, /data_fingerprint: string/)
  assert.match(typesSource, /supersedes_run_id: string \| null/)
  assert.match(typesSource, /exclusion_codes: string\[\]/)
  assert.match(typesSource, /constraint_codes: string\[\]/)
  assert.match(typesSource, /factor_values: Record<string, number>/)
  assert.match(typesSource, /z_scores: Record<string, number>/)
  assert.match(typesSource, /horizon: 5 \| 20 \| 60 \| number/)
  assert.match(typesSource, /candidate_total: number/)
  assert.match(typesSource, /candidate_limit: number/)
  assert.match(typesSource, /candidate_offset: number/)
  assert.match(typesSource, /min_history_coverage: number/)
  assert.match(typesSource, /history_count\?: number/)
  assert.match(typesSource, /history_coverage\?: number/)
  assert.match(typesSource, /input_evidence\?: \{[\s\S]*expected_history_dates: string\[\][\s\S]*trade_date: string[\s\S]*close: number \| null[\s\S]*amount: number \| null/)
})

test('audit panel uses a compact run-aware command bar, KPI strip, and three-column workspace', () => {
  assert.match(viewSource, /data-testid="automatic-selection-audit-command"/)
  assert.match(viewSource, /\{\{ runContextLabel \}\}/)
  assert.match(viewSource, /\? '最新一次' : '历史记录'/)
  assert.doesNotMatch(viewSource, /运行研究流水线|automaticSelectionDispatch/)
  assert.match(viewSource, /<h2 id="automatic-selection-audit-title">运行记录与策略管理<\/h2>/)
  assert.doesNotMatch(viewSource, /<h1|研究驾驶舱/)
  assert.match(viewSource, /<h3 id="quality-gate-title">数据检查与剔除明细<\/h3>/)
  assert.match(viewSource, /<h2 id="ranking-title">全部排名<\/h2>/)
  assert.doesNotMatch(viewSource, /class="eyebrow"/)
  assert.doesNotMatch(viewSource, /letter-spacing: \.13em/)
  assert.doesNotMatch(viewSource, /A-SHARE QUANT|RUN SELECTOR|QUALITY GATE|SERVER-RANKED EVIDENCE/)
  assert.doesNotMatch(viewSource, />立即运行</)
  assert.match(viewSource, /data-testid="automatic-selection-kpis"/)
  assert.match(viewSource, /data-testid="automatic-selection-workspace"/)
  assert.match(viewSource, /grid-template-columns: 240px minmax\(520px, 1fr\) 320px/)
  assert.match(viewSource, /class="run-rail cockpit-panel"/)
  assert.match(viewSource, /class="ranking-console cockpit-panel"/)
  assert.match(viewSource, /class="candidate-inspector cockpit-panel"/)
  assert.match(viewSource, /这次运行出错/)
  assert.match(viewSource, /currentRun\.error/)
})

test('audit panel exposes data checks, the full ranking and forward evaluation in plain words', () => {
  assert.match(viewSource, /data-testid="automatic-selection-quality-gate"/)
  assert.match(viewSource, /不会只拿部分股票硬算/)
  assert.match(viewSource, /data-testid="automatic-selection-ranking"/)
  assert.match(viewSource, />入选</)
  assert.match(viewSource, />全部合格</)
  assert.match(viewSource, />被剔除</)
  assert.match(viewSource, /因子原始值与标准化分（z）/)
  assert.match(viewSource, /data-testid="automatic-selection-evaluations"/)
  assert.match(viewSource, /5 \/ 20 \/ 60 个交易日后的表现/)
  assert.match(viewSource, /以下一个交易日开盘价为起点/)
  assert.match(viewSource, /历史行情覆盖/)
  assert.match(viewSource, /qualityGate\.metrics\.history_count/)
  assert.match(viewSource, /qualityGate\.thresholds\.min_history_coverage/)
  assert.match(viewSource, /fmtQualityValue\(failure\.code/)
  assert.match(viewSource, /candidateScope === 'excluded'/)
  assert.match(viewSource, /剔除原因/)
  assert.match(viewSource, /item\.exclusion_codes\.slice\(0, 2\)/)
  assert.match(viewSource, /比上期换手/)
  assert.match(viewSource, /selected_turnover/)
  assert.match(viewSource, /收盘价 \/ 涨跌/)
  assert.match(viewSource, /marketCapLabel\(item\.market_cap_bucket\)/)
  assert.match(viewSource, /small: '小盘', mid: '中盘', large: '大盘'/)
  assert.match(viewSource, /fmtNumber\(item\.composite_score, 2\)/)
  assert.doesNotMatch(viewSource, /Top-N|质量门禁|合格池|研究池|落库|supersede 链|挑战者|影子|晋升|退役/)
  assert.match(viewSource, /20 日均成交额/)
  assert.match(viewSource, /value \/ 100_000/)
  assert.match(viewSource, /input_evidence\?\.history\?\.length/)
})

test('cockpit paginates immutable server evidence and maintains a page-local selection boundary', () => {
  assert.match(viewSource, /const CANDIDATE_PAGE_SIZE = 50/)
  assert.match(viewSource, /requestSequence !== evidenceRequestSequence/)
  assert.match(viewSource, /const selectedRunDetail = computed/)
  assert.match(viewSource, /runDetail\.value\?\.run_id === selectedRunId\.value/)
  assert.match(viewSource, /overview\.value\?\.latest_run\?\.run_id === selectedRunId\.value/)
  assert.match(viewSource, /CANDIDATE_PAGE_SIZE,[\s\S]*offset/)
  assert.match(viewSource, /data-testid="automatic-selection-pagination"/)
  assert.match(viewSource, /previousCandidatePage/)
  assert.match(viewSource, /nextCandidatePage/)
  assert.doesNotMatch(viewSource, /固定 50 行服务端分页|不在本地跨页重排|页面不做二次排序|会清空旧选择/)
  assert.match(viewSource, /v-memo="\[item, selectedCandidateSymbol === item\.symbol, candidateScope\]"/)
  assert.doesNotMatch(viewSource, /candidates\.value\.sort\(/)
  assert.match(viewSource, /const selectedCandidateSymbol = ref\(''\)/)
  assert.match(viewSource, /const selectedCandidate = computed/)
  assert.match(viewSource, /function selectCandidate\(symbol: string\)/)
  assert.match(viewSource, /@click="loadDashboard\(\)"/)
  assert.match(viewSource, /selectedRunId\.value = ''/)
})

test('selecting a run clears old evidence before the request and a failed load stays empty', async () => {
  const nextDetail = deferred()
  const harness = createViewHarness({
    automaticSelectionRunDetail: () => nextDetail.promise,
  })
  harness.overview.value = overviewEvidence('A')
  harness.selectedRunId.value = 'A'
  harness.runDetail.value = runEvidence('A')
  harness.selectedCandidateSymbol.value = 'candidate-A'
  assert.equal(harness.selectedCandidate.value.symbol, 'candidate-A')

  const pending = harness.loadRun('B')
  assert.equal(harness.selectedRunId.value, 'B')
  assert.equal(harness.runDetail.value, null)
  assert.equal(harness.selectedCandidateSymbol.value, '')
  assert.equal(harness.selectedCandidate.value, null)
  assert.equal(harness.selectedRunDetail.value, null)
  assert.equal(harness.selectedOverviewRun.value, null)
  assert.equal(harness.currentRun.value, null)
  assert.equal(harness.candidates.value.length, 0)
  assert.equal(harness.evaluations.value.length, 0)
  assert.equal(harness.candidateTotal.value, 0)
  assert.equal(harness.candidateLimit.value, 50)
  assert.equal(harness.candidateOffset.value, 0)
  assert.equal(harness.candidatePage.value, 1)
  assert.equal(harness.candidatePageCount.value, 1)

  nextDetail.reject(new Error('B detail failed'))
  await pending
  assert.equal(harness.currentRun.value, null)
  assert.equal(harness.candidates.value.length, 0)
  assert.equal(harness.selectedCandidateSymbol.value, '')
  assert.equal(harness.selectedCandidate.value, null)
  assert.match(harness.error.value, /B detail failed/)
  assert.equal(harness.detailLoading.value, false)
})

test('an empty run id cannot invalidate an in-flight dashboard request', async () => {
  const nextOverview = deferred()
  const harness = createViewHarness({
    automaticSelectionOverview: () => nextOverview.promise,
    automaticSelectionStrategies: async () => [],
  })

  const pendingDashboard = harness.loadDashboard()
  await harness.loadRun('')
  nextOverview.resolve(overviewEvidence('latest'))
  await pendingDashboard

  assert.equal(harness.loading.value, false)
  assert.equal(harness.detailLoading.value, false)
  assert.equal(harness.currentRun.value.run_id, 'latest')
  assert.equal(harness.selectedCandidateSymbol.value, 'candidate-latest')
})

test('dashboard failure cannot display run A evidence under selected run B', async () => {
  const nextOverview = deferred()
  const harness = createViewHarness({
    automaticSelectionOverview: () => nextOverview.promise,
    automaticSelectionStrategies: async () => [],
  })
  harness.overview.value = overviewEvidence('A')
  harness.selectedRunId.value = 'A'
  harness.runDetail.value = runEvidence('A')
  harness.selectedCandidateSymbol.value = 'candidate-A'

  const pending = harness.loadDashboard('B')
  assert.equal(harness.selectedRunId.value, 'B')
  assert.equal(harness.runDetail.value, null)
  assert.equal(harness.selectedCandidateSymbol.value, '')
  assert.equal(harness.selectedCandidate.value, null)
  assert.equal(harness.currentRun.value, null)
  assert.equal(harness.candidates.value.length, 0)
  assert.equal(harness.evaluations.value.length, 0)

  nextOverview.reject(new Error('dashboard failed'))
  await pending
  assert.equal(harness.selectedRunId.value, 'B')
  assert.equal(harness.currentRun.value, null)
  assert.equal(harness.candidateTotal.value, 0)
  assert.match(harness.error.value, /dashboard failed/)
  assert.equal(harness.loading.value, false)
})

test('a stale detail response cannot replace the newer run selection', async () => {
  const requests = {
    B: deferred(),
    C: deferred(),
  }
  const harness = createViewHarness({
    automaticSelectionRunDetail: (runId) => requests[runId].promise,
  })
  harness.overview.value = overviewEvidence('A')
  harness.selectedRunId.value = 'A'
  harness.runDetail.value = runEvidence('A')
  harness.selectedCandidateSymbol.value = 'candidate-A'

  const first = harness.loadRun('B')
  const second = harness.loadRun('C')
  requests.B.resolve(runEvidence('B'))
  await first
  assert.equal(harness.selectedRunId.value, 'C')
  assert.equal(harness.runDetail.value, null)
  assert.equal(harness.currentRun.value, null)
  assert.equal(harness.selectedCandidateSymbol.value, '')
  assert.equal(harness.selectedCandidate.value, null)

  requests.C.resolve(runEvidence('C'))
  await second
  assert.equal(harness.selectedRunId.value, 'C')
  assert.equal(harness.selectedRunDetail.value.run_id, 'C')
  assert.equal(harness.currentRun.value.run_id, 'C')
  assert.equal(harness.candidates.value[0].symbol, 'candidate-C')
  assert.equal(harness.selectedCandidateSymbol.value, 'candidate-C')
  assert.equal(harness.selectedCandidate.value.symbol, 'candidate-C')
  assert.equal(harness.evaluations.value[0].run_id, 'C')
  assert.equal(harness.candidateTotal.value, 1)
  assert.equal(harness.detailLoading.value, false)
})

test('scope and page requests clear selection, then seed the first candidate from current evidence', async () => {
  const scopeDetail = deferred()
  const scopeHarness = createViewHarness({
    automaticSelectionRunDetail: () => scopeDetail.promise,
  })
  scopeHarness.overview.value = overviewEvidence('A')
  scopeHarness.selectedRunId.value = 'A'
  scopeHarness.runDetail.value = runEvidence('A')
  scopeHarness.selectedCandidateSymbol.value = 'candidate-A'

  const pendingScope = scopeHarness.selectScope('excluded')
  assert.equal(scopeHarness.candidateScope.value, 'excluded')
  assert.equal(scopeHarness.runDetail.value, null)
  assert.equal(scopeHarness.selectedCandidateSymbol.value, '')
  assert.equal(scopeHarness.selectedCandidate.value, null)

  scopeDetail.resolve(runEvidence('A', 'excluded'))
  await pendingScope
  assert.equal(scopeHarness.selectedCandidateSymbol.value, 'candidate-excluded')
  assert.equal(scopeHarness.selectedCandidate.value.symbol, 'candidate-excluded')

  const pageDetail = deferred()
  const pageCalls = []
  const pageHarness = createViewHarness({
    automaticSelectionRunDetail: (runId, scope, limit, offset) => {
      pageCalls.push({ runId, scope, limit, offset })
      return pageDetail.promise
    },
  })
  pageHarness.overview.value = overviewEvidence('A')
  pageHarness.selectedRunId.value = 'A'
  pageHarness.runDetail.value = {
    ...runEvidence('A', 'page-one'),
    candidate_total: 101,
    candidate_limit: 50,
    candidate_offset: 0,
  }
  pageHarness.selectedCandidateSymbol.value = 'candidate-page-one'

  const pendingPage = pageHarness.nextCandidatePage()
  assert.equal(pageHarness.runDetail.value, null)
  assert.equal(pageHarness.selectedCandidateSymbol.value, '')
  assert.equal(pageHarness.selectedCandidate.value, null)
  assert.deepEqual(pageCalls, [{ runId: 'A', scope: 'top', limit: 50, offset: 50 }])

  pageDetail.resolve({
    ...runEvidence('A', 'page-two'),
    candidate_total: 101,
    candidate_limit: 50,
    candidate_offset: 50,
  })
  await pendingPage
  assert.equal(pageHarness.candidatePage.value, 2)
  assert.equal(pageHarness.selectedCandidateSymbol.value, 'candidate-page-two')
  assert.equal(pageHarness.selectedCandidate.value.symbol, 'candidate-page-two')
})

test('candidate selection is constrained to the loaded page', () => {
  const harness = createViewHarness()
  harness.selectedRunId.value = 'A'
  harness.runDetail.value = {
    ...runEvidence('A'),
    candidates: [{ symbol: '600000.SH' }, { symbol: '000001.SZ' }],
  }
  harness.selectedCandidateSymbol.value = '600000.SH'

  harness.selectCandidate('000001.SZ')
  assert.equal(harness.selectedCandidateSymbol.value, '000001.SZ')
  assert.equal(harness.selectedCandidate.value.symbol, '000001.SZ')

  harness.selectCandidate('not-on-this-page')
  assert.equal(harness.selectedCandidateSymbol.value, '000001.SZ')
})

test('cockpit formats trade dates separately from database timestamps', () => {
  assert.match(viewSource, /import \{ beijingFromDb \} from '\.\.\/utils\/datetime'/)
  assert.match(viewSource, /fmtTradeDate\(currentRun\.target_trade_date\)/)
  assert.match(viewSource, /beijingFromDb\(currentRun\.finished_at \|\| currentRun\.started_at\)/)
  assert.match(viewSource, /beijingFromDb\(strategy\.created_at\)/)
})

test('browser history restores the backtest origin independently from automatic selection', () => {
  assert.match(appSource, /else if \(activePage\.value === 'backtestReport'\) \{[\s\S]*activePage\.value = backtestReturnPage\.value/)
  assert.match(appSource, /else if \(activePage\.value === 'automaticSelection'\) \{[\s\S]*activePage\.value = 'workbench'/)
})

test('strategy lifecycle actions are permission-gated and require human confirmation', () => {
  assert.match(viewSource, /permissions\.includes\('model:manage'\)/)
  assert.match(pageSource, /hasPermission\('schedule:manage'\)/)
  assert.match(viewSource, /v-if="canManageStrategies"/)
  assert.match(viewSource, /人工核验说明/)
  assert.match(viewSource, /minlength="4"/)
  assert.match(viewSource, /确认设为正式方案/)
  assert.match(viewSource, /至少有 1 期已到期、数据覆盖达标的 5 日表现/)
  assert.match(viewSource, /不代表策略已被证明有效/)
})

test('strategy versions are edited through a form with an expert JSON mode and a change summary', () => {
  assert.match(viewSource, /const WEIGHT_FIELDS = \[/)
  assert.match(viewSource, /function configFromForm\(\)/)
  assert.match(viewSource, /function formFromConfig\(/)
  assert.match(viewSource, /min_avg_amount: Number\(form\.minAvgAmountWan\) \* 10/)
  assert.match(viewSource, /专家模式（直接编辑 JSON）/)
  assert.match(viewSource, /const configChanges = computed/)
  assert.match(viewSource, /createForm\.value\.expertMode\s*\? JSON\.parse/)
})

test('cockpit accessibility covers semantic selection, busy/live state, and modal focus behavior', () => {
  assert.match(viewSource, /role="listbox"/)
  assert.match(viewSource, /role="option"/)
  assert.match(viewSource, /class="candidate-button"/)
  assert.match(viewSource, /:aria-pressed="selectedCandidateSymbol === item\.symbol"/)
  assert.match(viewSource, /role="tablist"/)
  assert.match(viewSource, /aria-controls="candidate-ranking-panel"/)
  assert.match(viewSource, /@keydown="handleScopeKeydown"/)
  assert.match(viewSource, /ArrowLeft.*ArrowRight.*Home.*End/)
  assert.match(viewSource, /role="tabpanel"/)
  assert.match(viewSource, /:aria-busy="loading \|\| detailLoading"/)
  assert.match(viewSource, /aria-live="polite"/)
  assert.match(viewSource, /<Teleport to="body">/)
  assert.match(viewSource, /role="dialog"/)
  assert.match(viewSource, /aria-labelledby="strategy-dialog-title"/)
  assert.match(viewSource, /@keydown\.esc\.stop\.prevent="closeAction"/)
  assert.match(viewSource, /@keydown\.tab="trapDialogFocus"/)
  assert.match(viewSource, /:inert="strategyAction \? true : undefined"/)
  assert.match(viewSource, /document\.body\.style\.overflow = 'hidden'/)
  assert.match(viewSource, /trigger\?\.focus\(\)/)
  assert.match(viewSource, /button:focus-visible/)
})

test('cockpit includes explicit 1380, 980, and mobile responsive modes', () => {
  assert.match(viewSource, /@media \(max-width: 1380px\)/)
  assert.match(viewSource, /@media \(max-width: 980px\)/)
  assert.match(viewSource, /@media \(max-width: 680px\)/)
  assert.match(viewSource, /\.candidate-inspector \{[^}]*grid-column: 1 \/ -1[^}]*repeat\(4, minmax\(0, 1fr\)\)/)
  assert.match(viewSource, /\.cockpit-workspace \{ grid-template-columns: minmax\(0, 1fr\)/)
  assert.match(viewSource, /\.candidate-table td \{[^}]*font-size: 12px/)
  assert.match(viewSource, /\.scope-tabs button,[\s\S]*min-height: 44px/)
})

test('drawdown uses risk severity rather than A-share return direction', () => {
  assert.match(viewSource, /function toneForDrawdown/)
  assert.match(viewSource, /toneForDrawdown\(evaluation\.max_drawdown\)/)
  assert.doesNotMatch(viewSource, /toneForReturn\(evaluation\.max_drawdown\)/)
  assert.match(viewSource, /\.risk-contained/)
  assert.match(viewSource, /\.risk-watch/)
  assert.match(viewSource, /\.risk-critical/)
})

test('the page states the research boundary and exposes no order integration', () => {
  assert.match(pageSource, /不是对未来涨跌的预测/)
  assert.match(pageSource, /不构成投资建议/)
  for (const source of [viewSource, pageSource]) {
    assert.doesNotMatch(source, /paper_service|place_order|paperOrder|placeOrder/)
  }
  assert.doesNotMatch(apiSource, /automaticSelection(?:Buy|Sell|Order)/)
})

test('return colors follow A-share red-up green-down convention', () => {
  assert.match(viewSource, /return value > 0 \? 'pnl-up' : 'pnl-down'/)
  assert.match(viewSource, /\.pnl-up \{ color: #ff6767/)
  assert.match(viewSource, /\.pnl-down \{ color: #35cb8f/)
})

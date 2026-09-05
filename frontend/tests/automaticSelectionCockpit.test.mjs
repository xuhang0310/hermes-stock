import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'

const viewSource = await readFile(
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

function createViewHarness(apiOverrides = {}) {
  const setupMatch = viewSource.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)
  assert.ok(setupMatch, 'AutomaticSelectionView script setup should be present')
  const exposure = `
;(globalThis as Record<string, unknown>).__automaticSelectionHarness = {
  loadDashboard,
  loadRun,
  selectedRunId,
  runDetail,
  selectedRunDetail,
  selectedOverviewRun,
  currentRun,
  candidates,
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
  const transpiled = ts.transpileModule(`${setupMatch[1]}\n${exposure}`, {
    compilerOptions: {
      module: ts.ModuleKind.ESNext,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText
  const executable = transpiled
    .split(/\r?\n/)
    .filter((line) => !line.startsWith('import ') && !/^export\s*\{\s*\};?$/.test(line))
    .join('\n')

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
    onMounted: () => {},
    ref,
    defineProps: () => ({ user: { permissions: [] } }),
    renderMarkdown: (value) => value,
    beijingFromDb: (value) => value,
  }
  vm.runInNewContext(executable, context)
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

test('cockpit exposes quality gate, deterministic ranking and forward evaluation', () => {
  assert.match(viewSource, /data-testid="automatic-selection-quality-gate"/)
  assert.match(viewSource, /不会静默删除缺失因子后继续排名/)
  assert.match(viewSource, /data-testid="automatic-selection-ranking"/)
  assert.match(viewSource, />Top-N</)
  assert.match(viewSource, />全量合格</)
  assert.match(viewSource, />排除项</)
  assert.match(viewSource, /原始值 \/ z-score/)
  assert.match(viewSource, /data-testid="automatic-selection-evaluations"/)
  assert.match(viewSource, /5 \/ 20 \/ 60 交易日前瞻评估/)
  assert.match(viewSource, /下一交易日开盘进入观察窗口/)
  assert.match(viewSource, /历史行情覆盖/)
  assert.match(viewSource, /qualityGate\.metrics\.history_count/)
  assert.match(viewSource, /qualityGate\.thresholds\.min_history_coverage/)
})

test('cockpit paginates evidence and rejects stale detail responses', () => {
  assert.match(viewSource, /const CANDIDATE_PAGE_SIZE = 50/)
  assert.match(viewSource, /requestSequence !== evidenceRequestSequence/)
  assert.match(viewSource, /const selectedRunDetail = computed/)
  assert.match(viewSource, /runDetail\.value\?\.run_id === selectedRunId\.value/)
  assert.match(viewSource, /overview\.value\?\.latest_run\?\.run_id === selectedRunId\.value/)
  assert.match(viewSource, /CANDIDATE_PAGE_SIZE,[\s\S]*offset/)
  assert.match(viewSource, /data-testid="automatic-selection-pagination"/)
  assert.match(viewSource, /previousCandidatePage/)
  assert.match(viewSource, /nextCandidatePage/)
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

  const pending = harness.loadRun('B')
  assert.equal(harness.selectedRunId.value, 'B')
  assert.equal(harness.runDetail.value, null)
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
  assert.match(harness.error.value, /B detail failed/)
  assert.equal(harness.detailLoading.value, false)
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

  const pending = harness.loadDashboard('B')
  assert.equal(harness.selectedRunId.value, 'B')
  assert.equal(harness.runDetail.value, null)
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

  const first = harness.loadRun('B')
  const second = harness.loadRun('C')
  requests.B.resolve(runEvidence('B'))
  await first
  assert.equal(harness.selectedRunId.value, 'C')
  assert.equal(harness.runDetail.value, null)
  assert.equal(harness.currentRun.value, null)

  requests.C.resolve(runEvidence('C'))
  await second
  assert.equal(harness.selectedRunId.value, 'C')
  assert.equal(harness.selectedRunDetail.value.run_id, 'C')
  assert.equal(harness.currentRun.value.run_id, 'C')
  assert.equal(harness.candidates.value[0].symbol, 'candidate-C')
  assert.equal(harness.evaluations.value[0].run_id, 'C')
  assert.equal(harness.candidateTotal.value, 1)
  assert.equal(harness.detailLoading.value, false)
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
  assert.match(viewSource, /permissions\.includes\('schedule:manage'\)/)
  assert.match(viewSource, /v-if="canManageStrategies"/)
  assert.match(viewSource, /人工核验说明/)
  assert.match(viewSource, /minlength="4"/)
  assert.match(viewSource, /确认人工晋升/)
  assert.match(viewSource, /至少 1 个已到期且覆盖达标的 5 日前瞻窗口/)
  assert.match(viewSource, /不代表统计显著或策略已通过统计验证/)
})

test('cockpit states the research boundary and exposes no order integration', () => {
  assert.match(viewSource, /只做确定性选股与前瞻观察/)
  assert.match(viewSource, /不构成投资建议或交易指令/)
  assert.doesNotMatch(viewSource, /paper_service|place_order|paperOrder|placeOrder/)
  assert.doesNotMatch(apiSource, /automaticSelection(?:Buy|Sell|Order)/)
})

test('return colors follow A-share red-up green-down convention', () => {
  assert.match(viewSource, /return value > 0 \? 'pnl-up' : 'pnl-down'/)
  assert.match(viewSource, /\.pnl-up \{ color: #ff6767/)
  assert.match(viewSource, /\.pnl-down \{ color: #35cb8f/)
})

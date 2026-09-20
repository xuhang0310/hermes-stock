<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { api } from '../api'
import type {
  AutomaticSelectionCandidate,
  AutomaticSelectionLifecycle,
  AutomaticSelectionOverview,
  AutomaticSelectionRun,
  AutomaticSelectionRunDetail,
  AutomaticSelectionStrategy,
  AutomaticSelectionStrategyConfig,
  UserPayload,
} from '../types'
import { renderMarkdown } from '../utils/markdown'
import { beijingFromDb } from '../utils/datetime'

const props = defineProps<{ user: UserPayload }>()

const CANDIDATE_PAGE_SIZE = 50
let evidenceRequestSequence = 0

type CandidateScope = 'top' | 'eligible' | 'excluded'
const candidateScopeOrder: CandidateScope[] = ['top', 'eligible', 'excluded']
type StrategyAction = 'create' | 'promote' | 'retire' | null

const loading = ref(true)
const detailLoading = ref(false)
const actionLoading = ref(false)
const error = ref('')
const notice = ref('')
const overview = ref<AutomaticSelectionOverview | null>(null)
const strategies = ref<AutomaticSelectionStrategy[]>([])
const runDetail = ref<AutomaticSelectionRunDetail | null>(null)
const selectedRunId = ref('')
const selectedCandidateSymbol = ref('')
const candidateScope = ref<CandidateScope>('top')
const strategyAction = ref<StrategyAction>(null)
const actionStrategy = ref<AutomaticSelectionStrategy | null>(null)
const confirmationNote = ref('')
const dialogEl = ref<HTMLElement | null>(null)
let dialogTrigger: HTMLElement | null = null
let previousBodyOverflow = ''
const createForm = ref({
  strategyKey: '',
  name: '',
  lifecycleState: 'challenger' as Exclude<AutomaticSelectionLifecycle, 'official' | 'retired'>,
  parentStrategyId: null as number | null,
  configText: '',
})

const canManageStrategies = computed(
  () => props.user.permissions.includes('all') || props.user.permissions.includes('model:manage')
)
const canDispatch = computed(
  () => props.user.permissions.includes('all') || props.user.permissions.includes('schedule:manage')
)
const selectedRunDetail = computed<AutomaticSelectionRunDetail | null>(() =>
  runDetail.value?.run_id === selectedRunId.value ? runDetail.value : null
)
const selectedOverviewRun = computed<AutomaticSelectionRun | null>(() =>
  overview.value?.latest_run?.run_id === selectedRunId.value ? overview.value.latest_run : null
)
const currentRun = computed<AutomaticSelectionRun | null>(
  () => selectedRunDetail.value || selectedOverviewRun.value
)
const isSelectedLatestRun = computed(
  () => Boolean(selectedRunId.value && overview.value?.latest_run?.run_id === selectedRunId.value)
)
const runContextLabel = computed(() => isSelectedLatestRun.value ? '最新运行' : '历史运行')
const qualityGate = computed(() => currentRun.value?.quality_gate || null)
const candidates = computed<AutomaticSelectionCandidate[]>(() => {
  if (selectedRunDetail.value) return selectedRunDetail.value.candidates
  if (candidateScope.value === 'top' && selectedOverviewRun.value) {
    return overview.value?.top_candidates || []
  }
  return []
})
const selectedCandidate = computed<AutomaticSelectionCandidate | null>(() =>
  candidates.value.find((candidate) => candidate.symbol === selectedCandidateSymbol.value) || null
)
const sortedStrategies = computed(() => [...strategies.value].sort((left, right) => {
  if (left.is_official !== right.is_official) return left.is_official ? -1 : 1
  return left.strategy_key.localeCompare(right.strategy_key) || right.version - left.version
}))
const selectableRuns = computed<AutomaticSelectionRun[]>(() => {
  const result: AutomaticSelectionRun[] = []
  const seen = new Set<string>()
  const latest = overview.value?.latest_run
  if (latest) {
    result.push(latest)
    seen.add(latest.run_id)
  }
  for (const run of overview.value?.recent_runs || []) {
    if (!seen.has(run.run_id)) result.push(run)
    seen.add(run.run_id)
  }
  return result
})
const explanationHtml = computed(() => renderMarkdown(currentRun.value?.llm_explanation || ''))
const factorCoverage = computed(() => Object.entries(qualityGate.value?.metrics.factor_coverage || {}))
const exclusionEvidence = computed(() => Object.entries(currentRun.value?.exclusion_summary || {})
  .filter(([, count]) => count > 0)
  .sort((left, right) => right[1] - left[1]))
const evaluations = computed(() => {
  if (selectedRunDetail.value) return selectedRunDetail.value.evaluations
  return selectedOverviewRun.value ? overview.value?.evaluations || [] : []
})
const candidateTotal = computed(() => selectedRunDetail.value?.candidate_total ?? candidates.value.length)
const candidateLimit = computed(() => selectedRunDetail.value?.candidate_limit || CANDIDATE_PAGE_SIZE)
const candidateOffset = computed(() => selectedRunDetail.value?.candidate_offset || 0)
const candidatePage = computed(() => Math.floor(candidateOffset.value / candidateLimit.value) + 1)
const candidatePageCount = computed(() => Math.max(1, Math.ceil(candidateTotal.value / candidateLimit.value)))
const candidateRange = computed(() => {
  if (!candidateTotal.value) return '0'
  const start = candidateOffset.value + 1
  const end = Math.min(candidateOffset.value + candidates.value.length, candidateTotal.value)
  return `${start}–${end}`
})

const lifecycleLabels: Record<AutomaticSelectionLifecycle, string> = {
  official: '正式',
  challenger: '挑战者',
  shadow: '影子',
  retired: '已退役',
}
const statusLabels: Record<string, string> = {
  running: '运行中',
  completed: '已完成',
  blocked: '质量阻断',
  failed: '运行失败',
  pending: '待到期',
  insufficient_data: '覆盖不足',
}
const stageLabels: Record<string, string> = {
  quality_gate: '质量门禁',
  ranked: '排名已落库',
  failed: '执行失败',
}
const changeLabels: Record<string, string> = {
  added: '新增',
  retained: '保留',
  removed: '移除',
  unchanged: '未变化',
}
const exclusionLabels: Record<string, string> = {
  not_listed: '非当前上市状态',
  st: 'ST 类标的',
  listing_too_short: '上市时间不足',
  target_bar_missing: '目标日行情缺失',
  daily_basic_missing: '基础指标缺失',
  valuation_invalid: '估值指标非法',
  history_insufficient: '历史行情不足',
  liquidity_low: '流动性不足',
  required_factor_missing: '必需因子缺失',
  not_in_current_catalog: '不在本期目录',
}
const constraintLabels: Record<string, string> = {
  industry_missing: '行业信息缺失',
  industry_limit: '行业集中度约束',
  market_cap_bucket_limit: '市值集中度约束',
}
const qualityLabels: Record<string, string> = {
  bar_coverage: '目标日行情覆盖',
  basic_coverage: '基础指标覆盖',
  industry_coverage: '行业信息覆盖',
  history_coverage: '历史行情覆盖',
  eligible_count: '最终合格池',
  strategy_error: '策略执行异常',
}
const featureLabels: Record<string, string> = {
  value_pe: 'PE 价值',
  value_pb: 'PB 价值',
  momentum_20: '20 日动量',
  liquidity_20: '20 日流动性',
  low_volatility_20: '低波动',
}

function apiMessage(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason)
}

function fmtTradeDate(value: string | null | undefined): string {
  if (!value) return '—'
  const digits = value.replace(/-/g, '')
  if (digits.length === 8) return `${digits.slice(0, 4)}-${digits.slice(4, 6)}-${digits.slice(6, 8)}`
  return value
}

function fmtNumber(value: number | null | undefined, digits = 2): string {
  if (value == null || !Number.isFinite(value)) return '—'
  return value.toLocaleString('zh-CN', { maximumFractionDigits: digits, minimumFractionDigits: digits })
}

function fmtPercent(value: number | null | undefined, digits = 2): string {
  if (value == null || !Number.isFinite(value)) return '—'
  return `${(value * 100).toFixed(digits)}%`
}

function fmtCoverage(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '—'
  return `${(value * 100).toFixed(1)}%`
}

function fmtMarketCap(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '—'
  return `${fmtNumber(value / 10_000, 1)} 亿`
}

function fmtAmount(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '—'
  return `${fmtNumber(value / 100_000, 2)} 亿元`
}

function toneForReturn(value: number | null | undefined): string {
  if (value == null || value === 0) return ''
  return value > 0 ? 'pnl-up' : 'pnl-down'
}

function toneForDrawdown(value: number | null | undefined): string {
  if (value == null) return ''
  const magnitude = Math.abs(value)
  if (magnitude >= 0.2) return 'risk-critical'
  if (magnitude >= 0.1) return 'risk-watch'
  return 'risk-contained'
}

function toneForStatus(status: string): string {
  if (status === 'completed') return 'tone-ok'
  if (status === 'blocked' || status === 'insufficient_data') return 'tone-warning'
  if (status === 'failed') return 'tone-danger'
  return 'tone-muted'
}

function lifecycleLabel(state: string): string {
  return lifecycleLabels[state as AutomaticSelectionLifecycle] || state
}

function codeLabel(code: string): string {
  return exclusionLabels[code] || constraintLabels[code] || code
}

function featureLabel(feature: string): string {
  return featureLabels[feature] || feature.replace(/^tushare:/, '')
}

function metricActual(code: string): number | undefined {
  const metrics = qualityGate.value?.metrics
  if (!metrics) return undefined
  if (code.startsWith('factor_coverage:')) {
    return metrics.factor_coverage?.[code.slice('factor_coverage:'.length)]
  }
  const value = metrics[code as keyof typeof metrics]
  return typeof value === 'number' ? value : undefined
}

function metricRequired(code: string): number | undefined {
  const thresholds = qualityGate.value?.thresholds || {}
  const thresholdKey: Record<string, string> = {
    bar_coverage: 'min_bar_coverage',
    basic_coverage: 'min_basic_coverage',
    industry_coverage: 'min_industry_coverage',
    history_coverage: 'min_history_coverage',
    eligible_count: 'min_eligible_count',
  }
  if (code.startsWith('factor_coverage:')) return thresholds.min_factor_coverage
  return thresholds[thresholdKey[code] || code]
}

const qualityCountCodes = new Set([
  'eligible_count',
  'catalog_count',
  'bar_count',
  'basic_count',
  'industry_count',
  'history_count',
])

function fmtQualityValue(code: string, value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '—'
  return qualityCountCodes.has(code) ? fmtNumber(value, 0) : fmtCoverage(value)
}

function clearCandidateSelection() {
  selectedCandidateSymbol.value = ''
}

function seedCurrentCandidate() {
  selectedCandidateSymbol.value = candidates.value[0]?.symbol || ''
}

function selectCandidate(symbol: string) {
  if (candidates.value.some((candidate) => candidate.symbol === symbol)) {
    selectedCandidateSymbol.value = symbol
  }
}

async function loadDashboard(preferredRunId?: string) {
  const requestSequence = ++evidenceRequestSequence
  loading.value = true
  detailLoading.value = false
  error.value = ''
  runDetail.value = null
  clearCandidateSelection()
  if (preferredRunId !== undefined) selectedRunId.value = preferredRunId
  try {
    const [nextOverview, nextStrategies] = await Promise.all([
      api.automaticSelectionOverview(),
      api.automaticSelectionStrategies(true),
    ])
    if (requestSequence !== evidenceRequestSequence) return
    overview.value = nextOverview
    strategies.value = nextStrategies
    loading.value = false
    const runId = preferredRunId ?? nextOverview.latest_run?.run_id ?? ''
    if (runId) {
      await loadRun(runId, candidateScope.value, 0, true, requestSequence)
    } else {
      selectedRunId.value = ''
      runDetail.value = null
      clearCandidateSelection()
    }
  } catch (reason) {
    if (requestSequence === evidenceRequestSequence) error.value = apiMessage(reason)
  } finally {
    if (requestSequence === evidenceRequestSequence) loading.value = false
  }
}

async function loadRun(
  runId: string,
  scope: CandidateScope = candidateScope.value,
  offset = 0,
  showSpinner = true,
  requestSequence?: number
) {
  if (!runId) return
  const activeRequestSequence = requestSequence ?? ++evidenceRequestSequence
  runDetail.value = null
  selectedRunId.value = runId
  candidateScope.value = scope
  clearCandidateSelection()
  if (showSpinner) detailLoading.value = true
  error.value = ''
  try {
    const detail = await api.automaticSelectionRunDetail(
      runId,
      scope,
      CANDIDATE_PAGE_SIZE,
      offset
    )
    if (activeRequestSequence !== evidenceRequestSequence) return
    runDetail.value = detail
    seedCurrentCandidate()
  } catch (reason) {
    if (activeRequestSequence === evidenceRequestSequence) error.value = apiMessage(reason)
  } finally {
    if (activeRequestSequence === evidenceRequestSequence) detailLoading.value = false
  }
}

async function selectScope(scope: CandidateScope) {
  if (scope === candidateScope.value || !selectedRunId.value) return
  await loadRun(selectedRunId.value, scope, 0)
}

async function handleScopeKeydown(event: KeyboardEvent) {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
  event.preventDefault()
  const currentIndex = candidateScopeOrder.indexOf(candidateScope.value)
  const nextIndex = event.key === 'Home'
    ? 0
    : event.key === 'End'
      ? candidateScopeOrder.length - 1
      : (currentIndex + (event.key === 'ArrowRight' ? 1 : -1) + candidateScopeOrder.length)
        % candidateScopeOrder.length
  const nextScope = candidateScopeOrder[nextIndex]
  await selectScope(nextScope)
  if (candidateScope.value !== nextScope) return
  await nextTick()
  if (typeof document !== 'undefined') {
    document.getElementById(`candidate-scope-${nextScope}`)?.focus()
  }
}

async function previousCandidatePage() {
  if (!selectedRunId.value || candidateOffset.value <= 0) return
  await loadRun(
    selectedRunId.value,
    candidateScope.value,
    Math.max(0, candidateOffset.value - candidateLimit.value)
  )
}

async function nextCandidatePage() {
  const nextOffset = candidateOffset.value + candidateLimit.value
  if (!selectedRunId.value || nextOffset >= candidateTotal.value) return
  await loadRun(selectedRunId.value, candidateScope.value, nextOffset)
}

async function dispatchRun() {
  if (!canDispatch.value || actionLoading.value || loading.value || detailLoading.value) return
  actionLoading.value = true
  error.value = ''
  notice.value = ''
  try {
    const result = await api.automaticSelectionDispatch()
    evidenceRequestSequence += 1
    selectedRunId.value = ''
    runDetail.value = null
    clearCandidateSelection()
    candidateScope.value = 'top'
    detailLoading.value = false
    loading.value = false
    notice.value = `任务已投递，运行编号 ${result.job_run_id}。完成后可刷新查看不可变证据。`
  } catch (reason) {
    error.value = apiMessage(reason)
  } finally {
    actionLoading.value = false
  }
}

function openActionDialog(action: Exclude<StrategyAction, null>) {
  if (actionLoading.value) return
  if (typeof document !== 'undefined') {
    dialogTrigger = document.activeElement instanceof HTMLElement ? document.activeElement : null
    previousBodyOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
  }
  strategyAction.value = action
  void nextTick(() => dialogEl.value?.focus())
}

function dismissActionDialog(restoreFocus = true) {
  strategyAction.value = null
  actionStrategy.value = null
  confirmationNote.value = ''
  if (typeof document !== 'undefined') {
    document.body.style.overflow = previousBodyOverflow
  }
  const trigger = dialogTrigger
  dialogTrigger = null
  if (restoreFocus) void nextTick(() => trigger?.focus())
}

function trapDialogFocus(event: KeyboardEvent) {
  const dialog = dialogEl.value
  if (!dialog || typeof document === 'undefined') return
  const focusable = Array.from(dialog.querySelectorAll<HTMLElement>(
    'button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])'
  ))
  if (!focusable.length) {
    event.preventDefault()
    dialog.focus()
    return
  }
  const first = focusable[0]
  const last = focusable[focusable.length - 1]
  if (event.shiftKey && (document.activeElement === first || !dialog.contains(document.activeElement))) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first.focus()
  }
}

function openCreate(strategy?: AutomaticSelectionStrategy) {
  if (!canManageStrategies.value || actionLoading.value) return
  const source = strategy || overview.value?.official_strategy || strategies.value[0]
  createForm.value = {
    strategyKey: source?.strategy_key || '',
    name: source ? `${source.name}（新版本）` : '',
    lifecycleState: 'challenger',
    parentStrategyId: source?.id || null,
    configText: JSON.stringify(source?.config || {}, null, 2),
  }
  actionStrategy.value = source || null
  confirmationNote.value = ''
  openActionDialog('create')
}

function openPromote(strategy: AutomaticSelectionStrategy) {
  if (actionLoading.value) return
  actionStrategy.value = strategy
  confirmationNote.value = ''
  openActionDialog('promote')
}

function openRetire(strategy: AutomaticSelectionStrategy) {
  if (actionLoading.value) return
  actionStrategy.value = strategy
  confirmationNote.value = ''
  openActionDialog('retire')
}

function closeAction() {
  if (actionLoading.value) return
  dismissActionDialog()
}

async function submitStrategyAction() {
  if (!strategyAction.value || actionLoading.value) return
  actionLoading.value = true
  error.value = ''
  notice.value = ''
  try {
    if (strategyAction.value === 'create') {
      const parsed = JSON.parse(createForm.value.configText) as AutomaticSelectionStrategyConfig
      const created = await api.automaticSelectionCreateStrategy({
        strategy_key: createForm.value.strategyKey.trim(),
        name: createForm.value.name.trim(),
        config: parsed,
        lifecycle_state: createForm.value.lifecycleState,
        parent_strategy_id: createForm.value.parentStrategyId,
      })
      notice.value = `已创建 ${created.name} v${created.version}，原版本保持不变。`
    } else if (strategyAction.value === 'promote' && actionStrategy.value) {
      const promoted = await api.automaticSelectionPromoteStrategy(
        actionStrategy.value.id,
        confirmationNote.value.trim()
      )
      notice.value = `已人工晋升 ${promoted.name} v${promoted.version}。`
    } else if (strategyAction.value === 'retire' && actionStrategy.value) {
      const retired = await api.automaticSelectionRetireStrategy(
        actionStrategy.value.id,
        confirmationNote.value.trim() || null
      )
      notice.value = `已退役 ${retired.name} v${retired.version}，历史证据仍保留。`
    }
    dismissActionDialog(false)
    await loadDashboard(selectedRunId.value)
    await nextTick()
    if (typeof document !== 'undefined') {
      document.querySelector<HTMLElement>(
        '[data-testid="automatic-selection-dispatch"], [data-testid="automatic-selection-command"] button'
      )?.focus()
    }
  } catch (reason) {
    error.value = reason instanceof SyntaxError ? '策略配置不是有效 JSON' : apiMessage(reason)
  } finally {
    actionLoading.value = false
  }
}

onMounted(() => void loadDashboard())
onBeforeUnmount(() => {
  if (typeof document !== 'undefined') document.body.style.overflow = previousBodyOverflow
})
</script>

<template>
  <section
    class="automatic-selection-page"
    aria-labelledby="automatic-selection-title"
    :aria-busy="loading || detailLoading"
    :inert="strategyAction ? true : undefined"
  >
    <header class="cockpit-command" data-testid="automatic-selection-command">
      <div class="command-identity">
        <div class="command-title-line">
          <h1 id="automatic-selection-title">自动选股研究驾驶舱</h1>
          <span v-if="currentRun" class="run-context" :class="{ historical: !isSelectedLatestRun }">
            {{ runContextLabel }}
          </span>
        </div>
        <p>确定性筛选、质量门禁与前瞻验证的一体化研究台</p>
      </div>

      <div v-if="currentRun" class="command-run" aria-label="当前运行摘要">
        <div>
          <span>目标交易日</span>
          <strong>{{ fmtTradeDate(currentRun.target_trade_date) }}</strong>
        </div>
        <div>
          <span>策略版本</span>
          <strong>{{ currentRun.strategy_name }} · v{{ currentRun.strategy_version }}</strong>
        </div>
        <div>
          <span>运行状态</span>
          <span class="status-pill" :class="toneForStatus(currentRun.status)">
            {{ statusLabels[currentRun.status] || currentRun.status }}
          </span>
        </div>
      </div>

      <div class="command-actions">
        <button class="auto-button auto-button--ghost" type="button" @click="loadDashboard()">
          刷新证据
        </button>
        <button
          v-if="canDispatch"
          class="auto-button auto-button--primary"
          type="button"
          :disabled="actionLoading || loading || detailLoading"
          data-testid="automatic-selection-dispatch"
          @click="dispatchRun"
        >
          {{ actionLoading ? '正在投递...' : '运行研究流水线' }}
        </button>
      </div>
    </header>

    <div class="research-boundary" role="note">
      <span class="boundary-mark">研究边界</span>
      <strong>只做确定性选股与前瞻观察</strong>
      <span>{{ overview?.limitations || '仅用于上线后的研究观察，不构成投资建议或交易指令。' }}</span>
    </div>

    <div class="message-stack" aria-live="polite" aria-atomic="true">
      <div v-if="error" class="auto-notice auto-notice--danger" role="alert">{{ error }}</div>
      <div v-if="notice" class="auto-notice auto-notice--success" role="status">{{ notice }}</div>
      <div v-if="currentRun?.error" class="run-error" role="alert">
        <strong>当前运行错误</strong>
        <span>{{ currentRun.error }}</span>
      </div>
    </div>

    <div v-if="loading" class="page-section auto-loading" role="status" aria-live="polite">
      正在读取自动选股研究账本...
    </div>

    <template v-else>
      <section class="kpi-strip" aria-label="运行关键指标" data-testid="automatic-selection-kpis">
        <div>
          <span>研究池</span>
          <strong>{{ currentRun?.universe_count ?? '—' }}</strong>
          <small>全量输入标的</small>
        </div>
        <div>
          <span>合格池</span>
          <strong>{{ currentRun?.eligible_count ?? '—' }}</strong>
          <small>通过硬性规则</small>
        </div>
        <div>
          <span>Top-N</span>
          <strong>{{ currentRun?.selected_count ?? '—' }}</strong>
          <small>研究观察集</small>
        </div>
        <div>
          <span>质量门禁</span>
          <strong :class="qualityGate?.passed ? 'kpi-ok' : qualityGate ? 'kpi-warning' : ''">
            {{ qualityGate ? (qualityGate.passed ? '通过' : '阻断') : '—' }}
          </strong>
          <small>{{ qualityGate?.failures.length ?? 0 }} 项阻断证据</small>
        </div>
        <div>
          <span>入选换手</span>
          <strong>{{ fmtPercent(currentRun?.selected_turnover) }}</strong>
          <small>相邻观察集变化</small>
        </div>
        <div>
          <span>运行阶段</span>
          <strong class="kpi-stage">{{ currentRun ? (stageLabels[currentRun.stage] || currentRun.stage) : '—' }}</strong>
          <small>{{ currentRun ? beijingFromDb(currentRun.finished_at || currentRun.started_at) : '等待首轮运行' }}</small>
        </div>
      </section>

      <section class="cockpit-workspace" data-testid="automatic-selection-workspace">
        <aside class="run-rail cockpit-panel" aria-labelledby="run-selector-title">
          <header class="panel-heading">
            <div>
              <h2 id="run-selector-title">运行选择</h2>
            </div>
            <span>{{ selectableRuns.length }}</span>
          </header>

          <div v-if="selectableRuns.length" class="run-selector" role="listbox" aria-label="近期研究运行">
            <button
              v-for="run in selectableRuns"
              :key="run.run_id"
              type="button"
              role="option"
              class="run-option"
              :class="{ selected: selectedRunId === run.run_id }"
              :aria-selected="selectedRunId === run.run_id"
              @click="loadRun(run.run_id)"
            >
              <span class="run-option-top">
                <b>{{ fmtTradeDate(run.target_trade_date) }}</b>
                <em :class="{ historical: overview?.latest_run?.run_id !== run.run_id }">
                  {{ overview?.latest_run?.run_id === run.run_id ? '最新运行' : '历史运行' }}
                </em>
              </span>
              <span>{{ run.strategy_name }} · v{{ run.strategy_version }}</span>
              <span class="run-option-bottom">
                <i :class="toneForStatus(run.status)">{{ statusLabels[run.status] || run.status }}</i>
                <code>{{ run.run_id.slice(0, 8) }}</code>
              </span>
            </button>
          </div>
          <div v-else class="empty-state compact-empty">暂无运行账本</div>

          <section class="rail-evidence" data-testid="automatic-selection-quality-gate" aria-labelledby="quality-gate-title">
            <header class="rail-section-title">
              <div>
                <h3 id="quality-gate-title">质量与排除证据</h3>
              </div>
              <span v-if="qualityGate" class="gate-dot" :class="qualityGate.passed ? 'gate-pass' : 'gate-blocked'">
                {{ qualityGate.passed ? '通过' : '阻断' }}
              </span>
            </header>
            <p class="gate-policy">覆盖率不足时整条策略被阻断，不会静默删除缺失因子后继续排名。</p>

            <dl v-if="qualityGate" class="quality-ledger">
              <div>
                <dt>目标日行情覆盖</dt>
                <dd>{{ fmtCoverage(qualityGate.metrics.bar_coverage) }} <small>/ {{ fmtCoverage(qualityGate.thresholds.min_bar_coverage) }}</small></dd>
              </div>
              <div>
                <dt>基础指标覆盖</dt>
                <dd>{{ fmtCoverage(qualityGate.metrics.basic_coverage) }} <small>/ {{ fmtCoverage(qualityGate.thresholds.min_basic_coverage) }}</small></dd>
              </div>
              <div>
                <dt>行业信息覆盖</dt>
                <dd>{{ fmtCoverage(qualityGate.metrics.industry_coverage) }} <small>/ {{ fmtCoverage(qualityGate.thresholds.min_industry_coverage) }}</small></dd>
              </div>
              <div>
                <dt>历史行情覆盖</dt>
                <dd>
                  {{ fmtCoverage(qualityGate.metrics.history_coverage) }} <small>/ {{ fmtCoverage(qualityGate.thresholds.min_history_coverage) }}</small>
                  <small class="quality-detail">{{ qualityGate.metrics.history_count ?? 0 }} 只历史完整</small>
                </dd>
              </div>
              <div>
                <dt>最终合格池</dt>
                <dd>{{ qualityGate.metrics.eligible_count ?? 0 }} <small>/ {{ qualityGate.thresholds.min_eligible_count ?? 0 }}</small></dd>
              </div>
              <div v-for="[factor, coverage] in factorCoverage" :key="factor">
                <dt>{{ featureLabel(factor) }} 覆盖</dt>
                <dd>{{ fmtCoverage(coverage) }} <small>/ {{ fmtCoverage(qualityGate.thresholds.min_factor_coverage) }}</small></dd>
              </div>
            </dl>
            <div v-else class="empty-state compact-empty">首轮运行后展示数据完整性判定。</div>

            <div v-if="qualityGate?.failures.length" class="gate-failures">
              <strong>阻断证据</strong>
              <span v-for="failure in qualityGate.failures" :key="failure.code">
                {{ qualityLabels[failure.code] || featureLabel(failure.code.replace('factor_coverage:', '')) }}：
                实际 {{ fmtQualityValue(failure.code, failure.actual ?? metricActual(failure.code)) }}，要求 {{ fmtQualityValue(failure.code, failure.required ?? metricRequired(failure.code)) }}
                {{ failure.message || '' }}
              </span>
            </div>

            <div class="exclusion-ledger">
              <h4>排除分布</h4>
              <div v-if="exclusionEvidence.length">
                <span v-for="[code, count] in exclusionEvidence" :key="code">
                  <b>{{ codeLabel(code) }}</b><strong>{{ count }}</strong>
                </span>
              </div>
              <p v-else>当前运行没有可展示的排除汇总。</p>
            </div>
          </section>
        </aside>

        <section
          class="ranking-console cockpit-panel"
          data-testid="automatic-selection-ranking"
          aria-labelledby="ranking-title"
          :aria-busy="detailLoading"
        >
          <header class="ranking-toolbar">
            <div>
              <h2 id="ranking-title">候选排名</h2>
              <p>按服务端名次与入选状态稳定呈现，页面不做二次排序。</p>
            </div>
            <div class="ranking-page-token" aria-label="当前服务端分页范围">
              <span>当前页</span>
              <strong>{{ candidateRange }}</strong>
              <small>/ {{ candidateTotal }}</small>
            </div>
          </header>

          <div class="scope-tabs" role="tablist" aria-label="候选范围" @keydown="handleScopeKeydown">
            <button
              id="candidate-scope-top"
              type="button"
              role="tab"
              aria-controls="candidate-ranking-panel"
              :aria-selected="candidateScope === 'top'"
              :tabindex="candidateScope === 'top' ? 0 : -1"
              :class="{ active: candidateScope === 'top' }"
              @click="selectScope('top')"
            >Top-N</button>
            <button
              id="candidate-scope-eligible"
              type="button"
              role="tab"
              aria-controls="candidate-ranking-panel"
              :aria-selected="candidateScope === 'eligible'"
              :tabindex="candidateScope === 'eligible' ? 0 : -1"
              :class="{ active: candidateScope === 'eligible' }"
              @click="selectScope('eligible')"
            >全量合格</button>
            <button
              id="candidate-scope-excluded"
              type="button"
              role="tab"
              aria-controls="candidate-ranking-panel"
              :aria-selected="candidateScope === 'excluded'"
              :tabindex="candidateScope === 'excluded' ? 0 : -1"
              :class="{ active: candidateScope === 'excluded' }"
              @click="selectScope('excluded')"
            >排除项</button>
          </div>

          <div
            id="candidate-ranking-panel"
            class="ranking-panel"
            role="tabpanel"
            :aria-labelledby="`candidate-scope-${candidateScope}`"
          >
            <div class="rank-boundary" role="note">
              {{ selectedRunDetail
                ? '固定 50 行服务端分页；仅选择当前页标的，不在本地跨页重排。'
                : '运行详情暂不可用，当前仅展示首页摘要候选，不在本地重排。' }}
            </div>
            <div v-if="detailLoading" class="auto-loading auto-loading--inline" role="status" aria-live="polite">
              正在切换证据范围...
            </div>
            <div v-else-if="candidates.length" class="candidate-table-wrap">
              <table class="candidate-table">
                <thead>
                  <tr>
                    <th class="rank-column">{{ candidateScope === 'excluded' ? '序号' : '排名' }}</th>
                    <th>标的</th>
                    <th class="num">目标日收盘 / 涨跌</th>
                    <th>行业 / 市值</th>
                    <template v-if="candidateScope === 'excluded'">
                      <th colspan="2" class="exclusion-column">主要排除原因</th>
                    </template>
                    <template v-else>
                      <th class="num">综合分</th>
                      <th class="num">百分位</th>
                    </template>
                    <th class="status-column">状态</th>
                  </tr>
                </thead>
                <tbody>
                  <tr
                    v-for="(item, itemIndex) in candidates"
                    :key="item.symbol"
                    v-memo="[item, selectedCandidateSymbol === item.symbol, candidateScope]"
                    class="candidate-row"
                    :class="{
                      selected: selectedCandidateSymbol === item.symbol,
                      'candidate-excluded': !item.eligible,
                    }"
                  >
                    <td class="rank-cell"><strong>{{ candidateScope === 'excluded' ? candidateOffset + itemIndex + 1 : (item.rank ?? '—') }}</strong></td>
                    <td>
                      <button
                        type="button"
                        class="candidate-button"
                        :aria-pressed="selectedCandidateSymbol === item.symbol"
                        :aria-controls="selectedCandidateSymbol === item.symbol ? 'candidate-evidence-inspector' : undefined"
                        @click="selectCandidate(item.symbol)"
                      >
                        <strong>{{ item.name || item.symbol }}</strong>
                        <small>{{ item.symbol }}</small>
                      </button>
                    </td>
                    <td class="num">
                      <strong>{{ fmtNumber(item.close_price) }}</strong>
                      <small :class="toneForReturn(item.pct_chg)">{{ item.pct_chg == null ? '—' : `${fmtNumber(item.pct_chg)}%` }}</small>
                    </td>
                    <td>
                      {{ item.industry || '未知' }}
                      <small>{{ item.market_cap_bucket || '—' }} · {{ fmtMarketCap(item.total_mv) }}</small>
                    </td>
                    <td v-if="candidateScope === 'excluded'" colspan="2" class="exclusion-cell">
                      <span v-for="code in item.exclusion_codes.slice(0, 2)" :key="code">{{ codeLabel(code) }}</span>
                      <small v-if="item.exclusion_codes.length > 2">另有 {{ item.exclusion_codes.length - 2 }} 项</small>
                    </td>
                    <template v-else>
                      <td class="num score-cell">{{ fmtNumber(item.composite_score, 4) }}</td>
                      <td class="num">{{ fmtPercent(item.percentile) }}</td>
                    </template>
                    <td>
                      <span class="change-pill" :class="`change-${item.change_state}`">
                        {{ changeLabels[item.change_state] || item.change_state }}
                      </span>
                      <small>{{ item.eligible ? (item.selected ? '入选观察集' : '合格未入选') : '规则排除' }}</small>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div v-else class="empty-state">当前范围暂无候选证据。</div>
          </div>

          <nav
            v-if="!detailLoading && selectedRunDetail && candidateTotal > 0"
            class="candidate-pagination"
            aria-label="候选证据服务端分页"
            data-testid="automatic-selection-pagination"
          >
            <span>共 {{ candidateTotal }} 条 · 第 {{ candidatePage }} / {{ candidatePageCount }} 页</span>
            <div>
              <button
                class="auto-button auto-button--ghost"
                type="button"
                :disabled="candidateOffset <= 0"
                @click="previousCandidatePage"
              >
                上一页
              </button>
              <button
                class="auto-button auto-button--ghost"
                type="button"
                :disabled="candidateOffset + candidateLimit >= candidateTotal"
                @click="nextCandidatePage"
              >
                下一页
              </button>
            </div>
          </nav>
        </section>

        <aside
          id="candidate-evidence-inspector"
          class="candidate-inspector cockpit-panel"
          aria-labelledby="candidate-inspector-title"
          aria-live="polite"
        >
          <header class="panel-heading">
            <div>
              <h2 id="candidate-inspector-title">标的证据检查器</h2>
            </div>
            <span v-if="selectedCandidate">#{{ selectedCandidate.rank ?? '—' }}</span>
          </header>

          <template v-if="selectedCandidate">
            <section class="inspector-identity">
              <div>
                <strong>{{ selectedCandidate.name || selectedCandidate.symbol }}</strong>
                <code>{{ selectedCandidate.symbol }}</code>
              </div>
              <span class="change-pill" :class="`change-${selectedCandidate.change_state}`">
                {{ changeLabels[selectedCandidate.change_state] || selectedCandidate.change_state }}
              </span>
              <dl>
                <div><dt>综合分</dt><dd>{{ fmtNumber(selectedCandidate.composite_score, 4) }}</dd></div>
                <div><dt>百分位</dt><dd>{{ fmtPercent(selectedCandidate.percentile) }}</dd></div>
                <div><dt>研究权重</dt><dd>{{ fmtPercent(selectedCandidate.research_weight) }}</dd></div>
                <div><dt>20 日动量</dt><dd :class="toneForReturn(selectedCandidate.momentum_20)">{{ fmtPercent(selectedCandidate.momentum_20) }}</dd></div>
              </dl>
            </section>

            <section class="inspector-block rule-evidence">
              <h3>规则判定</h3>
              <div v-if="selectedCandidate.exclusion_codes.length || selectedCandidate.constraint_codes.length" class="code-list">
                <span v-for="code in [...selectedCandidate.exclusion_codes, ...selectedCandidate.constraint_codes]" :key="code">
                  {{ codeLabel(code) }}
                </span>
              </div>
              <p v-else class="positive-evidence">未记录排除项或组合约束。</p>
            </section>

            <section class="inspector-block factor-evidence">
              <h3>原始值 / z-score</h3>
              <div v-if="Object.keys(selectedCandidate.factor_values).length || Object.keys(selectedCandidate.z_scores).length" class="factor-ledger">
                <div v-for="(value, key) in selectedCandidate.factor_values" :key="`raw-${key}`">
                  <span>{{ featureLabel(String(key)) }}</span><strong>{{ fmtNumber(value, 4) }}</strong><small>raw</small>
                </div>
                <div v-for="(value, key) in selectedCandidate.z_scores" :key="`z-${key}`">
                  <span>{{ featureLabel(String(key)) }}</span><strong>{{ fmtNumber(value, 4) }}</strong><small>z</small>
                </div>
              </div>
              <p v-else>暂无扩展因子证据。</p>
            </section>

            <section class="inspector-block market-evidence">
              <h3>行情与输入快照</h3>
              <dl>
                <div><dt>收盘价</dt><dd>{{ fmtNumber(selectedCandidate.close_price) }}</dd></div>
                <div><dt>PE / PB</dt><dd>{{ fmtNumber(selectedCandidate.pe_ttm) }} / {{ fmtNumber(selectedCandidate.pb) }}</dd></div>
                <div><dt>总市值</dt><dd>{{ fmtMarketCap(selectedCandidate.total_mv) }}</dd></div>
                <div><dt>20 日均成交额</dt><dd>{{ fmtAmount(selectedCandidate.avg_amount_20) }}</dd></div>
                <div><dt>20 日波动</dt><dd>{{ fmtPercent(selectedCandidate.volatility_20) }}</dd></div>
                <div><dt>历史点位</dt><dd>{{ selectedCandidate.input_evidence?.history?.length ?? 0 }} / {{ selectedCandidate.input_evidence?.expected_history_dates?.length ?? 0 }}</dd></div>
              </dl>
            </section>
          </template>
          <div v-else class="empty-state inspector-empty">
            <strong>未选择当前页标的</strong>
            <span>运行、范围或分页切换时会清空旧选择；新证据到达后自动选择当前页首项。</span>
          </div>
        </aside>
      </section>

      <section class="validation-section" aria-labelledby="validation-title" data-testid="automatic-selection-validation">
        <header class="governance-heading">
          <div>
            <h2 id="validation-title">前瞻验证与落库后解释</h2>
          </div>
          <p>确定性排名先落库，统计观察与模型解释均不能回写排名。</p>
        </header>

        <div class="validation-grid">
          <article class="validation-panel evaluation-section" data-testid="automatic-selection-evaluations">
            <header class="validation-panel-header">
              <div>
                <h3>5 / 20 / 60 交易日前瞻评估</h3>
                <p>T 日收盘后选择，下一交易日开盘进入观察窗口。</p>
              </div>
            </header>
            <div v-if="evaluations.length" class="evaluation-grid">
              <article v-for="evaluation in evaluations" :key="evaluation.horizon">
                <header>
                  <strong>{{ evaluation.horizon }} 日</strong>
                  <span class="status-pill" :class="toneForStatus(evaluation.status)">{{ statusLabels[evaluation.status] || evaluation.status }}</span>
                </header>
                <dl>
                  <div><dt>组合收益</dt><dd :class="toneForReturn(evaluation.basket_return)">{{ fmtPercent(evaluation.basket_return) }}</dd></div>
                  <div><dt>基准收益</dt><dd :class="toneForReturn(evaluation.benchmark_return)">{{ fmtPercent(evaluation.benchmark_return) }}</dd></div>
                  <div><dt>超额收益</dt><dd :class="toneForReturn(evaluation.excess_return)">{{ fmtPercent(evaluation.excess_return) }}</dd></div>
                  <div><dt>覆盖率</dt><dd>{{ fmtCoverage(evaluation.coverage_ratio) }}</dd></div>
                  <div><dt>最大回撤</dt><dd :class="toneForDrawdown(evaluation.max_drawdown)">{{ fmtPercent(evaluation.max_drawdown) }}</dd></div>
                  <div><dt>Sharpe / IR</dt><dd>{{ fmtNumber(evaluation.sharpe) }} / {{ fmtNumber(evaluation.information_ratio) }}</dd></div>
                </dl>
                <small>{{ fmtTradeDate(evaluation.entry_trade_date) }} → {{ fmtTradeDate(evaluation.exit_trade_date) }}</small>
                <p v-if="evaluation.error" class="evaluation-error">{{ evaluation.error }}</p>
              </article>
            </div>
            <div v-else class="empty-state">运行完成后创建 5 / 20 / 60 日评估窗口。</div>
          </article>

          <article class="validation-panel explanation-section">
            <header class="validation-panel-header">
              <div>
                <h3>落库后模型解释</h3>
                <p>模型仅解释已落库内容，不参与评分、排名或策略晋升。</p>
              </div>
              <span v-if="currentRun" class="status-pill" :class="currentRun.llm_status === 'completed' ? 'tone-ok' : currentRun.llm_status === 'failed' ? 'tone-warning' : 'tone-muted'">
                {{ currentRun.llm_status || '未生成' }}
              </span>
            </header>
            <div v-if="explanationHtml" class="explanation-body" v-html="explanationHtml"></div>
            <div v-else-if="currentRun?.llm_error" class="explanation-error">解释生成失败：{{ currentRun.llm_error }}</div>
            <div v-else class="empty-state">暂无模型解释；确定性排名仍可独立使用和复核。</div>
            <footer v-if="currentRun?.llm_model">模型：{{ currentRun.llm_model }}</footer>
          </article>
        </div>
      </section>

      <section class="governance-section" aria-labelledby="governance-title" data-testid="automatic-selection-governance">
        <header class="governance-heading">
          <div>
            <h2 id="governance-title">策略治理与运行审计</h2>
          </div>
          <p>生命周期动作由人确认；不可变版本与运行证据长期保留。</p>
        </header>

        <div class="governance-grid">
          <article class="governance-panel strategy-section" data-testid="automatic-selection-strategies">
            <header class="governance-panel-header">
              <div>
                <h3>策略版本与生命周期</h3>
                <p v-if="overview?.official_strategy">
                  当前正式：{{ overview.official_strategy.name }} · v{{ overview.official_strategy.version }}
                </p>
                <p v-else>当前暂无正式策略</p>
              </div>
              <button v-if="canManageStrategies" class="auto-button auto-button--primary" type="button" @click="openCreate()">新建策略版本</button>
            </header>
            <div class="strategy-list">
              <article v-for="strategy in sortedStrategies" :key="strategy.id" :class="{ official: strategy.is_official }">
                <header>
                  <div>
                    <span class="lifecycle-pill" :class="`lifecycle-${strategy.lifecycle_state}`">{{ lifecycleLabel(strategy.lifecycle_state) }}</span>
                    <strong>{{ strategy.name }} <small>v{{ strategy.version }}</small></strong>
                  </div>
                  <div v-if="canManageStrategies" class="strategy-actions">
                    <button type="button" @click="openCreate(strategy)">基于此版本新建</button>
                    <button v-if="!strategy.is_official && strategy.lifecycle_state !== 'retired'" type="button" @click="openPromote(strategy)">人工晋升</button>
                    <button v-if="!strategy.is_official && strategy.lifecycle_state !== 'retired'" type="button" class="danger-link" @click="openRetire(strategy)">退役</button>
                  </div>
                </header>
                <div class="strategy-meta">
                  <span>{{ strategy.strategy_key }}</span>
                  <span>Top-N {{ strategy.config?.top_n ?? '—' }}</span>
                  <span>{{ strategy.enabled ? '运行中' : '已停用' }}</span>
                  <span>{{ beijingFromDb(strategy.created_at) }}</span>
                </div>
                <code>{{ strategy.config_hash }}</code>
                <p v-if="strategy.promotion_note">晋升说明：{{ strategy.promotion_note }}</p>
              </article>
            </div>
          </article>

          <article class="governance-panel run-audit-section">
            <header class="governance-panel-header">
              <div>
                <h3>运行审计账本</h3>
                <p>相同输入复用同一运行；修订通过 supersede 链追溯。</p>
              </div>
            </header>
            <div v-if="overview?.recent_runs.length" class="audit-list">
              <button
                v-for="run in overview.recent_runs"
                :key="run.run_id"
                type="button"
                :aria-selected="selectedRunId === run.run_id"
                :class="{ selected: selectedRunId === run.run_id }"
                @click="loadRun(run.run_id)"
              >
                <span><strong>{{ fmtTradeDate(run.target_trade_date) }}</strong><small>{{ run.strategy_name }} · v{{ run.strategy_version }}</small></span>
                <span><b>{{ run.universe_count }} / {{ run.eligible_count }} / {{ run.selected_count }}</b><small>研究池 / 合格 / Top-N</small></span>
                <span><code>{{ run.data_fingerprint.slice(0, 14) }}…</code><small>{{ run.supersedes_run_id ? '修订运行' : '原始运行' }}</small></span>
                <span class="status-pill" :class="toneForStatus(run.status)">{{ statusLabels[run.status] || run.status }}</span>
              </button>
            </div>
            <div v-else class="empty-state">暂无运行账本。</div>
          </article>
        </div>
      </section>
    </template>

    <Teleport to="body">
      <div v-if="strategyAction" class="auto-dialog-backdrop" @mousedown.self="closeAction">
        <form
          ref="dialogEl"
          class="auto-dialog"
          role="dialog"
          aria-modal="true"
          aria-labelledby="strategy-dialog-title"
          tabindex="-1"
          @keydown.esc.stop.prevent="closeAction"
          @keydown.tab="trapDialogFocus"
          @submit.prevent="submitStrategyAction"
        >
          <header>
            <div>
              <h2 v-if="strategyAction === 'create'" id="strategy-dialog-title">创建不可变策略版本</h2>
              <h2 v-else-if="strategyAction === 'promote'" id="strategy-dialog-title">人工晋升策略</h2>
              <h2 v-else id="strategy-dialog-title">退役策略</h2>
            </div>
            <button type="button" aria-label="关闭策略治理对话框" @click="closeAction">×</button>
          </header>

          <div v-if="strategyAction === 'create'" class="auto-dialog-body">
            <label>策略标识<input v-model="createForm.strategyKey" required pattern="[a-z][a-z0-9-]{2,63}" /></label>
            <label>版本名称<input v-model="createForm.name" required maxlength="160" /></label>
            <label>初始状态<select v-model="createForm.lifecycleState"><option value="challenger">挑战者</option><option value="shadow">影子</option></select></label>
            <label class="dialog-span">版本化配置 JSON<textarea v-model="createForm.configText" rows="18" required spellcheck="false"></textarea></label>
            <p class="dialog-span">父版本：{{ actionStrategy ? `${actionStrategy.name} v${actionStrategy.version}` : '无' }}。提交后只能继续创建新版本，不能原地修改。</p>
          </div>
          <div v-else class="auto-dialog-body single-column">
            <div class="action-target"><strong>{{ actionStrategy?.name }} v{{ actionStrategy?.version }}</strong><code>{{ actionStrategy?.config_hash }}</code></div>
            <label v-if="strategyAction === 'promote'">人工核验说明<textarea v-model="confirmationNote" rows="5" minlength="4" maxlength="1000" required placeholder="说明已核验的前瞻证据、质量门禁与风险边界"></textarea></label>
            <label v-else>退役说明<textarea v-model="confirmationNote" rows="4" maxlength="1000" placeholder="可选：说明退役原因"></textarea></label>
            <p v-if="strategyAction === 'promote'" class="promotion-evidence-notice" role="note">
              最低晋升证据：至少 1 个已到期且覆盖达标的 5 日前瞻窗口；这只是最低观察证据，不代表统计显著或策略已通过统计验证。
            </p>
            <p>{{ strategyAction === 'promote' ? '该动作会将当前正式策略降为挑战者，并追加不可变审计事件。' : '退役后不再参与每日运行，但历史运行、候选和评估证据仍保留。' }}</p>
          </div>

          <footer>
            <button class="auto-button auto-button--ghost" type="button" :disabled="actionLoading" @click="closeAction">取消</button>
            <button class="auto-button" :class="strategyAction === 'retire' ? 'auto-button--danger' : 'auto-button--primary'" type="submit" :disabled="actionLoading">
              {{ actionLoading ? '正在提交...' : strategyAction === 'create' ? '创建新版本' : strategyAction === 'promote' ? '确认人工晋升' : '确认退役' }}
            </button>
          </footer>
        </form>
      </div>
    </Teleport>
  </section>
</template>

<style scoped>
.automatic-selection-page {
  display: grid;
  gap: 12px;
  color: var(--text);
}

.boundary-mark {
  color: var(--gold);
  font-size: 11px;
}

.cockpit-command {
  display: grid;
  grid-template-columns: minmax(280px, 1fr) minmax(420px, auto) auto;
  align-items: center;
  gap: 20px;
  min-height: 78px;
  border: 1px solid var(--line-strong);
  border-radius: 6px;
  padding: 12px 14px;
  background:
    linear-gradient(100deg, rgba(213, 180, 106, .08), transparent 38%),
    #0e0e10;
  box-shadow: inset 3px 0 #8a713b;
}

.command-identity { min-width: 0; }
.command-title-line { display: flex; align-items: center; gap: 10px; margin-top: 3px; }
.command-title-line h1 { margin: 0; color: #f1e4bd; font-size: 19px; letter-spacing: -.02em; }
.command-identity p { margin: 3px 0 0; color: var(--muted); font-size: 11px; }
.run-context { border: 1px solid #75602f; border-radius: 3px; padding: 2px 6px; background: rgba(213, 180, 106, .1); color: var(--gold-2); font-size: 12px; white-space: nowrap; }
.run-context.historical { border-color: var(--line-strong); background: var(--surface-2); color: #b9b4a8; }
.command-run { display: grid; grid-template-columns: repeat(3, minmax(112px, auto)); border-left: 1px solid var(--line); }
.command-run > div { display: grid; align-content: center; gap: 4px; min-height: 48px; padding: 0 14px; border-right: 1px solid var(--line); }
.command-run span { color: var(--muted); font-size: 11px; }
.command-run strong { overflow: hidden; color: var(--text); font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.command-run > div:first-child strong { color: var(--gold-2); font-size: 15px; font-variant-numeric: tabular-nums; }
.command-actions { display: flex; gap: 7px; justify-content: flex-end; }

.research-boundary {
  display: flex;
  align-items: center;
  gap: 9px;
  min-height: 34px;
  border: 1px solid #4b4026;
  border-radius: 4px;
  padding: 6px 10px;
  background: rgba(213, 180, 106, .035);
  color: #bdb6a3;
  font-size: 12px;
}
.research-boundary strong { color: var(--gold-2); white-space: nowrap; }
.boundary-mark { border: 1px solid var(--gold-dim); border-radius: 2px; padding: 2px 5px; }

.message-stack:empty { display: none; }
.message-stack { display: grid; gap: 6px; }
.auto-notice,
.run-error { border: 1px solid; border-radius: 4px; padding: 9px 12px; font-size: 11px; }
.auto-notice--danger,
.run-error { border-color: rgba(255, 93, 93, .4); background: rgba(255, 93, 93, .07); color: #ff9b9b; }
.auto-notice--success { border-color: rgba(39, 197, 138, .35); background: rgba(39, 197, 138, .07); color: #78dbb8; }
.run-error { display: flex; gap: 9px; }
.run-error strong { color: #ffc1c1; white-space: nowrap; }

.auto-loading,
.empty-state { display: grid; min-height: 116px; place-content: center; gap: 6px; padding: 20px; color: var(--muted); text-align: center; }
.auto-loading--inline { min-height: 84px; }
.empty-state strong { color: var(--text); font-size: 13px; }
.empty-state span { max-width: 260px; font-size: 12px; line-height: 1.55; }
.compact-empty { min-height: 68px; padding: 12px; font-size: 12px; }

.kpi-strip {
  display: grid;
  grid-template-columns: repeat(5, minmax(100px, 1fr)) minmax(170px, 1.35fr);
  gap: 1px;
  overflow: hidden;
  border: 1px solid var(--line-strong);
  border-radius: 5px;
  background: var(--line);
}
.kpi-strip > div { display: grid; gap: 3px; min-width: 0; min-height: 68px; align-content: center; padding: 9px 13px; background: #121214; }
.kpi-strip span,
.kpi-strip small { overflow: hidden; color: var(--muted); font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.kpi-strip strong { overflow: hidden; color: #e8dfc9; font-size: 19px; font-variant-numeric: tabular-nums; text-overflow: ellipsis; white-space: nowrap; }
.kpi-strip .kpi-stage { font-size: 13px; }
.kpi-strip .kpi-ok { color: #69d4ae; }
.kpi-strip .kpi-warning { color: #e5bd61; }

.cockpit-workspace {
  display: grid;
  grid-template-columns: 240px minmax(520px, 1fr) 320px;
  min-height: 650px;
  border: 1px solid var(--line-strong);
  border-radius: 6px;
  background: #0d0d0f;
}
.cockpit-panel { min-width: 0; background: var(--surface); }
.run-rail { border-right: 1px solid var(--line-strong); }
.ranking-console { display: flex; min-height: 0; flex-direction: column; border-right: 1px solid var(--line-strong); background: #101012; }
.candidate-inspector { min-height: 0; background: #111113; }
.panel-heading,
.ranking-toolbar,
.governance-heading,
.governance-panel-header,
.validation-panel-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}
.panel-heading { min-height: 55px; padding: 11px 12px; border-bottom: 1px solid var(--line); background: rgba(213, 180, 106, .025); }
.panel-heading h2,
.ranking-toolbar h2,
.governance-heading h2 { margin: 3px 0 0; color: #eee2c0; font-size: 14px; }
.panel-heading > span { border: 1px solid var(--line); border-radius: 3px; padding: 3px 6px; color: var(--muted); font-size: 11px; }

.run-selector { max-height: 245px; overflow: auto; border-bottom: 1px solid var(--line-strong); scrollbar-width: thin; }
.run-option { display: grid; gap: 5px; width: 100%; min-height: 72px; border: 0; border-bottom: 1px solid var(--line); border-radius: 0; padding: 9px 11px; background: transparent; color: var(--muted); text-align: left; }
.run-option:hover { background: rgba(213, 180, 106, .04); }
.run-option.selected { background: linear-gradient(90deg, rgba(213, 180, 106, .11), transparent); box-shadow: inset 2px 0 var(--gold); }
.run-option-top,
.run-option-bottom { display: flex; align-items: center; justify-content: space-between; gap: 6px; }
.run-option-top b { color: var(--text); font-size: 12px; font-variant-numeric: tabular-nums; }
.run-option-top em { color: var(--gold-2); font-size: 11px; font-style: normal; }
.run-option-top em.historical { color: var(--muted-2); }
.run-option > span:nth-child(2) { overflow: hidden; font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.run-option-bottom i { border: 0; background: transparent; font-size: 11px; font-style: normal; }
.run-option-bottom code { color: var(--muted-2); font-size: 10px; }

.rail-evidence { padding-bottom: 10px; }
.rail-section-title { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 11px 12px 7px; }
.rail-section-title h3 { margin: 3px 0 0; color: #ddd4bc; font-size: 12px; }
.gate-dot { border: 1px solid; border-radius: 3px; padding: 3px 5px; font-size: 11px; }
.gate-pass { border-color: rgba(39, 197, 138, .4); background: rgba(39, 197, 138, .08); color: #6fd8b2; }
.gate-blocked { border-color: rgba(213, 166, 66, .45); background: rgba(213, 166, 66, .09); color: #e8c66f; }
.gate-policy { margin: 0 12px 8px; color: var(--muted); font-size: 11px; line-height: 1.5; }
.quality-ledger { display: grid; margin: 0; border-top: 1px solid var(--line); }
.quality-ledger > div { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 2px 8px; padding: 6px 12px; border-bottom: 1px solid rgba(255, 255, 255, .035); }
.quality-ledger dt { overflow: hidden; color: var(--muted); font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.quality-ledger dd { margin: 0; color: var(--text); font-size: 12px; font-variant-numeric: tabular-nums; }
.quality-ledger dd small { color: var(--muted-2); font-size: 10px; }
.quality-ledger .quality-detail { display: block; margin-top: 3px; white-space: nowrap; }
.gate-failures { display: grid; gap: 4px; margin: 8px; border-left: 2px solid #b38a34; padding: 7px 8px; background: rgba(213, 166, 66, .055); color: #d8c697; font-size: 10px; line-height: 1.45; }
.gate-failures strong { color: var(--warning); }
.exclusion-ledger { padding: 8px 12px 0; }
.exclusion-ledger h4 { margin: 0 0 6px; color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .08em; }
.exclusion-ledger > div { display: grid; gap: 4px; }
.exclusion-ledger span { display: flex; align-items: center; justify-content: space-between; gap: 7px; color: var(--muted); font-size: 11px; }
.exclusion-ledger b { overflow: hidden; font-weight: 400; text-overflow: ellipsis; white-space: nowrap; }
.exclusion-ledger strong { color: #d9b9a3; font-variant-numeric: tabular-nums; }
.exclusion-ledger p { margin: 0; color: var(--muted-2); font-size: 11px; }

.ranking-toolbar { min-height: 62px; padding: 11px 13px; border-bottom: 1px solid var(--line); }
.ranking-toolbar p { margin: 3px 0 0; color: var(--muted); font-size: 11px; }
.ranking-page-token { display: grid; grid-template-columns: auto auto auto; align-items: baseline; gap: 4px; color: var(--muted); font-size: 11px; }
.ranking-page-token strong { color: var(--gold-2); font-size: 15px; font-variant-numeric: tabular-nums; }
.ranking-page-token small { font-size: 11px; }
.scope-tabs { display: grid; grid-template-columns: repeat(3, 1fr); padding: 6px 8px; border-bottom: 1px solid var(--line); background: #0d0d0f; }
.scope-tabs button { min-height: 29px; border: 1px solid var(--line); border-right: 0; border-radius: 0; background: transparent; color: var(--muted); font-size: 12px; }
.scope-tabs button:first-child { border-radius: 3px 0 0 3px; }
.scope-tabs button:last-child { border-right: 1px solid var(--line); border-radius: 0 3px 3px 0; }
.scope-tabs button.active { border-color: #75602e; background: rgba(213, 180, 106, .1); color: var(--gold-2); }
.scope-tabs button.active + button { border-left-color: #75602e; }
.ranking-panel { display: flex; min-height: 0; flex: 1; flex-direction: column; }
.rank-boundary { padding: 6px 10px; border-bottom: 1px solid var(--line); background: rgba(84, 117, 135, .06); color: #8f9da3; font-size: 10px; letter-spacing: .02em; }
.candidate-table-wrap { min-height: 0; max-height: 505px; overflow: auto; scrollbar-width: thin; }
.candidate-table { width: 100%; min-width: 680px; border-collapse: collapse; table-layout: fixed; }
.candidate-table th { position: sticky; z-index: 2; top: 0; height: 34px; border-bottom: 1px solid var(--line-strong); padding: 7px 8px; background: #151517; color: #958f82; font-size: 10px; font-weight: 600; letter-spacing: .05em; text-align: left; white-space: nowrap; }
.candidate-table th:nth-child(1) { width: 48px; }
.candidate-table th:nth-child(2) { width: 126px; }
.candidate-table th:nth-child(3) { width: 105px; }
.candidate-table th:nth-child(4) { width: 132px; }
.candidate-table th:nth-child(5),
.candidate-table th:nth-child(6) { width: 82px; }
.candidate-table th:nth-child(7) { width: 100px; }
.candidate-table .status-column { width: 108px; }
.candidate-table .exclusion-column { width: 164px; }
.candidate-table td { height: 58px; border-bottom: 1px solid rgba(255, 255, 255, .04); padding: 6px 8px; color: #cbc6ba; font-size: 12px; vertical-align: middle; }
.candidate-table td small { display: block; margin-top: 3px; color: var(--muted); font-size: 10px; }
.candidate-row:hover { background: rgba(213, 180, 106, .03); }
.candidate-row.selected { background: linear-gradient(90deg, rgba(213, 180, 106, .1), rgba(213, 180, 106, .025)); box-shadow: inset 2px 0 var(--gold); }
.candidate-row.candidate-excluded { background-color: rgba(255, 93, 93, .022); }
.rank-cell strong { color: #9d9480; font-size: 12px; font-variant-numeric: tabular-nums; }
.candidate-button { display: grid; gap: 2px; width: 100%; border: 0; padding: 0; background: transparent; color: inherit; text-align: left; }
.candidate-button strong { overflow: hidden; color: #e7dfcd; font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.candidate-button small { margin: 0; font-variant-numeric: tabular-nums; }
.num { text-align: right !important; font-variant-numeric: tabular-nums; white-space: nowrap; }
.score-cell { color: var(--gold-2) !important; font-weight: 700; }
.exclusion-cell { text-align: left; white-space: normal; }
.exclusion-cell span { display: inline-flex; margin: 2px 4px 2px 0; border: 1px solid #5b3738; border-radius: 3px; padding: 3px 6px; background: rgba(255, 93, 93, .06); color: #e5a6a6; }
.status-pill,
.change-pill,
.lifecycle-pill { display: inline-flex; align-items: center; width: fit-content; border: 1px solid; border-radius: 99px; padding: 2px 6px; font-size: 10px; white-space: nowrap; }
.tone-ok { border-color: rgba(39, 197, 138, .35); background: rgba(39, 197, 138, .08); color: #6fd8b2 !important; }
.tone-warning { border-color: rgba(213, 166, 66, .4); background: rgba(213, 166, 66, .1); color: #e6c36f !important; }
.tone-danger { border-color: rgba(255, 93, 93, .4); background: rgba(255, 93, 93, .08); color: #ff8b8b !important; }
.tone-muted { border-color: var(--line); background: var(--surface-2); color: var(--muted) !important; }
.change-pill { border-color: var(--line); color: var(--muted); }
.change-added { border-color: rgba(242, 84, 184, .35); color: #ef9dce; }
.change-retained { border-color: rgba(43, 212, 212, .3); color: #73d4d4; }
.change-removed { border-color: rgba(255, 93, 93, .3); color: #e89494; }
.candidate-pagination { display: flex; align-items: center; justify-content: space-between; gap: 10px; min-height: 49px; margin-top: auto; border-top: 1px solid var(--line-strong); padding: 7px 10px; background: #0d0d0f; color: var(--muted); font-size: 11px; }
.candidate-pagination > div { display: flex; gap: 5px; }

.inspector-identity { display: grid; gap: 11px; padding: 13px; border-bottom: 1px solid var(--line); }
.inspector-identity > div:first-child { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
.inspector-identity > div:first-child strong { overflow: hidden; color: #f0e4c4; font-size: 16px; text-overflow: ellipsis; white-space: nowrap; }
.inspector-identity code { color: var(--gold); font-size: 12px; }
.inspector-identity dl { display: grid; grid-template-columns: 1fr 1fr; gap: 1px; margin: 0; background: var(--line); }
.inspector-identity dl div { display: grid; gap: 3px; padding: 8px; background: #151517; }
.inspector-identity dt,
.market-evidence dt { color: var(--muted); font-size: 10px; }
.inspector-identity dd { margin: 0; color: var(--text); font-size: 12px; font-weight: 700; font-variant-numeric: tabular-nums; }
.inspector-block { padding: 12px 13px; border-bottom: 1px solid var(--line); }
.inspector-block h3 { margin: 0 0 8px; color: #a9a291; font-size: 11px; letter-spacing: .08em; text-transform: uppercase; }
.inspector-block > p { margin: 0; color: var(--muted-2); font-size: 11px; }
.inspector-block .positive-evidence { color: #6fb497; }
.code-list { display: flex; flex-wrap: wrap; gap: 4px; }
.code-list span { border: 1px solid #493233; border-radius: 3px; padding: 3px 5px; background: rgba(255, 93, 93, .05); color: #d9a2a2; font-size: 10px; }
.factor-ledger { display: grid; gap: 1px; background: var(--line); }
.factor-ledger > div { display: grid; grid-template-columns: minmax(0, 1fr) auto 24px; gap: 6px; align-items: center; min-height: 27px; padding: 4px 7px; background: #151517; }
.factor-ledger span { overflow: hidden; color: var(--muted); font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }
.factor-ledger strong { color: #ddd5c1; font-size: 11px; font-variant-numeric: tabular-nums; }
.factor-ledger small { color: var(--gold-dim); font-size: 9px; text-align: right; text-transform: uppercase; }
.market-evidence dl { display: grid; gap: 6px; margin: 0; }
.market-evidence dl div { display: flex; justify-content: space-between; gap: 8px; }
.market-evidence dd { margin: 0; color: #d2ccbd; font-size: 11px; font-variant-numeric: tabular-nums; }
.inspector-empty { min-height: 300px; }

.validation-section,
.governance-section { overflow: hidden; border: 1px solid var(--line-strong); border-radius: 6px; background: #0f0f11; }
.governance-heading { align-items: center; min-height: 58px; padding: 10px 13px; border-bottom: 1px solid var(--line-strong); background: rgba(213, 180, 106, .025); }
.governance-heading p { margin: 0; color: var(--muted); font-size: 11px; }
.validation-grid { display: grid; grid-template-columns: minmax(0, 1.25fr) minmax(340px, .75fr); }
.validation-panel { min-width: 0; }
.validation-panel + .validation-panel { border-left: 1px solid var(--line-strong); }
.validation-panel-header,
.governance-panel-header { min-height: 58px; padding: 11px 13px; border-bottom: 1px solid var(--line); }
.validation-panel-header h3,
.governance-panel-header h3 { margin: 0; color: #ddd4bc; font-size: 12px; }
.validation-panel-header p,
.governance-panel-header p { margin: 3px 0 0; color: var(--muted); font-size: 11px; }
.evaluation-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); }
.evaluation-grid > article { min-width: 0; padding: 12px; border-right: 1px solid var(--line); }
.evaluation-grid > article:last-child { border-right: 0; }
.evaluation-grid header { display: flex; align-items: center; justify-content: space-between; gap: 7px; }
.evaluation-grid header strong { color: var(--gold-2); font-size: 13px; }
.evaluation-grid dl { display: grid; gap: 5px; margin: 11px 0; }
.evaluation-grid dl div { display: flex; justify-content: space-between; gap: 7px; }
.evaluation-grid dt { color: var(--muted); font-size: 10px; }
.evaluation-grid dd { margin: 0; font-size: 11px; font-weight: 700; font-variant-numeric: tabular-nums; }
.evaluation-grid article > small,
.evaluation-grid article > p { color: var(--muted); font-size: 10px; }
.evaluation-grid .evaluation-error { color: #e49b9b; }
.explanation-section { display: flex; min-height: 260px; flex-direction: column; }
.explanation-body { flex: 1; overflow-wrap: anywhere; padding: 13px; color: #d4cfc2; font-size: 12px; line-height: 1.7; }
.explanation-error { padding: 13px; color: var(--warning); font-size: 12px; }
.explanation-section footer { border-top: 1px solid var(--line); padding: 7px 13px; color: var(--muted); font-size: 10px; }
.risk-contained { color: #9bb7aa; }
.risk-watch { color: #e3bd65; }
.risk-critical { color: #ff8c8c; }

.governance-grid { display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(360px, .8fr); }
.governance-panel { min-width: 0; }
.governance-panel + .governance-panel { border-left: 1px solid var(--line-strong); }
.strategy-list { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); max-height: 370px; overflow: auto; }
.strategy-list > article { display: grid; gap: 8px; min-width: 0; padding: 11px; border-right: 1px solid var(--line); border-bottom: 1px solid var(--line); }
.strategy-list > article.official { background: linear-gradient(135deg, rgba(213, 180, 106, .09), transparent 58%); }
.strategy-list article header { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; }
.strategy-list article header > div:first-child { display: flex; align-items: center; gap: 6px; min-width: 0; }
.strategy-list article strong { overflow: hidden; color: var(--text); font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.strategy-list article strong small { color: var(--muted); font-weight: 400; }
.lifecycle-pill { border-color: var(--line); color: var(--muted); }
.lifecycle-official { border-color: #745e2d; background: rgba(213, 180, 106, .1); color: var(--gold-2); }
.lifecycle-challenger { border-color: rgba(242, 84, 184, .3); color: #df91c0; }
.lifecycle-shadow { border-color: rgba(43, 212, 212, .28); color: #69caca; }
.strategy-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 3px; }
.strategy-actions button { border: 0; padding: 2px 3px; background: transparent; color: var(--gold); font-size: 10px; }
.strategy-actions .danger-link { color: #e47777; }
.strategy-meta { display: flex; flex-wrap: wrap; gap: 4px 10px; color: var(--muted); font-size: 10px; }
.strategy-list code { overflow: hidden; color: var(--muted-2); font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }
.strategy-list p { margin: 0; color: var(--muted); font-size: 10px; }
.audit-list { display: grid; max-height: 370px; overflow: auto; }
.audit-list > button { display: grid; grid-template-columns: minmax(95px, 1fr) minmax(100px, 1fr) minmax(100px, 1fr) auto; align-items: center; gap: 8px; min-height: 58px; border: 0; border-bottom: 1px solid var(--line); border-radius: 0; padding: 8px 11px; background: transparent; color: var(--text); text-align: left; }
.audit-list > button:hover,
.audit-list > button.selected { background: rgba(213, 180, 106, .05); }
.audit-list > button.selected { box-shadow: inset 2px 0 var(--gold); }
.audit-list span { min-width: 0; }
.audit-list strong,
.audit-list b,
.audit-list code { display: block; overflow: hidden; color: #d8d0bc; font-size: 11px; font-weight: 600; text-overflow: ellipsis; white-space: nowrap; }
.audit-list code { color: var(--muted); font-size: 10px; }
.audit-list small { display: block; margin-top: 3px; color: var(--muted-2); font-size: 9px; }

.auto-button { min-height: 31px; border: 1px solid var(--line-strong); border-radius: 4px; padding: 6px 10px; background: var(--surface-2); color: var(--text); font-size: 12px; font-weight: 600; }
.auto-button:hover:not(:disabled) { border-color: var(--gold-dim); color: var(--gold-2); }
.auto-button:disabled { cursor: not-allowed; opacity: .5; }
.auto-button--primary { border-color: #785f2d; background: linear-gradient(180deg, #c29d4f, #8e6f33); color: #100d07; }
.auto-button--primary:hover:not(:disabled) { color: #050505; filter: brightness(1.08); }
.auto-button--ghost { background: transparent; }
.auto-button--danger { border-color: #7d3030; background: #5a2424; color: #ffd1d1; }
button:focus-visible,
[tabindex]:focus-visible,
input:focus-visible,
select:focus-visible,
textarea:focus-visible { outline: 2px solid #d5b46a; outline-offset: 2px; }
.pnl-up { color: #ff6767 !important; }
.pnl-down { color: #35cb8f !important; }

.auto-dialog-backdrop { position: fixed; z-index: 1000; inset: 0; display: grid; place-items: center; padding: 20px; background: rgba(0, 0, 0, .78); backdrop-filter: blur(4px); }
.auto-dialog { width: min(780px, 100%); max-height: calc(100vh - 40px); overflow: auto; border: 1px solid var(--line-strong); border-radius: 7px; outline: 0; background: #101012; box-shadow: 0 24px 80px rgba(0, 0, 0, .65); }
.auto-dialog > header { display: flex; align-items: flex-start; justify-content: space-between; padding: 14px 16px; border-bottom: 1px solid var(--line); }
.auto-dialog h2 { margin: 3px 0 0; color: var(--gold-2); font-size: 15px; }
.auto-dialog > header > button { border: 0; background: transparent; color: var(--muted); font-size: 22px; }
.auto-dialog-body { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; padding: 16px; }
.auto-dialog-body.single-column { grid-template-columns: 1fr; }
.auto-dialog label { display: grid; gap: 6px; color: var(--muted); font-size: 12px; }
.auto-dialog input,
.auto-dialog select,
.auto-dialog textarea { width: 100%; border: 1px solid var(--line); border-radius: 4px; outline: 0; background: var(--bg-2); padding: 9px 10px; color: var(--text); }
.auto-dialog input:focus,
.auto-dialog select:focus,
.auto-dialog textarea:focus { border-color: var(--gold-dim); }
.auto-dialog textarea { resize: vertical; font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 12px; line-height: 1.5; }
.dialog-span { grid-column: 1 / -1; }
.auto-dialog-body p { margin: 0; color: var(--muted); font-size: 11px; }
.auto-dialog-body .promotion-evidence-notice { border-left: 2px solid var(--warning); padding: 7px 9px; background: rgba(213, 166, 66, .06); color: #d8c697; line-height: 1.55; }
.action-target { display: grid; gap: 5px; padding: 11px; border: 1px solid var(--line); border-radius: 4px; background: var(--surface-2); }
.action-target code { overflow-wrap: anywhere; color: var(--muted); font-size: 11px; }
.auto-dialog > footer { display: flex; justify-content: flex-end; gap: 7px; padding: 11px 16px; border-top: 1px solid var(--line); }

@media (max-width: 1380px) {
  .cockpit-command { grid-template-columns: minmax(250px, 1fr) minmax(350px, auto) auto; gap: 12px; }
  .command-run > div { padding: 0 9px; }
  .cockpit-workspace { grid-template-columns: 220px minmax(0, 1fr); }
  .ranking-console { border-right: 0; }
  .candidate-inspector { display: grid; grid-column: 1 / -1; grid-template-columns: repeat(4, minmax(0, 1fr)); border-top: 1px solid var(--line-strong); }
  .candidate-inspector > .panel-heading { grid-column: 1 / -1; }
  .inspector-identity,
  .rule-evidence,
  .factor-evidence { border-right: 1px solid var(--line); }
  .inspector-empty { grid-column: 1 / -1; }
}

@media (max-width: 980px) {
  .cockpit-command { grid-template-columns: minmax(0, 1fr) auto; }
  .command-run { grid-row: 2; grid-column: 1 / -1; border-top: 1px solid var(--line); border-left: 0; padding-top: 8px; }
  .kpi-strip { grid-template-columns: repeat(3, 1fr); }
  .cockpit-workspace { grid-template-columns: minmax(0, 1fr); }
  .run-rail { border-right: 0; border-bottom: 1px solid var(--line-strong); }
  .candidate-inspector { display: grid; grid-template-columns: 1fr 1fr; }
  .candidate-inspector > .panel-heading,
  .inspector-empty { grid-column: 1 / -1; }
  .inspector-identity,
  .factor-evidence { border-right: 1px solid var(--line); }
  .rule-evidence { border-right: 0; }
  .validation-grid,
  .governance-grid { grid-template-columns: 1fr; }
  .validation-panel + .validation-panel,
  .governance-panel + .governance-panel { border-top: 1px solid var(--line-strong); border-left: 0; }
}

@media (max-width: 680px) {
  .automatic-selection-page { gap: 9px; }
  .cockpit-command { grid-template-columns: 1fr; align-items: stretch; }
  .command-title-line { align-items: flex-start; flex-direction: column; gap: 5px; }
  .command-actions { justify-content: stretch; }
  .command-actions .auto-button,
  .scope-tabs button,
  .candidate-pagination .auto-button { min-height: 44px; }
  .command-actions .auto-button { flex: 1; }
  .command-run { grid-row: auto; grid-column: auto; grid-template-columns: 1fr; }
  .command-run > div { min-height: 39px; border-right: 0; border-bottom: 1px solid var(--line); }
  .research-boundary { align-items: flex-start; flex-direction: column; }
  .research-boundary strong { white-space: normal; }
  .kpi-strip { grid-template-columns: repeat(2, 1fr); }
  .run-selector { display: flex; max-height: none; overflow-x: auto; }
  .run-option { min-width: 210px; border-right: 1px solid var(--line); }
  .ranking-console { min-height: 590px; }
  .candidate-table-wrap { max-height: 430px; }
  .candidate-inspector { display: block; }
  .candidate-inspector > * { border-right: 0; }
  .candidate-pagination { align-items: stretch; flex-direction: column; }
  .candidate-pagination > div { justify-content: flex-end; }
  .governance-heading { align-items: flex-start; flex-direction: column; }
  .evaluation-grid { grid-template-columns: 1fr; }
  .evaluation-grid > article { border-right: 0; border-bottom: 1px solid var(--line); }
  .audit-list > button { grid-template-columns: 1fr 1fr; }
  .auto-dialog-body { grid-template-columns: 1fr; }
  .dialog-span { grid-column: auto; }
}
</style>

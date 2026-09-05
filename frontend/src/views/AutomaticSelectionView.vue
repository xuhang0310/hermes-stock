<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
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
const candidateScope = ref<CandidateScope>('top')
const strategyAction = ref<StrategyAction>(null)
const actionStrategy = ref<AutomaticSelectionStrategy | null>(null)
const confirmationNote = ref('')
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
const qualityGate = computed(() => currentRun.value?.quality_gate || null)
const candidates = computed<AutomaticSelectionCandidate[]>(() => {
  if (selectedRunDetail.value) return selectedRunDetail.value.candidates
  if (candidateScope.value === 'top' && selectedOverviewRun.value) {
    return overview.value?.top_candidates || []
  }
  return []
})
const sortedStrategies = computed(() => [...strategies.value].sort((left, right) => {
  if (left.is_official !== right.is_official) return left.is_official ? -1 : 1
  return left.strategy_key.localeCompare(right.strategy_key) || right.version - left.version
}))
const explanationHtml = computed(() => renderMarkdown(currentRun.value?.llm_explanation || ''))
const factorCoverage = computed(() => Object.entries(qualityGate.value?.metrics.factor_coverage || {}))
const selectedScopeLabel = computed(() => ({ top: 'Top-N', eligible: '全量合格', excluded: '排除项' })[candidateScope.value])
const evaluations = computed(() => {
  if (selectedRunDetail.value) return selectedRunDetail.value.evaluations
  return selectedOverviewRun.value ? overview.value?.evaluations || [] : []
})
const candidateTotal = computed(() => selectedRunDetail.value?.candidate_total ?? candidates.value.length)
const candidateLimit = computed(() => selectedRunDetail.value?.candidate_limit || CANDIDATE_PAGE_SIZE)
const candidateOffset = computed(() => selectedRunDetail.value?.candidate_offset || 0)
const candidatePage = computed(() => Math.floor(candidateOffset.value / candidateLimit.value) + 1)
const candidatePageCount = computed(() => Math.max(1, Math.ceil(candidateTotal.value / candidateLimit.value)))

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

function toneForReturn(value: number | null | undefined): string {
  if (value == null || value === 0) return ''
  return value > 0 ? 'pnl-up' : 'pnl-down'
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

async function loadDashboard(preferredRunId?: string) {
  const requestSequence = ++evidenceRequestSequence
  loading.value = true
  detailLoading.value = false
  error.value = ''
  runDetail.value = null
  if (preferredRunId !== undefined) selectedRunId.value = preferredRunId
  try {
    const [nextOverview, nextStrategies] = await Promise.all([
      api.automaticSelectionOverview(),
      api.automaticSelectionStrategies(true),
    ])
    if (requestSequence !== evidenceRequestSequence) return
    overview.value = nextOverview
    strategies.value = nextStrategies
    const runId = preferredRunId ?? nextOverview.latest_run?.run_id ?? ''
    if (runId) {
      await loadRun(runId, candidateScope.value, 0, false, requestSequence)
    } else {
      selectedRunId.value = ''
      runDetail.value = null
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
  requestSequence = ++evidenceRequestSequence
) {
  if (!runId) return
  runDetail.value = null
  selectedRunId.value = runId
  candidateScope.value = scope
  if (showSpinner) detailLoading.value = true
  error.value = ''
  try {
    const detail = await api.automaticSelectionRunDetail(
      runId,
      scope,
      CANDIDATE_PAGE_SIZE,
      offset
    )
    if (requestSequence !== evidenceRequestSequence) return
    runDetail.value = detail
  } catch (reason) {
    if (requestSequence === evidenceRequestSequence) error.value = apiMessage(reason)
  } finally {
    if (requestSequence === evidenceRequestSequence) detailLoading.value = false
  }
}

async function selectScope(scope: CandidateScope) {
  if (scope === candidateScope.value || !selectedRunId.value) return
  await loadRun(selectedRunId.value, scope, 0)
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
  if (!canDispatch.value || actionLoading.value) return
  actionLoading.value = true
  error.value = ''
  notice.value = ''
  try {
    const result = await api.automaticSelectionDispatch()
    evidenceRequestSequence += 1
    selectedRunId.value = ''
    runDetail.value = null
    candidateScope.value = 'top'
    detailLoading.value = false
    notice.value = `任务已投递，运行编号 ${result.job_run_id}。完成后可刷新查看不可变证据。`
  } catch (reason) {
    error.value = apiMessage(reason)
  } finally {
    actionLoading.value = false
  }
}

function openCreate(strategy?: AutomaticSelectionStrategy) {
  if (!canManageStrategies.value) return
  const source = strategy || overview.value?.official_strategy || strategies.value[0]
  createForm.value = {
    strategyKey: source?.strategy_key || '',
    name: source ? `${source.name}（新版本）` : '',
    lifecycleState: 'challenger',
    parentStrategyId: source?.id || null,
    configText: JSON.stringify(source?.config || {}, null, 2),
  }
  actionStrategy.value = source || null
  strategyAction.value = 'create'
  confirmationNote.value = ''
}

function openPromote(strategy: AutomaticSelectionStrategy) {
  actionStrategy.value = strategy
  confirmationNote.value = ''
  strategyAction.value = 'promote'
}

function openRetire(strategy: AutomaticSelectionStrategy) {
  actionStrategy.value = strategy
  confirmationNote.value = ''
  strategyAction.value = 'retire'
}

function closeAction() {
  if (actionLoading.value) return
  strategyAction.value = null
  actionStrategy.value = null
  confirmationNote.value = ''
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
    strategyAction.value = null
    actionStrategy.value = null
    await loadDashboard(selectedRunId.value)
  } catch (reason) {
    error.value = reason instanceof SyntaxError ? '策略配置不是有效 JSON' : apiMessage(reason)
  } finally {
    actionLoading.value = false
  }
}

onMounted(() => void loadDashboard())
</script>

<template>
  <section class="automatic-selection-page">
    <div class="research-boundary" role="note">
      <span class="boundary-mark">研究边界</span>
      <strong>只做确定性选股与前瞻观察</strong>
      <span>{{ overview?.limitations || '仅用于上线后的研究观察，不构成投资建议或交易指令。' }}</span>
    </div>

    <div v-if="error" class="auto-notice auto-notice--danger" role="alert">{{ error }}</div>
    <div v-if="notice" class="auto-notice auto-notice--success" role="status">{{ notice }}</div>

    <div v-if="loading" class="page-section auto-loading">正在读取自动选股研究账本...</div>

    <template v-else>
      <section class="hero-grid">
        <article class="page-section run-hero">
          <header class="section-header">
            <div>
              <span class="eyebrow">LATEST DETERMINISTIC RUN</span>
              <h2>最新确定性运行</h2>
              <p>排名先于解释落库，任何解释异常都不会改变分数。</p>
            </div>
            <div class="section-actions">
              <button class="auto-button auto-button--ghost" type="button" @click="loadDashboard()">
                刷新证据
              </button>
              <button
                v-if="canDispatch"
                class="auto-button auto-button--primary"
                type="button"
                :disabled="actionLoading"
                data-testid="automatic-selection-dispatch"
                @click="dispatchRun"
              >
                {{ actionLoading ? '正在投递...' : '立即运行' }}
              </button>
            </div>
          </header>

          <div v-if="currentRun" class="run-summary">
            <div class="trade-date-block">
              <span>目标交易日</span>
              <strong>{{ fmtTradeDate(currentRun.target_trade_date) }}</strong>
              <small>{{ currentRun.strategy_name }} · v{{ currentRun.strategy_version }}</small>
            </div>
            <div class="run-status-block">
              <span class="status-pill" :class="toneForStatus(currentRun.status)">
                {{ statusLabels[currentRun.status] || currentRun.status }}
              </span>
              <strong>{{ currentRun.stage }}</strong>
              <small>{{ beijingFromDb(currentRun.finished_at || currentRun.started_at) }}</small>
            </div>
            <div class="run-stat"><span>研究池</span><strong>{{ currentRun.universe_count }}</strong></div>
            <div class="run-stat"><span>合格</span><strong>{{ currentRun.eligible_count }}</strong></div>
            <div class="run-stat"><span>Top-N</span><strong>{{ currentRun.selected_count }}</strong></div>
          </div>
          <div v-else class="empty-state">
            <strong>等待首轮运行</strong>
            <span>工作日 16:10 自动执行；有调度权限的账号也可手动投递。</span>
          </div>
        </article>

        <article class="page-section official-card">
          <header class="section-header">
            <div>
              <span class="eyebrow">OFFICIAL STRATEGY</span>
              <h2>当前正式策略</h2>
            </div>
          </header>
          <div v-if="overview?.official_strategy" class="official-body">
            <div>
              <strong>{{ overview.official_strategy.name }}</strong>
              <span>{{ overview.official_strategy.strategy_key }} · v{{ overview.official_strategy.version }}</span>
            </div>
            <dl>
              <div><dt>Top-N</dt><dd>{{ overview.official_strategy.config?.top_n ?? '—' }}</dd></div>
              <div><dt>行业上限</dt><dd>{{ overview.official_strategy.config?.constraints?.max_per_industry || '关闭' }}</dd></div>
              <div><dt>市值桶上限</dt><dd>{{ overview.official_strategy.config?.constraints?.max_per_market_cap_bucket || '关闭' }}</dd></div>
            </dl>
            <code>{{ overview.official_strategy.config_hash.slice(0, 16) }}…</code>
          </div>
          <div v-else class="empty-state">暂无正式策略</div>
        </article>
      </section>

      <section class="page-section quality-section" data-testid="automatic-selection-quality-gate">
        <header class="section-header">
          <div>
            <span class="eyebrow">QUALITY GATE</span>
            <h2>质量门禁</h2>
            <p>覆盖率不足时整条策略被阻断，不会静默删除缺失因子后继续排名。</p>
          </div>
          <span v-if="qualityGate" class="gate-verdict" :class="qualityGate.passed ? 'gate-pass' : 'gate-blocked'">
            {{ qualityGate.passed ? '门禁通过' : '门禁阻断' }}
          </span>
        </header>
        <div v-if="qualityGate" class="quality-grid">
          <article>
            <span>目标日行情覆盖</span>
            <strong>{{ fmtCoverage(qualityGate.metrics.bar_coverage) }}</strong>
            <small>阈值 {{ fmtCoverage(qualityGate.thresholds.min_bar_coverage) }}</small>
          </article>
          <article>
            <span>基础指标覆盖</span>
            <strong>{{ fmtCoverage(qualityGate.metrics.basic_coverage) }}</strong>
            <small>阈值 {{ fmtCoverage(qualityGate.thresholds.min_basic_coverage) }}</small>
          </article>
          <article>
            <span>行业信息覆盖</span>
            <strong>{{ fmtCoverage(qualityGate.metrics.industry_coverage) }}</strong>
            <small>阈值 {{ fmtCoverage(qualityGate.thresholds.min_industry_coverage) }}</small>
          </article>
          <article>
            <span>历史行情覆盖</span>
            <strong>{{ fmtCoverage(qualityGate.metrics.history_coverage) }}</strong>
            <small>
              阈值 {{ fmtCoverage(qualityGate.thresholds.min_history_coverage) }} ·
              {{ qualityGate.metrics.history_count ?? 0 }} 只历史完整
            </small>
          </article>
          <article>
            <span>最终合格池</span>
            <strong>{{ qualityGate.metrics.eligible_count ?? 0 }}</strong>
            <small>阈值 {{ qualityGate.thresholds.min_eligible_count ?? 0 }}</small>
          </article>
          <article v-for="[factor, coverage] in factorCoverage" :key="factor">
            <span>{{ featureLabel(factor) }} 覆盖</span>
            <strong>{{ fmtCoverage(coverage) }}</strong>
            <small>阈值 {{ fmtCoverage(qualityGate.thresholds.min_factor_coverage) }}</small>
          </article>
        </div>
        <div v-if="qualityGate?.failures.length" class="gate-failures">
          <strong>阻断证据</strong>
          <span v-for="failure in qualityGate.failures" :key="failure.code">
            {{ qualityLabels[failure.code] || featureLabel(failure.code.replace('factor_coverage:', '')) }}：
            实际 {{ failure.actual ?? metricActual(failure.code) ?? '—' }}，要求 {{ failure.required ?? metricRequired(failure.code) ?? '—' }}
            {{ failure.message || '' }}
          </span>
        </div>
        <div v-else-if="!qualityGate" class="empty-state">首轮运行后展示数据完整性判定。</div>
      </section>

      <section class="page-section ranking-section" data-testid="automatic-selection-ranking">
        <header class="section-header ranking-header">
          <div>
            <span class="eyebrow">IMMUTABLE RANKING EVIDENCE</span>
            <h2>候选排名证据</h2>
            <p>{{ selectedScopeLabel }} · 按综合分降序、股票代码升序稳定破同分。</p>
          </div>
          <div class="scope-tabs" role="tablist" aria-label="候选范围">
            <button type="button" role="tab" :aria-selected="candidateScope === 'top'" :class="{ active: candidateScope === 'top' }" @click="selectScope('top')">Top-N</button>
            <button type="button" role="tab" :aria-selected="candidateScope === 'eligible'" :class="{ active: candidateScope === 'eligible' }" @click="selectScope('eligible')">全量合格</button>
            <button type="button" role="tab" :aria-selected="candidateScope === 'excluded'" :class="{ active: candidateScope === 'excluded' }" @click="selectScope('excluded')">排除项</button>
          </div>
        </header>
        <div v-if="detailLoading" class="auto-loading auto-loading--inline">正在切换证据范围...</div>
        <div v-else-if="candidates.length" class="table-wrap candidate-table-wrap">
          <table>
            <thead>
              <tr>
                <th>排名</th>
                <th>标的</th>
                <th>变更</th>
                <th>行业 / 市值桶</th>
                <th class="num">最新价</th>
                <th class="num">涨跌幅</th>
                <th class="num">综合分</th>
                <th class="num">百分位</th>
                <th>证据</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in candidates" :key="item.symbol" :class="{ 'candidate-excluded': !item.eligible }">
                <td><strong>{{ item.rank ?? '—' }}</strong></td>
                <td><strong>{{ item.name }}</strong><small>{{ item.symbol }}</small></td>
                <td><span class="change-pill" :class="`change-${item.change_state}`">{{ changeLabels[item.change_state] || item.change_state }}</span></td>
                <td>{{ item.industry || '未知' }}<small>{{ item.market_cap_bucket || '—' }} · {{ fmtMarketCap(item.total_mv) }}</small></td>
                <td class="num">{{ fmtNumber(item.close_price) }}</td>
                <td class="num" :class="toneForReturn(item.pct_chg)">{{ item.pct_chg == null ? '—' : `${fmtNumber(item.pct_chg)}%` }}</td>
                <td class="num score-cell">{{ fmtNumber(item.composite_score, 4) }}</td>
                <td class="num">{{ fmtPercent(item.percentile) }}</td>
                <td class="evidence-cell">
                  <div v-if="item.exclusion_codes.length || item.constraint_codes.length" class="code-list">
                    <span v-for="code in [...item.exclusion_codes, ...item.constraint_codes]" :key="code">{{ codeLabel(code) }}</span>
                  </div>
                  <details v-if="Object.keys(item.factor_values).length || Object.keys(item.z_scores).length">
                    <summary>原始值 / z-score</summary>
                    <div class="evidence-grid">
                      <span v-for="(value, key) in item.factor_values" :key="`raw-${key}`">
                        {{ featureLabel(String(key)) }} <b>{{ fmtNumber(value, 4) }}</b>
                      </span>
                      <span v-for="(value, key) in item.z_scores" :key="`z-${key}`">
                        z({{ featureLabel(String(key)) }}) <b>{{ fmtNumber(value, 4) }}</b>
                      </span>
                    </div>
                  </details>
                  <span v-else-if="!item.exclusion_codes.length && !item.constraint_codes.length" class="muted-text">暂无扩展因子</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-else class="empty-state">当前范围暂无候选证据。</div>
        <nav
          v-if="!detailLoading && selectedRunDetail && candidateTotal > 0"
          class="candidate-pagination"
          aria-label="候选证据分页"
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

      <section class="analysis-grid">
        <article class="page-section evaluation-section" data-testid="automatic-selection-evaluations">
          <header class="section-header">
            <div>
              <span class="eyebrow">FORWARD EVALUATION</span>
              <h2>5 / 20 / 60 交易日前瞻评估</h2>
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
                <div><dt>最大回撤</dt><dd :class="toneForReturn(evaluation.max_drawdown)">{{ fmtPercent(evaluation.max_drawdown) }}</dd></div>
                <div><dt>Sharpe / IR</dt><dd>{{ fmtNumber(evaluation.sharpe) }} / {{ fmtNumber(evaluation.information_ratio) }}</dd></div>
              </dl>
              <small>{{ fmtTradeDate(evaluation.entry_trade_date) }} → {{ fmtTradeDate(evaluation.exit_trade_date) }}</small>
              <p v-if="evaluation.error">{{ evaluation.error }}</p>
            </article>
          </div>
          <div v-else class="empty-state">运行完成后创建 5 / 20 / 60 日评估窗口。</div>
        </article>

        <article class="page-section explanation-section">
          <header class="section-header">
            <div>
              <span class="eyebrow">POST-PERSISTENCE EXPLANATION</span>
              <h2>证据解释</h2>
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
      </section>

      <section class="page-section strategy-section" data-testid="automatic-selection-strategies">
        <header class="section-header">
          <div>
            <span class="eyebrow">HUMAN-GOVERNED LIFECYCLE</span>
            <h2>策略版本与生命周期</h2>
            <p>配置和哈希创建后不可修改；晋升必须由有权限的人员明确确认。</p>
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
      </section>

      <section class="page-section history-section">
        <header class="section-header">
          <div>
            <span class="eyebrow">RUN LEDGER</span>
            <h2>近期运行账本</h2>
            <p>相同输入复用同一运行；数据修复后以 supersede 链保留旧证据。</p>
          </div>
        </header>
        <div v-if="overview?.recent_runs.length" class="table-wrap">
          <table>
            <thead><tr><th>目标日</th><th>策略</th><th>状态</th><th class="num">研究池</th><th class="num">合格</th><th class="num">Top-N</th><th>输入指纹</th></tr></thead>
            <tbody>
              <tr v-for="run in overview.recent_runs" :key="run.run_id" class="run-row" :class="{ selected: selectedRunId === run.run_id }" @click="loadRun(run.run_id)">
                <td>{{ fmtTradeDate(run.target_trade_date) }}</td>
                <td>{{ run.strategy_name }} <small>v{{ run.strategy_version }}</small></td>
                <td><span class="status-pill" :class="toneForStatus(run.status)">{{ statusLabels[run.status] || run.status }}</span></td>
                <td class="num">{{ run.universe_count }}</td><td class="num">{{ run.eligible_count }}</td><td class="num">{{ run.selected_count }}</td>
                <td><code>{{ run.data_fingerprint.slice(0, 14) }}…</code><small v-if="run.supersedes_run_id">修订运行</small></td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-else class="empty-state">暂无运行账本。</div>
      </section>
    </template>

    <div v-if="strategyAction" class="auto-dialog-backdrop" @mousedown.self="closeAction">
      <form class="auto-dialog" role="dialog" aria-modal="true" @submit.prevent="submitStrategyAction">
        <header>
          <div>
            <span class="eyebrow">MANUAL GOVERNANCE</span>
            <h2 v-if="strategyAction === 'create'">创建不可变策略版本</h2>
            <h2 v-else-if="strategyAction === 'promote'">人工晋升策略</h2>
            <h2 v-else>退役策略</h2>
          </div>
          <button type="button" aria-label="关闭" @click="closeAction">×</button>
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
  </section>
</template>

<style scoped>
.automatic-selection-page { display: grid; gap: 14px; }
.research-boundary { display: flex; align-items: center; gap: 10px; min-height: 44px; padding: 9px 13px; border: 1px solid #554724; border-radius: var(--radius); background: linear-gradient(90deg, rgba(213, 180, 106, .13), rgba(213, 180, 106, .025)); color: #d8cfb5; font-size: 12px; }
.research-boundary strong { color: var(--gold-2); white-space: nowrap; }
.boundary-mark, .eyebrow { color: var(--gold); font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 10px; letter-spacing: .11em; }
.boundary-mark { padding: 3px 6px; border: 1px solid var(--gold-dim); border-radius: 3px; }
.auto-notice { padding: 10px 13px; border: 1px solid; border-radius: var(--radius); font-size: 12px; }
.auto-notice--danger { border-color: rgba(255, 93, 93, .4); background: rgba(255, 93, 93, .08); color: #ff9b9b; }
.auto-notice--success { border-color: rgba(39, 197, 138, .35); background: rgba(39, 197, 138, .08); color: #78dbb8; }
.auto-loading, .empty-state { display: grid; min-height: 120px; place-content: center; gap: 6px; padding: 24px; color: var(--muted); text-align: center; }
.auto-loading--inline { min-height: 86px; }
.empty-state strong { color: var(--text); font-size: 15px; }
.section-header h2 { margin: 3px 0 0; }
.hero-grid, .analysis-grid { display: grid; grid-template-columns: minmax(0, 1.65fr) minmax(300px, .75fr); gap: 14px; }
.run-summary { display: grid; grid-template-columns: minmax(230px, 1.5fr) minmax(140px, .8fr) repeat(3, minmax(90px, .45fr)); min-height: 112px; }
.run-summary > div { display: flex; flex-direction: column; justify-content: center; padding: 17px; border-right: 1px solid var(--line); }
.run-summary > div:last-child { border-right: 0; }
.run-summary span, .run-summary small { color: var(--muted); font-size: 11px; }
.trade-date-block strong { margin: 4px 0; color: var(--gold-2); font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 27px; letter-spacing: -.04em; }
.run-status-block strong { margin: 8px 0 3px; font-size: 14px; }
.run-stat strong { margin-top: 4px; color: var(--text); font-size: 25px; }
.status-pill, .change-pill, .lifecycle-pill { display: inline-flex; align-items: center; width: fit-content; border: 1px solid; border-radius: 99px; padding: 3px 7px; font-size: 10px; white-space: nowrap; }
.tone-ok { border-color: rgba(39, 197, 138, .35); background: rgba(39, 197, 138, .08); color: #6fd8b2; }
.tone-warning { border-color: rgba(213, 166, 66, .4); background: rgba(213, 166, 66, .1); color: #e6c36f; }
.tone-danger { border-color: rgba(255, 93, 93, .4); background: rgba(255, 93, 93, .08); color: #ff8b8b; }
.tone-muted { border-color: var(--line); background: var(--surface-2); color: var(--muted); }
.official-body { display: grid; gap: 13px; padding: 15px; }
.official-body > div { display: grid; gap: 3px; }
.official-body > div strong { color: var(--gold-2); font-size: 16px; }
.official-body > div span, .official-body code { color: var(--muted); font-size: 11px; }
.official-body dl { display: grid; grid-template-columns: repeat(3, 1fr); margin: 0; border: 1px solid var(--line); border-radius: 5px; }
.official-body dl div { padding: 8px; border-right: 1px solid var(--line); }
.official-body dl div:last-child { border-right: 0; }
.official-body dt { color: var(--muted); font-size: 10px; }
.official-body dd { margin: 3px 0 0; color: var(--text); font-weight: 700; }
.auto-button { min-height: 32px; border: 1px solid var(--line-strong); border-radius: 5px; padding: 6px 11px; background: var(--surface-2); color: var(--text); font-weight: 600; font-size: 12px; }
.auto-button:hover:not(:disabled) { border-color: var(--gold-dim); color: var(--gold-2); }
.auto-button:disabled { cursor: not-allowed; opacity: .55; }
.auto-button--primary { border-color: #785f2d; background: linear-gradient(180deg, #c29d4f, #8e6f33); color: #100d07; }
.auto-button--primary:hover:not(:disabled) { color: #050505; filter: brightness(1.08); }
.auto-button--ghost { background: transparent; }
.auto-button--danger { border-color: #7d3030; background: #5a2424; color: #ffd1d1; }
.gate-verdict { align-self: center; border: 1px solid; border-radius: 5px; padding: 7px 10px; font-weight: 700; font-size: 12px; }
.gate-pass { border-color: rgba(39, 197, 138, .45); background: rgba(39, 197, 138, .09); color: #6fd8b2; }
.gate-blocked { border-color: rgba(213, 166, 66, .5); background: rgba(213, 166, 66, .1); color: #e8c66f; }
.quality-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); border-bottom: 1px solid var(--line); }
.quality-grid article { display: grid; gap: 4px; padding: 14px; border-right: 1px solid var(--line); }
.quality-grid span, .quality-grid small { color: var(--muted); font-size: 11px; }
.quality-grid strong { color: var(--text); font-size: 20px; }
.gate-failures { display: grid; gap: 5px; padding: 12px 14px; background: rgba(213, 166, 66, .055); color: #d8c697; font-size: 11px; }
.gate-failures strong { color: var(--warning); }
.ranking-header { align-items: center; }
.scope-tabs { display: inline-flex; padding: 2px; border: 1px solid var(--line); border-radius: 6px; background: var(--bg-2); }
.scope-tabs button { min-height: 28px; border: 0; border-radius: 4px; padding: 4px 10px; background: transparent; color: var(--muted); font-size: 11px; }
.scope-tabs button.active { background: var(--surface-3); color: var(--gold-2); box-shadow: inset 0 0 0 1px var(--line-strong); }
.candidate-table-wrap { max-height: 560px; }
.candidate-pagination { display: flex; align-items: center; justify-content: space-between; gap: 12px; border-top: 1px solid var(--line); padding: 9px 13px; color: var(--muted); font-size: 11px; }
.candidate-pagination > div { display: flex; gap: 7px; }
th { position: sticky; top: 0; z-index: 1; background: #121214; color: var(--muted); font-size: 10px; letter-spacing: .04em; white-space: nowrap; }
td { font-size: 12px; }
td small { display: block; margin-top: 2px; color: var(--muted); font-size: 10px; }
.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.score-cell { color: var(--gold-2); font-weight: 700; }
.candidate-excluded { background: rgba(255, 93, 93, .025); }
.change-pill { border-color: var(--line); color: var(--muted); }
.change-added { border-color: rgba(242, 84, 184, .4); color: #f59dd3; }
.change-retained { border-color: rgba(43, 212, 212, .35); color: #73dddd; }
.change-removed { border-color: rgba(255, 93, 93, .35); color: #ff9292; }
.code-list { display: flex; flex-wrap: wrap; gap: 4px; }
.code-list span { border: 1px solid #493233; border-radius: 3px; padding: 2px 5px; background: rgba(255, 93, 93, .05); color: #d9a2a2; font-size: 10px; }
.evidence-cell { min-width: 190px; }
.evidence-cell details { color: var(--muted); font-size: 10px; }
.evidence-cell summary { cursor: pointer; color: var(--gold); }
.evidence-grid { display: grid; gap: 3px; margin-top: 6px; }
.evidence-grid span { display: flex; justify-content: space-between; gap: 10px; }
.evidence-grid b { color: var(--text); font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }
.muted-text { color: var(--muted-2); font-size: 10px; }
.evaluation-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); }
.evaluation-grid > article { min-width: 0; padding: 14px; border-right: 1px solid var(--line); }
.evaluation-grid > article:last-child { border-right: 0; }
.evaluation-grid header { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.evaluation-grid header strong { color: var(--gold-2); font-size: 16px; }
.evaluation-grid dl { display: grid; gap: 6px; margin: 13px 0; }
.evaluation-grid dl div { display: flex; justify-content: space-between; gap: 8px; }
.evaluation-grid dt { color: var(--muted); font-size: 10px; }
.evaluation-grid dd { margin: 0; font-weight: 700; font-size: 11px; font-variant-numeric: tabular-nums; }
.evaluation-grid article > small, .evaluation-grid article > p { color: var(--muted); font-size: 10px; }
.explanation-section { display: flex; min-height: 285px; flex-direction: column; }
.explanation-body { flex: 1; overflow-wrap: anywhere; padding: 14px; color: #d8d3c6; font-size: 12px; line-height: 1.7; }
.explanation-error { padding: 14px; color: var(--warning); }
.explanation-section footer { border-top: 1px solid var(--line); padding: 8px 14px; color: var(--muted); font-size: 10px; }
.strategy-list { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); }
.strategy-list > article { display: grid; gap: 10px; min-width: 0; padding: 13px; border-right: 1px solid var(--line); border-bottom: 1px solid var(--line); }
.strategy-list > article.official { background: linear-gradient(135deg, rgba(213, 180, 106, .1), transparent 55%); }
.strategy-list article header { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; }
.strategy-list article header > div:first-child { display: flex; align-items: center; gap: 8px; }
.strategy-list article strong { color: var(--text); }
.strategy-list article strong small { color: var(--muted); font-weight: 400; }
.lifecycle-pill { border-color: var(--line); color: var(--muted); }
.lifecycle-official { border-color: #745e2d; background: rgba(213, 180, 106, .1); color: var(--gold-2); }
.lifecycle-challenger { border-color: rgba(242, 84, 184, .35); color: #ee91c9; }
.lifecycle-shadow { border-color: rgba(43, 212, 212, .3); color: #69d5d5; }
.strategy-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 5px; }
.strategy-actions button { border: 0; padding: 2px 4px; background: transparent; color: var(--gold); font-size: 10px; }
.strategy-actions .danger-link { color: #e47777; }
.strategy-meta { display: flex; flex-wrap: wrap; gap: 5px 12px; color: var(--muted); font-size: 10px; }
.strategy-list code { overflow: hidden; color: var(--muted-2); font-size: 9px; text-overflow: ellipsis; white-space: nowrap; }
.strategy-list p { margin: 0; color: var(--muted); font-size: 10px; }
.history-section .table-wrap { max-height: 330px; }
.run-row { cursor: pointer; }
.run-row:hover, .run-row.selected { background: rgba(213, 180, 106, .06); }
.run-row.selected { box-shadow: inset 2px 0 var(--gold); }
.run-row code { color: var(--muted); font-size: 10px; }
.pnl-up { color: #ff6767 !important; }
.pnl-down { color: #35cb8f !important; }
.auto-dialog-backdrop { position: fixed; z-index: 1000; inset: 0; display: grid; place-items: center; padding: 20px; background: rgba(0, 0, 0, .76); backdrop-filter: blur(4px); }
.auto-dialog { width: min(780px, 100%); max-height: calc(100vh - 40px); overflow: auto; border: 1px solid var(--line-strong); border-radius: 8px; background: #101012; box-shadow: 0 24px 80px rgba(0, 0, 0, .6); }
.auto-dialog > header { display: flex; align-items: flex-start; justify-content: space-between; padding: 15px 17px; border-bottom: 1px solid var(--line); }
.auto-dialog h2 { margin: 3px 0 0; color: var(--gold-2); font-size: 16px; }
.auto-dialog > header > button { border: 0; background: transparent; color: var(--muted); font-size: 22px; }
.auto-dialog-body { display: grid; grid-template-columns: 1fr 1fr; gap: 13px; padding: 17px; }
.auto-dialog-body.single-column { grid-template-columns: 1fr; }
.auto-dialog label { display: grid; gap: 6px; color: var(--muted); font-size: 11px; }
.auto-dialog input, .auto-dialog select, .auto-dialog textarea { width: 100%; border: 1px solid var(--line); border-radius: 5px; outline: 0; background: var(--bg-2); padding: 9px 10px; color: var(--text); }
.auto-dialog input:focus, .auto-dialog select:focus, .auto-dialog textarea:focus { border-color: var(--gold-dim); }
.auto-dialog textarea { resize: vertical; font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 11px; line-height: 1.5; }
.dialog-span { grid-column: 1 / -1; }
.auto-dialog-body p { margin: 0; color: var(--muted); font-size: 10px; }
.auto-dialog-body .promotion-evidence-notice { border-left: 2px solid var(--warning); padding: 7px 9px; background: rgba(213, 166, 66, .06); color: #d8c697; line-height: 1.55; }
.action-target { display: grid; gap: 5px; padding: 12px; border: 1px solid var(--line); border-radius: 5px; background: var(--surface-2); }
.action-target code { overflow-wrap: anywhere; color: var(--muted); font-size: 10px; }
.auto-dialog > footer { display: flex; justify-content: flex-end; gap: 8px; padding: 12px 17px; border-top: 1px solid var(--line); }
@media (max-width: 1100px) {
  .hero-grid, .analysis-grid { grid-template-columns: 1fr; }
  .run-summary { grid-template-columns: repeat(3, 1fr); }
  .trade-date-block, .run-status-block { grid-column: span 3; border-bottom: 1px solid var(--line); }
}
@media (max-width: 720px) {
  .research-boundary { align-items: flex-start; flex-direction: column; }
  .research-boundary strong { white-space: normal; }
  .ranking-header { align-items: stretch; flex-direction: column; gap: 10px; }
  .scope-tabs { align-self: flex-start; }
  .candidate-pagination { align-items: stretch; flex-direction: column; }
  .candidate-pagination > div { justify-content: flex-end; }
  .evaluation-grid { grid-template-columns: 1fr; }
  .evaluation-grid > article { border-right: 0; border-bottom: 1px solid var(--line); }
  .run-summary { grid-template-columns: 1fr 1fr 1fr; }
  .auto-dialog-body { grid-template-columns: 1fr; }
  .dialog-span { grid-column: auto; }
}
</style>

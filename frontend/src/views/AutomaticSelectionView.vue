<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { api } from '../api'
import AutomaticSelectionAudit from '../components/AutomaticSelectionAudit.vue'
import type {
  AutomaticSelectionDaily,
  AutomaticSelectionPeriodCell,
  AutomaticSelectionPick,
  AutomaticSelectionStrategy,
  AutomaticSelectionTrackRecord,
  UserPayload,
} from '../types'
import { renderMarkdown } from '../utils/markdown'
import { beijingFromDb } from '../utils/datetime'

const props = defineProps<{ user: UserPayload }>()
const emit = defineEmits<{
  askStock: [pick: AutomaticSelectionPick, tradeDate: string]
  watchlistAdded: [item: Awaited<ReturnType<typeof api.addWatchlist>>]
}>()

type TabKey = 'today' | 'history' | 'method' | 'advanced'
type WatchState = 'adding' | 'added' | 'error'

const activeTab = ref<TabKey>('today')
const daily = ref<AutomaticSelectionDaily | null>(null)
const track = ref<AutomaticSelectionTrackRecord | null>(null)
const loading = ref(true)
const trackLoading = ref(false)
const dispatching = ref(false)
const error = ref('')
const trackError = ref('')
const notice = ref('')
const showExplanation = ref(false)
const detailSymbol = ref('')
const drawerEl = ref<HTMLElement | null>(null)
const watchlistState = ref<Record<string, WatchState>>({})
// 空表示正式策略（服务端默认）；挑战者和正式策略并排看
const strategyId = ref<number | null>(null)
const strategies = ref<AutomaticSelectionStrategy[]>([])
let dailySequence = 0
let trackSequence = 0
let drawerTrigger: HTMLElement | null = null
let previousBodyOverflow = ''

const permissions = computed(() => props.user.permissions || [])
const hasPermission = (name: string) => permissions.value.includes('all') || permissions.value.includes(name)
const canDispatch = computed(() => hasPermission('schedule:manage'))
const canSeeAdvanced = computed(() => hasPermission('model:manage') || hasPermission('schedule:manage'))
const canWrite = computed(() => hasPermission('data:write'))
const canAsk = computed(() => hasPermission('agent:use'))

const tabs = computed<Array<{ key: TabKey; label: string }>>(() => [
  { key: 'today', label: '今日选股' },
  { key: 'history', label: '历史战绩' },
  { key: 'method', label: '选股方法' },
  ...(canSeeAdvanced.value ? [{ key: 'advanced' as TabKey, label: '高级' }] : []),
])

const strategyOptions = computed(() =>
  strategies.value
    .filter((item) => item.enabled && item.lifecycle_state !== 'retired')
    .sort((a, b) => Number(b.is_official) - Number(a.is_official) || a.id - b.id)
)
const currentStrategyId = computed(() => strategyId.value ?? daily.value?.strategy?.id ?? null)
function strategyLabel(item: AutomaticSelectionStrategy): string {
  if (item.is_official) return `${item.name}（正式）`
  return item.name
}

const status = computed(() => daily.value?.status || null)
const picks = computed<AutomaticSelectionPick[]>(() => daily.value?.picks || [])
const detailPick = computed<AutomaticSelectionPick | null>(
  () => picks.value.find((pick) => pick.symbol === detailSymbol.value) || null
)
const dateOptions = computed(() => daily.value?.available_dates || [])
const since = computed(() => daily.value?.performance.since_selection || null)
const dataSync = computed(() => daily.value?.data_sync || null)
const explanationHtml = computed(() => renderMarkdown(daily.value?.run?.llm_explanation || ''))
const trackLine = computed(() => {
  const brief = daily.value?.track_brief
  if (!brief) return '还没有到期的 5 日表现，选出后第 5 个交易日起开始统计。'
  const average = brief.avg_excess_return
  const direction = average == null ? '' : average >= 0 ? `，平均每期跑赢 ${fmtPct(average, false)}` : `，平均每期跑输 ${fmtPct(-average, false)}`
  return `最近 ${brief.periods} 期的 5 日表现：${brief.wins} 期跑赢合格股票平均${direction}。`
})
const trackSummary = computed(() => track.value?.summary || [])
const trackPeriods = computed(() => track.value?.periods || [])
const trackBars = computed(() => {
  const points = [...trackPeriods.value]
    .reverse()
    .map((period) => ({ date: period.trade_date, value: period.horizons['5']?.excess_return ?? null }))
    .filter((point): point is { date: string; value: number } => point.value != null)
  const peak = Math.max(0.01, ...points.map((point) => Math.abs(point.value)))
  const width = Math.max(points.length, 1)
  return points.map((point, index) => {
    const height = Math.max(1, (Math.abs(point.value) / peak) * 46)
    return {
      ...point,
      x: (index / width) * 100,
      width: Math.max(0.6, 100 / width - 0.6),
      y: point.value >= 0 ? 50 - height : 50,
      height,
    }
  })
})

const EMPTY_PERIOD_CELL: AutomaticSelectionPeriodCell = { status: 'missing', status_label: '—' }

function periodCells(period: AutomaticSelectionTrackRecord['periods'][number]): AutomaticSelectionPeriodCell[] {
  return ['5', '20', '60'].map((key) => period.horizons[key] || EMPTY_PERIOD_CELL)
}

function apiMessage(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason)
}

function fmtDate(value: string | null | undefined): string {
  if (!value) return '—'
  const digits = value.replace(/-/g, '')
  if (digits.length !== 8) return value
  return `${Number(digits.slice(4, 6))} 月 ${Number(digits.slice(6, 8))} 日`
}

function fmtFullDate(value: string | null | undefined): string {
  if (!value) return '—'
  const digits = value.replace(/-/g, '')
  if (digits.length !== 8) return value
  return `${digits.slice(0, 4)}-${digits.slice(4, 6)}-${digits.slice(6, 8)}`
}

function fmtPct(value: number | null | undefined, signed = true, digits = 2): string {
  if (value == null || !Number.isFinite(value)) return '—'
  const text = `${(value * 100).toFixed(digits)}%`
  return signed && value > 0 ? `+${text}` : text
}

function fmtNum(value: number | null | undefined, digits = 2): string {
  if (value == null || !Number.isFinite(value)) return '—'
  return value.toLocaleString('zh-CN', { maximumFractionDigits: digits, minimumFractionDigits: digits })
}

function fmtCount(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '—'
  return value.toLocaleString('zh-CN')
}

function toneForReturn(value: number | null | undefined): string {
  if (value == null || value === 0) return ''
  return value > 0 ? 'pnl-up' : 'pnl-down'
}

function scoreTone(score: number | null | undefined): string {
  if (score == null) return 'score-unknown'
  if (score >= 70) return 'score-strong'
  if (score < 30) return 'score-weak'
  return 'score-medium'
}

function sparkPoints(path: AutomaticSelectionPick['price_path']): string {
  if (path.length < 2) return ''
  const values = path.map((point) => point.value)
  const low = Math.min(...values)
  const high = Math.max(...values)
  const span = high - low || 1
  return values
    .map((value, index) => `${((index / (values.length - 1)) * 96 + 2).toFixed(1)},${(26 - ((value - low) / span) * 22).toFixed(1)}`)
    .join(' ')
}

function pathChange(path: AutomaticSelectionPick['price_path']): number | null {
  return path.length >= 2 ? path[path.length - 1].value / path[0].value - 1 : null
}

function funnelWidth(count: number): string {
  const universe = daily.value?.funnel?.steps[0]?.count || 0
  if (!universe) return '0%'
  return `${Math.max(4, (count / universe) * 100).toFixed(1)}%`
}

async function loadDaily(tradeDate = '') {
  const sequence = ++dailySequence
  loading.value = true
  error.value = ''
  closeDetail(false)
  try {
    const next = await api.automaticSelectionDaily(tradeDate || null, strategyId.value)
    if (sequence !== dailySequence) return
    daily.value = next
    showExplanation.value = false
  } catch (reason) {
    if (sequence === dailySequence) error.value = apiMessage(reason)
  } finally {
    if (sequence === dailySequence) loading.value = false
  }
}

async function loadTrack(force = false) {
  if ((track.value || trackLoading.value) && !force) return
  const sequence = ++trackSequence
  trackLoading.value = true
  trackError.value = ''
  try {
    const next = await api.automaticSelectionTrackRecord(120, strategyId.value)
    if (sequence !== trackSequence) return
    track.value = next
  } catch (reason) {
    if (sequence === trackSequence) trackError.value = apiMessage(reason)
  } finally {
    if (sequence === trackSequence) trackLoading.value = false
  }
}

async function loadStrategies() {
  try {
    strategies.value = await api.automaticSelectionStrategies(false)
  } catch {
    strategies.value = [] // 拿不到方案列表时只看正式策略，不影响主流程
  }
}

function selectStrategy(value: string) {
  const id = Number(value)
  strategyId.value = Number.isFinite(id) && id > 0 ? id : null
  daily.value = null
  trackSequence += 1
  track.value = null
  trackLoading.value = false
  trackError.value = ''
  void loadDaily()
  if (activeTab.value === 'history') void loadTrack(true)
}

function selectDate(tradeDate: string) {
  const latest = dateOptions.value[0]?.trade_date
  void loadDaily(tradeDate === latest ? '' : tradeDate)
}

function openDate(tradeDate: string) {
  activeTab.value = 'today'
  selectDate(tradeDate)
}

async function refresh() {
  notice.value = ''
  const current = daily.value && !daily.value.is_latest ? daily.value.trade_date || '' : ''
  await loadDaily(current)
  if (track.value) await loadTrack(true)
}

async function dispatchRun() {
  if (!canDispatch.value || dispatching.value) return
  dispatching.value = true
  error.value = ''
  notice.value = ''
  try {
    await api.automaticSelectionDispatch()
    notice.value = '已开始重新同步数据并计算，通常几分钟内完成，稍后点击“刷新”查看。'
  } catch (reason) {
    error.value = apiMessage(reason)
  } finally {
    dispatching.value = false
  }
}

function selectTab(key: TabKey) {
  activeTab.value = key
  if (key === 'history') void loadTrack()
}

async function handleTabKeydown(event: KeyboardEvent) {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
  event.preventDefault()
  const keys = tabs.value.map((tab) => tab.key)
  const index = keys.indexOf(activeTab.value)
  const nextIndex = event.key === 'Home'
    ? 0
    : event.key === 'End'
      ? keys.length - 1
      : (index + (event.key === 'ArrowRight' ? 1 : -1) + keys.length) % keys.length
  selectTab(keys[nextIndex])
  await nextTick()
  if (typeof document !== 'undefined') document.getElementById(`auto-tab-${keys[nextIndex]}`)?.focus()
}

function openDetail(symbol: string) {
  if (!picks.value.some((pick) => pick.symbol === symbol)) return
  if (typeof document !== 'undefined') {
    drawerTrigger = document.activeElement instanceof HTMLElement ? document.activeElement : null
    if (!detailSymbol.value) {
      previousBodyOverflow = document.body.style.overflow
      document.body.style.overflow = 'hidden'
    }
  }
  detailSymbol.value = symbol
  void nextTick(() => drawerEl.value?.focus())
}

function closeDetail(restoreFocus = true) {
  if (!detailSymbol.value) return
  detailSymbol.value = ''
  if (typeof document !== 'undefined') document.body.style.overflow = previousBodyOverflow
  const trigger = drawerTrigger
  drawerTrigger = null
  if (restoreFocus) void nextTick(() => trigger?.focus())
}

function trapDrawerFocus(event: KeyboardEvent) {
  const drawer = drawerEl.value
  if (!drawer || typeof document === 'undefined') return
  const focusable = Array.from(drawer.querySelectorAll<HTMLElement>('button:not(:disabled), [tabindex]:not([tabindex="-1"])'))
  if (!focusable.length) return
  const first = focusable[0]
  const last = focusable[focusable.length - 1]
  if (event.shiftKey && (document.activeElement === first || document.activeElement === drawer)) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first.focus()
  }
}

async function addToWatchlist(pick: AutomaticSelectionPick) {
  const state = watchlistState.value[pick.symbol]
  if (!canWrite.value || state === 'adding' || state === 'added') return
  watchlistState.value = { ...watchlistState.value, [pick.symbol]: 'adding' }
  try {
    const item = await api.addWatchlist({
      symbol: pick.symbol,
      group_name: '自动选股',
      reason: `自动选股 ${fmtFullDate(daily.value?.trade_date)} 入选：${pick.headline}`.slice(0, 500),
    })
    watchlistState.value = { ...watchlistState.value, [pick.symbol]: 'added' }
    emit('watchlistAdded', item)
  } catch {
    watchlistState.value = { ...watchlistState.value, [pick.symbol]: 'error' }
  }
}

function askStock(pick: AutomaticSelectionPick) {
  if (!canAsk.value) return
  closeDetail(false)
  emit('askStock', pick, daily.value?.trade_date || '')
}

function watchLabel(symbol: string): string {
  const state = watchlistState.value[symbol]
  if (state === 'adding') return '添加中...'
  if (state === 'added') return '已加入自选'
  if (state === 'error') return '加入失败，重试'
  return '加入自选'
}

onMounted(() => {
  void loadDaily()
  void loadStrategies()
})
onBeforeUnmount(() => {
  if (detailSymbol.value && typeof document !== 'undefined') document.body.style.overflow = previousBodyOverflow
})
</script>

<template>
  <section class="auto-page" aria-label="每日自动选股" :aria-busy="loading">
    <header class="auto-command" data-testid="automatic-selection-command">
      <div class="auto-context">
        <label class="date-picker">
          <span>交易日</span>
          <select
            :value="daily?.trade_date || ''"
            :disabled="loading || !dateOptions.length"
            aria-label="选择交易日"
            data-testid="automatic-selection-date"
            @change="selectDate(($event.target as HTMLSelectElement).value)"
          >
            <option v-if="!dateOptions.length" value="">暂无结果</option>
            <option v-for="(option, index) in dateOptions" :key="option.trade_date" :value="option.trade_date">
              {{ fmtFullDate(option.trade_date) }}{{ index === 0 ? '（最新）' : '' }}{{ option.status === 'completed' ? '' : ` · ${option.status_label}` }}
            </option>
          </select>
        </label>
        <label v-if="strategyOptions.length > 1" class="date-picker">
          <span>选股方案</span>
          <select
            :value="currentStrategyId ?? ''"
            :disabled="loading"
            aria-label="选择选股方案"
            data-testid="automatic-selection-strategy"
            @change="selectStrategy(($event.target as HTMLSelectElement).value)"
          >
            <option v-for="item in strategyOptions" :key="item.id" :value="item.id">{{ strategyLabel(item) }}</option>
          </select>
        </label>
        <div v-else-if="daily?.strategy" class="strategy-chip">
          <span>选股方案</span>
          <strong>{{ daily.strategy.name }}</strong>
        </div>
      </div>
      <div class="auto-actions">
        <button class="ui-btn ui-btn--ghost" type="button" :disabled="loading" @click="refresh">刷新</button>
        <button
          v-if="canDispatch"
          class="ui-btn ui-btn--primary"
          type="button"
          :disabled="dispatching"
          data-testid="automatic-selection-dispatch"
          @click="dispatchRun"
        >
          {{ dispatching ? '正在提交...' : '重新计算' }}
        </button>
      </div>
    </header>

    <p class="auto-disclaimer" role="note">
      每个交易日收盘后，系统按固定规则从全市场挑出一批观察股，并持续跟踪它们之后的表现。{{ daily?.disclaimer || '这是按固定规则打分排序的研究结果，不是对未来涨跌的预测，也不构成投资建议。' }}
    </p>

    <div class="message-stack" aria-live="polite" aria-atomic="true">
      <div v-if="error" class="auto-notice auto-notice--danger" role="alert">{{ error }}</div>
      <div v-if="notice" class="auto-notice auto-notice--success" role="status">{{ notice }}</div>
    </div>

    <div class="ui-tabs auto-tabs" role="tablist" aria-label="自动选股内容" @keydown="handleTabKeydown">
      <button
        v-for="tab in tabs"
        :id="`auto-tab-${tab.key}`"
        :key="tab.key"
        type="button"
        role="tab"
        :aria-selected="activeTab === tab.key"
        :aria-controls="`auto-panel-${tab.key}`"
        :tabindex="activeTab === tab.key ? 0 : -1"
        @click="selectTab(tab.key)"
      >
        {{ tab.label }}
      </button>
    </div>

    <!-- 今日选股 -->
    <section
      v-if="activeTab === 'today'"
      id="auto-panel-today"
      class="tab-panel"
      role="tabpanel"
      aria-labelledby="auto-tab-today"
      data-testid="automatic-selection-today"
    >
      <div v-if="loading && !daily" class="ui-empty" role="status">
        <strong>正在读取选股结果...</strong>
      </div>

      <template v-else-if="daily && status">
        <section
          v-if="dataSync && !dataSync.ok"
          class="sync-panel ui-panel"
          :class="`sync-${dataSync.status}`"
          role="status"
          data-testid="automatic-selection-data-sync"
        >
          <header>
            <strong>数据同步：{{ dataSync.title }}</strong>
            <span v-if="dataSync.at">
              {{ beijingFromDb(dataSync.at) }}<template v-if="dataSync.trigger_label"> · {{ dataSync.trigger_label }}触发</template><template v-if="dataSync.target_trade_date"> · 目标交易日 {{ fmtDate(dataSync.target_trade_date) }}</template>
            </span>
          </header>
          <ul v-if="dataSync.issues.length">
            <li v-for="issue in dataSync.issues" :key="issue">{{ issue }}</li>
          </ul>
          <p v-if="dataSync.next_step">{{ dataSync.next_step }}</p>
          <details v-if="dataSync.detail && canSeeAdvanced" class="tech-detail">
            <summary>技术详情</summary>
            <p>{{ dataSync.detail }}</p>
          </details>
        </section>
        <p v-else-if="dataSync?.ok" class="sync-line" data-testid="automatic-selection-data-sync">
          行情数据：{{ dataSync.title }}<template v-if="dataSync.at">（{{ beijingFromDb(dataSync.at) }}）</template>
        </p>

        <section
          v-if="status.code === 'ok'"
          class="conclusion ui-panel"
          aria-labelledby="conclusion-title"
          data-testid="automatic-selection-conclusion"
        >
          <div class="conclusion-main">
            <h2 id="conclusion-title">{{ status.title }}</h2>
            <p class="conclusion-headline">{{ daily.summary?.headline }}</p>
            <ul class="conclusion-facts">
              <li>
                <span>全市场</span><strong>{{ fmtCount(daily.summary?.universe_count) }}</strong>
                <i aria-hidden="true">→</i>
                <span>通过基础筛选</span><strong>{{ fmtCount(daily.summary?.eligible_count) }}</strong>
                <i aria-hidden="true">→</i>
                <span>入选</span><strong>{{ fmtCount(daily.summary?.selected_count) }}</strong>
              </li>
              <li>
                <span>比上期</span>
                <b class="change-added">新进 {{ daily.summary?.added ?? 0 }}</b>
                <b>保留 {{ daily.summary?.retained ?? 0 }}</b>
                <b>移出 {{ daily.summary?.removed ?? 0 }}</b>
              </li>
            </ul>
          </div>
          <div class="conclusion-side">
            <div class="since-card" data-testid="automatic-selection-since">
              <span>选出后至今</span>
              <template v-if="since?.status === 'observing'">
                <strong :class="toneForReturn(since.basket_return)">{{ fmtPct(since.basket_return) }}</strong>
                <small>
                  第 {{ since.trading_days }} 个交易日 · 合格股票平均
                  <em :class="toneForReturn(since.benchmark_return)">{{ fmtPct(since.benchmark_return) }}</em>
                </small>
                <small>{{ since.note }}，截至 {{ fmtDate(since.as_of) }}</small>
              </template>
              <template v-else>
                <strong class="muted-value">观察中</strong>
                <small>{{ since?.note || '下一个交易日开盘后开始观察' }}</small>
              </template>
            </div>
            <p class="track-line">{{ trackLine }}</p>
          </div>
          <div v-if="explanationHtml" class="explanation">
            <button
              class="ui-btn ui-btn--text"
              type="button"
              :aria-expanded="showExplanation"
              aria-controls="auto-explanation"
              @click="showExplanation = !showExplanation"
            >
              {{ showExplanation ? '收起 AI 点评' : '展开 AI 点评' }}
            </button>
            <div v-if="showExplanation" id="auto-explanation" class="explanation-body" v-html="explanationHtml"></div>
          </div>
        </section>

        <section
          v-else
          class="status-panel ui-panel"
          :class="`status-${status.code}`"
          role="status"
          data-testid="automatic-selection-status"
        >
          <h2>{{ status.title }}</h2>
          <p>{{ status.message }}</p>
          <ul v-if="status.issues.length">
            <li v-for="issue in status.issues" :key="issue">{{ issue }}</li>
          </ul>
          <details v-if="status.detail && canSeeAdvanced" class="tech-detail">
            <summary>技术详情</summary>
            <p>{{ status.detail }}</p>
          </details>
          <div class="status-actions">
            <button
              v-if="status.fallback_trade_date"
              class="ui-btn ui-btn--ghost"
              type="button"
              @click="selectDate(status.fallback_trade_date)"
            >
              查看最近一次结果（{{ fmtDate(status.fallback_trade_date) }}）
            </button>
            <button
              v-if="canDispatch && status.code !== 'running'"
              class="ui-btn ui-btn--ghost"
              type="button"
              :disabled="dispatching"
              @click="dispatchRun"
            >
              立即重新计算
            </button>
          </div>
        </section>

        <section v-if="picks.length" class="picks ui-panel" aria-labelledby="picks-title" data-testid="automatic-selection-picks">
          <header class="ui-panel-head">
            <div>
              <h3 id="picks-title">入选股票</h3>
              <p>按综合得分从高到低排列；条形表示这一项超过了百分之多少的合格股票。点击查看详情。</p>
            </div>
          </header>
          <ol class="pick-list">
            <li v-for="pick in picks" :key="pick.symbol">
              <button
                type="button"
                class="pick-row"
                :aria-label="`${pick.name} 第 ${pick.rank} 名，查看详情`"
                @click="openDetail(pick.symbol)"
              >
                <span class="pick-rank">{{ pick.rank }}</span>
                <span class="pick-identity">
                  <strong>{{ pick.name }}</strong>
                  <small>{{ pick.symbol }} · {{ pick.industry || '未分类' }}{{ pick.market_cap_label ? ` · ${pick.market_cap_label}` : '' }}</small>
                  <span class="pick-tags">
                    <em v-if="pick.change_state === 'added'" class="tag-new">新进</em>
                    <em v-if="pick.risks.some((risk) => risk.level === 'warning')" class="tag-risk">需留意</em>
                  </span>
                </span>
                <span class="pick-reason">{{ pick.headline }}</span>
                <span class="pick-dims" aria-hidden="true">
                  <span v-for="dim in pick.dimensions" :key="dim.key" class="dim-bar" :class="scoreTone(dim.score)">
                    <b>{{ dim.label }}</b>
                    <i><s :style="{ width: `${dim.score ?? 0}%` }"></s></i>
                    <em>{{ dim.score ?? '—' }}</em>
                  </span>
                </span>
                <span class="pick-perf">
                  <small>选出后</small>
                  <strong :class="toneForReturn(pick.performance.since_selection)">
                    {{ pick.performance.since_selection == null ? '观察中' : fmtPct(pick.performance.since_selection) }}
                  </strong>
                </span>
                <svg v-if="pick.price_path.length" class="pick-spark" viewBox="0 0 100 28" preserveAspectRatio="none" role="img" :aria-label="`选出前 20 个交易日走势 ${fmtPct(pathChange(pick.price_path))}`">
                  <polyline :points="sparkPoints(pick.price_path)" :class="toneForReturn(pathChange(pick.price_path))" />
                </svg>
                <span v-else class="pick-spark"></span>
              </button>
            </li>
          </ol>
          <p v-if="daily.removed.length" class="removed-line">
            本期移出 {{ daily.removed.length }} 只：{{ daily.removed.slice(0, 12).map((item) => item.name).join('、') }}{{ daily.removed.length > 12 ? ' 等' : '' }}
          </p>
        </section>

        <section v-if="daily.performance.horizons.length" class="horizons ui-panel" aria-labelledby="horizons-title">
          <header class="ui-panel-head">
            <div>
              <h3 id="horizons-title">这一批股票之后的表现</h3>
              <p>以下一个交易日开盘价为起点，扣除佣金和滑点，和同一天所有合格股票的平均表现对比。</p>
            </div>
          </header>
          <div class="horizon-grid">
            <article v-for="item in daily.performance.horizons" :key="item.horizon">
              <header>
                <strong>{{ item.horizon }} 个交易日</strong>
                <span class="ui-pill" :class="item.status === 'completed' ? 'ui-pill--ok' : item.status === 'pending' ? 'ui-pill--muted' : 'ui-pill--warning'">{{ item.status_label }}</span>
              </header>
              <template v-if="item.status === 'completed'">
                <dl>
                  <div><dt>入选组合</dt><dd :class="toneForReturn(item.basket_return)">{{ fmtPct(item.basket_return) }}</dd></div>
                  <div><dt>合格股票平均</dt><dd :class="toneForReturn(item.benchmark_return)">{{ fmtPct(item.benchmark_return) }}</dd></div>
                  <div>
                    <dt>{{ (item.excess_return ?? 0) >= 0 ? '跑赢' : '跑输' }}</dt>
                    <dd :class="toneForReturn(item.excess_return)">{{ fmtPct(item.excess_return == null ? null : Math.abs(item.excess_return), false) }}</dd>
                  </div>
                </dl>
                <small>{{ fmtDate(item.entry_trade_date) }} → {{ fmtDate(item.exit_trade_date) }}</small>
              </template>
              <p v-else>{{ item.status === 'pending' ? '还没到期，到期后自动统计。' : item.note || '这一期数据不足，系统会继续重试。' }}</p>
            </article>
          </div>
        </section>
      </template>

      <div v-else-if="!loading" class="ui-empty">
        <strong>暂时读不到选股结果</strong>
        <p>请稍后点击“刷新”重试。</p>
      </div>
    </section>

    <!-- 历史战绩 -->
    <section
      v-else-if="activeTab === 'history'"
      id="auto-panel-history"
      class="tab-panel"
      role="tabpanel"
      aria-labelledby="auto-tab-history"
      data-testid="automatic-selection-history"
    >
      <div v-if="trackLoading && !track" class="ui-empty" role="status"><strong>正在读取历史战绩...</strong></div>
      <div v-else-if="trackError" class="auto-notice auto-notice--danger" role="alert">{{ trackError }}</div>
      <template v-else-if="track">
        <div class="summary-grid">
          <article v-for="item in trackSummary" :key="item.horizon" class="ui-panel summary-card">
            <header>
              <h3>{{ item.horizon }} 个交易日后</h3>
              <span>{{ item.completed }} 期已到期<template v-if="item.pending"> · {{ item.pending }} 期观察中</template></span>
            </header>
            <template v-if="item.completed">
              <strong class="summary-rate">{{ item.wins }} / {{ item.completed }}</strong>
              <small>期跑赢合格股票平均（胜率 {{ fmtPct(item.win_rate, false, 0) }}）</small>
              <dl>
                <div><dt>平均每期{{ (item.avg_excess_return ?? 0) >= 0 ? '跑赢' : '跑输' }}</dt><dd :class="toneForReturn(item.avg_excess_return)">{{ fmtPct(item.avg_excess_return == null ? null : Math.abs(item.avg_excess_return), false) }}</dd></div>
                <div><dt>入选组合平均</dt><dd :class="toneForReturn(item.avg_basket_return)">{{ fmtPct(item.avg_basket_return) }}</dd></div>
                <div><dt>最好 / 最差一期</dt><dd><span :class="toneForReturn(item.best_excess_return)">{{ fmtPct(item.best_excess_return) }}</span> / <span :class="toneForReturn(item.worst_excess_return)">{{ fmtPct(item.worst_excess_return) }}</span></dd></div>
              </dl>
            </template>
            <p v-else class="summary-empty">还没有到期的结果。</p>
          </article>
        </div>

        <section v-if="trackBars.length" class="ui-panel chart-panel" aria-labelledby="track-chart-title">
          <header class="ui-panel-head">
            <div>
              <h3 id="track-chart-title">每期 5 日表现：跑赢（红）/ 跑输（绿）合格股票平均</h3>
              <p>从左到右按时间排列；柱子越高，差距越大。</p>
            </div>
          </header>
          <svg class="track-chart" viewBox="0 0 100 100" preserveAspectRatio="none" role="img" aria-label="每期 5 日超额表现柱状图">
            <line x1="0" y1="50" x2="100" y2="50" class="zero-line" />
            <rect
              v-for="bar in trackBars"
              :key="bar.date"
              :x="bar.x"
              :y="bar.y"
              :width="bar.width"
              :height="bar.height"
              :class="toneForReturn(bar.value)"
            >
              <title>{{ fmtFullDate(bar.date) }}：{{ fmtPct(bar.value) }}</title>
            </rect>
          </svg>
        </section>

        <section class="ui-panel" aria-labelledby="track-table-title">
          <header class="ui-panel-head">
            <div>
              <h3 id="track-table-title">每期明细</h3>
              <p>点击日期查看当天选出的股票。</p>
            </div>
          </header>
          <div v-if="trackPeriods.length" class="ui-table-wrap track-table-wrap">
            <table class="ui-table track-table">
              <thead>
                <tr>
                  <th>选出日期</th>
                  <th class="num">入选</th>
                  <th v-for="horizon in [5, 20, 60]" :key="horizon" class="num">{{ horizon }} 日：组合 / 平均 / 差距</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="period in trackPeriods" :key="period.run_id">
                  <td>
                    <button class="ui-btn ui-btn--text" type="button" @click="openDate(period.trade_date)">{{ fmtFullDate(period.trade_date) }}</button>
                  </td>
                  <td class="num">{{ period.selected_count }}</td>
                  <td v-for="(cell, index) in periodCells(period)" :key="index" class="num">
                    <template v-if="cell.status === 'completed'">
                      <span :class="toneForReturn(cell.basket_return)">{{ fmtPct(cell.basket_return) }}</span>
                      / {{ fmtPct(cell.benchmark_return) }} /
                      <strong :class="toneForReturn(cell.excess_return)">{{ fmtPct(cell.excess_return) }}</strong>
                    </template>
                    <span v-else class="muted-value">{{ cell.status_label }}</span>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <div v-else class="ui-empty">
            <strong>还没有完成的选股记录</strong>
            <p>每个交易日收盘后自动生成，之后在这里累积战绩。</p>
          </div>
        </section>

        <ul class="notes">
          <li v-for="note in track.notes" :key="note">{{ note }}</li>
        </ul>
      </template>
    </section>

    <!-- 选股方法 -->
    <section
      v-else-if="activeTab === 'method'"
      id="auto-panel-method"
      class="tab-panel method-panel"
      role="tabpanel"
      aria-labelledby="auto-tab-method"
      data-testid="automatic-selection-method"
    >
      <section v-if="daily?.funnel" class="ui-panel" aria-labelledby="funnel-title">
        <header class="ui-panel-head">
          <div>
            <h3 id="funnel-title">{{ fmtDate(daily.trade_date) }}：从全市场到入选</h3>
            <p>{{ daily.funnel.overlap_note }}</p>
          </div>
        </header>
        <div class="ui-panel-body funnel">
          <div v-for="step in daily.funnel.steps" :key="step.key" class="funnel-step">
            <div class="funnel-bar" :style="{ width: funnelWidth(step.count) }">
              <strong>{{ fmtCount(step.count) }}</strong>
              <span>{{ step.label }}</span>
            </div>
            <p>{{ step.note }}</p>
          </div>
          <div v-if="daily.funnel.exclusions.length" class="exclusion-list">
            <h4>剔除原因</h4>
            <ul>
              <li v-for="item in daily.funnel.exclusions" :key="item.code"><span>{{ item.label }}</span><b>{{ fmtCount(item.count) }}</b></li>
            </ul>
          </div>
        </div>
      </section>

      <div v-if="daily" class="method-grid">
        <section class="ui-panel">
          <header class="ui-panel-head"><h3>第一步：基础筛选</h3></header>
          <ul class="ui-panel-body rule-list">
            <li v-for="rule in daily.method.filters" :key="rule">{{ rule }}</li>
          </ul>
        </section>
        <section class="ui-panel">
          <header class="ui-panel-head"><h3>第二步：打分</h3></header>
          <div class="ui-panel-body">
            <ul class="weight-list">
              <li v-for="dim in daily.method.dimensions" :key="dim.key">
                <span><b>{{ dim.label }}</b><em>{{ dim.weight_percent }}%</em></span>
                <i><s :style="{ width: `${dim.weight_percent}%` }"></s></i>
                <small>{{ dim.description }}</small>
              </li>
            </ul>
            <p class="method-note">{{ daily.method.scoring }}</p>
          </div>
        </section>
        <section class="ui-panel">
          <header class="ui-panel-head"><h3>第三步：挑选</h3></header>
          <ul class="ui-panel-body rule-list">
            <li v-for="rule in daily.method.selection" :key="rule">{{ rule }}</li>
          </ul>
        </section>
        <section class="ui-panel">
          <header class="ui-panel-head"><h3>怎么检验效果</h3></header>
          <ul class="ui-panel-body rule-list">
            <li v-for="rule in daily.method.evaluation" :key="rule">{{ rule }}</li>
            <li>{{ daily.method.schedule }}</li>
          </ul>
        </section>
      </div>
      <p v-if="daily" class="method-limit">{{ daily.method.disclaimer }}{{ daily.method.limitations }}</p>
    </section>

    <!-- 高级：运行记录与策略管理 -->
    <section
      v-else-if="activeTab === 'advanced' && canSeeAdvanced"
      id="auto-panel-advanced"
      class="tab-panel"
      role="tabpanel"
      aria-labelledby="auto-tab-advanced"
    >
      <AutomaticSelectionAudit :user="user" />
    </section>

    <Teleport to="body">
      <div v-if="detailPick" class="drawer-backdrop" @mousedown.self="closeDetail()">
        <aside
          ref="drawerEl"
          class="pick-drawer"
          role="dialog"
          aria-modal="true"
          aria-labelledby="pick-drawer-title"
          tabindex="-1"
          data-testid="automatic-selection-detail"
          @keydown.esc.stop.prevent="closeDetail()"
          @keydown.tab="trapDrawerFocus"
        >
          <header class="drawer-head">
            <div>
              <h2 id="pick-drawer-title">{{ detailPick.name }} <small>{{ detailPick.symbol }}</small></h2>
              <p>
                第 {{ detailPick.rank }} 名 / {{ fmtCount(detailPick.eligible_count) }} 只合格股票
                <template v-if="detailPick.top_percent != null">（前 {{ detailPick.top_percent }}%）</template>
                · {{ detailPick.industry || '未分类' }}<template v-if="detailPick.change_label !== '—'"> · {{ detailPick.change_label }}</template>
              </p>
            </div>
            <button type="button" class="drawer-close" aria-label="关闭详情" @click="closeDetail()">×</button>
          </header>

          <section class="drawer-block">
            <h3>为什么入选</h3>
            <ul class="reason-list">
              <li v-for="dim in detailPick.dimensions" :key="dim.key" :class="scoreTone(dim.score)">
                <div class="reason-head">
                  <b>{{ dim.label }}</b>
                  <span>{{ dim.score ?? '—' }} 分 · 权重 {{ dim.weight_percent }}%</span>
                </div>
                <i><s :style="{ width: `${dim.score ?? 0}%` }"></s></i>
                <p>{{ dim.detail }}</p>
              </li>
            </ul>
          </section>

          <section class="drawer-block">
            <h3>需要注意</h3>
            <ul v-if="detailPick.risks.length" class="risk-list">
              <li v-for="risk in detailPick.risks" :key="risk.text" :class="`risk-${risk.level}`">{{ risk.text }}</li>
            </ul>
            <p v-else class="muted-value">按规则检查没有发现明显风险点，但这不代表没有风险。</p>
          </section>

          <section class="drawer-block">
            <h3>关键数据</h3>
            <dl class="fact-grid">
              <div><dt>收盘价</dt><dd>{{ fmtNum(detailPick.close_price) }} 元</dd></div>
              <div><dt>当天涨跌</dt><dd :class="toneForReturn(detailPick.pct_chg)">{{ detailPick.pct_chg == null ? '—' : `${fmtNum(detailPick.pct_chg)}%` }}</dd></div>
              <div><dt>总市值</dt><dd>{{ fmtNum(detailPick.total_mv_yi, 1) }} 亿元</dd></div>
              <div><dt>市盈率 / 市净率</dt><dd>{{ fmtNum(detailPick.pe_ttm, 1) }} / {{ fmtNum(detailPick.pb) }}</dd></div>
              <div><dt>近 20 日涨跌</dt><dd :class="toneForReturn(detailPick.momentum_20)">{{ fmtPct(detailPick.momentum_20) }}</dd></div>
              <div><dt>日均成交额</dt><dd>{{ fmtNum(detailPick.avg_amount_yi) }} 亿元</dd></div>
            </dl>
          </section>

          <section class="drawer-block">
            <h3>选出后表现</h3>
            <dl class="fact-grid">
              <div>
                <dt>至今</dt>
                <dd :class="toneForReturn(detailPick.performance.since_selection)">
                  {{ detailPick.performance.since_selection == null ? '观察中' : fmtPct(detailPick.performance.since_selection) }}
                </dd>
              </div>
              <div v-for="item in detailPick.performance.horizons" :key="item.horizon">
                <dt>{{ item.horizon }} 个交易日</dt>
                <dd :class="toneForReturn(item.net_return)">{{ item.net_return == null ? item.status_label : fmtPct(item.net_return) }}</dd>
              </div>
            </dl>
            <p v-if="since?.note" class="muted-value">“至今”{{ since.note }}。</p>
          </section>

          <footer class="drawer-actions">
            <button v-if="canAsk" class="ui-btn ui-btn--primary" type="button" @click="askStock(detailPick)">问股：深入分析</button>
            <button
              v-if="canWrite"
              class="ui-btn ui-btn--ghost"
              type="button"
              :disabled="watchlistState[detailPick.symbol] === 'adding' || watchlistState[detailPick.symbol] === 'added'"
              @click="addToWatchlist(detailPick)"
            >
              {{ watchLabel(detailPick.symbol) }}
            </button>
          </footer>
          <p class="drawer-foot">数据日期 {{ fmtFullDate(daily?.trade_date) }}<template v-if="daily?.run?.finished_at"> · 计算完成于 {{ beijingFromDb(daily.run.finished_at) }}</template></p>
        </aside>
      </div>
    </Teleport>
  </section>
</template>

<style scoped>
.auto-page { display: grid; gap: 12px; color: var(--text); }

.auto-command {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  border: 1px solid var(--line-strong);
  border-radius: 8px;
  padding: 12px 16px;
  background: var(--surface);
}
.auto-context { display: flex; flex-wrap: wrap; align-items: center; gap: 16px; }
.date-picker { display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: 12px; }
.date-picker select {
  min-height: 34px;
  border: 1px solid var(--line);
  border-radius: 4px;
  padding: 4px 8px;
  background: var(--bg-2);
  color: var(--text);
  font-size: 14px;
  font-variant-numeric: tabular-nums;
}
.strategy-chip { display: grid; gap: 2px; }
.strategy-chip span { color: var(--muted); font-size: 12px; }
.strategy-chip strong { font-size: 13px; font-weight: 500; }
.auto-actions { display: flex; gap: 8px; }
.auto-disclaimer { margin: 0; color: var(--muted); font-size: 12px; line-height: 1.6; }

.message-stack:empty { display: none; }
.message-stack { display: grid; gap: 8px; }
.auto-notice { border: 1px solid; border-radius: 4px; padding: 8px 12px; font-size: 13px; }
.auto-notice--danger { border-color: rgba(255, 93, 93, .4); background: rgba(255, 93, 93, .07); color: #ff9b9b; }
.auto-notice--success { border-color: rgba(39, 197, 138, .35); background: rgba(39, 197, 138, .07); color: #78dbb8; }

.auto-tabs [role="tab"] { min-height: 40px; font-size: 14px; }
.tab-panel { display: grid; gap: 12px; }

.conclusion {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(260px, 340px);
  gap: 16px 24px;
  padding: 20px;
  background: linear-gradient(110deg, rgba(213, 180, 106, .07), transparent 45%), var(--surface);
}
.conclusion-main h2 { margin: 0; font-size: 20px; font-weight: 500; }
.conclusion-headline { margin: 8px 0 0; color: var(--gold-2); font-size: 16px; line-height: 1.6; }
.conclusion-facts { display: grid; gap: 8px; margin: 16px 0 0; padding: 0; list-style: none; }
.conclusion-facts li { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 8px; color: var(--muted); font-size: 13px; }
.conclusion-facts strong { color: var(--text); font-size: 16px; font-weight: 500; font-variant-numeric: tabular-nums; }
.conclusion-facts i { color: var(--muted-2); font-style: normal; }
.conclusion-facts b { border: 1px solid var(--line); border-radius: 999px; padding: 2px 8px; color: var(--text); font-size: 12px; font-weight: 400; }
.conclusion-facts b.change-added { border-color: rgba(242, 84, 184, .35); color: #ef9dce; }
.conclusion-side { display: grid; align-content: start; gap: 12px; }
.since-card { display: grid; gap: 4px; border: 1px solid var(--line); border-radius: 4px; padding: 12px 16px; background: var(--surface-2); }
.since-card span { color: var(--muted); font-size: 12px; }
.since-card strong { font-size: 24px; font-weight: 500; font-variant-numeric: tabular-nums; }
.since-card small { color: var(--muted); font-size: 12px; line-height: 1.5; }
.since-card em { font-style: normal; font-variant-numeric: tabular-nums; }
.track-line { margin: 0; color: var(--muted); font-size: 13px; line-height: 1.6; }
.explanation { grid-column: 1 / -1; border-top: 1px solid var(--line); padding-top: 12px; }
.explanation-body { margin-top: 8px; color: #d4cfc2; font-size: 13px; line-height: 1.7; overflow-wrap: anywhere; }

.status-panel { display: grid; gap: 8px; padding: 20px; border-left: 2px solid var(--warning); }
.status-panel.status-failed { border-left-color: var(--danger); }
.status-panel.status-running,
.status-panel.status-empty { border-left-color: var(--gold-dim); }
.status-panel h2 { margin: 0; font-size: 20px; font-weight: 500; }
.status-panel p { margin: 0; color: var(--muted); font-size: 14px; line-height: 1.6; }
.status-panel ul { margin: 0; padding-left: 20px; color: #e6c36f; font-size: 13px; line-height: 1.7; }
.status-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 4px; }
.sync-panel { display: grid; gap: 8px; padding: 16px 20px; border-left: 2px solid var(--warning); }
.sync-panel.sync-never,
.sync-panel.sync-running { border-left-color: var(--gold-dim); }
.sync-panel header { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: 4px 12px; }
.sync-panel header strong { font-size: 14px; font-weight: 500; }
.sync-panel header span { color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; }
.sync-panel ul { margin: 0; padding-left: 20px; color: #e6c36f; font-size: 13px; line-height: 1.7; }
.sync-panel > p { margin: 0; color: var(--muted); font-size: 13px; line-height: 1.6; }
.sync-line { margin: 0; color: var(--muted); font-size: 12px; }
.tech-detail summary { color: var(--muted); font-size: 12px; cursor: pointer; }
.tech-detail p { margin: 8px 0 0; color: var(--muted-2); font-size: 12px; line-height: 1.6; overflow-wrap: anywhere; }

.ui-panel-head p { line-height: 1.5; }
.ui-panel-head h3 { margin: 0; font-size: 16px; font-weight: 500; }
.ui-panel-head { padding: 12px 16px; }

.pick-list { margin: 0; padding: 0; list-style: none; }
.pick-list li + li { border-top: 1px solid var(--line); }
.pick-row {
  display: grid;
  grid-template-columns: 32px minmax(150px, 1.1fr) minmax(180px, 1.4fr) minmax(220px, 1.3fr) 84px 100px;
  align-items: center;
  gap: 16px;
  width: 100%;
  min-height: 64px;
  border: 0;
  border-radius: 0;
  padding: 8px 16px;
  background: transparent;
  color: var(--text);
  text-align: left;
  cursor: pointer;
}
.pick-row:hover { background: rgba(213, 180, 106, .04); }
.pick-rank { color: var(--gold); font-size: 16px; font-weight: 500; font-variant-numeric: tabular-nums; text-align: center; }
.pick-identity { display: grid; gap: 2px; min-width: 0; }
.pick-identity strong { overflow: hidden; font-size: 14px; font-weight: 500; text-overflow: ellipsis; white-space: nowrap; }
.pick-identity small { overflow: hidden; color: var(--muted); font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.pick-tags { display: flex; gap: 4px; }
.pick-tags em { border: 1px solid; border-radius: 999px; padding: 0 8px; font-size: 12px; font-style: normal; }
.tag-new { border-color: rgba(242, 84, 184, .35); color: #ef9dce; }
.tag-risk { border-color: rgba(213, 166, 66, .4); color: #e6c36f; }
.pick-reason { color: #d8d2c2; font-size: 13px; line-height: 1.5; }
.pick-dims { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 4px 12px; }
.dim-bar { display: grid; grid-template-columns: 40px minmax(0, 1fr) 24px; align-items: center; gap: 4px; font-size: 12px; }
.dim-bar b { color: var(--muted); font-weight: 400; }
.dim-bar i { height: 6px; overflow: hidden; border-radius: 999px; background: var(--surface-3); }
.dim-bar s,
.reason-list s,
.weight-list s { display: block; height: 100%; border-radius: 999px; background: var(--gold-dim); text-decoration: none; }
.dim-bar em { color: var(--muted); font-style: normal; font-variant-numeric: tabular-nums; text-align: right; }
.score-strong s { background: var(--gold); }
.score-strong em { color: var(--gold-2); }
.score-weak s { background: var(--muted-2); }
.pick-perf { display: grid; justify-items: end; gap: 2px; }
.pick-perf small { color: var(--muted); font-size: 12px; }
.pick-perf strong { font-size: 14px; font-weight: 500; font-variant-numeric: tabular-nums; }
.pick-spark { width: 100px; height: 28px; }
.pick-spark polyline { fill: none; stroke: var(--muted); stroke-width: 1.5; vector-effect: non-scaling-stroke; }
.pick-spark polyline.pnl-up { stroke: var(--up); }
.pick-spark polyline.pnl-down { stroke: var(--down); }
.removed-line { margin: 0; border-top: 1px solid var(--line); padding: 12px 16px; color: var(--muted); font-size: 12px; }

.horizon-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); }
.horizon-grid article { display: grid; align-content: start; gap: 8px; padding: 16px; }
.horizon-grid article + article { border-left: 1px solid var(--line); }
.horizon-grid header { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.horizon-grid header strong { font-size: 14px; font-weight: 500; }
.horizon-grid dl,
.summary-card dl,
.fact-grid { display: grid; gap: 8px; margin: 0; }
.horizon-grid dl div,
.summary-card dl div { display: flex; justify-content: space-between; gap: 8px; }
.horizon-grid dt,
.summary-card dt,
.fact-grid dt { color: var(--muted); font-size: 12px; }
.horizon-grid dd,
.summary-card dd,
.fact-grid dd { margin: 0; font-size: 13px; font-variant-numeric: tabular-nums; }
.horizon-grid small,
.horizon-grid p { margin: 0; color: var(--muted); font-size: 12px; }

.summary-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
.summary-card { display: grid; align-content: start; gap: 8px; padding: 16px; }
.summary-card header { display: grid; gap: 4px; }
.summary-card h3 { margin: 0; font-size: 16px; font-weight: 500; }
.summary-card header span,
.summary-card small,
.summary-empty { margin: 0; color: var(--muted); font-size: 12px; }
.summary-rate { color: var(--gold-2); font-size: 32px; font-weight: 500; font-variant-numeric: tabular-nums; }
.chart-panel { overflow: hidden; }
.track-chart { display: block; width: 100%; height: 160px; padding: 8px 16px 16px; }
.track-chart rect { fill: var(--muted-2); }
.track-chart rect.pnl-up { fill: var(--up); }
.track-chart rect.pnl-down { fill: var(--down); }
.zero-line { stroke: var(--line-strong); stroke-width: 1; vector-effect: non-scaling-stroke; }
.track-table-wrap { max-height: 520px; }
.track-table th.num,
.track-table td.num { text-align: right; }
.notes { margin: 0; padding-left: 20px; color: var(--muted); font-size: 12px; line-height: 1.7; }

.funnel { display: grid; gap: 12px; }
.funnel-step { display: grid; gap: 4px; }
.funnel-bar {
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 180px;
  border-radius: 4px;
  padding: 8px 12px;
  background: linear-gradient(90deg, rgba(213, 180, 106, .22), rgba(213, 180, 106, .08));
}
.funnel-bar strong { font-size: 20px; font-weight: 500; font-variant-numeric: tabular-nums; }
.funnel-bar span { color: var(--muted); font-size: 13px; }
.funnel-step p { margin: 0; color: var(--muted); font-size: 12px; }
.exclusion-list h4 { margin: 0 0 8px; color: var(--muted); font-size: 13px; font-weight: 500; }
.exclusion-list ul { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 4px 24px; margin: 0; padding: 0; list-style: none; }
.exclusion-list li { display: flex; justify-content: space-between; gap: 8px; border-bottom: 1px solid var(--line); padding: 4px 0; font-size: 13px; }
.exclusion-list b { font-weight: 500; font-variant-numeric: tabular-nums; }
.method-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.rule-list { display: grid; gap: 8px; margin: 0; padding-left: 32px; color: #d8d2c2; font-size: 14px; line-height: 1.6; }
.weight-list { display: grid; gap: 12px; margin: 0; padding: 0; list-style: none; }
.weight-list li { display: grid; gap: 4px; }
.weight-list span { display: flex; justify-content: space-between; font-size: 14px; }
.weight-list b { font-weight: 500; }
.weight-list em { color: var(--gold-2); font-style: normal; font-variant-numeric: tabular-nums; }
.weight-list i { height: 6px; overflow: hidden; border-radius: 999px; background: var(--surface-3); }
.weight-list small { color: var(--muted); font-size: 12px; }
.method-note,
.method-limit { margin: 12px 0 0; color: var(--muted); font-size: 12px; line-height: 1.6; }
.method-limit { margin: 0; }

.drawer-backdrop { position: fixed; z-index: var(--z-dialog); inset: 0; display: flex; justify-content: flex-end; background: rgba(0, 0, 0, .72); }
.pick-drawer {
  display: grid;
  align-content: start;
  gap: 0;
  width: min(480px, 100vw);
  height: 100%;
  overflow-y: auto;
  border-left: 1px solid var(--line-strong);
  outline: 0;
  background: var(--surface);
}
.drawer-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; border-bottom: 1px solid var(--line); padding: 16px 20px; }
.drawer-head h2 { margin: 0; font-size: 20px; font-weight: 500; }
.drawer-head h2 small { color: var(--muted); font-size: 13px; font-weight: 400; font-variant-numeric: tabular-nums; }
.drawer-head p { margin: 4px 0 0; color: var(--muted); font-size: 12px; line-height: 1.5; }
.drawer-close { min-width: 44px; min-height: 44px; border: 0; background: transparent; color: var(--muted); font-size: 24px; cursor: pointer; }
.drawer-block { display: grid; gap: 12px; border-bottom: 1px solid var(--line); padding: 16px 20px; }
.drawer-block h3 { margin: 0; font-size: 14px; font-weight: 500; }
.reason-list { display: grid; gap: 12px; margin: 0; padding: 0; list-style: none; }
.reason-list li { display: grid; gap: 4px; }
.reason-head { display: flex; justify-content: space-between; gap: 8px; font-size: 13px; }
.reason-head b { font-weight: 500; }
.reason-head span { color: var(--muted); font-variant-numeric: tabular-nums; }
.reason-list i { height: 6px; overflow: hidden; border-radius: 999px; background: var(--surface-3); }
.reason-list p { margin: 0; color: #d8d2c2; font-size: 13px; line-height: 1.6; }
.risk-list { display: grid; gap: 8px; margin: 0; padding: 0; list-style: none; }
.risk-list li { border-left: 2px solid var(--line-strong); padding: 4px 8px; color: #d8d2c2; font-size: 13px; line-height: 1.5; }
.risk-list li.risk-warning { border-left-color: var(--warning); background: rgba(213, 166, 66, .06); color: #e6c36f; }
.fact-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.fact-grid div { display: grid; gap: 2px; }
.drawer-actions { display: flex; flex-wrap: wrap; gap: 8px; padding: 16px 20px 8px; }
.drawer-foot { margin: 0; padding: 0 20px 20px; color: var(--muted-2); font-size: 12px; }

.muted-value { color: var(--muted) !important; font-size: 13px; }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.pnl-up { color: var(--up) !important; }
.pnl-down { color: var(--down) !important; }
button:focus-visible,
select:focus-visible,
[tabindex]:focus-visible { outline: 2px solid var(--gold); outline-offset: 2px; }

@media (max-width: 1380px) {
  .pick-row { grid-template-columns: 32px minmax(140px, 1fr) minmax(160px, 1.2fr) minmax(200px, 1.2fr) 84px; }
  .pick-row .pick-spark { display: none; }
}

@media (max-width: 980px) {
  .conclusion { grid-template-columns: minmax(0, 1fr); }
  .pick-row { grid-template-columns: 32px minmax(0, 1fr) 84px; grid-template-areas: "rank who perf" ". reason reason" ". dims dims"; gap: 8px 12px; }
  .pick-rank { grid-area: rank; }
  .pick-identity { grid-area: who; }
  .pick-reason { grid-area: reason; }
  .pick-dims { grid-area: dims; }
  .pick-perf { grid-area: perf; }
  .summary-grid,
  .horizon-grid,
  .method-grid { grid-template-columns: minmax(0, 1fr); }
  .horizon-grid article + article { border-top: 1px solid var(--line); border-left: 0; }
}

@media (max-width: 680px) {
  .auto-command { align-items: stretch; flex-direction: column; }
  .auto-actions .ui-btn { flex: 1; min-height: 44px; }
  .date-picker select { min-height: 44px; flex: 1; }
  .date-picker { width: 100%; }
  .auto-tabs { overflow-x: auto; }
  .auto-tabs [role="tab"] { min-height: 44px; flex: 0 0 auto; }
  .conclusion,
  .status-panel { padding: 16px; }
  .pick-row { padding: 12px; }
  .pick-dims { grid-template-columns: minmax(0, 1fr); }
  .pick-drawer { width: 100vw; }
  .fact-grid { grid-template-columns: minmax(0, 1fr); }
  .status-actions .ui-btn,
  .drawer-actions .ui-btn { flex: 1; min-height: 44px; }
}
</style>

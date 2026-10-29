<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api } from '../api'
import AgentApprovalCard from '../components/AgentApprovalCard.vue'
import type {
  DashboardApproval,
  DashboardPayload,
  DashboardPosition,
  DashboardResearch,
  DashboardRun
} from '../types'
import { beijingFromDb } from '../utils/datetime'

type WorkbenchPage = 'askStock' | 'positions' | 'watchlist' | 'digitalHumans' | 'rhythm'

const props = defineProps<{
  dashboard: DashboardPayload
}>()

const emit = defineEmits<{
  navigate: [page: WorkbenchPage]
  openPositionAgent: []
  openChart: [position: DashboardPosition]
  askStock: [symbol: string]
  refresh: []
}>()

const LAYOUT_KEY = 'hermes_workbench_layout_v3'
const nowMs = ref(Date.now())
const layoutOpen = ref(false)
const approvalOpen = ref(false)
const riskOpen = ref(false)
const selectedReport = ref<DashboardResearch | null>(null)
const selectedRun = ref<DashboardRun | null>(null)
const runDetailLoading = ref(false)
const runDetailError = ref('')
const busyApprovalId = ref('')
const approvalMessage = ref('')
const resolvedApprovalIds = ref<Set<string>>(new Set())
const layout = ref({
  compact: false,
  showMarket: true,
  showReports: true
})

let clockTimer = 0
let runDetailRequest = 0

const pendingApprovals = computed(() =>
  props.dashboard.approvals.filter((item) => !resolvedApprovalIds.value.has(item.approval_id))
)
const latestApproval = computed(() => pendingApprovals.value[0] || null)
const latestRiskRows = computed(() => props.dashboard.risk_events.filter((item) => item.kind === 'risk').slice(0, 2))
const hasModal = computed(
  () => approvalOpen.value || riskOpen.value || Boolean(selectedReport.value) || Boolean(selectedRun.value)
)

watch(
  layout,
  (value) => localStorage.setItem(LAYOUT_KEY, JSON.stringify(value)),
  { deep: true }
)

watch(hasModal, (open) => {
  document.body.classList.toggle('workbench-modal-open', open)
})

onMounted(() => {
  try {
    const stored = JSON.parse(localStorage.getItem(LAYOUT_KEY) || '{}')
    layout.value = { ...layout.value, ...stored }
  } catch {
    localStorage.removeItem(LAYOUT_KEY)
  }
  clockTimer = window.setInterval(() => {
    nowMs.value = Date.now()
  }, 1000)
})

onBeforeUnmount(() => {
  window.clearInterval(clockTimer)
  document.body.classList.remove('workbench-modal-open')
})

function formatNumber(value: number | null | undefined, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—'
  return Number(value).toLocaleString('zh-CN', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits
  })
}

function formatCurrency(value: number | null | undefined) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—'
  return `¥${Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

function formatSignedCurrency(value: number | null | undefined) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—'
  const number = Number(value)
  const sign = number > 0 ? '+' : number < 0 ? '-' : ''
  return `${sign}¥${Math.abs(number).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

function formatPercent(value: number | null | undefined, signed = true) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—'
  const number = Number(value)
  const sign = signed && number > 0 ? '+' : ''
  return `${sign}${number.toFixed(2)}%`
}

function valueTone(value: number | null | undefined) {
  if (value === null || value === undefined || Number(value) === 0) return 'neutral'
  return Number(value) > 0 ? 'positive' : 'negative'
}

function shortTime(value: string | null | undefined) {
  const formatted = beijingFromDb(value)
  return formatted === '-' ? '—' : formatted.slice(-5)
}

function localDateTime(value: string | null | undefined) {
  if (!value) return '—'
  if (/[zZ]$|[+-]\d\d:?\d\d$/.test(value)) {
    return new Intl.DateTimeFormat('zh-CN', {
      timeZone: 'Asia/Shanghai',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false
    }).format(new Date(value))
  }
  return value.replace('T', ' ').slice(5, 16)
}

function localClock(value: string | null | undefined) {
  if (!value) return '—'
  return value.replace('T', ' ').slice(11, 19)
}

function parseLocalTimestamp(value: string | null | undefined) {
  if (!value) return NaN
  const normalized = /[zZ]$|[+-]\d\d:?\d\d$/.test(value) ? value : `${value.replace(' ', 'T')}+08:00`
  return new Date(normalized).getTime()
}

function formatElapsed(startedAt: string | null) {
  const started = parseLocalTimestamp(startedAt)
  if (!Number.isFinite(started)) return '—'
  return formatDuration(Math.max(0, nowMs.value - started))
}

function formatDuration(value: number | null | undefined) {
  if (value === null || value === undefined || value < 0) return '—'
  const seconds = Math.floor(value / 1000)
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  const rest = seconds % 60
  if (minutes < 60) return `${minutes}m ${String(rest).padStart(2, '0')}s`
  const hours = Math.floor(minutes / 60)
  return `${hours}h ${String(minutes % 60).padStart(2, '0')}m`
}

function countdown(target: string | null | undefined) {
  const timestamp = parseLocalTimestamp(target)
  if (!Number.isFinite(timestamp)) return '—'
  const seconds = Math.max(0, Math.floor((timestamp - nowMs.value) / 1000))
  const hours = Math.floor(seconds / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  const rest = seconds % 60
  return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(rest).padStart(2, '0')}`
}

function sparklinePoints(values: number[]) {
  if (values.length < 2) return ''
  const min = Math.min(...values)
  const max = Math.max(...values)
  const range = max - min || 1
  return values
    .map((value, index) => {
      const x = (index / (values.length - 1)) * 120
      const y = 28 - ((value - min) / range) * 24
      return `${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(' ')
}

function actionLabel(value: string | null | undefined) {
  const labels: Record<string, string> = {
    observe: '观察',
    hold: '持有',
    add: '增持',
    reduce: '减仓',
    avoid: '回避',
    review: '复核',
    buy: '关注买点',
    sell: '卖出'
  }
  return labels[String(value || '').toLowerCase()] || value || '查看'
}

function triggerLabel(value: string) {
  return value === 'schedule' ? '定时任务' : value === 'manual' ? '手动执行' : value
}

function runStatusLabel(value: string) {
  const labels: Record<string, string> = {
    running: '执行中',
    success: '成功',
    failed: '失败',
    skipped: '跳过'
  }
  return labels[value] || value
}

async function decideApproval(approval: DashboardApproval, decision: 'approved' | 'denied') {
  if (busyApprovalId.value) return
  busyApprovalId.value = approval.approval_id
  approvalMessage.value = ''
  try {
    const result = await api.resolveAgentApproval(approval.approval_id, decision)
    if (result.status === decision || result.status !== 'pending') {
      resolvedApprovalIds.value = new Set([...resolvedApprovalIds.value, approval.approval_id])
      approvalMessage.value = decision === 'approved' ? '已批准，Agent 将继续执行。' : '已拒绝本次工具调用。'
      emit('refresh')
    }
  } catch (error) {
    approvalMessage.value = error instanceof Error ? error.message : '审批处理失败'
  } finally {
    busyApprovalId.value = ''
  }
}

async function openRunDetail(run: DashboardRun) {
  const requestId = ++runDetailRequest
  selectedRun.value = run
  runDetailLoading.value = true
  runDetailError.value = ''
  try {
    const detail = await api.scheduleRun(run.run_id)
    if (requestId === runDetailRequest) selectedRun.value = { ...run, ...detail }
  } catch (error) {
    if (requestId === runDetailRequest) {
      runDetailError.value = error instanceof Error ? error.message : '完整结果加载失败'
    }
  } finally {
    if (requestId === runDetailRequest) runDetailLoading.value = false
  }
}

function closeRunDetail() {
  runDetailRequest += 1
  selectedRun.value = null
  runDetailLoading.value = false
  runDetailError.value = ''
}
</script>

<template>
  <div class="workbench-v3" :class="{ compact: layout.compact }">
    <section class="wb-command-bar" aria-label="工作台快捷操作">
      <div class="wb-command-list">
        <button type="button" @click="emit('navigate', 'askStock')">
          <img src="/icons/askStock.png" alt="" />
          <span>问一只股票</span>
        </button>
        <button type="button" @click="emit('openPositionAgent')">
          <img src="/icons/positions.png" alt="" />
          <span>录入持仓</span>
        </button>
        <button type="button" @click="emit('navigate', 'watchlist')">
          <img src="/icons/watchlist.png" alt="" />
          <span>加入自选</span>
        </button>
        <button type="button" @click="emit('navigate', 'digitalHumans')">
          <img src="/icons/digitalHumans.png" alt="" />
          <span>生成研究报告</span>
        </button>
      </div>
      <div class="wb-layout-control">
        <button class="wb-layout-trigger" type="button" :aria-expanded="layoutOpen" @click="layoutOpen = !layoutOpen">
          <img src="/icons/settings.png" alt="" />
          <span>布局设置</span>
        </button>
        <div v-if="layoutOpen" class="wb-layout-popover">
          <header>
            <strong>工作台布局</strong>
            <button type="button" title="关闭布局设置" @click="layoutOpen = false">×</button>
          </header>
          <label><input v-model="layout.compact" type="checkbox" /> 紧凑显示</label>
          <label><input v-model="layout.showMarket" type="checkbox" /> 市场与风险</label>
          <label><input v-model="layout.showReports" type="checkbox" /> 最新研究报告</label>
        </div>
      </div>
    </section>

    <section class="wb-metric-grid" aria-label="账户概览">
      <article>
        <span>总资产</span>
        <strong>{{ formatCurrency(dashboard.portfolio.total_asset) }}</strong>
        <small>{{ dashboard.portfolio.account_count }} 个账户</small>
      </article>
      <article>
        <span>持仓市值</span>
        <strong>{{ formatCurrency(dashboard.portfolio.holding_market_value) }}</strong>
        <small>行情快照汇总</small>
      </article>
      <article>
        <span>今日盈亏</span>
        <strong :class="valueTone(dashboard.portfolio.day_pnl)">{{ formatSignedCurrency(dashboard.portfolio.day_pnl) }}</strong>
        <small :class="valueTone(dashboard.portfolio.day_pnl_pct)">{{ formatPercent(dashboard.portfolio.day_pnl_pct) }}</small>
      </article>
      <article>
        <span>持仓</span>
        <strong>{{ dashboard.counts.open_positions }}</strong>
        <small>当前开放持仓</small>
      </article>
      <article>
        <span>自选</span>
        <strong>{{ dashboard.counts.watchlist }}</strong>
        <small>活跃观察标的</small>
      </article>
      <article class="risk-metric">
        <span>近24小时高风险</span>
        <strong>{{ dashboard.counts.high_risk_24h }}</strong>
        <small>{{ dashboard.risk_summary.medium }} 条中风险</small>
      </article>
    </section>

    <div class="wb-body-grid">
      <div class="wb-main-column">
        <div v-if="layout.showMarket" class="wb-upper-grid">
          <section class="wb-panel wb-market-panel">
            <header class="wb-panel-header">
              <h2>市场快照</h2>
              <span>{{ dashboard.market.source }} · {{ shortTime(dashboard.data_freshness.market) }}</span>
            </header>
            <div class="wb-market-list">
              <article v-for="item in dashboard.market.items" :key="item.symbol">
                <div>
                  <strong>{{ item.name }}</strong>
                  <small>{{ item.symbol }}</small>
                </div>
                <b>{{ formatNumber(item.price) }}</b>
                <svg v-if="item.sparkline.length > 1" viewBox="0 0 120 32" preserveAspectRatio="none" aria-hidden="true">
                  <polyline :class="valueTone(item.change_pct)" :points="sparklinePoints(item.sparkline)" />
                </svg>
                <span :class="valueTone(item.change_pct)">{{ formatPercent(item.change_pct) }}</span>
              </article>
            </div>
            <div v-if="dashboard.market.items.every((item) => item.price === null)" class="wb-inline-empty">
              指数数据暂不可用，请检查 TuShare Token 配置。
            </div>
          </section>

          <section class="wb-panel wb-event-panel">
            <header class="wb-panel-header">
              <h2>风险与事件</h2>
              <button type="button" @click="riskOpen = true">查看全部</button>
            </header>
            <div v-if="dashboard.risk_events.length" class="wb-event-list">
              <button v-for="item in dashboard.risk_events.slice(0, 3)" :key="`${item.kind}-${item.id}`" type="button" @click="riskOpen = true">
                <i :class="item.level">{{ item.level === 'high' ? '高' : item.level === 'medium' ? '中' : '低' }}</i>
                <span>
                  <strong>{{ item.symbol_name || item.symbol || '全市场' }}</strong>
                  <em>{{ item.title }}</em>
                </span>
                <small>{{ shortTime(item.created_at) }}</small>
              </button>
            </div>
            <div v-else class="wb-panel-empty">暂无风险或事件记录</div>
          </section>
        </div>

        <section class="wb-panel wb-position-panel">
          <header class="wb-panel-header">
            <h2>持仓与风险</h2>
            <span>持仓行情 {{ shortTime(dashboard.data_freshness.positions) }} · 风险检查 {{ shortTime(dashboard.data_freshness.risk) }}</span>
          </header>
          <div class="wb-table-wrap">
            <table class="wb-position-table">
              <thead>
                <tr>
                  <th>标的</th>
                  <th>最新价</th>
                  <th>今日盈亏</th>
                  <th>仓位</th>
                  <th>止损距离</th>
                  <th>最新研究</th>
                  <th>风险</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="position in dashboard.positions" :key="`${position.account_id || 0}-${position.symbol}`">
                  <td>
                    <strong>{{ position.name || position.symbol }}</strong>
                    <small>{{ position.symbol }}</small>
                  </td>
                  <td>{{ formatNumber(position.latest_price) }}</td>
                  <td>
                    <strong :class="valueTone(position.day_pnl)">{{ formatSignedCurrency(position.day_pnl) }}</strong>
                    <small :class="valueTone(position.day_pnl_pct)">{{ formatPercent(position.day_pnl_pct) }}</small>
                  </td>
                  <td>{{ formatPercent(position.position_pct, false) }}</td>
                  <td :class="position.stop_distance_pct !== null && position.stop_distance_pct <= 2 ? 'negative' : ''">
                    {{ formatPercent(position.stop_distance_pct, false) }}
                  </td>
                  <td>
                    <button
                      v-if="position.latest_research"
                      class="wb-research-link"
                      type="button"
                      @click="selectedReport = position.latest_research"
                    >
                      {{ actionLabel(position.latest_research.action_suggestion) }} · {{ shortTime(position.latest_research.created_at) }}
                    </button>
                    <span v-else class="muted">暂无报告</span>
                  </td>
                  <td><span class="wb-risk-badge" :class="position.risk_state.level">{{ position.risk_state.label }}</span></td>
                  <td>
                    <div class="wb-row-actions">
                      <button type="button" title="查看持仓图表" @click="emit('openChart', position)">详情</button>
                      <button type="button" title="带入该股票进入问股" @click="emit('askStock', position.symbol)">问Agent</button>
                    </div>
                  </td>
                </tr>
                <tr v-if="!dashboard.positions.length">
                  <td colspan="8" class="wb-table-empty">暂无持仓，使用上方“录入持仓”开始建立台账。</td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>

        <section v-if="layout.showReports" class="wb-panel wb-report-panel">
          <header class="wb-panel-header">
            <h2>最新研究报告</h2>
            <span>今日新增 {{ dashboard.today.reports }} 份</span>
          </header>
          <div v-if="dashboard.reports.length" class="wb-report-list">
            <button v-for="report in dashboard.reports.slice(0, 3)" :key="report.report_id" type="button" @click="selectedReport = report">
              <span class="wb-agent-mark">{{ (report.display_name || report.agent_name).slice(0, 1) }}</span>
              <span class="wb-report-agent">
                <strong>{{ report.display_name || report.agent_name }}</strong>
                <small>{{ report.symbol_name || report.symbol || '市场综述' }}</small>
              </span>
              <em>{{ report.conclusion || report.title }}</em>
              <time>{{ shortTime(report.created_at) }}</time>
              <b>查看报告</b>
            </button>
          </div>
          <div v-else class="wb-panel-empty">暂无 Agent 研究报告</div>
          <button class="wb-panel-footer" type="button" @click="emit('navigate', 'digitalHumans')">进入数字人中心</button>
        </section>
      </div>

      <aside class="wb-side-column">
        <section class="wb-panel wb-runtime-panel">
          <header class="wb-panel-header">
            <h2>运行动态</h2>
            <span>来自调度运行记录</span>
          </header>
          <div class="wb-runtime-group">
            <h3>正在执行 {{ dashboard.runtime.running.length }}</h3>
            <button
              v-for="run in dashboard.runtime.running"
              :key="run.run_id"
              class="wb-running-row"
              type="button"
              @click="openRunDetail(run)"
            >
              <span>
                <strong>{{ run.name }}</strong>
                <small>{{ triggerLabel(run.trigger_type) }}</small>
              </span>
              <b>执行中</b>
              <em>已运行 {{ formatElapsed(run.started_at) }}</em>
              <time>{{ localClock(run.started_at) }}</time>
            </button>
            <div v-if="!dashboard.runtime.running.length" class="wb-runtime-empty">当前没有调度任务运行</div>
          </div>
          <div class="wb-runtime-group recent">
            <h3>最近完成</h3>
            <button v-for="run in dashboard.runtime.recent.slice(0, 3)" :key="run.run_id" type="button" @click="openRunDetail(run)">
              <span>{{ run.name }}</span>
              <time>{{ localDateTime(run.started_at) }}</time>
              <b :class="run.status">{{ runStatusLabel(run.status) }} · {{ formatDuration(run.duration_ms) }}</b>
            </button>
            <div v-if="!dashboard.runtime.recent.length" class="wb-runtime-empty">暂无运行记录</div>
          </div>
          <button class="wb-panel-footer" type="button" @click="emit('navigate', 'rhythm')">查看运行记录</button>
        </section>

        <section class="wb-panel wb-approval-panel">
          <header class="wb-panel-header">
            <h2>工具审批</h2>
            <span v-if="pendingApprovals.length" class="wb-count-badge danger">待审批 {{ pendingApprovals.length }}</span>
            <span v-else class="wb-count-badge">无待审批</span>
          </header>
          <template v-if="latestApproval">
            <div class="wb-approval-summary">
              <strong>{{ latestApproval.display_name }}</strong>
              <span>{{ latestApproval.session_title || 'Agent 会话' }} · {{ latestApproval.risk_level === 'high' ? '高风险' : '工作区操作' }}</span>
              <small>剩余 {{ countdown(latestApproval.expires_at) }}</small>
            </div>
            <button class="wb-primary-action" type="button" @click="approvalOpen = true">查看并处理</button>
          </template>
          <div v-else class="wb-panel-empty">暂无待审批工具请求</div>
        </section>

        <section class="wb-panel wb-risk-summary-panel">
          <header class="wb-panel-header">
            <h2>风险检查</h2>
            <span>近24小时 <b class="negative">高风险 {{ dashboard.risk_summary.high }}</b> · 中风险 {{ dashboard.risk_summary.medium }}</span>
          </header>
          <div v-if="latestRiskRows.length" class="wb-side-risk-list">
            <button v-for="item in latestRiskRows" :key="item.id" type="button" @click="riskOpen = true">
              <i :class="item.level">{{ item.level === 'high' ? '高' : '中' }}</i>
              <span>{{ item.symbol_name || item.symbol || '全市场' }}：{{ item.title }}</span>
              <time>{{ shortTime(item.created_at) }}</time>
            </button>
          </div>
          <div v-else class="wb-panel-empty">近24小时暂无风险检查记录</div>
          <button class="wb-panel-footer" type="button" @click="riskOpen = true">查看风险记录</button>
        </section>

        <section class="wb-panel wb-next-job-panel">
          <header class="wb-panel-header"><h2>下一项定时任务</h2></header>
          <div v-if="dashboard.next_job" class="wb-next-job">
            <span class="wb-clock-mark" aria-hidden="true"></span>
            <strong>{{ localClock(dashboard.next_job.run_at).slice(0, 5) }}</strong>
            <b>{{ dashboard.next_job.name }}</b>
            <small v-if="dashboard.next_job.trading_day_only">交易日</small>
            <time>{{ countdown(dashboard.next_job.run_at) }}</time>
          </div>
          <div v-else class="wb-panel-empty">暂无已启用的后续任务</div>
        </section>
      </aside>
    </div>

    <div v-if="approvalOpen" class="wb-modal-backdrop" @click.self="approvalOpen = false">
      <section class="wb-modal wb-approval-modal" role="dialog" aria-modal="true" aria-label="工具审批">
        <header>
          <div><span>APPROVAL CENTER</span><h2>工具审批</h2><p>仅处理当前账号发起的待审批工具调用。</p></div>
          <button type="button" title="关闭" @click="approvalOpen = false">×</button>
        </header>
        <div class="wb-modal-body">
          <div v-if="approvalMessage" class="wb-modal-message">{{ approvalMessage }}</div>
          <div v-if="pendingApprovals.length" class="wb-approval-list">
            <div v-for="approval in pendingApprovals" :key="approval.approval_id">
              <small>{{ approval.session_title || 'Agent 会话' }} · 剩余 {{ countdown(approval.expires_at) }}</small>
              <AgentApprovalCard
                :approval="approval"
                :busy="busyApprovalId === approval.approval_id"
                prominent
                @decide="(decision) => decideApproval(approval, decision)"
              />
            </div>
          </div>
          <div v-else class="wb-panel-empty">待审批请求已处理完毕</div>
        </div>
      </section>
    </div>

    <div v-if="riskOpen" class="wb-modal-backdrop" @click.self="riskOpen = false">
      <section class="wb-modal" role="dialog" aria-modal="true" aria-label="风险与事件">
        <header>
          <div><span>RISK & EVENTS</span><h2>风险与事件</h2><p>风险检查与事件记录只读展示，不混入审批流程。</p></div>
          <button type="button" title="关闭" @click="riskOpen = false">×</button>
        </header>
        <div class="wb-modal-body wb-risk-modal-list">
          <article v-for="item in dashboard.risk_events" :key="`${item.kind}-${item.id}`">
            <i :class="item.level">{{ item.level === 'high' ? '高' : item.level === 'medium' ? '中' : '低' }}</i>
            <div>
              <strong>{{ item.symbol_name || item.symbol || '全市场' }}</strong>
              <p>{{ item.title }}</p>
              <small v-if="item.summary">{{ item.summary }}</small>
            </div>
            <time>{{ beijingFromDb(item.created_at) }}</time>
          </article>
          <div v-if="!dashboard.risk_events.length" class="wb-panel-empty">暂无风险或事件记录</div>
        </div>
      </section>
    </div>

    <div v-if="selectedReport" class="wb-modal-backdrop" @click.self="selectedReport = null">
      <section class="wb-modal" role="dialog" aria-modal="true" aria-label="研究报告">
        <header>
          <div><span>AGENT REPORT</span><h2>{{ selectedReport.title }}</h2><p>{{ selectedReport.display_name || selectedReport.agent_name }} · {{ beijingFromDb(selectedReport.created_at) }}</p></div>
          <button type="button" title="关闭" @click="selectedReport = null">×</button>
        </header>
        <div class="wb-modal-body wb-report-detail">
          <div><span>标的</span><strong>{{ selectedReport.symbol_name || selectedReport.symbol || '全市场' }}</strong></div>
          <div><span>操作建议</span><strong>{{ actionLabel(selectedReport.action_suggestion) }}</strong></div>
          <section><h3>结论</h3><p>{{ selectedReport.conclusion || '暂无结构化结论' }}</p></section>
          <section><h3>风险点</h3><p>{{ selectedReport.risk_points || '暂无结构化风险点' }}</p></section>
        </div>
      </section>
    </div>

    <div v-if="selectedRun" class="wb-modal-backdrop" @click.self="closeRunDetail">
      <section class="wb-modal" role="dialog" aria-modal="true" aria-label="运行详情">
        <header>
          <div><span>JOB RUN</span><h2>{{ selectedRun.name }}</h2><p>{{ selectedRun.run_id }}</p></div>
          <button type="button" title="关闭" @click="closeRunDetail">×</button>
        </header>
        <div class="wb-modal-body wb-run-detail">
          <div><span>状态</span><strong :class="selectedRun.status">{{ runStatusLabel(selectedRun.status) }}</strong></div>
          <div><span>触发方式</span><strong>{{ triggerLabel(selectedRun.trigger_type) }}</strong></div>
          <div><span>开始时间</span><strong>{{ localDateTime(selectedRun.started_at) }}</strong></div>
          <div><span>耗时</span><strong>{{ selectedRun.status === 'running' ? formatElapsed(selectedRun.started_at) : formatDuration(selectedRun.duration_ms) }}</strong></div>
          <section>
            <h3>{{ selectedRun.error ? '错误信息' : '结果输出' }}</h3>
            <pre v-if="runDetailLoading">正在读取完整结果…</pre>
            <pre v-else>{{ runDetailError ? `${runDetailError}\n\n当前显示摘要：\n${selectedRun.error || selectedRun.output_preview || '暂无运行结果摘要'}` : selectedRun.error || selectedRun.output_content || selectedRun.output_preview || '暂无运行结果' }}</pre>
          </section>
        </div>
      </section>
    </div>
  </div>
</template>

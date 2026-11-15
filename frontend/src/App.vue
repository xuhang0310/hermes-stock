<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api, ApiError, getAuthToken, setAuthToken } from './api'
import AppShell from './components/AppShell.vue'
import PositionsWorkbench from './components/PositionsWorkbench.vue'
import AskStockView from './views/AskStockView.vue'
import DigitalHumanCenter from './views/DigitalHumanCenter.vue'
import LoginView from './views/LoginView.vue'
import PositionChartView from './views/PositionChartView.vue'
import RhythmView from './views/RhythmView.vue'
import SettingsView from './views/SettingsView.vue'
import KnowledgeBaseView from './views/KnowledgeBaseView.vue'
import WorkspaceDocsView from './views/WorkspaceDocsView.vue'
import KnowledgeSearchView from './views/KnowledgeSearchView.vue'
import WorkbenchView from './views/WorkbenchView.vue'
import WatchlistView from './views/WatchlistView.vue'
import SkillSquareView from './views/SkillSquareView.vue'
import BacktestReportView from './views/BacktestReportView.vue'
import BacktestHistoryView from './views/BacktestHistoryView.vue'
import FactorAnalysisView from './views/FactorAnalysisView.vue'
import ScreenerView from './views/ScreenerView.vue'
import IndustryAnalysisView from './views/IndustryAnalysisView.vue'
import { industryQuestion } from './utils/industry'
import AutomaticSelectionView from './views/AutomaticSelectionView.vue'
import ScorecardView from './views/ScorecardView.vue'
import OpsConsoleView from './views/OpsConsoleView.vue'
import AgentTaskBoardView from './views/AgentTaskBoardView.vue'
import { renderMarkdown } from './utils/markdown'
import type {
  AgentItem,
  DashboardPayload,
  HealthPayload,
  ModelConfig,
  PositionAccount,
  PositionAgentPendingAction,
  PositionAgentStep,
  PositionAgentStreamEvent,
  PositionItem,
  PositionSymbolValidation,
  ScreenerItem,
  AutomaticSelectionPick,
  IndustryMember,
  UserPayload,
  WatchlistItem
} from './types'

type PageKey =
  | 'workbench'
  | 'askStock'
  | 'positions'
  | 'positionChart'
  | 'watchlist'
  | 'digitalHumans'
  | 'agentTaskBoard'
  | 'rhythm'
  | 'knowledge'
  | 'knowledgeSearch'
  | 'workspaceDocs'
  | 'skillSquare'
  | 'backtestHistory'
  | 'backtestReport'
  | 'factorAnalysis'
  | 'screener'
  | 'industryAnalysis'
  | 'automaticSelection'
  | 'scorecard'
  | 'opsConsole'
  | 'settings'

type PositionAgentChat = {
  id: number
  role: 'user' | 'assistant'
  content: string
  imageName?: string
  kind?: 'reply' | 'saved'
  steps?: PositionAgentStep[]
  status?: 'thinking' | 'done' | 'error'
  error?: string
}

type PositionAgentReviewRow = Record<string, unknown> & {
  symbol: string
  name: string
  validation: PositionSymbolValidation
  validating: boolean
}

const activePage = ref<PageKey>('workbench')
const screenerInitialIndustry = ref('')
const bootstrapping = ref(false)
const loading = ref(false)
const loginLoading = ref(false)
const loginError = ref('')
const error = ref('')
const currentUser = ref<UserPayload | null>(null)
const canViewSettings = computed(
  () => ['admin', 'super_admin'].includes(currentUser.value?.role || '')
    || !!currentUser.value?.permissions.some(
      permission => ['all', 'data:read', 'data:write', 'schedule:manage'].includes(permission)
    )
)
const canViewOps = computed(
  () =>
    !!currentUser.value &&
    (currentUser.value.permissions.includes('all') ||
      currentUser.value.permissions.includes('ops_console:view'))
)
watch(
  () => [currentUser.value, activePage.value] as const,
  () => {
    if (activePage.value === 'opsConsole' && currentUser.value && !canViewOps.value) {
      activePage.value = 'workbench'
    }
    if (activePage.value === 'settings' && currentUser.value && !canViewSettings.value) {
      activePage.value = 'workbench'
    }
  }
)

const health = ref<HealthPayload | null>(null)
const dashboard = ref<DashboardPayload | null>(null)
const positions = ref<PositionItem[]>([])
const watchlist = ref<WatchlistItem[]>([])
const watchlistLoading = ref(false)
const agents = ref<AgentItem[]>([])
let dashboardRefreshRequest = 0
let watchlistRefreshRequest = 0
let dashboardRefreshTimer: number | null = null
let dashboardRefreshInFlightCount = 0
const DASHBOARD_REFRESH_INTERVAL_MS = 30_000

const positionAgentInstruction = ref('')
const positionAgentImageBase64 = ref('')
const positionAgentMimeType = ref('image/png')
const positionAgentFileName = ref('')
const positionAgentBusy = ref(false)
const positionAgentMessage = ref('')
const positionAgentRows = ref<PositionItem[]>([])
const positionDrawerOpen = ref(false)
const positionAgentChats = ref<PositionAgentChat[]>([])
const positionChatScroll = ref<HTMLElement | null>(null)
const positionModels = ref<ModelConfig[]>([])
const selectedPositionModelId = ref<number | null>(null)
const selectedChartPosition = ref<PositionItem | null>(null)
const selectedChartInstrument = ref<{ symbol: string; name: string } | null>(null)
const chartReturnPage = ref<'positions' | 'watchlist'>('positions')
const digitalHumanInitialTab = ref<'chat' | 'feishuBots'>('chat')
const digitalHumanInitialSession = ref<number | null>(null)
const digitalHumanInitialQuestion = ref('')
const askStockInitialQuestion = ref('')
const backtestRunId = ref('')
type BacktestReturnPage = 'digitalHumans' | 'backtestHistory' | 'screener'
const backtestReturnPage = ref<BacktestReturnPage>('digitalHumans')
const positionAgentTargetAccount = ref<number | null>(null)
const positionAgentAccounts = ref<PositionAccount[]>([])
const positionAgentPendingAction = ref<PositionAgentPendingAction | null>(null)
const positionAgentReviewRows = ref<PositionAgentReviewRow[]>([])
const positionAgentConfirming = ref(false)
const positionAgentDragging = ref(false)
const positionAgentClientSessionId = ref('')
let positionAgentDragDepth = 0
let restoredPositionAgentUserId: number | null = null

function positionAgentStorageKey(userId: number) {
  return `sixrabbits:position-agent:v1:${userId}`
}

function persistPositionAgentSession() {
  const userId = currentUser.value?.id
  if (!userId) return
  const reviewRows = positionAgentReviewRows.value.map((row) => ({
    ...row,
    validating: false
  }))
  const session = {
    updatedAt: Date.now(),
    chats: positionAgentChats.value.slice(-40),
    pendingAction: positionAgentPendingAction.value,
    reviewRows,
    targetAccount: positionAgentTargetAccount.value
  }
  try {
    window.localStorage.setItem(positionAgentStorageKey(userId), JSON.stringify(session))
  } catch {
    const compactSession = {
      updatedAt: Date.now(),
      chats: positionAgentChats.value.slice(-12).map((item) => ({
        ...item,
        content: item.content.slice(-20000),
        steps: item.steps?.slice(-12)
      })),
      pendingAction: positionAgentPendingAction.value,
      reviewRows,
      targetAccount: positionAgentTargetAccount.value
    }
    try {
      window.localStorage.setItem(
        positionAgentStorageKey(userId),
        JSON.stringify(compactSession)
      )
    } catch {
      // Storage can be unavailable in private browsing; the active in-memory chat still works.
    }
  }
}

function restorePositionAgentSession() {
  const userId = currentUser.value?.id
  if (!userId || restoredPositionAgentUserId === userId) return
  restoredPositionAgentUserId = userId
  try {
    const raw = window.localStorage.getItem(positionAgentStorageKey(userId))
    if (!raw) return
    const stored = JSON.parse(raw) as {
      updatedAt?: number
      chats?: PositionAgentChat[]
      pendingAction?: PositionAgentPendingAction | null
      reviewRows?: PositionAgentReviewRow[]
      targetAccount?: number | null
    }
    if (stored.updatedAt && Date.now() - stored.updatedAt > 30 * 24 * 60 * 60 * 1000) {
      window.localStorage.removeItem(positionAgentStorageKey(userId))
      return
    }
    positionAgentChats.value = Array.isArray(stored.chats) ? stored.chats.slice(-40) : []
    positionAgentPendingAction.value = stored.pendingAction || null
    positionAgentReviewRows.value = Array.isArray(stored.reviewRows)
      ? stored.reviewRows.map((row) => ({ ...row, validating: false }))
      : []
    positionAgentTargetAccount.value = Number.isInteger(stored.targetAccount)
      ? Number(stored.targetAccount)
      : null
  } catch {
    window.localStorage.removeItem(positionAgentStorageKey(userId))
  }
}

function resetPositionAgentSessionMemory() {
  restoredPositionAgentUserId = null
  positionAgentInstruction.value = ''
  positionAgentImageBase64.value = ''
  positionAgentFileName.value = ''
  positionAgentMessage.value = ''
  positionAgentRows.value = []
  positionAgentChats.value = []
  positionAgentPendingAction.value = null
  positionAgentReviewRows.value = []
  positionAgentTargetAccount.value = null
  positionAgentClientSessionId.value = ''
  positionDrawerOpen.value = false
}

function createPositionAgentClientSessionId() {
  return globalThis.crypto?.randomUUID?.()
    || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
}

watch(
  [positionAgentChats, positionAgentPendingAction, positionAgentReviewRows, positionAgentTargetAccount],
  persistPositionAgentSession,
  { deep: true }
)

const pageTitle = computed(() => {
  if (activePage.value === 'positionChart') {
    return chartReturnPage.value === 'watchlist' ? '自选行情图表' : '持仓图表'
  }
  const labels: Record<PageKey, string> = {
    askStock: '问股',
    workbench: '今日工作台',
    digitalHumans: '数字人中心',
    agentTaskBoard: 'Agent 任务',
    positions: '持仓',
    positionChart: '行情图表',
    watchlist: '自选池',
    rhythm: '定时任务',
    knowledge: '知识库',
    knowledgeSearch: '检索agent',
    workspaceDocs: '生成文档',
    skillSquare: '技能广场',
    backtestHistory: '回测报告',
    backtestReport: '策略回测报告',
    factorAnalysis: '因子评估',
    screener: 'AI 选股',
    industryAnalysis: '行业分析',
    automaticSelection: '自动选股',
    scorecard: '建议成绩单',
    opsConsole: '运维控制台',
    settings: '设置'
  }
  return labels[activePage.value]
})

const pageSubtitle = computed(() => {
  if (activePage.value === 'industryAnalysis') return '行业强弱、市场广度与成分股研究'
  if (activePage.value === 'automaticSelection') {
    return '每个交易日收盘后按固定规则选出观察股，并持续跟踪之后的表现'
  }
  if (activePage.value === 'scorecard') return '计划里的每条建议，到期后和全市场平均比，算出对错'
  if (activePage.value !== 'workbench' || !dashboard.value) {
    return '问股、数字人、持仓、自选池和模型配置统一入口'
  }
  const source = dashboard.value.server_time || dashboard.value.generated_at
  const parsed = source ? new Date(source) : null
  if (!parsed || Number.isNaN(parsed.getTime())) {
    return `${dashboard.value.market_state.label} · 数据状态以各模块更新时间为准`
  }
  const dateText = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: 'long',
    day: 'numeric',
    weekday: 'short'
  }).format(parsed)
  const timeText = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false
  }).format(parsed)
  return `${dateText} · ${dashboard.value.market_state.label} · 数据更新 ${timeText}`
})

const activePositionModel = computed(
  () =>
    positionModels.value.find((item) => item.id === selectedPositionModelId.value) ||
    positionModels.value.find((item) => item.is_active) ||
    positionModels.value[0] ||
    null
)

const positionAgentComposerMessage = computed(() => {
  const text = positionAgentMessage.value.trim()
  if (!text) return ''
  const lastAssistant = [...positionAgentChats.value].reverse().find((item) => item.role === 'assistant')
  if (
    lastAssistant?.status === 'done'
    && (text.length > 240 || lastAssistant.content.includes(text))
  ) {
    return ''
  }
  return text
})

const positionModelGroups = computed(() => {
  const groups = new Map<string, ModelConfig[]>()
  for (const model of positionModels.value) {
    const platform = model.platform_name || model.provider_name || '默认平台'
    groups.set(platform, [...(groups.get(platform) || []), model])
  }
  return Array.from(groups, ([platform, rows]) => ({
    platform,
    rows: [...rows].sort((a, b) => Number(b.is_active) - Number(a.is_active) || a.id - b.id)
  }))
})

async function refreshAll() {
  if (!currentUser.value) return
  const dashboardRequestId = ++dashboardRefreshRequest
  const watchlistRequestId = ++watchlistRefreshRequest
  dashboardRefreshInFlightCount += 1
  loading.value = true
  error.value = ''
  try {
    const [
      healthPayload,
      dashboardPayload,
      positionPayload,
      watchlistPayload,
      agentPayload
    ] = await Promise.all([
      api.health(),
      api.dashboard(),
      api.positions(),
      api.watchlist(),
      api.agents()
    ])

    health.value = healthPayload
    if (dashboardRequestId === dashboardRefreshRequest) {
      dashboard.value = dashboardPayload
    }
    positions.value = positionPayload
    if (watchlistRequestId === watchlistRefreshRequest) {
      watchlist.value = watchlistPayload
    }
    agents.value = agentPayload
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载失败'
  } finally {
    if (watchlistRequestId === watchlistRefreshRequest) {
      watchlistLoading.value = false
    }
    loading.value = false
    dashboardRefreshInFlightCount = Math.max(0, dashboardRefreshInFlightCount - 1)
  }
}

async function refreshDashboard(silent = false) {
  if (!currentUser.value) return
  // 静默自动刷新可合并；用户切到工作台等显式刷新不能被丢弃。
  // 用计数而不是“仅最新请求清 flag”，避免 refreshAll 抬高 requestId 后把 inFlight 永久卡住。
  if (silent && dashboardRefreshInFlightCount > 0) return
  const requestId = ++dashboardRefreshRequest
  dashboardRefreshInFlightCount += 1
  try {
    const payload = await api.dashboard()
    if (requestId === dashboardRefreshRequest) {
      dashboard.value = payload
    }
  } catch (err) {
    if (!silent && requestId === dashboardRefreshRequest) {
      error.value = err instanceof Error ? err.message : '工作台刷新失败'
    }
  } finally {
    dashboardRefreshInFlightCount = Math.max(0, dashboardRefreshInFlightCount - 1)
  }
}

function refreshVisibleDashboard() {
  if (
    currentUser.value
    && activePage.value === 'workbench'
    && document.visibilityState === 'visible'
  ) {
    void refreshDashboard(true)
  }
}

function startDashboardAutoRefresh() {
  if (dashboardRefreshTimer !== null) return
  dashboardRefreshTimer = window.setInterval(
    refreshVisibleDashboard,
    DASHBOARD_REFRESH_INTERVAL_MS
  )
}

function stopDashboardAutoRefresh() {
  if (dashboardRefreshTimer === null) return
  window.clearInterval(dashboardRefreshTimer)
  dashboardRefreshTimer = null
}

// 静默刷新持仓+自选：买入/卖出/删除后调用，不显示 loading 遮罩，避免页面闪烁
async function refreshPositions() {
  if (!currentUser.value) return
  const watchlistRequestId = ++watchlistRefreshRequest
  try {
    const [positionPayload, watchlistPayload] = await Promise.all([
      api.positions(),
      api.watchlist()
    ])
    positions.value = positionPayload
    if (watchlistRequestId === watchlistRefreshRequest) {
      watchlist.value = watchlistPayload
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载失败'
  } finally {
    if (watchlistRequestId === watchlistRefreshRequest) {
      watchlistLoading.value = false
    }
  }
}

async function refreshWatchlist() {
  if (!currentUser.value) return
  const requestId = ++watchlistRefreshRequest
  watchlist.value = []
  watchlistLoading.value = true
  error.value = ''
  try {
    const payload = await api.watchlist()
    if (requestId === watchlistRefreshRequest) {
      watchlist.value = payload
    }
  } catch (err) {
    if (requestId === watchlistRefreshRequest) {
      error.value = err instanceof Error ? err.message : '自选池加载失败'
    }
  } finally {
    if (requestId === watchlistRefreshRequest) {
      watchlistLoading.value = false
    }
  }
}

function applyWatchlistItem(item: WatchlistItem) {
  watchlist.value = [
    item,
    ...watchlist.value.filter((existing) => existing.id !== item.id)
  ]
  if (dashboard.value) {
    dashboard.value = {
      ...dashboard.value,
      counts: {
        ...dashboard.value.counts,
        watchlist: watchlist.value.length
      }
    }
  }
}

async function login(payload: { username: string; password: string; remember_me: boolean }) {
  loginLoading.value = true
  loginError.value = ''
  try {
    const result = await api.login(payload)
    setAuthToken(result.access_token, result.remember_me)
    currentUser.value = result.user
    restorePositionAgentSession()
    await refreshAll()
  } catch (err) {
    loginError.value = err instanceof Error ? err.message : '登录失败'
  } finally {
    loginLoading.value = false
  }
}

function logout() {
  persistPositionAgentSession()
  setAuthToken('')
  currentUser.value = null
  resetPositionAgentSessionMemory()
  dashboard.value = null
  activePage.value = 'workbench'
}

function handleAuthExpired() {
  logout()
  bootstrapping.value = false
  loginError.value = '登录已过期或已在其他位置失效，请重新登录。'
}

async function initialize() {
  if (!getAuthToken()) return
  bootstrapping.value = true
  try {
    // 只有 401（token 真失效）才清 token 落登录页；连接被拒/5xx（部署重启窗口、
    // 网络抖动）自动重试 ~16s，期间显示"正在连接"而不是把用户踢去重新登录。
    for (let attempt = 1; ; attempt++) {
      try {
        currentUser.value = await api.me()
        restorePositionAgentSession()
        break
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) {
          logout()
          return
        }
        if (attempt >= 6) throw err
        await new Promise((resolve) => setTimeout(resolve, Math.min(1500 * attempt, 4000)))
      }
    }
    await refreshAll()
  } catch {
    // 后端长时间不可达：保留 token 落到登录页兜底；服务恢复后刷新即可直接进入
    currentUser.value = null
  } finally {
    bootstrapping.value = false
  }
}

function modelOptionLabel(model: ModelConfig | null | undefined) {
  if (!model) return '默认模型'
  const active = model.is_active ? ' · 默认' : ''
  return `${model.display_name || model.model_name}${active}`
}

async function loadPositionModels() {
  const payload = await api.modelConfigs()
  positionModels.value = payload.filter((item) => item.status !== 'disabled')
  // 默认用“设置 → 模型配置”里标为默认的模型；上传截图时后端会自动换成支持识图的模型
  const preferred = positionModels.value.find((item) => item.is_active) || positionModels.value[0]
  selectedPositionModelId.value = preferred?.id || null
}

async function openPositionAgent(accountId: number | null = null) {
  resetPositionAgentSessionMemory()
  positionAgentClientSessionId.value = createPositionAgentClientSessionId()
  positionAgentTargetAccount.value = accountId
  positionDrawerOpen.value = true
  try {
    await loadPositionModels()
  } catch (err) {
    positionAgentMessage.value = err instanceof Error ? err.message : '模型列表加载失败'
  }
  try {
    positionAgentAccounts.value = await api.positionAccounts()
  } catch {
    positionAgentAccounts.value = [] // 账户列表加载失败不阻塞录入，仍可自动识别账户
  }
  scrollPositionChat(false)
}

function positionAccountLabel(account: PositionAccount) {
  const name = account.account_name || account.account_no_masked || `账户#${account.id}`
  return `${account.broker_name} · ${name}`
}

function closePositionAgent() {
  if (positionAgentBusy.value || positionAgentConfirming.value) return
  positionDrawerOpen.value = false
}

async function scrollPositionChat(smooth = true) {
  await nextTick()
  const element = positionChatScroll.value
  if (!element) return
  element.scrollTo({ top: element.scrollHeight, behavior: smooth ? 'smooth' : 'auto' })
}

function readFileAsDataUrl(file: File) {
  return new Promise<string>((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result || ''))
    reader.onerror = () => reject(reader.error || new Error('读取图片失败'))
    reader.readAsDataURL(file)
  })
}

async function setPositionAgentImage(file: File) {
  if (!file.type.startsWith('image/')) {
    positionAgentMessage.value = '只能拖入图片文件'
    return
  }
  positionAgentMimeType.value = file.type || 'image/png'
  positionAgentFileName.value = file.name
  positionAgentImageBase64.value = await readFileAsDataUrl(file)
  positionAgentPendingAction.value = null
  positionAgentReviewRows.value = []
  try {
    await api.discardPositionAgentDraft()
  } catch (err) {
    positionAgentMessage.value = err instanceof Error ? err.message : '旧的 OCR 草稿清理失败，请重试'
  }
  if (!positionAgentInstruction.value.trim()) {
    positionAgentInstruction.value = '请识别这张 A 股券商持仓截图，按持仓明细保存。'
  }
}

async function onPositionImageSelected(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  await setPositionAgentImage(file)
  input.value = ''
}

function onPositionImageDragEnter(event: DragEvent) {
  if (positionAgentBusy.value || !Array.from(event.dataTransfer?.types || []).includes('Files')) return
  positionAgentDragDepth += 1
  positionAgentDragging.value = true
}

function onPositionImageDragOver(event: DragEvent) {
  if (positionAgentBusy.value || !event.dataTransfer) return
  event.dataTransfer.dropEffect = 'copy'
}

function onPositionImageDragLeave() {
  positionAgentDragDepth = Math.max(0, positionAgentDragDepth - 1)
  if (positionAgentDragDepth === 0) positionAgentDragging.value = false
}

async function onPositionImageDrop(event: DragEvent) {
  positionAgentDragDepth = 0
  positionAgentDragging.value = false
  if (positionAgentBusy.value) return
  const files = Array.from(event.dataTransfer?.files || [])
  const file = files.find((item) => item.type.startsWith('image/'))
  if (!file) {
    positionAgentMessage.value = '没有检测到可用的图片文件'
    return
  }
  await setPositionAgentImage(file)
}

function clearPositionAgentImage() {
  positionAgentImageBase64.value = ''
  positionAgentFileName.value = ''
  positionAgentMimeType.value = 'image/png'
}

function setPositionAgentReviewDraft(pending: PositionAgentPendingAction | null) {
  if (!pending?.review_required || !Array.isArray(pending.arguments.rows)) {
    positionAgentReviewRows.value = []
    return
  }
  const validations = pending.validations || []
  positionAgentReviewRows.value = pending.arguments.rows.map((source, index) => {
    const validation = validations.find((item) => Number(item.index) === index) || {
      index,
      valid: false,
      status: 'unchecked',
      input_symbol: String(source.symbol || ''),
      normalized_symbol: String(source.symbol || ''),
      message: '等待校验'
    }
    return {
      ...source,
      symbol: String(source.symbol || ''),
      name: String(source.name || ''),
      validation,
      validating: false
    }
  })
}

function markPositionSymbolEdited(row: PositionAgentReviewRow) {
  row.validation = {
    valid: false,
    status: 'unchecked',
    input_symbol: row.symbol,
    normalized_symbol: row.symbol,
    message: '代码已修改，请重新校验'
  }
}

function applyPositionCorrection(corrected: PositionItem) {
  const index = positions.value.findIndex((item) => item.id === corrected.id)
  if (index < 0) {
    void refreshPositions()
    return
  }
  positions.value = positions.value.map((item, itemIndex) =>
    itemIndex === index ? { ...item, ...corrected } : item
  )
}

async function validatePositionReviewRow(index: number) {
  const row = positionAgentReviewRows.value[index]
  if (!row || row.validating) return
  row.validating = true
  try {
    const validation = await api.validatePositionAgentSymbol({
      symbol: row.symbol.trim(),
      name: row.name.trim() || null
    })
    row.validation = validation
    if (validation.valid) {
      row.symbol = validation.normalized_symbol
      row.name = validation.canonical_name || row.name
    }
  } catch (err) {
    row.validation = {
      valid: false,
      status: 'validation_error',
      input_symbol: row.symbol,
      normalized_symbol: row.symbol,
      message: err instanceof Error ? err.message : '股票代码校验失败'
    }
  } finally {
    row.validating = false
  }
}

async function applyPositionSymbolSuggestion(index: number) {
  const row = positionAgentReviewRows.value[index]
  const suggestion = row?.validation.suggested_symbol
  if (!row || !suggestion) return
  row.symbol = suggestion
  if (row.validation.suggested_name) row.name = row.validation.suggested_name
  await validatePositionReviewRow(index)
}

async function cancelPositionAgentReview() {
  positionAgentConfirming.value = true
  try {
    await api.discardPositionAgentDraft()
    positionAgentPendingAction.value = null
    positionAgentReviewRows.value = []
    positionAgentMessage.value = '已取消本次 OCR 录入，未写入任何持仓。'
  } catch (err) {
    positionAgentMessage.value = err instanceof Error ? err.message : 'OCR 草稿取消失败'
  } finally {
    positionAgentConfirming.value = false
  }
}

async function confirmPositionAgentReview() {
  if (!positionAgentPendingAction.value || positionAgentConfirming.value) return
  positionAgentConfirming.value = true
  positionAgentMessage.value = ''
  try {
    await Promise.all(positionAgentReviewRows.value.map((_, index) => validatePositionReviewRow(index)))
    const invalidRows = positionAgentReviewRows.value.filter((row) => !row.validation.valid)
    if (invalidRows.length) {
      positionAgentMessage.value = `还有 ${invalidRows.length} 条股票代码未通过校验，请修正后再入库。`
      return
    }
    const pendingArguments = positionAgentPendingAction.value.arguments
    const rows = positionAgentReviewRows.value.map(({ validation: _validation, validating: _validating, ...row }) => row)
    const result = await api.confirmPositionAgentDraft({
      rows,
      account: pendingArguments.account,
      as_of_date: pendingArguments.as_of_date,
      account_id: positionAgentTargetAccount.value
    })
    positionAgentRows.value = result.rows
    positionAgentPendingAction.value = null
    positionAgentReviewRows.value = []
    positionAgentMessage.value = result.summary
    positionAgentChats.value.push({
      id: Date.now(),
      role: 'assistant',
      status: 'done',
      kind: 'saved',
      content: positionAgentResultMarkdown(result.rows, [], result.summary)
    })
    await refreshAll()
    scrollPositionChat()
  } catch (err) {
    positionAgentMessage.value = err instanceof Error ? err.message : '复核后的持仓保存失败'
  } finally {
    positionAgentConfirming.value = false
  }
}

function positionReviewNumber(value: unknown, digits = 2) {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? formatNumber(parsed, digits) : '-'
}

function buildPositionAgentHistory() {
  return positionAgentChats.value
    .filter((item) => item.content.trim() && (item.role === 'user' || item.status === 'done'))
    .map((item) => ({ role: item.role, content: item.content.trim() }))
    .slice(-8)
}

async function agentAddPositions() {
  const instruction = positionAgentInstruction.value.trim()
  const fallbackInstruction = positionAgentImageBase64.value ? '请识别这张 A 股券商持仓截图，按持仓明细保存。' : ''
  if (!instruction && !fallbackInstruction) {
    positionAgentMessage.value = '请输入持仓描述，或先选择一张持仓截图'
    return
  }
  positionAgentBusy.value = true
  positionAgentMessage.value = ''
  positionAgentRows.value = []
  const userContent = instruction || fallbackInstruction
  const userImageName = positionAgentFileName.value
  const history = buildPositionAgentHistory()
  positionAgentChats.value.push({
    id: Date.now(),
    role: 'user',
    content: userContent,
    imageName: userImageName || undefined
  })
  const assistantMessage: PositionAgentChat = {
    id: Date.now() + 1,
    role: 'assistant',
    content: '',
    steps: [],
    status: 'thinking'
  }
  positionAgentChats.value.push(assistantMessage)
  const assistantMessageId = assistantMessage.id
  scrollPositionChat()
  try {
    await api.streamPositionAgentAdd(
      {
        instruction: userContent,
        image_base64: positionAgentImageBase64.value || undefined,
        mime_type: positionAgentMimeType.value,
        model_config_id: selectedPositionModelId.value,
        account_id: positionAgentTargetAccount.value,
        client_session_id: positionAgentClientSessionId.value,
        history,
        pending_action: positionAgentPendingAction.value
      },
      (event) => handlePositionAgentEvent(event, assistantMessageId)
    )
    positionAgentInstruction.value = ''
    clearPositionAgentImage()
  } catch (err) {
    positionAgentMessage.value = err instanceof Error ? err.message : 'Agent 录入失败'
    updatePositionAgentMessage(assistantMessageId, (message) => ({
      ...message,
      status: 'error',
      error: positionAgentMessage.value,
      content: ''
    }))
  } finally {
    positionAgentBusy.value = false
    scrollPositionChat()
  }
}

function updatePositionAgentMessage(id: number, updater: (message: PositionAgentChat) => PositionAgentChat) {
  const index = positionAgentChats.value.findIndex((item) => item.id === id)
  if (index < 0) return
  positionAgentChats.value[index] = updater(positionAgentChats.value[index])
}

function currentPositionAgentMessage(id: number) {
  return positionAgentChats.value.find((item) => item.id === id)
}

function handlePositionAgentEvent(event: PositionAgentStreamEvent, messageId: number) {
  const message = currentPositionAgentMessage(messageId)
  if (!message) return

  if (event.type === 'model_selected' && event.step) {
    updatePositionAgentMessage(messageId, (current) => ({
      ...current,
      steps: [...(current.steps || []), event.step as PositionAgentStep]
    }))
  } else if (event.type === 'thinking') {
    const content = event.message || '持仓 Agent 正在分析...'
    const last = message.steps?.[message.steps.length - 1]
    if (/^AgentScope 正在分析/.test(content)) {
      // 引擎的通用进度提示，状态条已经显示“正在识别”，不进处理过程
    } else if (last?.type !== 'thought' || last.content !== content) {
      updatePositionAgentMessage(messageId, (current) => ({
        ...current,
        steps: [...(current.steps || []), { type: 'thought', content }]
      }))
    }
  } else if (event.type === 'answer_delta') {
    updatePositionAgentMessage(messageId, (current) => ({
      ...current,
      content: `${current.content}${event.content || ''}`
    }))
  } else if ((event.type === 'tool_start' || event.type === 'tool_done') && event.step) {
    updatePositionAgentMessage(messageId, (current) => ({
      ...current,
      steps: [...(current.steps || []), event.step as PositionAgentStep]
    }))
    if (event.rows?.length) {
      positionAgentRows.value = event.rows.filter((row): row is PositionItem => !('error' in row))
    }
  } else if (event.type === 'done') {
    const latest = currentPositionAgentMessage(messageId) || message
    const rows = event.rows || []
    positionAgentRows.value = rows.filter((row): row is PositionItem => !('error' in row))
    const failedRows = rows.filter((row): row is { error: string; raw?: Record<string, unknown> } => 'error' in row)
    positionAgentMessage.value = event.summary || event.reply || `已保存 ${positionAgentRows.value.length} 条持仓`
    positionAgentPendingAction.value = event.pending_action || null
    setPositionAgentReviewDraft(positionAgentPendingAction.value)
    const finalContent = event.should_save
      ? positionAgentResultMarkdown(positionAgentRows.value, failedRows, positionAgentMessage.value)
      : latest.content.trim()
        ? latest.content
        : (event.reply || positionAgentMessage.value || '我可以帮你录入 A 股持仓。你可以输入持仓描述，或上传券商持仓截图。')
    updatePositionAgentMessage(messageId, (current) => ({
      ...current,
      status: 'done',
      kind: event.should_save ? 'saved' : 'reply',
      content: finalContent
    }))
    if (event.should_save) {
      void refreshAll()
    }
  } else if (event.type === 'error') {
    const errorMessage = event.message || '持仓 Agent 流式响应异常'
    updatePositionAgentMessage(messageId, (current) => ({
      ...current,
      status: 'error',
      error: errorMessage,
      content: ''
    }))
    positionAgentMessage.value = errorMessage
  }
  scrollPositionChat()
}

function positionAgentResultMarkdown(rows: PositionItem[], errors: Array<{ error: string }>, summary: string) {
  const lines = [`**${summary}**`]
  if (rows.length) {
    lines.push('', '| 标的 | 名称 | 持仓 | 可用 | 成本价 | 现价 | 市值 | 浮盈 |', '| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |')
    for (const row of rows) {
      lines.push(
        `| ${row.symbol} | ${row.name || '-'} | ${formatNumber(row.position_quantity ?? row.quantity, 0)} | ${formatNumber(row.available_quantity, 0)} | ${formatNumber(row.cost_price)} | ${formatNumber(row.latest_price)} | ${formatNumber(row.market_value)} | ${formatNumber(row.floating_pnl)} |`
      )
    }
  }
  if (errors.length) {
    lines.push('', `有 ${errors.length} 条没有保存：${errors.map((item) => item.error).join('；')}`)
  }
  return lines.join('\n')
}

function positionStatusText(message: PositionAgentChat) {
  if (message.status === 'thinking') return '持仓 Agent 正在识别'
  if (message.status === 'done') return message.kind === 'saved' ? '持仓已保存' : 'Agent 已回复'
  if (message.status === 'error') return '录入失败'
  return '持仓 Agent'
}

function positionStepLabel(step: { type: string; display_name?: string; tool?: string }) {
  const tool = step.display_name || step.tool || '工具'
  if (step.type === 'model') return '模型'
  if (step.type === 'thought') return '思考'
  if (step.type === 'action') return `调用 · ${tool}`
  if (step.type === 'observation') return `结果 · ${tool}`
  if (step.type === 'error') return `出错 · ${tool}`
  return step.type
}

function positionTraceSummary(message: PositionAgentChat) {
  const steps = message.steps || []
  const modelStep = steps.find((step) => step.type === 'model')
  const model = modelStep?.content.replace(/^使用\s*/, '').replace(/\s*处理。?$/, '').trim()
  const toolCount = steps.filter((step) => step.type === 'action').length
  const parts = [
    message.status === 'thinking' ? '处理中' : '处理过程',
    model ? `模型 ${model}` : '',
    toolCount ? `调用工具 ${toolCount} 次` : ''
  ]
  return parts.filter(Boolean).join(' · ')
}

function onPositionComposerKeydown(event: KeyboardEvent) {
  // 回车发送，Shift+回车换行；中文输入法选字时的回车不发送
  if (event.key !== 'Enter' || event.shiftKey || event.isComposing || event.keyCode === 229) return
  event.preventDefault()
  if (positionAgentBusy.value) return
  if (!positionAgentInstruction.value.trim() && !positionAgentImageBase64.value) return
  void agentAddPositions()
}

function formatNumber(value: number | null | undefined, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-'
  return Number(value).toLocaleString('zh-CN', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits
  })
}

function openChartFromPosition(position: PositionItem) {
  selectedChartPosition.value = position
  selectedChartInstrument.value = null
  chartReturnPage.value = 'positions'
  activePage.value = 'positionChart'
}

function openChartFromWatchlist(item: WatchlistItem) {
  selectedChartPosition.value = null
  selectedChartInstrument.value = { symbol: item.symbol, name: item.name }
  chartReturnPage.value = 'watchlist'
  activePage.value = 'positionChart'
}

function backFromChart() {
  activePage.value = chartReturnPage.value
}

function navigate(page: PageKey) {
  if (page === 'settings' && !canViewSettings.value) return
  if (page === 'digitalHumans') {
    digitalHumanInitialTab.value = 'chat'
    digitalHumanInitialSession.value = null
    digitalHumanInitialQuestion.value = ''
  }
  if (page === 'askStock') askStockInitialQuestion.value = ''
  if (page === 'workbench') void refreshDashboard()
  if (page === 'watchlist') void refreshWatchlist()
  if (page === 'industryAnalysis') {
    if (!/^\/industries\/?$/i.test(window.location.pathname)) window.history.pushState({}, '', '/industries')
  } else if (page === 'settings') {
    if (!/^\/settings\/?$/i.test(window.location.pathname)) {
      const initialTab = ['admin', 'super_admin'].includes(currentUser.value?.role || '') ? 'models' : 'mcp'
      window.history.pushState({}, '', `/settings?tab=${initialTab}`)
    }
  } else if (page === 'automaticSelection') {
    if (!/^\/automatic-selection\/?$/i.test(window.location.pathname)) {
      window.history.pushState({}, '', '/automatic-selection')
    }
  } else if (page === 'scorecard') {
    if (!/^\/scorecard\/?$/i.test(window.location.pathname)) window.history.pushState({}, '', '/scorecard')
  } else if (
    page !== 'backtestReport' &&
    page !== 'opsConsole' &&
    (window.location.pathname.startsWith('/backtests/') ||
      /^\/oc-console-/i.test(window.location.pathname) ||
      /^\/automatic-selection\/?$/i.test(window.location.pathname) ||
      /^\/scorecard\/?$/i.test(window.location.pathname) ||
      /^\/settings\/?$/i.test(window.location.pathname) ||
      /^\/industries\/?$/i.test(window.location.pathname))
  ) {
    window.history.pushState({}, '', '/')
  }
  activePage.value = page
}

function syncPageFromLocation() {
  const pathname = window.location.pathname
  if (/^\/settings\/?$/i.test(pathname)) {
    activePage.value = 'settings'
    return
  }
  if (/^\/industries\/?$/i.test(pathname)) {
    activePage.value = 'industryAnalysis'
    return
  }
  if (/^\/automatic-selection\/?$/i.test(pathname)) {
    activePage.value = 'automaticSelection'
    return
  }
  if (/^\/scorecard\/?$/i.test(pathname)) {
    activePage.value = 'scorecard'
    return
  }
  if (/^\/oc-console-[0-9a-f]+\/?$/i.test(pathname)) {
    activePage.value = 'opsConsole'
    return
  }
  const match = pathname.match(/^\/backtests\/([a-zA-Z0-9_-]{8,64})\/?$/)
  if (match) {
    backtestRunId.value = match[1]
    activePage.value = 'backtestReport'
  } else if (activePage.value === 'backtestReport') {
    activePage.value = backtestReturnPage.value
  } else if (activePage.value === 'automaticSelection') {
    activePage.value = 'workbench'
  } else if (activePage.value === 'scorecard') {
    activePage.value = 'workbench'
  } else if (activePage.value === 'industryAnalysis') {
    activePage.value = 'workbench'
  } else if (activePage.value === 'settings') {
    activePage.value = 'workbench'
  }
}

function openBacktest(runId: string, returnPage: BacktestReturnPage = 'digitalHumans') {
  backtestRunId.value = runId
  backtestReturnPage.value = returnPage
  window.history.pushState({ backtestRunId: runId }, '', `/backtests/${runId}`)
  activePage.value = 'backtestReport'
}

function openBacktestFromHistory(runId: string) {
  openBacktest(runId, 'backtestHistory')
}

function openScreenerBacktest(runId: string) {
  openBacktest(runId, 'screener')
}

function closeBacktest() {
  window.history.pushState({}, '', '/')
  activePage.value = backtestReturnPage.value
}

function openFeishuBindings() {
  digitalHumanInitialTab.value = 'feishuBots'
  digitalHumanInitialSession.value = null
  digitalHumanInitialQuestion.value = ''
  activePage.value = 'digitalHumans'
}

function openBoardSession(sessionId: number) {
  // 任务看板点击卡片 → 进入数字中心对应的对话窗口
  digitalHumanInitialTab.value = 'chat'
  digitalHumanInitialSession.value = sessionId
  digitalHumanInitialQuestion.value = ''
  activePage.value = 'digitalHumans'
}

function screenIndustry(industry: string) {
  screenerInitialIndustry.value = industry
  navigate('screener')
}

function openIndustryStockAnalysis(item: IndustryMember) {
  navigate('digitalHumans')
  digitalHumanInitialQuestion.value = industryQuestion(item)
}

function openScreenerStockAnalysis(item: ScreenerItem) {
  const snapshot = [
    item.price != null ? `最新价 ${formatNumber(item.price)} 元` : '',
    item.pct_chg != null ? `涨跌幅 ${formatNumber(item.pct_chg)}%` : '',
    item.volume_ratio != null ? `量比 ${formatNumber(item.volume_ratio)}` : '',
    item.turnover_rate != null ? `换手率 ${formatNumber(item.turnover_rate)}%` : '',
    item.total_mv_yi != null ? `总市值 ${formatNumber(item.total_mv_yi, 1)} 亿元` : '',
    item.pe_ttm != null ? `PE(TTM) ${formatNumber(item.pe_ttm, 1)}` : ''
  ].filter(Boolean).join('，')

  digitalHumanInitialTab.value = 'chat'
  digitalHumanInitialSession.value = null
  digitalHumanInitialQuestion.value = [
    `请对 ${item.name}（${item.symbol}）进行专项解读。`,
    snapshot ? `该标的来自 AI 选股结果，当前筛选快照：${snapshot}。` : '',
    '请先查最新数据，再用大白话告诉我：现在走势怎么样、关键价位在哪，基本面和估值贵不贵，最近发生了什么事，最大的风险是什么，接下来该盯什么。'
  ].filter(Boolean).join('\n')
  activePage.value = 'digitalHumans'
}

function openAutomaticSelectionStockAnalysis(pick: AutomaticSelectionPick, tradeDate: string) {
  const risks = pick.risks.slice(0, 3).map((risk) => risk.text).join('；')
  const dateText = /^\d{8}$/.test(tradeDate)
    ? `${tradeDate.slice(0, 4)}-${tradeDate.slice(4, 6)}-${tradeDate.slice(6, 8)}`
    : tradeDate
  digitalHumanInitialTab.value = 'chat'
  digitalHumanInitialSession.value = null
  digitalHumanInitialQuestion.value = [
    `请对 ${pick.name}（${pick.symbol}）进行专项解读。`,
    `该股票来自每日自动选股（${dateText} 收盘后第 ${pick.rank} 名）：${pick.headline}。`,
    risks ? `规则提示的注意点：${risks}。` : '',
    '请先查最新数据，再用大白话告诉我：现在走势怎么样、关键价位在哪，基本面和估值贵不贵，最近发生了什么事，最大的风险是什么，接下来该盯什么。'
  ].filter(Boolean).join('\n')
  activePage.value = 'digitalHumans'
}

function createScreenerAgentTask(items: ScreenerItem[]) {
  const pool = items.slice(0, 20).map((item) => `${item.name}（${item.symbol}）`).join('、')
  digitalHumanInitialTab.value = 'chat'
  digitalHumanInitialSession.value = null
  digitalHumanInitialQuestion.value = [
    '请为这组选股结果创建一个持续跟踪的 Agent 任务。',
    `候选池：${pool}。`,
    '请先确认可用的任务与调度工具，再创建每日收盘后的跟踪任务：核对量价趋势、显著异动、公告与主要风险，只在有实质变化时给出摘要。创建前请展示任务名称、时间、股票池和输出内容供我确认。'
  ].join('\n')
  activePage.value = 'digitalHumans'
}

function openAskStock(symbol: string) {
  askStockInitialQuestion.value = `请分析 ${symbol} 的最新行情、趋势、风险和关键价位。`
  activePage.value = 'askStock'
}

onMounted(() => {
  syncPageFromLocation()
  window.addEventListener('popstate', syncPageFromLocation)
  window.addEventListener('focus', refreshVisibleDashboard)
  window.addEventListener('hermes-auth-expired', handleAuthExpired)
  document.addEventListener('visibilitychange', refreshVisibleDashboard)
  startDashboardAutoRefresh()
  void initialize()
})
onBeforeUnmount(() => {
  stopDashboardAutoRefresh()
  window.removeEventListener('popstate', syncPageFromLocation)
  window.removeEventListener('focus', refreshVisibleDashboard)
  window.removeEventListener('hermes-auth-expired', handleAuthExpired)
  document.removeEventListener('visibilitychange', refreshVisibleDashboard)
})
</script>

<template>
  <div v-if="bootstrapping && !currentUser" class="boot-splash">
    <span class="boot-splash-pulse"></span>
    正在连接 SixRabbits Stock...
  </div>

  <LoginView v-else-if="!currentUser" :loading="loginLoading" :error="loginError" @login="login" />

  <AppShell
    v-else
    :active-page="activePage === 'positionChart' ? chartReturnPage : activePage === 'backtestReport' ? backtestReturnPage : activePage"
    :page-title="pageTitle"
    :page-subtitle="pageSubtitle"
    :health="health"
    :user="currentUser"
    @navigate="navigate"
    @refresh="refreshAll"
    @logout="logout"
  >
    <div v-if="error" class="notice notice-danger">
      <strong>接口错误</strong>
      <span>{{ error }}</span>
    </div>

    <div v-if="loading" class="loading-panel">正在加载 A股研究台...</div>

    <template v-else>
      <WorkbenchView
        v-if="activePage === 'workbench' && dashboard"
        :dashboard="dashboard"
        @navigate="navigate"
        @open-position-agent="openPositionAgent()"
        @open-chart="openChartFromPosition"
        @ask-stock="openAskStock"
        @refresh="refreshAll"
      />

      <AskStockView v-else-if="activePage === 'askStock'" :initial-question="askStockInitialQuestion" />
      <BacktestHistoryView
        v-else-if="activePage === 'backtestHistory'"
        @open="openBacktestFromHistory"
      />
      <BacktestReportView
        v-else-if="activePage === 'backtestReport'"
        :run-id="backtestRunId"
        :back-label="backtestReturnPage === 'screener' ? '返回策略构建' : backtestReturnPage === 'backtestHistory' ? '返回回测报告列表' : '返回数字人中心'"
        @back="closeBacktest"
      />
      <FactorAnalysisView v-else-if="activePage === 'factorAnalysis'" />
      <AutomaticSelectionView
        v-else-if="activePage === 'automaticSelection'"
        :user="currentUser"
        @watchlist-added="applyWatchlistItem"
        @ask-stock="openAutomaticSelectionStockAnalysis"
      />
      <ScorecardView v-else-if="activePage === 'scorecard'" :user="currentUser" />
      <IndustryAnalysisView
        v-else-if="activePage === 'industryAnalysis'"
        :user="currentUser"
        @watchlist-added="applyWatchlistItem"
        @ask-stock="openIndustryStockAnalysis"
        @screen-industry="screenIndustry"
        @open-screener="navigate('screener')"
      />
      <ScreenerView
        v-else-if="activePage === 'screener'"
        :initial-industry="screenerInitialIndustry"
        @industry-applied="screenerInitialIndustry = ''"
        @watchlist-added="applyWatchlistItem"
        @ask-stock="openScreenerStockAnalysis"
        @open-backtest="openScreenerBacktest"
        @create-agent-task="createScreenerAgentTask"
      />
      <DigitalHumanCenter
        v-else-if="activePage === 'digitalHumans'"
        :initial-tab="digitalHumanInitialTab"
        :initial-session-id="digitalHumanInitialSession"
        :initial-question="digitalHumanInitialQuestion"
        @open-backtest="openBacktest"
      />

      <PositionChartView
        v-else-if="activePage === 'positionChart'"
        :position="selectedChartPosition"
        :instrument="selectedChartInstrument"
        :back-label="chartReturnPage === 'watchlist' ? '返回自选池' : '返回持仓'"
        @back="backFromChart"
      />

      <PositionsWorkbench
        v-else-if="activePage === 'positions'"
        :positions="positions"
        @refresh="refreshAll"
        @refresh-positions="refreshPositions"
        @position-corrected="applyPositionCorrection"
        @open-agent="openPositionAgent"
        @open-chart="openChartFromPosition"
      />

      <WatchlistView
        v-else-if="activePage === 'watchlist'"
        :watchlist="watchlist"
        :loading="watchlistLoading"
        @refresh="refreshWatchlist"
        @open-chart="openChartFromWatchlist"
        @manage-feishu="openFeishuBindings"
      />

      <RhythmView v-else-if="activePage === 'rhythm'" />

      <KnowledgeBaseView v-else-if="activePage === 'knowledge'" />

      <KnowledgeSearchView v-else-if="activePage === 'knowledgeSearch'" />

      <WorkspaceDocsView v-else-if="activePage === 'workspaceDocs'" />

      <SkillSquareView v-else-if="activePage === 'skillSquare'" />

      <OpsConsoleView v-else-if="activePage === 'opsConsole' && canViewOps" />

      <AgentTaskBoardView
        v-else-if="activePage === 'agentTaskBoard'"
        @open-session="openBoardSession"
      />

      <SettingsView
        v-else-if="activePage === 'settings' && canViewSettings"
        :health="health"
        :user="currentUser"
        :agents="agents"
      />
    </template>

    <div v-if="positionDrawerOpen" class="position-agent-backdrop" @click.self="closePositionAgent">
      <aside class="position-agent-drawer">
        <header class="position-agent-drawer-header">
          <div>
            <h2>持仓录入 Agent</h2>
            <p>可以直接输入持仓描述，也可以上传券商持仓截图后让模型识别入库。</p>
          </div>
          <button type="button" :disabled="positionAgentBusy || positionAgentConfirming" @click="closePositionAgent">关闭</button>
        </header>

        <section ref="positionChatScroll" class="message-list position-agent-messages">
          <div v-if="!positionAgentChats.length" class="chat-empty">
            <strong>添加持仓</strong>
            <span>试试输入“平安银行 1000 股，成本 10.2，现价 10.8，止损 9.89”，或者上传一张持仓截图。</span>
          </div>

          <article v-for="item in positionAgentChats" :key="item.id" class="message" :class="item.role">
            <template v-if="item.role === 'user'">
              <p>{{ item.content }}</p>
              <small v-if="item.imageName" class="position-image-chip">{{ item.imageName }}</small>
            </template>
            <template v-else>
              <div
                v-if="item.status && (item.status !== 'done' || item.kind === 'saved')"
                class="stream-status"
                :class="{ tooling: item.status === 'thinking', done: item.status === 'done', error: item.status === 'error' }"
              >
                <span class="stream-status-pulse"></span>
                <strong>{{ positionStatusText(item) }}</strong>
              </div>
              <details v-if="item.steps?.length" class="position-agent-trace" :open="item.status === 'thinking'">
                <summary>{{ positionTraceSummary(item) }}</summary>
                <div class="react-steps">
                  <div v-for="(step, index) in item.steps" :key="index" class="react-step" :class="step.type">
                    <strong>{{ positionStepLabel(step) }}</strong>
                    <span>{{ step.content }}</span>
                  </div>
                </div>
              </details>
              <div v-if="item.content" class="md-body" v-html="renderMarkdown(item.content)"></div>
              <div v-else-if="item.error" class="chat-error">{{ item.error }}</div>
              <div v-else class="chat-typing">正在识别并写入持仓...</div>
            </template>
          </article>

          <section v-if="positionAgentReviewRows.length" class="position-ocr-review">
            <header>
              <div>
                <span>OCR 识别复核</span>
                <strong>确认股票代码后再写入持仓</strong>
              </div>
              <small>{{ positionAgentReviewRows.filter((row) => row.validation.valid).length }}/{{ positionAgentReviewRows.length }} 已通过</small>
            </header>
            <div class="position-ocr-review-list">
              <article
                v-for="(row, index) in positionAgentReviewRows"
                :key="index"
                class="position-ocr-review-row"
                :class="{ valid: row.validation.valid, invalid: !row.validation.valid && row.validation.status !== 'unchecked' }"
              >
                <div class="position-ocr-code">
                  <label :for="`position-ocr-symbol-${index}`">股票代码</label>
                  <div>
                    <input
                      :id="`position-ocr-symbol-${index}`"
                      v-model.trim="row.symbol"
                      maxlength="16"
                      :disabled="row.validating || positionAgentConfirming"
                      @input="markPositionSymbolEdited(row)"
                      @keyup.enter.prevent="validatePositionReviewRow(index)"
                    />
                    <button type="button" :disabled="row.validating || positionAgentConfirming" @click="validatePositionReviewRow(index)">
                      {{ row.validating ? '校验中' : '校验' }}
                    </button>
                  </div>
                </div>
                <div class="position-ocr-stock">
                  <span>{{ row.name || '未识别名称' }}</span>
                  <small>持仓 {{ positionReviewNumber(row.position_quantity, 0) }} 股 · 成本 {{ positionReviewNumber(row.cost_price) }}</small>
                </div>
                <div class="position-ocr-validation" :class="{ valid: row.validation.valid }">
                  <strong>{{ row.validation.valid ? '校验通过' : '需要核对' }}</strong>
                  <span>{{ row.validation.message }}</span>
                  <button
                    v-if="row.validation.suggested_symbol"
                    type="button"
                    :disabled="positionAgentConfirming"
                    @click="applyPositionSymbolSuggestion(index)"
                  >
                    使用建议 {{ row.validation.suggested_symbol }} {{ row.validation.suggested_name || '' }}
                  </button>
                </div>
              </article>
            </div>
            <footer>
              <button type="button" class="secondary" :disabled="positionAgentConfirming" @click="cancelPositionAgentReview">取消本次录入</button>
              <button type="button" class="primary" :disabled="positionAgentConfirming || positionAgentReviewRows.some((row) => row.validating)" @click="confirmPositionAgentReview">
                {{ positionAgentConfirming ? '正在入库' : '确认无误并入库' }}
              </button>
            </footer>
          </section>
        </section>

        <form
          class="position-agent-composer"
          :class="{ 'is-dragging-image': positionAgentDragging }"
          @submit.prevent="agentAddPositions"
          @dragenter.prevent="onPositionImageDragEnter"
          @dragover.prevent="onPositionImageDragOver"
          @dragleave.prevent="onPositionImageDragLeave"
          @drop.prevent="onPositionImageDrop"
        >
          <div v-if="positionAgentDragging" class="position-agent-drop-hint">
            <strong>松开即可添加图片</strong>
          </div>
          <div v-if="positionAgentFileName" class="position-selected-file">
            <span>{{ positionAgentFileName }}</span>
            <button type="button" :disabled="positionAgentBusy" @click="clearPositionAgentImage">移除</button>
          </div>
          <textarea
            v-model="positionAgentInstruction"
            rows="2"
            placeholder="说一句就行，比如“方正 卖出同力天启 500 股 24.15”；也可以上传持仓截图"
            :disabled="positionAgentBusy"
            @keydown="onPositionComposerKeydown"
          ></textarea>
          <div class="position-agent-composer-actions">
            <label class="position-agent-upload" title="上传券商持仓截图">
              <input type="file" accept="image/*" :disabled="positionAgentBusy" @change="onPositionImageSelected" />
              <span>上传截图</span>
            </label>
            <select
              v-model="positionAgentTargetAccount"
              class="position-agent-account"
              :disabled="positionAgentBusy"
              title="选择这批持仓保存到哪个证券账户"
            >
              <option :value="null">自动识别账户</option>
              <option v-for="account in positionAgentAccounts" :key="account.id" :value="account.id">
                {{ positionAccountLabel(account) }}
              </option>
            </select>
            <select
              v-model.number="selectedPositionModelId"
              class="position-agent-model"
              :disabled="positionAgentBusy || !positionModels.length"
              :title="modelOptionLabel(activePositionModel)"
            >
              <optgroup v-for="group in positionModelGroups" :key="group.platform" :label="group.platform">
                <option v-for="item in group.rows" :key="item.id" :value="item.id">
                  {{ modelOptionLabel(item) }}
                </option>
              </optgroup>
            </select>
            <span class="position-agent-hint">Enter 发送 · Shift+Enter 换行</span>
            <button
              type="submit"
              class="position-agent-send"
              :disabled="positionAgentBusy || (!positionAgentInstruction.trim() && !positionAgentImageBase64)"
            >
              {{ positionAgentBusy ? '处理中…' : '发送' }}
            </button>
          </div>
          <p v-if="positionAgentComposerMessage && !positionAgentBusy" class="form-message">{{ positionAgentComposerMessage }}</p>
        </form>
      </aside>
    </div>
  </AppShell>
</template>

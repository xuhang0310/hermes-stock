<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue'
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
import { renderMarkdown } from './utils/markdown'
import type {
  AgentItem,
  DashboardPayload,
  HealthPayload,
  ModelConfig,
  PositionAccount,
  PositionAgentStep,
  PositionAgentStreamEvent,
  PositionItem,
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
  | 'rhythm'
  | 'knowledge'
  | 'knowledgeSearch'
  | 'workspaceDocs'
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

const activePage = ref<PageKey>('workbench')
const bootstrapping = ref(false)
const loading = ref(false)
const loginLoading = ref(false)
const loginError = ref('')
const error = ref('')
const currentUser = ref<UserPayload | null>(null)

const health = ref<HealthPayload | null>(null)
const dashboard = ref<DashboardPayload | null>(null)
const positions = ref<PositionItem[]>([])
const watchlist = ref<WatchlistItem[]>([])
const agents = ref<AgentItem[]>([])

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
const positionAgentTargetAccount = ref<number | null>(null)
const positionAgentAccounts = ref<PositionAccount[]>([])

const pageTitle = computed(() => {
  if (activePage.value === 'positionChart') {
    return chartReturnPage.value === 'watchlist' ? '自选行情图表' : '持仓图表'
  }
  const labels: Record<PageKey, string> = {
    askStock: '问股',
    workbench: '今日工作台',
    digitalHumans: '数字人中心',
    positions: '持仓',
    positionChart: '行情图表',
    watchlist: '自选池',
    rhythm: '定时任务',
    knowledge: '知识库',
    knowledgeSearch: '检索agent',
    workspaceDocs: '生成文档',
    settings: '设置'
  }
  return labels[activePage.value]
})

const activePositionModel = computed(
  () =>
    positionModels.value.find((item) => item.id === selectedPositionModelId.value) ||
    positionModels.value.find((item) => item.is_active) ||
    positionModels.value[0] ||
    null
)

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
    dashboard.value = dashboardPayload
    positions.value = positionPayload
    watchlist.value = watchlistPayload
    agents.value = agentPayload
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载失败'
  } finally {
    loading.value = false
  }
}

// 静默刷新持仓+自选：买入/卖出/删除后调用，不显示 loading 遮罩，避免页面闪烁
async function refreshPositions() {
  if (!currentUser.value) return
  try {
    const [positionPayload, watchlistPayload] = await Promise.all([
      api.positions(),
      api.watchlist()
    ])
    positions.value = positionPayload
    watchlist.value = watchlistPayload
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载失败'
  }
}

async function login(payload: { username: string; password: string }) {
  loginLoading.value = true
  loginError.value = ''
  try {
    const result = await api.login(payload)
    setAuthToken(result.access_token)
    currentUser.value = result.user
    await refreshAll()
  } catch (err) {
    loginError.value = err instanceof Error ? err.message : '登录失败'
  } finally {
    loginLoading.value = false
  }
}

function logout() {
  setAuthToken('')
  currentUser.value = null
  dashboard.value = null
  activePage.value = 'workbench'
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
  if (!selectedPositionModelId.value || !positionModels.value.some((item) => item.id === selectedPositionModelId.value)) {
    selectedPositionModelId.value = positionModels.value.find((item) => item.is_active)?.id || positionModels.value[0]?.id || null
  }
}

async function openPositionAgent(accountId: number | null = null) {
  positionAgentTargetAccount.value = accountId
  positionDrawerOpen.value = true
  positionAgentMessage.value = ''
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
  if (positionAgentBusy.value) return
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

async function onPositionImageSelected(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  positionAgentMimeType.value = file.type || 'image/png'
  positionAgentFileName.value = file.name
  positionAgentImageBase64.value = await readFileAsDataUrl(file)
  if (!positionAgentInstruction.value.trim()) {
    positionAgentInstruction.value = '请识别这张 A 股券商持仓截图，按持仓明细保存。'
  }
}

function clearPositionAgentImage() {
  positionAgentImageBase64.value = ''
  positionAgentFileName.value = ''
  positionAgentMimeType.value = 'image/png'
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
        account_id: positionAgentTargetAccount.value
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
    if (last?.type !== 'thought' || last.content !== content) {
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
    const finalContent = latest.content.trim()
      ? latest.content
      : event.should_save
        ? positionAgentResultMarkdown(positionAgentRows.value, failedRows, positionAgentMessage.value)
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
  if (step.type === 'model') return 'Model'
  if (step.type === 'thought') return 'Thinking'
  if (step.type === 'action') return `Action · ${step.display_name || step.tool || 'tool'}`
  if (step.type === 'observation') return `Observation · ${step.display_name || step.tool || 'tool'}`
  if (step.type === 'error') return `Error · ${step.display_name || step.tool || 'tool'}`
  return step.type
}

function formatNumber(value: number | null | undefined, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-'
  return Number(value).toLocaleString('zh-CN', {
    minimumFractionDigits: 0,
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
  if (page === 'digitalHumans') digitalHumanInitialTab.value = 'chat'
  activePage.value = page
}

function openFeishuBindings() {
  digitalHumanInitialTab.value = 'feishuBots'
  activePage.value = 'digitalHumans'
}

async function seedDemo() {
  await api.seedDemo()
  await refreshAll()
}

onMounted(initialize)
</script>

<template>
  <div v-if="bootstrapping && !currentUser" class="boot-splash">
    <span class="boot-splash-pulse"></span>
    正在连接 SixRabbits Stock...
  </div>

  <LoginView v-else-if="!currentUser" :loading="loginLoading" :error="loginError" @login="login" />

  <AppShell
    v-else
    :active-page="activePage === 'positionChart' ? chartReturnPage : activePage"
    :page-title="pageTitle"
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
        @seed-demo="seedDemo"
      />

      <AskStockView v-else-if="activePage === 'askStock'" />
      <DigitalHumanCenter
        v-else-if="activePage === 'digitalHumans'"
        :initial-tab="digitalHumanInitialTab"
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
        @open-agent="openPositionAgent"
        @open-chart="openChartFromPosition"
      />

      <WatchlistView
        v-else-if="activePage === 'watchlist'"
        :watchlist="watchlist"
        @refresh="refreshAll"
        @open-chart="openChartFromWatchlist"
        @manage-feishu="openFeishuBindings"
      />

      <RhythmView v-else-if="activePage === 'rhythm'" />

      <KnowledgeBaseView v-else-if="activePage === 'knowledge'" />

      <KnowledgeSearchView v-else-if="activePage === 'knowledgeSearch'" />

      <WorkspaceDocsView v-else-if="activePage === 'workspaceDocs'" />

      <SettingsView
        v-else-if="activePage === 'settings'"
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
          <button type="button" :disabled="positionAgentBusy" @click="closePositionAgent">关闭</button>
        </header>

        <section ref="positionChatScroll" class="message-list position-agent-messages">
          <div v-if="!positionAgentChats.length" class="chat-empty">
            <strong>添加持仓</strong>
            <span>试试输入“平安银行 1000 股，成本 10.2，现价 10.8，止损 9.89”，或者上传一张持仓截图。</span>
          </div>

          <article v-for="item in positionAgentChats" :key="item.id" class="message" :class="item.role">
            <template v-if="item.role === 'user'">
              <span>你</span>
              <p>{{ item.content }}</p>
              <small v-if="item.imageName" class="position-image-chip">{{ item.imageName }}</small>
            </template>
            <template v-else>
              <div v-if="item.status" class="stream-status" :class="{ tooling: item.status === 'thinking', done: item.status === 'done', error: item.status === 'error' }">
                <span class="stream-status-pulse"></span>
                <strong>{{ positionStatusText(item) }}</strong>
              </div>
              <div v-if="item.steps?.length" class="react-steps">
                <div v-for="(step, index) in item.steps" :key="index" class="react-step" :class="step.type">
                  <strong>{{ positionStepLabel(step) }}</strong>
                  <span>{{ step.content }}</span>
                </div>
              </div>
              <div v-if="item.content" class="md-body" v-html="renderMarkdown(item.content)"></div>
              <div v-else-if="item.error" class="chat-error">{{ item.error }}</div>
              <div v-else class="chat-typing">正在识别并写入持仓...</div>
            </template>
          </article>
        </section>

        <form class="position-agent-composer" @submit.prevent="agentAddPositions">
          <div v-if="positionAgentFileName" class="position-selected-file">
            <span>{{ positionAgentFileName }}</span>
            <button type="button" :disabled="positionAgentBusy" @click="clearPositionAgentImage">移除</button>
          </div>
          <textarea
            v-model="positionAgentInstruction"
            rows="3"
            placeholder="输入持仓描述，或上传截图后补充账户、日期、券商等信息"
            :disabled="positionAgentBusy"
          ></textarea>
          <div class="position-agent-composer-actions">
            <label class="position-agent-upload">
              <input type="file" accept="image/*" :disabled="positionAgentBusy" @change="onPositionImageSelected" />
              <span>上传图片</span>
            </label>
            <select v-model="positionAgentTargetAccount" :disabled="positionAgentBusy" title="选择这批持仓保存到哪个证券账户">
              <option :value="null">自动识别账户</option>
              <option v-for="account in positionAgentAccounts" :key="account.id" :value="account.id">
                {{ positionAccountLabel(account) }}
              </option>
            </select>
            <select v-model.number="selectedPositionModelId" :disabled="positionAgentBusy || !positionModels.length" :title="modelOptionLabel(activePositionModel)">
              <optgroup v-for="group in positionModelGroups" :key="group.platform" :label="group.platform">
                <option v-for="item in group.rows" :key="item.id" :value="item.id">
                  {{ modelOptionLabel(item) }}
                </option>
              </optgroup>
            </select>
            <button type="submit" :disabled="positionAgentBusy || (!positionAgentInstruction.trim() && !positionAgentImageBase64)">
              {{ positionAgentBusy ? '识别中' : '发送' }}
            </button>
          </div>
          <p v-if="positionAgentMessage && !positionAgentBusy" class="form-message">{{ positionAgentMessage }}</p>
        </form>
      </aside>
    </div>
  </AppShell>
</template>

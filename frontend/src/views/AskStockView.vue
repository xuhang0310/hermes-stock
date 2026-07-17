<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue'
import { api } from '../api'
import type { AgentSession, AgentStreamEvent, ModelConfig, PromptRecommendation, StockStrategy } from '../types'
import { renderMarkdown } from '../utils/markdown'
import { applyToolProgress, type LiveToolProgress } from '../utils/toolProgress'
import { downloadMarkdown, isWorkspaceSaveworthy } from '../utils/workspace'

type ReactStep = {
  type:
    | 'model'
    | 'reasoning'
    | 'thought'
    | 'action'
    | 'observation'
    | 'thinking'
    | 'tool_start'
    | 'tool_done'
    | 'stage_start'
    | 'stage_done'
    | 'generating'
    | 'error'
  content: string
  tool?: string
  displayName?: string
  success?: boolean
  duration?: number
  stage?: string
  status?: string
}

type ChatMessage = {
  id: number
  role: 'user' | 'assistant'
  content: string
  error?: string
  steps: ReactStep[]
  liveReasoning: string
  liveContent: string
  liveTool?: LiveToolProgress | null
  streamStatus?: 'connecting' | 'streaming' | 'tooling' | 'done' | 'error'
  saving?: boolean
  savedPath?: string
}

const quickQuestions = [
  '帮我看看贵州茅台现在能不能买',
  '分析一下 300750 近期趋势和风险',
  '比亚迪最近有什么消息面变化',
  '600519 支撑位和压力位在哪里',
  '我想做短线，帮我筛一下这个票的风险点'
]

const promptRecommendations = ref<PromptRecommendation[]>([])
const fallbackPrompts = ref<string[]>([])
const loadingPrompts = ref(false)

const displayedPrompts = computed(() => {
  const rows: Array<{ id?: number; text: string; reason?: string | null; source: 'dynamic' | 'fallback' }> = []
  const seen = new Set<string>()
  for (const item of promptRecommendations.value) {
    const text = item.prompt_text.trim()
    if (!text || seen.has(text)) continue
    seen.add(text)
    rows.push({ id: item.id, text, reason: item.reason, source: 'dynamic' })
  }
  const fallback = fallbackPrompts.value.length ? fallbackPrompts.value : quickQuestions
  for (const text of fallback) {
    const normalized = text.trim()
    if (!normalized || seen.has(normalized)) continue
    seen.add(normalized)
    rows.push({ text: normalized, source: 'fallback' })
    if (rows.length >= 6) break
  }
  return rows.slice(0, 6)
})

const models = ref<ModelConfig[]>([])
const sessions = ref<AgentSession[]>([])
const strategies = ref<StockStrategy[]>([])
const selectedStrategyIds = ref<string[]>([])
const selectedModelId = ref<number | null>(null)
const selectedSessionId = ref<number | null>(null)
const messages = ref<ChatMessage[]>([])
const question = ref('')
const sending = ref(false)
const abortController = ref<AbortController | null>(null)
const loadingSession = ref(false)
const error = ref('')
const chatScroll = ref<HTMLElement | null>(null)
const maxSelectedStrategies = 3

const activeModel = computed(
  () =>
    models.value.find((item) => item.id === selectedModelId.value) ||
    models.value.find((item) => item.is_active) ||
    models.value[0] ||
    null
)

const selectedStrategyText = computed(() =>
  selectedStrategyIds.value.length ? selectedStrategyIds.value.map(strategyLabel).join(' / ') : '通用分析'
)

const modelGroups = computed(() => {
  const groups = new Map<string, ModelConfig[]>()
  for (const model of models.value) {
    const platform = model.platform_name || model.provider_name || '默认平台'
    groups.set(platform, [...(groups.get(platform) || []), model])
  }
  return Array.from(groups, ([platform, rows]) => ({
    platform,
    rows: [...rows].sort((a, b) => Number(b.is_active) - Number(a.is_active) || a.id - b.id)
  }))
})

async function load() {
  const [modelPayload, strategyPayload, sessionPayload] = await Promise.all([
    api.modelConfigs(),
    api.stockStrategies(),
    api.agentSessions('stock')
  ])
  models.value = modelPayload.filter((item) => item.status !== 'disabled')
  strategies.value = strategyPayload.skills
  sessions.value = sessionPayload
  selectedStrategyIds.value = strategyPayload.default_skill_id
    ? [strategyPayload.default_skill_id]
    : strategies.value[0]
      ? [strategies.value[0].id]
      : []
  selectedModelId.value = models.value.find((item) => item.is_active)?.id || models.value[0]?.id || null
  void loadStockRecommendations()
}

async function refreshSessions() {
  try {
    sessions.value = await api.agentSessions('stock')
  } catch {
    // History refresh should not interrupt the active streaming conversation.
  }
}

async function openSession(id: number) {
  if (sending.value) return
  error.value = ''
  loadingSession.value = true
  try {
    const detail = await api.agentSession(id)
    selectedSessionId.value = detail.id
    selectedModelId.value = detail.model_config_id || selectedModelId.value
    messages.value = detail.messages.map((item) => ({
      id: item.id,
      role: item.role,
      content: item.error || item.content,
      error: item.error || undefined,
      steps: [],
      liveReasoning: '',
      liveContent: '',
      streamStatus: item.role === 'assistant' ? (item.error ? 'error' : 'done') : undefined
    }))
    scrollChat(false)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载历史问股会话失败'
  } finally {
    loadingSession.value = false
  }
}

async function deleteSession(item: AgentSession) {
  if (sending.value) return
  const removedId = item.id
  const wasActive = selectedSessionId.value === removedId
  // 乐观移除：立即从列表隐藏（逻辑删除，服务端仅置为 deleted、记录仍保留在后台）
  sessions.value = sessions.value.filter((session) => session.id !== removedId)
  try {
    await api.agentDeleteSession(removedId)
    if (wasActive) newSession()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '删除会话失败'
    refreshSessions() // 失败则回滚到服务端真实列表
  }
}

async function loadStockRecommendations() {
  loadingPrompts.value = true
  try {
    const payload = await api.stockPromptRecommendations()
    promptRecommendations.value = payload.items
    fallbackPrompts.value = payload.fallback_prompts
  } catch {
    promptRecommendations.value = []
    fallbackPrompts.value = []
  } finally {
    loadingPrompts.value = false
  }
}

async function refreshStockRecommendations() {
  if (sending.value) return
  loadingPrompts.value = true
  try {
    const payload = await api.refreshStockPromptRecommendations()
    promptRecommendations.value = payload.items
    fallbackPrompts.value = payload.fallback_prompts
  } catch (err) {
    error.value = err instanceof Error ? err.message : '刷新推荐失败'
  } finally {
    loadingPrompts.value = false
  }
}

function newSession() {
  if (sending.value) return
  selectedSessionId.value = null
  messages.value = []
  question.value = ''
  error.value = ''
}

function applyQuickQuestion(prompt: string) {
  if (sending.value) return
  question.value = prompt
}

function modelOptionLabel(model: ModelConfig | null) {
  if (!model) return '默认模型'
  return `${model.display_name} / ${model.model_name}`
}

function sessionModelLabel(session: AgentSession) {
  const model = models.value.find((item) => item.id === session.model_config_id)
  return model ? modelOptionLabel(model) : session.model_display_name || '默认模型'
}

function strategyLabel(id: string) {
  return strategies.value.find((item) => item.id === id)?.name || id
}

function toggleStrategy(id: string) {
  if (sending.value) return
  if (selectedStrategyIds.value.includes(id)) {
    selectedStrategyIds.value = selectedStrategyIds.value.filter((item) => item !== id)
    return
  }
  if (selectedStrategyIds.value.length >= maxSelectedStrategies) return
  selectedStrategyIds.value = [...selectedStrategyIds.value, id]
}

function useGeneralAnalysis() {
  if (sending.value) return
  selectedStrategyIds.value = []
}

function stepLabel(step: ReactStep) {
  if (step.type === 'model') return '模型'
  if (step.type === 'reasoning') return 'Reasoning'
  if (step.type === 'thought') return 'Thought'
  if (step.type === 'action') return `Action: ${step.tool || 'tool'}`
  if (step.type === 'observation') return 'Observation'
  if (step.type === 'error') return 'Error'
  return '进度'
}

const progressStepTypes = new Set<ReactStep['type']>([
  'thinking',
  'tool_start',
  'tool_done',
  'stage_start',
  'stage_done',
  'generating'
])

function isProgressEventType(type: string): type is ReactStep['type'] {
  return progressStepTypes.has(type as ReactStep['type'])
}

function isProgressStep(step: ReactStep) {
  return progressStepTypes.has(step.type)
}

function progressSteps(message: ChatMessage) {
  return message.steps.filter(isProgressStep)
}

function detailSteps(message: ChatMessage) {
  return message.steps.filter((step) => !isProgressStep(step) && step.type !== 'action' && step.type !== 'observation')
}

function progressSummary(message: ChatMessage) {
  const tools = message.steps.filter((step) => step.type === 'tool_done')
  const duration = tools.reduce((total, step) => total + (step.duration || 0), 0)
  return `${tools.length} 个工具调用 · ${duration.toFixed(1)}s`
}

function progressStepClass(step: ReactStep) {
  return {
    success: step.type === 'tool_done' && step.success !== false,
    danger: step.type === 'tool_done' && step.success === false,
    generating: step.type === 'generating',
    muted: step.type === 'stage_start' || step.type === 'stage_done'
  }
}

function progressStepText(step: ReactStep) {
  const label = step.displayName || step.tool || step.content
  if (step.type === 'tool_start') return `${label}...`
  if (step.type === 'tool_done') {
    const suffix = typeof step.duration === 'number' ? ` (${step.duration.toFixed(2)}s)` : ''
    return `${label}${suffix}`
  }
  if (step.type === 'stage_start') return step.content || 'Starting agent analysis...'
  if (step.type === 'stage_done') return step.duration ? `分析链路完成 (${step.duration.toFixed(2)}s)` : '分析链路完成'
  if (step.type === 'generating') return step.content || '正在生成最终分析...'
  return step.content
}

function progressEventContent(event: AgentStreamEvent) {
  const label = event.display_name || event.tool || '工具'
  if (event.message || event.content) return event.message || event.content || ''
  if (event.type === 'tool_start') return `${label}...`
  if (event.type === 'tool_done') return label
  if (event.type === 'stage_start') return 'Starting agent analysis...'
  if (event.type === 'stage_done') return '分析链路完成'
  if (event.type === 'generating') return '正在生成最终分析...'
  return '正在制定分析路径...'
}

function pushProgressEvent(reply: ChatMessage, event: AgentStreamEvent) {
  flushReasoning(reply)
  flushThought(reply)
  if (event.type === 'generating') {
    reply.streamStatus = 'streaming'
  } else if (event.type !== 'stage_done' || reply.streamStatus !== 'done') {
    reply.streamStatus = 'tooling'
  }
  pushStep(reply, {
    type: event.type as ReactStep['type'],
    tool: event.tool,
    displayName: event.display_name,
    success: event.success,
    duration: event.duration,
    stage: event.stage,
    status: event.status,
    content: progressEventContent(event)
  })
}

function hasVisibleAssistantWork(message: ChatMessage) {
  return message.steps.length > 0 || Boolean(message.liveReasoning)
}

function hasProgressSteps(message: ChatMessage) {
  return progressSteps(message).length > 0
}

function hasDetailSteps(message: ChatMessage) {
  return detailSteps(message).length > 0
}

function streamStatusText(message: ChatMessage) {
  if (message.streamStatus === 'connecting') return '正在连接问股 Agent'
  if (message.streamStatus === 'streaming') return '正在接收模型分片'
  if (message.streamStatus === 'tooling') return '正在调用行情/搜索工具'
  if (message.streamStatus === 'error') return '问股响应异常'
  if (message.streamStatus === 'done') return '问股响应完成'
  return '等待响应'
}

function scrollChat(smooth = true) {
  nextTick(() => {
    chatScroll.value?.scrollTo({
      top: chatScroll.value.scrollHeight,
      behavior: smooth ? 'smooth' : 'auto'
    })
  })
}

function pushStep(reply: ChatMessage, step: ReactStep) {
  if (!step.content.trim()) return
  reply.steps.push(step)
}

function flushReasoning(reply: ChatMessage) {
  if (reply.liveReasoning.trim()) {
    pushStep(reply, { type: 'reasoning', content: reply.liveReasoning.trim() })
  }
  reply.liveReasoning = ''
}

function questionBefore(item: ChatMessage): string {
  const idx = messages.value.indexOf(item)
  for (let i = idx - 1; i >= 0; i--) {
    if (messages.value[i].role === 'user') return messages.value[i].content
  }
  return ''
}

// 只给研究产物挂"下载/存入工作区"，寒暄闲聊不挂
function canSaveNote(item: ChatMessage): boolean {
  if (item.liveContent) return false
  return isWorkspaceSaveworthy(item.content, item.steps.some((s) => s.type === 'action'))
}

// 直接下载回复为 .md 文件（客户端生成，点开即用编辑器/Word 打开）
function downloadNote(item: ChatMessage) {
  downloadMarkdown(item.content, questionBefore(item))
}

// 回答一键存工作区：前端已有全文，直接落盘，零 token（借鉴 deepresearch"文件按需物化"）
async function saveToWorkspace(item: ChatMessage) {
  if (!item.content || item.saving || item.savedPath) return
  item.saving = true
  try {
    const res = await api.saveWorkspaceNote({ content: item.content, title: questionBefore(item), session_id: selectedSessionId.value })
    item.savedPath = res.path
  } catch (err) {
    error.value = err instanceof Error ? err.message : '存入工作区失败'
  } finally {
    item.saving = false
  }
}

function flushThought(reply: ChatMessage) {
  if (reply.liveContent.trim()) {
    pushStep(reply, { type: 'thought', content: reply.liveContent.trim() })
  }
  reply.liveContent = ''
}

async function send() {
  const text = question.value.trim()
  if (!text || sending.value) return

  const history = messages.value
    .filter((item) => item.content)
    .map((item) => ({ role: item.role, content: item.content }))
    .slice(-8)

  sending.value = true
  error.value = ''
  question.value = ''
  messages.value.push({
    id: Date.now(),
    role: 'user',
    content: text,
    steps: [],
    liveReasoning: '',
    liveContent: ''
  })
  messages.value.push({
    id: Date.now() + 1,
    role: 'assistant',
    content: '',
    steps: [],
    liveReasoning: '',
    liveContent: '',
    streamStatus: 'connecting'
  })
  const reply = messages.value[messages.value.length - 1]
  scrollChat()

  const controller = new AbortController()
  abortController.value = controller
  try {
    await api.streamStockChat(
      {
        question: text,
        history,
        session_id: selectedSessionId.value,
        model_config_id: selectedModelId.value,
        skills: selectedStrategyIds.value
      },
      (event: AgentStreamEvent) => {
        if (event.type === 'session') {
          selectedSessionId.value = event.session_id || selectedSessionId.value
          return
        }
        if (event.type === 'model_selected') {
          if (event.model?.id) selectedModelId.value = event.model.id
          pushStep(reply, {
            type: 'model',
            content: `${event.model?.display_name || '默认模型'} / ${event.model?.model_name || ''}`
          })
        } else if (event.type === 'reasoning_delta') {
          reply.streamStatus = 'streaming'
          reply.liveReasoning += event.content || ''
        } else if (event.type === 'content_delta') {
          reply.streamStatus = 'streaming'
          flushReasoning(reply)
          reply.liveContent += event.content || ''
        } else if (event.type === 'content_end') {
          flushReasoning(reply)
          if (event.role === 'answer') {
            reply.content = reply.liveContent.trim()
            reply.liveContent = ''
            reply.streamStatus = 'done'
          } else {
            flushThought(reply)
            reply.streamStatus = 'tooling'
          }
        } else if (event.type === 'tool_progress') {
          // 模型正在生成工具调用参数（写文件内容等长参数期）：瞬态进度，action 到达即清
          reply.streamStatus = 'tooling'
          reply.liveTool = applyToolProgress(reply.liveTool, event)
        } else if (isProgressEventType(event.type)) {
          pushProgressEvent(reply, event)
        } else if (event.type === 'action') {
          reply.streamStatus = 'tooling'
          reply.liveTool = null
          flushReasoning(reply)
          flushThought(reply)
          pushStep(reply, { type: 'action', tool: event.tool, content: event.content || '' })
        } else if (event.type === 'observation') {
          reply.streamStatus = 'tooling'
          flushReasoning(reply)
          pushStep(reply, { type: 'observation', content: event.content || '' })
        } else if (event.type === 'error') {
          flushReasoning(reply)
          flushThought(reply)
          reply.liveTool = null
          reply.error = event.content || '问股响应失败'
          reply.streamStatus = 'error'
          pushStep(reply, { type: 'error', content: reply.error })
          error.value = reply.error
        } else if (event.type === 'done') {
          reply.liveTool = null
          reply.streamStatus = reply.error ? 'error' : 'done'
        }
        scrollChat()
      },
      controller.signal
    )
    // 流正常结束却没拿到最终答案：服务端干净关闭（重启/断流）时 reader 正常读完而不抛异常，
    // 需明确兜底——有半截答案就保留，否则提示重试，避免消息空白卡住。
    if (!reply.content && !reply.error) {
      if (reply.liveContent.trim()) {
        reply.content = reply.liveContent.trim()
        reply.liveContent = ''
        reply.streamStatus = 'done'
      } else {
        reply.error = '回复未完成，可能是网络中断或服务重启，请重试。'
        reply.streamStatus = 'error'
      }
    }
    nextTick(() => { refreshSessions().catch(() => {}) })
  } catch (err) {
    if (controller.signal.aborted) {
      // 用户主动点了“停止”：保留已经收到的内容，不当作错误
      flushReasoning(reply)
      flushThought(reply)
      reply.liveTool = null
      if (reply.liveContent) {
        reply.content = reply.liveContent.trim()
        reply.liveContent = ''
      }
      reply.streamStatus = 'done'
    } else {
      reply.error = err instanceof Error ? err.message : '发送失败'
      reply.streamStatus = 'error'
      error.value = reply.error
    }
  } finally {
    abortController.value = null
    sending.value = false
    if (!reply.error && reply.streamStatus !== 'done') {
      reply.streamStatus = 'done'
    }
    scrollChat()
  }
}

function stopSending() {
  abortController.value?.abort()
}

onMounted(load)
</script>

<template>
  <section class="stock-chat-page">
    <aside class="session-rail page-section stock-session-rail">
      <header class="section-header">
        <div>
          <h2>历史问股</h2>
          <p>{{ sessions.length }} 条记录</p>
        </div>
        <button type="button" :disabled="sending" @click="newSession">新建</button>
      </header>

      <div class="assistant-switcher stock-strategy-switcher">
        <label>策略</label>
        <div class="stock-skill-list">
          <label class="skill-check" title="不注入特定策略，按行情、技术、消息、风险的通用框架分析。">
            <input
              type="checkbox"
              :checked="selectedStrategyIds.length === 0"
              :disabled="sending"
              @change="useGeneralAnalysis"
            />
            <span>通用分析</span>
          </label>
          <label v-for="item in strategies" :key="item.id" class="skill-check" :title="item.description">
            <input
              type="checkbox"
              :checked="selectedStrategyIds.includes(item.id)"
              :disabled="
                sending ||
                (!selectedStrategyIds.includes(item.id) && selectedStrategyIds.length >= maxSelectedStrategies)
              "
              @change="toggleStrategy(item.id)"
            />
            <span>{{ item.name }}</span>
          </label>
        </div>
      </div>

      <div class="session-list">
        <div
          v-for="session in sessions"
          :key="session.id"
          class="session-item-wrap"
        >
          <button
            type="button"
            class="session-item"
            :class="{ active: selectedSessionId === session.id }"
            :disabled="sending || loadingSession"
            @click="openSession(session.id)"
          >
            <strong>{{ session.title }}</strong>
            <span>{{ sessionModelLabel(session) }}</span>
            <small>{{ session.message_count }} 条 · {{ new Date(session.updated_at).toLocaleString() }}</small>
          </button>
          <button
            class="session-delete"
            type="button"
            title="删除该会话（从列表移除，记录仍保留在后台）"
            :disabled="sending || loadingSession"
            @click.stop="deleteSession(session)"
          >×</button>
        </div>
        <div v-if="!sessions.length" class="session-empty">暂无历史问股</div>
      </div>
    </aside>

    <main class="chat-workspace page-section">
      <section class="persona-strip">
        <div>
          <strong>问股助手</strong>
          <span>TuShare + Tavily Search</span>
        </div>
        <div class="stock-active-strategy">
          <span>当前策略</span>
          <strong>{{ selectedStrategyText }}</strong>
        </div>
        <p>基于行情工具和搜索工具回答，支持多轮追问。缺少数据源 Token 时会直接提示，不会编造数据。</p>
        <div class="quick-prompts">
          <button
            v-for="prompt in displayedPrompts"
            :key="`${prompt.source}-${prompt.id || prompt.text}`"
            type="button"
            :disabled="sending"
            :class="{ dynamic: prompt.source === 'dynamic' }"
            :title="prompt.reason || (prompt.source === 'dynamic' ? '根据历史对话和系统数据推荐' : '问股常用提示词')"
            @click="applyQuickQuestion(prompt.text)"
          >
            {{ prompt.text }}
          </button>
          <button type="button" :disabled="sending || loadingPrompts" @click="refreshStockRecommendations">
            {{ loadingPrompts ? '推荐生成中' : '刷新推荐' }}
          </button>
        </div>
      </section>

      <section ref="chatScroll" class="message-list stock-message-list">
        <div v-if="!messages.length" class="chat-empty">
          <strong>输入股票代码或名称开始问股</strong>
          <span>例：600519、贵州茅台、300750 近期趋势、比亚迪消息面。</span>
        </div>

        <article v-for="item in messages" :key="item.id" class="message" :class="item.role">
          <template v-if="item.role === 'user'">
            <span>你</span>
            <p>{{ item.content }}</p>
          </template>
          <template v-else>
            <div v-if="item.streamStatus" class="stream-status" :class="item.streamStatus">
              <span class="stream-status-pulse"></span>
              <strong>{{ streamStatusText(item) }}</strong>
            </div>

            <div v-if="hasProgressSteps(item)" class="thinking-progress">
              <div class="thinking-progress-summary">
                <span class="thinking-caret">⌄</span>
                <strong>思考过程</strong>
                <span>· {{ progressSummary(item) }}</span>
              </div>
              <div class="thinking-progress-list">
                <div
                  v-for="(step, index) in progressSteps(item)"
                  :key="index"
                  class="chat-progress-item"
                  :class="progressStepClass(step)"
                >
                  <span class="chat-progress-dot"></span>
                  <span>{{ progressStepText(step) }}</span>
                </div>
              </div>
            </div>

            <div v-if="hasDetailSteps(item)" class="react-steps">
              <div v-for="(step, index) in detailSteps(item)" :key="index" class="react-step" :class="step.type">
                <strong>{{ stepLabel(step) }}</strong>
                <span>{{ step.content }}</span>
              </div>
            </div>

            <div v-if="item.liveReasoning" class="react-step reasoning live">
              <strong>Reasoning</strong>
              <span>{{ item.liveReasoning }}</span>
            </div>

            <div v-if="item.liveTool" class="react-step action tool-progress">
              <strong><span class="tool-progress-pulse"></span>正在生成「{{ item.liveTool.displayName }}」· 已 {{ item.liveTool.chars.toLocaleString() }} 字</strong>
              <pre v-if="item.liveTool.preview" class="tool-progress-preview">{{ item.liveTool.preview }}</pre>
            </div>

            <div v-if="item.liveContent" class="md-body" v-html="renderMarkdown(item.liveContent)"></div>
            <div v-else-if="item.content" class="md-body" v-html="renderMarkdown(item.content)"></div>
            <div v-else-if="item.error" class="chat-error">{{ item.error }}</div>
            <div v-else-if="!hasVisibleAssistantWork(item)" class="chat-typing">正在理解股票范围...</div>

            <div v-if="canSaveNote(item)" class="msg-actions">
              <button type="button" class="msg-save-btn" @click="downloadNote(item)">⬇ 下载 .md</button>
              <button
                type="button"
                class="msg-save-btn"
                :class="{ saved: item.savedPath }"
                :disabled="item.saving || !!item.savedPath"
                @click="saveToWorkspace(item)"
              >
                {{ item.savedPath ? `✓ 已存入 ${item.savedPath}` : item.saving ? '保存中…' : '存入工作区' }}
              </button>
            </div>
          </template>
        </article>
      </section>

      <form class="chat-input stock-chat-input" @submit.prevent="send">
        <input
          v-model="question"
          type="text"
          placeholder="输入股票代码/名称和问题，例如：600519 现在怎么看？"
          :disabled="sending"
        />
        <select v-model.number="selectedModelId" :disabled="sending || !models.length" :title="modelOptionLabel(activeModel)">
          <optgroup v-for="group in modelGroups" :key="group.platform" :label="group.platform">
            <option v-for="item in group.rows" :key="item.id" :value="item.id">
              {{ modelOptionLabel(item) }}
            </option>
          </optgroup>
        </select>
        <button type="button" :disabled="sending" @click="newSession">新会话</button>
        <button
          :type="sending ? 'button' : 'submit'"
          :class="{ 'btn-stop': sending }"
          :disabled="!sending && !question.trim()"
          @click="sending ? stopSending() : undefined"
        >
          {{ sending ? '⏹ 停止' : '发送' }}
        </button>
      </form>
      <p v-if="error" class="chat-page-error">{{ error }}</p>
    </main>
  </section>
</template>

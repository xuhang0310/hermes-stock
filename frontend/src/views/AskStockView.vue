<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue'
import { api } from '../api'
import AgentApprovalCard from '../components/AgentApprovalCard.vue'
import AgentProcessDisclosure from '../components/AgentProcessDisclosure.vue'
import ApprovalPolicyControl from '../components/ApprovalPolicyControl.vue'
import ChatAttachmentPicker from '../components/ChatAttachmentPicker.vue'
import SkillSlashMenu from '../components/SkillSlashMenu.vue'
import type {
  AgentAttachment,
  AgentSkill,
  AgentSession,
  AgentStreamEvent,
  AgentToolApproval,
  ApprovalPolicy,
  ModelConfig,
  PromptRecommendation,
  StockStrategy
} from '../types'
import { renderMarkdown } from '../utils/markdown'
import { applyToolProgress, type LiveToolProgress } from '../utils/toolProgress'
import { extractGeneratedFiles, type GeneratedFile } from '../utils/workspace'
import { beijingFromDb } from '../utils/datetime'
import { elapsedStreamMilliseconds, formatStreamDuration } from '../utils/streamDuration'

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
  files: GeneratedFile[]
  attachments: AgentAttachment[]
  approval?: AgentToolApproval | null
  approvalBusy?: boolean
  streamStatus?: 'connecting' | 'streaming' | 'tooling' | 'done' | 'error'
  startedAt?: number
  durationMs?: number
  terminalDurationMs?: number
  streamFinalized?: boolean
  processExpanded?: boolean
  processAutoCollapsed?: boolean
}

type ChatAttachmentPickerHandle = {
  handlePaste: (event: ClipboardEvent) => Promise<boolean>
}

type SkillSlashMenuHandle = {
  sync: (text: string, caret: number) => void
  handleKeydown: (event: KeyboardEvent) => boolean
  close: () => void
}

const props = withDefaults(defineProps<{
  initialQuestion?: string
}>(), {
  initialQuestion: ''
})

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
const promptRefreshFeedback = ref('')

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
const approvalPolicy = ref<ApprovalPolicy>('ask')
const pendingAttachments = ref<AgentAttachment[]>([])
const attachmentPicker = ref<ChatAttachmentPickerHandle | null>(null)
const composerTextarea = ref<HTMLTextAreaElement | null>(null)
const skillSlashMenu = ref<SkillSlashMenuHandle | null>(null)
const selectedCoreSkills = ref<AgentSkill[]>([])
const workspaceExecAvailable = ref(false)
const sending = ref(false)
const abortController = ref<AbortController | null>(null)
const loadingSession = ref(false)
const error = ref('')
const chatScroll = ref<HTMLElement | null>(null)
const mobileRailOpen = ref(false)
function closeMobileRail() { mobileRailOpen.value = false }
const maxSelectedStrategies = 3

function pasteIntoComposer(event: ClipboardEvent) {
  void attachmentPicker.value?.handlePaste(event)
}

function syncSkillSlashMenu() {
  const caret = composerTextarea.value?.selectionStart ?? question.value.length
  skillSlashMenu.value?.sync(question.value, caret)
}

function selectCoreSkill(payload: { skill: AgentSkill; start: number; end: number }) {
  const alreadySelected = selectedCoreSkills.value.some((item) => item.name === payload.skill.name)
  if (!alreadySelected && selectedCoreSkills.value.length >= 8) {
    error.value = '每轮最多选择 8 个技能'
  } else if (!alreadySelected) {
    selectedCoreSkills.value = [...selectedCoreSkills.value, payload.skill]
    error.value = ''
  }
  question.value = `${question.value.slice(0, payload.start)}${question.value.slice(payload.end)}`
  nextTick(() => {
    composerTextarea.value?.focus()
    composerTextarea.value?.setSelectionRange(payload.start, payload.start)
  })
}

function removeCoreSkill(name: string) {
  selectedCoreSkills.value = selectedCoreSkills.value.filter((item) => item.name !== name)
}

const activeModel = computed(
  () =>
    models.value.find((item) => item.id === selectedModelId.value) ||
    models.value.find((item) => item.is_active) ||
    models.value[0] ||
    null
)

const pendingApproval = computed<{ message: ChatMessage; approval: AgentToolApproval } | null>(() => {
  for (let index = messages.value.length - 1; index >= 0; index -= 1) {
    const message = messages.value[index]
    if (message.approval?.status === 'pending') return { message, approval: message.approval }
  }
  return null
})

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
  const [modelPayload, strategyPayload, sessionPayload, workspaceStatus] = await Promise.all([
    api.modelConfigs(),
    api.stockStrategies(),
    api.agentSessions('stock'),
    api.workspaceStatus().catch(() => null)
  ])
  workspaceExecAvailable.value = Boolean(workspaceStatus?.exec_enabled && workspaceStatus.exec_available)
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
  closeMobileRail()
  if (sending.value) return
  selectedCoreSkills.value = []
  skillSlashMenu.value?.close()
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
      files: [],
      attachments: item.attachments || [],
      approval: null,
      streamStatus: item.role === 'assistant' ? (item.error ? 'error' : 'done') : undefined,
      durationMs: item.duration_ms ?? undefined,
      processExpanded: false,
      processAutoCollapsed: true
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
    if (payload.refreshing) {
      void pollStockRecommendations(recommendationSignature(payload.items)).catch(() => {})
    }
  } catch {
    promptRecommendations.value = []
    fallbackPrompts.value = []
  } finally {
    loadingPrompts.value = false
  }
}

function recommendationSignature(items: PromptRecommendation[]) {
  return items.map((item) => `${item.id}:${item.updated_at || ''}`).join('|')
}

async function pollStockRecommendations(previousSignature: string) {
  for (let attempt = 0; attempt < 24; attempt += 1) {
    await new Promise((resolve) => window.setTimeout(resolve, 1500))
    const payload = await api.stockPromptRecommendations()
    fallbackPrompts.value = payload.fallback_prompts
    const signature = recommendationSignature(payload.items)
    if (payload.items.length && signature !== previousSignature) {
      promptRecommendations.value = payload.items
      return true
    }
  }
  return false
}

async function refreshStockRecommendations() {
  if (sending.value) return
  loadingPrompts.value = true
  promptRefreshFeedback.value = ''
  try {
    const previousSignature = recommendationSignature(promptRecommendations.value)
    const payload = await api.refreshStockPromptRecommendations()
    promptRecommendations.value = payload.items
    fallbackPrompts.value = payload.fallback_prompts
    promptRefreshFeedback.value = '正在后台生成推荐...'
    const changed = await pollStockRecommendations(previousSignature)
    promptRefreshFeedback.value = changed ? '推荐已更新' : '内容暂无变化'
    window.setTimeout(() => {
      promptRefreshFeedback.value = ''
    }, 1800)
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
  selectedCoreSkills.value = []
  skillSlashMenu.value?.close()
  pendingAttachments.value = []
  approvalPolicy.value = 'ask'
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

function hasProcessDetails(message: ChatMessage) {
  return hasProgressSteps(message) || hasDetailSteps(message) || Boolean(message.liveReasoning) || Boolean(message.liveTool)
}

function processStepCount(message: ChatMessage) {
  return progressSteps(message).length
    + detailSteps(message).length
    + Number(Boolean(message.liveReasoning))
    + Number(Boolean(message.liveTool))
}

function streamStatusText(message: ChatMessage) {
  if (message.streamStatus === 'connecting') return '正在连接问股 Agent'
  if (message.streamStatus === 'streaming') return '正在接收模型分片'
  if (message.streamStatus === 'tooling') return '正在调用行情/搜索工具'
  if (message.streamStatus === 'error') return '问股响应异常'
  if (message.streamStatus === 'done') return '问股响应完成'
  return '等待响应'
}

function finishStreamDuration(message: ChatMessage, durationMs?: number) {
  if (typeof durationMs === 'number' && Number.isFinite(durationMs)) {
    message.durationMs = Math.max(0, Math.round(durationMs))
    return
  }
  if (typeof message.durationMs === 'number') return
  message.durationMs = elapsedStreamMilliseconds(message.startedAt)
}

function finalizeStream(message: ChatMessage, outcome: 'done' | 'aborted' | 'error') {
  finishStreamDuration(message, message.terminalDurationMs)
  if (message.streamFinalized) return
  message.streamFinalized = true
  message.streamStatus = outcome === 'error' ? 'error' : 'done'
  if (outcome === 'error') return
  if (!message.processAutoCollapsed) {
    message.processExpanded = false
    message.processAutoCollapsed = true
  }
}

function releaseComposer(controller: AbortController) {
  if (abortController.value !== controller) return
  abortController.value = null
  sending.value = false
}

function handleRunEvent(reply: ChatMessage, event: AgentStreamEvent) {
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
  } else if (event.type === 'retrying') {
    flushReasoning(reply)
    flushThought(reply)
    reply.liveTool = null
    if (event.reset_stream) {
      reply.liveReasoning = ''
      reply.liveContent = ''
    }
    reply.streamStatus = 'connecting'
    pushStep(reply, {
      type: 'thinking',
      content: event.message || `第 ${event.attempt || 2} 次尝试正在启动`
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
    } else {
      flushThought(reply)
      reply.streamStatus = 'tooling'
    }
  } else if (event.type === 'tool_progress') {
    reply.streamStatus = 'tooling'
    reply.liveTool = applyToolProgress(reply.liveTool, event)
  } else if (event.type === 'approval_required' && event.approval_id) {
    reply.streamStatus = 'tooling'
    reply.liveTool = null
    reply.approval = {
      approval_id: event.approval_id,
      tool: event.tool || '',
      display_name: event.display_name || event.tool || '工具操作',
      risk_level: event.risk_level || 'high',
      arguments_preview: event.arguments_preview || '',
      expires_at: event.expires_at || '',
      status: 'pending'
    }
  } else if (event.type === 'approval_resolved') {
    const approval = reply.approval
    if (approval && event.approval_id && approval.approval_id === event.approval_id) {
      approval.status = event.decision || approval.status
      reply.approvalBusy = false
    }
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
  } else if (event.type === 'artifact') {
    const path = event.path?.trim()
    if (path && !reply.files.some((file) => file.path === path)) {
      reply.files.push({ path, name: event.name || path.split('/').pop() || path, bytes: event.bytes })
    }
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
    reply.terminalDurationMs = event.duration_ms
    reply.streamStatus = reply.error ? 'error' : 'done'
  }
  scrollChat()
}

let scrollQueued = false

function scrollChat(smooth = false) {
  if (scrollQueued) return
  scrollQueued = true
  requestAnimationFrame(() => {
    scrollQueued = false
    nextTick(() => {
      chatScroll.value?.scrollTo({
        top: chatScroll.value.scrollHeight,
        behavior: smooth ? 'smooth' : 'auto'
      })
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

// 本次回复真正生成的工作区文件（有 ws_write_file 动作才有），下载的是真实文件
function generatedFiles(item: ChatMessage): GeneratedFile[] {
  const rows = [...extractGeneratedFiles(item.steps), ...item.files]
  return rows.filter((file, index) => rows.findIndex((candidate) => candidate.path === file.path) === index)
}

async function downloadFile(file: GeneratedFile) {
  try {
    await api.downloadWorkspaceDoc(file.path, file.name)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '下载失败'
  }
}

function flushThought(reply: ChatMessage) {
  if (reply.liveContent.trim()) {
    pushStep(reply, { type: 'thought', content: reply.liveContent.trim() })
  }
  reply.liveContent = ''
}

async function send() {
  closeMobileRail()
  const enteredText = question.value.trim()
  const sentAttachments = pendingAttachments.value.map(({ preview_url: _preview, ...item }) => item)
  if ((!enteredText && !sentAttachments.length) || sending.value) return
  const text = enteredText || '请分析我上传的附件。'
  const selectedSkillNames = selectedCoreSkills.value.map((item) => item.name)

  const history = messages.value
    .filter((item) => item.content)
    .map((item) => ({ role: item.role, content: item.content }))
    .slice(-8)
  sending.value = true
  error.value = ''
  question.value = ''
  selectedCoreSkills.value = []
  skillSlashMenu.value?.close()
  pendingAttachments.value = []
  messages.value.push({
    id: Date.now(),
    role: 'user',
    content: text,
    steps: [],
    liveReasoning: '',
    liveContent: '',
    files: [],
    attachments: sentAttachments
  })
  messages.value.push({
    id: Date.now() + 1,
    role: 'assistant',
    content: '',
    steps: [],
    liveReasoning: '',
    liveContent: '',
    files: [],
    attachments: [],
    approval: null,
    streamStatus: 'connecting',
    startedAt: Date.now(),
    processExpanded: true,
    processAutoCollapsed: false
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
        skills: selectedStrategyIds.value,
        selected_skill_names: selectedSkillNames,
        attachments: sentAttachments.map((item) => ({ path: item.path })),
        approval_policy: approvalPolicy.value
      },
      (event: AgentStreamEvent) => handleRunEvent(reply, event),
      controller.signal
    )
    // 收尾：优先保住已产出内容（有半截答案就转正）；只有从头到尾什么都没收到才提示重试，
    // 避免把"已完整回答但没收到最后一帧 content_end"误判成失败（并发/网络抖动会触发）。
    if (!reply.content && reply.liveContent.trim()) {
      reply.content = reply.liveContent.trim()
      reply.liveContent = ''
    }
    if (!reply.error) {
      const gotNothing = !reply.content && !reply.liveContent.trim() && !hasVisibleAssistantWork(reply)
      if (gotNothing) {
        reply.error = '回复未完成，请重试。'
      }
    }
    nextTick(() => { refreshSessions().catch(() => {}) })
  } catch (err) {
    if (controller.signal.aborted) {
      // 用户主动点了“停止”：保留已经收到的内容，不当作错误
      flushReasoning(reply)
      reply.liveTool = null
      if (reply.liveContent) {
        reply.content = reply.liveContent.trim()
        reply.liveContent = ''
      }
    } else {
      reply.error = err instanceof Error ? err.message : '发送失败'
      error.value = reply.error
    }
  } finally {
    finalizeStream(
      reply,
      reply.error ? 'error' : controller.signal.aborted ? 'aborted' : 'done'
    )
    releaseComposer(controller)
    scrollChat()
  }
}

async function recoverActiveRun() {
  if (sending.value) return
  try {
    const [run] = await api.activeAgentRuns({ chat_mode: 'stock' })
    if (!run) return

    await openSession(run.session_id)
    if (sending.value) return

    const reply: ChatMessage = {
      id: -Date.now(),
      role: 'assistant',
      content: '',
      steps: [],
      liveReasoning: '',
      liveContent: '',
      files: [],
      attachments: [],
      approval: null,
      streamStatus: 'connecting',
      startedAt: Date.now(),
      processExpanded: true,
      processAutoCollapsed: false
    }
    messages.value.push(reply)
    sending.value = true
    error.value = ''
    scrollChat()

    const controller = new AbortController()
    abortController.value = controller
    try {
      await api.resumeAgentRun(
        run.run_id,
        (event: AgentStreamEvent) => handleRunEvent(reply, event),
        controller.signal
      )
      if (!reply.content && reply.liveContent.trim()) {
        reply.content = reply.liveContent.trim()
        reply.liveContent = ''
      }
      nextTick(() => { refreshSessions().catch(() => {}) })
    } catch (err) {
      if (controller.signal.aborted) {
        flushReasoning(reply)
        reply.liveTool = null
        if (reply.liveContent) {
          reply.content = reply.liveContent.trim()
          reply.liveContent = ''
        }
      } else {
        reply.error = err instanceof Error ? err.message : '恢复问股任务失败'
        error.value = reply.error
      }
    } finally {
      finalizeStream(
        reply,
        reply.error ? 'error' : controller.signal.aborted ? 'aborted' : 'done'
      )
      releaseComposer(controller)
      scrollChat()
    }
  } catch {
    // Runtime recovery is best-effort and must not block the chat page.
  }
}

function stopSending() {
  abortController.value?.abort()
}

async function resolveApproval(message: ChatMessage, decision: 'approved' | 'denied') {
  if (!message.approval || message.approval.status !== 'pending' || message.approvalBusy) return
  message.approvalBusy = true
  error.value = ''
  try {
    const result = await api.resolveAgentApproval(message.approval.approval_id, decision)
    if (['approved', 'denied', 'expired', 'cancelled'].includes(result.status)) {
      message.approval.status = result.status as AgentToolApproval['status']
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : '审批操作失败'
  } finally {
    message.approvalBusy = false
  }
}

function resolvePendingApproval(decision: 'approved' | 'denied') {
  const pending = pendingApproval.value
  if (pending) void resolveApproval(pending.message, decision)
}

function submitFromKeyboard(event: KeyboardEvent) {
  if (event.isComposing || event.shiftKey) return
  event.preventDefault()
  void send()
}

function composerKeydown(event: KeyboardEvent) {
  if (skillSlashMenu.value?.handleKeydown(event)) return
  if (event.key === 'Enter') submitFromKeyboard(event)
}

onMounted(async () => {
  if (props.initialQuestion.trim()) question.value = props.initialQuestion.trim()
  await load()
  await recoverActiveRun()
})
</script>

<template>
  <section class="stock-chat-page">
    <aside class="session-rail page-section stock-session-rail" :class="{ 'rail-mobile-visible': mobileRailOpen }">
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
            <small>{{ session.message_count }} 条 · {{ beijingFromDb(session.updated_at) }}</small>
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
      <button type="button" class="chat-rail-toggle" :aria-expanded="mobileRailOpen" aria-label="切换会话列表" @click="mobileRailOpen = !mobileRailOpen">≡</button>
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
            {{ loadingPrompts ? '推荐生成中' : (promptRefreshFeedback || '刷新推荐') }}
          </button>
        </div>
      </section>

      <section ref="chatScroll" class="message-list stock-message-list">
        <div v-if="pendingApproval" class="pending-approval-dock">
          <div class="pending-approval-dock-label">
            <strong>操作等待审批</strong>
            <span>Agent 已暂停，确认后将继续执行</span>
          </div>
          <AgentApprovalCard
            :approval="pendingApproval.approval"
            :busy="pendingApproval.message.approvalBusy"
            prominent
            @decide="resolvePendingApproval"
          />
        </div>

        <div v-if="!messages.length" class="chat-empty">
          <strong>输入股票代码或名称开始问股</strong>
          <span>例：600519、贵州茅台、300750 近期趋势、比亚迪消息面。</span>
        </div>

        <article v-for="item in messages" :key="item.id" class="message" :class="item.role">
          <template v-if="item.role === 'user'">
            <span>你</span>
            <p>{{ item.content }}</p>
            <div v-if="item.attachments.length" class="message-attachment-list">
              <button v-for="file in item.attachments" :key="file.path" type="button" @click="downloadFile(file)">
                <b>{{ file.kind === 'image' ? '图' : '文' }}</b><span>{{ file.name }}</span>
              </button>
            </div>
          </template>
          <template v-else>
            <div v-if="item.streamStatus" class="stream-status" :class="item.streamStatus">
              <span class="stream-status-pulse"></span>
              <strong>{{ streamStatusText(item) }}</strong>
              <span v-if="item.streamStatus === 'done' && typeof item.durationMs === 'number'" class="stream-duration">
                用时 {{ formatStreamDuration(item.durationMs) }}
              </span>
            </div>

            <AgentApprovalCard
              v-if="item.approval && item.approval.status !== 'pending'"
              :approval="item.approval"
              :busy="item.approvalBusy"
              @decide="(decision) => resolveApproval(item, decision)"
            />

            <AgentProcessDisclosure
              v-if="hasProcessDetails(item)"
              :completed="item.streamStatus === 'done'"
              :expanded="Boolean(item.processExpanded)"
              :step-count="processStepCount(item)"
              @toggle="item.processExpanded = !item.processExpanded"
            >
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
            </AgentProcessDisclosure>

            <div v-if="item.liveContent" class="md-body" v-html="renderMarkdown(item.liveContent)"></div>
            <div v-else-if="item.content" class="md-body" v-html="renderMarkdown(item.content)"></div>
            <div v-else-if="item.error" class="chat-error">{{ item.error }}</div>
            <div v-else-if="!hasVisibleAssistantWork(item)" class="chat-typing">正在理解股票范围...</div>

            <div v-if="!item.liveContent && generatedFiles(item).length" class="msg-actions">
              <button
                v-for="file in generatedFiles(item)"
                :key="file.path"
                type="button"
                class="msg-save-btn"
                @click="downloadFile(file)"
              >
                ⬇ 下载 {{ file.name }}
              </button>
            </div>
          </template>
        </article>
      </section>

      <form class="chat-composer stock-chat-composer" @paste="pasteIntoComposer" @submit.prevent="send">
        <textarea
          ref="composerTextarea"
          v-model="question"
          rows="2"
          placeholder="输入股票代码、问题，或上传研报、表格和图片"
          :disabled="sending"
          @input="syncSkillSlashMenu"
          @click="syncSkillSlashMenu"
          @keydown="composerKeydown"
        ></textarea>
        <SkillSlashMenu
          ref="skillSlashMenu"
          chat-mode="stock"
          :selected-skills="selectedCoreSkills"
          :disabled="sending"
          @select="selectCoreSkill"
          @remove="removeCoreSkill"
        />
        <footer class="chat-composer-toolbar">
          <ChatAttachmentPicker ref="attachmentPicker" v-model="pendingAttachments" :disabled="sending" />
          <ApprovalPolicyControl v-model="approvalPolicy" :disabled="sending" :exec-available="workspaceExecAvailable" />
          <span class="chat-composer-spacer"></span>
          <select v-model.number="selectedModelId" :disabled="sending || !models.length" :title="modelOptionLabel(activeModel)">
            <optgroup v-for="group in modelGroups" :key="group.platform" :label="group.platform">
              <option v-for="item in group.rows" :key="item.id" :value="item.id">{{ modelOptionLabel(item) }}</option>
            </optgroup>
          </select>
          <button class="chat-composer-command" type="button" :disabled="sending" @click="newSession">新会话</button>
          <button
            class="chat-composer-send"
            :class="{ stop: sending }"
            :type="sending ? 'button' : 'submit'"
            :title="sending ? '停止生成' : '发送'"
            :disabled="!sending && !question.trim() && !pendingAttachments.length"
            @click="sending ? stopSending() : undefined"
          >{{ sending ? '■' : '↑' }}</button>
        </footer>
      </form>
      <p v-if="error" class="chat-page-error">{{ error }}</p>
    </main>

    <div v-if="mobileRailOpen" class="rail-mobile-overlay" @click="closeMobileRail"></div>
  </section>
</template>

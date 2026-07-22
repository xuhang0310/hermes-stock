<script setup lang="ts">
import { computed, nextTick, onMounted, ref, shallowReactive } from 'vue'
import { api } from '../api'
import AgentApprovalCard from '../components/AgentApprovalCard.vue'
import AgentProcessDisclosure from '../components/AgentProcessDisclosure.vue'
import ApprovalPolicyControl from '../components/ApprovalPolicyControl.vue'
import ChatAttachmentPicker from '../components/ChatAttachmentPicker.vue'
import FeishuBotPanel from './FeishuBotPanel.vue'
import type {
  AgentAttachment,
  AgentMessage,
  AgentSession,
  AgentSessionDetail,
  AgentStreamEvent,
  AgentToolApproval,
  ApprovalPolicy,
  DigitalHuman,
  ModelConfig,
  PromptRecommendation
} from '../types'
import { renderMarkdown } from '../utils/markdown'
import { applyToolProgress, type LiveToolProgress } from '../utils/toolProgress'
import { extractGeneratedFiles, type GeneratedFile } from '../utils/workspace'
import { elapsedStreamMilliseconds, formatStreamDuration } from '../utils/streamDuration'

const props = withDefaults(defineProps<{
  initialTab?: 'chat' | 'feishuBots'
}>(), {
  initialTab: 'chat'
})

const activeCenterTab = ref<'chat' | 'feishuBots'>(props.initialTab)

type ReactStep = {
  type: 'model' | 'reasoning' | 'thought' | 'action' | 'observation' | 'team' | 'error'
  content: string
  tool?: string
  displayName?: string
}

type ChatMessage = {
  id: number
  role: 'user' | 'assistant'
  content: string
  error?: string
  created_at?: string
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
  processExpanded?: boolean
  processAutoCollapsed?: boolean
}

type ChatAttachmentPickerHandle = {
  handlePaste: (event: ClipboardEvent) => Promise<boolean>
}

type ActiveChatRequest = {
  key: string
  sessionId: number | null
  profileId: number | null
  modelId: number | null
  controller: AbortController
  messages: ChatMessage[]
}

let draftSequence = 0

function createDraftKey() {
  draftSequence += 1
  return `draft:${Date.now()}:${draftSequence}`
}

function sessionKey(sessionId: number) {
  return `session:${sessionId}`
}

const profiles = ref<DigitalHuman[]>([])
const models = ref<ModelConfig[]>([])
const sessions = ref<AgentSession[]>([])
const promptRecommendations = ref<PromptRecommendation[]>([])
const fallbackPrompts = ref<string[]>([])
const messages = ref<ChatMessage[]>([])
const question = ref('')
const approvalPolicy = ref<ApprovalPolicy>('ask')
const pendingAttachments = ref<AgentAttachment[]>([])
const attachmentPicker = ref<ChatAttachmentPickerHandle | null>(null)
const workspaceExecAvailable = ref(false)
const selectedProfileId = ref<number | null>(null)
const selectedModelId = ref<number | null>(null)
const selectedSessionId = ref<number | null>(null)
const selectedSessionSourceType = ref('web')
const selectedSessionSourceLabel = ref('网页')
const selectedSessionSourceIdentifier = ref<string | null>(null)
const selectedSessionReadOnly = ref(false)
const selectedDraftKey = ref(createDraftKey())
const activeRequests = shallowReactive(new Map<string, ActiveChatRequest>())
const loadingSessions = ref(false)
const loadingSession = ref(false)
const loadingPrompts = ref(false)
const error = ref('')
const chatScroll = ref<HTMLElement | null>(null)

function pasteIntoComposer(event: ClipboardEvent) {
  if (selectedSessionReadOnly.value) return
  void attachmentPicker.value?.handlePaste(event)
}

const selectedProfile = computed(() => profiles.value.find((item) => item.id === selectedProfileId.value) || null)
const selectedConversationKey = computed(() =>
  selectedSessionId.value ? sessionKey(selectedSessionId.value) : selectedDraftKey.value
)
const selectedSessionRunning = computed(() => activeRequests.has(selectedConversationKey.value))
const filteredSessions = computed(() => {
  const profileId = selectedProfileId.value
  if (!profileId) return sessions.value
  return sessions.value.filter((item) => item.digital_human_id === profileId)
})
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

const displayedPrompts = computed(() => {
  const rows: Array<{ id?: number; text: string; reason?: string | null; source: 'dynamic' | 'fallback' }> = []
  const seen = new Set<string>()
  for (const item of promptRecommendations.value) {
    const text = item.prompt_text.trim()
    if (!text || seen.has(text)) continue
    seen.add(text)
    rows.push({ id: item.id, text, reason: item.reason, source: 'dynamic' })
  }
  const fallback = fallbackPrompts.value.length ? fallbackPrompts.value : selectedProfile.value?.quick_prompts || []
  for (const text of fallback) {
    const normalized = text.trim()
    if (!normalized || seen.has(normalized)) continue
    seen.add(normalized)
    rows.push({ text: normalized, source: 'fallback' })
    if (rows.length >= 6) break
  }
  return rows.slice(0, 6)
})

async function load() {
  const [profilePayload, modelPayload, workspaceStatus] = await Promise.all([
    api.digitalHumans(),
    api.modelConfigs(),
    api.workspaceStatus().catch(() => null)
  ])
  workspaceExecAvailable.value = Boolean(workspaceStatus?.exec_enabled && workspaceStatus.exec_available)
  profiles.value = profilePayload
  models.value = modelPayload.filter((item) => item.status !== 'disabled')
  if (!selectedProfileId.value && profiles.value[0]) {
    selectedProfileId.value = profiles.value[0].id
  }
  if (!selectedModelId.value || !models.value.some((item) => item.id === selectedModelId.value)) {
    selectedModelId.value = models.value.find((item) => item.is_active)?.id || models.value[0]?.id || null
  }
  const profileId = selectedProfileId.value
  await Promise.all([
    refreshSessions(profileId),
    profileId ? loadPromptRecommendations(profileId) : Promise.resolve()
  ])
}

async function refreshSessions(profileId: number | null = selectedProfileId.value) {
  const requestedProfileId = profileId ?? null
  loadingSessions.value = true
  try {
    const payload = await api.agentSessions('research', requestedProfileId)
    if ((selectedProfileId.value ?? null) === requestedProfileId) {
      sessions.value = payload
    }
  } catch {
    // 历史会话刷新失败不影响当前流式会话。
  } finally {
    if ((selectedProfileId.value ?? null) === requestedProfileId) {
      loadingSessions.value = false
    }
  }
}

function toChatMessages(items: AgentMessage[]): ChatMessage[] {
  return items.map((item) => ({
    id: item.id,
    role: item.role,
    content: item.error || item.content,
    error: item.error || undefined,
    created_at: item.created_at,
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
}

function selectSessionDetail(detail: AgentSessionDetail, sessionMessages: ChatMessage[]) {
  selectedSessionId.value = detail.id
  selectedProfileId.value = detail.digital_human_id || selectedProfileId.value
  selectedModelId.value = detail.model_config_id || selectedModelId.value
  selectedSessionSourceType.value = detail.source_type || 'web'
  selectedSessionSourceLabel.value = detail.source_label || '网页'
  selectedSessionSourceIdentifier.value = detail.source_identifier || null
  selectedSessionReadOnly.value = Boolean(detail.read_only)
  messages.value = sessionMessages
}

async function openSession(id: number) {
  error.value = ''
  const activeRequest = activeRequests.get(sessionKey(id))
  if (activeRequest) {
    selectedSessionId.value = id
    selectedProfileId.value = activeRequest.profileId || selectedProfileId.value
    selectedModelId.value = activeRequest.modelId || selectedModelId.value
    selectedSessionSourceType.value = 'web'
    selectedSessionSourceLabel.value = '网页'
    selectedSessionSourceIdentifier.value = null
    selectedSessionReadOnly.value = false
    messages.value = activeRequest.messages
    if (selectedProfileId.value) await loadPromptRecommendations(selectedProfileId.value)
    scrollChat(false)
    return
  }
  loadingSession.value = true
  try {
    const detail = await api.agentSession(id)
    selectSessionDetail(detail, toChatMessages(detail.messages))
    if (selectedProfileId.value) {
      await loadPromptRecommendations(selectedProfileId.value)
    }
    scrollChat(false)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载历史会话失败'
  } finally {
    loadingSession.value = false
  }
}

function newSession(profileId?: number | null) {
  selectedSessionId.value = null
  selectedDraftKey.value = createDraftKey()
  selectedSessionSourceType.value = 'web'
  selectedSessionSourceLabel.value = '网页'
  selectedSessionSourceIdentifier.value = null
  selectedSessionReadOnly.value = false
  selectedProfileId.value = profileId ?? selectedProfileId.value ?? profiles.value[0]?.id ?? null
  messages.value = []
  question.value = ''
  pendingAttachments.value = []
  approvalPolicy.value = 'ask'
  error.value = ''
  if (selectedProfileId.value) {
    loadPromptRecommendations(selectedProfileId.value)
  }
  refreshSessions(selectedProfileId.value)
}

async function deleteSession(item: AgentSession) {
  if (sessionIsRunning(item.id)) return
  const removedId = item.id
  const wasActive = selectedSessionId.value === removedId
  // 乐观移除：立即从列表隐藏（逻辑删除，服务端仅置为 deleted、记录仍保留）
  sessions.value = sessions.value.filter((session) => session.id !== removedId)
  try {
    await api.agentDeleteSession(removedId)
    if (wasActive) newSession(selectedProfileId.value)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '删除会话失败'
    refreshSessions(selectedProfileId.value) // 失败则回滚到服务端真实列表
  }
}

function applyQuickPrompt(prompt: string) {
  if (selectedSessionRunning.value) return
  question.value = prompt
}

async function loadPromptRecommendations(profileId: number) {
  loadingPrompts.value = true
  try {
    const payload = await api.promptRecommendations(profileId)
    if (selectedProfileId.value !== profileId) return
    promptRecommendations.value = payload.items
    fallbackPrompts.value = payload.fallback_prompts
  } catch {
    if (selectedProfileId.value === profileId) {
      promptRecommendations.value = []
      fallbackPrompts.value = selectedProfile.value?.quick_prompts || []
    }
  } finally {
    if (selectedProfileId.value === profileId) {
      loadingPrompts.value = false
    }
  }
}

async function refreshPromptRecommendations() {
  if (!selectedProfileId.value || selectedSessionRunning.value) return
  loadingPrompts.value = true
  try {
    const payload = await api.refreshPromptRecommendations(selectedProfileId.value)
    promptRecommendations.value = payload.items
    fallbackPrompts.value = payload.fallback_prompts
  } catch (err) {
    error.value = err instanceof Error ? err.message : '刷新推荐失败'
  } finally {
    loadingPrompts.value = false
  }
}

async function dismissPrompt(item: { id?: number }) {
  if (!item.id || selectedSessionRunning.value) return
  promptRecommendations.value = promptRecommendations.value.filter((row) => row.id !== item.id)
  try {
    await api.dismissPromptRecommendation(item.id)
  } catch {
    // 关闭失败不影响当前会话，下一次刷新会恢复服务端状态。
  }
}

function modelOptionLabel(model: ModelConfig | null) {
  if (!model) return '默认模型'
  return `${model.display_name} / ${model.model_name}`
}

function sessionModelLabel(session: AgentSession) {
  const model = models.value.find((item) => item.id === session.model_config_id)
  return model ? modelOptionLabel(model) : session.model_display_name || '默认模型'
}

function sessionSourceDescription(session: AgentSession) {
  if (session.source_type === 'web') return sessionModelLabel(session)
  return [session.source_label || session.source_type, session.source_identifier].filter(Boolean).join(' · ')
}

function stepLabel(step: ReactStep) {
  if (step.type === 'model') return '模型'
  if (step.type === 'reasoning') return 'Reasoning'
  if (step.type === 'thought') return 'Thought'
  if (step.type === 'action') return `Action: ${step.displayName || step.tool || 'tool'}`
  if (step.type === 'observation') return 'Observation'
  if (step.type === 'team') return 'Agent Team'
  return 'Error'
}

function teamEventText(event: AgentStreamEvent) {
  if (event.type === 'team_created') return `已创建协作组，${event.member_count || 0} 个数字人开始并行执行`
  if (event.type === 'team_member_queued') return `${event.agent_name || event.agent_code || '数字人'} 已进入执行队列`
  if (event.type === 'team_member_done') return `${event.agent_name || event.agent_code || '数字人'}：${event.status || '完成'}`
  if (event.type === 'team_done') return event.summary || `协作完成 ${event.completed || 0}/${event.total || 0}`
  return event.summary || '子任务结果已回流，总调度继续统稿'
}

function streamStatusText(message: ChatMessage) {
  if (message.streamStatus === 'connecting') return 'Stream 连接中'
  if (message.streamStatus === 'streaming') return '正在接收模型分片'
  if (message.streamStatus === 'tooling') return '正在执行 ReAct 工具'
  if (message.streamStatus === 'error') return '流式响应异常'
  if (message.streamStatus === 'done') return '流式响应完成'
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

function completeStream(message: ChatMessage, durationMs?: number) {
  finishStreamDuration(message, durationMs)
  message.streamStatus = 'done'
  if (!message.processAutoCollapsed) {
    message.processExpanded = false
    message.processAutoCollapsed = true
  }
}

function hasProcessDetails(message: ChatMessage) {
  return message.steps.length > 0 || Boolean(message.liveReasoning) || Boolean(message.liveTool)
}

function processStepCount(message: ChatMessage) {
  return message.steps.length + Number(Boolean(message.liveReasoning)) + Number(Boolean(message.liveTool))
}

function releaseRequest(request: ActiveChatRequest) {
  if (activeRequests.get(request.key) === request) {
    activeRequests.delete(request.key)
  }
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

function flushThought(reply: ChatMessage) {
  if (reply.liveContent.trim()) {
    pushStep(reply, { type: 'thought', content: reply.liveContent.trim() })
  }
  reply.liveContent = ''
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

function handleRunEvent(reply: ChatMessage, request: ActiveChatRequest, event: AgentStreamEvent) {
  if (event.type === 'session') {
    if (event.profile?.id) request.profileId = event.profile.id
    if (event.model?.id) request.modelId = event.model.id
    if (event.session_id) {
      const previousKey = request.key
      request.sessionId = event.session_id
      request.key = sessionKey(event.session_id)
      activeRequests.delete(previousKey)
      activeRequests.set(request.key, request)
    }
    if (requestIsVisible(request)) {
      selectedSessionId.value = request.sessionId || selectedSessionId.value
      selectedSessionSourceType.value = 'web'
      selectedSessionSourceLabel.value = '网页'
      selectedSessionSourceIdentifier.value = null
      selectedSessionReadOnly.value = false
      if (request.profileId) selectedProfileId.value = request.profileId
      if (request.modelId) selectedModelId.value = request.modelId
    }
    void refreshSessions(request.profileId)
    return
  }
  if (event.type === 'model_selected') {
    if (event.model?.id) {
      request.modelId = event.model.id
      if (requestIsVisible(request)) selectedModelId.value = event.model.id
    }
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
      type: 'reasoning',
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
      completeStream(reply)
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
  } else if (event.type.startsWith('team_')) {
    reply.streamStatus = 'tooling'
    reply.liveTool = null
    flushReasoning(reply)
    flushThought(reply)
    pushStep(reply, { type: 'team', content: teamEventText(event) })
  } else if (event.type === 'action') {
    reply.streamStatus = 'tooling'
    reply.liveTool = null
    flushReasoning(reply)
    flushThought(reply)
    pushStep(reply, { type: 'action', tool: event.tool, displayName: event.display_name, content: event.content || '' })
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
    reply.error = event.content || 'ReAct 响应失败'
    reply.streamStatus = 'error'
    finishStreamDuration(reply)
    pushStep(reply, { type: 'error', content: reply.error })
    if (requestIsVisible(request)) error.value = reply.error
  } else if (event.type === 'done') {
    reply.liveTool = null
    if (reply.error) {
      reply.streamStatus = 'error'
      finishStreamDuration(reply, event.duration_ms)
    } else {
      completeStream(reply, event.duration_ms)
    }
    releaseRequest(request)
  }
  if (requestIsVisible(request)) scrollChat()
}

async function send() {
  if (selectedSessionReadOnly.value) {
    error.value = `${selectedSessionSourceLabel.value}会话为只读。`
    return
  }
  const enteredText = question.value.trim()
  const sentAttachments = pendingAttachments.value.map(({ preview_url: _preview, ...item }) => item)
  const requestKey = selectedConversationKey.value
  if ((!enteredText && !sentAttachments.length) || activeRequests.has(requestKey)) return
  const text = enteredText || '请分析我上传的附件。'
  const requestSessionId = selectedSessionId.value
  const requestProfileId = selectedProfileId.value
  const requestModelId = selectedModelId.value
  const requestMessages = messages.value

  const history = requestMessages
    .filter((item) => item.content)
    .map((item) => ({ role: item.role, content: item.content }))
    .slice(-8)
  error.value = ''
  question.value = ''
  pendingAttachments.value = []
  requestMessages.push({
    id: Date.now(),
    role: 'user',
    content: text,
    created_at: new Date().toISOString(),
    steps: [],
    liveReasoning: '',
    liveContent: '',
    files: [],
    attachments: sentAttachments
  })
  requestMessages.push({
    id: Date.now() + 1,
    role: 'assistant',
    content: '',
    created_at: new Date().toISOString(),
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
  const reply = requestMessages[requestMessages.length - 1]
  scrollChat()

  const controller = new AbortController()
  const request: ActiveChatRequest = {
    key: requestKey,
    sessionId: requestSessionId,
    profileId: requestProfileId,
    modelId: requestModelId,
    controller,
    messages: requestMessages
  }
  activeRequests.set(request.key, request)
  try {
    await api.streamAgentChat(
      {
        question: text,
        history,
        session_id: requestSessionId,
        digital_human_id: requestProfileId,
        model_config_id: requestModelId,
        attachments: sentAttachments.map((item) => ({ path: item.path })),
        approval_policy: approvalPolicy.value
      },
      (event: AgentStreamEvent) => handleRunEvent(reply, request, event),
      controller.signal
    )
    // 收尾：优先保住已产出内容（有半截答案就转正）；只有从头到尾什么都没收到才提示重试，
    // 避免把"已完整回答但没收到最后一帧 content_end"误判成失败（并发/网络抖动会触发）。
    if (!reply.content && reply.liveContent.trim()) {
      reply.content = reply.liveContent.trim()
      reply.liveContent = ''
    }
    if (!reply.error) {
      const gotNothing = !reply.content && !reply.liveContent.trim() && !reply.steps.length && !reply.liveReasoning
      if (gotNothing) {
        reply.error = '回复未完成，请重试。'
        reply.streamStatus = 'error'
      } else {
        completeStream(reply)
      }
    }
    nextTick(() => {
      refreshSessions(request.profileId).catch(() => {})
      if (requestIsVisible(request) && request.profileId) {
        loadPromptRecommendations(request.profileId).catch(() => {})
      }
    })
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
      completeStream(reply)
    } else {
      reply.error = err instanceof Error ? err.message : '发送失败'
      reply.streamStatus = 'error'
      finishStreamDuration(reply)
      if (requestIsVisible(request)) error.value = reply.error
    }
  } finally {
    releaseRequest(request)
    if (!reply.error) completeStream(reply)
    else finishStreamDuration(reply)
    if (requestIsVisible(request)) scrollChat()
  }
}

async function resumeRecoveredRun(runId: string, request: ActiveChatRequest, reply: ChatMessage) {
  const controller = request.controller
  try {
    await api.resumeAgentRun(
      runId,
      (event: AgentStreamEvent) => handleRunEvent(reply, request, event),
      controller.signal
    )
    if (!reply.content && reply.liveContent.trim()) {
      reply.content = reply.liveContent.trim()
      reply.liveContent = ''
    }
    if (!reply.error) completeStream(reply)
  } catch (err) {
    if (controller.signal.aborted) {
      flushReasoning(reply)
      flushThought(reply)
      reply.liveTool = null
      completeStream(reply)
    } else {
      reply.error = err instanceof Error ? err.message : '恢复数字人任务失败'
      reply.streamStatus = 'error'
      finishStreamDuration(reply)
      if (requestIsVisible(request)) error.value = reply.error
    }
  } finally {
    releaseRequest(request)
    if (!reply.error) completeStream(reply)
    if (requestIsVisible(request)) scrollChat()
    nextTick(() => {
      refreshSessions(request.profileId).catch(() => {})
    })
  }
}

async function recoverActiveRuns() {
  try {
    const runs = (await api.activeAgentRuns({ chat_mode: 'research' })).filter(
      (run) => !run.source_type || run.source_type === 'web'
    )
    let selectedRecovered = false
    for (const run of runs) {
      const key = sessionKey(run.session_id)
      if (activeRequests.has(key)) continue
      const detail = await api.agentSession(run.session_id)
      if (detail.read_only) continue
      const requestMessages = toChatMessages(detail.messages)
      const reply: ChatMessage = {
        id: -Date.now() - run.session_id,
        role: 'assistant',
        content: '',
        created_at: new Date().toISOString(),
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
      requestMessages.push(reply)
      const request: ActiveChatRequest = {
        key,
        sessionId: run.session_id,
        profileId: detail.digital_human_id,
        modelId: detail.model_config_id,
        controller: new AbortController(),
        messages: requestMessages
      }
      activeRequests.set(key, request)
      if (!selectedRecovered) {
        selectSessionDetail(detail, requestMessages)
        selectedRecovered = true
        error.value = ''
        scrollChat()
      }
      void resumeRecoveredRun(run.run_id, request, reply)
    }
  } catch {
    // Runtime recovery is best-effort and must not block the chat page.
  }
}

function stopSending() {
  activeRequests.get(selectedConversationKey.value)?.controller.abort()
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

onMounted(async () => {
  await load()
  await recoverActiveRuns()
})

function sessionIsRunning(sessionId: number) {
  return activeRequests.has(sessionKey(sessionId))
}

function requestIsVisible(request: ActiveChatRequest) {
  return messages.value === request.messages
}
</script>

<template>
  <div class="digital-center-page" :class="{ 'digital-center-page--chat': activeCenterTab === 'chat' }">
  <header class="settings-tabs page-section digital-center-tabs">
    <button type="button" :class="{ active: activeCenterTab === 'chat' }" @click="activeCenterTab = 'chat'">
      数字人对话
    </button>
    <button type="button" :class="{ active: activeCenterTab === 'feishuBots' }" @click="activeCenterTab = 'feishuBots'">
      飞书机器人
    </button>
  </header>

  <FeishuBotPanel v-if="activeCenterTab === 'feishuBots'" />

  <section v-else class="digital-center">
    <aside class="session-rail page-section">
      <header class="section-header">
        <div>
          <h2>历史会话</h2>
          <p>{{ filteredSessions.length }} 条记录，切换后恢复上下文。</p>
        </div>
        <button type="button" @click="newSession()">新建</button>
      </header>

      <div class="assistant-switcher">
        <label>当前数字人</label>
        <select v-model.number="selectedProfileId" @change="newSession(selectedProfileId)">
          <option v-for="item in profiles" :key="item.id" :value="item.id">{{ item.name }}</option>
        </select>
        <p>{{ selectedProfile?.description || '暂无可用数字人，请先配置画像。' }}</p>
      </div>

      <div class="session-list">
        <div
          v-for="item in filteredSessions"
          :key="item.id"
          class="session-item-wrap"
        >
          <button
            class="session-item"
            :class="{ active: selectedSessionId === item.id }"
            type="button"
            @click="openSession(item.id)"
          >
            <div class="session-item-heading">
              <strong>{{ item.title }}</strong>
              <em v-if="item.is_child_task" class="session-source-badge child-task">子任务</em>
              <em v-else-if="item.source_type !== 'web'" class="session-source-badge">{{ item.source_label || item.source_type }}</em>
              <em v-if="sessionIsRunning(item.id)" class="session-running-badge">执行中</em>
            </div>
            <span>{{ item.digital_human_name || '数字人' }} · {{ sessionSourceDescription(item) }}</span>
            <small>{{ item.message_count }} 条 · {{ item.status }}</small>
          </button>
          <button
            class="session-delete"
            type="button"
            title="删除该会话（从列表移除，记录仍保留在后台）"
            :disabled="sessionIsRunning(item.id) || loadingSession"
            @click.stop="deleteSession(item)"
          >×</button>
        </div>
        <div v-if="!filteredSessions.length" class="session-empty">
          {{ loadingSessions || loadingSession ? '正在加载历史会话' : '当前数字人暂无历史会话' }}
        </div>
      </div>
    </aside>

    <main class="chat-workspace page-section">
      <section v-if="selectedProfile" class="persona-strip">
        <div>
          <strong>{{ selectedProfile.name }}</strong>
          <span>{{ selectedProfile.avatar_tone }}</span>
        </div>
        <p>{{ selectedProfile.description }}</p>
        <div class="quick-prompts">
          <button
            v-for="prompt in displayedPrompts"
            :key="`${prompt.source}-${prompt.id || prompt.text}`"
            type="button"
            :disabled="selectedSessionRunning"
            :class="{ dynamic: prompt.source === 'dynamic' }"
            :title="prompt.reason || (prompt.source === 'dynamic' ? '根据历史对话和系统数据推荐' : '该数字人的固定提示词')"
            @click="applyQuickPrompt(prompt.text)"
          >
            {{ prompt.text }}
          </button>
          <button type="button" :disabled="selectedSessionRunning || loadingPrompts || !selectedProfileId" @click="refreshPromptRecommendations">
            {{ loadingPrompts ? '推荐生成中' : '刷新推荐' }}
          </button>
        </div>
      </section>

      <section ref="chatScroll" class="message-list">
        <div v-if="selectedSessionReadOnly" class="external-session-notice">
          <b aria-hidden="true">{{ selectedSessionSourceType === 'delegation' ? '子' : selectedSessionSourceType === 'feishu' ? '飞' : '时' }}</b>
          <div>
            <strong>{{ selectedSessionSourceLabel }} · {{ selectedSessionSourceIdentifier || selectedSessionSourceType }}</strong>
            <span v-if="selectedSessionSourceType === 'delegation'">该会话是总调度派发的子任务，第一条消息展示完整派发提示词，执行记录和结果在这里只读保留。</span>
            <span v-else>该记录来自外部渠道并在这里只读展示。</span>
          </div>
        </div>
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
          <strong>{{ selectedProfile?.name || 'A股研究数字人' }}</strong>
          <span>选择数字人和模型后开始会话，ReAct 的 Thought / Action / Observation 会在这里随回答一起出现。</span>
        </div>

        <article v-for="item in messages" :key="item.id" class="message" :class="item.role">
          <template v-if="item.role === 'user'">
            <span>{{ selectedSessionSourceType === 'delegation' ? '派发指令' : selectedSessionReadOnly ? selectedSessionSourceLabel : '你' }}</span>
            <div
              v-if="selectedSessionSourceType === 'delegation'"
              class="md-body child-task-prompt"
              v-html="renderMarkdown(item.content)"
            ></div>
            <p v-else>{{ item.content }}</p>
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
              <div v-if="item.steps.length" class="react-steps">
                <div v-for="(step, index) in item.steps" :key="index" class="react-step" :class="step.type">
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
            <div v-else-if="!item.steps.length && !item.liveReasoning" class="chat-typing">正在思考与查询...</div>

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

      <div v-if="selectedSessionReadOnly" class="chat-composer chat-composer-readonly">
        <strong>{{ selectedSessionSourceLabel }}会话为只读</strong>
        <span v-if="selectedSessionSourceType === 'feishu'">请在 {{ selectedSessionSourceIdentifier || '对应飞书会话' }} 中继续发送消息。</span>
        <span v-else-if="selectedSessionSourceType === 'delegation'">这是总调度派发的独立子任务，可查看完整指令和结果，但不能在子任务中继续追加对话。</span>
        <span v-else>该会话由内部调度器触发，不能在这里直接追加消息。</span>
      </div>
      <form v-else class="chat-composer" @paste="pasteIntoComposer" @submit.prevent="send">
        <textarea
          v-model="question"
          rows="2"
          placeholder="输入问题，或上传研报、表格和图片"
          :disabled="selectedSessionRunning || !selectedProfile"
          @keydown.enter="submitFromKeyboard"
        ></textarea>
        <footer class="chat-composer-toolbar">
          <ChatAttachmentPicker ref="attachmentPicker" v-model="pendingAttachments" :disabled="selectedSessionRunning || !selectedProfile" />
          <ApprovalPolicyControl v-model="approvalPolicy" :disabled="selectedSessionRunning" :exec-available="workspaceExecAvailable" />
          <span class="chat-composer-spacer"></span>
          <select v-model.number="selectedModelId" :disabled="selectedSessionRunning || !models.length" :title="modelOptionLabel(activeModel)">
            <optgroup v-for="group in modelGroups" :key="group.platform" :label="group.platform">
              <option v-for="item in group.rows" :key="item.id" :value="item.id">{{ modelOptionLabel(item) }}</option>
            </optgroup>
          </select>
          <button
            class="chat-composer-send"
            :class="{ stop: selectedSessionRunning }"
            :type="selectedSessionRunning ? 'button' : 'submit'"
            :title="selectedSessionRunning ? '停止当前会话' : '发送'"
            :disabled="!selectedSessionRunning && ((!question.trim() && !pendingAttachments.length) || !selectedProfile)"
            @click="selectedSessionRunning ? stopSending() : undefined"
          >{{ selectedSessionRunning ? '■' : '↑' }}</button>
        </footer>
      </form>
      <p v-if="error" class="chat-page-error">{{ error }}</p>
    </main>
  </section>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue'
import { api } from '../api'
import type { AgentSession, AgentStreamEvent, ModelConfig } from '../types'
import { renderMarkdown } from '../utils/markdown'
import { beijingFromDb } from '../utils/datetime'

type ReactStep = {
  type: 'model' | 'reasoning' | 'thought' | 'action' | 'observation' | 'error'
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
  streamStatus?: 'connecting' | 'streaming' | 'tooling' | 'done' | 'error'
}

const knowledgePrompts = [
  {
    label: '政策跟踪',
    title: '新能源政策有哪些新变化？',
    prompt: '最近有哪些关于新能源的政策公告？请按时间排序并标注来源。'
  },
  {
    label: '研报观点',
    title: '汇总芯片半导体研报',
    prompt: '搜索关于芯片半导体的研报观点，归纳共识、分歧与核心依据。'
  },
  {
    label: '行业研究',
    title: '医药行业最新研究',
    prompt: '知识库里关于医药行业的最新研究报告有哪些？请提取核心结论。'
  },
  {
    label: '持仓风险',
    title: '检查持仓标的风险提示',
    prompt: '帮我在上传文档里找出与持仓标的相关的风险提示，按标的分组列出。'
  },
  {
    label: '主题检索',
    title: '检索 AI 相关的内外部报告',
    prompt: '检索所有与 AI 相关的内部报告和外部研报，按文档类型与时间分类。'
  },
  {
    label: '文档盘点',
    title: '摘要最近一个月的文档',
    prompt: '最近一个月上传了哪些文档？请按时间列出并概括主要内容。'
  }
]

const models = ref<ModelConfig[]>([])
const sessions = ref<AgentSession[]>([])
const selectedModelId = ref<number | null>(null)
const selectedSessionId = ref<number | null>(null)
const messages = ref<ChatMessage[]>([])
const question = ref('')
const sending = ref(false)
const abortController = ref<AbortController | null>(null)
const loadingSession = ref(false)
const error = ref('')
const chatScroll = ref<HTMLElement | null>(null)
const sessionSearch = ref('')
const mobileRailOpen = ref(false)

const activeModel = computed(
  () =>
    models.value.find((item) => item.id === selectedModelId.value) ||
    models.value.find((item) => item.is_active) ||
    models.value[0] ||
    null
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

const filteredSessions = computed(() => {
  const keyword = sessionSearch.value.trim().toLocaleLowerCase()
  if (!keyword) return sessions.value
  return sessions.value.filter((session) => {
    const haystack = `${session.title} ${sessionModelLabel(session)}`.toLocaleLowerCase()
    return haystack.includes(keyword)
  })
})

async function load() {
  const modelPayload = await api.modelConfigs()
  models.value = modelPayload.filter((item) => item.status !== 'disabled')
  if (!selectedModelId.value || !models.value.some((item) => item.id === selectedModelId.value)) {
    selectedModelId.value = models.value.find((item) => item.is_active)?.id || models.value[0]?.id || null
  }
  await refreshSessions()
}

async function refreshSessions() {
  try {
    sessions.value = await api.agentSessions('knowledge')
  } catch {
    // 历史会话刷新失败不影响当前流式会话。
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
      created_at: item.created_at,
      steps: [],
      liveReasoning: '',
      liveContent: '',
      streamStatus: item.role === 'assistant' ? (item.error ? 'error' : 'done') : undefined
    }))
    mobileRailOpen.value = false
    scrollChat(false)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载历史会话失败'
  } finally {
    loadingSession.value = false
  }
}

async function deleteSession(item: AgentSession) {
  if (sending.value) return
  const removedId = item.id
  const wasActive = selectedSessionId.value === removedId
  sessions.value = sessions.value.filter((session) => session.id !== removedId)
  try {
    await api.agentDeleteSession(removedId)
    if (wasActive) newSession()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '删除会话失败'
    refreshSessions()
  }
}

function newSession() {
  if (sending.value) return
  selectedSessionId.value = null
  messages.value = []
  question.value = ''
  error.value = ''
  mobileRailOpen.value = false
}

function applyQuickPrompt(prompt: string) {
  if (sending.value) return
  question.value = prompt
}

function handleComposerKeydown(event: KeyboardEvent) {
  if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return
  event.preventDefault()
  send()
}

function modelOptionLabel(model: ModelConfig | null) {
  if (!model) return '默认模型'
  return `${model.display_name} / ${model.model_name}`
}

function sessionModelLabel(session: AgentSession) {
  const model = models.value.find((item) => item.id === session.model_config_id)
  return model ? modelOptionLabel(model) : session.model_display_name || '默认模型'
}

function stepLabel(step: ReactStep) {
  if (step.type === 'model') return '模型'
  if (step.type === 'reasoning') return 'Reasoning'
  if (step.type === 'thought') return 'Thought'
  if (step.type === 'action') return `Action: ${step.displayName || step.tool || 'tool'}`
  if (step.type === 'observation') return 'Observation'
  return 'Error'
}

function streamStatusText(message: ChatMessage) {
  if (message.streamStatus === 'connecting') return 'Stream 连接中'
  if (message.streamStatus === 'streaming') return '正在接收模型分片'
  if (message.streamStatus === 'tooling') return '正在执行检索工具'
  if (message.streamStatus === 'error') return '流式响应异常'
  if (message.streamStatus === 'done') return '流式响应完成'
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

function flushThought(reply: ChatMessage) {
  if (reply.liveContent.trim()) {
    pushStep(reply, { type: 'thought', content: reply.liveContent.trim() })
  }
  reply.liveContent = ''
}

function releaseComposer(controller: AbortController) {
  if (abortController.value !== controller) return
  abortController.value = null
  sending.value = false
}

function handleRunEvent(reply: ChatMessage, controller: AbortController, event: AgentStreamEvent) {
  if (event.type === 'session') {
    selectedSessionId.value = event.session_id || selectedSessionId.value
    if (event.model?.id) selectedModelId.value = event.model.id
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
      reply.streamStatus = 'done'
    } else {
      flushThought(reply)
      reply.streamStatus = 'tooling'
    }
  } else if (event.type === 'action') {
    reply.streamStatus = 'tooling'
    flushReasoning(reply)
    flushThought(reply)
    pushStep(reply, { type: 'action', tool: event.tool, displayName: event.display_name, content: event.content || '' })
  } else if (event.type === 'observation') {
    reply.streamStatus = 'tooling'
    flushReasoning(reply)
    pushStep(reply, { type: 'observation', content: event.content || '' })
  } else if (event.type === 'error') {
    flushReasoning(reply)
    flushThought(reply)
    reply.error = event.content || '检索 Agent 响应失败'
    reply.streamStatus = 'error'
    pushStep(reply, { type: 'error', content: reply.error })
    error.value = reply.error
  } else if (event.type === 'done') {
    reply.streamStatus = reply.error ? 'error' : 'done'
    releaseComposer(controller)
  }
  scrollChat()
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
    created_at: new Date().toISOString(),
    steps: [],
    liveReasoning: '',
    liveContent: ''
  })
  messages.value.push({
    id: Date.now() + 1,
    role: 'assistant',
    content: '',
    created_at: new Date().toISOString(),
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
    await api.streamAgentChat(
      {
        question: text,
        history,
        session_id: selectedSessionId.value,
        model_config_id: selectedModelId.value,
        chat_mode: 'knowledge'
      },
      (event: AgentStreamEvent) => handleRunEvent(reply, controller, event),
      controller.signal
    )
    if (!reply.content && !reply.error && !reply.steps.length) {
      reply.error = '未收到最终回答，请重试。'
      reply.streamStatus = 'error'
    }
    nextTick(() => { refreshSessions().catch(() => {}) })
  } catch (err) {
    if (controller.signal.aborted) {
      flushReasoning(reply)
      flushThought(reply)
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
    releaseComposer(controller)
    if (!reply.error && reply.streamStatus !== 'done') {
      reply.streamStatus = 'done'
    }
    scrollChat()
  }
}

async function recoverActiveRun() {
  if (sending.value) return
  try {
    const [run] = await api.activeAgentRuns({ chat_mode: 'knowledge' })
    if (!run) return

    await openSession(run.session_id)
    if (sending.value) return

    const reply: ChatMessage = {
      id: -Date.now(),
      role: 'assistant',
      content: '',
      created_at: new Date().toISOString(),
      steps: [],
      liveReasoning: '',
      liveContent: '',
      streamStatus: 'connecting'
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
        (event: AgentStreamEvent) => handleRunEvent(reply, controller, event),
        controller.signal
      )
      if (!reply.content && reply.liveContent.trim()) {
        reply.content = reply.liveContent.trim()
        reply.liveContent = ''
      }
      if (!reply.error) reply.streamStatus = 'done'
      nextTick(() => { refreshSessions().catch(() => {}) })
    } catch (err) {
      if (controller.signal.aborted) {
        flushReasoning(reply)
        if (reply.liveContent.trim()) {
          reply.content = reply.liveContent.trim()
          reply.liveContent = ''
        }
        reply.streamStatus = 'done'
      } else {
        reply.error = err instanceof Error ? err.message : '恢复检索任务失败'
        reply.streamStatus = 'error'
        error.value = reply.error
      }
    } finally {
      releaseComposer(controller)
      if (!reply.error) reply.streamStatus = 'done'
      scrollChat()
    }
  } catch {
    // Runtime recovery is best-effort and must not block the chat page.
  }
}

function stopSending() {
  abortController.value?.abort()
}

onMounted(async () => {
  await load()
  await recoverActiveRun()
})
</script>

<template>
  <div class="knowledge-center-page">
    <div class="knowledge-center">
      <button
        v-if="mobileRailOpen"
        type="button"
        class="rail-mobile-overlay"
        aria-label="关闭检索历史"
        @click="mobileRailOpen = false"
      ></button>

      <aside class="session-rail knowledge-session-rail page-section" :class="{ 'rail-mobile-visible': mobileRailOpen }">
        <header class="knowledge-rail-header">
          <div>
            <span class="knowledge-eyebrow">会话工作区</span>
            <h2>检索历史 <small>{{ sessions.length }}</small></h2>
          </div>
          <button class="knowledge-new-button" type="button" :disabled="sending" title="新建会话" @click="newSession">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14" /></svg>
          </button>
        </header>

        <div class="knowledge-rail-tools">
          <label class="knowledge-search-box">
            <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="6.5" /><path d="m16 16 4 4" /></svg>
            <input v-model="sessionSearch" type="search" placeholder="搜索历史会话" />
          </label>

          <label class="model-switcher">
            <span>当前模型</span>
            <select v-model.number="selectedModelId" :disabled="sending || !models.length" :title="modelOptionLabel(activeModel)">
              <optgroup v-for="group in modelGroups" :key="group.platform" :label="group.platform">
                <option v-for="item in group.rows" :key="item.id" :value="item.id">
                  {{ modelOptionLabel(item) }}
                </option>
              </optgroup>
            </select>
          </label>
        </div>

        <div class="session-list knowledge-session-list">
          <div v-for="session in filteredSessions" :key="session.id" class="session-item-wrap">
            <button
              type="button"
              class="session-item knowledge-session-item"
              :class="{ active: selectedSessionId === session.id }"
              :disabled="sending || loadingSession"
              @click="openSession(session.id)"
            >
              <span class="knowledge-session-icon">
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 4h8l3 3v13H7z" /><path d="M15 4v4h4M10 12h5M10 16h5" /></svg>
              </span>
              <span class="knowledge-session-copy">
                <strong>{{ session.title }}</strong>
                <small>{{ session.message_count }} 条消息 · {{ beijingFromDb(session.updated_at) }}</small>
                <em>{{ sessionModelLabel(session) }}</em>
              </span>
            </button>
            <button
              class="session-delete"
              type="button"
              title="删除该会话（从列表移除，记录仍保留在后台）"
              :disabled="sending || loadingSession"
              @click.stop="deleteSession(session)"
            >×</button>
          </div>
          <div v-if="!filteredSessions.length" class="session-empty">
            {{ sessions.length ? '没有匹配的会话' : '暂无检索历史' }}
          </div>
        </div>
      </aside>

      <main class="chat-workspace knowledge-workspace page-section">
        <button
          type="button"
          class="chat-rail-toggle knowledge-rail-toggle"
          aria-label="打开检索历史"
          @click="mobileRailOpen = true"
        >
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h10" /></svg>
        </button>
        <header class="knowledge-workspace-header">
          <div class="knowledge-agent-mark">
            <span class="knowledge-agent-icon">
              <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="10.5" cy="10.5" r="5.5" /><path d="m15 15 4.5 4.5M8 9h5M8 12h3" /></svg>
            </span>
            <div>
              <span class="knowledge-eyebrow">KNOWLEDGE RETRIEVAL</span>
              <h1>知识库检索 Agent</h1>
            </div>
          </div>
          <div class="knowledge-pipeline" aria-label="检索流程">
            <span>向量 + 关键词</span><i></i><span>RRF 融合</span><i></i><span>重排精排</span>
          </div>
        </header>

        <section ref="chatScroll" class="message-list knowledge-message-list">
          <div v-if="!messages.length" class="knowledge-empty-state">
            <div class="knowledge-empty-copy">
              <span class="knowledge-empty-icon">
                <svg viewBox="0 0 32 32" aria-hidden="true"><path d="M7 5h13l5 5v17H7z" /><path d="M20 5v6h6M11 16h10M11 21h7" /></svg>
              </span>
              <div>
                <span class="knowledge-eyebrow">从已索引文档中找答案</span>
                <h2>你想从知识库里查什么？</h2>
                <p>可直接提问，也可在问题中指定标的、文档类型、时间范围或可信度。回答将结合检索片段给出依据。</p>
              </div>
            </div>

            <div class="knowledge-prompt-grid">
              <button
                v-for="(item, index) in knowledgePrompts"
                :key="index"
                type="button"
                :disabled="sending"
                @click="applyQuickPrompt(item.prompt)"
              >
                <span>{{ item.label }}</span>
                <strong>{{ item.title }}</strong>
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14M14 7l5 5-5 5" /></svg>
              </button>
            </div>

            <div class="knowledge-capabilities">
              <span><b></b>仅检索已索引文档</span>
              <span>symbol 标的过滤</span>
              <span>doc_types 类型过滤</span>
              <span>trust_levels 可信度过滤</span>
            </div>
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

              <div v-if="item.liveContent" class="md-body" v-html="renderMarkdown(item.liveContent)"></div>
              <div v-else-if="item.content" class="md-body" v-html="renderMarkdown(item.content)"></div>
              <div v-else-if="item.error" class="chat-error">{{ item.error }}</div>
              <div v-else-if="!item.steps.length && !item.liveReasoning" class="chat-typing">正在检索知识库...</div>
            </template>
          </article>
        </section>

        <div class="knowledge-composer-wrap">
          <form class="knowledge-composer" @submit.prevent="send">
            <textarea
              v-model="question"
              rows="1"
              placeholder="输入问题，例如：找出近 30 天新能源政策的主要变化和来源……"
              :disabled="sending"
              @keydown="handleComposerKeydown"
            ></textarea>
            <div class="knowledge-composer-footer">
              <div>
                <span><kbd>Enter</kbd> 发送</span>
                <span><kbd>Shift</kbd> + <kbd>Enter</kbd> 换行</span>
                <span class="knowledge-model-hint">{{ activeModel ? activeModel.display_name : '默认模型' }}</span>
              </div>
              <button
                :type="sending ? 'button' : 'submit'"
                class="knowledge-send-button"
                :class="{ 'btn-stop': sending }"
                :disabled="!sending && !question.trim()"
                @click="sending ? stopSending() : undefined"
              >
                <svg v-if="!sending" viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 14-7-4 14-3-6z" /><path d="m12 13 7-8" /></svg>
                <svg v-else viewBox="0 0 24 24" aria-hidden="true"><rect x="7" y="7" width="10" height="10" rx="1" /></svg>
                {{ sending ? '停止' : '发送' }}
              </button>
            </div>
          </form>
          <p v-if="error" class="chat-page-error">{{ error }}</p>
        </div>
      </main>
    </div>
  </div>
</template>

<style scoped>
.knowledge-center-page {
  --knowledge-panel: #101214;
  --knowledge-panel-strong: #141719;
  --knowledge-line: rgba(213, 180, 106, 0.16);
  --knowledge-line-strong: rgba(213, 180, 106, 0.32);
  --knowledge-muted: #8f958f;
  min-height: 0;
}

.knowledge-center {
  width: 100%;
  max-width: 1920px;
  margin: 0 auto;
  grid-template-columns: 286px minmax(0, 1fr);
  gap: 12px;
}

.knowledge-session-rail,
.knowledge-workspace {
  border-color: var(--knowledge-line);
  border-radius: 10px;
  background: var(--knowledge-panel);
  box-shadow: 0 18px 50px rgba(0, 0, 0, 0.16);
}

.knowledge-session-rail {
  grid-template-rows: auto auto minmax(0, 1fr);
}

.knowledge-rail-header,
.knowledge-workspace-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.knowledge-rail-header {
  min-height: 76px;
  padding: 15px 16px 13px;
  border-bottom: 1px solid var(--knowledge-line);
}

.knowledge-eyebrow {
  display: block;
  color: #a9935f;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.14em;
  line-height: 1.4;
  text-transform: uppercase;
}

.knowledge-rail-header h2 {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 4px 0 0;
  color: #eee9dc;
  font-family: "Noto Serif SC", "Songti SC", serif;
  font-size: 17px;
  font-weight: 700;
}

.knowledge-rail-header h2 small {
  display: grid;
  min-width: 22px;
  height: 20px;
  place-items: center;
  border: 1px solid rgba(213, 180, 106, 0.22);
  border-radius: 999px;
  background: rgba(213, 180, 106, 0.07);
  color: #b8aa85;
  padding: 0 6px;
  font-family: inherit;
  font-size: 10px;
}

.knowledge-new-button {
  display: grid;
  width: 34px;
  height: 34px;
  place-items: center;
  border-color: var(--knowledge-line-strong) !important;
  border-radius: 50% !important;
  background: rgba(213, 180, 106, 0.08) !important;
  padding: 0 !important;
}

.knowledge-new-button svg,
.knowledge-search-box svg,
.knowledge-session-icon svg,
.knowledge-agent-icon svg,
.knowledge-empty-icon svg,
.knowledge-prompt-grid svg,
.knowledge-send-button svg,
.knowledge-rail-toggle svg {
  fill: none;
  stroke: currentColor;
  stroke-linecap: round;
  stroke-linejoin: round;
  stroke-width: 1.7;
}

.knowledge-new-button svg {
  width: 17px;
  height: 17px;
}

.knowledge-rail-tools {
  display: grid;
  gap: 10px;
  padding: 13px 14px 14px;
  border-bottom: 1px solid var(--knowledge-line);
  background: rgba(255, 255, 255, 0.012);
}

.knowledge-search-box {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  align-items: center;
  gap: 8px;
  height: 36px;
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 7px;
  background: #0a0c0d;
  padding: 0 10px;
  transition: border-color 0.16s ease, background 0.16s ease;
}

.knowledge-search-box:focus-within {
  border-color: rgba(213, 180, 106, 0.5);
  background: #0d0f10;
}

.knowledge-search-box svg {
  width: 15px;
  height: 15px;
  color: #777d79;
}

.knowledge-search-box input {
  width: 100%;
  min-width: 0;
  border: 0;
  outline: 0;
  background: transparent;
  color: #dedbd2;
  font-size: 12px;
}

.knowledge-search-box input::placeholder {
  color: #606562;
}

.model-switcher {
  display: grid;
  gap: 6px;
}

.model-switcher > span {
  color: #878c88;
  font-size: 11px;
}

.model-switcher select {
  width: 100%;
  min-width: 0;
  height: 36px;
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 7px;
  outline: 0;
  background: #0a0c0d;
  color: #d8d4c9;
  padding: 0 9px;
  font-size: 12px;
}

.model-switcher select:focus {
  border-color: rgba(213, 180, 106, 0.5);
}

.knowledge-session-list {
  gap: 6px;
  padding: 10px;
}

.knowledge-session-item {
  display: grid;
  min-height: 84px;
  grid-template-columns: 30px minmax(0, 1fr);
  gap: 9px;
  border: 1px solid transparent !important;
  border-radius: 8px !important;
  background: transparent !important;
  padding: 11px 30px 10px 10px !important;
}

.knowledge-session-item:hover {
  border-color: rgba(213, 180, 106, 0.18) !important;
  background: rgba(213, 180, 106, 0.045) !important;
}

.knowledge-session-item.active {
  border-color: rgba(213, 180, 106, 0.28) !important;
  background: rgba(213, 180, 106, 0.085) !important;
  box-shadow: inset 2px 0 0 #d5b46a;
}

.knowledge-session-icon {
  display: grid !important;
  width: 30px !important;
  height: 30px;
  place-items: center;
  border: 1px solid rgba(213, 180, 106, 0.2);
  border-radius: 7px;
  background: rgba(213, 180, 106, 0.055);
  color: #bca564 !important;
}

.knowledge-session-icon svg {
  width: 15px;
  height: 15px;
}

.knowledge-session-copy {
  display: grid !important;
  width: auto !important;
  min-width: 0;
  gap: 3px;
}

.knowledge-session-copy strong {
  padding-right: 0 !important;
  color: #e2ded3;
  font-size: 12px;
  font-weight: 650;
  line-height: 1.4;
}

.knowledge-session-copy small,
.knowledge-session-copy em {
  display: block;
  overflow: hidden;
  color: #747a76;
  font-size: 10px;
  font-style: normal;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.knowledge-session-copy em {
  color: #90866e;
}

.knowledge-session-list .session-delete {
  top: 10px;
  right: 9px;
  border-color: transparent;
  background: transparent;
}

.knowledge-workspace {
  position: relative;
  grid-template-rows: auto minmax(0, 1fr) auto;
  border-top: 1px solid var(--knowledge-line);
}

.knowledge-workspace-header {
  min-height: 76px;
  gap: 20px;
  padding: 12px 18px;
  border-bottom: 1px solid var(--knowledge-line);
  background: rgba(255, 255, 255, 0.012);
}

.knowledge-agent-mark {
  display: flex;
  align-items: center;
  gap: 11px;
  min-width: 0;
}

.knowledge-agent-icon {
  display: grid;
  width: 38px;
  height: 38px;
  flex: 0 0 auto;
  place-items: center;
  border: 1px solid rgba(213, 180, 106, 0.32);
  border-radius: 9px;
  background: rgba(213, 180, 106, 0.075);
  color: #e1c16f;
}

.knowledge-agent-icon svg {
  width: 20px;
  height: 20px;
}

.knowledge-agent-mark h1 {
  margin: 2px 0 0;
  color: #eee9dc;
  font-family: "Noto Serif SC", "Songti SC", serif;
  font-size: 17px;
  font-weight: 700;
  letter-spacing: 0.01em;
}

.knowledge-pipeline {
  display: flex;
  align-items: center;
  gap: 7px;
  color: #8f958f;
  font-size: 11px;
  white-space: nowrap;
}

.knowledge-pipeline span {
  padding: 5px 8px;
  border: 1px solid rgba(255, 255, 255, 0.07);
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.018);
}

.knowledge-pipeline i {
  width: 12px;
  height: 1px;
  background: rgba(213, 180, 106, 0.36);
}

.knowledge-message-list {
  min-height: 0;
  padding: 24px clamp(18px, 3vw, 52px);
  background:
    radial-gradient(circle at 50% 35%, rgba(213, 180, 106, 0.035), transparent 36%),
    #090a0b;
}

.knowledge-empty-state {
  display: grid;
  width: min(100%, 1040px);
  margin: auto;
  gap: 24px;
}

.knowledge-empty-copy {
  display: flex;
  align-items: center;
  gap: 17px;
  max-width: 760px;
}

.knowledge-empty-icon {
  display: grid;
  width: 58px;
  height: 58px;
  flex: 0 0 58px;
  place-items: center;
  border: 1px solid rgba(213, 180, 106, 0.3);
  border-radius: 14px;
  background: rgba(213, 180, 106, 0.06);
  color: #d4b566;
  box-shadow: inset 0 0 0 5px rgba(213, 180, 106, 0.018);
}

.knowledge-empty-icon svg {
  width: 28px;
  height: 28px;
}

.knowledge-empty-copy h2 {
  margin: 5px 0 7px;
  color: #f0ece2;
  font-family: "Noto Serif SC", "Songti SC", serif;
  font-size: clamp(21px, 2vw, 28px);
  font-weight: 700;
  letter-spacing: 0.01em;
}

.knowledge-empty-copy p {
  max-width: 720px;
  margin: 0;
  color: #8e948f;
  font-size: 12px;
  line-height: 1.75;
}

.knowledge-prompt-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 9px;
}

.knowledge-prompt-grid button {
  position: relative;
  display: grid;
  min-height: 78px;
  gap: 6px;
  overflow: hidden;
  border: 1px solid rgba(255, 255, 255, 0.075);
  border-radius: 9px;
  background: #111315;
  padding: 13px 46px 13px 14px;
  text-align: left;
  transition: border-color 0.16s ease, background 0.16s ease, transform 0.16s ease;
}

.knowledge-prompt-grid button:hover {
  border-color: rgba(213, 180, 106, 0.38);
  background: #161715;
  transform: translateY(-1px);
}

.knowledge-prompt-grid button > span {
  color: #a9935f;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.08em;
}

.knowledge-prompt-grid button > strong {
  overflow: hidden;
  color: #dedbd1;
  font-size: 13px;
  font-weight: 600;
  line-height: 1.45;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.knowledge-prompt-grid button svg {
  position: absolute;
  right: 15px;
  bottom: 18px;
  width: 17px;
  height: 17px;
  color: #776c50;
  transition: color 0.16s ease, transform 0.16s ease;
}

.knowledge-prompt-grid button:hover svg {
  color: #e0bf6c;
  transform: translateX(2px);
}

.knowledge-capabilities {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 18px;
  padding-top: 3px;
  color: #6f7571;
  font-size: 10px;
}

.knowledge-capabilities span {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.knowledge-capabilities b {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #4ebd8b;
  box-shadow: 0 0 0 3px rgba(78, 189, 139, 0.1);
}

.knowledge-message-list > .message {
  width: min(100%, 960px);
}

.knowledge-composer-wrap {
  display: grid;
  gap: 6px;
  padding: 13px clamp(18px, 3vw, 52px) 15px;
  border-top: 1px solid var(--knowledge-line);
  background: #0d0f10;
}

.knowledge-composer {
  width: min(100%, 1040px);
  margin: 0 auto;
  overflow: hidden;
  border: 1px solid rgba(213, 180, 106, 0.28);
  border-radius: 10px;
  background: #080a0b;
  box-shadow: 0 10px 28px rgba(0, 0, 0, 0.2);
  transition: border-color 0.16s ease, box-shadow 0.16s ease;
}

.knowledge-composer:focus-within {
  border-color: rgba(213, 180, 106, 0.56);
  box-shadow: 0 10px 30px rgba(0, 0, 0, 0.26), 0 0 0 3px rgba(213, 180, 106, 0.045);
}

.knowledge-composer textarea {
  display: block;
  width: 100%;
  min-height: 54px;
  max-height: 150px;
  resize: vertical;
  border: 0;
  outline: 0;
  background: transparent;
  color: #e7e3d8;
  padding: 14px 15px 8px;
  font-size: 13px;
  line-height: 1.6;
}

.knowledge-composer textarea::placeholder {
  color: #656b67;
}

.knowledge-composer-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 5px 7px 7px 14px;
}

.knowledge-composer-footer > div {
  display: flex;
  min-width: 0;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 12px;
  color: #646a66;
  font-size: 9px;
}

.knowledge-composer-footer kbd {
  border: 1px solid rgba(255, 255, 255, 0.1);
  border-radius: 3px;
  background: #121415;
  color: #929791;
  padding: 1px 4px;
  font-family: inherit;
  font-size: 9px;
}

.knowledge-model-hint {
  max-width: 220px;
  overflow: hidden;
  padding-left: 12px;
  border-left: 1px solid rgba(255, 255, 255, 0.08);
  color: #9a8962;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.knowledge-send-button {
  display: inline-flex;
  min-width: 78px;
  height: 34px;
  align-items: center;
  justify-content: center;
  gap: 6px;
  border: 1px solid #d4b566;
  border-radius: 7px;
  background: #d4b566;
  color: #17140c;
  padding: 0 13px;
  font-size: 12px;
  font-weight: 700;
}

.knowledge-send-button:hover:not(:disabled) {
  border-color: #ecd184;
  background: #e2c575;
}

.knowledge-send-button svg {
  width: 15px;
  height: 15px;
}

.knowledge-composer-wrap > .chat-page-error {
  width: min(100%, 1040px);
  margin: 0 auto;
  padding: 0 2px;
}

.knowledge-rail-toggle {
  display: none;
}

@media (min-width: 1600px) {
  .knowledge-prompt-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}

@media (max-width: 1100px) {
  .knowledge-pipeline {
    display: none;
  }
}

@media (max-width: 900px) {
  .knowledge-center {
    grid-template-columns: 1fr;
  }

  .knowledge-rail-toggle {
    display: grid;
  }

  .knowledge-workspace-header {
    padding-right: 58px;
  }

  .knowledge-session-rail {
    background: #0d0f10;
  }
}

@media (max-width: 640px) {
  .knowledge-workspace-header {
    min-height: 68px;
  }

  .knowledge-agent-icon {
    width: 34px;
    height: 34px;
  }

  .knowledge-agent-mark h1 {
    font-size: 15px;
  }

  .knowledge-empty-state {
    gap: 18px;
  }

  .knowledge-empty-copy {
    align-items: flex-start;
  }

  .knowledge-empty-icon {
    width: 44px;
    height: 44px;
    flex-basis: 44px;
    border-radius: 11px;
  }

  .knowledge-empty-copy h2 {
    font-size: 19px;
  }

  .knowledge-prompt-grid {
    grid-template-columns: 1fr;
  }

  .knowledge-prompt-grid button {
    min-height: 68px;
  }

  .knowledge-capabilities span:not(:first-child),
  .knowledge-composer-footer > div > span:not(.knowledge-model-hint) {
    display: none;
  }

  .knowledge-model-hint {
    max-width: 150px;
    padding-left: 0;
    border-left: 0;
  }
}
</style>

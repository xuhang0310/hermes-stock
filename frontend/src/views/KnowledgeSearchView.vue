<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue'
import { api } from '../api'
import type { AgentSession, AgentStreamEvent, ModelConfig } from '../types'
import { renderMarkdown } from '../utils/markdown'

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
  '最近有哪些关于新能源的政策公告？',
  '搜索关于芯片半导体的所有研报观点',
  '知识库里关于医药行业的最新研究报告有哪些？',
  '帮我在上传文档里找出与持仓标的相关的风险提示',
  '检索所有与AI相关的内部报告和外部研报',
  '最近一个月上传了哪些文档？摘要说说内容'
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
}

function applyQuickPrompt(prompt: string) {
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
      (event: AgentStreamEvent) => {
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
        }
        scrollChat()
      },
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
  <div class="knowledge-center-page">
  <div class="knowledge-center">
    <aside class="session-rail page-section">
      <header class="section-header">
        <div>
          <h2>检索历史</h2>
          <p>{{ sessions.length }} 条会话</p>
        </div>
        <button type="button" :disabled="sending" @click="newSession">新建</button>
      </header>

      <div class="model-switcher">
        <label>当前模型</label>
        <select v-model.number="selectedModelId" :disabled="sending || !models.length" :title="modelOptionLabel(activeModel)">
          <optgroup v-for="group in modelGroups" :key="group.platform" :label="group.platform">
            <option v-for="item in group.rows" :key="item.id" :value="item.id">
              {{ modelOptionLabel(item) }}
            </option>
          </optgroup>
        </select>
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
        <div v-if="!sessions.length" class="session-empty">暂无检索历史</div>
      </div>
    </aside>

    <main class="chat-workspace page-section">
      <section class="persona-strip">
        <div>
          <strong>知识库检索 Agent</strong>
          <span>混合检索 · RRF 融合 · 重排精排</span>
        </div>
        <p>基于向量 + 关键词双路召回 → RRF 融合 → 重排精排，检索已索引的文档片段并推理回答。支持按标的(symbol)、文档类型(doc_types)、可信度(trust_levels)过滤。也可用 SQL 精确查询知识库统计。</p>
        <div class="quick-prompts">
          <button
            v-for="(p, i) in knowledgePrompts"
            :key="i"
            type="button"
            :disabled="sending"
            @click="applyQuickPrompt(p)"
          >
            {{ p }}
          </button>
        </div>
      </section>

      <section ref="chatScroll" class="message-list">
        <div v-if="!messages.length" class="chat-empty">
          <strong>输入问题检索知识库</strong>
          <span>例：最近有哪些新能源政策公告？芯片行业的最新研报观点？AI相关的技术文档有哪些？</span>
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

      <form class="chat-input" @submit.prevent="send">
        <input
          v-model="question"
          type="text"
          placeholder="输入知识检索问题，例如：最近新能源政策有哪些变化？"
          :disabled="sending"
        />
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
  </div>
  </div>
</template>

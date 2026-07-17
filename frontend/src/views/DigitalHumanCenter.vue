<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue'
import { api } from '../api'
import FeishuBotPanel from './FeishuBotPanel.vue'
import type { AgentSession, AgentStreamEvent, DigitalHuman, ModelConfig, PromptRecommendation } from '../types'
import { renderMarkdown } from '../utils/markdown'
import { applyToolProgress, type LiveToolProgress } from '../utils/toolProgress'
import { isWorkspaceSaveworthy } from '../utils/workspace'

const activeCenterTab = ref<'chat' | 'feishuBots'>('chat')

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
  liveTool?: LiveToolProgress | null
  streamStatus?: 'connecting' | 'streaming' | 'tooling' | 'done' | 'error'
  saving?: boolean
  savedPath?: string
}

const profiles = ref<DigitalHuman[]>([])
const models = ref<ModelConfig[]>([])
const sessions = ref<AgentSession[]>([])
const promptRecommendations = ref<PromptRecommendation[]>([])
const fallbackPrompts = ref<string[]>([])
const messages = ref<ChatMessage[]>([])
const question = ref('')
const selectedProfileId = ref<number | null>(null)
const selectedModelId = ref<number | null>(null)
const selectedSessionId = ref<number | null>(null)
const sending = ref(false)
const abortController = ref<AbortController | null>(null)
const loadingSessions = ref(false)
const loadingSession = ref(false)
const loadingPrompts = ref(false)
const error = ref('')
const chatScroll = ref<HTMLElement | null>(null)

const selectedProfile = computed(() => profiles.value.find((item) => item.id === selectedProfileId.value) || null)
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
  const [profilePayload, modelPayload] = await Promise.all([
    api.digitalHumans(),
    api.modelConfigs()
  ])
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

async function openSession(id: number) {
  if (sending.value) return
  error.value = ''
  loadingSession.value = true
  try {
    const detail = await api.agentSession(id)
    selectedSessionId.value = detail.id
    selectedProfileId.value = detail.digital_human_id || selectedProfileId.value
    selectedModelId.value = detail.model_config_id || selectedModelId.value
    if (selectedProfileId.value) {
      await loadPromptRecommendations(selectedProfileId.value)
    }
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

function newSession(profileId?: number | null) {
  if (sending.value) return
  selectedSessionId.value = null
  selectedProfileId.value = profileId ?? selectedProfileId.value ?? profiles.value[0]?.id ?? null
  messages.value = []
  question.value = ''
  error.value = ''
  if (selectedProfileId.value) {
    loadPromptRecommendations(selectedProfileId.value)
  }
  refreshSessions(selectedProfileId.value)
}

async function deleteSession(item: AgentSession) {
  if (sending.value) return
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
  if (sending.value) return
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
  if (!selectedProfileId.value || sending.value) return
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
  if (!item.id || sending.value) return
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
  if (message.streamStatus === 'tooling') return '正在执行 ReAct 工具'
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

function questionBefore(item: ChatMessage): string {
  const idx = messages.value.indexOf(item)
  for (let i = idx - 1; i >= 0; i--) {
    if (messages.value[i].role === 'user') return messages.value[i].content
  }
  return ''
}

// 只给研究产物挂"存入工作区"，寒暄闲聊不挂（工作区是研究产物层，不是聊天记录）
function canSaveNote(item: ChatMessage): boolean {
  if (item.liveContent) return false
  return isWorkspaceSaveworthy(item.content, item.steps.some((s) => s.type === 'action'))
}

// 回答一键存工作区：前端已有全文，直接落盘，零 token（借鉴 deepresearch"文件按需物化"）
async function saveToWorkspace(item: ChatMessage) {
  if (!item.content || item.saving || item.savedPath) return
  item.saving = true
  try {
    const res = await api.saveWorkspaceNote({ content: item.content, title: questionBefore(item) })
    item.savedPath = res.path
  } catch (err) {
    error.value = err instanceof Error ? err.message : '存入工作区失败'
  } finally {
    item.saving = false
  }
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
        digital_human_id: selectedProfileId.value,
        model_config_id: selectedModelId.value
      },
      (event: AgentStreamEvent) => {
        if (event.type === 'session') {
          selectedSessionId.value = event.session_id || selectedSessionId.value
          if (event.profile?.id) selectedProfileId.value = event.profile.id
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
        } else if (event.type === 'tool_progress') {
          // 模型正在生成工具调用参数（写文件内容等长参数期）：瞬态进度，action 到达即清
          reply.streamStatus = 'tooling'
          reply.liveTool = applyToolProgress(reply.liveTool, event)
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
        } else if (event.type === 'error') {
          flushReasoning(reply)
          flushThought(reply)
          reply.liveTool = null
          reply.error = event.content || 'ReAct 响应失败'
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
    if (!reply.content && !reply.error && !reply.steps.length) {
      reply.error = '未收到最终回答，请重试。'
      reply.streamStatus = 'error'
    }
    // 不阻塞输入框：推迟到 nextTick 后跑，确保 sending=false 先渲染到 DOM
    nextTick(() => {
      refreshSessions().catch(() => {})
      if (selectedProfileId.value) {
        loadPromptRecommendations(selectedProfileId.value).catch(() => {})
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
  <div class="digital-center-page">
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
        <select v-model.number="selectedProfileId" :disabled="sending" @change="newSession(selectedProfileId)">
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
            <strong>{{ item.title }}</strong>
            <span>{{ item.digital_human_name || '数字人' }} · {{ sessionModelLabel(item) }}</span>
            <small>{{ item.message_count }} 条 · {{ item.status }}</small>
          </button>
          <button
            class="session-delete"
            type="button"
            title="删除该会话（从列表移除，记录仍保留在后台）"
            :disabled="sending || loadingSession"
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
            :disabled="sending"
            :class="{ dynamic: prompt.source === 'dynamic' }"
            :title="prompt.reason || (prompt.source === 'dynamic' ? '根据历史对话和系统数据推荐' : '该数字人的固定提示词')"
            @click="applyQuickPrompt(prompt.text)"
          >
            {{ prompt.text }}
          </button>
          <button type="button" :disabled="sending || loadingPrompts || !selectedProfileId" @click="refreshPromptRecommendations">
            {{ loadingPrompts ? '推荐生成中' : '刷新推荐' }}
          </button>
        </div>
      </section>

      <section ref="chatScroll" class="message-list">
        <div v-if="!messages.length" class="chat-empty">
          <strong>{{ selectedProfile?.name || 'A股研究数字人' }}</strong>
          <span>选择数字人和模型后开始会话，ReAct 的 Thought / Action / Observation 会在这里随回答一起出现。</span>
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

            <div v-if="item.liveTool" class="react-step action tool-progress">
              <strong><span class="tool-progress-pulse"></span>正在生成「{{ item.liveTool.displayName }}」· 已 {{ item.liveTool.chars.toLocaleString() }} 字</strong>
              <pre v-if="item.liveTool.preview" class="tool-progress-preview">{{ item.liveTool.preview }}</pre>
            </div>

            <div v-if="item.liveContent" class="md-body" v-html="renderMarkdown(item.liveContent)"></div>
            <div v-else-if="item.content" class="md-body" v-html="renderMarkdown(item.content)"></div>
            <div v-else-if="item.error" class="chat-error">{{ item.error }}</div>
            <div v-else-if="!item.steps.length && !item.liveReasoning" class="chat-typing">正在思考与查询...</div>

            <div v-if="canSaveNote(item)" class="msg-actions">
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

      <form class="chat-input" @submit.prevent="send">
        <input
          v-model="question"
          type="text"
          placeholder="例如：今天有哪些需要优先处理的事件？"
          :disabled="sending || !selectedProfile"
        />
        <select v-model.number="selectedModelId" :disabled="sending || !models.length" :title="modelOptionLabel(activeModel)">
          <optgroup v-for="group in modelGroups" :key="group.platform" :label="group.platform">
            <option v-for="item in group.rows" :key="item.id" :value="item.id">
              {{ modelOptionLabel(item) }}
            </option>
          </optgroup>
        </select>
        <button
          :type="sending ? 'button' : 'submit'"
          :class="{ 'btn-stop': sending }"
          :disabled="!sending && (!question.trim() || !selectedProfile)"
          @click="sending ? stopSending() : undefined"
        >
          {{ sending ? '⏹ 停止' : '发送' }}
        </button>
      </form>
      <p v-if="error" class="chat-page-error">{{ error }}</p>
    </main>
  </section>
  </div>
</template>

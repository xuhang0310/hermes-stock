<script setup lang="ts">
import { computed, nextTick, ref } from 'vue'
import { api } from '../api'
import { renderMarkdown } from '../utils/markdown'
import type {
  ModelConfig,
  PositionAgentStep,
  WatchlistAgentStreamEvent,
  WatchlistItem
} from '../types'

type AgentMessage = {
  id: number
  role: 'user' | 'assistant'
  content: string
  steps?: PositionAgentStep[]
  status?: 'thinking' | 'done' | 'error'
  changed?: boolean
  error?: string
}

const props = defineProps<{
  watchlist: WatchlistItem[]
}>()

const emit = defineEmits<{
  refresh: []
  openChart: [item: WatchlistItem]
  manageFeishu: []
}>()

const symbol = ref('')
const reason = ref('')
const formMessage = ref('')
const adding = ref(false)
const deletingId = ref<number | null>(null)
const agentOpen = ref(false)
const agentBusy = ref(false)
const agentInstruction = ref('')
const agentMessage = ref('')
const agentMessages = ref<AgentMessage[]>([])
const agentScroll = ref<HTMLElement | null>(null)
const models = ref<ModelConfig[]>([])
const selectedModelId = ref<number | null>(null)

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

async function addItem() {
  formMessage.value = ''
  if (!symbol.value.trim()) {
    formMessage.value = '请输入标的代码'
    return
  }
  adding.value = true
  try {
    await api.addWatchlist({
      symbol: symbol.value.trim().toUpperCase(),
      group_name: 'default',
      reason: reason.value.trim() || undefined
    })
    symbol.value = ''
    reason.value = ''
    formMessage.value = '已加入自选池'
    emit('refresh')
  } catch (err) {
    formMessage.value = err instanceof Error ? err.message : '加入失败'
  } finally {
    adding.value = false
  }
}

async function removeItem(item: WatchlistItem) {
  if (!window.confirm(`确定从自选池删除「${item.name}（${item.symbol}）」？`)) return
  deletingId.value = item.id
  formMessage.value = ''
  try {
    await api.deleteWatchlist(item.id)
    formMessage.value = `已删除 ${item.name}`
    emit('refresh')
  } catch (err) {
    formMessage.value = err instanceof Error ? err.message : '删除失败'
  } finally {
    deletingId.value = null
  }
}

function formatTime(value: string) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value || '-'
  return date.toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23'
  })
}

function modelLabel(model: ModelConfig | null | undefined) {
  if (!model) return '默认模型'
  return `${model.display_name || model.model_name}${model.is_active ? ' · 默认' : ''}`
}

async function openAgent() {
  agentOpen.value = true
  agentMessage.value = ''
  try {
    const payload = await api.modelConfigs()
    models.value = payload.filter((item) => item.status !== 'disabled')
    if (!selectedModelId.value || !models.value.some((item) => item.id === selectedModelId.value)) {
      selectedModelId.value = models.value.find((item) => item.is_active)?.id || models.value[0]?.id || null
    }
  } catch (err) {
    agentMessage.value = err instanceof Error ? err.message : '模型列表加载失败'
  }
  await scrollAgent(false)
}

function closeAgent() {
  if (!agentBusy.value) agentOpen.value = false
}

async function scrollAgent(smooth = true) {
  await nextTick()
  const element = agentScroll.value
  if (!element) return
  element.scrollTo({ top: element.scrollHeight, behavior: smooth ? 'smooth' : 'auto' })
}

function updateMessage(id: number, updater: (message: AgentMessage) => AgentMessage) {
  const index = agentMessages.value.findIndex((item) => item.id === id)
  if (index >= 0) agentMessages.value[index] = updater(agentMessages.value[index])
}

function currentMessage(id: number) {
  return agentMessages.value.find((item) => item.id === id)
}

async function sendAgentMessage() {
  const instruction = agentInstruction.value.trim()
  if (!instruction || agentBusy.value) return
  const history = agentMessages.value
    .filter((item) => item.content.trim() && item.status !== 'thinking')
    .slice(-8)
    .map((item) => ({ role: item.role, content: item.content }))
  const now = Date.now()
  agentMessages.value.push({ id: now, role: 'user', content: instruction })
  agentMessages.value.push({ id: now + 1, role: 'assistant', content: '', steps: [], status: 'thinking' })
  agentInstruction.value = ''
  agentMessage.value = ''
  agentBusy.value = true
  await scrollAgent()
  try {
    await api.streamWatchlistAgent(
      {
        instruction,
        model_config_id: selectedModelId.value,
        history
      },
      (event) => handleAgentEvent(event, now + 1)
    )
  } catch (err) {
    const message = err instanceof Error ? err.message : '自选录入 Agent 响应失败'
    agentMessage.value = message
    updateMessage(now + 1, (item) => ({ ...item, status: 'error', error: message }))
  } finally {
    agentBusy.value = false
    await scrollAgent()
  }
}

function handleAgentEvent(event: WatchlistAgentStreamEvent, messageId: number) {
  const message = currentMessage(messageId)
  if (!message) return
  if (event.type === 'model_selected' && event.step) {
    updateMessage(messageId, (item) => ({ ...item, steps: [...(item.steps || []), event.step as PositionAgentStep] }))
  } else if (event.type === 'thinking') {
    const content = event.message || '自选录入 Agent 正在分析...'
    const last = message.steps?.[message.steps.length - 1]
    if (last?.type !== 'thought' || last.content !== content) {
      updateMessage(messageId, (item) => ({
        ...item,
        steps: [...(item.steps || []), { type: 'thought', content }]
      }))
    }
  } else if (event.type === 'answer_delta') {
    updateMessage(messageId, (item) => ({ ...item, content: `${item.content}${event.content || ''}` }))
  } else if ((event.type === 'tool_start' || event.type === 'tool_done') && event.step) {
    updateMessage(messageId, (item) => ({ ...item, steps: [...(item.steps || []), event.step as PositionAgentStep] }))
  } else if (event.type === 'done') {
    const latest = currentMessage(messageId) || message
    const content = latest.content.trim() || event.reply || event.summary || '自选池处理完成。'
    updateMessage(messageId, (item) => ({
      ...item,
      content,
      status: 'done',
      changed: Boolean(event.should_save)
    }))
    if (event.should_save) emit('refresh')
  } else if (event.type === 'error') {
    const error = event.message || '自选录入 Agent 流式响应异常'
    agentMessage.value = error
    updateMessage(messageId, (item) => ({ ...item, status: 'error', error }))
  }
  void scrollAgent()
}

function statusText(message: AgentMessage) {
  if (message.status === 'thinking') return '自选录入 Agent 正在处理'
  if (message.status === 'done') return message.changed ? '自选池已更新' : 'Agent 已回复'
  if (message.status === 'error') return '处理失败'
  return '自选录入 Agent'
}

function stepLabel(step: PositionAgentStep) {
  if (step.type === 'model') return 'Model'
  if (step.type === 'thought') return 'Thinking'
  if (step.type === 'action') return `Action · ${step.display_name || step.tool || 'tool'}`
  if (step.type === 'observation') return `Observation · ${step.display_name || step.tool || 'tool'}`
  if (step.type === 'error') return `Error · ${step.display_name || step.tool || 'tool'}`
  return step.type
}
</script>

<template>
  <section class="page-section watchlist-page">
    <header class="section-header">
      <div>
        <h2>自选池</h2>
        <p>点击标的进入 TradingView 风格 K 线页；支持手工录入、删除和 Agent 批量维护。</p>
      </div>
      <div class="section-actions">
        <button type="button" @click="emit('manageFeishu')">绑定飞书</button>
        <button type="button" @click="openAgent">自选录入 Agent</button>
      </div>
    </header>

    <form class="inline-form" @submit.prevent="addItem">
      <input v-model="symbol" placeholder="代码，例如 600519.SH 或 600519" :disabled="adding" />
      <input v-model="reason" placeholder="入池理由（可选）" :disabled="adding" />
      <button type="submit" :disabled="adding || !symbol.trim()">{{ adding ? '加入中' : '加入自选' }}</button>
      <span class="form-message">{{ formMessage }}</span>
    </form>

    <div class="table-wrap">
      <table class="watchlist-table">
        <thead>
          <tr>
            <th>标的</th>
            <th>名称</th>
            <th>分组</th>
            <th>理由</th>
            <th>加入时间</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="!props.watchlist.length">
            <td colspan="6" class="empty-cell">暂无自选标的</td>
          </tr>
          <tr
            v-for="item in props.watchlist"
            :key="item.id"
            class="clickable-row"
            tabindex="0"
            :aria-label="`查看 ${item.name} K线`"
            @click="emit('openChart', item)"
            @keydown.enter="emit('openChart', item)"
          >
            <td><strong class="watchlist-symbol">{{ item.symbol }}</strong></td>
            <td>{{ item.name }}</td>
            <td>{{ item.group_name }}</td>
            <td class="watchlist-reason" :title="item.reason || ''">{{ item.reason || '-' }}</td>
            <td>{{ formatTime(item.added_at) }}</td>
            <td>
              <div class="watchlist-row-actions">
                <button type="button" title="查看K线" @click.stop="emit('openChart', item)">K线</button>
                <button
                  type="button"
                  class="danger-button"
                  title="删除自选"
                  :disabled="deletingId === item.id"
                  @click.stop="removeItem(item)"
                >
                  {{ deletingId === item.id ? '删除中' : '删除' }}
                </button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>

  <div v-if="agentOpen" class="position-agent-backdrop" @click.self="closeAgent">
    <aside class="position-agent-drawer">
      <header class="position-agent-drawer-header">
        <div>
          <h2>自选录入 Agent</h2>
          <p>用自然语言查询、增加或删除自选；标的不明确时会先检索并请你确认。</p>
        </div>
        <button type="button" :disabled="agentBusy" @click="closeAgent">关闭</button>
      </header>

      <section ref="agentScroll" class="message-list position-agent-messages">
        <div v-if="!agentMessages.length" class="chat-empty">
          <strong>管理自选池</strong>
          <span>试试输入“把招商银行加入自选，理由是跟踪银行板块”或“删除 600036”。</span>
        </div>
        <article v-for="item in agentMessages" :key="item.id" class="message" :class="item.role">
          <template v-if="item.role === 'user'">
            <span>你</span>
            <p>{{ item.content }}</p>
          </template>
          <template v-else>
            <div v-if="item.status" class="stream-status" :class="{ tooling: item.status === 'thinking', done: item.status === 'done', error: item.status === 'error' }">
              <span class="stream-status-pulse"></span>
              <strong>{{ statusText(item) }}</strong>
            </div>
            <div v-if="item.steps?.length" class="react-steps">
              <div v-for="(step, index) in item.steps" :key="index" class="react-step" :class="step.type">
                <strong>{{ stepLabel(step) }}</strong>
                <span>{{ step.content }}</span>
              </div>
            </div>
            <div v-if="item.content" class="md-body" v-html="renderMarkdown(item.content)"></div>
            <div v-else-if="item.error" class="chat-error">{{ item.error }}</div>
            <div v-else class="chat-typing">正在核对并更新自选池...</div>
          </template>
        </article>
      </section>

      <form class="position-agent-composer" @submit.prevent="sendAgentMessage">
        <textarea
          v-model="agentInstruction"
          rows="3"
          placeholder="输入要加入、删除或查询的自选标的"
          :disabled="agentBusy"
        ></textarea>
        <div class="watchlist-agent-actions">
          <select v-model.number="selectedModelId" :disabled="agentBusy || !models.length" :title="modelLabel(activeModel)">
            <optgroup v-for="group in modelGroups" :key="group.platform" :label="group.platform">
              <option v-for="item in group.rows" :key="item.id" :value="item.id">{{ modelLabel(item) }}</option>
            </optgroup>
          </select>
          <button type="submit" :disabled="agentBusy || !agentInstruction.trim()">
            {{ agentBusy ? '处理中' : '发送' }}
          </button>
        </div>
        <p v-if="agentMessage && !agentBusy" class="form-message">{{ agentMessage }}</p>
      </form>
    </aside>
  </div>
</template>

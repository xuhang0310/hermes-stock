<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api } from '../api'
import type { AgentBoardAgent, AgentBoardCounts, AgentTask } from '../types'

const props = defineProps<{ runId?: string }>()
const emit = defineEmits<{ openSession: [sessionId: number] }>()

type BoardColumn = 'queued' | 'running' | 'approval' | 'completed' | 'abnormal'
type TimeFilter = 'today' | 'yesterday' | 'this_week' | 'last_week' | 'custom'

const loading = ref(false)
const error = ref('')
const tasks = ref<AgentTask[]>([])
const counts = ref<AgentBoardCounts>({ queued: 0, running: 0, approval: 0, completed: 0, abnormal: 0 })
const agents = ref<AgentBoardAgent[]>([])
const searchQuery = ref('')
const filterStatus = ref('')
const filterAgent = ref('')
const timeFilter = ref<TimeFilter>('today')
const customDateFrom = ref('')
const customDateTo = ref('')
const activeColumn = ref<BoardColumn | ''>('')
const refreshTimer = ref<number | null>(null)
const refreshSeconds = ref(15)

const columnLabels: Record<BoardColumn, string> = {
  queued: '待执行',
  running: '执行中',
  approval: '待审批',
  completed: '已完成',
  abnormal: '异常',
}

const columnIcons: Record<BoardColumn, string> = {
  queued: '○',
  running: '●',
  approval: '●',
  completed: '✓',
  abnormal: '⚠',
}

const columnColors: Record<BoardColumn, string> = {
  queued: 'var(--muted)',
  running: 'var(--cyan)',
  approval: 'var(--gold)',
  completed: 'var(--ok)',
  abnormal: 'var(--danger)',
}

const columns: BoardColumn[] = ['queued', 'running', 'approval', 'completed', 'abnormal']

const filteredTasks = computed(() => {
  let result = tasks.value
  if (activeColumn.value) {
    result = result.filter((t) => t.board_column === activeColumn.value)
  }
  if (filterStatus.value) {
    result = result.filter((t) => t.status === filterStatus.value)
  }
  if (filterAgent.value) {
    result = result.filter((t) => t.agent_code === filterAgent.value)
  }
  if (searchQuery.value.trim()) {
    const q = searchQuery.value.toLowerCase()
    result = result.filter(
      (t) =>
        (t.task_title || '').toLowerCase().includes(q) ||
        (t.agent_name || '').toLowerCase().includes(q) ||
        (t.agent_code || '').toLowerCase().includes(q)
    )
  }
  return result
})

function tasksInColumn(col: BoardColumn) {
  return filteredTasks.value.filter((t) => t.board_column === col)
}

function formatDuration(seconds: number | null): string {
  if (!seconds) return '-'
  if (seconds < 60) return `${seconds}秒`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}分钟`
  return `${Math.floor(seconds / 3600)}小时${Math.floor((seconds % 3600) / 60)}分`
}

function formatTime(iso: string | null): string {
  if (!iso) return '-'
  const d = new Date(iso)
  return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}

function formatDate(d: Date): string {
  const year = d.getFullYear()
  const month = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

function getDateRange(): { date_from?: string; date_to?: string } {
  const now = new Date()
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate())

  switch (timeFilter.value) {
    case 'today':
      return { date_from: formatDate(today), date_to: formatDate(today) }
    case 'yesterday': {
      const y = new Date(today)
      y.setDate(y.getDate() - 1)
      return { date_from: formatDate(y), date_to: formatDate(y) }
    }
    case 'this_week': {
      const day = today.getDay() || 7
      const mon = new Date(today)
      mon.setDate(today.getDate() - day + 1)
      return { date_from: formatDate(mon), date_to: formatDate(today) }
    }
    case 'last_week': {
      const day = today.getDay() || 7
      const lastMon = new Date(today)
      lastMon.setDate(today.getDate() - day - 6)
      const lastSun = new Date(today)
      lastSun.setDate(today.getDate() - day)
      return { date_from: formatDate(lastMon), date_to: formatDate(lastSun) }
    }
    case 'custom':
      return {
        date_from: customDateFrom.value || undefined,
        date_to: customDateTo.value || undefined,
      }
    default:
      return {}
  }
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const dateRange = getDateRange()
    const [summaryRes, tasksRes, agentsRes] = await Promise.all([
      api.agentBoardSummary(dateRange),
      api.agentBoardTasks({ page_size: 100, ...dateRange }),
      api.agentBoardAgents(),
    ])
    counts.value = summaryRes.counts
    tasks.value = tasksRes.items
    agents.value = agentsRes.agents
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '加载任务看板失败'
  } finally {
    loading.value = false
  }
}

function setRefreshInterval(seconds: number) {
  refreshSeconds.value = seconds
  if (refreshTimer.value) {
    clearInterval(refreshTimer.value)
    refreshTimer.value = null
  }
  if (seconds > 0) {
    refreshTimer.value = window.setInterval(load, seconds * 1000)
  }
}

async function handleRetry(task: AgentTask) {
  try {
    await api.agentBoardRetry(task.run_id)
    await load()
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '重试失败'
  }
}

async function handleCancel(task: AgentTask) {
  try {
    await api.agentBoardCancel(task.run_id)
    await load()
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '取消失败'
  }
}

async function handlePriority(task: AgentTask, priority: number) {
  try {
    await api.agentBoardSetPriority(task.run_id, priority)
    await load()
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '优先级调整失败'
  }
}

function openTaskSession(task: AgentTask) {
  // 点击任务卡片 → 进入数字中心对应的对话窗口
  if (task.session_id) {
    emit('openSession', task.session_id)
  }
}

watch(() => props.runId, load)
onMounted(() => {
  load()
  setRefreshInterval(15)
})
onBeforeUnmount(() => {
  if (refreshTimer.value) clearInterval(refreshTimer.value)
})
</script>

<template>
  <section class="agent-task-board">
    <header class="board-toolbar">
      <div class="board-title">
        <h2>Agent 任务看板</h2>
        <p>查看每个数字人的当前工作状态</p>
      </div>
      <div class="board-actions">
        <button type="button" class="btn-outline" @click="load" :disabled="loading">
          {{ loading ? '刷新中...' : '刷新' }}
        </button>
      </div>
    </header>

    <!-- 筛选栏 -->
    <div class="board-filters">
      <div class="search-box">
        <span class="search-icon">&var(--ok);</span>
        <input
          v-model="searchQuery"
          type="text"
          placeholder="搜索 Agent 或任务"
          class="search-input"
        />
      </div>
      <select v-model="filterAgent" class="filter-select">
        <option value="">全部 Agent</option>
        <option v-for="a in agents" :key="a.code" :value="a.code">
          {{ a.name }}
        </option>
      </select>
      <select v-model="filterStatus" class="filter-select">
        <option value="">全部状态</option>
        <option value="queued">queued</option>
        <option value="running">running</option>
        <option value="waiting_approval">waiting_approval</option>
        <option value="completed">completed</option>
        <option value="failed">failed</option>
        <option value="timed_out">timed_out</option>
      </select>
      <select v-model="timeFilter" class="filter-select" @change="load">
        <option value="today">今天</option>
        <option value="yesterday">昨天</option>
        <option value="this_week">本周</option>
        <option value="last_week">上周</option>
        <option value="custom">自定义</option>
      </select>
      <template v-if="timeFilter === 'custom'">
        <input v-model="customDateFrom" type="date" class="filter-date" @change="load" />
        <span class="filter-date-sep">至</span>
        <input v-model="customDateTo" type="date" class="filter-date" @change="load" />
      </template>
      <div class="refresh-control">
        <span>自动刷新：{{ refreshSeconds }} 秒</span>
        <button
          type="button"
          class="btn-sm"
          @click="setRefreshInterval(refreshSeconds === 15 ? 0 : 15)"
        >
          {{ refreshSeconds > 0 ? '暂停' : '开启' }}
        </button>
      </div>
    </div>

    <div v-if="loading && !tasks.length" class="board-state">
      <span class="loading-mark"></span>
      正在加载任务看板...
    </div>
    <div v-else-if="error" class="notice notice-danger">
      <strong>加载失败</strong>
      <span>{{ error }}</span>
    </div>

    <template v-else>
      <!-- 五状态框 -->
      <div class="board-columns">
        <div
          v-for="col in columns"
          :key="col"
          class="board-column"
          :class="{ active: activeColumn === col }"
          @click="activeColumn = activeColumn === col ? '' : col"
        >
          <div class="column-header" :style="{ borderColor: columnColors[col] }">
            <span class="column-icon" :style="{ color: columnColors[col] }">
              {{ columnIcons[col] }}
            </span>
            <span class="column-label">{{ columnLabels[col] }}</span>
            <span class="column-count" :style="{ color: columnColors[col] }">
              {{ counts[col] || 0 }}
            </span>
          </div>
          <div class="column-cards">
            <div
              v-for="task in tasksInColumn(col)"
              :key="task.run_id"
              class="task-card"
              :class="task.board_column"
              title="点击进入数字中心对应会话"
              @click.stop="openTaskSession(task)"
            >
              <div class="card-header">
                <div class="card-agent">
                  <span class="agent-avatar" v-if="task.agent_avatar">
                    <img :src="task.agent_avatar" :alt="task.agent_name || ''" />
                  </span>
                  <span class="agent-avatar agent-icon" v-else>&var(--ok);</span>
                  <span class="agent-name">{{ task.agent_name || '未分配' }}</span>
                </div>
              </div>
              <div class="card-title">{{ task.task_title }}</div>
              <div v-if="task.scheduled_job" class="card-source">
                {{ task.scheduled_job.name }}
              </div>
              <div v-if="task.board_column === 'queued' && task.priority > 100" class="card-priority">
                <span class="priority-badge high">高优先级</span>
              </div>
              <div v-if="task.board_column === 'running'" class="card-progress">
                <div class="progress-bar">
                  <div class="progress-fill" style="width: 68%"></div>
                </div>
                <span class="progress-text">已运行 {{ formatDuration(task.run_seconds) }}</span>
              </div>
              <div v-if="task.board_column === 'running'" class="card-duration">
                已运行 {{ formatDuration(task.run_seconds) }}
              </div>
              <div v-if="task.board_column === 'approval'" class="card-approval-info">
                等待高风险工具审批
              </div>
              <div v-if="task.board_column === 'completed'" class="card-completed">
                完成于 {{ formatTime(task.finished_at) }}
              </div>
              <div v-if="task.board_column === 'abnormal'" class="card-error">
                {{ task.error || '未知错误' }}
              </div>
              <div v-if="task.board_column === 'abnormal'" class="card-retry-info">
                已重试 {{ task.attempts }} 次
              </div>
              <div class="card-actions" v-if="task.board_column === 'abnormal'">
                <button type="button" class="btn-retry" @click.stop="handleRetry(task)">
                  重试
                </button>
                <button type="button" class="btn-detail" @click.stop="openTaskSession(task)">
                  查看会话
                </button>
              </div>
              <div class="card-actions" v-if="task.board_column === 'approval'">
                <button type="button" class="btn-approve">批准</button>
                <button type="button" class="btn-reject">拒绝</button>
              </div>
              <div class="card-meta">
                <span v-if="task.agent_code" class="meta-tag">{{ task.agent_code }}</span>
                <span v-if="task.priority !== 100" class="meta-tag">P{{ task.priority }}</span>
              </div>
            </div>
            <div v-if="tasksInColumn(col).length === 0" class="column-empty">
              暂无任务
            </div>
          </div>
        </div>
      </div>
    </template>
  </section>
</template>

<style scoped>
.agent-task-board {
  padding: 24px;
  min-height: 100vh;
  background: var(--bg-2);
  color: var(--text);
}

.board-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 20px;
  padding-bottom: 16px;
  border-bottom: 1px solid var(--line);
}

.board-title h2 {
  font-size: 24px;
  font-weight: 600;
  margin: 0 0 4px;
  color: var(--gold);
}

.board-title p {
  font-size: 13px;
  color: var(--muted);
  margin: 0;
}

.board-actions .btn-outline {
  background: transparent;
  border: 1px solid var(--line);
  color: var(--text);
  padding: 8px 16px;
  border-radius: 4px;
  cursor: pointer;
  font-size: 13px;
  transition: all 0.2s;
}

.board-actions .btn-outline:hover:not(:disabled) {
  border-color: var(--gold);
  color: var(--gold);
}

.board-filters {
  display: flex;
  gap: 12px;
  align-items: center;
  margin-bottom: 24px;
  flex-wrap: wrap;
}

.search-box {
  position: relative;
  flex: 1;
  min-width: 200px;
}

.search-icon {
  position: absolute;
  left: 12px;
  top: 50%;
  transform: translateY(-50%);
  color: var(--muted);
  font-size: 14px;
}

.search-input {
  width: 100%;
  padding: 10px 12px 10px 36px;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 4px;
  color: var(--text);
  font-size: 14px;
  outline: none;
  transition: border-color 0.2s;
}

.search-input:focus {
  border-color: var(--gold);
}

.filter-select {
  padding: 10px 12px;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 4px;
  color: var(--text);
  font-size: 13px;
  outline: none;
  cursor: pointer;
  min-width: 120px;
}

.filter-date {
  padding: 10px 12px;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 4px;
  color: var(--text);
  font-size: 13px;
  outline: none;
  cursor: pointer;
  min-width: 140px;
}

.filter-date:focus {
  border-color: var(--gold);
}

.filter-date-sep {
  color: var(--muted);
  font-size: 13px;
  padding: 0 4px;
}

.refresh-control {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--muted);
}

.btn-sm {
  padding: 4px 10px;
  background: var(--surface-2);
  border: 1px solid var(--line);
  border-radius: 4px;
  color: var(--text);
  font-size: 12px;
  cursor: pointer;
}

.btn-sm:hover {
  border-color: var(--gold);
  color: var(--gold);
}

.board-columns {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 16px;
  min-height: 500px;
}

@media (max-width: 1200px) {
  .board-columns {
    grid-template-columns: repeat(3, 1fr);
  }
}

@media (max-width: 768px) {
  .board-columns {
    grid-template-columns: 1fr;
  }
}

.board-column {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 8px;
  overflow: hidden;
  cursor: pointer;
  transition: border-color 0.2s;
}

.board-column.active {
  border-color: var(--gold);
}

.board-column:hover {
  border-color: var(--muted-2);
}

.column-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 16px;
  border-bottom: 2px solid;
  border-top: 2px solid transparent;
}

.column-icon {
  font-size: 14px;
}

.column-label {
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
  flex: 1;
}

.column-count {
  font-size: 16px;
  font-weight: 700;
}

.column-cards {
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-height: 100px;
}

.column-empty {
  text-align: center;
  color: var(--muted-2);
  font-size: 13px;
  padding: 20px 0;
}

.task-card {
  background: var(--bg-2);
  border: 1px solid var(--line);
  border-radius: 4px;
  padding: 12px;
  cursor: pointer;
  transition: border-color 0.2s;
}

.task-card:hover {
  border-color: var(--muted-2);
}

.task-card.queued {
  border-left: 3px solid var(--muted);
}

.task-card.running {
  border-left: 3px solid var(--cyan);
}

.task-card.approval {
  border-left: 3px solid var(--gold);
}

.task-card.completed {
  border-left: 3px solid var(--ok);
}

.task-card.abnormal {
  border-left: 3px solid var(--danger);
}

.card-header {
  display: flex;
  align-items: center;
  margin-bottom: 8px;
}

.card-agent {
  display: flex;
  align-items: center;
  gap: 8px;
}

.agent-avatar {
  width: 24px;
  height: 24px;
  border-radius: 50%;
  overflow: hidden;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--surface-2);
  font-size: 14px;
}

.agent-avatar img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.agent-name {
  font-size: 13px;
  color: var(--text);
  font-weight: 500;
}

.card-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
  margin-bottom: 6px;
  line-height: 1.4;
}

.card-source {
  font-size: 12px;
  color: var(--muted);
  margin-bottom: 6px;
}

.card-priority {
  margin-bottom: 6px;
}

.priority-badge {
  font-size: 12px;
  padding: 2px 6px;
  border-radius: 4px;
  font-weight: 600;
}

.priority-badge.high {
  background: var(--danger);
  color: #fff;
}

.card-progress {
  margin-bottom: 6px;
}

.progress-bar {
  height: 4px;
  background: var(--surface-2);
  border-radius: 4px;
  overflow: hidden;
  margin-bottom: 4px;
}

.progress-fill {
  height: 100%;
  background: var(--cyan);
  border-radius: 4px;
  transition: width 0.3s;
}

.progress-text {
  font-size: 12px;
  color: var(--muted);
}

.card-duration {
  font-size: 12px;
  color: var(--muted);
  margin-bottom: 6px;
}

.card-approval-info {
  font-size: 12px;
  color: var(--gold);
  margin-bottom: 8px;
}

.card-completed {
  font-size: 12px;
  color: var(--ok);
  margin-bottom: 6px;
}

.card-error {
  font-size: 12px;
  color: var(--danger);
  margin-bottom: 4px;
  line-height: 1.4;
}

.card-retry-info {
  font-size: 12px;
  color: var(--muted);
  margin-bottom: 8px;
}

.card-actions {
  display: flex;
  gap: 6px;
  margin-bottom: 6px;
}

.btn-retry,
.btn-approve,
.btn-reject,
.btn-detail {
  flex: 1;
  padding: 5px 8px;
  border-radius: 4px;
  font-size: 12px;
  cursor: pointer;
  border: 1px solid;
  transition: all 0.2s;
}

.btn-retry {
  background: transparent;
  border-color: var(--danger);
  color: var(--danger);
}

.btn-retry:hover {
  background: var(--danger);
  color: #fff;
}

.btn-detail {
  background: transparent;
  border-color: var(--line);
  color: var(--text);
}

.btn-detail:hover {
  border-color: var(--text);
}

.btn-approve {
  background: var(--ok);
  border-color: var(--ok);
  color: var(--bg-2);
}

.btn-approve:hover {
  background: var(--ok);
}

.btn-reject {
  background: transparent;
  border-color: var(--line);
  color: var(--text);
}

.btn-reject:hover {
  border-color: var(--danger);
  color: var(--danger);
}

.card-meta {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
}

.meta-tag {
  font-size: 12px;
  padding: 1px 5px;
  background: var(--surface-2);
  border-radius: 4px;
  color: var(--muted);
}

.board-state {
  text-align: center;
  padding: 40px;
  color: var(--muted);
}

.loading-mark {
  display: inline-block;
  width: 16px;
  height: 16px;
  border: 2px solid var(--line);
  border-top-color: var(--gold);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
  margin-right: 8px;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

.notice {
  padding: 12px 16px;
  border-radius: 4px;
  font-size: 13px;
}

.notice-danger {
  background: #f25f6420;
  border: 1px solid var(--danger);
  color: var(--danger);
}

.notice-danger strong {
  margin-right: 8px;
}
</style>

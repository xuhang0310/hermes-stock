<template>
  <div class="ops-console">
    <header class="ops-header">
      <h2>运维控制台</h2>
      <span class="ops-readonly-badge">只读视图 · 跨用户</span>
      <div class="ops-filter">
        <label>用户筛选</label>
        <select v-model="selectedUserId" @change="onFilterChange">
          <option :value="0">全部用户</option>
          <option v-for="u in users" :key="u.id" :value="u.id">
            {{ u.display_name }}（{{ u.username }}）
          </option>
        </select>
      </div>
    </header>

    <nav class="ops-tabs">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        :class="['ops-tab', { active: activeTab === tab.key }]"
        @click="activeTab = tab.key"
      >
        {{ tab.label }}
      </button>
    </nav>

    <div v-if="loading" class="ops-empty">加载中…</div>
    <div v-else-if="error" class="ops-empty ops-error">{{ error }}</div>

    <!-- 持仓 -->
    <section v-else-if="activeTab === 'positions'">
      <div v-if="!positions.length" class="ops-empty">暂无持仓数据</div>
      <table v-else class="ops-table">
        <thead>
          <tr>
            <th>归属用户</th>
            <th>标的</th>
            <th>数量</th>
            <th>成本价</th>
            <th>最新价</th>
            <th>市值</th>
            <th>浮动盈亏</th>
            <th>日内盈亏</th>
            <th>状态</th>
            <th>账户</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in positions" :key="`${row.owner_user_id}-${row.id}`">
            <td>{{ row.owner_display_name }}<br /><span class="muted">{{ row.owner_username }}</span></td>
            <td>{{ row.symbol }}<br /><span class="muted">{{ row.name }}</span></td>
            <td>{{ fmtQty(row.quantity) }}</td>
            <td>{{ fmtPrice(row.cost_price) }}</td>
            <td>{{ fmtPrice(row.latest_price) }}</td>
            <td>{{ fmtAmt(row.market_value) }}</td>
            <td :class="pnlClass(row.floating_pnl)">{{ fmtPnl(row.floating_pnl, row.floating_pnl_pct) }}</td>
            <td :class="pnlClass(row.day_pnl)">{{ fmtPrice(row.day_pnl) }}</td>
            <td>{{ row.status }}</td>
            <td>{{ row.broker_name || '—' }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <!-- 自选 -->
    <section v-else-if="activeTab === 'watchlist'">
      <div v-if="!watchlist.length" class="ops-empty">暂无自选股</div>
      <table v-else class="ops-table">
        <thead>
          <tr>
            <th>归属用户</th>
            <th>标的</th>
            <th>分组</th>
            <th>现价</th>
            <th>涨跌幅</th>
            <th>总市值(亿)</th>
            <th>添加理由</th>
            <th>更新时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in watchlist" :key="`wl-${row.owner_user_id}-${row.id}`">
            <td>{{ row.owner_display_name }}<br /><span class="muted">{{ row.owner_username }}</span></td>
            <td>{{ row.symbol }}<br /><span class="muted">{{ row.name }}</span></td>
            <td>{{ row.group_name }}</td>
            <td>{{ fmtPrice(row.current_price) }}</td>
            <td :class="pnlClass(row.current_pct_chg)">{{ fmtPct(row.current_pct_chg) }}</td>
            <td>{{ row.total_mv_yi ?? '—' }}</td>
            <td class="reason-cell">{{ row.reason || '—' }}</td>
            <td>{{ row.updated_at || '—' }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <!-- 聊天记录 -->
    <section v-else-if="activeTab === 'chat'">
      <div v-if="!sessions.length" class="ops-empty">暂无对话记录</div>
      <div v-else class="ops-chat">
        <aside class="ops-chat-list">
          <div
            v-for="s in sessions"
            :key="s.id"
            :class="['ops-chat-item', { active: selectedSessionId === s.id }]"
            @click="loadSession(s.id)"
          >
            <div class="session-title">{{ s.title || '（无标题）' }}</div>
            <div class="session-meta">
              <span>{{ s.username }}</span>
              <span v-if="s.digital_human_name" class="muted">· {{ s.digital_human_name }}</span>
              <span class="muted">· {{ s.message_count }} 条</span>
            </div>
            <div class="session-time muted">{{ s.updated_at || '—' }}</div>
          </div>
        </aside>
        <div class="ops-chat-detail">
          <div v-if="!selectedSessionId" class="ops-empty">从左侧选择会话查看回放</div>
          <div v-else-if="sessionLoading" class="ops-empty">加载中…</div>
          <template v-else-if="sessionDetail">
            <div class="session-detail-head">
              <strong>{{ sessionDetail.title }}</strong>
              <span class="muted">{{ sessionDetail.username }} · {{ sessionDetail.digital_human_name || '数字人' }}</span>
            </div>
            <div class="msg-stream">
              <div
                v-for="m in sessionDetail.messages"
                :key="m.id"
                :class="['msg-row', `msg-${m.role}`]"
              >
                <div class="msg-role">{{ m.role === 'user' ? '用户' : sessionDetail.digital_human_name || '助手' }}</div>
                <div v-if="m.content" class="md-body" v-html="renderMarkdown(m.content)"></div>
                <div v-if="m.error" class="msg-error">出错：{{ m.error }}</div>
              </div>
            </div>
          </template>
        </div>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, onMounted } from 'vue'
import { api } from '../api'
import { renderMarkdown } from '../utils/markdown'
import type {
  OpsConsoleUser,
  OpsConsolePosition,
  OpsConsoleWatchlistItem,
  OpsChatSessionListItem,
  OpsChatSessionDetail,
} from '../types'

const tabs = [
  { key: 'positions', label: '全部持仓' },
  { key: 'watchlist', label: '全部自选' },
  { key: 'chat', label: '聊天记录' },
] as const

const users = ref<OpsConsoleUser[]>([])
const selectedUserId = ref(0)
const activeTab = ref<(typeof tabs)[number]['key']>('positions')

const positions = ref<OpsConsolePosition[]>([])
const watchlist = ref<OpsConsoleWatchlistItem[]>([])
const sessions = ref<OpsChatSessionListItem[]>([])

const loading = ref(false)
const error = ref('')

const selectedSessionId = ref<number | null>(null)
const sessionLoading = ref(false)
const sessionDetail = ref<OpsChatSessionDetail | null>(null)

async function loadUsers() {
  try {
    users.value = await api.opsConsoleUsers()
  } catch {
    // 权限不足等，由具体数据加载报错
  }
}

async function loadTabData() {
  loading.value = true
  error.value = ''
  try {
    if (activeTab.value === 'positions') {
      positions.value = await api.opsConsolePositions(selectedUserId.value || undefined)
    } else if (activeTab.value === 'watchlist') {
      watchlist.value = await api.opsConsoleWatchlist(selectedUserId.value || undefined)
    } else {
      sessions.value = await api.opsConsoleChatSessions({ userId: selectedUserId.value || undefined })
      sessionDetail.value = null
      selectedSessionId.value = null
    }
  } catch (e: any) {
    error.value = e?.message || '加载失败'
  } finally {
    loading.value = false
  }
}

async function onFilterChange() {
  await loadTabData()
}

async function loadSession(id: number) {
  selectedSessionId.value = id
  sessionLoading.value = true
  sessionDetail.value = null
  try {
    sessionDetail.value = await api.opsConsoleChatSession(id)
  } catch (e: any) {
    error.value = e?.message || '加载会话失败'
  } finally {
    sessionLoading.value = false
  }
}

function pnlClass(v: number | null | undefined) {
  if (v === null || v === undefined) return ''
  if (v > 0) return 'pnl-up'
  if (v < 0) return 'pnl-down'
  return ''
}

function fmtPrice(v: number | null | undefined) {
  if (v === null || v === undefined) return '—'
  return Number(v).toFixed(2)
}
function fmtQty(v: number | null | undefined) {
  if (v === null || v === undefined) return '—'
  return Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 0 })
}
function fmtAmt(v: number | null | undefined) {
  if (v === null || v === undefined) return '—'
  return Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 2 })
}
function fmtPct(v: number | null | undefined) {
  if (v === null || v === undefined) return '—'
  return `${Number(v) >= 0 ? '+' : ''}${Number(v).toFixed(2)}%`
}
function fmtPnl(v: number | null | undefined, pct: number | null | undefined) {
  if (v === null || v === undefined) return '—'
  const sign = Number(v) >= 0 ? '+' : ''
  const pctTxt = pct !== null && pct !== undefined ? ` (${fmtPct(pct)})` : ''
  return `${sign}${Number(v).toFixed(2)}${pctTxt}`
}

onMounted(async () => {
  await loadUsers()
  await loadTabData()
})

watch(activeTab, () => {
  loadTabData()
})
</script>

<style scoped>
.ops-console {
  padding: 20px 28px;
  max-width: 1400px;
  margin: 0 auto;
}
.ops-header {
  display: flex;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
  margin-bottom: 16px;
}
.ops-header h2 {
  margin: 0;
  font-size: 20px;
}
.ops-readonly-badge {
  font-size: 12px;
  color: #8a6d3b;
  background: #fcf8e3;
  border: 1px solid #faebcc;
  border-radius: 4px;
  padding: 2px 8px;
}
.ops-filter {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}
.ops-filter select {
  padding: 4px 8px;
  border: 1px solid #d0d0d0;
  border-radius: 4px;
  background: #fff;
}
.ops-tabs {
  display: flex;
  gap: 4px;
  border-bottom: 1px solid #e0e0e0;
  margin-bottom: 16px;
}
.ops-tab {
  background: none;
  border: none;
  padding: 8px 18px;
  cursor: pointer;
  font-size: 14px;
  color: #666;
  border-bottom: 2px solid transparent;
}
.ops-tab.active {
  color: #1a1a1a;
  border-bottom-color: #1890ff;
  font-weight: 600;
}
.ops-empty {
  padding: 32px;
  text-align: center;
  color: #999;
}
.ops-error {
  color: #d4493b;
}
.ops-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.ops-table th,
.ops-table td {
  border: 1px solid #eee;
  padding: 8px 10px;
  text-align: left;
  vertical-align: top;
}
.ops-table th {
  background: #fafafa;
  font-weight: 600;
}
.ops-table tr:nth-child(even) td {
  background: #fcfcfd;
}
.muted {
  color: #999;
  font-size: 12px;
}
.reason-cell {
  max-width: 280px;
  white-space: pre-wrap;
  word-break: break-word;
}
.ops-chat {
  display: flex;
  gap: 12px;
  height: calc(100vh - 240px);
  min-height: 360px;
}
.ops-chat-list {
  width: 320px;
  overflow-y: auto;
  border: 1px solid #eee;
  border-radius: 6px;
}
.ops-chat-item {
  padding: 10px 12px;
  border-bottom: 1px solid #f0f0f0;
  cursor: pointer;
}
.ops-chat-item:hover {
  background: #f7f9fc;
}
.ops-chat-item.active {
  background: #e6f7ff;
}
.session-title {
  font-weight: 600;
  font-size: 13px;
  margin-bottom: 4px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.session-meta {
  font-size: 12px;
  color: #555;
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.session-time {
  margin-top: 2px;
}
.ops-chat-detail {
  flex: 1;
  border: 1px solid #eee;
  border-radius: 6px;
  overflow-y: auto;
  padding: 16px;
}
.session-detail-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin-bottom: 12px;
  padding-bottom: 8px;
  border-bottom: 1px solid #f0f0f0;
}
.msg-stream {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.msg-row {
  border-left: 3px solid #ddd;
  padding: 6px 12px;
}
.msg-user {
  border-left-color: #1890ff;
}
.msg-assistant {
  border-left-color: #52c41a;
}
.msg-role {
  font-size: 12px;
  color: #888;
  margin-bottom: 4px;
}
.msg-error {
  color: #d4493b;
  font-size: 12px;
  margin-top: 4px;
}
</style>

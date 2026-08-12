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
  width: min(1480px, 100%);
  margin: 0 auto;
  padding: 22px 24px 42px;
}

.ops-header {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
  margin-bottom: 18px;
}
.ops-header h2 {
  margin: 0;
  color: var(--gold-2);
  font-size: 19px;
  font-weight: 700;
}
.ops-readonly-badge {
  font-size: 11px;
  letter-spacing: 0.08em;
  color: #d8b35f;
  background: rgba(216, 179, 95, 0.1);
  border: 1px solid rgba(216, 179, 95, 0.3);
  border-radius: 999px;
  padding: 3px 10px;
}
.ops-filter {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--muted);
  font-size: 12px;
}
.ops-filter select {
  min-width: 180px;
  height: 34px;
  border: 1px solid var(--line-strong);
  border-radius: 6px;
  background: #09090a;
  color: var(--text);
  padding: 0 10px;
  font: inherit;
}
.ops-filter select:focus {
  border-color: var(--gold);
  outline: none;
}

.ops-tabs {
  display: flex;
  gap: 8px;
  padding: 10px 0;
  border-bottom: 1px solid var(--line);
  margin-bottom: 16px;
}
.ops-tab {
  min-height: 34px;
  border: 1px solid var(--line-strong);
  border-radius: 6px;
  background: #0b0b0c;
  color: var(--muted);
  padding: 7px 16px;
  font-size: 13px;
}
.ops-tab:hover {
  border-color: var(--gold-dim);
  color: var(--gold-2);
}
.ops-tab.active {
  border-color: var(--gold-dim);
  background: rgba(213, 180, 106, 0.14);
  color: var(--gold-2);
  font-weight: 700;
}

.ops-empty {
  padding: 40px;
  text-align: center;
  color: var(--muted);
  font-size: 13px;
}
.ops-error {
  color: var(--danger);
}

/* 表格沿用全局 dark 主题（th 金色、td 浅米色、hover 金光），只补结构 */
.ops-table {
  width: 100%;
  border-collapse: collapse;
  font-variant-numeric: tabular-nums;
}
.ops-table th,
.ops-table td {
  border-bottom: 1px solid var(--line);
  padding: 10px 12px;
  text-align: left;
  vertical-align: top;
  line-height: 1.45;
}
.ops-table th {
  background: #141414;
  color: var(--gold-2);
  font-size: 12px;
  font-weight: 700;
  white-space: nowrap;
}
.ops-table td {
  color: #e3dfd2;
  font-size: 13px;
}
.ops-table tbody tr:hover td {
  background: rgba(213, 180, 106, 0.055);
}
/* 红涨绿跌：td 自带 color，需更高特异性覆盖全局 */
.ops-table td.pnl-up {
  color: var(--danger);
  font-weight: 700;
}
.ops-table td.pnl-down {
  color: var(--ok);
  font-weight: 700;
}
.ops-table .num {
  white-space: nowrap;
}
.muted {
  color: var(--muted);
  font-size: 12px;
}
.reason-cell {
  max-width: 280px;
  white-space: pre-wrap;
  word-break: break-word;
}

/* 聊天双栏 */
.ops-chat {
  display: grid;
  grid-template-columns: 320px minmax(0, 1fr);
  gap: 14px;
  height: calc(100vh - 260px);
  min-height: 420px;
}
.ops-chat-list {
  display: grid;
  align-content: start;
  gap: 8px;
  overflow-y: auto;
  padding: 8px;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: #0a0a0b;
}
.ops-chat-item {
  display: grid;
  gap: 4px;
  min-width: 0;
  border: 1px solid rgba(213, 180, 106, 0.2);
  border-radius: 6px;
  background: rgba(12, 12, 13, 0.72);
  padding: 10px 12px;
  cursor: pointer;
}
.ops-chat-item:hover {
  border-color: rgba(213, 180, 106, 0.56);
  background: rgba(213, 180, 106, 0.08);
}
.ops-chat-item.active {
  border-color: var(--gold-2);
  background: linear-gradient(90deg, rgba(213, 180, 106, 0.24), rgba(213, 180, 106, 0.08)), #111113;
  box-shadow: inset 3px 0 0 var(--gold-2);
}
.session-title {
  overflow: hidden;
  color: #f2ead2;
  font-size: 13px;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.session-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  color: #cfc7b6;
  font-size: 12px;
}
.session-time {
  color: var(--muted);
  font-size: 11px;
}

.ops-chat-detail {
  min-width: 0;
  overflow-y: auto;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: #080809;
  padding: 16px 18px;
}
.session-detail-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin-bottom: 14px;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--line);
}
.session-detail-head strong {
  color: var(--gold-2);
  font-size: 15px;
}
.msg-stream {
  display: grid;
  gap: 12px;
}
.msg-row {
  border-left: 3px solid var(--line-strong);
  border-radius: 0 6px 6px 0;
  background: rgba(12, 12, 13, 0.6);
  padding: 10px 14px;
}
.msg-user {
  border-left-color: var(--gold);
  background: rgba(213, 180, 106, 0.06);
}
.msg-assistant {
  border-left-color: rgba(213, 180, 106, 0.3);
}
.msg-role {
  margin-bottom: 5px;
  color: var(--gold-2);
  font-size: 12px;
  font-weight: 700;
}
.msg-error {
  margin-top: 5px;
  color: var(--danger);
  font-size: 12px;
}
</style>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'
import type { BacktestListItem } from '../types'

const emit = defineEmits<{ open: [runId: string] }>()

const loading = ref(false)
const error = ref('')
const items = ref<BacktestListItem[]>([])
const searchQuery = ref('')

const strategyNames: Record<string, string> = {
  macd_cross: 'MACD 交叉',
  signal_macd: 'MACD 信号',
  signal_ma_cross: '均线交叉',
  signal_bollinger: '布林带',
  signal_rsi: 'RSI',
  factor_combination: '因子组合',
  portfolio_risk_parity: '风险平价组合',
}

const confidenceLabels: Record<string, string> = {
  insufficient: '数据不足',
  low: '低',
  medium: '中',
  high: '高',
}

const filtered = computed(() => {
  const q = searchQuery.value.trim().toLowerCase()
  if (!q) return items.value
  return items.value.filter(
    (it) =>
      it.symbol.toLowerCase().includes(q) ||
      it.strategy_code.toLowerCase().includes(q) ||
      strategyLabel(it.strategy_code).toLowerCase().includes(q)
  )
})

function strategyLabel(code: string): string {
  return strategyNames[code] || code
}

function confidenceLabel(conf: string): string {
  return confidenceLabels[conf] || conf
}

function percent(value: number | null | undefined, signed = true): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '-'
  const sign = signed && value > 0 ? '+' : ''
  return `${sign}${(value * 100).toFixed(2)}%`
}

function num(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '-'
  return Number(value).toFixed(2)
}

function tone(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return ''
  if (value > 0) return 'pos'
  if (value < 0) return 'neg'
  return ''
}

function formatTime(iso: string | null): string {
  if (!iso) return '-'
  const d = new Date(iso)
  return d.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const res = await api.backtests()
    items.value = res.items
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '加载回测记录失败'
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <section class="bt-history">
    <header class="bt-toolbar">
      <div class="bt-title">
        <h2>回测报告</h2>
        <p>共 {{ items.length }} 条回测历史，点击任意一条查看完整明细。</p>
      </div>
      <button type="button" class="btn-outline" :disabled="loading" @click="load">
        {{ loading ? '刷新中...' : '刷新' }}
      </button>
    </header>

    <div class="bt-filters">
      <input
        v-model="searchQuery"
        type="text"
        class="search-input"
        placeholder="搜索股票代码或策略"
      />
    </div>

    <div v-if="loading && !items.length" class="bt-state">正在加载回测记录...</div>
    <div v-else-if="error" class="notice notice-danger">
      <strong>加载失败</strong>
      <span>{{ error }}</span>
    </div>
    <div v-else-if="!filtered.length" class="bt-state">
      {{ items.length ? '没有匹配的记录' : '暂无回测记录，去数字人中心发起一次回测吧' }}
    </div>

    <div v-else class="bt-table-wrap">
      <table class="bt-table">
        <thead>
          <tr>
            <th>时间</th>
            <th>股票</th>
            <th>策略</th>
            <th>周期</th>
            <th>回测区间</th>
            <th class="num">总收益</th>
            <th class="num">最大回撤</th>
            <th class="num">夏普</th>
            <th class="num">胜率</th>
            <th>置信度</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="it in filtered"
            :key="it.run_id"
            class="bt-row"
            @click="emit('open', it.run_id)"
          >
            <td class="muted">{{ formatTime(it.created_at) }}</td>
            <td class="symbol">{{ it.symbol }}</td>
            <td>{{ strategyLabel(it.strategy_code) }}</td>
            <td class="muted">{{ it.timeframe }}</td>
            <td class="muted">{{ it.start_date }} ~ {{ it.end_date }}</td>
            <td class="num" :class="tone(it.metrics.total_return)">
              {{ percent(it.metrics.total_return) }}
            </td>
            <td class="num neg">{{ percent(it.metrics.max_drawdown, false) }}</td>
            <td class="num" :class="tone(it.metrics.sharpe)">{{ num(it.metrics.sharpe) }}</td>
            <td class="num">{{ percent(it.metrics.win_rate, false) }}</td>
            <td>
              <span class="conf-tag">{{ confidenceLabel(it.confidence) }}</span>
            </td>
            <td class="num">
              <span class="open-hint">查看 ›</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>

<style scoped>
.bt-history {
  padding: 24px;
  min-height: 100vh;
  background: #0d1117;
  color: #e6edf3;
}

.bt-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 20px;
  padding-bottom: 16px;
  border-bottom: 1px solid #30363d;
}

.bt-title h2 {
  font-size: 22px;
  font-weight: 600;
  margin: 0 0 4px;
  color: #eabf63;
}

.bt-title p {
  font-size: 13px;
  color: #8b949e;
  margin: 0;
}

.btn-outline {
  background: transparent;
  border: 1px solid #30363d;
  color: #e6edf3;
  padding: 8px 16px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 13px;
  transition: all 0.2s;
}

.btn-outline:hover:not(:disabled) {
  border-color: #eabf63;
  color: #eabf63;
}

.bt-filters {
  margin-bottom: 16px;
}

.search-input {
  width: 100%;
  max-width: 360px;
  padding: 10px 12px;
  background: #161b22;
  border: 1px solid #30363d;
  border-radius: 6px;
  color: #e6edf3;
  font-size: 14px;
  outline: none;
  transition: border-color 0.2s;
}

.search-input:focus {
  border-color: #eabf63;
}

.bt-state {
  text-align: center;
  padding: 48px;
  color: #8b949e;
  font-size: 14px;
}

.bt-table-wrap {
  border: 1px solid #30363d;
  border-radius: 8px;
  overflow-x: auto;
  background: #161b22;
}

.bt-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
  min-width: 860px;
}

.bt-table thead th {
  text-align: left;
  padding: 12px 14px;
  color: #8b949e;
  font-weight: 600;
  border-bottom: 1px solid #30363d;
  white-space: nowrap;
}

.bt-table th.num,
.bt-table td.num {
  text-align: right;
}

.bt-table tbody td {
  padding: 12px 14px;
  border-bottom: 1px solid #21262d;
  white-space: nowrap;
}

.bt-row {
  cursor: pointer;
  transition: background 0.15s;
}

.bt-row:hover {
  background: #1c2530;
}

.bt-row:last-child td {
  border-bottom: none;
}

.symbol {
  font-weight: 600;
  color: #e6edf3;
}

.muted {
  color: #8b949e;
}

.pos {
  color: #f25f64;
  font-weight: 600;
}

.neg {
  color: #20c997;
  font-weight: 600;
}

.conf-tag {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 10px;
  background: #21262d;
  color: #8b949e;
}

.open-hint {
  color: #eabf63;
  font-size: 12px;
}

.notice {
  padding: 12px 16px;
  border-radius: 6px;
  font-size: 13px;
}

.notice-danger {
  background: #f25f6420;
  border: 1px solid #f25f64;
  color: #f25f64;
}

.notice-danger strong {
  margin-right: 8px;
}
</style>

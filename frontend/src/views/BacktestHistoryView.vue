<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'
import type { BacktestListItem } from '../types'

const emit = defineEmits<{ open: [runId: string] }>()

const loading = ref(false)
const error = ref('')
const items = ref<BacktestListItem[]>([])
const searchQuery = ref('')
const revealKey = ref(0)

const strategyNames: Record<string, string> = {
  macd_cross: 'MACD 交叉',
  signal_macd: 'MACD 信号',
  signal_ma_cross: '均线交叉',
  signal_bollinger: '布林带',
  signal_rsi: 'RSI',
  factor_combination: '因子组合',
  risk_parity: '风险平价组合',
  portfolio_risk_parity: '风险平价组合',
}

const strategyColors: Record<string, string> = {
  macd_cross: '#eabf63',
  signal_macd: '#eabf63',
  signal_ma_cross: '#3bb2d6',
  signal_bollinger: '#a371f7',
  signal_rsi: '#f0883e',
  factor_combination: '#20c997',
  risk_parity: '#f25f64',
  portfolio_risk_parity: '#f25f64',
}
const FALLBACK_COLORS = ['#eabf63', '#3bb2d6', '#20c997', '#f0883e', '#f25f64', '#8b949e']

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
      (it.symbol_name || '').toLowerCase().includes(q) ||
      it.strategy_code.toLowerCase().includes(q) ||
      strategyLabel(it.strategy_code).toLowerCase().includes(q)
  )
})

const summary = computed(() => {
  const all = items.value
  const withReturn = all.filter((it) => Number.isFinite(it.metrics?.total_return))
  const win = withReturn.filter((it) => it.metrics.total_return > 0).length
  const lose = withReturn.filter((it) => it.metrics.total_return < 0).length
  const decided = win + lose
  const sharpes = all.map((it) => it.metrics?.sharpe).filter((v): v is number => Number.isFinite(v))
  const avgSharpe = sharpes.length ? sharpes.reduce((a, b) => a + b, 0) / sharpes.length : null
  const best = withReturn.length
    ? withReturn.reduce((a, b) => (b.metrics.total_return > a.metrics.total_return ? b : a))
    : null
  return {
    total: all.length,
    winPct: decided ? win / decided : null,
    win,
    lose,
    avgSharpe,
    best,
  }
})

function strategyLabel(code: string): string {
  return strategyNames[code] || code
}

function strategyColor(code: string): string {
  if (strategyColors[code]) return strategyColors[code]
  let h = 0
  for (let i = 0; i < code.length; i++) h = (h * 31 + code.charCodeAt(i)) >>> 0
  return FALLBACK_COLORS[h % FALLBACK_COLORS.length]
}

function confidenceLabel(conf: string): string {
  return confidenceLabels[conf] || conf
}

function confidenceClass(conf: string): string {
  if (conf === 'high') return 'conf-high'
  if (conf === 'medium') return 'conf-mid'
  return 'conf-low'
}

function isPortfolio(it: BacktestListItem): boolean {
  return it.symbol.toUpperCase() === 'PORTFOLIO'
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

function winMeter(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '0%'
  return `${Math.max(0, Math.min(100, value * 100)).toFixed(0)}%`
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
    revealKey.value += 1
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
    <div class="bt-ambient" aria-hidden="true"></div>

    <header class="bt-toolbar">
      <div class="bt-title">
        <span class="bt-title-mark"></span>
        <div>
          <h2>回测报告</h2>
          <p>历史策略回测档案 · 点击任意一条查看完整明细</p>
        </div>
      </div>
      <button type="button" class="btn-outline" :disabled="loading" @click="load">
        <span class="btn-spin" :class="{ spin: loading }"></span>
        {{ loading ? '刷新中' : '刷新' }}
      </button>
    </header>

    <div class="bt-summary">
      <div class="stat">
        <span class="stat-num">{{ summary.total }}</span>
        <span class="stat-label">回测总数</span>
      </div>
      <div class="stat">
        <span class="stat-num" :class="summary.winPct !== null && summary.winPct >= 0.5 ? 'pos' : 'neg'">
          {{ summary.winPct === null ? '-' : percent(summary.winPct, false) }}
        </span>
        <span class="stat-label">盈利占比 · {{ summary.win }} 盈 / {{ summary.lose }} 亏</span>
      </div>
      <div class="stat">
        <span class="stat-num" :class="tone(summary.avgSharpe)">{{ num(summary.avgSharpe) }}</span>
        <span class="stat-label">平均 Sharpe</span>
      </div>
      <div class="stat stat-best" v-if="summary.best">
        <span class="stat-num pos">{{ percent(summary.best.metrics.total_return) }}</span>
        <span class="stat-label">最佳 · {{ summary.best.symbol_name || summary.best.symbol }}</span>
      </div>
    </div>

    <div class="bt-filters">
      <label class="search-box">
        <svg class="search-icon" viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
          <circle cx="11" cy="11" r="7" fill="none" stroke="currentColor" stroke-width="2" />
          <line x1="16.5" y1="16.5" x2="21" y2="21" stroke="currentColor" stroke-width="2" stroke-linecap="round" />
        </svg>
        <input
          v-model="searchQuery"
          type="text"
          class="search-input"
          placeholder="搜索股票代码、名称或策略"
        />
      </label>
      <span class="result-count">{{ filtered.length }} / {{ items.length }}</span>
    </div>

    <div v-if="loading && !items.length" class="bt-state">
      <span class="loading-mark"></span>正在加载回测记录...
    </div>
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
            <th>标的</th>
            <th>策略</th>
            <th>周期</th>
            <th>回测区间</th>
            <th class="num">总收益</th>
            <th class="num">最大回撤</th>
            <th class="num">夏普</th>
            <th class="num">胜率</th>
            <th>置信度</th>
            <th class="num"></th>
          </tr>
        </thead>
        <tbody :key="revealKey">
          <tr
            v-for="(it, idx) in filtered"
            :key="it.run_id"
            class="bt-row"
            :class="tone(it.metrics.total_return)"
            :style="{ '--d': idx * 32 + 'ms', '--accent': strategyColor(it.strategy_code) }"
            @click="emit('open', it.run_id)"
          >
            <td class="muted mono">{{ formatTime(it.created_at) }}</td>
            <td class="instrument">
              <template v-if="isPortfolio(it)">
                <span class="inst-name">组合</span>
                <span class="inst-code mono">PORTFOLIO</span>
              </template>
              <template v-else>
                <span class="inst-name">{{ it.symbol_name || it.symbol }}</span>
                <span class="inst-code mono" v-if="it.symbol_name">{{ it.symbol }}</span>
              </template>
            </td>
            <td>
              <span class="chip" :style="{ '--c': strategyColor(it.strategy_code) }">
                {{ strategyLabel(it.strategy_code) }}
              </span>
            </td>
            <td class="muted mono">{{ it.timeframe }}</td>
            <td class="muted mono range">{{ it.start_date }}<span class="tilde">~</span>{{ it.end_date }}</td>
            <td class="num big" :class="tone(it.metrics.total_return)">
              <span class="caret">{{ (it.metrics.total_return ?? 0) > 0 ? '▲' : (it.metrics.total_return ?? 0) < 0 ? '▼' : '' }}</span>
              {{ percent(it.metrics.total_return) }}
            </td>
            <td class="num mono neg">{{ percent(it.metrics.max_drawdown, false) }}</td>
            <td class="num mono" :class="tone(it.metrics.sharpe)">{{ num(it.metrics.sharpe) }}</td>
            <td class="num winrate">
              <span class="wr-num mono">{{ percent(it.metrics.win_rate, false) }}</span>
              <span class="wr-meter"><i :style="{ width: winMeter(it.metrics.win_rate) }"></i></span>
            </td>
            <td>
              <span class="conf-tag" :class="confidenceClass(it.confidence)">{{ confidenceLabel(it.confidence) }}</span>
            </td>
            <td class="num">
              <span class="open-hint">查看 <span class="chev">›</span></span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>

<style scoped>
.bt-history {
  position: relative;
  padding: 28px 24px 48px;
  min-height: 100vh;
  background: #0d1117;
  color: #e6edf3;
  overflow: hidden;
}

.bt-ambient {
  position: absolute;
  inset: 0;
  pointer-events: none;
  background:
    radial-gradient(520px 280px at 12% -6%, rgba(234, 191, 99, 0.10), transparent 70%),
    radial-gradient(480px 300px at 96% 4%, rgba(59, 178, 214, 0.08), transparent 72%);
}

.bt-toolbar {
  position: relative;
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 22px;
}

.bt-title {
  display: flex;
  align-items: center;
  gap: 14px;
}

.bt-title-mark {
  width: 4px;
  height: 38px;
  border-radius: 2px;
  background: linear-gradient(180deg, #eabf63, #f0883e);
}

.bt-title h2 {
  font-size: 26px;
  font-weight: 700;
  letter-spacing: -0.02em;
  margin: 0;
  color: #f2f5f8;
}

.bt-title p {
  font-size: 12.5px;
  color: #8b949e;
  margin: 3px 0 0;
  letter-spacing: 0.01em;
}

.btn-outline {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  background: rgba(22, 27, 34, 0.7);
  border: 1px solid #30363d;
  color: #e6edf3;
  padding: 8px 16px;
  border-radius: 8px;
  cursor: pointer;
  font-size: 13px;
  transition: border-color 0.2s, color 0.2s, transform 0.12s;
}

.btn-outline:hover:not(:disabled) {
  border-color: #eabf63;
  color: #eabf63;
  transform: translateY(-1px);
}

.btn-spin {
  width: 11px;
  height: 11px;
  border-radius: 50%;
  border: 2px solid currentColor;
  border-top-color: transparent;
  opacity: 0.7;
}

.btn-spin.spin {
  animation: spin 0.7s linear infinite;
}

.bt-summary {
  position: relative;
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 14px;
  margin-bottom: 20px;
}

.stat {
  position: relative;
  padding: 16px 18px;
  background: linear-gradient(180deg, rgba(22, 27, 34, 0.9), rgba(13, 17, 23, 0.6));
  border: 1px solid #21262d;
  border-radius: 12px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  overflow: hidden;
  transition: border-color 0.2s, transform 0.15s;
}

.stat::after {
  content: '';
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 3px;
  background: var(--bar, #30363d);
  opacity: 0.8;
}

.stat:nth-child(1) { --bar: #3bb2d6; }
.stat:nth-child(2) { --bar: #eabf63; }
.stat:nth-child(3) { --bar: #8b949e; }
.stat:nth-child(4) { --bar: #f25f64; }

.stat:hover {
  border-color: #3a424d;
  transform: translateY(-2px);
}

.stat-num {
  font-family: ui-monospace, 'SF Mono', 'Cascadia Code', 'JetBrains Mono', Consolas, monospace;
  font-size: 28px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.02em;
  color: #f2f5f8;
  line-height: 1;
}

.stat-label {
  font-size: 12px;
  color: #8b949e;
}

.bt-filters {
  position: relative;
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 14px;
}

.search-box {
  position: relative;
  display: inline-flex;
  align-items: center;
  flex: 0 1 380px;
}

.search-icon {
  position: absolute;
  left: 12px;
  color: #6e7681;
  pointer-events: none;
}

.search-input {
  width: 100%;
  padding: 10px 12px 10px 36px;
  background: rgba(22, 27, 34, 0.8);
  border: 1px solid #30363d;
  border-radius: 9px;
  color: #e6edf3;
  font-size: 13.5px;
  outline: none;
  transition: border-color 0.2s, box-shadow 0.2s;
}

.search-input:focus {
  border-color: #eabf63;
  box-shadow: 0 0 0 3px rgba(234, 191, 99, 0.12);
}

.result-count {
  font-family: ui-monospace, 'SF Mono', Consolas, monospace;
  font-size: 12px;
  color: #6e7681;
}

.bt-state {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  padding: 56px;
  color: #8b949e;
  font-size: 14px;
}

.loading-mark {
  width: 16px;
  height: 16px;
  border: 2px solid #30363d;
  border-top-color: #eabf63;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

.bt-table-wrap {
  position: relative;
  border: 1px solid #21262d;
  border-radius: 14px;
  overflow: hidden;
  background: rgba(22, 27, 34, 0.55);
}

.bt-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
  min-width: 980px;
}

.bt-table thead th {
  text-align: left;
  padding: 13px 16px;
  color: #7d8590;
  font-weight: 600;
  font-size: 11.5px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  background: rgba(13, 17, 23, 0.6);
  border-bottom: 1px solid #21262d;
  white-space: nowrap;
}

.bt-table th.num,
.bt-table td.num {
  text-align: right;
}

.bt-table tbody td {
  padding: 13px 16px;
  border-bottom: 1px solid rgba(33, 38, 45, 0.7);
  white-space: nowrap;
}

.bt-row {
  position: relative;
  cursor: pointer;
  animation: rowIn 0.42s cubic-bezier(0.22, 0.61, 0.36, 1) both;
  animation-delay: var(--d);
  transition: background 0.16s;
}

.bt-row::before {
  content: '';
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 3px;
  background: var(--accent, #30363d);
  opacity: 0.55;
  transition: width 0.16s, opacity 0.16s;
}

.bt-row:hover {
  background: rgba(28, 37, 48, 0.85);
}

.bt-row:hover::before {
  width: 5px;
  opacity: 1;
}

.bt-row:last-child td {
  border-bottom: none;
}

.mono {
  font-family: ui-monospace, 'SF Mono', 'Cascadia Code', Consolas, monospace;
  font-variant-numeric: tabular-nums;
}

.instrument {
  display: flex;
  flex-direction: column;
  gap: 2px;
  line-height: 1.25;
}

.inst-name {
  font-size: 14px;
  font-weight: 600;
  color: #f2f5f8;
}

.inst-code {
  font-size: 11px;
  color: #6e7681;
  letter-spacing: 0.02em;
}

.chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  padding: 3px 9px 3px 8px;
  border-radius: 999px;
  color: var(--c, #e6edf3);
  background: color-mix(in srgb, var(--c, #8b949e) 14%, transparent);
  border: 1px solid color-mix(in srgb, var(--c, #8b949e) 32%, transparent);
}

.chip::before {
  content: '';
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--c, #8b949e);
}

.range .tilde {
  margin: 0 5px;
  color: #484f58;
}

.num {
  font-variant-numeric: tabular-nums;
}

.big {
  font-family: ui-monospace, 'SF Mono', 'Cascadia Code', Consolas, monospace;
  font-size: 15px;
  font-weight: 700;
}

.caret {
  font-size: 9px;
  margin-right: 3px;
  opacity: 0.85;
}

.pos {
  color: #f25f64;
}

.neg {
  color: #20c997;
}

.muted {
  color: #8b949e;
}

.winrate {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 9px;
}

.wr-num {
  min-width: 48px;
  text-align: right;
  color: #c9d1d9;
}

.wr-meter {
  width: 46px;
  height: 4px;
  border-radius: 2px;
  background: #21262d;
  overflow: hidden;
}

.wr-meter i {
  display: block;
  height: 100%;
  background: linear-gradient(90deg, #eabf63, #f0883e);
  border-radius: 2px;
}

.conf-tag {
  font-size: 11px;
  padding: 2px 9px;
  border-radius: 999px;
  font-weight: 600;
}

.conf-high {
  color: #20c997;
  background: rgba(32, 201, 151, 0.12);
  border: 1px solid rgba(32, 201, 151, 0.3);
}

.conf-mid {
  color: #eabf63;
  background: rgba(234, 191, 99, 0.12);
  border: 1px solid rgba(234, 191, 99, 0.3);
}

.conf-low {
  color: #8b949e;
  background: #21262d;
}

.open-hint {
  color: #6e7681;
  font-size: 12px;
  transition: color 0.16s;
}

.bt-row:hover .open-hint {
  color: #eabf63;
}

.chev {
  display: inline-block;
  transition: transform 0.16s;
}

.bt-row:hover .chev {
  transform: translateX(3px);
}

.notice {
  position: relative;
  padding: 12px 16px;
  border-radius: 8px;
  font-size: 13px;
}

.notice-danger {
  background: rgba(242, 95, 100, 0.12);
  border: 1px solid #f25f64;
  color: #f25f64;
}

.notice-danger strong {
  margin-right: 8px;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

@keyframes rowIn {
  from {
    opacity: 0;
    transform: translateY(8px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

@media (max-width: 900px) {
  .bt-summary { grid-template-columns: repeat(2, 1fr); }
}

@media (prefers-reduced-motion: reduce) {
  .bt-row,
  .btn-spin.spin,
  .loading-mark { animation: none; }
  .bt-row::before,
  .chev,
  .stat,
  .btn-outline { transition: none; }
}
</style>

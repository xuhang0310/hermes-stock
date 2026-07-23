<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'
import type { BacktestRun } from '../types'

const props = defineProps<{ runId: string }>()
const emit = defineEmits<{ open: [runId: string] }>()

const run = ref<BacktestRun | null>(null)
const error = ref('')

const curvePoints = computed(() => {
  const rows = run.value?.equity_curve || []
  if (rows.length < 2) return ''
  const values = rows.map((item) => item.equity)
  const min = Math.min(...values)
  const max = Math.max(...values)
  const range = max - min || 1
  return values
    .map((value, index) => {
      const x = (index / (values.length - 1)) * 100
      const y = 34 - ((value - min) / range) * 28
      return `${x.toFixed(2)},${y.toFixed(2)}`
    })
    .join(' ')
})

function percent(value: number | null | undefined) {
  if (value === null || value === undefined) return '-'
  return `${value >= 0 ? '+' : ''}${(value * 100).toFixed(2)}%`
}

function number(value: number | null | undefined, digits = 2) {
  if (value === null || value === undefined) return '-'
  return Number(value).toFixed(digits)
}

onMounted(async () => {
  try {
    run.value = await api.backtest(props.runId)
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '回测摘要加载失败'
  }
})
</script>

<template>
  <section v-if="run" class="backtest-summary-card">
    <header>
      <div>
        <span class="backtest-summary-kicker">STRATEGY BACKTEST</span>
        <strong>{{ run.strategy.name }} · {{ run.symbol }}</strong>
      </div>
      <span class="backtest-confidence" :class="run.confidence">{{ run.rating }}</span>
    </header>
    <div class="backtest-summary-metrics">
      <div><span>总收益</span><b :class="{ positive: run.metrics.total_return >= 0, negative: run.metrics.total_return < 0 }">{{ percent(run.metrics.total_return) }}</b></div>
      <div><span>年化</span><b>{{ percent(run.metrics.annual_return) }}</b></div>
      <div><span>Sharpe</span><b>{{ number(run.metrics.sharpe) }}</b></div>
      <div><span>最大回撤</span><b class="negative">{{ percent(run.metrics.max_drawdown) }}</b></div>
      <div><span>胜率</span><b>{{ percent(run.metrics.win_rate) }}</b></div>
      <div><span>交易</span><b>{{ run.metrics.trade_count }}</b></div>
    </div>
    <svg class="backtest-mini-curve" viewBox="0 0 100 38" preserveAspectRatio="none" aria-hidden="true">
      <polyline :points="curvePoints" fill="none" stroke="currentColor" stroke-width="1.5" vector-effect="non-scaling-stroke" />
    </svg>
    <footer>
      <span>{{ run.period.start_date }} 至 {{ run.period.end_date }} · {{ run.period.trading_days }} 个交易日</span>
      <button type="button" @click="emit('open', run.run_id)">查看完整报告 →</button>
    </footer>
  </section>
  <p v-else-if="error" class="backtest-summary-error">{{ error }}</p>
</template>

<style scoped>
.backtest-summary-card {
  margin-top: 14px;
  border: 1px solid rgba(216, 178, 94, 0.35);
  border-radius: 8px;
  background: #0d0e0f;
  overflow: hidden;
}
.backtest-summary-card header,
.backtest-summary-card footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 14px 16px;
}
.backtest-summary-card header {
  border-bottom: 1px solid rgba(216, 178, 94, 0.16);
}
.backtest-summary-card header div {
  display: grid;
  gap: 3px;
}
.backtest-summary-kicker {
  color: #b99448;
  font-size: 10px;
  letter-spacing: 0.12em;
}
.backtest-summary-card strong {
  color: #f2e8ce;
  font-size: 15px;
}
.backtest-confidence {
  border: 1px solid #6f5b2e;
  padding: 4px 8px;
  color: #e0bd68;
  font-size: 12px;
}
.backtest-confidence.insufficient,
.backtest-confidence.low {
  border-color: rgba(255, 93, 93, 0.5);
  color: #ff7e7e;
}
.backtest-summary-metrics {
  display: grid;
  grid-template-columns: repeat(6, minmax(80px, 1fr));
  padding: 14px 16px 8px;
}
.backtest-summary-metrics div {
  display: grid;
  gap: 4px;
  min-width: 0;
}
.backtest-summary-metrics span,
.backtest-summary-card footer span {
  color: #8e8a80;
  font-size: 11px;
}
.backtest-summary-metrics b {
  color: #ddd6c4;
  font-family: Consolas, monospace;
  font-size: 16px;
}
.backtest-summary-metrics .positive { color: #2bd49b; }
.backtest-summary-metrics .negative { color: #ff6b6b; }
.backtest-mini-curve {
  display: block;
  width: calc(100% - 32px);
  height: 54px;
  margin: 0 16px;
  color: #d8b25e;
}
.backtest-summary-card footer {
  padding-top: 8px;
}
.backtest-summary-card button {
  border: 0;
  background: transparent;
  color: #e5bd61;
  cursor: pointer;
  font-weight: 700;
}
.backtest-summary-error { color: #ff7474; font-size: 12px; }
@media (max-width: 820px) {
  .backtest-summary-metrics { grid-template-columns: repeat(3, 1fr); gap: 12px; }
}
</style>

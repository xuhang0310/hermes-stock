<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  CandlestickSeries,
  ColorType,
  HistogramSeries,
  LineSeries,
  createChart,
  createSeriesMarkers
} from 'lightweight-charts'
import type { Time } from 'lightweight-charts'
import { api } from '../api'
import type { BacktestRun } from '../types'

const props = defineProps<{ runId: string }>()
const emit = defineEmits<{ back: [] }>()

type ReportTab = 'chart' | 'trades' | 'attribution' | 'code'
type MetricTone = 'positive' | 'negative' | 'neutral' | 'accent'

const run = ref<BacktestRun | null>(null)
const loading = ref(false)
const error = ref('')
const activeTab = ref<ReportTab>('chart')
const priceHost = ref<HTMLElement | null>(null)
const equityHost = ref<HTMLElement | null>(null)
const macdHost = ref<HTMLElement | null>(null)

let priceChart: ReturnType<typeof createChart> | null = null
let equityChart: ReturnType<typeof createChart> | null = null
let macdChart: ReturnType<typeof createChart> | null = null
let resizeObserver: ResizeObserver | null = null

function number(value: number | null | undefined, digits = 2) {
  return value === null || value === undefined ? '-' : Number(value).toFixed(digits)
}

function percent(value: number | null | undefined, signed = true) {
  if (value === null || value === undefined) return '-'
  const sign = signed && value > 0 ? '+' : ''
  return `${sign}${(value * 100).toFixed(2)}%`
}

function currency(value: number | null | undefined) {
  if (value === null || value === undefined) return '-'
  const sign = value < 0 ? '-' : ''
  return `${sign}¥${Math.abs(Math.round(value)).toLocaleString('zh-CN')}`
}

function tone(value: number | null | undefined): MetricTone {
  if (value === null || value === undefined || value === 0) return 'neutral'
  return value > 0 ? 'positive' : 'negative'
}

function tradeReason(reason: string) {
  if (reason === 'signal') return '死叉信号'
  if (reason === 'end_of_backtest') return '回测到期平仓'
  if (reason === 'stop_loss') return '止损'
  if (reason === 'take_profit') return '止盈'
  return reason || '-'
}

const primaryMetrics = computed(() => {
  const metrics = run.value?.metrics
  if (!metrics) return []
  return [
    { label: '总收益率', value: percent(metrics.total_return), tone: tone(metrics.total_return), detail: '策略区间收益' },
    { label: '超额收益', value: percent(metrics.excess_return), tone: tone(metrics.excess_return), detail: '相对沪深300' },
    { label: '最大回撤', value: percent(metrics.max_drawdown, false), tone: 'negative' as MetricTone, detail: '峰值至谷值' },
    { label: 'Sharpe', value: number(metrics.sharpe), tone: tone(metrics.sharpe), detail: '风险调整收益' },
    { label: '胜率', value: percent(metrics.win_rate, false), tone: metrics.win_rate >= 0.5 ? 'positive' as MetricTone : 'negative' as MetricTone, detail: `${metrics.winning_trades} 赢 / ${metrics.losing_trades} 亏` },
    { label: '交易次数', value: String(metrics.trade_count), tone: 'accent' as MetricTone, detail: `平均持仓 ${number(metrics.avg_holding_days, 1)} 天` }
  ]
})

const secondaryMetrics = computed(() => {
  const metrics = run.value?.metrics
  if (!metrics) return []
  return [
    ['年化收益', percent(metrics.annual_return), tone(metrics.annual_return)],
    ['Sortino', number(metrics.sortino), tone(metrics.sortino)],
    ['Calmar', number(metrics.calmar), tone(metrics.calmar)],
    ['盈亏比', number(metrics.profit_loss_ratio), tone(metrics.profit_loss_ratio - 1)],
    ['基准收益', percent(metrics.benchmark_return), tone(metrics.benchmark_return)],
    ['信息比率', number(metrics.information_ratio), tone(metrics.information_ratio)],
    ['Beta', number(metrics.beta), 'neutral' as MetricTone],
    ['Alpha 年化', percent(metrics.alpha_annual), tone(metrics.alpha_annual)],
    ['Alpha t-stat', number(metrics.alpha_t_stat), Math.abs(metrics.alpha_t_stat || 0) >= 2 ? 'positive' as MetricTone : 'negative' as MetricTone],
    ['最终权益', currency(metrics.final_equity), tone(metrics.final_equity - metrics.initial_cash)]
  ]
})

const confidenceMeta = computed(() => {
  const confidence = run.value?.confidence
  if (confidence === 'medium') {
    return { label: '中等可信度', description: '样本量达到初步观察要求，仍需滚动窗口和样本外验证。', tone: 'medium' }
  }
  if (confidence === 'low') {
    return { label: '低可信度', description: '样本或交易次数偏少，当前指标只能作为策略线索，不能作为稳定性结论。', tone: 'low' }
  }
  return { label: '样本不足', description: '当前样本不足以形成统计判断，请扩大回测区间后再比较。', tone: 'insufficient' }
})

const totalProfit = computed(() => {
  if (!run.value) return 0
  return run.value.metrics.final_equity - run.value.metrics.initial_cash
})

const tabs: Array<{ key: ReportTab; label: string; detail: string }> = [
  { key: 'chart', label: '图表分析', detail: 'K线 / 指标 / 净值' },
  { key: 'trades', label: '逐笔交易', detail: '成交与费用' },
  { key: 'attribution', label: '归因检验', detail: '稳健性与风险' },
  { key: 'code', label: '代码参数', detail: '可复现配置' }
]

function destroyCharts() {
  resizeObserver?.disconnect()
  resizeObserver = null
  priceChart?.remove()
  equityChart?.remove()
  macdChart?.remove()
  priceChart = null
  equityChart = null
  macdChart = null
}

function baseChart(host: HTMLElement, height: number) {
  return createChart(host, {
    width: host.clientWidth || 1000,
    height,
    layout: {
      background: { type: ColorType.Solid, color: '#101315' },
      textColor: '#8f979d',
      fontFamily: '"Cascadia Mono", "SFMono-Regular", Consolas, monospace',
      fontSize: 11
    },
    grid: {
      vertLines: { color: 'rgba(112, 123, 130, 0.08)' },
      horzLines: { color: 'rgba(112, 123, 130, 0.10)' }
    },
    crosshair: {
      vertLine: { color: 'rgba(77, 208, 190, .45)', labelBackgroundColor: '#197f73' },
      horzLine: { color: 'rgba(77, 208, 190, .45)', labelBackgroundColor: '#197f73' }
    },
    rightPriceScale: { borderColor: '#2a3034' },
    timeScale: { borderColor: '#2a3034', timeVisible: false },
    localization: { locale: 'zh-CN' }
  })
}

function renderCharts() {
  if (!run.value || activeTab.value !== 'chart' || !priceHost.value || !equityHost.value || !macdHost.value) return
  destroyCharts()

  priceChart = baseChart(priceHost.value, 430)
  const candles = priceChart.addSeries(CandlestickSeries, {
    upColor: '#20c997',
    downColor: '#f25f64',
    borderUpColor: '#20c997',
    borderDownColor: '#f25f64',
    wickUpColor: '#20c997',
    wickDownColor: '#f25f64'
  })
  candles.setData(run.value.price_series.map((item) => ({
    time: item.time as Time,
    open: item.open,
    high: item.high,
    low: item.low,
    close: item.close
  })))
  createSeriesMarkers(candles, run.value.trade_markers.map((marker) => ({
    ...marker,
    time: marker.time as Time,
    color: marker.shape === 'arrowUp' ? '#42dfad' : '#ff6b70'
  })))
  const volume = priceChart.addSeries(HistogramSeries, {
    priceFormat: { type: 'volume' },
    priceScaleId: 'volume',
    priceLineVisible: false,
    lastValueVisible: false
  })
  priceChart.priceScale('volume').applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } })
  volume.setData(run.value.price_series.map((item) => ({
    time: item.time as Time,
    value: item.volume,
    color: item.close >= item.open ? 'rgba(32,201,151,.25)' : 'rgba(242,95,100,.25)'
  })))
  priceChart.timeScale().fitContent()

  macdChart = baseChart(macdHost.value, 220)
  const macd = macdChart.addSeries(LineSeries, { color: '#eabf63', lineWidth: 2, priceLineVisible: false })
  const signal = macdChart.addSeries(LineSeries, { color: '#49b8d1', lineWidth: 2, priceLineVisible: false })
  const histogram = macdChart.addSeries(HistogramSeries, { priceLineVisible: false })
  macd.setData(run.value.price_series.map((item) => ({ time: item.time as Time, value: item.macd })))
  signal.setData(run.value.price_series.map((item) => ({ time: item.time as Time, value: item.signal })))
  histogram.setData(run.value.price_series.map((item) => ({
    time: item.time as Time,
    value: item.histogram,
    color: item.histogram >= 0 ? 'rgba(32,201,151,.58)' : 'rgba(242,95,100,.58)'
  })))
  macdChart.timeScale().fitContent()

  equityChart = baseChart(equityHost.value, 280)
  const equity = equityChart.addSeries(LineSeries, {
    color: '#dfb65c',
    lineWidth: 2,
    priceLineVisible: false
  })
  const drawdown = equityChart.addSeries(LineSeries, {
    color: '#f25f64',
    lineWidth: 2,
    priceScaleId: 'drawdown',
    priceLineVisible: false,
    lastValueVisible: true
  })
  equityChart.priceScale('drawdown').applyOptions({ scaleMargins: { top: 0.72, bottom: 0.04 } })
  equity.setData(run.value.equity_curve.map((item) => ({ time: item.time as Time, value: item.equity })))
  drawdown.setData(run.value.equity_curve.map((item) => ({ time: item.time as Time, value: item.drawdown * 100 })))
  equityChart.timeScale().fitContent()

  resizeObserver = new ResizeObserver(() => {
    if (priceChart && priceHost.value) priceChart.applyOptions({ width: priceHost.value.clientWidth })
    if (macdChart && macdHost.value) macdChart.applyOptions({ width: macdHost.value.clientWidth })
    if (equityChart && equityHost.value) equityChart.applyOptions({ width: equityHost.value.clientWidth })
  })
  resizeObserver.observe(priceHost.value)
  resizeObserver.observe(macdHost.value)
  resizeObserver.observe(equityHost.value)
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    run.value = await api.backtest(props.runId)
    await nextTick()
    renderCharts()
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '完整回测报告加载失败'
  } finally {
    loading.value = false
  }
}

function openTab(tab: ReportTab) {
  activeTab.value = tab
  if (tab === 'chart') void nextTick(renderCharts)
}

function download(kind: 'trades.csv' | 'metrics.json') {
  void api.downloadBacktestFile(props.runId, kind)
}

watch(() => props.runId, load)
onMounted(load)
onBeforeUnmount(destroyCharts)
</script>

<template>
  <section class="backtest-report">
    <header class="report-toolbar">
      <button type="button" class="back-button" @click="emit('back')" aria-label="返回数字人中心">←</button>
      <div v-if="run" class="report-identity">
        <div class="report-title-line">
          <span class="strategy-tag">策略回测</span>
          <h2>{{ run.strategy.name }}</h2>
          <strong>{{ run.symbol }}</strong>
        </div>
        <p>{{ run.period.start_date }} — {{ run.period.end_date }} · 日线 · {{ run.period.trading_days }} 个交易日</p>
      </div>
      <div v-if="run" class="report-actions">
        <button type="button" @click="download('trades.csv')">导出交易 CSV</button>
        <button type="button" @click="download('metrics.json')">导出指标 JSON</button>
      </div>
    </header>

    <div v-if="loading" class="report-state">
      <span class="loading-mark"></span>
      正在生成量化报告视图
    </div>
    <div v-else-if="error" class="notice notice-danger">
      <strong>报告加载失败</strong>
      <span>{{ error }}</span>
    </div>

    <template v-else-if="run">
      <section class="performance-overview">
        <div class="performance-lead">
          <span>策略区间收益</span>
          <strong :class="tone(run.metrics.total_return)">{{ percent(run.metrics.total_return) }}</strong>
          <p>
            期末权益 {{ currency(run.metrics.final_equity) }}
            <b :class="tone(totalProfit)">{{ totalProfit >= 0 ? '盈利' : '亏损' }} {{ currency(totalProfit) }}</b>
          </p>
        </div>
        <div class="benchmark-comparison">
          <div>
            <span>策略</span>
            <strong :class="tone(run.metrics.total_return)">{{ percent(run.metrics.total_return) }}</strong>
          </div>
          <div>
            <span>沪深300</span>
            <strong :class="tone(run.metrics.benchmark_return)">{{ percent(run.metrics.benchmark_return) }}</strong>
          </div>
          <div class="excess">
            <span>超额</span>
            <strong :class="tone(run.metrics.excess_return)">{{ percent(run.metrics.excess_return) }}</strong>
          </div>
        </div>
        <div class="confidence-panel" :class="confidenceMeta.tone">
          <span>统计结论</span>
          <strong>{{ confidenceMeta.label }}</strong>
          <p>{{ confidenceMeta.description }}</p>
        </div>
      </section>

      <section class="primary-metrics" aria-label="核心回测指标">
        <article v-for="metric in primaryMetrics" :key="metric.label">
          <span>{{ metric.label }}</span>
          <strong :class="metric.tone">{{ metric.value }}</strong>
          <small>{{ metric.detail }}</small>
        </article>
      </section>

      <nav class="report-tabs" aria-label="回测报告栏目">
        <button
          v-for="tab in tabs"
          :key="tab.key"
          type="button"
          :class="{ active: activeTab === tab.key }"
          @click="openTab(tab.key)"
        >
          <strong>{{ tab.label }}</strong>
          <span>{{ tab.detail }}</span>
        </button>
      </nav>

      <div v-show="activeTab === 'chart'" class="chart-workspace">
        <article class="chart-panel price-panel">
          <header>
            <div>
              <span>PRICE ACTION</span>
              <strong>{{ run.symbol }} K线与成交量</strong>
            </div>
            <div class="chart-legend">
              <span class="buy">● 买入</span>
              <span class="sell">● 卖出</span>
            </div>
          </header>
          <div ref="priceHost" class="report-chart price"></div>
        </article>

        <div class="chart-grid">
          <article class="chart-panel">
            <header>
              <div>
                <span>MOMENTUM</span>
                <strong>MACD 动量结构</strong>
              </div>
              <div class="chart-legend"><span class="dif">DIF</span><span class="dea">DEA</span></div>
            </header>
            <div ref="macdHost" class="report-chart macd"></div>
          </article>
          <article class="chart-panel">
            <header>
              <div>
                <span>RISK PATH</span>
                <strong>净值与动态回撤</strong>
              </div>
              <div class="chart-legend"><span class="equity">净值</span><span class="sell">回撤%</span></div>
            </header>
            <div ref="equityHost" class="report-chart equity"></div>
          </article>
        </div>

        <section class="secondary-metrics">
          <div v-for="[label, value, metricTone] in secondaryMetrics" :key="label">
            <span>{{ label }}</span>
            <strong :class="metricTone">{{ value }}</strong>
          </div>
        </section>

        <section class="execution-notes">
          <div>
            <span>执行口径</span>
            <strong>收盘确认信号，下一交易日开盘执行</strong>
          </div>
          <p>已模拟 A 股 T+1、100 股整手、佣金最低 5 元、印花税、过户费与滑点，避免未来函数。</p>
        </section>
      </div>

      <div v-if="activeTab === 'trades'" class="trade-section">
        <header class="section-heading">
          <div><span>EXECUTION LEDGER</span><h3>逐笔交易与成本</h3></div>
          <p>共 {{ run.trades.length }} 笔完整交易 · 费用已计入最终收益</p>
        </header>
        <div class="table-shell">
          <table>
            <thead>
              <tr>
                <th>#</th><th>方向</th><th>入场日</th><th>出场日</th><th>入场价</th><th>出场价</th>
                <th>数量</th><th>持仓</th><th>费用</th><th>净盈亏</th><th>收益率</th><th>出场原因</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(trade, index) in run.trades" :key="`${trade.entry_date}-${index}`">
                <td class="sequence">{{ String(index + 1).padStart(2, '0') }}</td>
                <td><span class="direction-tag">做多</span></td>
                <td>{{ trade.entry_date }}</td><td>{{ trade.exit_date }}</td>
                <td>{{ number(trade.entry_price, 3) }}</td><td>{{ number(trade.exit_price, 3) }}</td>
                <td>{{ trade.quantity.toLocaleString('zh-CN') }}</td><td>{{ trade.holding_days }} 天</td>
                <td>{{ currency(trade.fees) }}</td><td :class="tone(trade.pnl)">{{ currency(trade.pnl) }}</td>
                <td :class="tone(trade.return_pct)">{{ percent(trade.return_pct) }}</td><td>{{ tradeReason(trade.exit_reason) }}</td>
              </tr>
              <tr v-if="!run.trades.length"><td colspan="12" class="empty-row">该区间没有形成完整交易。</td></tr>
            </tbody>
          </table>
        </div>
      </div>

      <div v-if="activeTab === 'attribution'" class="attribution-section">
        <header class="section-heading">
          <div><span>ROBUSTNESS AUDIT</span><h3>收益归因与稳健性检查</h3></div>
          <p>不只看收益，也检查收益从哪里来、是否依赖偶然交易</p>
        </header>
        <section class="attribution-metrics">
          <article>
            <span>最佳交易</span>
            <strong :class="tone(run.attribution.top_winner?.pnl)">
              {{ run.attribution.top_winner ? currency(run.attribution.top_winner.pnl) : '-' }}
            </strong>
            <small v-if="run.attribution.top_winner">{{ run.attribution.top_winner.entry_date }} → {{ run.attribution.top_winner.exit_date }}</small>
          </article>
          <article>
            <span>最差交易</span>
            <strong :class="tone(run.attribution.top_loser?.pnl)">
              {{ run.attribution.top_loser ? currency(run.attribution.top_loser.pnl) : '-' }}
            </strong>
            <small v-if="run.attribution.top_loser">{{ run.attribution.top_loser.entry_date }} → {{ run.attribution.top_loser.exit_date }}</small>
          </article>
          <article>
            <span>移除最佳交易后</span>
            <strong :class="tone(run.attribution.remove_top_winner_pnl)">{{ currency(run.attribution.remove_top_winner_pnl) }}</strong>
            <small>识别单笔幸运交易依赖</small>
          </article>
          <article>
            <span>Beta / Alpha t-stat</span>
            <strong>{{ number(run.metrics.beta) }} / {{ number(run.metrics.alpha_t_stat) }}</strong>
            <small>|t| ≥ 2 才具初步显著性</small>
          </article>
        </section>

        <section class="audit-grid">
          <article class="risk-list">
            <header><span>MODEL LIMITS</span><h3>风险与局限</h3></header>
            <ol><li v-for="warning in run.warnings" :key="warning">{{ warning }}</li></ol>
          </article>
          <article class="reason-table">
            <header><span>EXIT ATTRIBUTION</span><h3>出场原因分解</h3></header>
            <div class="table-shell">
              <table>
                <thead><tr><th>原因</th><th>笔数</th><th>总盈亏</th><th>平均盈亏</th><th>胜率</th></tr></thead>
                <tbody>
                  <tr v-for="row in run.attribution.exit_reasons" :key="row.reason">
                    <td>{{ tradeReason(row.reason) }}</td><td>{{ row.count }}</td>
                    <td :class="tone(row.total_pnl)">{{ currency(row.total_pnl) }}</td>
                    <td :class="tone(row.avg_pnl)">{{ currency(row.avg_pnl) }}</td><td>{{ percent(row.win_rate, false) }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </article>
        </section>
      </div>

      <div v-if="activeTab === 'code'" class="code-section">
        <article class="source-panel">
          <header><span>REPRODUCIBLE LOGIC</span><h3>实际执行逻辑</h3></header>
          <pre><code>{{ run.source_code }}</code></pre>
        </article>
        <article class="parameter-panel">
          <header><span>RUN MANIFEST</span><h3>参数与数据指纹</h3></header>
          <dl>
            <template v-for="(value, key) in run.config" :key="key">
              <dt>{{ key }}</dt><dd>{{ value }}</dd>
            </template>
          </dl>
          <div class="fingerprint">
            <span>SHA-256</span>
            <code>{{ run.data_fingerprint }}</code>
          </div>
        </article>
      </div>
    </template>
  </section>
</template>

<style scoped>
.backtest-report {
  --report-bg: #0b0d0f;
  --report-surface: #101315;
  --report-surface-2: #14181b;
  --report-border: #293035;
  --report-border-soft: #20262a;
  --report-text: #e7e4dc;
  --report-muted: #899197;
  --report-gold: #dfb65c;
  --report-cyan: #4dd0be;
  --report-green: #31d39f;
  --report-red: #ff6b70;
  display: grid;
  gap: 12px;
  min-width: 0;
  color: var(--report-text);
  font-family: "Microsoft YaHei UI", "PingFang SC", sans-serif;
}

button { font: inherit; }
.positive { color: var(--report-green) !important; }
.negative { color: var(--report-red) !important; }
.neutral { color: var(--report-text) !important; }
.accent { color: var(--report-gold) !important; }

.report-toolbar {
  display: grid;
  grid-template-columns: 40px minmax(0, 1fr) auto;
  align-items: center;
  gap: 14px;
  min-height: 72px;
  padding: 12px 16px;
  border: 1px solid var(--report-border);
  background: var(--report-surface);
}

.back-button {
  width: 36px;
  height: 36px;
  border: 1px solid #4b4432;
  background: #171710;
  color: var(--report-gold);
  font-size: 20px;
  cursor: pointer;
}

.back-button:hover,
.report-actions button:hover { border-color: var(--report-gold); color: #ffe4a0; }
.report-identity { min-width: 0; }
.report-title-line { display: flex; align-items: center; gap: 10px; min-width: 0; }
.report-title-line h2 { overflow: hidden; margin: 0; color: #f3eee1; font-size: 18px; text-overflow: ellipsis; white-space: nowrap; }
.report-title-line > strong { color: var(--report-cyan); font-family: "Cascadia Mono", Consolas, monospace; font-size: 13px; }
.strategy-tag { padding: 4px 7px; border: 1px solid #5c4d2d; color: var(--report-gold); font-size: 10px; }
.report-identity p { margin: 5px 0 0; color: var(--report-muted); font-size: 11px; }
.report-actions { display: flex; gap: 8px; }
.report-actions button {
  min-height: 34px;
  padding: 0 11px;
  border: 1px solid #3a4247;
  background: #15191c;
  color: #c8cdd0;
  cursor: pointer;
}

.performance-overview {
  display: grid;
  grid-template-columns: minmax(260px, .9fr) minmax(320px, 1fr) minmax(280px, .8fr);
  align-items: stretch;
  border: 1px solid var(--report-border);
  background: linear-gradient(100deg, #13140f 0%, #101416 54%, #101315 100%);
}

.performance-lead,
.benchmark-comparison,
.confidence-panel { min-width: 0; padding: 20px 22px; }
.performance-lead { border-right: 1px solid var(--report-border); }
.performance-lead > span,
.confidence-panel > span,
.chart-panel header div > span,
.section-heading span,
.audit-grid header span,
.code-section header span {
  color: #858c90;
  font-size: 9px;
  letter-spacing: .14em;
}
.performance-lead > strong { display: block; margin-top: 4px; font-family: "Cascadia Mono", Consolas, monospace; font-size: 38px; line-height: 1.1; }
.performance-lead p { margin: 9px 0 0; color: var(--report-muted); font-size: 11px; }
.performance-lead p b { margin-left: 10px; font-weight: 600; }

.benchmark-comparison {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  align-items: center;
  border-right: 1px solid var(--report-border);
}
.benchmark-comparison div { display: grid; gap: 7px; padding: 2px 18px; border-right: 1px solid var(--report-border-soft); }
.benchmark-comparison div:last-child { border-right: 0; }
.benchmark-comparison span { color: var(--report-muted); font-size: 10px; }
.benchmark-comparison strong { font-family: "Cascadia Mono", Consolas, monospace; font-size: 19px; }
.benchmark-comparison .excess { background: rgba(77, 208, 190, .05); }

.confidence-panel { display: grid; align-content: center; gap: 6px; border-left: 3px solid var(--report-gold); }
.confidence-panel strong { color: var(--report-gold); font-size: 17px; }
.confidence-panel p { margin: 0; color: #abb0b2; font-size: 11px; line-height: 1.7; }
.confidence-panel.low,
.confidence-panel.insufficient { border-left-color: var(--report-red); background: rgba(242, 95, 100, .045); }
.confidence-panel.low strong,
.confidence-panel.insufficient strong { color: var(--report-red); }
.confidence-panel.medium { border-left-color: var(--report-cyan); }
.confidence-panel.medium strong { color: var(--report-cyan); }

.primary-metrics {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  border: 1px solid var(--report-border);
  background: var(--report-surface);
}
.primary-metrics article { display: grid; gap: 6px; min-width: 0; padding: 15px 16px; border-right: 1px solid var(--report-border-soft); }
.primary-metrics article:last-child { border-right: 0; }
.primary-metrics span,
.primary-metrics small { overflow: hidden; color: var(--report-muted); font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }
.primary-metrics strong { font-family: "Cascadia Mono", Consolas, monospace; font-size: 20px; }
.primary-metrics small { color: #666f74; }

.report-tabs {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  border: 1px solid var(--report-border);
  background: #0d1012;
}
.report-tabs button {
  display: grid;
  gap: 3px;
  min-height: 54px;
  padding: 9px 16px;
  border: 0;
  border-right: 1px solid var(--report-border-soft);
  border-bottom: 2px solid transparent;
  background: transparent;
  color: #a6adb1;
  text-align: left;
  cursor: pointer;
}
.report-tabs button:last-child { border-right: 0; }
.report-tabs button strong { font-size: 12px; }
.report-tabs button span { color: #626b70; font-size: 9px; }
.report-tabs button.active { border-bottom-color: var(--report-gold); background: #171710; color: #f0d58e; }
.report-tabs button.active span { color: #a89461; }

.chart-workspace,
.trade-section,
.attribution-section { display: grid; gap: 12px; }
.chart-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.2fr); gap: 12px; }
.chart-panel { min-width: 0; overflow: hidden; border: 1px solid var(--report-border); background: var(--report-surface); }
.chart-panel > header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  min-height: 52px;
  padding: 9px 14px;
  border-bottom: 1px solid var(--report-border-soft);
}
.chart-panel header > div:first-child { display: grid; gap: 3px; }
.chart-panel header strong { color: #dfe2df; font-size: 12px; }
.chart-legend { display: flex; gap: 12px; color: #778086; font-size: 9px; }
.chart-legend .buy { color: var(--report-green); }
.chart-legend .sell { color: var(--report-red); }
.chart-legend .dif,
.chart-legend .equity { color: var(--report-gold); }
.chart-legend .dea { color: #49b8d1; }
.report-chart { width: 100%; }

.secondary-metrics {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  border: 1px solid var(--report-border);
  background: #0e1113;
}
.secondary-metrics div { display: flex; align-items: center; justify-content: space-between; gap: 8px; min-width: 0; padding: 11px 13px; border-right: 1px solid var(--report-border-soft); border-bottom: 1px solid var(--report-border-soft); }
.secondary-metrics div:nth-child(5n) { border-right: 0; }
.secondary-metrics div:nth-last-child(-n + 5) { border-bottom: 0; }
.secondary-metrics span { overflow: hidden; color: var(--report-muted); font-size: 9px; text-overflow: ellipsis; white-space: nowrap; }
.secondary-metrics strong { color: #d5d9d8; font-family: "Cascadia Mono", Consolas, monospace; font-size: 12px; white-space: nowrap; }

.execution-notes {
  display: grid;
  grid-template-columns: minmax(280px, .8fr) minmax(0, 1.2fr);
  align-items: center;
  padding: 13px 16px;
  border-left: 3px solid var(--report-cyan);
  background: rgba(77, 208, 190, .055);
}
.execution-notes div { display: grid; gap: 3px; }
.execution-notes span { color: var(--report-cyan); font-size: 9px; letter-spacing: .1em; }
.execution-notes strong { color: #cdeee8; font-size: 11px; }
.execution-notes p { margin: 0; color: #8faaa7; font-size: 10px; line-height: 1.7; }

.section-heading {
  display: flex;
  align-items: end;
  justify-content: space-between;
  gap: 20px;
  padding: 15px 18px;
  border: 1px solid var(--report-border);
  background: var(--report-surface);
}
.section-heading h3,
.audit-grid h3,
.code-section h3 { margin: 4px 0 0; color: #e5e2d9; font-size: 15px; }
.section-heading p { margin: 0; color: var(--report-muted); font-size: 10px; }

.table-shell { min-width: 0; overflow: auto; }
.trade-section > .table-shell,
.reason-table .table-shell { border: 1px solid var(--report-border); background: var(--report-surface); }
table { width: 100%; border-collapse: collapse; font-size: 11px; }
th { position: sticky; top: 0; color: #aeb4b7; font-weight: 600; background: #171b1e; text-align: left; }
th, td { padding: 12px 13px; border-bottom: 1px solid var(--report-border-soft); white-space: nowrap; }
td { color: #bdc2c3; font-family: "Cascadia Mono", Consolas, monospace; }
tbody tr:hover { background: rgba(77, 208, 190, .035); }
.sequence { color: #667076; }
.direction-tag { padding: 3px 6px; border: 1px solid rgba(49, 211, 159, .3); color: var(--report-green); font-family: "Microsoft YaHei UI", sans-serif; }
.empty-row { padding: 60px; color: var(--report-muted); text-align: center; }

.attribution-metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); border: 1px solid var(--report-border); background: var(--report-surface); }
.attribution-metrics article { display: grid; gap: 7px; min-width: 0; padding: 17px; border-right: 1px solid var(--report-border-soft); }
.attribution-metrics article:last-child { border-right: 0; }
.attribution-metrics span,
.attribution-metrics small { color: var(--report-muted); font-size: 9px; }
.attribution-metrics strong { color: #e0ddd5; font-family: "Cascadia Mono", Consolas, monospace; font-size: 19px; }

.audit-grid { display: grid; grid-template-columns: minmax(320px, .75fr) minmax(0, 1.25fr); gap: 12px; }
.audit-grid > article { min-width: 0; border: 1px solid var(--report-border); background: var(--report-surface); }
.audit-grid article > header,
.code-section article > header { padding: 14px 16px; border-bottom: 1px solid var(--report-border-soft); }
.risk-list ol { display: grid; gap: 10px; margin: 0; padding: 16px 20px 18px 44px; color: #aeb4b4; font-size: 11px; line-height: 1.65; }
.risk-list li::marker { color: var(--report-red); font-family: "Cascadia Mono", Consolas, monospace; }

.code-section { display: grid; grid-template-columns: minmax(0, 1.35fr) minmax(300px, .65fr); gap: 12px; }
.code-section > article { min-width: 0; border: 1px solid var(--report-border); background: var(--report-surface); }
.source-panel pre { overflow: auto; min-height: 330px; max-height: 620px; margin: 0; padding: 18px; background: #090b0d; color: #bfe7dc; font-family: "Cascadia Mono", Consolas, monospace; font-size: 11px; line-height: 1.8; }
.parameter-panel dl { display: grid; grid-template-columns: 1fr 1fr; margin: 0; padding: 14px 16px; }
.parameter-panel dt,
.parameter-panel dd { margin: 0; padding: 8px 0; border-bottom: 1px solid var(--report-border-soft); font-size: 10px; }
.parameter-panel dt { color: var(--report-muted); }
.parameter-panel dd { overflow: hidden; color: #d6dad9; font-family: "Cascadia Mono", Consolas, monospace; text-align: right; text-overflow: ellipsis; }
.fingerprint { display: grid; gap: 7px; margin: 0 16px 16px; padding: 12px; border-left: 2px solid var(--report-cyan); background: #0c1012; }
.fingerprint span { color: var(--report-cyan); font-size: 9px; }
.fingerprint code { overflow-wrap: anywhere; color: #758086; font-size: 9px; line-height: 1.6; }

.report-state { display: flex; align-items: center; justify-content: center; gap: 10px; min-height: 300px; border: 1px solid var(--report-border); color: var(--report-muted); background: var(--report-surface); }
.loading-mark { width: 10px; height: 10px; border: 2px solid #495158; border-top-color: var(--report-gold); border-radius: 50%; animation: spin .8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

@media (max-width: 1280px) {
  .performance-overview { grid-template-columns: minmax(240px, .8fr) minmax(300px, 1fr); }
  .confidence-panel { grid-column: 1 / -1; border-top: 1px solid var(--report-border); border-left-width: 3px; }
  .primary-metrics { grid-template-columns: repeat(3, 1fr); }
  .primary-metrics article:nth-child(3) { border-right: 0; }
  .primary-metrics article:nth-child(-n + 3) { border-bottom: 1px solid var(--report-border-soft); }
  .secondary-metrics { grid-template-columns: repeat(2, 1fr); }
  .secondary-metrics div:nth-child(5n) { border-right: 1px solid var(--report-border-soft); }
  .secondary-metrics div:nth-child(2n) { border-right: 0; }
  .secondary-metrics div:nth-last-child(-n + 5) { border-bottom: 1px solid var(--report-border-soft); }
  .secondary-metrics div:nth-last-child(-n + 2) { border-bottom: 0; }
}

@media (max-width: 900px) {
  .report-toolbar { grid-template-columns: 40px minmax(0, 1fr); }
  .report-actions { grid-column: 1 / -1; }
  .performance-overview { grid-template-columns: 1fr; }
  .performance-lead,
  .benchmark-comparison { border-right: 0; border-bottom: 1px solid var(--report-border); }
  .chart-grid,
  .audit-grid,
  .code-section { grid-template-columns: 1fr; }
  .attribution-metrics { grid-template-columns: repeat(2, 1fr); }
  .attribution-metrics article:nth-child(2) { border-right: 0; }
  .attribution-metrics article:nth-child(-n + 2) { border-bottom: 1px solid var(--report-border-soft); }
}

@media (max-width: 640px) {
  .report-toolbar { padding: 10px; }
  .report-title-line { align-items: flex-start; flex-wrap: wrap; }
  .report-title-line h2 { width: calc(100% - 52px); font-size: 15px; }
  .report-actions { display: grid; grid-template-columns: 1fr 1fr; }
  .performance-lead > strong { font-size: 32px; }
  .benchmark-comparison { grid-template-columns: 1fr; gap: 9px; }
  .benchmark-comparison div { grid-template-columns: 1fr auto; border-right: 0; border-bottom: 1px solid var(--report-border-soft); }
  .benchmark-comparison div:last-child { border-bottom: 0; }
  .primary-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .primary-metrics article:nth-child(3) { border-right: 1px solid var(--report-border-soft); }
  .primary-metrics article:nth-child(2n) { border-right: 0; }
  .primary-metrics article:nth-child(-n + 4) { border-bottom: 1px solid var(--report-border-soft); }
  .primary-metrics strong { font-size: 17px; }
  .report-tabs button { min-height: 44px; padding: 8px; text-align: center; }
  .report-tabs button span { display: none; }
  .chart-panel > header,
  .section-heading { align-items: flex-start; flex-direction: column; }
  .secondary-metrics { grid-template-columns: 1fr; }
  .secondary-metrics div { border-right: 0; border-bottom: 1px solid var(--report-border-soft) !important; }
  .secondary-metrics div:last-child { border-bottom: 0 !important; }
  .execution-notes { grid-template-columns: 1fr; gap: 8px; }
  .attribution-metrics { grid-template-columns: 1fr; }
  .attribution-metrics article { border-right: 0; border-bottom: 1px solid var(--report-border-soft); }
  .attribution-metrics article:last-child { border-bottom: 0; }
}
</style>

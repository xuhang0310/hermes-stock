<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  CandlestickSeries,
  ColorType,
  HistogramSeries,
  LineSeries,
  LineStyle,
  createChart
} from 'lightweight-charts'
import type { CreatePriceLineOptions, Time } from 'lightweight-charts'
import { api } from '../api'
import type { ChartBar, ChartResolution, PositionChartPayload, PositionItem } from '../types'

const props = defineProps<{
  position: PositionItem | null
  instrument?: { symbol: string; name: string } | null
  backLabel?: string
}>()

const emit = defineEmits<{
  back: []
}>()

const chartHost = ref<HTMLElement | null>(null)
const payload = ref<PositionChartPayload | null>(null)
const loading = ref(false)
const error = ref('')
const selectedResolution = ref<ChartResolution>('1D')

const resolutionOptions: Array<{ value: ChartResolution; label: string; limit: number }> = [
  { value: '1D', label: '日线', limit: 300 },
  { value: '1', label: '1分', limit: 720 },
  { value: '5', label: '5分', limit: 720 },
  { value: '15', label: '15分', limit: 720 },
  { value: '30', label: '30分', limit: 720 },
  { value: '60', label: '60分', limit: 720 }
]

let chart: ReturnType<typeof createChart> | null = null
let resizeObserver: ResizeObserver | null = null
let currentMinuteBarsInOneDay = false

const chartTimeFormatter = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hourCycle: 'h23'
})

const symbol = computed(() => props.position?.symbol || props.instrument?.symbol || '')
const activeResolution = computed(() => resolutionOptions.find((item) => item.value === selectedResolution.value) || resolutionOptions[0])
const latestBar = computed(() => {
  const bars = payload.value?.bars || []
  return bars.length ? bars[bars.length - 1] : null
})

function formatNumber(value: number | null | undefined, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-'
  return Number(value).toLocaleString('zh-CN', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits
  })
}

function formatPercent(value: number | null | undefined) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-'
  return `${formatNumber(value, 2)}%`
}

function pad2(value: number | string) {
  return String(value).padStart(2, '0')
}

function timeParts(time: Time) {
  if (typeof time === 'number') {
    const parts = chartTimeFormatter.formatToParts(new Date(time * 1000))
    const pick = (type: string) => parts.find((item) => item.type === type)?.value || '00'
    return {
      year: pick('year'),
      month: pick('month'),
      day: pick('day'),
      hour: pick('hour'),
      minute: pick('minute')
    }
  }

  if (typeof time === 'string') {
    const [datePart, timePart = '00:00:00'] = time.split(' ')
    const [year = '0000', month = '00', day = '00'] = datePart.split('-')
    const [hour = '00', minute = '00'] = timePart.split(':')
    return {
      year,
      month: pad2(month),
      day: pad2(day),
      hour: pad2(hour),
      minute: pad2(minute)
    }
  }

  return {
    year: String(time.year),
    month: pad2(time.month),
    day: pad2(time.day),
    hour: '00',
    minute: '00'
  }
}

function minuteBarsInOneDay(bars: ChartBar[]) {
  if (selectedResolution.value === '1D' || bars.length < 2) return false
  const first = timeParts(bars[0].time as Time)
  const last = timeParts(bars[bars.length - 1].time as Time)
  return `${first.year}-${first.month}-${first.day}` === `${last.year}-${last.month}-${last.day}`
}

function formatAxisTime(time: Time) {
  const parts = timeParts(time)
  if (selectedResolution.value === '1D') {
    return `${parts.month}-${parts.day}`
  }
  if (currentMinuteBarsInOneDay) {
    return `${parts.hour}:${parts.minute}`
  }
  return `${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`
}

function formatCrosshairTime(time: Time) {
  const parts = timeParts(time)
  if (selectedResolution.value === '1D') {
    return `${parts.year}-${parts.month}-${parts.day}`
  }
  return `${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`
}

async function loadChart(refresh = false) {
  if (!symbol.value) {
    error.value = '请选择一个标的'
    return
  }
  loading.value = true
  error.value = ''
  try {
    payload.value = await api.positionChart(symbol.value, {
      limit: activeResolution.value.limit,
      refresh,
      resolution: selectedResolution.value
    })
    error.value = payload.value.error || ''
    await nextTick()
    renderChart()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '图表加载失败'
    payload.value = null
    destroyChart()
  } finally {
    loading.value = false
  }
}

function changeResolution(value: ChartResolution) {
  if (loading.value || selectedResolution.value === value) return
  selectedResolution.value = value
  void loadChart(false)
}

function destroyChart() {
  resizeObserver?.disconnect()
  resizeObserver = null
  chart?.remove()
  chart = null
}

function renderChart() {
  const host = chartHost.value
  const data = payload.value
  if (!host || !data?.bars.length) {
    destroyChart()
    return
  }

  destroyChart()
  host.replaceChildren()
  currentMinuteBarsInOneDay = minuteBarsInOneDay(data.bars)
  chart = createChart(host, {
    width: host.clientWidth || 900,
    height: host.clientHeight || 520,
    layout: {
      background: { type: ColorType.Solid, color: '#080809' },
      textColor: '#d7d1be'
    },
    grid: {
      vertLines: { color: 'rgba(76, 68, 48, 0.22)' },
      horzLines: { color: 'rgba(76, 68, 48, 0.22)' }
    },
    crosshair: {
      mode: 0
    },
    rightPriceScale: {
      borderColor: '#4c4430',
      scaleMargins: { top: 0.08, bottom: 0.24 }
    },
    timeScale: {
      borderColor: '#4c4430',
      timeVisible: selectedResolution.value !== '1D',
      secondsVisible: false,
      tickMarkFormatter: (time: Time) => formatAxisTime(time)
    },
    localization: {
      locale: 'zh-CN',
      timeFormatter: (time: Time) => formatCrosshairTime(time)
    }
  })

  const candleSeries = chart.addSeries(CandlestickSeries, {
    upColor: '#ff5d5d',
    downColor: '#27c58a',
    borderUpColor: '#ff5d5d',
    borderDownColor: '#27c58a',
    wickUpColor: '#ff5d5d',
    wickDownColor: '#27c58a'
  })
  candleSeries.setData(data.bars.map(toCandle))

  const volumeSeries = chart.addSeries(HistogramSeries, {
    priceFormat: { type: 'volume' },
    priceScaleId: 'volume',
    priceLineVisible: false,
    lastValueVisible: false
  })
  chart.priceScale('volume').applyOptions({
    scaleMargins: { top: 0.78, bottom: 0 }
  })
  volumeSeries.setData(
    data.bars.map((bar) => ({
      time: bar.time as Time,
      value: bar.volume || 0,
      color: bar.close >= bar.open ? 'rgba(255, 93, 93, 0.34)' : 'rgba(39, 197, 138, 0.34)'
    }))
  )

  addMaLine('ma5', '#f1d48a')
  addMaLine('ma10', '#2bd4d4')
  addMaLine('ma20', '#f254b8')
  addMaLine('ma60', '#9d9a8f')

  addPositionLine(candleSeries, data.position?.cost_price, '成本', '#f1d48a')
  addPositionLine(candleSeries, data.position?.stop_price, '止损', '#27c58a')
  addPositionLine(candleSeries, data.position?.take_profit_price, '止盈', '#ff5d5d')

  chart.timeScale().fitContent()
  resizeObserver = new ResizeObserver(([entry]) => {
    if (!chart || !entry) return
    chart.applyOptions({
      width: Math.floor(entry.contentRect.width),
      height: Math.floor(entry.contentRect.height)
    })
  })
  resizeObserver.observe(host)
}

function toCandle(bar: ChartBar) {
  return {
    time: bar.time as Time,
    open: bar.open,
    high: bar.high,
    low: bar.low,
    close: bar.close
  }
}

function addMaLine(key: string, color: string) {
  if (!chart || !payload.value?.overlays[key]?.length) return
  const series = chart.addSeries(LineSeries, {
    color,
    lineWidth: 1,
    priceLineVisible: false,
    lastValueVisible: false
  })
  series.setData(payload.value.overlays[key].map((point) => ({ ...point, time: point.time as Time })))
}

function addPositionLine(series: { createPriceLine: (options: CreatePriceLineOptions) => unknown }, price: number | null | undefined, title: string, color: string) {
  if (price === null || price === undefined || Number.isNaN(Number(price))) return
  series.createPriceLine({
    price,
    color,
    lineWidth: 1,
    lineStyle: LineStyle.Dashed,
    axisLabelVisible: true,
    title
  })
}

watch(symbol, () => {
  void loadChart(false)
}, { immediate: true })

onMounted(() => {
  if (payload.value) renderChart()
})

onBeforeUnmount(destroyChart)
</script>

<template>
  <section class="position-chart-page page-section">
    <header class="section-header">
      <div>
        <h2>{{ payload?.name || position?.name || instrument?.name || symbol }} · A股图表</h2>
        <p>{{ symbol }} · {{ activeResolution.label }} · {{ payload?.source || 'TuShare / 本地缓存' }}</p>
      </div>
      <div class="row-actions">
        <button type="button" @click="emit('back')">{{ backLabel || '返回持仓' }}</button>
        <button type="button" :disabled="loading || !symbol" @click="loadChart(true)">
          {{ loading ? '同步中' : '刷新TuShare' }}
        </button>
      </div>
    </header>

    <div class="chart-toolbar">
      <div class="chart-resolution-tabs" aria-label="K线周期">
        <button
          v-for="option in resolutionOptions"
          :key="option.value"
          type="button"
          :class="{ active: selectedResolution === option.value }"
          :disabled="loading"
          @click="changeResolution(option.value)"
        >
          {{ option.label }}
        </button>
      </div>
      <span>分钟线使用 TuShare stk_mins；如果账号未开历史分钟权限，会在图表提示里显示错误。</span>
    </div>

    <div class="chart-summary-strip">
      <div>
        <span>周期</span>
        <strong>{{ activeResolution.label }}</strong>
      </div>
      <div>
        <span>最新收盘</span>
        <strong>{{ formatNumber(latestBar?.close) }}</strong>
      </div>
      <div>
        <span>涨跌幅</span>
        <strong :class="{ up: (latestBar?.change_pct || 0) >= 0, down: (latestBar?.change_pct || 0) < 0 }">
          {{ formatPercent(latestBar?.change_pct) }}
        </strong>
      </div>
      <div>
        <span>持仓</span>
        <strong>{{ formatNumber(position?.position_quantity ?? position?.quantity, 0) }}</strong>
      </div>
      <div>
        <span>成本 / 止损</span>
        <strong>{{ formatNumber(position?.cost_price) }} / {{ formatNumber(position?.stop_price) }}</strong>
      </div>
      <div>
        <span>浮盈亏</span>
        <strong :class="{ up: (position?.floating_pnl || 0) >= 0, down: (position?.floating_pnl || 0) < 0 }">
          {{ formatNumber(position?.floating_pnl) }}
        </strong>
      </div>
    </div>

    <div v-if="error" class="notice notice-danger chart-notice">
      <strong>图表提示</strong>
      <span>{{ error }}</span>
    </div>

    <div class="position-chart-layout">
      <div class="chart-panel">
        <div v-if="loading" class="chart-loading">正在加载 K 线...</div>
        <div v-else-if="!payload?.bars.length" class="chart-loading">暂无 K 线数据</div>
        <div ref="chartHost" class="tv-lite-chart" aria-label="A股K线图"></div>
      </div>
      <aside class="chart-side-panel">
        <h3>图层</h3>
        <div class="legend-row"><span class="legend ma5"></span>MA5</div>
        <div class="legend-row"><span class="legend ma10"></span>MA10</div>
        <div class="legend-row"><span class="legend ma20"></span>MA20</div>
        <div class="legend-row"><span class="legend cost"></span>成本线</div>
        <div class="legend-row"><span class="legend stop"></span>止损线</div>
        <h3>{{ position ? '持仓备注' : '标的信息' }}</h3>
        <p>{{ position?.thesis || position?.risk_note || (instrument ? `${instrument.name} · ${instrument.symbol}` : '暂无备注。') }}</p>
      </aside>
    </div>
  </section>
</template>

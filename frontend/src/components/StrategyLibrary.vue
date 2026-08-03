<script setup lang="ts">
import { onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { api } from '../api'
import type { StrategyDescriptor } from '../types'
import StockSymbolPicker from './StockSymbolPicker.vue'

const emit = defineEmits<{ selectStrategy: [payload: { instruction: string; label: string }] }>()

const loading = ref(true)
const catalogError = ref('')
const strategies = ref<StrategyDescriptor[]>([])
const explainOpen = ref<string | null>(null)
const symbolInputs = reactive<Record<string, string>>({})
const poolInputs = reactive<Record<string, string[]>>({})
const selectingCode = ref<string | null>(null)
const selectErrors = reactive<Record<string, string>>({})

const riskLabels: Record<string, string> = { low: '低风险', medium: '中风险', high: '高风险' }

const strategyColors: Record<string, string> = {
  macd: 'var(--gold)',
  ma_cross: 'var(--cyan)',
  bollinger: 'var(--pink)',
  rsi: 'var(--warning)',
  momentum: 'var(--ok)',
  turtle: '#e08a5a',
}

function strategyColor(code: string): string {
  return strategyColors[code] || 'var(--muted)'
}

function defaultPeriod(): { start: string; end: string } {
  const fmt = (d: Date) =>
    `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, '0')}${String(d.getDate()).padStart(2, '0')}`
  const end = new Date()
  const start = new Date()
  start.setDate(start.getDate() - 730)
  return { start: fmt(start), end: fmt(end) }
}

async function selectStrategy(strategy: StrategyDescriptor) {
  if (selectingCode.value) return
  selectingCode.value = strategy.code
  selectErrors[strategy.code] = ''
  try {
    const { start, end } = defaultPeriod()
    const period = `区间 ${start} 至 ${end}`
    let instruction: string
    let label: string
    if (strategy.kind === 'single') {
      const raw = (symbolInputs[strategy.code] || '').trim()
      if (!raw) {
        selectErrors[strategy.code] = '请填写股票名称或代码'
        return
      }
      let tsCode = raw
      let who = (strategy.symbol_labels || {})[raw] || ''
      if (!/^\d{6}\.(SH|SZ|BJ)$/.test(raw)) {
        try {
          const matched = (await api.searchStocks(raw)).items[0]
          if (!matched) {
            selectErrors[strategy.code] = `未找到“${raw}”，请从联想列表选择或检查名称`
            return
          }
          tsCode = matched.ts_code
          who = matched.name || who
        } catch {
          selectErrors[strategy.code] = '股票识别失败，请稍后重试'
          return
        }
      }
      const whom = who ? `${who}（${tsCode}）` : tsCode
      label = `${strategy.name} · ${whom}`
      instruction =
        `请用「${strategy.name}」策略对 ${whom} 做历史回测，${period}。` +
        '请直接调用 run_signal_backtest 工具执行，strategy 用 ' + strategy.code + '，不要追问、不要编写临时脚本。'
    } else {
      const pool = (poolInputs[strategy.code] || []).slice()
      if (pool.length < 3) {
        selectErrors[strategy.code] = '横截面策略至少需要 3 只股票'
        return
      }
      const defaults = strategy.default_symbols || []
      const isDefault = pool.length === defaults.length && pool.every((code, i) => code === defaults[i])
      label = `${strategy.name} · 默认股票池`
      if (isDefault) {
        instruction =
          `请用「${strategy.name}」（横截面动量 / 强者恒强）做历史回测，${period}，symbols 省略、使用该策略的默认股票池。` +
          `请直接调用 run_signal_backtest，strategy 用 ${strategy.code}，不要追问、不要编写临时脚本。`
      } else {
        label = `${strategy.name} · ${pool.length} 只股票`
        instruction =
          `请用「${strategy.name}」（横截面动量 / 强者恒强）做历史回测，${period}，` +
          `symbols 原样使用以下代码、不要增减：${pool.join('、')}。` +
          `请直接调用 run_signal_backtest，strategy 用 ${strategy.code}，不要追问、不要编写临时脚本。`
      }
    }
    emit('selectStrategy', { instruction, label })
  } catch (reason) {
    selectErrors[strategy.code] = reason instanceof Error ? reason.message : '准备回测失败，请稍后重试'
  } finally {
    selectingCode.value = null
  }
}

function toggleExplain(code: string, event: Event) {
  event.stopPropagation()
  explainOpen.value = explainOpen.value === code ? null : code
}

function onDocumentClick(event: MouseEvent) {
  if (!explainOpen.value) return
  const target = event.target as HTMLElement | null
  if (target && target.closest('.sl-help, .sl-popover')) return
  explainOpen.value = null
}

onMounted(async () => {
  document.addEventListener('click', onDocumentClick)
  try {
    const res = await api.strategyCatalog()
    strategies.value = res.items
    for (const strategy of strategies.value) {
      symbolInputs[strategy.code] = strategy.default_symbols[0] || ''
      poolInputs[strategy.code] = (strategy.default_symbols || []).slice()
    }
  } catch (reason) {
    catalogError.value = reason instanceof Error ? reason.message : '加载策略库失败'
  } finally {
    loading.value = false
  }
})

onBeforeUnmount(() => {
  document.removeEventListener('click', onDocumentClick)
})
</script>

<template>
  <section class="sl-wrap" @click.stop>
    <header class="sl-head">
      <span class="sl-head-mark"></span>
      <div>
        <h3>策略库</h3>
        <p>看不懂术语没关系——选一个策略，AI 会把回测结果讲给你听。</p>
      </div>
    </header>

    <div v-if="loading" class="sl-state">正在加载策略库…</div>
    <div v-else-if="catalogError" class="sl-state sl-error">{{ catalogError }}</div>

    <div v-else class="sl-grid">
      <article
        v-for="strategy in strategies"
        :key="strategy.code"
        class="sl-card"
        :style="{ '--c': strategyColor(strategy.code) }"
      >
        <div class="sl-card-top">
          <span class="sl-dot"></span>
          <h4>{{ strategy.name }}</h4>
          <button type="button" class="sl-help" title="它是什么？" @click="toggleExplain(strategy.code, $event)">?</button>
        </div>

        <p class="sl-desc">{{ strategy.description }}</p>

        <div class="sl-tags">
          <span class="sl-risk" :class="`risk-${strategy.risk_level}`">{{ riskLabels[strategy.risk_level] || strategy.risk_level }}</span>
          <span class="sl-kind">{{ strategy.kind === 'cross_sectional' ? '一篮子选股' : '单只股票' }}</span>
        </div>

        <StockSymbolPicker
          v-if="strategy.kind === 'single'"
          v-model="symbolInputs[strategy.code]"
          :labels="strategy.symbol_labels || {}"
          :disabled="selectingCode !== null"
          placeholder="股票名称或代码，如 平安银行 / 000001.SZ"
        />
        <StockSymbolPicker
          v-else
          v-model="poolInputs[strategy.code]"
          multiple
          :labels="strategy.symbol_labels || {}"
          :disabled="selectingCode !== null"
          placeholder="加减股票：名称或代码，如 茅台 / 600519.SH"
        />

        <button
          type="button"
          class="sl-run"
          :disabled="selectingCode !== null"
          @click="selectStrategy(strategy)"
        >
          <span v-if="selectingCode === strategy.code" class="sl-spin"></span>
          {{ selectingCode === strategy.code ? '解析中…' : '选择策略' }}
        </button>

        <p v-if="selectErrors[strategy.code]" class="sl-run-error">{{ selectErrors[strategy.code] }}</p>

        <div v-if="explainOpen === strategy.code" class="sl-popover">
          <strong>它是什么？</strong>
          <p>{{ strategy.explain }}</p>
          <p class="sl-popover-scenario">适合场景：{{ strategy.scenario }}</p>
        </div>
      </article>
    </div>
  </section>
</template>

<style scoped>
.sl-wrap {
  position: relative;
  width: 100%;
  text-align: left;
  margin-top: 22px;
}

.sl-head {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  margin-bottom: 14px;
}

.sl-head-mark {
  width: 3px;
  height: 34px;
  border-radius: 2px;
  background: linear-gradient(180deg, var(--gold-2), var(--gold-dim));
  flex: none;
  margin-top: 2px;
}

.sl-head h3 {
  margin: 0;
  font-size: 16px;
  color: var(--text);
  letter-spacing: 0.02em;
}

.sl-head p {
  margin: 4px 0 0;
  font-size: 12.5px;
  color: var(--muted);
}

.sl-state {
  padding: 24px;
  text-align: center;
  color: var(--muted);
  font-size: 13px;
  border: 1px dashed var(--line);
  border-radius: var(--radius);
}

.sl-error {
  color: var(--danger);
  border-color: rgba(255, 93, 93, 0.35);
}

.sl-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(232px, 1fr));
  gap: 12px;
  align-items: start;
}

.sl-card {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 14px;
  background: linear-gradient(180deg, var(--surface-2), var(--surface));
  border: 1px solid var(--line);
  border-radius: var(--radius);
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.04);
  transition: border-color 0.18s, transform 0.15s, box-shadow 0.18s;
  animation: sl-in 0.4s cubic-bezier(0.22, 0.61, 0.36, 1) both;
}

.sl-card::before {
  content: '';
  position: absolute;
  left: 0;
  top: 10px;
  bottom: 10px;
  width: 2px;
  border-radius: 1px;
  background: var(--c);
  opacity: 0.5;
  transition: opacity 0.18s;
}

.sl-card:hover {
  border-color: var(--line-strong);
  transform: translateY(-2px);
  box-shadow: 0 10px 28px rgba(0, 0, 0, 0.4), 0 0 22px -10px var(--c), inset 0 1px 0 rgba(255, 255, 255, 0.07);
}

.sl-card:hover::before {
  opacity: 1;
}

.sl-card-top {
  display: flex;
  align-items: center;
  gap: 8px;
}

.sl-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--c);
  flex: none;
}

.sl-card-top h4 {
  margin: 0;
  font-size: 14.5px;
  font-weight: 600;
  color: var(--text);
  flex: 1;
  min-width: 0;
}

.sl-help {
  width: 19px;
  height: 19px;
  border-radius: 50%;
  border: 1px solid var(--line-strong);
  background: transparent;
  color: var(--muted);
  font-size: 11px;
  line-height: 1;
  cursor: pointer;
  flex: none;
  transition: color 0.15s, border-color 0.15s;
}

.sl-help:hover {
  color: var(--gold-2);
  border-color: var(--gold);
}

.sl-desc {
  margin: 0;
  font-size: 12.5px;
  line-height: 1.55;
  color: rgba(242, 240, 232, 0.72);
  min-height: 3.1em;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.sl-tags {
  display: flex;
  align-items: center;
  gap: 6px;
}

.sl-risk,
.sl-kind {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 999px;
  border: 1px solid var(--line);
  color: var(--muted);
  background: rgba(255, 255, 255, 0.02);
}

.sl-risk.risk-low {
  color: var(--ok);
  border-color: rgba(39, 197, 138, 0.35);
}

.sl-risk.risk-medium {
  color: var(--warning);
  border-color: rgba(213, 166, 66, 0.35);
}

.sl-risk.risk-high {
  color: var(--danger);
  border-color: rgba(255, 93, 93, 0.35);
}

.sl-kind {
  color: rgba(242, 240, 232, 0.8);
  border-color: var(--line-strong);
}

.sl-input-row {
  display: flex;
}

.sl-input {
  width: 100%;
  padding: 7px 10px;
  background: var(--bg-2);
  border: 1px solid var(--line);
  border-radius: 6px;
  color: var(--text);
  font-size: 12.5px;
  font-family: ui-monospace, 'Cascadia Code', Consolas, monospace;
  outline: none;
  transition: border-color 0.15s, box-shadow 0.15s;
}

.sl-input:focus {
  border-color: var(--gold);
  box-shadow: 0 0 0 2px rgba(213, 180, 106, 0.14);
}

.sl-input:disabled {
  opacity: 0.55;
}

.sl-pool {
  font-size: 12px;
  color: var(--muted);
  padding: 7px 10px;
  border: 1px dashed var(--line);
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.015);
}

.sl-run {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 7px;
  padding: 8px 12px;
  border-radius: 6px;
  border: 1px solid var(--line-strong);
  background: rgba(213, 180, 106, 0.08);
  color: var(--gold-2);
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s, transform 0.12s;
}

.sl-run:hover:not(:disabled) {
  background: rgba(213, 180, 106, 0.16);
  border-color: var(--gold);
  transform: translateY(-1px);
}

.sl-run:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.sl-spin {
  width: 11px;
  height: 11px;
  border-radius: 50%;
  border: 2px solid currentColor;
  border-top-color: transparent;
  animation: sl-spin 0.7s linear infinite;
}

.sl-run-error {
  margin: 0;
  font-size: 12px;
  color: var(--danger);
  line-height: 1.5;
}

.sl-popover {
  position: absolute;
  top: calc(100% + 6px);
  left: 8px;
  right: 8px;
  z-index: 30;
  padding: 12px;
  background: var(--surface-3);
  border: 1px solid var(--line-strong);
  border-radius: var(--radius);
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.5);
  animation: sl-in 0.16s ease-out both;
}

.sl-popover strong {
  display: block;
  font-size: 12px;
  color: var(--gold-2);
  margin-bottom: 6px;
}

.sl-popover p {
  margin: 0;
  font-size: 12.5px;
  line-height: 1.65;
  color: var(--text);
}

.sl-popover-scenario {
  margin-top: 8px;
  color: var(--muted) !important;
  font-size: 12px !important;
}

@keyframes sl-in {
  from {
    opacity: 0;
    transform: translateY(6px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

@keyframes sl-spin {
  to {
    transform: rotate(360deg);
  }
}

@media (prefers-reduced-motion: reduce) {
  .sl-card,
  .sl-popover {
    animation: none;
  }
  .sl-card,
  .sl-run,
  .sl-help,
  .sl-input {
    transition: none;
  }
  .sl-spin {
    animation: none;
  }
}
</style>

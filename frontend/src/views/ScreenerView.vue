<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue'
import { api } from '../api'
import type {
  ScreenerItem,
  ScreenerPromptHistoryItem,
  ScreenerQuery,
  ScreenerResult,
  ScreenerSavedStrategy,
  ScreenerSyncStats,
  ScreenerSyncStatus,
  ScreenerTemplateItem,
  ScreenerTemplateRecommendation,
} from '../types'
import { renderMarkdown } from '../utils/markdown'

type TemplateCategory = '热门' | '量价' | '趋势' | '估值' | '质量'
type QuickTemplate = ScreenerTemplateItem & { category: TemplateCategory; prompt: string }
type ScreenerWorkspace = 'quick' | 'builder' | 'saved'

const emit = defineEmits<{
  watchlistAdded: [item: Awaited<ReturnType<typeof api.addWatchlist>>]
  askStock: [item: ScreenerItem]
  openBacktest: [runId: string]
  createAgentTask: [items: ScreenerItem[]]
}>()

const DRAFT_KEY = 'hermes_screener_prompt_draft'
const running = ref(false)
const error = ref('')
const result = ref<ScreenerResult | null>(null)
const ran = ref(false)
const lastQuery = ref<ScreenerQuery>({})

const market = ref('')
const priceMin = ref('')
const priceMax = ref('')
const pctChgMin = ref('')
const pctChgMax = ref('')
const volumeRatioMin = ref('')
const turnoverMin = ref('')
const turnoverMax = ref('')
const mvMin = ref('')
const mvMax = ref('')
const peMin = ref('')
const peMax = ref('')
const aboveMa = ref('')
const keyword = ref('')

const syncStatus = ref<ScreenerSyncStatus | null>(null)
const syncLoading = ref(false)
const syncError = ref('')
const syncStats = ref<ScreenerSyncStats | null>(null)

const nlText = ref('')
const parseLoading = ref(false)
const parseSummary = ref('')
const parseError = ref('')
const parsedQuery = ref<ScreenerQuery | null>(null)
const activeHistoryId = ref<number | null>(null)
const advancedOpen = ref(false)
const draftSaved = ref(false)
const activeWorkspace = ref<ScreenerWorkspace>('quick')
const resultSection = ref<HTMLElement | null>(null)
const ruleEditor = ref<HTMLElement | null>(null)
type ResultSort = 'score_desc' | 'pct_desc' | 'turnover_desc' | 'mv_asc' | 'pe_asc'
type ResultView = 'ranking' | 'factors' | 'compare'
type ResultColumn = 'value' | 'momentum' | 'liquidity' | 'reasons' | 'risks'
type ScoredScreenerItem = ScreenerItem & {
  compositeScore: number
  valueScore: number
  momentumScore: number
  liquidityScore: number
  reasons: string[]
  risks: string[]
  completeness: number
}
const resultSort = ref<ResultSort>('score_desc')
const resultView = ref<ResultView>('ranking')
const resultSearch = ref('')
const resultSearchOpen = ref(false)
const resultColumnsOpen = ref(false)
const visibleResultColumns = ref<ResultColumn[]>(['value', 'momentum', 'liquidity', 'reasons', 'risks'])
const resultPage = ref(1)
const resultPageSize = 8
const resultLimit = ref(20)
const selectedSymbol = ref('')
const checkedSymbols = ref<string[]>([])
const comparisonSymbols = ref<string[]>([])
const ruleEditorOpen = ref(false)
const lastRunAt = ref('')
const scoreValueWeight = ref(40)
const scoreMomentumWeight = ref(40)
const scoreLiquidityWeight = ref(20)
const backtestLoading = ref(false)
const backtestError = ref('')
const bulkWatchlistAdding = ref(false)
const watchlistState = ref<Record<string, 'adding' | 'added' | 'error'>>({})
const savedStrategies = ref<ScreenerSavedStrategy[]>([])
const savedLoading = ref(false)
const savedError = ref('')
const strategyName = ref('')
const strategySaving = ref(false)
const strategySaved = ref(false)
const activeSavedStrategyId = ref<number | null>(null)

const interpretLoading = ref(false)
const interpretation = ref('')
const interpretModel = ref('')
const interpretError = ref('')

const smartRec = ref<ScreenerTemplateRecommendation | null>(null)
const recLoading = ref(false)
const recError = ref('')

const promptHistory = ref<ScreenerPromptHistoryItem[]>([])
const historyTotal = ref(0)
const historyLoading = ref(false)
const historyError = ref('')
const historySearch = ref('')
const historyTab = ref<'全部' | '已收藏'>('全部')
const historyExpanded = ref(false)
const showAllTemplates = ref(false)
const profileDetailsOpen = ref(false)

const templateCategory = ref<TemplateCategory>('热门')
const TEMPLATE_CATEGORIES: TemplateCategory[] = ['热门', '量价', '趋势', '估值', '质量']
const TEMPLATES: QuickTemplate[] = [
  {
    name: '温和放量',
    category: '量价',
    prompt: '量比1.5以上，换手率3%–10%，涨幅1%–7%',
    desc: '量比≥1.5、换手率3–10%、涨幅1–7%',
    query: { volume_ratio_min: 1.5, turnover_rate_min: 3, turnover_rate_max: 10, pct_chg_min: 1, pct_chg_max: 7 },
  },
  {
    name: '小盘活跃',
    category: '热门',
    prompt: '总市值100亿以下，换手率3%以上，量比1.5以上',
    desc: '市值≤100亿、换手率≥3%、量比≥1.5',
    query: { total_mv_max: 100, turnover_rate_min: 3, volume_ratio_min: 1.5 },
  },
  {
    name: '低估值稳健',
    category: '估值',
    prompt: 'PE在0到25之间，总市值300亿以上，排除高估值标的',
    desc: '市值≥300亿、PE(TTM) 0–25',
    query: { total_mv_min: 300, pe_min: 0, pe_max: 25 },
  },
  {
    name: '站上20日线',
    category: '趋势',
    prompt: '收盘站上20日均线，并且今日上涨',
    desc: '站上20日均线且今日上涨',
    query: { above_ma: 20, pct_chg_min: 0 },
  },
  {
    name: '放量强势',
    category: '量价',
    prompt: '量比2以上，换手率5%–15%，今日涨幅2%以上',
    desc: '量比≥2、换手率5–15%、今日涨幅≥2%',
    query: { volume_ratio_min: 2, turnover_rate_min: 5, turnover_rate_max: 15, pct_chg_min: 2 },
  },
  {
    name: '趋势启动',
    category: '趋势',
    prompt: '站上10日均线，今日上涨，量比1.5以上',
    desc: '站上10日线、今日上涨、量比≥1.5',
    query: { above_ma: 10, pct_chg_min: 0, volume_ratio_min: 1.5 },
  },
  {
    name: '大盘低估值',
    category: '质量',
    prompt: '总市值500亿以上，PE在0到30之间，今日没有大幅上涨',
    desc: '市值≥500亿、PE 0–30、涨幅≤5%',
    query: { total_mv_min: 500, pe_min: 0, pe_max: 30, pct_chg_max: 5 },
  },
]

function toNumber(value: string | number): number | undefined {
  if (value === '' || value == null) return undefined
  const num = Number(value)
  return Number.isFinite(num) ? num : undefined
}

function fmtDate(value: string | null | undefined): string {
  if (!value) return '—'
  const digits = value.replace(/-/g, '')
  if (digits.length !== 8) return value
  return `${digits.slice(0, 4)}-${digits.slice(4, 6)}-${digits.slice(6, 8)}`
}

function fmtValue(value: number): string {
  return Number.isInteger(value) ? String(value) : String(value)
}

function addRangeChip(
  chips: string[],
  label: string,
  low: number | undefined,
  high: number | undefined,
  suffix = ''
) {
  if (low != null && high != null) chips.push(`${label} ${fmtValue(low)}–${fmtValue(high)}${suffix}`)
  else if (low != null) chips.push(`${label} ≥ ${fmtValue(low)}${suffix}`)
  else if (high != null) chips.push(`${label} ≤ ${fmtValue(high)}${suffix}`)
}

function queryToChips(query: ScreenerQuery): string[] {
  const chips: string[] = []
  if (query.market) chips.push({ SH: '沪市', SZ: '深市', BSE: '北交所' }[query.market] || query.market)
  addRangeChip(chips, '价格', query.price_min, query.price_max, '元')
  addRangeChip(chips, '涨跌幅', query.pct_chg_min, query.pct_chg_max, '%')
  if (query.volume_ratio_min != null) chips.push(`量比 ≥ ${fmtValue(query.volume_ratio_min)}`)
  addRangeChip(chips, '换手率', query.turnover_rate_min, query.turnover_rate_max, '%')
  addRangeChip(chips, '总市值', query.total_mv_min, query.total_mv_max, '亿')
  addRangeChip(chips, 'PE', query.pe_min, query.pe_max)
  if (query.above_ma) chips.push(`站上 MA${query.above_ma}`)
  if (query.keyword) chips.push(`关键词 ${query.keyword}`)
  if (query.limit) chips.push(`Top${query.limit}`)
  return chips
}

const statusLine = computed(() => {
  const status = syncStatus.value
  if (!status) return ''
  if (!status.latest_bar_date) return '尚未同步全市场数据'
  return `数据截至 ${fmtDate(status.latest_bar_date)} · 覆盖 ${status.bar_symbols.toLocaleString('zh-CN')} 只`
})
const personalizedTemplates = computed(() => (smartRec.value?.templates || []).slice(0, 3))
const visibleTemplates = computed(() => {
  if (showAllTemplates.value) return TEMPLATES
  if (templateCategory.value === '热门') return TEMPLATES.slice(0, 4)
  return TEMPLATES.filter((item) => item.category === templateCategory.value)
})
const profileTags = computed(() => {
  const basis = smartRec.value?.basis || ''
  const tags: string[] = []
  if (/小盘|中小|市值/.test(basis)) tags.push('中小市值')
  if (/换手|活跃|放量|量能/.test(basis)) tags.push('交易活跃')
  if (/估值偏高|高估值|PE偏高/.test(basis)) tags.push('估值偏高')
  else if (/低估|估值偏低/.test(basis)) tags.push('估值偏低')
  if (/均线|趋势|站上/.test(basis)) tags.push('趋势特征')
  return tags.slice(0, 4)
})
const visibleHistory = computed(() => {
  const keyword = historySearch.value.trim().toLowerCase()
  return promptHistory.value
    .filter((item) => historyTab.value === '全部' || item.is_favorite)
    .filter((item) => !keyword || item.text.toLowerCase().includes(keyword))
    .slice(0, historyExpanded.value ? 50 : 3)
})
const builderChips = computed(() => queryToChips(buildQuery()))
const hardFilterChips = computed(() => builderChips.value.filter((chip) => !chip.startsWith('Top')))
const hasBuilderConditions = computed(() => hardFilterChips.value.length > 0)
const activeStrategyName = computed(() => {
  if (strategyName.value.trim()) return strategyName.value.trim()
  const traits: string[] = []
  if (mvMax.value) traits.push('小市值')
  if (peMax.value) traits.push('低估值')
  if (aboveMa.value) traits.push(`MA${aboveMa.value}趋势`)
  if (volumeRatioMin.value || turnoverMin.value) traits.push('活跃')
  return `${traits.slice(0, 3).join('·') || '多因子'}选股策略`
})

function relativeScore(
  items: ScreenerItem[],
  item: ScreenerItem,
  pick: (row: ScreenerItem) => number | null,
  inverse = false
): number {
  const values = items.map(pick).filter((value): value is number => typeof value === 'number' && Number.isFinite(value))
  const current = pick(item)
  if (current == null || !Number.isFinite(current) || !values.length) return 45
  const min = Math.min(...values)
  const max = Math.max(...values)
  if (max === min) return 75
  const normalized = ((current - min) / (max - min)) * 100
  return inverse ? 100 - normalized : normalized
}

function explainItem(item: ScreenerItem): { reasons: string[]; risks: string[]; completeness: number } {
  const reasons: string[] = []
  const risks: string[] = []
  if (item.pe_ttm != null && item.pe_ttm > 0 && item.pe_ttm <= 30) reasons.push(`PE ${item.pe_ttm.toFixed(1)}，估值相对克制`)
  if (item.total_mv_yi != null && item.total_mv_yi <= 120) reasons.push(`市值 ${item.total_mv_yi.toFixed(1)} 亿，规模符合条件`)
  if (item.pct_chg != null && item.pct_chg > 0) reasons.push(`当日上涨 ${item.pct_chg.toFixed(2)}%，动量为正`)
  if (item.volume_ratio != null && item.volume_ratio >= 1.5) reasons.push(`量比 ${item.volume_ratio.toFixed(2)}，成交放大`)
  if (item.turnover_rate != null && item.turnover_rate >= 3) reasons.push(`换手率 ${item.turnover_rate.toFixed(2)}%，交易活跃`)
  if (item.ma_value != null && item.price != null && item.price >= item.ma_value) reasons.push(`价格站上 MA${aboveMa.value || ''}`.trim())
  if (!reasons.length) reasons.push('满足当前全部硬性筛选条件')

  if (item.pe_ttm == null || item.pe_ttm <= 0) risks.push('PE 数据缺失或不适用')
  else if (item.pe_ttm > 60) risks.push(`PE ${item.pe_ttm.toFixed(1)}，估值偏高`)
  if (item.turnover_rate != null && item.turnover_rate > 15) risks.push(`换手率 ${item.turnover_rate.toFixed(1)}%，短线交易过热`)
  if (item.pct_chg != null && Math.abs(item.pct_chg) >= 9.5) risks.push('当日波动接近涨跌停区间')
  if (item.total_mv_yi != null && item.total_mv_yi < 30) risks.push('市值较小，流动性与波动风险更高')
  if (!risks.length) risks.push('未触发当前快照的显著量价风险')

  const fields = [item.price, item.pct_chg, item.volume_ratio, item.turnover_rate, item.total_mv_yi, item.pe_ttm, item.ma_value]
  const completeness = Math.round((fields.filter((value) => value != null).length / fields.length) * 100)
  return { reasons: reasons.slice(0, 3), risks: risks.slice(0, 3), completeness }
}

const scoredResultItems = computed<ScoredScreenerItem[]>(() => {
  const items = result.value?.items || []
  const rawWeights = scoreValueWeight.value + scoreMomentumWeight.value + scoreLiquidityWeight.value || 100
  return items.map((item) => {
    const peScore = relativeScore(items, item, (row) => row.pe_ttm && row.pe_ttm > 0 ? row.pe_ttm : null, true)
    const sizeScore = relativeScore(items, item, (row) => row.total_mv_yi, true)
    const valueScore = peScore * 0.55 + sizeScore * 0.45
    const momentumScore = relativeScore(items, item, (row) => row.pct_chg)
    const volumeScore = relativeScore(items, item, (row) => row.volume_ratio)
    const turnoverScore = relativeScore(items, item, (row) => row.turnover_rate)
    const liquidityScore = volumeScore * 0.45 + turnoverScore * 0.55
    const compositeScore = (
      valueScore * scoreValueWeight.value +
      momentumScore * scoreMomentumWeight.value +
      liquidityScore * scoreLiquidityWeight.value
    ) / rawWeights
    const explanation = explainItem(item)
    return {
      ...item,
      valueScore: Math.round(valueScore * 10) / 10,
      momentumScore: Math.round(momentumScore * 10) / 10,
      liquidityScore: Math.round(liquidityScore * 10) / 10,
      compositeScore: Math.round(compositeScore * 10) / 10,
      ...explanation,
    }
  })
})

const allSortedResultItems = computed(() => {
  const items = [...scoredResultItems.value]
  const numberOr = (value: number | null | undefined, fallback: number) =>
    typeof value === 'number' && Number.isFinite(value) ? value : fallback
  const sorted = items.sort((a, b) => {
    if (resultSort.value === 'score_desc') return b.compositeScore - a.compositeScore
    if (resultSort.value === 'turnover_desc') return numberOr(b.turnover_rate, -Infinity) - numberOr(a.turnover_rate, -Infinity)
    if (resultSort.value === 'mv_asc') return numberOr(a.total_mv_yi, Infinity) - numberOr(b.total_mv_yi, Infinity)
    if (resultSort.value === 'pe_asc') return numberOr(a.pe_ttm, Infinity) - numberOr(b.pe_ttm, Infinity)
    return numberOr(b.pct_chg, -Infinity) - numberOr(a.pct_chg, -Infinity)
  })
  const search = resultSearch.value.trim().toLowerCase()
  if (!search) return sorted
  return sorted.filter((item) => `${item.symbol} ${item.name}`.toLowerCase().includes(search))
})
const resultPages = computed(() => Math.max(1, Math.ceil(allSortedResultItems.value.length / resultPageSize)))
const sortedResultItems = computed(() => {
  const safePage = Math.min(resultPage.value, resultPages.value)
  return allSortedResultItems.value.slice((safePage - 1) * resultPageSize, safePage * resultPageSize)
})
const checkedResultItems = computed(() =>
  allSortedResultItems.value.filter((item) => checkedSymbols.value.includes(item.symbol))
)
const allPageChecked = computed(() =>
  sortedResultItems.value.length > 0 && sortedResultItems.value.every((item) => checkedSymbols.value.includes(item.symbol))
)
const somePageChecked = computed(() =>
  sortedResultItems.value.some((item) => checkedSymbols.value.includes(item.symbol))
)
const selectedResultItem = computed(() =>
  allSortedResultItems.value.find((item) => item.symbol === selectedSymbol.value) || allSortedResultItems.value[0] || null
)
const comparedItems = computed(() => {
  const selected = allSortedResultItems.value.filter((item) => comparisonSymbols.value.includes(item.symbol))
  return selected.length ? selected : allSortedResultItems.value.slice(0, 3)
})
const resultFeatureDistribution = computed(() => {
  const items = scoredResultItems.value
  const total = items.length || 1
  return [
    { label: '估值规模', value: Math.round(items.filter((item) => item.valueScore >= 60).length / total * 100) },
    { label: '动量强势', value: Math.round(items.filter((item) => item.momentumScore >= 60).length / total * 100) },
    { label: '交易活跃', value: Math.round(items.filter((item) => item.liquidityScore >= 60).length / total * 100) },
    { label: '数据完整', value: Math.round(items.filter((item) => item.completeness >= 85).length / total * 100) },
  ]
})

function weightedContribution(score: number, weight: number): number {
  return Math.round(score * weight) / 100
}

const resultColumnOptions: Array<{ key: ResultColumn; label: string }> = [
  { key: 'value', label: '估值规模' },
  { key: 'momentum', label: '动量' },
  { key: 'liquidity', label: '流动性' },
  { key: 'reasons', label: '入选理由' },
  { key: 'risks', label: '主要风险' },
]

function isResultColumnVisible(column: ResultColumn): boolean {
  return visibleResultColumns.value.includes(column)
}

function toggleResultColumn(column: ResultColumn) {
  if (visibleResultColumns.value.includes(column)) {
    if (visibleResultColumns.value.length === 1) return
    visibleResultColumns.value = visibleResultColumns.value.filter((item) => item !== column)
    return
  }
  visibleResultColumns.value = [...visibleResultColumns.value, column]
}

function reasonTag(reason: string): string {
  if (reason.includes('PE')) return '估值合理'
  if (reason.includes('市值')) return '规模符合'
  if (reason.includes('上涨')) return '动量向上'
  if (reason.includes('量比')) return '成交放大'
  if (reason.includes('换手率')) return '交易活跃'
  if (reason.includes('均线')) return '趋势向上'
  return reason.split('，')[0]
}

async function loadRecommendation() {
  try {
    smartRec.value = await api.screenerTemplates()
  } catch {
    smartRec.value = null
  }
}

async function refreshRecommendation() {
  if (recLoading.value) return
  recLoading.value = true
  recError.value = ''
  try {
    smartRec.value = await api.screenerTemplatesRefresh()
  } catch (err) {
    recError.value = err instanceof Error ? err.message : String(err)
  } finally {
    recLoading.value = false
  }
}

async function loadPromptHistory() {
  if (historyLoading.value) return
  historyLoading.value = true
  historyError.value = ''
  try {
    const page = await api.screenerPromptHistory({ limit: 50 })
    promptHistory.value = page.items
    historyTotal.value = page.total
  } catch (err) {
    historyError.value = err instanceof Error ? err.message : String(err)
  } finally {
    historyLoading.value = false
  }
}

async function loadSavedStrategies() {
  if (savedLoading.value) return
  savedLoading.value = true
  savedError.value = ''
  try {
    savedStrategies.value = await api.screenerStrategies()
  } catch (err) {
    savedError.value = err instanceof Error ? err.message : String(err)
  } finally {
    savedLoading.value = false
  }
}

async function toggleFavorite(item: ScreenerPromptHistoryItem) {
  const previous = item.is_favorite
  item.is_favorite = !previous
  try {
    const updated = await api.screenerPromptFavorite(item.id, item.is_favorite)
    Object.assign(item, updated)
  } catch {
    item.is_favorite = previous
  }
}

async function usePrompt(text: string) {
  if (parseLoading.value) return
  nlText.value = text
  draftSaved.value = false
  parsedQuery.value = null
  activeHistoryId.value = null
  parseSummary.value = ''
  parseError.value = ''
  applyQuery({})
  result.value = null
  ran.value = false
  interpretation.value = ''
  interpretError.value = ''
  advancedOpen.value = true
  ruleEditorOpen.value = false
  activeWorkspace.value = 'builder'
  window.scrollTo({ top: 0, behavior: 'smooth' })
  await nextTick()
  await parseNL()
  if (!parsedQuery.value) {
    ruleEditorOpen.value = true
  }
}

function templatePrompt(tpl: ScreenerTemplateItem): string {
  return String(tpl.prompt || tpl.desc || tpl.name).trim()
}

function templateRationale(tpl: ScreenerTemplateItem): string {
  if (tpl.rationale) return tpl.rationale
  return smartRec.value?.basis ? `依据：${smartRec.value.basis}` : '依据：当前持仓风格分析'
}

function templateCount(tpl: ScreenerTemplateItem): string {
  return tpl.universe != null ? `候选 ${tpl.universe.toLocaleString('zh-CN')} 只` : '运行后查看命中数'
}

function historyTime(value: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value.replace('T', ' ')
  const now = new Date()
  const isToday = date.toDateString() === now.toDateString()
  const yesterday = new Date(now)
  yesterday.setDate(now.getDate() - 1)
  const hhmm = date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
  if (isToday) return `今天 ${hhmm}`
  if (date.toDateString() === yesterday.toDateString()) return `昨天 ${hhmm}`
  return `${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')} ${hhmm}`
}

async function loadSyncStatus() {
  try {
    syncStatus.value = await api.screenerSyncStatus()
  } catch {
    syncStatus.value = null
  }
}

async function syncNow() {
  if (syncLoading.value) return
  syncLoading.value = true
  syncError.value = ''
  syncStats.value = null
  try {
    syncStats.value = await api.screenerSync(20)
    result.value = null
    ran.value = false
    interpretation.value = ''
    interpretModel.value = ''
    await loadSyncStatus()
  } catch (err) {
    syncError.value = err instanceof Error ? err.message : String(err)
  } finally {
    syncLoading.value = false
  }
}

function applyQuery(query: ScreenerQuery) {
  market.value = query.market ?? ''
  priceMin.value = query.price_min != null ? String(query.price_min) : ''
  priceMax.value = query.price_max != null ? String(query.price_max) : ''
  pctChgMin.value = query.pct_chg_min != null ? String(query.pct_chg_min) : ''
  pctChgMax.value = query.pct_chg_max != null ? String(query.pct_chg_max) : ''
  volumeRatioMin.value = query.volume_ratio_min != null ? String(query.volume_ratio_min) : ''
  turnoverMin.value = query.turnover_rate_min != null ? String(query.turnover_rate_min) : ''
  turnoverMax.value = query.turnover_rate_max != null ? String(query.turnover_rate_max) : ''
  mvMin.value = query.total_mv_min != null ? String(query.total_mv_min) : ''
  mvMax.value = query.total_mv_max != null ? String(query.total_mv_max) : ''
  peMin.value = query.pe_min != null ? String(query.pe_min) : ''
  peMax.value = query.pe_max != null ? String(query.pe_max) : ''
  aboveMa.value = query.above_ma != null ? String(query.above_ma) : ''
  keyword.value = query.keyword ?? ''
  resultLimit.value = query.limit ?? 20
}

function buildQuery(): ScreenerQuery {
  return {
    market: market.value || undefined,
    price_min: toNumber(priceMin.value),
    price_max: toNumber(priceMax.value),
    pct_chg_min: toNumber(pctChgMin.value),
    pct_chg_max: toNumber(pctChgMax.value),
    volume_ratio_min: toNumber(volumeRatioMin.value),
    turnover_rate_min: toNumber(turnoverMin.value),
    turnover_rate_max: toNumber(turnoverMax.value),
    total_mv_min: toNumber(mvMin.value),
    total_mv_max: toNumber(mvMax.value),
    pe_min: toNumber(peMin.value),
    pe_max: toNumber(peMax.value),
    above_ma: aboveMa.value ? Number(aboveMa.value) : undefined,
    keyword: String(keyword.value).trim() || undefined,
    limit: resultLimit.value,
  }
}

function saveDraft() {
  localStorage.setItem(DRAFT_KEY, nlText.value)
  draftSaved.value = true
}

function clearPrompt() {
  nlText.value = ''
  parsedQuery.value = null
  activeHistoryId.value = null
  parseSummary.value = ''
  parseError.value = ''
  draftSaved.value = false
}

function newStrategy() {
  clearPrompt()
  applyQuery({})
  result.value = null
  ran.value = false
  interpretation.value = ''
  advancedOpen.value = false
  activeWorkspace.value = 'quick'
  strategyName.value = ''
  strategySaved.value = false
  activeSavedStrategyId.value = null
  selectedSymbol.value = ''
  checkedSymbols.value = []
  comparisonSymbols.value = []
  resultPage.value = 1
  lastRunAt.value = ''
  backtestError.value = ''
  window.scrollTo({ top: 0, behavior: 'smooth' })
}

function openBuilder() {
  activeWorkspace.value = 'builder'
  advancedOpen.value = true
  checkedSymbols.value = []
  comparisonSymbols.value = []
  void nextTick(() => window.scrollTo({ top: 0, behavior: 'smooth' }))
}

function openQuick() {
  activeWorkspace.value = 'quick'
  void nextTick(() => window.scrollTo({ top: 0, behavior: 'smooth' }))
}

function openSavedStrategies() {
  activeWorkspace.value = 'saved'
  void loadSavedStrategies()
  void nextTick(() => window.scrollTo({ top: 0, behavior: 'smooth' }))
}

function useSavedStrategy(item: ScreenerSavedStrategy, runImmediately = false) {
  applyQuery(item.query)
  parsedQuery.value = item.query
  parseSummary.value = ''
  result.value = null
  ran.value = false
  error.value = ''
  interpretation.value = ''
  strategyName.value = item.name
  strategySaved.value = true
  activeSavedStrategyId.value = item.id
  activeWorkspace.value = 'builder'
  advancedOpen.value = true
  void nextTick(async () => {
    window.scrollTo({ top: 0, behavior: 'smooth' })
    if (runImmediately) await run()
  })
}

async function saveCurrentStrategy() {
  const name = strategyName.value.trim()
  if (!name || !hasBuilderConditions.value || strategySaving.value) return
  strategySaving.value = true
  savedError.value = ''
  try {
    const saved = await api.screenerStrategySave({
      name,
      query: buildQuery(),
      last_result_count: result.value?.total ?? null,
    })
    strategyName.value = saved.name
    activeSavedStrategyId.value = saved.id
    strategySaved.value = true
    await loadSavedStrategies()
  } catch (err) {
    savedError.value = err instanceof Error ? err.message : String(err)
    strategySaved.value = false
  } finally {
    strategySaving.value = false
  }
}

async function deleteSavedStrategy(item: ScreenerSavedStrategy) {
  if (!window.confirm(`确定删除策略“${item.name}”吗？`)) return
  try {
    await api.screenerStrategyDelete(item.id)
    savedStrategies.value = savedStrategies.value.filter((strategy) => strategy.id !== item.id)
    if (activeSavedStrategyId.value === item.id) {
      activeSavedStrategyId.value = null
      strategySaved.value = false
    }
  } catch (err) {
    savedError.value = err instanceof Error ? err.message : String(err)
  }
}

function clearBuilderConditions() {
  applyQuery({})
  parsedQuery.value = null
  parseSummary.value = ''
  result.value = null
  ran.value = false
  error.value = ''
  interpretation.value = ''
  interpretError.value = ''
  watchlistState.value = {}
  selectedSymbol.value = ''
  checkedSymbols.value = []
  comparisonSymbols.value = []
  resultPage.value = 1
  strategySaved.value = false
  void nextTick(() => ruleEditor.value?.scrollIntoView({ behavior: 'smooth', block: 'start' }))
}

function focusRuleEditor() {
  ruleEditor.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

function relaxDataSensitiveRules() {
  volumeRatioMin.value = ''
  aboveMa.value = ''
  result.value = null
  ran.value = false
  error.value = ''
  interpretation.value = ''
  void nextTick(() => ruleEditor.value?.scrollIntoView({ behavior: 'smooth', block: 'start' }))
}

async function addResultToWatchlist(item: ScreenerItem) {
  if (watchlistState.value[item.symbol] === 'adding' || watchlistState.value[item.symbol] === 'added') return
  watchlistState.value = { ...watchlistState.value, [item.symbol]: 'adding' }
  try {
    const addedItem = await api.addWatchlist({
      symbol: item.symbol,
      group_name: 'AI选股',
      reason: `来自 AI 选股：${builderChips.value.join('、')}`.slice(0, 500),
    })
    emit('watchlistAdded', addedItem)
    watchlistState.value = { ...watchlistState.value, [item.symbol]: 'added' }
  } catch {
    watchlistState.value = { ...watchlistState.value, [item.symbol]: 'error' }
  }
}

function selectResult(item: ScreenerItem) {
  selectedSymbol.value = item.symbol
}

function toggleResultChecked(item: ScreenerItem) {
  selectedSymbol.value = item.symbol
  checkedSymbols.value = checkedSymbols.value.includes(item.symbol)
    ? checkedSymbols.value.filter((symbol) => symbol !== item.symbol)
    : [...checkedSymbols.value, item.symbol]
}

function toggleCurrentPageChecked() {
  const pageSymbols = sortedResultItems.value.map((item) => item.symbol)
  if (allPageChecked.value) {
    checkedSymbols.value = checkedSymbols.value.filter((symbol) => !pageSymbols.includes(symbol))
    return
  }
  checkedSymbols.value = Array.from(new Set([...checkedSymbols.value, ...pageSymbols]))
}

function clearCheckedResults() {
  checkedSymbols.value = []
}

async function addCheckedToWatchlist() {
  if (!checkedResultItems.value.length || bulkWatchlistAdding.value) return
  bulkWatchlistAdding.value = true
  try {
    for (const item of checkedResultItems.value) {
      await addResultToWatchlist(item)
    }
  } finally {
    bulkWatchlistAdding.value = false
  }
}

function addCheckedToComparison() {
  if (!checkedResultItems.value.length || checkedResultItems.value.length > 4) return
  comparisonSymbols.value = checkedResultItems.value.map((item) => item.symbol)
  resultView.value = 'compare'
}

function askCheckedStock() {
  if (checkedResultItems.value.length !== 1) return
  emit('askStock', checkedResultItems.value[0])
}

function createCheckedAgentTask() {
  if (!checkedResultItems.value.length) return
  emit('createAgentTask', checkedResultItems.value.slice(0, 20))
}

function toggleComparison(item: ScreenerItem) {
  if (comparisonSymbols.value.includes(item.symbol)) {
    comparisonSymbols.value = comparisonSymbols.value.filter((symbol) => symbol !== item.symbol)
    return
  }
  comparisonSymbols.value = [...comparisonSymbols.value.slice(-2), item.symbol]
}

function changeResultPage(page: number) {
  resultPage.value = Math.min(Math.max(page, 1), resultPages.value)
  const first = allSortedResultItems.value[(resultPage.value - 1) * resultPageSize]
  if (first) selectedSymbol.value = first.symbol
}

function isoDate(date: Date): string {
  const year = date.getFullYear()
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${year}${month}${day}`
}

async function runQuickBacktest() {
  if (backtestLoading.value || checkedResultItems.value.length < 3 || checkedResultItems.value.length > 20) return
  backtestLoading.value = true
  backtestError.value = ''
  try {
    const end = syncStatus.value?.latest_bar_date
      ? new Date(`${fmtDate(syncStatus.value.latest_bar_date)}T00:00:00`)
      : new Date()
    if (Number.isNaN(end.getTime())) end.setTime(Date.now())
    const start = new Date(end)
    start.setFullYear(start.getFullYear() - 2)
    const symbols = checkedResultItems.value.map((item) => item.symbol)
    const out = await api.runStrategyBacktest({
      strategy_code: 'momentum',
      symbols,
      start_date: isoDate(start),
      end_date: isoDate(end),
      strategy_params: { lookback: 20, top_n: Math.min(5, symbols.length) },
    })
    emit('openBacktest', out.run_id)
  } catch (err) {
    backtestError.value = err instanceof Error ? err.message : String(err)
  } finally {
    backtestLoading.value = false
  }
}

async function parseNL() {
  const text = String(nlText.value).trim()
  if (!text || parseLoading.value) return
  parseLoading.value = true
  parseError.value = ''
  parseSummary.value = ''
  parsedQuery.value = null
  result.value = null
  ran.value = false
  error.value = ''
  interpretation.value = ''
  interpretError.value = ''
  try {
    const parsed = await api.screenerParse(text)
    parseSummary.value = parsed.summary
    activeHistoryId.value = parsed.history_id
    if (!parsed.fallback && Object.keys(parsed.query).length) {
      parsedQuery.value = parsed.query
      applyQuery(parsed.query)
      activeWorkspace.value = 'builder'
      advancedOpen.value = true
      await nextTick()
      window.scrollTo({ top: 0, behavior: 'smooth' })
    }
    localStorage.removeItem(DRAFT_KEY)
    draftSaved.value = true
    void loadPromptHistory()
  } catch (err) {
    parseError.value = err instanceof Error ? err.message : String(err)
  } finally {
    parseLoading.value = false
  }
}

async function run() {
  if (running.value) return
  if (!hasBuilderConditions.value) {
    activeWorkspace.value = 'builder'
    advancedOpen.value = true
    error.value = '请至少设置一项筛选条件后再运行。'
    return
  }
  running.value = true
  watchlistState.value = {}
  error.value = ''
  interpretation.value = ''
  interpretModel.value = ''
  interpretError.value = ''
  const query = buildQuery()
  try {
    result.value = await api.runScreener(query)
    lastQuery.value = query
    ran.value = true
    resultPage.value = 1
    resultSearch.value = ''
    resultSearchOpen.value = false
    resultColumnsOpen.value = false
    checkedSymbols.value = []
    comparisonSymbols.value = []
    selectedSymbol.value = result.value.items[0]?.symbol || ''
    lastRunAt.value = new Date().toLocaleString('zh-CN', { hour12: false })
    backtestError.value = ''
    await nextTick()
    if (window.innerWidth < 1180) {
      resultSection.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    }
    if (activeHistoryId.value != null) {
      void api.screenerPromptResult(activeHistoryId.value, result.value.total).then(() => loadPromptHistory())
    }
    if (activeSavedStrategyId.value != null) {
      void api.screenerStrategyResult(activeSavedStrategyId.value, result.value.total).then(() => loadSavedStrategies())
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    running.value = false
  }
}

async function interpret() {
  if (!result.value || !result.value.items.length || interpretLoading.value) return
  interpretLoading.value = true
  interpretError.value = ''
  try {
    const out = await api.screenerInterpret({
      query: lastQuery.value,
      universe: result.value.universe,
      total: result.value.total,
      items: result.value.items.slice(0, 15),
    })
    interpretation.value = out.interpretation
    interpretModel.value = out.model ?? ''
  } catch (err) {
    interpretError.value = err instanceof Error ? err.message : 'AI 解读失败'
  } finally {
    interpretLoading.value = false
  }
}

function pctTone(value: number | null): string {
  if (value == null || value === 0) return ''
  return value > 0 ? 'pnl-up' : 'pnl-down'
}

function formatPct(value: number | null): string {
  if (value == null) return '—'
  const sign = value > 0 ? '+' : ''
  return `${sign}${value.toFixed(2)}%`
}

function formatNum(value: number | null, digits = 2): string {
  return value == null ? '—' : value.toFixed(digits)
}

onMounted(() => {
  const draft = localStorage.getItem(DRAFT_KEY)
  if (draft) nlText.value = draft
  void Promise.all([loadSyncStatus(), loadRecommendation(), loadPromptHistory(), loadSavedStrategies()])
})
</script>

<template>
  <section class="sc-wrap">
    <header class="sc-pagebar">
      <div class="sc-brand">
        <h2>AI 选股</h2>
        <p>用自然语言，把选股想法变成可核对的筛选条件</p>
      </div>
      <nav class="sc-page-tabs" aria-label="AI 选股工作区">
        <button type="button" :class="{ 'is-active': activeWorkspace === 'quick' }" @click="openQuick">快速选股</button>
        <button
          type="button"
          data-testid="screener-strategy-tab"
          :class="{ 'is-active': activeWorkspace === 'builder' }"
          @click="openBuilder"
        >
          策略构建
        </button>
        <button type="button" :class="{ 'is-active': activeWorkspace === 'saved' }" @click="openSavedStrategies">我的策略</button>
      </nav>
      <div class="sc-page-actions">
        <div class="sc-data-status">
          <span class="sc-status-dot"></span>
          <span>{{ statusLine || '正在读取市场数据状态' }}</span>
          <button type="button" :disabled="syncLoading" @click="syncNow">
            {{ syncLoading ? '同步中…' : '同步数据' }}
          </button>
        </div>
        <button type="button" class="sc-btn sc-btn-ghost" @click="activeWorkspace === 'quick' ? saveDraft() : openQuick()">
          {{ activeWorkspace === 'quick' ? (draftSaved ? '已保存' : '保存草稿') : '返回快速选股' }}
        </button>
        <button type="button" class="sc-btn sc-btn-primary" @click="newStrategy">新建选股</button>
      </div>
    </header>

    <div v-if="syncError" class="notice notice-danger sc-top-notice">
      <strong>同步失败</strong>
      <span>{{ syncError }}</span>
    </div>
    <p v-else-if="syncStats" class="sc-sync-note">
      同步完成：新增 {{ syncStats.dates_fetched }} 个交易日 K 线，更新 {{ syncStats.latest_prices_upserted }} 条最新行情，耗时
      {{ syncStats.elapsed_seconds }} 秒。
      <template v-if="syncStats.errors.length">部分失败：{{ syncStats.errors.join('；') }}</template>
    </p>

    <main class="sc-workspace">
      <template v-if="activeWorkspace === 'quick'">
      <section class="sc-start-grid">
        <article class="sc-panel sc-composer">
          <header class="sc-panel-head">
            <div>
              <span class="sc-eyebrow">快速选股</span>
              <h3>描述你想找什么样的股票</h3>
              <p>可以写市值、估值、量价、趋势等条件，AI 只负责翻译，执行前由你确认。</p>
            </div>
            <span v-if="draftSaved" class="sc-saved-state">已保存</span>
          </header>
          <div class="sc-prompt-box" :class="{ 'has-error': parseError }">
            <textarea
              v-model="nlText"
              rows="5"
              placeholder="例如：找市值 200 亿以下、量比大于 1.5、换手率 3% 到 10%，并且站上 20 日均线的股票"
              @input="draftSaved = false"
              @keydown.ctrl.enter.prevent="parseNL"
              @keydown.meta.enter.prevent="parseNL"
            ></textarea>
            <div class="sc-prompt-footer">
              <span>支持自然语言 · Ctrl / ⌘ + Enter 生成</span>
              <button v-if="nlText" type="button" class="sc-text-btn" @click="clearPrompt">清空</button>
            </div>
          </div>
          <div v-if="parseError" class="sc-inline-error">{{ parseError }}</div>
          <div class="sc-composer-actions">
            <button type="button" class="sc-text-btn" @click="openBuilder">
              手动设置高级条件
            </button>
            <button type="button" class="sc-btn sc-btn-primary sc-generate" :disabled="parseLoading || !nlText.trim()" @click="parseNL">
              <span v-if="parseLoading" class="btn-spin spin"></span>
              {{ parseLoading ? '正在理解条件…' : '生成选股方案' }}
            </button>
          </div>
        </article>

        <article class="sc-panel sc-profile">
          <header class="sc-panel-head sc-profile-head">
            <div>
              <span class="sc-eyebrow">我的持仓风格</span>
              <h3>先看懂你，再推荐模板</h3>
            </div>
            <span class="sc-count-badge">已分析 {{ smartRec?.position_count || 0 }} 只</span>
          </header>
          <template v-if="smartRec?.position_count">
            <p class="sc-profile-summary">{{ smartRec.basis || '已根据当前持仓完成风格归纳。' }}</p>
            <div v-if="profileTags.length" class="sc-tag-row">
              <span v-for="tag in profileTags" :key="tag">{{ tag }}</span>
            </div>
            <div class="sc-profile-facts">
              <div><span>分析来源</span><strong>当前持仓</strong></div>
              <div><span>推荐用途</span><strong>发现相似机会</strong></div>
            </div>
            <p v-if="profileDetailsOpen" class="sc-profile-detail">
              风格标签来自持仓结构和当前可用行情，仅用于组织筛选条件，不代表收益判断。
            </p>
            <button type="button" class="sc-text-btn" @click="profileDetailsOpen = !profileDetailsOpen">
              {{ profileDetailsOpen ? '收起分析说明' : '查看分析说明' }}
            </button>
          </template>
          <div v-else class="sc-profile-empty">
            <strong>还没有可分析的持仓</strong>
            <p>录入持仓后，可生成与你当前风格更贴近的快捷模板。</p>
          </div>
          <button type="button" class="sc-btn sc-btn-ghost sc-profile-refresh" :disabled="recLoading" @click="refreshRecommendation">
            <span class="btn-spin" :class="{ spin: recLoading }"></span>
            {{ recLoading ? '正在分析持仓…' : '重新分析并推荐' }}
          </button>
        </article>
      </section>

      <section class="sc-panel sc-personalized">
        <header class="sc-section-head">
          <div>
            <span class="sc-eyebrow">为我推荐</span>
            <h3>结合持仓风格生成的快速模板</h3>
          </div>
          <span>推荐只组织筛选条件，不构成投资建议</span>
        </header>
        <div v-if="recError" class="sc-inline-error">{{ recError }}</div>
        <div v-if="personalizedTemplates.length" class="sc-recommend-grid">
          <article v-for="tpl in personalizedTemplates" :key="tpl.name" class="sc-recommend-card">
            <div class="sc-card-title">
              <h4>{{ tpl.name }}</h4>
              <span>基于持仓</span>
            </div>
            <p class="sc-template-prompt">“{{ templatePrompt(tpl) }}”</p>
            <p class="sc-rationale">{{ templateRationale(tpl) }}</p>
            <footer>
              <span>{{ templateCount(tpl) }}</span>
              <button type="button" class="sc-btn sc-btn-soft" @click="usePrompt(templatePrompt(tpl))">使用此模板</button>
            </footer>
          </article>
        </div>
        <div v-else-if="recLoading" class="sc-loading-state">正在分析持仓并生成模板…</div>
        <div v-else class="sc-empty-line">
          <span>暂无个性化模板，可先使用下方通用模板。</span>
          <button type="button" class="sc-text-btn" @click="refreshRecommendation">立即生成</button>
        </div>
      </section>

      <section class="sc-library-grid">
        <article class="sc-panel sc-template-library">
          <header class="sc-section-head">
            <div>
              <span class="sc-eyebrow">快速模板</span>
              <h3>从成熟条件组合开始</h3>
            </div>
            <button type="button" class="sc-text-btn" @click="showAllTemplates = !showAllTemplates">
              {{ showAllTemplates ? '按分类查看' : '查看全部模板' }}
            </button>
          </header>
          <div class="sc-filter-tabs" role="tablist" aria-label="模板分类">
            <button
              v-for="category in TEMPLATE_CATEGORIES"
              :key="category"
              type="button"
              :class="{ 'is-active': !showAllTemplates && templateCategory === category }"
              @click="templateCategory = category; showAllTemplates = false"
            >
              {{ category }}
            </button>
          </div>
          <div class="sc-template-list">
            <button v-for="item in visibleTemplates" :key="item.name" type="button" class="sc-template-row" @click="usePrompt(item.prompt)">
              <span class="sc-template-icon">{{ item.name.slice(0, 1) }}</span>
              <span class="sc-template-copy">
                <strong>{{ item.name }}</strong>
                <small>{{ item.prompt }}</small>
              </span>
              <span class="sc-template-meta">{{ queryToChips(item.query).length }} 项条件</span>
              <span class="sc-row-arrow">›</span>
            </button>
          </div>
        </article>

        <article class="sc-panel sc-history">
          <header class="sc-section-head">
            <div>
              <span class="sc-eyebrow">历史提示词</span>
              <h3>用过的想法，都可以再次调用</h3>
            </div>
            <span>已保留 {{ historyTotal }} 条</span>
          </header>
          <label class="sc-history-search">
            <span>⌕</span>
            <input v-model="historySearch" type="search" placeholder="搜索历史提示词" />
          </label>
          <div class="sc-filter-tabs" role="tablist" aria-label="历史提示词筛选">
            <button type="button" :class="{ 'is-active': historyTab === '全部' }" @click="historyTab = '全部'">全部</button>
            <button type="button" :class="{ 'is-active': historyTab === '已收藏' }" @click="historyTab = '已收藏'">已收藏</button>
          </div>
          <div v-if="historyError" class="sc-inline-error">{{ historyError }}</div>
          <div v-else-if="historyLoading" class="sc-loading-state">正在读取历史提示词…</div>
          <div v-else-if="visibleHistory.length" class="sc-history-list">
            <article v-for="item in visibleHistory" :key="item.id" class="sc-history-row">
              <button
                type="button"
                class="sc-star"
                :class="{ 'is-active': item.is_favorite }"
                :aria-label="item.is_favorite ? '取消收藏' : '收藏提示词'"
                @click="toggleFavorite(item)"
              >
                {{ item.is_favorite ? '★' : '☆' }}
              </button>
              <button
                type="button"
                class="sc-history-entry"
                data-testid="screener-history-entry"
                :disabled="parseLoading"
                :aria-label="`使用历史提示词进入策略构建：${item.text}`"
                @click="usePrompt(item.text)"
              >
                <span class="sc-history-copy">
                  <span class="sc-history-prompt">{{ item.text }}</span>
                  <span>{{ historyTime(item.last_used_at) }} · 使用 {{ item.use_count }} 次 · {{ item.last_result_count == null ? '未运行' : `命中 ${item.last_result_count} 只` }}</span>
                </span>
              </button>
              <button
                type="button"
                class="sc-btn sc-btn-soft"
                data-testid="screener-history-open-builder"
                :disabled="parseLoading"
                @click="usePrompt(item.text)"
              >
                进入策略构建
              </button>
            </article>
          </div>
          <div v-else class="sc-empty-line">{{ historySearch ? '没有找到匹配的提示词' : '生成过的选股方案会自动保存在这里' }}</div>
          <button v-if="visibleHistory.length >= 3 || historyExpanded" type="button" class="sc-history-more" @click="historyExpanded = !historyExpanded">
            {{ historyExpanded ? '收起历史' : '查看全部历史' }}
          </button>
        </article>
      </section>
      </template>

      <section v-else-if="activeWorkspace === 'builder'" data-testid="screener-builder" class="sc-builder-workbench sc-strategy-workbench">
        <div v-if="parseError" class="notice notice-danger sc-builder-parse-notice">
          <strong>提示词解析失败</strong>
          <span>{{ parseError }}。你仍可在下方手动设置策略条件。</span>
        </div>
        <header class="sc-strategy-hero">
          <div class="sc-strategy-heading">
            <div>
              <span class="sc-eyebrow">策略构建</span>
              <h2>{{ activeStrategyName }}</h2>
            </div>
            <span class="sc-run-status" :class="{ 'is-ready': ran }">{{ parseLoading ? '解析条件中' : running ? '运行中' : ran ? '已运行' : '待运行' }}</span>
            <span v-if="lastRunAt" class="sc-last-run">最后运行 {{ lastRunAt }}</span>
          </div>
          <div class="sc-hero-actions">
            <button type="button" class="sc-btn sc-btn-ghost" @click="ruleEditorOpen = !ruleEditorOpen">{{ ruleEditorOpen ? '收起条件' : '编辑条件' }}</button>
            <input v-model="strategyName" type="text" maxlength="120" placeholder="策略名称" @input="strategySaved = false" @keydown.enter.prevent="saveCurrentStrategy" />
            <button type="button" class="sc-btn sc-btn-ghost" :disabled="strategySaving || !strategyName.trim() || !hasBuilderConditions" @click="saveCurrentStrategy">{{ strategySaving ? '保存中…' : strategySaved ? '已保存' : '保存版本' }}</button>
            <button type="button" data-testid="screener-run" class="sc-btn sc-btn-primary" :disabled="running || !hasBuilderConditions" @click="run"><span class="btn-spin" :class="{ spin: running }"></span>{{ running ? '正在运行…' : ran ? '重新运行' : '运行筛选' }}</button>
          </div>
          <div class="sc-strategy-chips">
            <button type="button" class="sc-rule-chip" @click="ruleEditorOpen = true">{{ market ? ({ SH: '沪市', SZ: '深市', BSE: '北交所' }[market] || market) : '全 A 股' }} <span>✎</span></button>
            <button v-for="chip in builderChips" :key="chip" type="button" class="sc-rule-chip" @click="ruleEditorOpen = true">{{ chip }} <span>✎</span></button>
            <button v-if="!builderChips.length" type="button" class="sc-rule-chip is-empty" @click="ruleEditorOpen = true">+ 添加筛选条件</button>
          </div>
        </header>

        <div v-if="savedError" class="notice notice-danger sc-builder-notice"><strong>策略保存失败</strong><span>{{ savedError }}</span></div>
        <div v-if="error" class="notice notice-danger sc-builder-notice"><strong>筛选失败</strong><span>{{ error }}</span></div>

        <div class="sc-strategy-layout">
          <aside class="sc-strategy-sidebar sc-panel">
            <header><h3>策略规则</h3><span>所有规则实时生效</span></header>
            <section class="sc-logic-card">
              <div class="sc-logic-title"><strong>1. 股票池与范围</strong><button type="button" @click="ruleEditorOpen = true">⌃</button></div>
              <ul>
                <li><i class="is-ok">✓</i>{{ market ? `${market} 市场` : '全 A 股市场' }}</li>
                <li><i class="is-ok">✓</i>使用最新交易日快照</li>
                <li><i class="is-ok">✓</i>{{ keyword ? `限定：${keyword}` : '不限股票代码或名称' }}</li>
              </ul>
            </section>
            <section class="sc-logic-card">
              <div class="sc-logic-title"><strong>2. 硬性过滤</strong><button type="button" @click="ruleEditorOpen = true">⌃</button></div>
              <ul v-if="hardFilterChips.length">
                <li v-for="chip in hardFilterChips" :key="chip"><i class="is-rule"></i>{{ chip }}</li>
              </ul>
              <p v-else class="sc-no-rule">尚未设置硬性条件</p>
            </section>
            <section class="sc-logic-card sc-score-weights">
              <div class="sc-logic-title"><strong>3. 综合评分</strong><span>候选集相对分</span></div>
              <label><span>估值规模</span><input v-model.number="scoreValueWeight" type="range" min="0" max="100" /><b>{{ scoreValueWeight }}%</b></label>
              <label><span>动量</span><input v-model.number="scoreMomentumWeight" type="range" min="0" max="100" /><b>{{ scoreMomentumWeight }}%</b></label>
              <label><span>流动性</span><input v-model.number="scoreLiquidityWeight" type="range" min="0" max="100" /><b>{{ scoreLiquidityWeight }}%</b></label>
              <small>评分只使用本页真实量价、市值与 PE 数据，不包含未接入的财务字段。</small>
            </section>
            <section class="sc-logic-card">
              <div class="sc-logic-title"><strong>4. 输出设置</strong><span>当前快照</span></div>
              <ul><li><i>→</i>选取 Top {{ resultLimit }}</li><li><i>→</i>按综合分优先排序</li></ul>
            </section>
            <button type="button" class="sc-add-condition" @click="ruleEditorOpen = true">＋ 添加 / 修改条件</button>
          </aside>

          <section ref="resultSection" class="sc-results-center sc-panel">
            <header class="sc-center-head">
              <div><h3>筛选结果 <b v-if="result">{{ allSortedResultItems.length }} / {{ result.universe.toLocaleString('zh-CN') }}</b></h3><span v-if="result">已按当前规则筛选 · 展示 Top {{ resultLimit }} 实时结果</span><span v-else>{{ statusLine || '使用最近一次同步的市场数据' }}</span></div>
            </header>
            <nav class="sc-result-tabs" aria-label="结果视图">
              <div class="sc-result-tabset"><button type="button" :class="{ active: resultView === 'ranking' }" @click="resultView = 'ranking'">排名表</button><button type="button" :class="{ active: resultView === 'factors' }" @click="resultView = 'factors'">图表</button><button type="button" :class="{ active: resultView === 'compare' }" @click="resultView = 'compare'">对比 <em v-if="comparisonSymbols.length">{{ comparisonSymbols.length }}</em></button></div>
              <div v-if="result?.items.length" class="sc-result-toolbar">
                <select v-model="resultSort" class="sc-result-sort" aria-label="排序" @change="resultPage = 1"><option value="score_desc">按综合分排序</option><option value="pct_desc">涨幅从高到低</option><option value="turnover_desc">换手率从高到低</option><option value="mv_asc">市值从小到大</option><option value="pe_asc">PE 从低到高</option></select>
                <div class="sc-column-picker"><button type="button" :aria-expanded="resultColumnsOpen" @click="resultColumnsOpen = !resultColumnsOpen">自定义列</button><div v-if="resultColumnsOpen" class="sc-column-menu"><label v-for="column in resultColumnOptions" :key="column.key"><input type="checkbox" :checked="isResultColumnVisible(column.key)" @change="toggleResultColumn(column.key)" />{{ column.label }}</label></div></div>
                <div class="sc-result-search" :class="{ open: resultSearchOpen }"><input v-if="resultSearchOpen" v-model="resultSearch" type="search" placeholder="代码或名称" aria-label="搜索筛选结果" @input="resultPage = 1" @keydown.esc="resultSearchOpen = false; resultSearch = ''" /><button type="button" aria-label="搜索结果" @click="resultSearchOpen = !resultSearchOpen">⌕</button></div>
              </div>
            </nav>

            <template v-if="result?.items.length">
              <div v-if="resultView === 'ranking'" class="sc-ranking-table-wrap">
                <table class="sc-ranking-table">
                  <thead><tr><th class="sc-check-cell"><input type="checkbox" data-testid="screener-select-page" aria-label="选择本页股票" :checked="allPageChecked" :indeterminate="somePageChecked && !allPageChecked" @change="toggleCurrentPageChecked" /></th><th>排名</th><th>标的</th><th>综合分</th><th v-if="isResultColumnVisible('value')">估值规模</th><th v-if="isResultColumnVisible('momentum')">动量</th><th v-if="isResultColumnVisible('liquidity')">流动性</th><th v-if="isResultColumnVisible('reasons')">入选理由</th><th v-if="isResultColumnVisible('risks')">主要风险</th><th></th></tr></thead>
                  <tbody>
                    <tr v-for="item in sortedResultItems" :key="item.symbol" :class="{ selected: selectedResultItem?.symbol === item.symbol, 'is-checked': checkedSymbols.includes(item.symbol) }" @click="selectResult(item)">
                      <td class="sc-check-cell" @click.stop><input type="checkbox" data-testid="screener-result-checkbox" :aria-label="`选择 ${item.name} ${item.symbol}`" :checked="checkedSymbols.includes(item.symbol)" @change="toggleResultChecked(item)" /></td>
                      <td>{{ allSortedResultItems.findIndex((row) => row.symbol === item.symbol) + 1 }}</td>
                      <td><strong>{{ item.symbol }}</strong><span>{{ item.name }}</span></td>
                      <td class="sc-score-cell"><b>{{ item.compositeScore.toFixed(1) }}</b></td>
                      <td v-if="isResultColumnVisible('value')">{{ item.valueScore.toFixed(1) }}</td><td v-if="isResultColumnVisible('momentum')">{{ item.momentumScore.toFixed(1) }}</td><td v-if="isResultColumnVisible('liquidity')">{{ item.liquidityScore.toFixed(1) }}</td>
                      <td v-if="isResultColumnVisible('reasons')"><div class="sc-reason-tags"><span v-for="reason in item.reasons.slice(0, 3)" :key="reason">{{ reasonTag(reason) }}</span></div></td>
                      <td v-if="isResultColumnVisible('risks')" class="sc-risk-cell">{{ item.risks[0] }}</td>
                      <td><button type="button" class="sc-mini-ask" @click.stop="emit('askStock', item)">问股</button></td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <div v-else-if="resultView === 'factors'" class="sc-factor-board">
                <article v-for="item in sortedResultItems" :key="item.symbol" :class="{ selected: selectedResultItem?.symbol === item.symbol, 'is-checked': checkedSymbols.includes(item.symbol) }" @click="selectResult(item)"><input type="checkbox" class="sc-factor-checkbox" data-testid="screener-factor-checkbox" :aria-label="`选择 ${item.name} ${item.symbol}`" :checked="checkedSymbols.includes(item.symbol)" @click.stop @change="toggleResultChecked(item)" /><header><strong>{{ item.name }}</strong><b>{{ item.compositeScore.toFixed(1) }}</b></header><div><span>估值规模</span><i><em :style="{ width: `${item.valueScore}%` }"></em></i><b>{{ item.valueScore.toFixed(0) }}</b></div><div><span>动量</span><i><em :style="{ width: `${item.momentumScore}%` }"></em></i><b>{{ item.momentumScore.toFixed(0) }}</b></div><div><span>交易活跃</span><i><em :style="{ width: `${item.liquidityScore}%` }"></em></i><b>{{ item.liquidityScore.toFixed(0) }}</b></div></article>
              </div>
              <div v-else class="sc-compare-board">
                <article v-for="item in comparedItems" :key="item.symbol"><header><div><strong>{{ item.name }}</strong><span>{{ item.symbol }}</span></div><b>{{ item.compositeScore.toFixed(1) }}</b></header><dl><div><dt>估值规模</dt><dd>{{ item.valueScore.toFixed(1) }}</dd></div><div><dt>动量</dt><dd>{{ item.momentumScore.toFixed(1) }}</dd></div><div><dt>活跃度</dt><dd>{{ item.liquidityScore.toFixed(1) }}</dd></div><div><dt>PE</dt><dd>{{ formatNum(item.pe_ttm, 1) }}</dd></div></dl><button type="button" @click="toggleComparison(item)">移出对比</button></article>
              </div>
              <footer class="sc-result-footer">
                <span>显示 {{ (resultPage - 1) * resultPageSize + 1 }}–{{ Math.min(resultPage * resultPageSize, allSortedResultItems.length) }} 条，共 {{ allSortedResultItems.length }} 条</span>
                <div><button type="button" :disabled="resultPage <= 1" @click="changeResultPage(resultPage - 1)">‹</button><button v-for="page in resultPages" :key="page" type="button" :class="{ active: resultPage === page }" @click="changeResultPage(page)">{{ page }}</button><button type="button" :disabled="resultPage >= resultPages" @click="changeResultPage(resultPage + 1)">›</button></div>
              </footer>
              <div class="sc-workbench-actions">
                <div class="sc-selection-summary"><strong>已选 {{ checkedResultItems.length }} 只</strong><button v-if="checkedResultItems.length" type="button" @click="clearCheckedResults">清空</button></div>
                <button type="button" :disabled="!checkedResultItems.length || bulkWatchlistAdding" @click="addCheckedToWatchlist">{{ bulkWatchlistAdding ? '加入中…' : `加入自选 (${checkedResultItems.length})` }}</button>
                <button type="button" :disabled="!checkedResultItems.length || checkedResultItems.length > 4" :title="checkedResultItems.length > 4 ? '对比最多选择 4 只股票' : ''" @click="addCheckedToComparison">加入对比 ({{ checkedResultItems.length }})</button>
                <button type="button" :disabled="checkedResultItems.length !== 1" title="问股一次只能选择 1 只股票" @click="askCheckedStock">问股</button>
                <button type="button" class="primary" :disabled="backtestLoading || checkedResultItems.length < 3 || checkedResultItems.length > 20" title="快速回测请选择 3–20 只股票" @click="runQuickBacktest">{{ backtestLoading ? '回测运行中…' : `快速回测 (${checkedResultItems.length})` }}</button>
                <button type="button" :disabled="!checkedResultItems.length" @click="createCheckedAgentTask">创建 Agent 任务</button>
              </div>
              <p v-if="backtestError" class="sc-action-error">快速回测失败：{{ backtestError }}</p>
            </template>
            <div v-else-if="running" class="sc-result-loading"><span class="btn-spin spin"></span><strong>正在按 {{ builderChips.length }} 项条件筛选</strong><p>完成后会生成排名、相对评分和标的解释。</p></div>
            <div v-else-if="result" class="sc-result-empty"><strong>当前条件没有找到股票</strong><p>通常是条件组合过严，或量比、均线等指标在最新数据中缺失。</p><div class="sc-empty-actions"><button v-if="volumeRatioMin || aboveMa" type="button" class="sc-btn sc-btn-soft" @click="relaxDataSensitiveRules">去掉量比与均线条件</button><button type="button" class="sc-btn sc-btn-ghost" @click="ruleEditorOpen = true">调整条件</button></div></div>
            <div v-else class="sc-result-ready"><strong>先设置规则并运行筛选</strong><p>左侧配置策略逻辑；运行后，中间展示真实候选集排名，右侧解释选中标的。</p><button type="button" class="sc-btn sc-btn-primary" @click="ruleEditorOpen = true">开始设置条件</button></div>
          </section>

          <aside class="sc-explain-panel sc-panel">
            <template v-if="selectedResultItem">
              <header><h3>选中标的解释</h3><span>候选集相对评分</span></header>
              <section class="sc-selected-stock"><div><strong>{{ selectedResultItem.symbol }} {{ selectedResultItem.name }}</strong><span>最新 {{ formatNum(selectedResultItem.price) }} · <em :class="pctTone(selectedResultItem.pct_chg)">{{ formatPct(selectedResultItem.pct_chg) }}</em></span></div><b>{{ selectedResultItem.compositeScore.toFixed(1) }}</b></section>
              <section class="sc-contribution"><h4>因子贡献（权重）</h4><div><span>估值规模（{{ scoreValueWeight }}%）</span><i><em :style="{ width: `${selectedResultItem.valueScore}%` }"></em></i><b>+{{ weightedContribution(selectedResultItem.valueScore, scoreValueWeight).toFixed(1) }}</b></div><div><span>动量（{{ scoreMomentumWeight }}%）</span><i><em :style="{ width: `${selectedResultItem.momentumScore}%` }"></em></i><b>+{{ weightedContribution(selectedResultItem.momentumScore, scoreMomentumWeight).toFixed(1) }}</b></div><div><span>流动性（{{ scoreLiquidityWeight }}%）</span><i><em :style="{ width: `${selectedResultItem.liquidityScore}%` }"></em></i><b>+{{ weightedContribution(selectedResultItem.liquidityScore, scoreLiquidityWeight).toFixed(1) }}</b></div></section>
              <section class="sc-explain-list"><h4>为什么入选</h4><ul><li v-for="reason in selectedResultItem.reasons" :key="reason">{{ reason }}</li></ul></section>
              <section class="sc-explain-list"><h4>风险提示</h4><ul><li v-for="risk in selectedResultItem.risks" :key="risk">{{ risk }}</li></ul></section>
              <section class="sc-completeness"><span>数据完整度</span><strong>{{ selectedResultItem.completeness }}%</strong></section>
              <section class="sc-distribution"><h4>候选集特征分布</h4><div v-for="feature in resultFeatureDistribution" :key="feature.label"><span>{{ feature.label }}</span><i><em :style="{ width: `${feature.value}%` }"></em></i><b>{{ feature.value }}%</b></div></section>
            </template>
            <div v-else class="sc-explain-empty"><strong>运行后查看标的解释</strong><p>这里会解释真实数据贡献、入选原因、风险与数据完整度。</p></div>
          </aside>
        </div>

        <section v-if="ruleEditorOpen" ref="ruleEditor" class="sc-panel sc-rule-config-panel" @input="strategySaved = false">
          <header><div><span class="sc-eyebrow">编辑筛选条件</span><h3>留空表示不限，所有条件按“并且”执行</h3></div><div><button type="button" class="sc-btn sc-btn-ghost" :disabled="!hasBuilderConditions" @click="clearBuilderConditions">清空条件</button><button type="button" class="sc-btn sc-btn-ghost" @click="ruleEditorOpen = false">完成</button></div></header>
          <div class="sc-config-grid">
            <section><h4>股票范围</h4><label class="sc-field"><span>市场</span><select v-model="market" class="sc-input"><option value="">全部市场</option><option value="SH">沪市</option><option value="SZ">深市</option><option value="BSE">北交所</option></select></label><label class="sc-field"><span>代码或名称</span><input v-model="keyword" type="text" class="sc-input" placeholder="例如 600519 或 贵州茅台" /></label></section>
            <section><h4>价格与规模</h4><div class="sc-range-field"><span>最新价（元）</span><div><input v-model="priceMin" type="number" class="sc-input" placeholder="最低" /><em>至</em><input v-model="priceMax" type="number" class="sc-input" placeholder="最高" /></div></div><div class="sc-range-field"><span>总市值（亿元）</span><div><input v-model="mvMin" type="number" class="sc-input" placeholder="最低" /><em>至</em><input v-model="mvMax" type="number" class="sc-input" placeholder="最高" /></div></div></section>
            <section><h4>估值</h4><div class="sc-range-field"><span>PE（TTM）</span><div><input v-model="peMin" type="number" class="sc-input" placeholder="最低" /><em>至</em><input v-model="peMax" type="number" class="sc-input" placeholder="最高" /></div></div></section>
            <section><h4>今日交易特征</h4><div class="sc-range-field"><span>涨跌幅（%）</span><div><input v-model="pctChgMin" type="number" class="sc-input" placeholder="最低" /><em>至</em><input v-model="pctChgMax" type="number" class="sc-input" placeholder="最高" /></div></div><div class="sc-range-field"><span>换手率（%）</span><div><input v-model="turnoverMin" type="number" class="sc-input" placeholder="最低" /><em>至</em><input v-model="turnoverMax" type="number" class="sc-input" placeholder="最高" /></div></div><label class="sc-field"><span>量比不低于</span><input v-model="volumeRatioMin" type="number" class="sc-input" placeholder="例如 1.5" /></label></section>
            <section><h4>趋势与输出</h4><label class="sc-field"><span>站上均线</span><select v-model="aboveMa" class="sc-input"><option value="">不限</option><option value="5">5 日均线</option><option value="10">10 日均线</option><option value="20">20 日均线</option></select></label><label class="sc-field"><span>选取数量</span><select v-model.number="resultLimit" class="sc-input"><option :value="20">Top 20</option><option :value="50">Top 50</option><option :value="100">Top 100</option></select></label></section>
          </div>
        </section>
      </section>

      <section v-else class="sc-saved-workspace">
        <header class="sc-panel sc-saved-hero">
          <div><span class="sc-eyebrow">我的策略</span><h3>保存过的筛选条件</h3><p>可以直接运行，也可以打开后调整并覆盖保存。</p></div>
          <button type="button" class="sc-btn sc-btn-primary" @click="openBuilder">新建策略</button>
        </header>
        <div v-if="savedError" class="notice notice-danger"><strong>读取策略失败</strong><span>{{ savedError }}</span></div>
        <div v-if="savedLoading" class="sc-panel sc-saved-empty">正在读取策略…</div>
        <div v-else-if="savedStrategies.length" class="sc-saved-grid">
          <article v-for="item in savedStrategies" :key="item.id" class="sc-panel sc-saved-card">
            <header><div><h3>{{ item.name }}</h3><span>{{ item.updated_at ? `更新于 ${item.updated_at.replace('T', ' ')}` : '已保存' }}</span></div><strong>{{ item.last_result_count == null ? '未运行' : `${item.last_result_count} 只` }}</strong></header>
            <div class="sc-plan-chips"><span v-for="chip in queryToChips(item.query)" :key="chip">{{ chip }}</span></div>
            <footer><button type="button" class="sc-text-btn sc-danger-text" @click="deleteSavedStrategy(item)">删除</button><div><button type="button" class="sc-btn sc-btn-ghost" @click="useSavedStrategy(item)">编辑条件</button><button type="button" class="sc-btn sc-btn-primary" @click="useSavedStrategy(item, true)">运行策略</button></div></footer>
          </article>
        </div>
        <div v-else class="sc-panel sc-saved-empty"><strong>还没有保存的策略</strong><p>在“策略构建”中设置条件、填写名称并保存，之后可以从这里直接运行。</p><button type="button" class="sc-btn sc-btn-primary" @click="openBuilder">创建第一个策略</button></div>
      </section>

      <p v-if="parseSummary && !parsedQuery" class="sc-parse-warning">{{ parseSummary }}</p>
    </main>
  </section>
</template>

<style scoped>
.sc-wrap {
  position: relative;
  padding: 18px 20px 30px;
  text-align: left;
}

.sc-ambient {
  position: fixed;
  inset: 0;
  pointer-events: none;
  background:
    radial-gradient(1200px 500px at 85% -10%, rgba(213, 180, 106, 0.05), transparent 60%),
    radial-gradient(900px 400px at 0% 110%, rgba(55, 139, 255, 0.04), transparent 60%);
  z-index: 0;
}

.sc-toolbar {
  position: relative;
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 14px;
  flex-wrap: wrap;
}

.sc-title {
  display: flex;
  align-items: flex-start;
  gap: 12px;
}

.sc-title-mark {
  width: 3px;
  height: 34px;
  border-radius: 2px;
  background: linear-gradient(180deg, var(--gold-2), var(--gold-dim));
  flex: none;
  margin-top: 2px;
}

.sc-title h2 {
  margin: 0;
  font-size: 16px;
  color: var(--text);
  letter-spacing: 0.02em;
}

.sc-title p {
  margin: 4px 0 0;
  font-size: 12.5px;
  color: var(--muted);
}

.sc-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.sc-status {
  font-size: 12px;
  color: var(--muted);
}

.sc-nl {
  position: relative;
  display: flex;
  gap: 10px;
  margin-bottom: 8px;
}

.sc-nl-input {
  flex: 1;
}

.sc-parse-line {
  position: relative;
  margin: 0 0 10px;
  font-size: 12.5px;
  color: var(--gold-2);
}

.sc-parse-warn {
  color: var(--muted);
}

.sc-templates {
  position: relative;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 12px;
}

.sc-templates-label {
  font-size: 12px;
  color: var(--muted);
}

.sc-chip {
  padding: 5px 12px;
  font-size: 12.5px;
  color: var(--text);
  background: var(--surface-2);
  border: 1px solid var(--line);
  border-radius: 999px;
  cursor: pointer;
  transition: border-color 0.15s, color 0.15s;
}

.sc-chip:hover:not(:disabled) {
  border-color: var(--gold);
  color: var(--gold-2);
}

.sc-chip:disabled {
  opacity: 0.6;
  cursor: default;
}

.sc-chips {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.sc-chip-ai {
  border-color: rgba(213, 180, 106, 0.35);
}

.sc-chip-count {
  margin-left: 6px;
  font-size: 11px;
  color: var(--muted);
}

.sc-rec {
  position: relative;
  margin-bottom: 12px;
  padding: 10px 12px;
  border: 1px dashed rgba(213, 180, 106, 0.35);
  border-radius: var(--radius);
  background: rgba(213, 180, 106, 0.04);
}

.sc-rec-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
}

.sc-rec-meta {
  font-size: 12px;
  color: var(--muted);
  flex: 1;
}

.sc-rec-basis {
  margin: 8px 0 0;
  font-size: 12.5px;
  color: var(--gold-2);
}

.sc-rec-error {
  margin-top: 8px;
  font-size: 12.5px;
  color: var(--danger);
}

.sc-rec .sc-chips {
  margin-top: 10px;
}

.btn-mini {
  padding: 4px 10px;
  font-size: 12px;
}

.sc-config {
  position: relative;
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: flex-end;
  padding: 12px;
  margin-bottom: 14px;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--surface-2);
}

.sc-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12px;
  color: var(--muted);
}

.sc-field-wide {
  flex: 1 1 220px;
}

.sc-input {
  padding: 7px 10px;
  min-width: 96px;
  background: var(--bg-2);
  border: 1px solid var(--line);
  border-radius: 6px;
  color: var(--text);
  font-size: 12.5px;
  font-family: ui-monospace, 'Cascadia Code', Consolas, monospace;
  outline: none;
  transition: border-color 0.15s, box-shadow 0.15s;
}

.sc-input:focus {
  border-color: var(--gold);
  box-shadow: 0 0 0 2px rgba(213, 180, 106, 0.14);
}

select.sc-input {
  cursor: pointer;
}

.sc-summary {
  position: relative;
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 10px;
  margin-bottom: 10px;
}

.sc-note {
  position: relative;
  margin: 0 0 12px;
  font-size: 11.5px;
  color: var(--muted);
}

.sc-table-wrap {
  position: relative;
  overflow-x: auto;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--surface);
  margin-bottom: 12px;
}

.sc-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12.5px;
}

.sc-table th {
  position: sticky;
  top: 0;
  z-index: 2;
  background: var(--surface-3);
  color: var(--muted);
  font-weight: 600;
  text-align: left;
  padding: 9px 12px;
  border-bottom: 1px solid var(--line);
  white-space: nowrap;
}

.sc-table td {
  padding: 8px 12px;
  border-bottom: 1px solid var(--line);
  color: var(--text);
  white-space: nowrap;
}

.sc-table tbody tr:hover {
  background: rgba(255, 255, 255, 0.025);
}

.sc-table .num {
  text-align: right;
  font-family: ui-monospace, 'Cascadia Code', Consolas, monospace;
}

/* A 股红涨绿跌：本表 td 自带 color，需用更高特异性覆盖全局 .pnl-up/.pnl-down */
.sc-table td.pnl-up {
  color: var(--danger);
}

.sc-table td.pnl-down {
  color: var(--ok);
}

.sc-symbol {
  font-family: ui-monospace, 'Cascadia Code', Consolas, monospace;
  color: var(--gold-2);
}

.sc-state {
  position: relative;
  padding: 30px;
  text-align: center;
  color: var(--muted);
  font-size: 13px;
  border: 1px dashed var(--line);
  border-radius: var(--radius);
}

.sc-interpret {
  position: relative;
  margin-top: 16px;
  padding: 14px;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--surface-2);
}

.sc-interpret-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 10px;
}

.sc-interpret-head strong {
  color: var(--text);
  font-size: 13.5px;
}

.sc-interpret-body {
  border-top: 1px dashed var(--line);
  padding-top: 10px;
}

.sc-interpret-model {
  font-size: 11.5px;
  color: var(--muted);
  margin-bottom: 6px;
}

/* 快速选股 V2：信息密度优先，视觉层级与现有深色终端保持一致。 */
.sc-wrap {
  --sc-accent: #d7b76f;
  --sc-accent-strong: #ead08f;
  --sc-accent-soft: rgba(215, 183, 111, 0.1);
  --sc-panel: #121518;
  --sc-panel-raised: #171b1f;
  --sc-line: rgba(255, 255, 255, 0.1);
  --sc-line-strong: rgba(215, 183, 111, 0.32);
  --sc-text: #f1eee7;
  --sc-text-2: #b9b5ab;
  --sc-text-3: #777b7d;
  padding: 0 24px 36px;
  color: var(--sc-text);
}

.sc-builder-workbench {
  display: grid;
  gap: 14px;
}

.sc-builder-commandbar {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 16px 24px;
  padding: 18px 20px;
}

.sc-builder-intro p {
  max-width: 760px;
  margin-top: 7px !important;
  color: var(--sc-text-3);
  font-size: 12.5px;
  line-height: 1.65;
}

.sc-builder-command-actions {
  display: flex;
  align-items: flex-start;
  justify-content: flex-end;
  gap: 8px;
}

.sc-run-main {
  min-width: 112px;
}

.sc-builder-rule-summary {
  display: flex;
  grid-column: 1 / -1;
  align-items: flex-start;
  gap: 12px;
  padding-top: 14px;
  border-top: 1px solid var(--sc-line);
  color: var(--sc-text-3);
  font-size: 12px;
}

.sc-builder-rule-summary > strong {
  flex: none;
  padding-top: 4px;
  color: var(--sc-text-2);
  font-size: 12px;
}

.sc-builder-rule-summary .sc-plan-chips {
  margin-top: 0;
}

.sc-strategy-save {
  display: flex;
  flex: none;
  gap: 7px;
  margin-left: auto;
}

.sc-strategy-save input {
  width: 180px;
  min-height: 34px;
  padding: 6px 10px;
  border: 1px solid var(--sc-line);
  border-radius: 6px;
  outline: 0;
  background: #0c0f11;
  color: var(--sc-text);
  font: inherit;
  font-size: 12.5px;
  box-sizing: border-box;
}

.sc-strategy-save input:focus {
  border-color: var(--sc-accent);
}

.sc-builder-notice {
  margin: 0 !important;
}

.sc-builder-layout {
  display: grid;
  grid-template-columns: minmax(390px, 0.72fr) minmax(560px, 1.35fr);
  gap: 14px;
  align-items: start;
}

.sc-rule-editor,
.sc-results-workspace {
  min-width: 0;
  overflow: hidden;
  scroll-margin-top: 88px;
}

.sc-rule-editor-head,
.sc-results-head {
  display: flex;
  min-height: 70px;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
  padding: 14px 18px;
  border-bottom: 1px solid var(--sc-line);
  box-sizing: border-box;
}

.sc-rule-editor-head > span,
.sc-results-head > span {
  color: var(--sc-text-3);
  font-size: 11.5px;
  line-height: 1.5;
  text-align: right;
}

.sc-result-tools {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 7px;
}

.sc-result-tools > span,
.sc-result-tools label {
  color: var(--sc-text-3);
  font-size: 11.5px;
}

.sc-result-tools label {
  display: inline-flex;
  align-items: center;
  gap: 7px;
}

.sc-result-sort {
  min-height: 30px;
  padding: 4px 26px 4px 8px;
  border: 1px solid var(--sc-line);
  border-radius: 5px;
  outline: 0;
  background: #0c0f11;
  color: var(--sc-text-2);
  font: inherit;
  font-size: 11.5px;
}

.sc-rule-group {
  padding: 15px 18px 17px;
  border-bottom: 1px solid var(--sc-line);
}

.sc-rule-group:last-child {
  border-bottom: 0;
}

.sc-rule-group > header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 11px;
}

.sc-rule-group > header strong {
  color: var(--sc-text);
  font-size: 13px;
}

.sc-rule-group > header span {
  color: var(--sc-text-3);
  font-size: 11px;
  text-align: right;
}

.sc-rule-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}

.sc-rule-grid-scope {
  grid-template-columns: minmax(132px, 0.58fr) minmax(0, 1.42fr);
}

.sc-range-field {
  display: grid;
  gap: 6px;
}

.sc-range-field > span {
  color: var(--sc-text-2);
  font-size: 12px;
}

.sc-range-field > div {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
  align-items: center;
  gap: 7px;
}

.sc-range-field em {
  color: var(--sc-text-3);
  font-size: 11px;
  font-style: normal;
}

.sc-rule-editor .sc-field {
  gap: 6px;
  color: var(--sc-text-2);
}

.sc-rule-editor .sc-input {
  width: 100%;
  min-width: 0;
  min-height: 40px;
  box-sizing: border-box;
  font-size: 13px;
}

.sc-results-workspace .sc-table-wrap {
  max-height: 620px;
  margin: 0;
  overflow: auto;
  border: 0;
  border-radius: 0;
}

.sc-results-workspace .sc-table th,
.sc-results-workspace .sc-table td {
  padding: 10px 12px;
}

.sc-results-workspace .sc-table td:first-child {
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.sc-results-workspace .sc-table .action {
  min-width: 150px;
  text-align: right;
}

.sc-row-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 6px;
  white-space: nowrap;
}

.sc-row-action {
  min-width: 68px;
  min-height: 30px;
  padding: 5px 9px;
  border: 1px solid var(--sc-line-strong);
  border-radius: 5px;
  background: var(--sc-accent-soft);
  color: var(--sc-accent-strong);
  font: inherit;
  font-size: 11.5px;
  cursor: pointer;
}

.sc-row-action:hover:not(:disabled) {
  border-color: var(--sc-accent);
  background: rgba(215, 183, 111, 0.16);
}

.sc-row-action.sc-row-action-ask {
  min-width: 52px;
  border-color: var(--sc-accent);
  background: var(--sc-accent);
  color: #17130a;
  font-weight: 700;
}

.sc-row-action.sc-row-action-ask:hover:not(:disabled) {
  border-color: var(--sc-accent-strong);
  background: var(--sc-accent-strong);
}

.sc-row-action.is-added {
  border-color: rgba(94, 169, 120, 0.28);
  background: rgba(94, 169, 120, 0.08);
  color: #8fc4a1;
}

.sc-row-action.has-error {
  border-color: rgba(221, 96, 96, 0.32);
  background: rgba(221, 96, 96, 0.08);
  color: #e59292;
}

.sc-results-workspace .sc-symbol {
  color: var(--sc-text-3);
  font-size: 10.5px;
}

.sc-result-error {
  margin: 14px 18px 0 !important;
}

.sc-result-ready,
.sc-result-loading,
.sc-result-empty {
  display: flex;
  min-height: 320px;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  padding: 34px;
  color: var(--sc-text-3);
  text-align: center;
  box-sizing: border-box;
}

.sc-result-ready strong,
.sc-result-loading strong,
.sc-result-empty strong {
  color: var(--sc-text);
  font-size: 14px;
}

.sc-result-ready p,
.sc-result-loading p,
.sc-result-empty p {
  max-width: 520px;
  color: var(--sc-text-3);
  font-size: 12.5px;
  line-height: 1.65;
}

.sc-empty-rules {
  display: flex;
  max-width: 620px;
  flex-wrap: wrap;
  justify-content: center;
  gap: 7px;
  margin: 4px 0 8px;
}

.sc-empty-rules span {
  padding: 5px 8px;
  border: 1px solid var(--sc-line);
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.02);
  color: var(--sc-text-2);
  font-size: 11px;
}

.sc-empty-actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 8px;
}

.sc-results-workspace .sc-interpret {
  margin: 0;
  border-width: 1px 0 0;
  border-radius: 0;
  background: rgba(255, 255, 255, 0.012);
}

.sc-saved-workspace {
  display: grid;
  gap: 14px;
}

.sc-saved-hero {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
  padding: 18px 20px;
}

.sc-saved-hero p {
  margin-top: 6px !important;
  color: var(--sc-text-3);
  font-size: 12.5px;
}

.sc-saved-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 14px;
}

.sc-saved-card {
  display: flex;
  min-height: 210px;
  flex-direction: column;
  padding: 17px;
}

.sc-saved-card > header,
.sc-saved-card > footer,
.sc-saved-card > footer > div {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.sc-saved-card > header h3 {
  font-size: 15px;
}

.sc-saved-card > header span {
  display: block;
  margin-top: 5px;
  color: var(--sc-text-3);
  font-size: 10.5px;
}

.sc-saved-card > header > strong {
  flex: none;
  color: var(--sc-accent-strong);
  font-size: 13px;
}

.sc-saved-card .sc-plan-chips {
  align-content: flex-start;
  flex: 1;
  margin: 15px 0;
}

.sc-saved-card > footer {
  padding-top: 13px;
  border-top: 1px solid var(--sc-line);
}

.sc-danger-text {
  color: #d98282;
}

.sc-saved-empty {
  display: flex;
  min-height: 280px;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  padding: 30px;
  color: var(--sc-text-3);
  text-align: center;
}

.sc-saved-empty strong {
  color: var(--sc-text);
  font-size: 14px;
}

.sc-saved-empty p {
  max-width: 480px;
  color: var(--sc-text-3);
  font-size: 12.5px;
  line-height: 1.65;
}

.sc-pagebar {
  display: grid;
  grid-template-columns: minmax(190px, 1fr) auto minmax(300px, 1fr);
  align-items: center;
  gap: 24px;
  min-height: 76px;
  margin: 0 -24px 22px;
  padding: 0 24px;
  border-bottom: 1px solid var(--sc-line);
  background: rgba(10, 12, 14, 0.96);
}

.sc-brand h2,
.sc-brand p,
.sc-panel h3,
.sc-panel h4,
.sc-panel p {
  margin: 0;
}

.sc-brand h2 {
  font-size: 18px;
  line-height: 1.2;
  letter-spacing: 0.02em;
}

.sc-brand p {
  margin-top: 5px;
  color: var(--sc-text-3);
  font-size: 11.5px;
}

.sc-page-tabs {
  display: flex;
  align-self: stretch;
}

.sc-page-tabs button {
  position: relative;
  min-width: 88px;
  padding: 0 16px;
  border: 0;
  background: transparent;
  color: var(--sc-text-3);
  font: inherit;
  font-size: 13px;
}

.sc-page-tabs button:not(:disabled) {
  cursor: pointer;
}

.sc-page-tabs button.is-active {
  color: var(--sc-text);
  font-weight: 650;
}

.sc-page-tabs button.is-active::after {
  position: absolute;
  right: 16px;
  bottom: 0;
  left: 16px;
  height: 2px;
  background: var(--sc-accent);
  content: '';
}

.sc-page-tabs button:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.sc-page-actions,
.sc-data-status {
  display: flex;
  align-items: center;
}

.sc-page-actions {
  justify-content: flex-end;
  gap: 8px;
}

.sc-data-status {
  gap: 7px;
  margin-right: 4px;
  color: var(--sc-text-3);
  font-size: 11.5px;
  white-space: nowrap;
}

.sc-data-status button,
.sc-text-btn {
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--sc-accent-strong);
  font: inherit;
  cursor: pointer;
}

.sc-data-status button:disabled,
.sc-text-btn:disabled {
  opacity: 0.55;
  cursor: default;
}

.sc-status-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #5ea978;
  box-shadow: 0 0 0 3px rgba(94, 169, 120, 0.12);
}

.sc-btn {
  display: inline-flex;
  min-height: 34px;
  align-items: center;
  justify-content: center;
  gap: 7px;
  padding: 7px 14px;
  border: 1px solid transparent;
  border-radius: 6px;
  color: var(--sc-text);
  font: inherit;
  font-size: 12.5px;
  line-height: 1;
  cursor: pointer;
  transition: border-color 140ms ease, background-color 140ms ease, color 140ms ease;
}

.sc-btn:disabled {
  opacity: 0.48;
  cursor: default;
}

.sc-btn-primary {
  border-color: var(--sc-accent);
  background: var(--sc-accent);
  color: #17140e;
  font-weight: 700;
}

.sc-btn-primary:hover:not(:disabled) {
  border-color: var(--sc-accent-strong);
  background: var(--sc-accent-strong);
}

.sc-btn-ghost {
  border-color: var(--sc-line);
  background: transparent;
  color: var(--sc-text-2);
}

.sc-btn-ghost:hover:not(:disabled) {
  border-color: var(--sc-line-strong);
  color: var(--sc-text);
}

.sc-btn-soft {
  min-height: 30px;
  padding: 6px 11px;
  border-color: var(--sc-line-strong);
  background: var(--sc-accent-soft);
  color: var(--sc-accent-strong);
}

.sc-workspace {
  display: grid;
  gap: 16px;
}

.sc-panel {
  border: 1px solid var(--sc-line);
  border-radius: 8px;
  background: var(--sc-panel);
}

.sc-start-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.65fr) minmax(300px, 0.8fr);
  gap: 16px;
}

.sc-composer,
.sc-profile,
.sc-personalized,
.sc-template-library,
.sc-history,
.sc-plan-confirm,
.sc-advanced {
  padding: 18px;
}

.sc-panel-head,
.sc-section-head,
.sc-card-title,
.sc-recommend-card footer,
.sc-plan-actions {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.sc-eyebrow {
  display: block;
  margin-bottom: 7px;
  color: var(--sc-accent-strong);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.08em;
}

.sc-panel h3 {
  color: var(--sc-text);
  font-size: 16px;
  line-height: 1.35;
}

.sc-panel-head > div > p {
  margin-top: 7px;
  color: var(--sc-text-3);
  font-size: 12px;
  line-height: 1.6;
}

.sc-saved-state,
.sc-count-badge {
  flex: none;
  padding: 4px 8px;
  border: 1px solid var(--sc-line);
  border-radius: 999px;
  color: var(--sc-text-3);
  font-size: 10.5px;
}

.sc-prompt-box {
  margin-top: 16px;
  overflow: hidden;
  border: 1px solid var(--sc-line-strong);
  border-radius: 7px;
  background: #0c0f11;
}

.sc-prompt-box:focus-within {
  border-color: var(--sc-accent);
  box-shadow: 0 0 0 2px rgba(215, 183, 111, 0.08);
}

.sc-prompt-box.has-error {
  border-color: rgba(221, 96, 96, 0.65);
}

.sc-prompt-box textarea {
  display: block;
  width: 100%;
  min-height: 112px;
  resize: vertical;
  padding: 15px 16px;
  border: 0;
  outline: 0;
  background: transparent;
  color: var(--sc-text);
  font: inherit;
  font-size: 14px;
  line-height: 1.7;
  box-sizing: border-box;
}

.sc-prompt-box textarea::placeholder {
  color: #5f6467;
}

.sc-prompt-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  min-height: 36px;
  padding: 0 14px;
  border-top: 1px solid var(--sc-line);
  color: var(--sc-text-3);
  font-size: 11px;
}

.sc-composer-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 12px;
}

.sc-text-btn {
  font-size: 12px;
}

.sc-generate {
  min-width: 146px;
}

.sc-inline-error,
.sc-parse-warning {
  margin-top: 10px;
  color: #df7b75;
  font-size: 12px;
}

.sc-profile {
  display: flex;
  min-height: 248px;
  flex-direction: column;
}

.sc-profile-summary {
  display: -webkit-box;
  margin-top: 17px !important;
  overflow: hidden;
  color: var(--sc-text-2);
  font-size: 12.5px;
  line-height: 1.7;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 3;
}

.sc-tag-row {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
  margin-top: 14px;
}

.sc-tag-row span,
.sc-plan-chips span {
  padding: 5px 9px;
  border: 1px solid var(--sc-line-strong);
  border-radius: 999px;
  background: var(--sc-accent-soft);
  color: var(--sc-accent-strong);
  font-size: 11px;
}

.sc-profile-facts {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 9px;
  margin: 15px 0 12px;
}

.sc-profile-facts div {
  padding: 9px 10px;
  border: 1px solid var(--sc-line);
  border-radius: 6px;
  background: #0f1214;
}

.sc-profile-facts span,
.sc-profile-facts strong {
  display: block;
}

.sc-profile-facts span {
  color: var(--sc-text-3);
  font-size: 10.5px;
}

.sc-profile-facts strong {
  margin-top: 4px;
  color: var(--sc-text-2);
  font-size: 11.5px;
  font-weight: 600;
}

.sc-profile-detail {
  margin: 0 0 8px !important;
  color: var(--sc-text-3);
  font-size: 11px;
  line-height: 1.55;
}

.sc-profile-empty {
  margin-top: 22px;
  padding: 16px;
  border: 1px dashed var(--sc-line);
  border-radius: 7px;
  color: var(--sc-text-2);
}

.sc-profile-empty p {
  margin-top: 7px;
  color: var(--sc-text-3);
  font-size: 11.5px;
  line-height: 1.55;
}

.sc-profile-refresh {
  align-self: flex-start;
  margin-top: auto;
}

.sc-section-head {
  align-items: flex-end;
  margin-bottom: 16px;
}

.sc-section-head > span {
  color: var(--sc-text-3);
  font-size: 11px;
}

.sc-recommend-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
}

.sc-recommend-card {
  padding: 15px;
  border: 1px solid var(--sc-line);
  border-radius: 7px;
  background: var(--sc-panel-raised);
}

.sc-card-title h4 {
  font-size: 13.5px;
}

.sc-card-title span {
  padding: 3px 7px;
  border-radius: 999px;
  background: var(--sc-accent-soft);
  color: var(--sc-accent-strong);
  font-size: 10px;
}

.sc-template-prompt {
  min-height: 42px;
  margin-top: 14px !important;
  color: var(--sc-text-2);
  font-size: 12.5px;
  line-height: 1.65;
}

.sc-rationale {
  display: -webkit-box;
  min-height: 34px;
  margin-top: 8px !important;
  overflow: hidden;
  color: var(--sc-text-3);
  font-size: 11px;
  line-height: 1.55;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

.sc-recommend-card footer {
  align-items: center;
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px solid var(--sc-line);
}

.sc-recommend-card footer > span {
  color: var(--sc-text-3);
  font-size: 10.5px;
}

.sc-library-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 16px;
}

.sc-filter-tabs {
  display: flex;
  gap: 4px;
  margin-bottom: 11px;
  padding-bottom: 9px;
  overflow-x: auto;
  border-bottom: 1px solid var(--sc-line);
}

.sc-filter-tabs button {
  flex: none;
  padding: 5px 9px;
  border: 1px solid transparent;
  border-radius: 5px;
  background: transparent;
  color: var(--sc-text-3);
  font: inherit;
  font-size: 11.5px;
  cursor: pointer;
}

.sc-filter-tabs button.is-active {
  border-color: var(--sc-line-strong);
  background: var(--sc-accent-soft);
  color: var(--sc-accent-strong);
}

.sc-template-list,
.sc-history-list {
  display: grid;
}

.sc-template-row {
  display: grid;
  grid-template-columns: 34px minmax(0, 1fr) auto 14px;
  align-items: center;
  gap: 10px;
  min-height: 58px;
  padding: 8px 3px;
  border: 0;
  border-bottom: 1px solid var(--sc-line);
  background: transparent;
  color: var(--sc-text);
  text-align: left;
  cursor: pointer;
}

.sc-template-row:hover {
  background: rgba(255, 255, 255, 0.02);
}

.sc-template-icon {
  display: grid;
  width: 30px;
  height: 30px;
  place-items: center;
  border: 1px solid var(--sc-line-strong);
  border-radius: 6px;
  color: var(--sc-accent-strong);
  font-size: 12px;
}

.sc-template-copy,
.sc-history-copy {
  min-width: 0;
}

.sc-history-entry {
  display: block;
  min-width: 0;
  padding: 10px 0;
  border: 0;
  background: transparent;
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.sc-history-entry:hover .sc-history-prompt,
.sc-history-entry:focus-visible .sc-history-prompt {
  color: var(--sc-accent-strong);
}

.sc-history-entry:focus-visible {
  border-radius: 4px;
  outline: 1px solid var(--sc-line-strong);
  outline-offset: 3px;
}

.sc-history-entry:disabled {
  cursor: wait;
  opacity: 0.65;
}

.sc-template-copy strong,
.sc-template-copy small {
  display: block;
}

.sc-template-copy strong {
  font-size: 12.5px;
}

.sc-template-copy small {
  margin-top: 4px;
  overflow: hidden;
  color: var(--sc-text-3);
  font-size: 10.5px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sc-template-meta,
.sc-row-arrow {
  color: var(--sc-text-3);
  font-size: 10.5px;
}

.sc-row-arrow {
  font-size: 18px;
}

.sc-history-search {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
  padding: 0 10px;
  border: 1px solid var(--sc-line);
  border-radius: 6px;
  background: #0d1012;
  color: var(--sc-text-3);
}

.sc-history-search:focus-within {
  border-color: var(--sc-line-strong);
}

.sc-history-search input {
  width: 100%;
  height: 34px;
  border: 0;
  outline: 0;
  background: transparent;
  color: var(--sc-text);
  font: inherit;
  font-size: 11.5px;
}

.sc-history-row {
  display: grid;
  grid-template-columns: 24px minmax(0, 1fr) auto;
  align-items: center;
  gap: 8px;
  min-height: 62px;
  border-bottom: 1px solid var(--sc-line);
}

.sc-star {
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--sc-text-3);
  font-size: 17px;
  cursor: pointer;
}

.sc-star.is-active {
  color: var(--sc-accent-strong);
}

.sc-history-prompt {
  display: block;
  overflow: hidden;
  color: var(--sc-text-2);
  font-size: 11.5px;
  text-overflow: ellipsis;
  transition: color 140ms ease;
  white-space: nowrap;
}

.sc-history-copy span {
  display: block;
  margin-top: 5px;
  color: var(--sc-text-3);
  font-size: 10px;
}

.sc-history-copy .sc-history-prompt {
  margin-top: 0;
  color: var(--sc-text-2);
  font-size: 11.5px;
}

.sc-builder-parse-notice {
  margin: 0 0 4px;
}

.sc-history-more {
  width: 100%;
  margin-top: 10px;
  padding: 7px;
  border: 0;
  background: transparent;
  color: var(--sc-accent-strong);
  font: inherit;
  font-size: 11.5px;
  cursor: pointer;
}

.sc-loading-state,
.sc-empty-line {
  padding: 22px 10px;
  color: var(--sc-text-3);
  font-size: 12px;
  text-align: center;
}

.sc-empty-line .sc-text-btn {
  margin-left: 8px;
}

.sc-plan-confirm {
  border-color: var(--sc-line-strong);
}

.sc-plan-summary {
  color: var(--sc-text-2);
  font-size: 12.5px;
  line-height: 1.65;
}

.sc-plan-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
  margin-top: 13px;
}

.sc-plan-actions {
  align-items: center;
  margin-top: 17px;
  padding-top: 14px;
  border-top: 1px solid var(--sc-line);
}

.sc-advanced .sc-config-v2 {
  display: grid;
  grid-template-columns: repeat(6, minmax(110px, 1fr));
  gap: 12px;
  margin: 0;
  padding: 0;
  border: 0;
  background: transparent;
}

.sc-advanced .sc-field-wide {
  grid-column: span 2;
}

.sc-advanced .sc-input {
  width: 100%;
  box-sizing: border-box;
}

.sc-top-notice,
.sc-sync-note {
  margin: -10px 0 16px;
}

.sc-sync-note {
  padding: 9px 11px;
  border: 1px solid rgba(94, 169, 120, 0.22);
  border-radius: 6px;
  background: rgba(94, 169, 120, 0.06);
  color: #8fb99e;
  font-size: 11.5px;
}

.sc-result-error,
.sc-summary,
.sc-wrap > .sc-note,
.sc-table-wrap,
.sc-interpret,
.sc-wrap > template + .sc-state {
  margin-top: 16px;
}

/* 策略工作台：对应设计稿的规则 / 结果 / 解释三栏结构。 */
.sc-strategy-workbench { gap: 12px; }
.sc-strategy-hero {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 14px 20px;
  padding: 18px 8px 12px;
  border-bottom: 1px solid var(--sc-line);
}
.sc-strategy-heading,
.sc-hero-actions,
.sc-strategy-chips { display: flex; align-items: center; gap: 10px; }
.sc-strategy-heading h2 { margin: 3px 0 0; font-size: 24px; font-weight: 650; letter-spacing: .01em; }
.sc-run-status { padding: 5px 11px; border: 1px solid rgba(215,183,111,.22); border-radius: 5px; background: rgba(215,183,111,.08); color: var(--sc-accent); font-size: 12px; }
.sc-run-status.is-ready { border-color: rgba(70,181,123,.28); background: rgba(70,181,123,.09); color: #6dc794; }
.sc-last-run { color: var(--sc-text-3); font-size: 12px; }
.sc-hero-actions { justify-content: flex-end; }
.sc-hero-actions input { width: 150px; min-height: 34px; padding: 0 10px; border: 1px solid var(--sc-line); border-radius: 5px; outline: none; background: #0b0e10; color: var(--sc-text); font: inherit; font-size: 12px; }
.sc-hero-actions input:focus { border-color: var(--sc-accent); }
.sc-strategy-chips { grid-column: 1 / -1; flex-wrap: wrap; }
.sc-rule-chip { min-height: 34px; padding: 6px 11px; border: 1px solid var(--sc-line); border-radius: 5px; background: #101417; color: var(--sc-text-2); font: inherit; font-size: 12px; cursor: pointer; }
.sc-rule-chip:hover { border-color: var(--sc-line-strong); color: var(--sc-text); }
.sc-rule-chip span { margin-left: 5px; color: var(--sc-text-3); }
.sc-rule-chip.is-empty { border-style: dashed; color: var(--sc-accent); }

.sc-strategy-layout { display: grid; grid-template-columns: minmax(235px, .75fr) minmax(620px, 2.25fr) minmax(270px, .95fr); gap: 12px; height: calc(100dvh - 340px); min-height: 600px; align-items: stretch; }
.sc-strategy-sidebar,
.sc-results-center,
.sc-explain-panel { min-width: 0; overflow: hidden; border-color: rgba(255,255,255,.11); background: linear-gradient(180deg,#14191d,#111518); }
.sc-results-center { display: flex; flex-direction: column; }
.sc-strategy-sidebar,
.sc-explain-panel { overflow-y: auto; }
.sc-strategy-sidebar > header,
.sc-explain-panel > header { display: flex; align-items: center; justify-content: space-between; min-height: 55px; padding: 0 14px; border-bottom: 1px solid var(--sc-line); }
.sc-strategy-sidebar > header h3,
.sc-explain-panel > header h3 { margin: 0; font-size: 15px; }
.sc-strategy-sidebar > header span,
.sc-explain-panel > header span { color: var(--sc-text-3); font-size: 10.5px; }
.sc-logic-card { margin: 10px 10px 0; padding: 12px; border: 1px solid var(--sc-line); border-radius: 6px; background: rgba(255,255,255,.012); }
.sc-logic-title { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 9px; }
.sc-logic-title strong { font-size: 12.5px; }
.sc-logic-title > button { border: 0; background: transparent; color: var(--sc-text-3); cursor: pointer; }
.sc-logic-title > span { color: var(--sc-text-3); font-size: 10px; }
.sc-logic-card ul { display: grid; gap: 7px; margin: 0; padding: 0; list-style: none; }
.sc-logic-card li { display: flex; gap: 7px; align-items: flex-start; color: var(--sc-text-2); font-size: 11.5px; line-height: 1.45; }
.sc-logic-card li i { min-width: 14px; color: var(--sc-text-3); font-style: normal; }
.sc-logic-card li i.is-ok { color: #5db987; }
.sc-logic-card li i.is-rule { width: 10px; min-width: 10px; height: 10px; margin-top: 2px; border: 1px solid var(--sc-line-strong); border-radius: 2px; background: #0c0f11; box-shadow: inset 0 0 0 2px #0c0f11; }
.sc-no-rule { margin: 0; color: var(--sc-text-3); font-size: 11px; }
.sc-score-weights label { display: grid; grid-template-columns: 64px minmax(0,1fr) 34px; align-items: center; gap: 7px; margin-top: 9px; color: var(--sc-text-2); font-size: 11px; }
.sc-score-weights input { width: 100%; accent-color: var(--sc-accent); }
.sc-score-weights b { color: var(--sc-text-2); font-size: 10px; text-align: right; }
.sc-score-weights small { display: block; margin-top: 9px; color: var(--sc-text-3); font-size: 9.5px; line-height: 1.5; }
.sc-add-condition { width: calc(100% - 20px); min-height: 38px; margin: 12px 10px; border: 1px solid var(--sc-line-strong); border-radius: 5px; background: transparent; color: var(--sc-accent); font: inherit; font-size: 11.5px; cursor: pointer; }

.sc-center-head { display: flex; align-items: center; justify-content: space-between; min-height: 63px; padding: 0 14px; border-bottom: 1px solid var(--sc-line); }
.sc-center-head h3 { margin: 0 0 5px; font-size: 15px; }
.sc-center-head h3 b { margin-left: 5px; color: var(--sc-text-2); font-size: 12px; }
.sc-center-head span { color: var(--sc-text-3); font-size: 10.5px; }
.sc-center-tools label { display: flex; align-items: center; gap: 6px; color: var(--sc-text-3); font-size: 10.5px; }
.sc-result-tabs { display: flex; align-items: stretch; justify-content: space-between; gap: 18px; min-height: 43px; padding: 0 14px; border-bottom: 1px solid var(--sc-line); }
.sc-result-tabset { display: flex; gap: 28px; }
.sc-result-tabset button { position: relative; border: 0; background: transparent; color: var(--sc-text-3); font: inherit; font-size: 11.5px; cursor: pointer; }
.sc-result-tabset button.active { color: var(--sc-accent); }
.sc-result-tabset button.active::after { position: absolute; right: 0; bottom: 0; left: 0; height: 2px; background: var(--sc-accent); content: ''; }
.sc-result-tabs em { padding: 1px 4px; border-radius: 999px; background: rgba(215,183,111,.14); font-size: 9px; font-style: normal; }
.sc-result-toolbar { display: flex; align-items: center; gap: 7px; margin-left: auto; }
.sc-result-toolbar button,
.sc-result-toolbar input,
.sc-result-toolbar .sc-result-sort { height: 30px; box-sizing: border-box; border: 1px solid var(--sc-line); border-radius: 4px; outline: 0; background: #0c0f11; color: var(--sc-text-2); font: inherit; font-size: 10.5px; }
.sc-result-toolbar button { padding: 0 10px; cursor: pointer; }
.sc-result-toolbar button:hover,
.sc-result-toolbar button[aria-expanded="true"] { border-color: var(--sc-line-strong); color: var(--sc-accent); }
.sc-result-toolbar .sc-result-sort { min-height: 30px; padding: 0 28px 0 9px; }
.sc-column-picker { position: relative; }
.sc-column-menu { position: absolute; z-index: 12; top: calc(100% + 7px); right: 0; display: grid; min-width: 150px; padding: 7px; border: 1px solid var(--sc-line-strong); border-radius: 5px; background: #111619; box-shadow: 0 14px 32px rgba(0,0,0,.42); }
.sc-column-menu label { display: flex; align-items: center; gap: 8px; min-height: 30px; padding: 0 7px; border-radius: 4px; color: var(--sc-text-2); font-size: 10.5px; cursor: pointer; }
.sc-column-menu label:hover { background: rgba(255,255,255,.035); }
.sc-column-menu input { width: 14px; height: 14px; accent-color: var(--sc-accent); }
.sc-result-search { display: flex; align-items: center; }
.sc-result-search input { width: 132px; margin-right: 5px; padding: 0 8px; }
.sc-result-search button { width: 32px; padding: 0; font-size: 18px; line-height: 1; }
.sc-ranking-table-wrap { min-height: 390px; flex: 1; overflow: auto; }
.sc-ranking-table { width: 100%; border-collapse: collapse; font-size: 11.5px; }
.sc-ranking-table th { padding: 11px 8px; border-bottom: 1px solid var(--sc-line); background: rgba(255,255,255,.015); color: var(--sc-text-3); font-weight: 500; text-align: left; white-space: nowrap; }
.sc-ranking-table td { padding: 13px 8px; border-bottom: 1px solid rgba(255,255,255,.065); color: var(--sc-text-2); vertical-align: middle; }
.sc-ranking-table tbody tr { cursor: pointer; transition: background .15s; }
.sc-ranking-table tbody tr:hover { background: rgba(255,255,255,.022); }
.sc-ranking-table tbody tr.selected { background: linear-gradient(90deg,rgba(215,183,111,.12),rgba(215,183,111,.035)); box-shadow: inset 2px 0 var(--sc-accent); }
.sc-ranking-table tbody tr.is-checked { background: rgba(215,183,111,.065); }
.sc-ranking-table td:nth-child(3) strong,
.sc-ranking-table td:nth-child(3) span { display: block; white-space: nowrap; }
.sc-ranking-table td:nth-child(3) strong { color: var(--sc-text); }
.sc-ranking-table td:nth-child(3) span { margin-top: 3px; color: var(--sc-text-3); font-size: 10px; }
.sc-check-cell { width: 34px; box-sizing: border-box; text-align: center !important; }
.sc-check-cell input,
.sc-factor-checkbox { width: 15px; height: 15px; margin: 0; accent-color: var(--sc-accent); cursor: pointer; }
.sc-score-cell b { color: var(--sc-accent-strong); font-size: 14px; }
.sc-reason-tags { display: flex; gap: 4px; white-space: nowrap; }
.sc-reason-tags span { padding: 3px 6px; border: 1px solid var(--sc-line); border-radius: 4px; color: var(--sc-text-2); background: rgba(255,255,255,.015); }
.sc-risk-cell { max-width: 110px; color: #b9aaa0 !important; }
.sc-mini-ask { padding: 4px 7px; border: 1px solid var(--sc-line-strong); border-radius: 4px; background: rgba(215,183,111,.08); color: var(--sc-accent); font: inherit; font-size: 10px; cursor: pointer; }
.sc-result-footer { display: flex; align-items: center; justify-content: space-between; min-height: 52px; padding: 0 14px; border-top: 1px solid var(--sc-line); color: var(--sc-text-3); font-size: 11px; }
.sc-result-footer > div { display: flex; gap: 4px; }
.sc-result-footer button { min-width: 26px; height: 26px; border: 1px solid transparent; border-radius: 4px; background: transparent; color: var(--sc-text-3); cursor: pointer; }
.sc-result-footer button.active { border-color: var(--sc-line-strong); color: var(--sc-accent); }
.sc-result-footer button:disabled { opacity: .3; }
.sc-workbench-actions { display: grid; grid-template-columns: auto repeat(5, minmax(0,1fr)); gap: 8px; margin-top: auto; padding: 11px 14px; border-top: 1px solid var(--sc-line); }
.sc-workbench-actions button { min-height: 38px; border: 1px solid var(--sc-line); border-radius: 5px; background: transparent; color: var(--sc-text-2); font: inherit; font-size: 11.5px; cursor: pointer; }
.sc-workbench-actions button:hover:not(:disabled) { border-color: var(--sc-line-strong); color: var(--sc-accent); }
.sc-workbench-actions button.primary { border-color: var(--sc-accent); background: var(--sc-accent); color: #17130a; font-weight: 700; }
.sc-workbench-actions button:disabled { cursor: default; opacity: .35; }
.sc-selection-summary { display: flex; min-width: 88px; align-items: center; gap: 7px; color: var(--sc-text-2); font-size: 10.5px; white-space: nowrap; }
.sc-selection-summary button { min-height: 0; padding: 0; border: 0; color: var(--sc-accent); }
.sc-action-error { margin: 0; padding: 0 14px 10px; color: #e18a8a; font-size: 10.5px; }
.sc-factor-board,
.sc-compare-board { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 8px; min-height: 390px; flex: 1; padding: 10px; box-sizing: border-box; align-content: start; }
.sc-factor-board article,
.sc-compare-board article { padding: 10px; border: 1px solid var(--sc-line); border-radius: 6px; background: rgba(255,255,255,.012); cursor: pointer; }
.sc-factor-board article { position: relative; padding-left: 34px; }
.sc-factor-board article.selected { border-color: var(--sc-line-strong); background: rgba(215,183,111,.05); }
.sc-factor-board article.is-checked { box-shadow: inset 2px 0 var(--sc-accent); }
.sc-factor-checkbox { position: absolute; top: 11px; left: 10px; }
.sc-factor-board header,
.sc-compare-board header { display: flex; justify-content: space-between; margin-bottom: 9px; }
.sc-factor-board header b,
.sc-compare-board header > b { color: var(--sc-accent); }
.sc-factor-board article > div,
.sc-contribution > div,
.sc-distribution > div { display: grid; grid-template-columns: 72px minmax(0,1fr) 34px; align-items: center; gap: 7px; margin-top: 8px; color: var(--sc-text-3); font-size: 10.5px; }
.sc-factor-board i,
.sc-contribution i,
.sc-distribution i { height: 5px; overflow: hidden; border-radius: 999px; background: rgba(255,255,255,.08); }
.sc-factor-board i em,
.sc-contribution i em,
.sc-distribution i em { display: block; height: 100%; border-radius: inherit; background: var(--sc-accent); }
.sc-compare-board header span { display: block; margin-top: 3px; color: var(--sc-text-3); font-size: 9px; }
.sc-compare-board dl { margin: 0; }
.sc-compare-board dl div { display: flex; justify-content: space-between; padding: 5px 0; border-top: 1px solid var(--sc-line); font-size: 10px; }
.sc-compare-board dt { color: var(--sc-text-3); }
.sc-compare-board dd { margin: 0; color: var(--sc-text-2); }
.sc-compare-board button { width: 100%; margin-top: 7px; border: 0; background: transparent; color: var(--sc-accent); font: inherit; font-size: 10px; cursor: pointer; }

.sc-selected-stock { display: flex; align-items: center; justify-content: space-between; padding: 14px; border-bottom: 1px solid var(--sc-line); }
.sc-selected-stock strong { display: block; font-size: 14px; }
.sc-selected-stock span { display: block; margin-top: 5px; color: var(--sc-text-3); font-size: 10.5px; }
.sc-selected-stock > b { color: var(--sc-accent); font-size: 25px; font-weight: 500; }
.sc-contribution,
.sc-explain-list,
.sc-completeness,
.sc-distribution { padding: 12px 14px; border-bottom: 1px solid var(--sc-line); }
.sc-contribution h4,
.sc-explain-list h4,
.sc-distribution h4 { margin: 0 0 10px; color: var(--sc-text-2); font-size: 12px; }
.sc-contribution > div,
.sc-distribution > div { grid-template-columns: 96px minmax(0,1fr) 34px; }
.sc-explain-list ul { display: grid; gap: 8px; margin: 0; padding-left: 16px; color: var(--sc-text-2); font-size: 11px; line-height: 1.5; }
.sc-explain-list li::marker { color: var(--sc-accent); }
.sc-completeness { display: flex; justify-content: space-between; color: var(--sc-text-2); font-size: 11px; }
.sc-completeness strong { color: #68c693; }
.sc-explain-empty { display: flex; min-height: 560px; flex-direction: column; align-items: center; justify-content: center; gap: 8px; padding: 24px; color: var(--sc-text-3); text-align: center; }
.sc-explain-empty strong { color: var(--sc-text); font-size: 13px; }
.sc-explain-empty p { font-size: 10.5px; line-height: 1.5; }

.sc-rule-config-panel { scroll-margin-top: 80px; }
.sc-rule-config-panel > header { display: flex; align-items: center; justify-content: space-between; padding: 14px 16px; border-bottom: 1px solid var(--sc-line); }
.sc-rule-config-panel > header > div:last-child { display: flex; gap: 8px; }
.sc-rule-config-panel h3 { margin: 3px 0 0; font-size: 14px; }
.sc-config-grid { display: grid; grid-template-columns: repeat(5,minmax(0,1fr)); }
.sc-config-grid > section { display: grid; align-content: start; gap: 10px; padding: 13px; border-right: 1px solid var(--sc-line); }
.sc-config-grid > section:last-child { border-right: 0; }
.sc-config-grid h4 { margin: 0; font-size: 11.5px; }
.sc-config-grid .sc-input { width: 100%; min-width: 0; min-height: 36px; box-sizing: border-box; }

@media (max-width: 1280px) {
  .sc-pagebar {
    grid-template-columns: 1fr auto;
  }

  .sc-page-tabs {
    order: 3;
    grid-column: 1 / -1;
    min-height: 42px;
    border-top: 1px solid var(--sc-line);
  }

  .sc-pagebar {
    padding-top: 13px;
  }

  .sc-advanced .sc-config-v2 {
    grid-template-columns: repeat(4, minmax(110px, 1fr));
  }

  .sc-builder-layout {
    grid-template-columns: 390px minmax(0, 1fr);
  }

  .sc-saved-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 960px) {
  .sc-start-grid,
  .sc-library-grid {
    grid-template-columns: 1fr;
  }

  .sc-recommend-grid {
    grid-template-columns: 1fr;
  }

  .sc-profile {
    min-height: 0;
  }

  .sc-profile-refresh {
    margin-top: 16px;
  }

  .sc-advanced .sc-config-v2 {
    grid-template-columns: repeat(2, minmax(110px, 1fr));
  }

  .sc-builder-layout {
    grid-template-columns: 1fr;
  }

  .sc-saved-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 720px) {
  .sc-wrap {
    padding: 0 12px 24px;
  }

  .sc-pagebar {
    grid-template-columns: 1fr;
    gap: 12px;
    margin: 0 -12px 14px;
    padding: 14px 12px 0;
  }

  .sc-page-actions {
    justify-content: flex-start;
    flex-wrap: wrap;
  }

  .sc-data-status {
    width: 100%;
    white-space: normal;
  }

  .sc-page-tabs {
    order: 4;
    grid-column: auto;
    width: 100%;
    overflow-x: auto;
  }

  .sc-page-tabs button {
    flex: 1;
    min-width: 92px;
  }

  .sc-composer,
  .sc-profile,
  .sc-personalized,
  .sc-template-library,
  .sc-history,
  .sc-plan-confirm,
  .sc-advanced {
    padding: 14px;
  }

  .sc-panel-head,
  .sc-section-head {
    align-items: flex-start;
  }

  .sc-section-head > span {
    display: none;
  }

  .sc-composer-actions,
  .sc-plan-actions {
    align-items: stretch;
    flex-direction: column;
  }

  .sc-generate,
  .sc-plan-actions .sc-btn {
    width: 100%;
  }

  .sc-template-row {
    grid-template-columns: 32px minmax(0, 1fr) 12px;
  }

  .sc-template-meta {
    display: none;
  }

  .sc-history-row {
    grid-template-columns: 22px minmax(0, 1fr);
    padding: 9px 0;
  }

  .sc-history-row .sc-btn {
    grid-column: 2;
    justify-self: start;
  }

  .sc-advanced .sc-config-v2 {
    grid-template-columns: 1fr;
  }

  .sc-advanced .sc-field-wide {
    grid-column: span 1;
  }

  .sc-builder-commandbar {
    grid-template-columns: 1fr;
    padding: 15px;
  }

  .sc-builder-command-actions {
    justify-content: stretch;
  }

  .sc-builder-command-actions .sc-btn {
    flex: 1;
    min-height: 42px;
  }

  .sc-builder-rule-summary {
    grid-column: auto;
    flex-direction: column;
  }

  .sc-strategy-save {
    width: 100%;
    margin-left: 0;
  }

  .sc-strategy-save input {
    min-height: 42px;
    flex: 1;
    width: auto;
  }

  .sc-saved-hero {
    align-items: flex-start;
    flex-direction: column;
    padding: 15px;
  }

  .sc-saved-hero .sc-btn {
    width: 100%;
  }

  .sc-saved-card > footer {
    align-items: flex-start;
    flex-direction: column;
  }

  .sc-saved-card > footer > div {
    width: 100%;
  }

  .sc-saved-card > footer > div .sc-btn {
    flex: 1;
  }

  .sc-rule-editor-head,
  .sc-results-head {
    min-height: 0;
    align-items: flex-start;
    padding: 14px;
  }

  .sc-results-head {
    flex-direction: column;
    gap: 7px;
  }

  .sc-results-head > span {
    text-align: left;
  }

  .sc-result-tools {
    width: 100%;
    align-items: flex-start;
  }

  .sc-rule-group {
    padding: 14px;
  }

  .sc-rule-group > header {
    align-items: flex-start;
    flex-direction: column;
    gap: 4px;
  }

  .sc-rule-group > header span {
    text-align: left;
  }

  .sc-rule-grid,
  .sc-rule-grid-scope {
    grid-template-columns: 1fr;
  }

  .sc-rule-editor .sc-input {
    min-height: 44px;
  }

  .sc-result-ready,
  .sc-result-loading,
  .sc-result-empty {
    min-height: 240px;
    padding: 26px 18px;
  }
}

@media (max-width: 1380px) {
  .sc-strategy-layout { grid-template-columns: 240px minmax(0,1fr); height: auto; min-height: 0; }
  .sc-explain-panel { grid-column: 1 / -1; }
  .sc-explain-panel:not(:has(.sc-explain-empty)) { display: grid; grid-template-columns: repeat(4,minmax(0,1fr)); }
  .sc-explain-panel > header,
  .sc-selected-stock { grid-column: 1 / -1; }
  .sc-explain-panel .sc-distribution { border-right: 0; }
  .sc-config-grid { grid-template-columns: repeat(3,minmax(0,1fr)); }
  .sc-config-grid > section { border-bottom: 1px solid var(--sc-line); }
}

@media (max-width: 980px) {
  .sc-strategy-hero { grid-template-columns: 1fr; }
  .sc-hero-actions { justify-content: flex-start; flex-wrap: wrap; }
  .sc-strategy-chips { grid-column: auto; }
  .sc-strategy-layout { grid-template-columns: 1fr; }
  .sc-explain-panel { grid-column: auto; }
  .sc-explain-panel:not(:has(.sc-explain-empty)) { display: block; }
  .sc-ranking-table-wrap { overflow-x: auto; }
  .sc-ranking-table { min-width: 840px; }
  .sc-result-tabs { align-items: flex-start; flex-direction: column; gap: 0; padding-top: 4px; }
  .sc-result-tabset { min-height: 39px; }
  .sc-result-toolbar { width: 100%; min-height: 42px; margin-left: 0; padding-bottom: 6px; }
  .sc-config-grid { grid-template-columns: repeat(2,minmax(0,1fr)); }
}

@media (max-width: 720px) {
  .sc-strategy-hero { padding: 12px 0; }
  .sc-strategy-heading { align-items: flex-start; flex-wrap: wrap; }
  .sc-strategy-heading > div { width: 100%; }
  .sc-strategy-heading h2 { font-size: 19px; }
  .sc-hero-actions { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); }
  .sc-hero-actions input { width: 100%; min-height: 42px; box-sizing: border-box; }
  .sc-hero-actions .sc-btn { min-height: 42px; }
  .sc-strategy-layout { gap: 10px; }
  .sc-strategy-sidebar { order: 2; }
  .sc-results-center { order: 1; }
  .sc-explain-panel { order: 3; }
  .sc-workbench-actions { grid-template-columns: repeat(2,minmax(0,1fr)); }
  .sc-selection-summary { grid-column: 1 / -1; min-height: 30px; }
  .sc-workbench-actions .primary { grid-column: span 2; }
  .sc-result-toolbar { flex-wrap: wrap; }
  .sc-result-search.open { width: 100%; }
  .sc-result-search input { min-width: 0; flex: 1; }
  .sc-factor-board,
  .sc-compare-board { grid-template-columns: 1fr; }
  .sc-config-grid { grid-template-columns: 1fr; }
  .sc-config-grid > section { border-right: 0; }
  .sc-rule-config-panel > header { align-items: flex-start; flex-direction: column; gap: 10px; }
}
</style>

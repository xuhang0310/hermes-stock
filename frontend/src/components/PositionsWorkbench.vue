<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { api } from '../api'
import DataTable from './DataTable.vue'
import type {
  BrokerPreset,
  PositionAccount,
  PositionItem,
  PositionOcrRecognizeResult,
  PositionOcrRow,
  PositionSummary,
  PositionSymbolValidation,
  PositionTrade
} from '../types'
import { beijingFromDb } from '../utils/datetime'

const props = defineProps<{ positions: PositionItem[] }>()
const emit = defineEmits<{
  refresh: []
  refreshPositions: []
  positionCorrected: [position: PositionItem]
  openAgent: [accountId: number | null]
  openChart: [position: PositionItem]
}>()

const accounts = ref<PositionAccount[]>([])
const brokers = ref<BrokerPreset[]>([])
const summary = ref<PositionSummary | null>(null)
const selectedAccountId = ref<number | null>(null)
const message = ref('')
const activeTab = ref<'positions' | 'trades'>('positions')
const trades = ref<PositionTrade[]>([])
const tradesLoading = ref(false)
const priceSyncing = ref(false)

const accountModalOpen = ref(false)
const fundModalOpen = ref(false)
const tradeModalOpen = ref(false)
const tradeSaving = ref(false)
const correctionModalOpen = ref(false)
const correctionSaving = ref(false)
const correctionPositionId = ref<number | null>(null)
const correctionValidation = ref<PositionSymbolValidation | null>(null)
const ocrModalOpen = ref(false)
const ocrRecognizing = ref(false)
const ocrSaving = ref(false)
const ocrDragging = ref(false)
const ocrFileName = ref('')
const ocrImageBase64 = ref('')
const ocrMimeType = ref('image/png')
const ocrAccountId = ref<number | null>(null)
const ocrAsOfDate = ref('')
const ocrMessage = ref('')
const ocrResult = ref<PositionOcrRecognizeResult | null>(null)
const ocrElapsedSeconds = ref(0)
const ocrProgress = ref(0)
const ocrStageIndex = ref(0)
let ocrProgressTimer: ReturnType<typeof setInterval> | null = null
const ocrStages = [
  { title: '正在预处理图片', detail: '检查清晰度、方向和可识别区域' },
  { title: '视觉模型正在读取', detail: '识别账户信息与持仓明细' },
  { title: '正在整理结构化数据', detail: '转换为统一的持仓 JSON 格式' },
  { title: '正在校验股票代码', detail: '核对市场后缀、名称和数值字段' }
]
const ocrWaitingTips = [
  '复杂截图需要逐行核对，稍等片刻会更准确。',
  '正在区分持仓数量、成本价和最新价。',
  '识别结果返回后可以直接修改，不会自动写入数据库。',
  '股票代码会经过系统数据源再次校验。'
]
type PositionOcrReviewRow = PositionOcrRow & {
  validation: PositionSymbolValidation
  validating: boolean
}
const ocrRows = ref<PositionOcrReviewRow[]>([])
const correctionOriginal = ref<{
  symbol: string
  name: string
  position_quantity: number
  cost_price: number
} | null>(null)
const editingAccountId = ref<number | null>(null)
const accountForm = reactive({ broker_code: 'manual', account_name: '', account_no_masked: '', is_default: false })
const fundForm = reactive({ account_id: 0, account_label: '', available_cash: null as number | null, holding_market_value: 0 })
const tradeForm = reactive({
  symbol: '',
  name: '',
  action: 'buy' as 'buy' | 'sell',
  mode: 'row' as 'row' | 'add',
  quantity: null as number | null,
  price: null as number | null,
  account_id: null as number | null,
  latest_price: 0,
  current_quantity: 0
})
const correctionForm = reactive({
  symbol: '',
  name: '',
  position_quantity: 0,
  cost_price: 0
})
const tradeTitle = computed(() => {
  const act = tradeForm.action === 'buy' ? '买入' : '卖出'
  const subj = tradeForm.name || tradeForm.symbol
  return subj ? `${act} · ${subj}` : act
})
const tradeEstimate = computed(() => {
  const qty = tradeForm.quantity || 0
  const p = tradeForm.price || tradeForm.latest_price || 0
  return qty * p
})

const selectedAccount = computed(() => accounts.value.find((a) => a.id === selectedAccountId.value) || null)
const searchQuery = ref('')
const filteredPositions = computed(() => {
  const base =
    selectedAccountId.value === null ? props.positions : props.positions.filter((p) => p.account_id === selectedAccountId.value)
  const needle = searchQuery.value.trim().toLowerCase()
  if (!needle) return base
  return base.filter((p) =>
    [p.symbol, p.name, p.broker_name, p.account_name].some((v) => String(v || '').toLowerCase().includes(needle))
  )
})
const searchEmpty = computed(() => Boolean(searchQuery.value.trim()) && displayedPositions.value.length === 0)
type PositionSortKey = 'market_value' | 'position_quantity' | 'floating_pnl' | 'floating_pnl_pct'
type PositionSortDirection = 'asc' | 'desc'

const positionSortKey = ref<PositionSortKey | null>(null)
const positionSortDirection = ref<PositionSortDirection>('desc')
const positionSortOptions: Array<{ key: PositionSortKey; label: string }> = [
  { key: 'market_value', label: '市值' },
  { key: 'position_quantity', label: '持仓' },
  { key: 'floating_pnl', label: '浮盈' },
  { key: 'floating_pnl_pct', label: '盈亏率' }
]

function positionSortValue(position: PositionItem, key: PositionSortKey) {
  const value = key === 'position_quantity'
    ? position.position_quantity ?? position.quantity
    : position[key]
  if (value === null || value === undefined || Number.isNaN(Number(value))) return null
  return Number(value)
}

function togglePositionSort(key: PositionSortKey) {
  if (positionSortKey.value === key) {
    positionSortDirection.value = positionSortDirection.value === 'desc' ? 'asc' : 'desc'
    return
  }
  positionSortKey.value = key
  positionSortDirection.value = 'desc'
}

const displayedPositions = computed(() => {
  const key = positionSortKey.value
  if (!key) return filteredPositions.value
  const direction = positionSortDirection.value === 'desc' ? -1 : 1
  return filteredPositions.value
    .map((position, originalIndex) => ({ position, originalIndex }))
    .sort((left, right) => {
      const leftValue = positionSortValue(left.position, key)
      const rightValue = positionSortValue(right.position, key)
      if (leftValue === null && rightValue === null) return left.originalIndex - right.originalIndex
      if (leftValue === null) return 1
      if (rightValue === null) return -1
      const result = (leftValue - rightValue) * direction
      return result || left.originalIndex - right.originalIndex
    })
    .map((item) => item.position)
})

const ocrJsonPreview = computed(() => {
  if (!ocrResult.value) return ''
  return JSON.stringify(
    {
      schema_version: ocrResult.value.schema_version,
      image_sha256: ocrResult.value.image_sha256,
      model: ocrResult.value.model,
      account: ocrResult.value.account,
      as_of_date: ocrAsOfDate.value || null,
      rows: ocrRows.value.map(({ validation: _validation, validating: _validating, ...row }) => row)
    },
    null,
    2
  )
})
const ocrCurrentStage = computed(() => ocrStages[ocrStageIndex.value])
const ocrWaitingTip = computed(() => ocrWaitingTips[Math.floor(ocrElapsedSeconds.value / 4) % ocrWaitingTips.length])
const ocrElapsedLabel = computed(() => {
  const minutes = Math.floor(ocrElapsedSeconds.value / 60)
  const seconds = ocrElapsedSeconds.value % 60
  return minutes ? `${minutes} 分 ${seconds} 秒` : `${seconds} 秒`
})

type PositionColumnKey =
  | 'broker_name'
  | 'symbol'
  | 'name'
  | 'position_quantity'
  | 'available_quantity'
  | 'cost_price'
  | 'latest_price'
  | 'market_value'
  | 'floating_pnl'
  | 'floating_pnl_pct'
  | 'stop_price'
  | 'status'

type PositionTableColumn = {
  key: PositionColumnKey
  label: string
}

const allPositionColumnDefaults: PositionColumnKey[] = [
  'broker_name',
  'symbol',
  'name',
  'position_quantity',
  'latest_price',
  'cost_price',
  'market_value',
  'floating_pnl',
  'floating_pnl_pct',
  'status'
]
const accountPositionColumnDefaults: PositionColumnKey[] = [
  'symbol',
  'name',
  'position_quantity',
  'available_quantity',
  'latest_price',
  'cost_price',
  'market_value',
  'floating_pnl',
  'floating_pnl_pct',
  'stop_price'
]
const positionColumnLabels: Record<PositionColumnKey, string> = {
  broker_name: '账户',
  symbol: '标的',
  name: '名称',
  position_quantity: '持仓',
  available_quantity: '可用',
  latest_price: '现价',
  cost_price: '成本',
  market_value: '市值',
  floating_pnl: '浮盈',
  floating_pnl_pct: '盈亏率',
  stop_price: '止损',
  status: '状态'
}
const positionColumnStorageKey = 'sixrabbits:positions:column-order:v1'

function normalizePositionColumnOrder(value: unknown, defaults: PositionColumnKey[]) {
  const allowed = new Set(defaults)
  const stored = Array.isArray(value)
    ? value.filter((item): item is PositionColumnKey => typeof item === 'string' && allowed.has(item as PositionColumnKey))
    : []
  return [...new Set(stored), ...defaults.filter((item) => !stored.includes(item))]
}

function loadPositionColumnOrders() {
  try {
    const stored = JSON.parse(window.localStorage.getItem(positionColumnStorageKey) || '{}') as {
      all?: PositionColumnKey[]
      account?: PositionColumnKey[]
    }
    return {
      all: normalizePositionColumnOrder(stored.all, allPositionColumnDefaults),
      account: normalizePositionColumnOrder(stored.account, accountPositionColumnDefaults)
    }
  } catch {
    return {
      all: [...allPositionColumnDefaults],
      account: [...accountPositionColumnDefaults]
    }
  }
}

const initialPositionColumnOrders = loadPositionColumnOrders()
const allPositionColumnOrder = ref<PositionColumnKey[]>(initialPositionColumnOrders.all)
const accountPositionColumnOrder = ref<PositionColumnKey[]>(initialPositionColumnOrders.account)

watch(
  [allPositionColumnOrder, accountPositionColumnOrder],
  () => {
    try {
      window.localStorage.setItem(
        positionColumnStorageKey,
        JSON.stringify({
          all: allPositionColumnOrder.value,
          account: accountPositionColumnOrder.value
        })
      )
    } catch {
      // Column order remains active in memory when browser storage is unavailable.
    }
  },
  { deep: true }
)

function fmtMoney(v: number | null | undefined) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return '—'
  return `¥${Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 2 })}`
}
function fmtNum(v: number | null | undefined, digits = 0) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return '-'
  return Number(v).toLocaleString('zh-CN', { maximumFractionDigits: digits })
}
function fmtPct(v: number | null | undefined) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return '-'
  return `${Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 2 })}%`
}
function pnlClass(v: number | null | undefined) {
  if (v === null || v === undefined) return ''
  return v > 0 ? 'pnl-up' : v < 0 ? 'pnl-down' : ''
}
function pnlSpan(v: number | null | undefined, text: string) {
  const cls = pnlClass(v)
  return cls ? `<span class="${cls}">${text}</span>` : text
}

const fundCards = computed(() => {
  const a = selectedAccount.value
  const s = summary.value
  const src = a
    ? {
        total_asset: a.total_asset,
        available_cash: a.available_cash,
        holding_market_value: a.holding_market_value,
        total_pnl: a.total_pnl,
        day_pnl: a.day_pnl,
        holding_count: a.holding_count
      }
    : s
  if (!src) return []
  return [
    { label: '总资产', value: fmtMoney(src.total_asset), cls: '' },
    { label: '可用资金', value: fmtMoney(src.available_cash), cls: '' },
    { label: '持仓市值', value: fmtMoney(src.holding_market_value), cls: '' },
    { label: '累计浮盈', value: fmtMoney(src.total_pnl), cls: pnlClass(src.total_pnl) },
    { label: '当日盈亏', value: fmtMoney(src.day_pnl), cls: pnlClass(src.day_pnl) },
    { label: '持仓数', value: fmtNum(src.holding_count), cls: '' }
  ]
})

const tableColumnDefinitions = computed<PositionTableColumn[]>(() => {
  const order = selectedAccountId.value === null
    ? allPositionColumnOrder.value
    : accountPositionColumnOrder.value
  return order.map((key) => ({ key, label: positionColumnLabels[key] }))
})

const tableColumns = computed(() => [
  ...tableColumnDefinitions.value.map((column) => column.label),
  '操作'
])

function positionColumnValue(position: PositionItem, key: PositionColumnKey) {
  if (key === 'broker_name') return position.broker_name || '—'
  if (key === 'symbol') return position.symbol
  if (key === 'name') return position.name || '—'
  if (key === 'position_quantity') return fmtNum(position.position_quantity ?? position.quantity)
  if (key === 'available_quantity') return fmtNum(position.available_quantity)
  if (key === 'latest_price') return fmtNum(position.latest_price, 2)
  if (key === 'cost_price') return fmtNum(position.cost_price, 2)
  if (key === 'market_value') return fmtNum(position.market_value, 2)
  if (key === 'floating_pnl') return pnlSpan(position.floating_pnl, fmtNum(position.floating_pnl, 2))
  if (key === 'floating_pnl_pct') return pnlSpan(position.floating_pnl_pct, fmtPct(position.floating_pnl_pct))
  if (key === 'stop_price') return fmtNum(position.stop_price, 2)
  return position.status
}

function reorderPositionColumns(fromIndex: number, toIndex: number) {
  const target = selectedAccountId.value === null
    ? allPositionColumnOrder
    : accountPositionColumnOrder
  if (fromIndex < 0 || toIndex < 0 || fromIndex >= target.value.length || toIndex >= target.value.length) {
    return
  }
  const next = [...target.value]
  const [moved] = next.splice(fromIndex, 1)
  next.splice(toIndex, 0, moved)
  target.value = next
}

const tableRows = computed(() =>
  displayedPositions.value.map((position) => {
    const actions = `<button class="wb-trade-btn chart" data-action="chart">K线</button><button class="wb-trade-btn edit" data-action="edit">编辑</button><button class="wb-trade-btn buy" data-action="buy">买入</button><button class="wb-trade-btn sell" data-action="sell">卖出</button><button class="wb-trade-btn delete" data-action="delete">删除</button><button class="wb-trade-btn xq" data-action="xq" title="进入雪球社区核对价格">雪球</button>`
    return [
      ...tableColumnDefinitions.value.map((column) => positionColumnValue(position, column.key)),
      actions
    ]
  })
)

function tradeActionText(action: PositionTrade['action']) {
  if (action === 'buy' || action === 'add') return '买入'
  if (action === 'sell' || action === 'reduce') return '卖出'
  return '平仓'
}
function tradeActionLabel(action: PositionTrade['action']) {
  if (action === 'buy' || action === 'add') return '<span class="trade-dir buy">买入</span>'
  if (action === 'sell' || action === 'reduce') return '<span class="trade-dir sell">卖出</span>'
  return '<span class="trade-dir close">平仓</span>'
}
const tradeColumns = ['时间', '标的', '名称', '方向', '数量', '价格', '金额', '来源', '操作']
const tradeRows = computed(() =>
  trades.value.map((t) => [
    beijingFromDb(t.created_at),
    t.symbol,
    t.name || '-',
    tradeActionLabel(t.action),
    fmtNum(t.quantity),
    fmtNum(t.price, 2),
    fmtNum(t.amount, 2),
    t.source_type || '-',
    '<button class="wb-trade-btn delete" data-action="delete-trade">删除</button>'
  ])
)

async function loadAccounts() {
  accounts.value = await api.positionAccounts()
  summary.value = await api.positionsSummary(selectedAccountId.value)
}
function selectAccount(id: number | null) {
  selectedAccountId.value = id
  void api.positionsSummary(id).then((s) => (summary.value = s))
  if (activeTab.value === 'trades') void loadTrades()
}

async function syncPositionPrices() {
  if (priceSyncing.value) return
  priceSyncing.value = true
  message.value = ''
  try {
    const result = await api.syncPositionPrices()
    message.value = result.message
    if (result.skipped) {
      // 已有同步在跑，结果几秒后落库，延迟刷新一次即可
      window.setTimeout(() => {
        emit('refreshPositions')
        void loadAccounts()
      }, 3000)
    } else {
      emit('refreshPositions')
      await loadAccounts()
    }
  } catch (error) {
    message.value = `价格更新失败：${error instanceof Error ? error.message : String(error)}`
  } finally {
    priceSyncing.value = false
  }
}

function resetPositionOcr() {
  stopPositionOcrProgress()
  ocrFileName.value = ''
  ocrImageBase64.value = ''
  ocrMimeType.value = 'image/png'
  ocrAsOfDate.value = ''
  ocrMessage.value = ''
  ocrResult.value = null
  ocrRows.value = []
  ocrDragging.value = false
}

function updatePositionOcrProgress() {
  const seconds = ocrElapsedSeconds.value
  if (seconds < 3) {
    ocrStageIndex.value = 0
    ocrProgress.value = Math.min(28, 10 + seconds * 6)
  } else if (seconds < 9) {
    ocrStageIndex.value = 1
    ocrProgress.value = Math.min(54, 28 + (seconds - 2) * 4)
  } else if (seconds < 16) {
    ocrStageIndex.value = 2
    ocrProgress.value = Math.min(74, 54 + (seconds - 8) * 3)
  } else {
    ocrStageIndex.value = 3
    ocrProgress.value = Math.min(92, 74 + Math.floor((seconds - 15) * 1.2))
  }
}

function startPositionOcrProgress() {
  stopPositionOcrProgress()
  ocrElapsedSeconds.value = 0
  ocrProgress.value = 10
  ocrStageIndex.value = 0
  ocrProgressTimer = setInterval(() => {
    ocrElapsedSeconds.value += 1
    updatePositionOcrProgress()
  }, 1000)
}

function stopPositionOcrProgress(completed = false) {
  if (ocrProgressTimer) {
    clearInterval(ocrProgressTimer)
    ocrProgressTimer = null
  }
  if (completed) ocrProgress.value = 100
}

function openPositionOcr() {
  resetPositionOcr()
  ocrAccountId.value =
    selectedAccountId.value
    ?? accounts.value.find((item) => item.is_default)?.id
    ?? accounts.value[0]?.id
    ?? null
  ocrModalOpen.value = true
}

function closePositionOcr() {
  if (ocrRecognizing.value || ocrSaving.value) return
  ocrModalOpen.value = false
}

function readPositionOcrFile(file: File) {
  return new Promise<string>((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result || ''))
    reader.onerror = () => reject(reader.error || new Error('图片读取失败'))
    reader.readAsDataURL(file)
  })
}

async function setPositionOcrFile(file: File) {
  ocrMessage.value = ''
  if (!file.type.startsWith('image/')) {
    ocrMessage.value = '请选择 PNG、JPG 或其他图片文件'
    return
  }
  if (file.size > 8 * 1024 * 1024) {
    ocrMessage.value = '图片不能超过 8MB'
    return
  }
  try {
    ocrFileName.value = file.name
    ocrMimeType.value = file.type || 'image/png'
    ocrImageBase64.value = await readPositionOcrFile(file)
    ocrResult.value = null
    ocrRows.value = []
  } catch (error) {
    ocrMessage.value = error instanceof Error ? error.message : '图片读取失败'
  }
}

async function onPositionOcrFileSelected(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (file) await setPositionOcrFile(file)
  input.value = ''
}

async function onPositionOcrDrop(event: DragEvent) {
  ocrDragging.value = false
  const file = Array.from(event.dataTransfer?.files || []).find((item) => item.type.startsWith('image/'))
  if (!file) {
    ocrMessage.value = '没有检测到图片文件'
    return
  }
  await setPositionOcrFile(file)
}

function positionOcrValidationAt(result: PositionOcrRecognizeResult, index: number, row: PositionOcrRow) {
  return result.validations.find((item) => Number(item.index) === index) || {
    index,
    valid: false,
    status: 'unchecked',
    input_symbol: row.symbol,
    normalized_symbol: row.symbol,
    message: '等待校验'
  }
}

function positionOcrValidationLabel(row: PositionOcrReviewRow) {
  if (row.validating) return '校验中'
  if (row.validation.status === 'symbol_corrected_from_name_conflict') return '已纠正'
  return row.validation.valid ? '已通过' : '需核对'
}

async function recognizePositionOcr() {
  if (!ocrImageBase64.value || ocrRecognizing.value) {
    if (!ocrImageBase64.value) ocrMessage.value = '请先选择一张券商持仓截图'
    return
  }
  ocrRecognizing.value = true
  startPositionOcrProgress()
  ocrMessage.value = ''
  ocrResult.value = null
  ocrRows.value = []
  try {
    const result = await api.recognizePositionOcr({
      image_base64: ocrImageBase64.value,
      mime_type: ocrMimeType.value
    })
    ocrResult.value = result
    ocrAsOfDate.value = result.as_of_date || ''
    ocrRows.value = result.rows.map((row, index) => ({
      ...row,
      action: 'replace',
      validation: positionOcrValidationAt(result, index, row),
      validating: false
    }))
    stopPositionOcrProgress(true)
    const correctedCount = result.validations.filter(
      (item) => item.status === 'symbol_corrected_from_name_conflict'
    ).length
    ocrMessage.value = result.invalid_count
      ? `识别到 ${result.rows.length} 条持仓，其中 ${result.invalid_count} 条代码需要修正。`
      : correctedCount
        ? `识别到 ${result.rows.length} 条持仓，其中 ${correctedCount} 条猜测代码已按截图名称自动纠正，请重点核对。`
        : `识别到 ${result.rows.length} 条持仓，股票代码已全部通过校验。`
  } catch (error) {
    stopPositionOcrProgress()
    ocrMessage.value = `识别失败：${error instanceof Error ? error.message : String(error)}`
  } finally {
    stopPositionOcrProgress(Boolean(ocrResult.value))
    ocrRecognizing.value = false
  }
}

function markPositionOcrSymbolEdited(row: PositionOcrReviewRow) {
  row.validation = {
    valid: false,
    status: 'unchecked',
    input_symbol: row.symbol,
    normalized_symbol: row.symbol,
    message: '代码已修改，请重新校验'
  }
}

async function validatePositionOcrRow(index: number) {
  const row = ocrRows.value[index]
  if (!row || row.validating) return false
  row.validating = true
  try {
    const validation = await api.validatePositionAgentSymbol({
      symbol: row.symbol.trim(),
      name: row.name.trim() || null
    })
    row.validation = validation
    if (validation.valid) {
      row.symbol = validation.normalized_symbol
      row.name = validation.canonical_name || row.name
    }
    return validation.valid
  } catch (error) {
    row.validation = {
      valid: false,
      status: 'validation_error',
      input_symbol: row.symbol,
      normalized_symbol: row.symbol,
      message: error instanceof Error ? error.message : '股票代码校验失败'
    }
    return false
  } finally {
    row.validating = false
  }
}

function removePositionOcrRow(index: number) {
  ocrRows.value.splice(index, 1)
}

async function confirmPositionOcr() {
  if (!ocrResult.value || ocrSaving.value) return
  if (!ocrAccountId.value) {
    ocrMessage.value = '请选择要写入的证券账户'
    return
  }
  if (!ocrRows.value.length) {
    ocrMessage.value = '没有可写入的持仓明细'
    return
  }
  ocrSaving.value = true
  ocrMessage.value = ''
  try {
    const checks = await Promise.all(ocrRows.value.map((_, index) => validatePositionOcrRow(index)))
    if (checks.some((valid) => !valid)) {
      ocrMessage.value = '仍有股票代码未通过校验，请修正后再确认入库'
      return
    }
    const invalidNumber = ocrRows.value.find(
      (row) =>
        !Number.isFinite(Number(row.position_quantity))
        || Number(row.position_quantity) < 0
        || (row.cost_price !== null && row.cost_price !== undefined && Number(row.cost_price) < 0)
    )
    if (invalidNumber) {
      ocrMessage.value = '持仓数量和成本价不能为负数'
      return
    }
    const rows = ocrRows.value.map(({ validation: _validation, validating: _validating, ...row }) => ({
      ...row,
      position_quantity: Number(row.position_quantity),
      action: 'replace' as const
    }))
    const result = await api.confirmPositionOcr({
      rows,
      account_id: ocrAccountId.value,
      as_of_date: ocrAsOfDate.value || null,
      image_sha256: ocrResult.value.image_sha256,
      mime_type: ocrMimeType.value
    })
    const failed = result.rows.filter((row) => 'error' in row).length
    message.value = failed
      ? `${result.summary} 另有 ${failed} 条保存失败。`
      : result.summary
    ocrModalOpen.value = false
    await loadAccounts()
    emit('refreshPositions')
  } catch (error) {
    ocrMessage.value = `入库失败：${error instanceof Error ? error.message : String(error)}`
  } finally {
    ocrSaving.value = false
  }
}

function openNewAccount() {
  editingAccountId.value = null
  Object.assign(accountForm, { broker_code: brokers.value[0]?.code || 'manual', account_name: '', account_no_masked: '', is_default: false })
  message.value = ''
  accountModalOpen.value = true
}
function editAccount(a: PositionAccount) {
  editingAccountId.value = a.id
  Object.assign(accountForm, {
    broker_code: a.broker_code || 'manual',
    account_name: a.account_name || '',
    account_no_masked: a.account_no_masked || '',
    is_default: a.is_default
  })
  message.value = ''
  accountModalOpen.value = true
}
async function saveAccount() {
  message.value = ''
  try {
    if (editingAccountId.value) {
      await api.updatePositionAccount(editingAccountId.value, {
        account_name: accountForm.account_name || null,
        account_no_masked: accountForm.account_no_masked || null,
        is_default: accountForm.is_default
      })
    } else {
      const broker = brokers.value.find((b) => b.code === accountForm.broker_code)
      await api.createPositionAccount({
        broker_code: accountForm.broker_code,
        broker_name: broker?.name,
        account_name: accountForm.account_name || null,
        account_no_masked: accountForm.account_no_masked || null,
        is_default: accountForm.is_default
      })
    }
    accountModalOpen.value = false
    await loadAccounts()
  } catch (error) {
    message.value = `保存失败：${error instanceof Error ? error.message : String(error)}`
  }
}
async function archiveAccount(a: PositionAccount) {
  await api.updatePositionAccount(a.id, { status: 'archived' })
  if (selectedAccountId.value === a.id) selectedAccountId.value = null
  await loadAccounts()
}
async function setDefault(a: PositionAccount) {
  await api.updatePositionAccount(a.id, { is_default: true })
  await loadAccounts()
}

function openFund(a: PositionAccount) {
  fundForm.account_id = a.id
  fundForm.account_label = a.broker_name + (a.account_name ? ` · ${a.account_name}` : '')
  fundForm.available_cash = a.available_cash
  fundForm.holding_market_value = a.holding_market_value || 0
  message.value = ''
  fundModalOpen.value = true
}
// 预览总资产 = 可用现金 + 持仓市值（可用现金是用户维护的输入，总资产系统算）
const previewTotal = computed(() =>
  fundForm.available_cash === null ? null : Number(fundForm.available_cash) + fundForm.holding_market_value
)
async function saveFund() {
  message.value = ''
  try {
    await api.updateAccountFunds(fundForm.account_id, fundForm.available_cash)
    fundModalOpen.value = false
    await loadAccounts()
    emit('refreshPositions')
  } catch (error) {
    message.value = `保存失败：${error instanceof Error ? error.message : String(error)}`
  }
}

function onCellClick(rowIndex: number, cellIndex: number, target: HTMLElement) {
  const actionCol = tableColumns.value.length - 1
  if (cellIndex !== actionCol) return
  const pos = displayedPositions.value[rowIndex]
  if (!pos) return
  const action = target.getAttribute('data-action')
  if (action === 'chart') {
    emit('openChart', pos)
  } else if (action === 'edit') {
    openCorrectionModal(pos)
  } else if (action === 'buy') {
    openTradeModal(pos, 'buy')
  } else if (action === 'sell') {
    openTradeModal(pos, 'sell')
  } else if (action === 'delete') {
    void confirmDelete(pos)
  } else if (action === 'xq') {
    const [code, exch] = pos.symbol.split('.')
    window.open(`https://xueqiu.com/S/${exch}${code}`, '_blank', 'noopener')
  }
}

function openCorrectionModal(pos: PositionItem) {
  correctionPositionId.value = pos.id
  const original = {
    symbol: pos.symbol,
    name: pos.name || '',
    position_quantity: Number(pos.position_quantity ?? pos.quantity ?? 0),
    cost_price: Number(pos.cost_price ?? 0)
  }
  Object.assign(correctionForm, original)
  correctionOriginal.value = original
  correctionValidation.value = null
  message.value = ''
  correctionModalOpen.value = true
}

function markCorrectionSymbolEdited() {
  correctionValidation.value = null
}

async function validateCorrectionSymbol(applyCanonicalName = true) {
  if (!correctionForm.symbol.trim()) {
    message.value = '请输入股票代码'
    return false
  }
  message.value = ''
  try {
    const validation = await api.validatePositionAgentSymbol({
      symbol: correctionForm.symbol.trim(),
      name: correctionForm.name.trim() || null
    })
    correctionValidation.value = validation
    if (validation.valid) {
      correctionForm.symbol = validation.normalized_symbol
      if (applyCanonicalName && validation.canonical_name) {
        correctionForm.name = validation.canonical_name
      }
    }
    return validation.valid
  } catch (error) {
    message.value = `校验失败：${error instanceof Error ? error.message : String(error)}`
    return false
  }
}

async function savePositionCorrection() {
  if (!correctionPositionId.value) return
  if (!correctionForm.name.trim()) {
    message.value = '请输入股票名称'
    return
  }
  if (!Number.isFinite(correctionForm.position_quantity) || correctionForm.position_quantity < 0) {
    message.value = '持仓数量必须大于等于 0'
    return
  }
  if (!Number.isFinite(correctionForm.cost_price) || correctionForm.cost_price < 0) {
    message.value = '成本价必须大于等于 0'
    return
  }
  correctionSaving.value = true
  message.value = ''
  try {
    const validation = await api.validatePositionAgentSymbol({
      symbol: correctionForm.symbol.trim(),
      name: correctionForm.name.trim()
    })
    correctionValidation.value = validation
    const sameExistingSymbol =
      validation.normalized_symbol === correctionOriginal.value?.symbol &&
      validation.status === 'catalog_unavailable'
    if (!validation.valid && !sameExistingSymbol) {
      message.value = validation.message || '股票代码校验失败'
      return
    }

    const normalizedSymbol = validation.normalized_symbol || correctionForm.symbol.trim().toUpperCase()
    correctionForm.symbol = normalizedSymbol
    const original = correctionOriginal.value
    const unchanged =
      original &&
      normalizedSymbol === original.symbol &&
      correctionForm.name.trim() === original.name &&
      Number(correctionForm.position_quantity) === original.position_quantity &&
      Number(correctionForm.cost_price) === original.cost_price
    if (unchanged) {
      message.value = '没有检测到修改，请先更改需要纠正的内容'
      return
    }

    const corrected = await api.correctPosition(correctionPositionId.value, {
      symbol: normalizedSymbol,
      name: correctionForm.name.trim(),
      position_quantity: correctionForm.position_quantity,
      cost_price: correctionForm.cost_price
    })
    emit('positionCorrected', corrected)
    correctionModalOpen.value = false
    await loadAccounts()
    emit('refreshPositions')
  } catch (error) {
    message.value = `纠正失败：${error instanceof Error ? error.message : String(error)}`
  } finally {
    correctionSaving.value = false
  }
}

async function confirmDelete(pos: PositionItem) {
  const label = `${pos.name || pos.symbol}（${pos.symbol}）`
  if (!window.confirm(`确定物理删除 ${label} 的持仓记录及全部历史快照吗？\n此操作不可逆，卖出请用"卖出"按钮。`)) {
    return
  }
  message.value = ''
  try {
    await api.deletePosition(pos.symbol, pos.account_id)
    await loadAccounts()
    emit('refreshPositions')
  } catch (error) {
    message.value = `删除失败：${error instanceof Error ? error.message : String(error)}`
  }
}

async function loadTrades() {
  tradesLoading.value = true
  try {
    trades.value = await api.positionTrades({ account_id: selectedAccountId.value, limit: 200 })
  } catch {
    trades.value = []
  } finally {
    tradesLoading.value = false
  }
}
function switchTab(tab: 'positions' | 'trades') {
  activeTab.value = tab
  if (tab === 'trades') void loadTrades()
}
async function confirmDeleteTrade(trade: PositionTrade) {
  if (!window.confirm(`确定删除该笔交易流水吗？\n${trade.symbol} ${tradeActionText(trade.action)} ${fmtNum(trade.quantity)}股\n（仅删流水，不影响持仓）`)) {
    return
  }
  message.value = ''
  try {
    await api.deletePositionTrade(trade.id)
    await loadTrades()
    emit('refreshPositions')
  } catch (error) {
    message.value = `删除失败：${error instanceof Error ? error.message : String(error)}`
  }
}
function onTradeCellClick(rowIndex: number, cellIndex: number, target: HTMLElement) {
  const actionCol = tradeColumns.length - 1
  if (cellIndex !== actionCol) return
  const action = target.getAttribute('data-action')
  if (action === 'delete-trade') {
    const trade = trades.value[rowIndex]
    if (trade) void confirmDeleteTrade(trade)
  }
}

function openTradeModal(pos: PositionItem | null, action: 'buy' | 'sell', mode: 'row' | 'add' = 'row') {
  tradeForm.mode = mode
  tradeForm.action = action
  tradeForm.quantity = null
  tradeForm.account_id = selectedAccountId.value
  message.value = ''
  if (mode === 'add') {
    // 总买入入口：symbol/name 留空由用户填，价格不强预填
    tradeForm.symbol = ''
    tradeForm.name = ''
    tradeForm.price = null
    tradeForm.latest_price = 0
    tradeForm.current_quantity = 0
  } else {
    tradeForm.symbol = pos!.symbol
    tradeForm.name = pos!.name || pos!.symbol
    tradeForm.price = pos!.latest_price ?? pos!.cost_price ?? null
    tradeForm.latest_price = pos!.latest_price ?? 0
    tradeForm.current_quantity = pos!.position_quantity ?? pos!.quantity ?? 0
  }
  tradeModalOpen.value = true
}

async function submitTrade() {
  if (tradeForm.mode === 'add' && !tradeForm.symbol.trim()) {
    message.value = '请输入股票代码'
    return
  }
  if (!tradeForm.quantity || tradeForm.quantity <= 0) {
    message.value = '请输入数量'
    return
  }
  // add 模式（总买入入口）价格必填；行内加仓价格可选（不填走最新价兜底）
  if (tradeForm.mode === 'add' && (!tradeForm.price || tradeForm.price <= 0)) {
    message.value = '请输入买入价格'
    return
  }
  if (!tradeForm.account_id) {
    message.value = '请选择证券账户'
    return
  }
  tradeSaving.value = true
  message.value = ''
  try {
    await api.addPosition({
      symbol: tradeForm.symbol.trim().toUpperCase(),
      name: tradeForm.name || undefined,
      position_quantity: tradeForm.quantity,
      cost_price: tradeForm.price || undefined,
      action: tradeForm.action,
      account_id: tradeForm.account_id
    })
    tradeModalOpen.value = false
    await loadAccounts()
    emit('refreshPositions')
    if (activeTab.value === 'trades') await loadTrades()
  } catch (error) {
    message.value = `操作失败：${error instanceof Error ? error.message : String(error)}`
  } finally {
    tradeSaving.value = false
  }
}

onMounted(async () => {
  brokers.value = await api.positionBrokers()
  await loadAccounts()
  await loadTrades()
})

onBeforeUnmount(() => {
  stopPositionOcrProgress()
})
</script>

<template>
  <section class="positions-workbench">
    <div class="account-tabs">
      <button type="button" :class="{ active: selectedAccountId === null }" @click="selectAccount(null)">
        <span class="acct-name">全部账户</span>
        <span class="acct-sub">{{ summary ? fmtMoney(summary.total_asset) : '' }}</span>
      </button>
      <button
        v-for="a in accounts"
        :key="a.id"
        type="button"
        :class="{ active: selectedAccountId === a.id }"
        @click="selectAccount(a.id)"
      >
        <span class="acct-name">{{ a.broker_name }}<em v-if="a.is_default">默认</em></span>
        <span class="acct-sub">{{ fmtMoney(a.total_asset) }}<template v-if="a.account_no_masked"> · {{ a.account_no_masked }}</template></span>
      </button>
      <button type="button" class="acct-add" @click="openNewAccount">＋ 添加账户</button>
    </div>

    <div class="fund-grid">
      <div v-for="card in fundCards" :key="card.label" class="fund-card">
        <span class="fund-label">{{ card.label }}</span>
        <strong :class="card.cls">{{ card.value }}</strong>
      </div>
    </div>

    <section class="page-section">
      <header class="section-header">
        <div>
          <h2>{{ selectedAccount ? selectedAccount.broker_name + ' 持仓' : '全部持仓' }}</h2>
          <p>点击操作列”K线”查看 K 线图表；录入有误时可使用”编辑”纠正。</p>
        </div>
        <div class="wb-actions">
          <button v-if="selectedAccount" type="button" @click="openFund(selectedAccount)">维护资金</button>
          <button v-if="selectedAccount" type="button" @click="editAccount(selectedAccount)">账户设置</button>
          <button
            type="button"
            :disabled="priceSyncing"
            title="拉取实时行情，刷新全部持仓的最新价/市值/浮盈并重算账户资金"
            @click="syncPositionPrices"
          >
            {{ priceSyncing ? '更新中…' : '更新价格' }}
          </button>
          <button type="button" class="primary" @click="openTradeModal(null, 'buy', 'add')">买入</button>
          <button type="button" class="ocr-action" @click="openPositionOcr">OCR 识别</button>
          <button type="button" @click="emit('openAgent', selectedAccountId)">Agent 录入</button>
        </div>
      </header>
      <p v-if="message && !accountModalOpen && !fundModalOpen && !tradeModalOpen && !correctionModalOpen" class="form-message positions-message">
        {{ message }}
      </p>
      <div class="wb-tabs">
        <button type="button" :class="{ active: activeTab === 'positions' }" @click="switchTab('positions')">持仓</button>
        <button type="button" :class="{ active: activeTab === 'trades' }" @click="switchTab('trades')">交易记录</button>
      </div>
      <div v-if="activeTab === 'positions'" class="position-sort-toolbar" aria-label="持仓排序与搜索">
        <span>排序</span>
        <button
          v-for="option in positionSortOptions"
          :key="option.key"
          type="button"
          :class="{ active: positionSortKey === option.key }"
          :aria-pressed="positionSortKey === option.key"
          :title="`按${option.label}${positionSortKey === option.key && positionSortDirection === 'asc' ? '升序' : '降序'}排列`"
          @click="togglePositionSort(option.key)"
        >
          {{ option.label }}
          <span v-if="positionSortKey === option.key" aria-hidden="true">
            {{ positionSortDirection === 'desc' ? '↓' : '↑' }}
          </span>
        </button>
        <input
          v-model.trim="searchQuery"
          type="search"
          class="position-search"
          placeholder="搜索代码 / 名称 / 账户"
          aria-label="搜索持仓"
        />
        <span v-if="searchEmpty" class="position-search-empty">无匹配持仓</span>
      </div>
      <DataTable
        v-if="activeTab === 'positions'"
        :columns="tableColumns"
        :rows="tableRows"
        :fixed-column-indexes="[tableColumns.length - 1]"
        reorderable
        @column-reorder="reorderPositionColumns"
        @cell-click="onCellClick"
      />
      <div v-else-if="tradesLoading" class="trade-empty">加载中…</div>
      <DataTable v-else :columns="tradeColumns" :rows="tradeRows" @cell-click="onTradeCellClick" />
    </section>

    <div v-if="ocrModalOpen" class="wb-modal-backdrop" @click.self="closePositionOcr">
      <div class="wb-modal position-ocr-modal">
        <header class="position-ocr-modal-header">
          <div>
            <span>POSITION OCR</span>
            <h3>券商持仓截图识别</h3>
            <p>图片只用于本次识别。确认前不会写入任何持仓数据。</p>
          </div>
          <button type="button" :disabled="ocrRecognizing || ocrSaving" aria-label="关闭" @click="closePositionOcr">×</button>
        </header>

        <div class="position-ocr-controls">
          <label>
            <span>写入账户</span>
            <select v-model.number="ocrAccountId" :disabled="ocrSaving">
              <option :value="null" disabled>请选择证券账户</option>
              <option v-for="account in accounts" :key="account.id" :value="account.id">
                {{ account.broker_name }}{{ account.account_name ? ' · ' + account.account_name : '' }}{{ account.account_no_masked ? ' · ' + account.account_no_masked : '' }}
              </option>
            </select>
          </label>
          <label>
            <span>持仓日期</span>
            <input v-model="ocrAsOfDate" type="date" :disabled="ocrSaving" />
          </label>
          <div v-if="ocrResult" class="position-ocr-model">
            <span>识别模型</span>
            <strong>{{ ocrResult.model.display_name || ocrResult.model.model_name || '视觉模型' }}</strong>
          </div>
        </div>

        <div
          class="position-ocr-dropzone"
          :class="{ dragging: ocrDragging, ready: Boolean(ocrImageBase64) }"
          @dragenter.prevent="ocrDragging = true"
          @dragover.prevent="ocrDragging = true"
          @dragleave.prevent="ocrDragging = false"
          @drop.prevent="onPositionOcrDrop"
        >
          <img v-if="ocrImageBase64" :src="ocrImageBase64" alt="待识别持仓截图" />
          <div>
            <strong>{{ ocrFileName || '拖入券商持仓截图' }}</strong>
            <span>{{ ocrImageBase64 ? '可重新选择图片，或直接开始识别' : '支持 PNG、JPG，单张不超过 8MB' }}</span>
            <label class="position-ocr-file-button">
              <input type="file" accept="image/*" :disabled="ocrRecognizing || ocrSaving" @change="onPositionOcrFileSelected" />
              <span>{{ ocrImageBase64 ? '重新选择' : '选择图片' }}</span>
            </label>
          </div>
          <button type="button" class="primary" :disabled="!ocrImageBase64 || ocrRecognizing || ocrSaving" @click="recognizePositionOcr">
            {{ ocrRecognizing ? 'AI 识别中' : '开始识别' }}
          </button>
        </div>

        <section v-if="ocrRecognizing" class="position-ocr-progress" aria-live="polite" aria-busy="true">
          <header>
            <span class="position-ocr-spinner" aria-hidden="true"></span>
            <div>
              <strong>{{ ocrCurrentStage.title }}</strong>
              <p>{{ ocrCurrentStage.detail }}</p>
            </div>
            <time>已等待 {{ ocrElapsedLabel }}</time>
          </header>
          <div class="position-ocr-progress-track" role="progressbar" :aria-valuenow="ocrProgress" aria-valuemin="0" aria-valuemax="100">
            <span :style="{ width: `${ocrProgress}%` }"></span>
          </div>
          <ol>
            <li
              v-for="(stage, index) in ocrStages"
              :key="stage.title"
              :class="{ active: index === ocrStageIndex, complete: index < ocrStageIndex }"
            >
              <span>{{ index < ocrStageIndex ? '✓' : index + 1 }}</span>
              {{ stage.title.replace('正在', '') }}
            </li>
          </ol>
          <p class="position-ocr-waiting-tip">{{ ocrWaitingTip }}</p>
        </section>

        <p v-if="ocrMessage" class="form-message position-ocr-message">{{ ocrMessage }}</p>

        <section v-if="ocrRows.length" class="position-ocr-result">
          <header>
            <div>
              <h4>识别结果</h4>
              <p>核对后可直接修改；确认入库时后端会再次校验股票代码。</p>
            </div>
            <strong>
              {{ ocrRows.filter((row) => row.validation.valid).length }}/{{ ocrRows.length }} 已校验
            </strong>
          </header>
          <div class="position-ocr-table-wrap">
            <table>
              <thead>
                <tr>
                  <th>#</th>
                  <th>股票代码</th>
                  <th>名称</th>
                  <th>持仓数量</th>
                  <th>成本价</th>
                  <th>现价</th>
                  <th>校验状态</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(row, index) in ocrRows" :key="`${row.symbol}-${index}`">
                  <td>{{ index + 1 }}</td>
                  <td>
                    <input
                      v-model.trim="row.symbol"
                      maxlength="16"
                      :disabled="ocrSaving"
                      @input="markPositionOcrSymbolEdited(row)"
                      @blur="validatePositionOcrRow(index)"
                    />
                  </td>
                  <td><input v-model.trim="row.name" maxlength="120" :disabled="ocrSaving" /></td>
                  <td><input v-model.number="row.position_quantity" type="number" min="0" step="1" :disabled="ocrSaving" /></td>
                  <td><input v-model.number="row.cost_price" type="number" min="0" step="0.0001" :disabled="ocrSaving" /></td>
                  <td><input v-model.number="row.latest_price" type="number" min="0" step="0.0001" :disabled="ocrSaving" /></td>
                  <td>
                    <span
                      class="position-ocr-validation-badge"
                      :class="{
                        valid: row.validation.valid,
                        corrected: row.validation.status === 'symbol_corrected_from_name_conflict'
                      }"
                    >
                      {{ positionOcrValidationLabel(row) }}
                    </span>
                    <small>{{ row.validation.message }}</small>
                  </td>
                  <td>
                    <button type="button" class="danger-text" :disabled="ocrSaving" @click="removePositionOcrRow(index)">移除</button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <details class="position-ocr-json">
            <summary>查看接口 JSON</summary>
            <pre>{{ ocrJsonPreview }}</pre>
          </details>
        </section>

        <footer class="wb-modal-actions position-ocr-footer">
          <span class="wb-spacer"></span>
          <button type="button" :disabled="ocrRecognizing || ocrSaving" @click="closePositionOcr">取消</button>
          <button
            type="button"
            class="primary"
            :disabled="!ocrRows.length || ocrRecognizing || ocrSaving"
            @click="confirmPositionOcr"
          >
            {{ ocrSaving ? '正在入库' : `确认并录入 ${ocrRows.length || ''} 条` }}
          </button>
        </footer>
      </div>
    </div>

    <div v-if="accountModalOpen" class="wb-modal-backdrop" @click.self="accountModalOpen = false">
      <div class="wb-modal">
        <h3>{{ editingAccountId ? '账户设置' : '添加券商账户' }}</h3>
        <label v-if="!editingAccountId">
          <span>券商</span>
          <select v-model="accountForm.broker_code">
            <option v-for="b in brokers" :key="b.code" :value="b.code">{{ b.name }}</option>
          </select>
        </label>
        <label>
          <span>账户别名（可选）</span>
          <input v-model="accountForm.account_name" placeholder="如 主账户 / 两融" />
        </label>
        <label>
          <span>脱敏账号（可选）</span>
          <input v-model="accountForm.account_no_masked" placeholder="如 ****1234" />
        </label>
        <label class="wb-checkbox">
          <input v-model="accountForm.is_default" type="checkbox" />
          <span>设为默认账户</span>
        </label>
        <p v-if="message" class="form-message">{{ message }}</p>
        <div class="wb-modal-actions">
          <button
            v-if="editingAccountId && accounts.length > 1"
            type="button"
            class="danger"
            @click="editingAccountId && archiveAccount(accounts.find((x) => x.id === editingAccountId)!)"
          >
            删除账户
          </button>
          <button
            v-if="editingAccountId && !accounts.find((x) => x.id === editingAccountId)?.is_default"
            type="button"
            @click="editingAccountId && setDefault(accounts.find((x) => x.id === editingAccountId)!)"
          >
            设为默认
          </button>
          <span class="wb-spacer"></span>
          <button type="button" @click="accountModalOpen = false">取消</button>
          <button type="button" class="primary" @click="saveAccount">保存</button>
        </div>
      </div>
    </div>

    <div v-if="correctionModalOpen" class="wb-modal-backdrop" @click.self="!correctionSaving && (correctionModalOpen = false)">
      <div class="wb-modal position-correction-modal">
        <h3>纠正持仓信息</h3>
        <p class="position-correction-hint">修改会同步当前持仓、历史快照身份和关联交易流水，并重新计算账户汇总。</p>
        <label>
          <span>股票代码 <em class="required">*</em></span>
          <div class="position-correction-symbol">
            <input v-model.trim="correctionForm.symbol" maxlength="16" placeholder="如 600519.SH" @input="markCorrectionSymbolEdited" @keyup.enter.prevent="validateCorrectionSymbol(true)" />
            <button type="button" :disabled="correctionSaving" @click="validateCorrectionSymbol(true)">校验代码</button>
          </div>
        </label>
        <div v-if="correctionValidation" class="position-correction-validation" :class="{ valid: correctionValidation.valid }">
          <strong>{{ correctionValidation.valid ? '代码校验通过' : '代码需要核对' }}</strong>
          <span>{{ correctionValidation.message }}</span>
        </div>
        <label>
          <span>股票名称 <em class="required">*</em></span>
          <input v-model.trim="correctionForm.name" maxlength="120" placeholder="校验代码后自动匹配证券名称" />
        </label>
        <label>
          <span>持仓数量 <em class="required">*</em></span>
          <input v-model.number="correctionForm.position_quantity" type="number" min="0" step="1" />
        </label>
        <label>
          <span>成本价 <em class="required">*</em></span>
          <input v-model.number="correctionForm.cost_price" type="number" min="0" step="0.0001" />
        </label>
        <p v-if="message" class="form-message">{{ message }}</p>
        <div class="wb-modal-actions">
          <span class="wb-spacer"></span>
          <button type="button" :disabled="correctionSaving" @click="correctionModalOpen = false">取消</button>
          <button type="button" class="primary" :disabled="correctionSaving" @click="savePositionCorrection">
            {{ correctionSaving ? '正在保存' : '确认纠正' }}
          </button>
        </div>
      </div>
    </div>

    <div v-if="fundModalOpen" class="wb-modal-backdrop" @click.self="fundModalOpen = false">
      <div class="wb-modal">
        <h3>维护资金 · {{ fundForm.account_label }}</h3>
        <label>
          <span>可用现金（券商 App 现金数）</span>
          <input v-model.number="fundForm.available_cash" type="number" min="0" step="0.01" placeholder="如 100000" />
        </label>
        <div class="fund-preview">
          <span>当前持仓市值 {{ fmtMoney(fundForm.holding_market_value) }}</span>
          <span>总资产（自动推算 = 可用现金 + 持仓市值）<strong>{{ fmtMoney(previewTotal) }}</strong></span>
        </div>
        <p class="fund-hint">维护"可用现金"后，总资产随持仓市值自动联动；累计浮盈/当日盈亏由持仓自动汇总。</p>
        <p v-if="message" class="form-message">{{ message }}</p>
        <div class="wb-modal-actions">
          <span class="wb-spacer"></span>
          <button type="button" @click="fundModalOpen = false">取消</button>
          <button type="button" class="primary" @click="saveFund">保存资金</button>
        </div>
      </div>
    </div>

    <!-- 买入/卖出弹窗 -->
    <div v-if="tradeModalOpen" class="wb-modal-backdrop" @click.self="tradeModalOpen = false">
      <div class="wb-modal">
        <h3>{{ tradeTitle }}</h3>
        <label v-if="tradeForm.mode === 'add'">
          <span>股票代码 <em class="required">*</em></span>
          <input v-model="tradeForm.symbol" placeholder="如 600519.SH 或 600519" />
        </label>
        <label v-if="tradeForm.mode === 'add'">
          <span>股票名称（选填）</span>
          <input v-model="tradeForm.name" placeholder="如 贵州茅台" />
        </label>
        <label>
          <span>证券账户 <em class="required">*</em></span>
          <select v-model.number="tradeForm.account_id">
            <option :value="null" disabled>— 请选择 —</option>
            <option v-for="a in accounts" :key="a.id" :value="a.id">
              {{ a.broker_name }}{{ a.account_name ? ' · ' + a.account_name : '' }}
            </option>
          </select>
        </label>
        <label>
          <span>数量 <em class="required">*</em></span>
          <input v-model.number="tradeForm.quantity" type="number" min="0" step="1" placeholder="股数" />
        </label>
        <label>
          <span>{{ tradeForm.mode === 'add' ? '买入价格 *' : '价格（选填，不填用最新价）' }}</span>
          <input v-model.number="tradeForm.price" type="number" min="0" step="0.01" placeholder="元/股" />
        </label>
        <div v-if="tradeForm.action === 'sell'" class="trade-hint">
          当前持仓 {{ fmtNum(tradeForm.current_quantity) }} 股，
          填写的数量将从现有持仓中扣减；全部卖出后自动关闭持仓。
        </div>
        <div class="trade-estimate">
          <span>{{ tradeForm.action === 'buy' ? '预计花费' : '预计收回' }}</span>
          <strong :class="pnlClass(tradeEstimate)">{{ fmtMoney(tradeEstimate) }}</strong>
        </div>
        <p v-if="message" class="form-message">{{ message }}</p>
        <div class="wb-modal-actions">
          <span class="wb-spacer"></span>
          <button type="button" :disabled="tradeSaving" @click="tradeModalOpen = false">取消</button>
          <button type="button" class="primary" :disabled="tradeSaving" @click="submitTrade">
            {{ tradeSaving ? '提交中' : '确认' + (tradeForm.action === 'buy' ? '买入' : '卖出') }}
          </button>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.positions-message {
  margin: 0 0 10px;
}
.wb-actions .ocr-action {
  border-color: rgba(37, 211, 180, 0.42);
  color: #7fe3cf;
}
.position-ocr-modal {
  width: min(1120px, calc(100vw - 32px));
  max-width: 1120px;
  max-height: calc(100vh - 32px);
  overflow: auto;
  padding: 0;
  gap: 0;
}
.position-ocr-modal-header {
  position: sticky;
  top: 0;
  z-index: 3;
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 20px;
  padding: 18px 20px;
  border-bottom: 1px solid var(--line);
  background: var(--surface-2);
}
.position-ocr-modal-header > div {
  display: grid;
  gap: 4px;
}
.position-ocr-modal-header span {
  color: #61d8c0;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0;
}
.position-ocr-modal-header h3 {
  font-size: 20px;
}
.position-ocr-modal-header p,
.position-ocr-result header p {
  margin: 0;
  color: var(--muted);
  font-size: 11px;
}
.position-ocr-modal-header > button {
  width: 34px;
  height: 34px;
  flex: 0 0 34px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: #111;
  color: var(--muted-2);
  font-size: 22px;
  line-height: 1;
}
.position-ocr-controls {
  display: grid;
  grid-template-columns: minmax(220px, 1fr) 180px minmax(180px, auto);
  gap: 12px;
  padding: 16px 20px 0;
}
.position-ocr-model {
  display: grid;
  align-content: center;
  gap: 4px;
  padding: 0 12px;
  border-left: 1px solid var(--line);
}
.position-ocr-model span {
  color: var(--muted);
  font-size: 11px;
}
.position-ocr-model strong {
  color: var(--gold-2);
  font-size: 12px;
}
.position-ocr-dropzone {
  display: grid;
  grid-template-columns: 90px minmax(0, 1fr) auto;
  align-items: center;
  gap: 16px;
  min-height: 112px;
  margin: 16px 20px 0;
  padding: 14px;
  border: 1px dashed var(--line-strong);
  border-radius: 6px;
  background: #0d0d0e;
}
.position-ocr-dropzone.dragging {
  border-color: #43cbb0;
  background: rgba(37, 211, 180, 0.06);
}
.position-ocr-dropzone img {
  width: 90px;
  height: 80px;
  object-fit: cover;
  border: 1px solid var(--line);
  border-radius: 4px;
}
.position-ocr-dropzone > div {
  display: grid;
  justify-items: start;
  gap: 6px;
  min-width: 0;
}
.position-ocr-dropzone > div > strong {
  max-width: 100%;
  overflow: hidden;
  color: var(--text);
  font-size: 13px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.position-ocr-dropzone > div > span {
  color: var(--muted);
  font-size: 11px;
}
.position-ocr-file-button {
  display: inline-flex !important;
  color: var(--gold-2) !important;
  cursor: pointer;
}
.position-ocr-file-button input {
  display: none;
}
.position-ocr-file-button span {
  padding: 5px 10px;
  border: 1px solid var(--gold-dim);
  border-radius: 5px;
  background: #111;
}
.position-ocr-dropzone > button {
  min-width: 100px;
  height: 36px;
  border: 1px solid #2dbda0;
  border-radius: 6px;
  background: rgba(37, 211, 180, 0.1);
  color: #7fe3cf;
}
.position-ocr-dropzone button:disabled,
.position-ocr-footer button:disabled {
  cursor: not-allowed;
  opacity: 0.45;
}
.position-ocr-message {
  margin: 12px 20px 0;
}
.position-ocr-progress {
  display: grid;
  gap: 12px;
  margin: 14px 20px 0;
  padding: 16px;
  border: 1px solid rgba(37, 211, 180, 0.3);
  border-radius: 6px;
  background: linear-gradient(90deg, rgba(37, 211, 180, 0.07), rgba(211, 170, 84, 0.04));
}
.position-ocr-progress header {
  display: grid;
  grid-template-columns: 34px minmax(0, 1fr) auto;
  align-items: center;
  gap: 12px;
}
.position-ocr-progress header > div {
  display: grid;
  gap: 3px;
}
.position-ocr-progress header strong {
  color: #8ce9d6;
  font-size: 14px;
}
.position-ocr-progress header p,
.position-ocr-waiting-tip {
  margin: 0;
  color: var(--muted);
  font-size: 11px;
}
.position-ocr-progress time {
  color: var(--gold-2);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}
.position-ocr-spinner {
  width: 30px;
  height: 30px;
  border: 2px solid rgba(37, 211, 180, 0.18);
  border-top-color: #61d8c0;
  border-radius: 50%;
  animation: position-ocr-spin 0.8s linear infinite;
}
.position-ocr-progress-track {
  height: 5px;
  overflow: hidden;
  border-radius: 3px;
  background: rgba(255, 255, 255, 0.08);
}
.position-ocr-progress-track > span {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: linear-gradient(90deg, #2dbda0, #d3aa54);
  transition: width 0.65s ease;
}
.position-ocr-progress ol {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
  margin: 0;
  padding: 0;
  list-style: none;
}
.position-ocr-progress li {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  color: var(--muted);
  font-size: 11px;
  white-space: nowrap;
}
.position-ocr-progress li > span {
  display: grid;
  width: 20px;
  height: 20px;
  flex: 0 0 20px;
  place-items: center;
  border: 1px solid var(--line-strong);
  border-radius: 50%;
  color: var(--muted);
  font-size: 10px;
}
.position-ocr-progress li.active {
  color: #8ce9d6;
}
.position-ocr-progress li.active > span {
  border-color: #2dbda0;
  background: rgba(37, 211, 180, 0.12);
  color: #8ce9d6;
  box-shadow: 0 0 0 4px rgba(37, 211, 180, 0.06);
}
.position-ocr-progress li.complete {
  color: var(--text);
}
.position-ocr-progress li.complete > span {
  border-color: rgba(37, 211, 180, 0.5);
  background: #2dbda0;
  color: #07110f;
}
.position-ocr-waiting-tip {
  min-height: 16px;
  padding-top: 2px;
  border-top: 1px solid rgba(255, 255, 255, 0.06);
}
@keyframes position-ocr-spin {
  to { transform: rotate(360deg); }
}
.position-ocr-result {
  display: grid;
  gap: 10px;
  margin-top: 16px;
  border-top: 1px solid var(--line);
}
.position-ocr-result > header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 14px 20px 0;
}
.position-ocr-result h4 {
  margin: 0 0 4px;
  color: var(--gold-2);
  font-size: 14px;
}
.position-ocr-result > header > strong {
  color: #61d8c0;
  font-size: 12px;
}
.position-ocr-table-wrap {
  overflow-x: auto;
  padding: 0 20px;
}
.position-ocr-table-wrap table {
  width: 100%;
  min-width: 930px;
  border-collapse: collapse;
  table-layout: fixed;
}
.position-ocr-table-wrap th,
.position-ocr-table-wrap td {
  padding: 8px;
  border: 1px solid var(--line);
  text-align: left;
  vertical-align: top;
}
.position-ocr-table-wrap th {
  background: #171713;
  color: var(--gold-2);
  font-size: 11px;
  font-weight: 600;
}
.position-ocr-table-wrap th:first-child {
  width: 42px;
}
.position-ocr-table-wrap th:nth-child(2) {
  width: 126px;
}
.position-ocr-table-wrap th:nth-child(3) {
  width: 118px;
}
.position-ocr-table-wrap th:nth-child(4),
.position-ocr-table-wrap th:nth-child(5),
.position-ocr-table-wrap th:nth-child(6) {
  width: 112px;
}
.position-ocr-table-wrap th:nth-child(7) {
  width: 190px;
}
.position-ocr-table-wrap th:last-child {
  width: 58px;
}
.position-ocr-table-wrap td {
  color: var(--muted-2);
  font-size: 11px;
}
.position-ocr-table-wrap input {
  width: 100%;
  height: 32px;
  padding: 0 7px;
}
.position-ocr-table-wrap td:nth-child(7) {
  display: grid;
  gap: 4px;
}
.position-ocr-table-wrap td small {
  color: var(--muted);
  line-height: 1.35;
}
.position-ocr-validation-badge {
  width: max-content;
  padding: 2px 6px;
  border: 1px solid rgba(255, 93, 93, 0.35);
  border-radius: 4px;
  color: var(--danger);
}
.position-ocr-validation-badge.valid {
  border-color: rgba(39, 197, 138, 0.35);
  color: var(--ok);
}
.position-ocr-validation-badge.corrected {
  border-color: rgba(227, 183, 92, 0.45);
  color: var(--gold);
}
.danger-text {
  padding: 4px 0;
  border: 0;
  background: transparent;
  color: var(--danger);
}
.position-ocr-json {
  margin: 0 20px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: #0c0c0d;
}
.position-ocr-json summary {
  padding: 9px 11px;
  color: var(--muted-2);
  font-size: 11px;
  cursor: pointer;
}
.position-ocr-json pre {
  max-height: 260px;
  margin: 0;
  overflow: auto;
  padding: 12px;
  border-top: 1px solid var(--line);
  color: #9bdacb;
  font-size: 10px;
  line-height: 1.5;
  white-space: pre-wrap;
}
.position-ocr-footer {
  position: sticky;
  bottom: 0;
  z-index: 3;
  padding: 14px 20px;
  border-top: 1px solid var(--line);
  background: var(--surface-2);
}
.wb-tabs {
  display: flex;
  gap: 4px;
  margin: 0 0 12px;
  border-bottom: 1px solid #e5e7eb;
}
.wb-tabs button {
  padding: 6px 16px;
  border: none;
  background: none;
  cursor: pointer;
  color: #6b7280;
  border-bottom: 2px solid transparent;
  font-size: 14px;
}
.wb-tabs button.active {
  color: #2563eb;
  border-bottom-color: #2563eb;
  font-weight: 600;
}
.trade-dir.buy { color: #dc2626; font-weight: 600; }
.trade-dir.sell { color: #16a34a; font-weight: 600; }
.trade-dir.close { color: #6b7280; font-weight: 600; }
.trade-empty { padding: 16px; color: #9ca3af; text-align: center; }
@media (max-width: 760px) {
  .position-ocr-modal {
    width: calc(100vw - 16px);
    max-height: calc(100vh - 16px);
  }
  .position-ocr-controls {
    grid-template-columns: 1fr;
  }
  .position-ocr-model {
    padding: 8px 0 0;
    border-top: 1px solid var(--line);
    border-left: 0;
  }
  .position-ocr-dropzone {
    grid-template-columns: 72px minmax(0, 1fr);
  }
  .position-ocr-dropzone img {
    width: 72px;
    height: 64px;
  }
  .position-ocr-dropzone > button {
    grid-column: 1 / -1;
    width: 100%;
  }
  .position-ocr-progress header {
    grid-template-columns: 30px minmax(0, 1fr);
  }
  .position-ocr-progress time {
    grid-column: 2;
  }
  .position-ocr-progress ol {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>

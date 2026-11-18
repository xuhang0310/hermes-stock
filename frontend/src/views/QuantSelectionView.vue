<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { api } from '../api'
import type {
  ExpectationGapThesis,
  LayeredSelectionItem,
  LayeredSelectionResponse,
  LayeredStockInspection,
  MarketRegimeL1,
  PositionAccount,
  QuantBoard,
  QuantPlanType,
  QuantVerdict,
  UserPayload,
} from '../types'

const props = defineProps<{
  user: UserPayload
  accounts?: PositionAccount[]
  initialIndustry?: string
  // 嵌在“选股”页里时不显示跳回自动选股的入口
  embedded?: boolean
}>()

const emit = defineEmits<{
  askStock: [item: LayeredSelectionItem, regime: MarketRegimeL1 | null]
  watchlistAdded: [item: Awaited<ReturnType<typeof api.addWatchlist>>]
  openBaseline: []
  openCustomScreener: []
}>()

type ViewTab = 'pool' | 'method'
type WatchStatus = 'adding' | 'added' | 'error'
type PoolTier = 'mainstream' | 'large_core' | 'growth_mid' | 'small_micro' | 'all'

const activeTab = ref<ViewTab>('pool')
const planType = ref<QuantPlanType>('swing')
const poolTier = ref<PoolTier>('mainstream')
const verdictFilter = ref<QuantVerdict | ''>('action')
const industryFilter = ref(props.initialIndustry || '')
const keyword = ref('')
const priceMax = ref('')
const peMax = ref('')
const totalMvMin = ref('50')
const totalMvMax = ref('')
const capital = ref(200000)
const selectedAccountId = ref<number | null>(null)
const selectedBoards = ref<QuantBoard[]>(['MAIN', 'CHINEXT', 'STAR'])

const loading = ref(false)
const rebuilding = ref(false)
const error = ref('')
const notice = ref('')
const response = ref<LayeredSelectionResponse | null>(null)

const drawerOpen = ref(false)
const drawerLoading = ref(false)
const detail = ref<LayeredStockInspection | null>(null)
const thesisLoading = ref(false)
const aiThesis = ref<ExpectationGapThesis | null>(null)
const drawerRef = ref<HTMLElement | null>(null)
const watchMap = ref<Record<string, WatchStatus>>({})

const permissions = computed(() => props.user.permissions || [])
const hasPerm = (p: string) => permissions.value.includes('all') || permissions.value.includes(p)
const canWrite = computed(() => hasPerm('data:write'))
// 重算全市场快照所有用户共用，和定时任务同一个权限
const canRebuild = computed(() => hasPerm('schedule:manage'))
const canAsk = computed(() => hasPerm('agent:use'))

const planOptions: Array<{ key: QuantPlanType; label: string; desc: string }> = [
  { key: 'swing', label: '波段交易（5–15日）', desc: '重 L5 量价反转与防破位飞刀，避开财报与利空窗口' },
  { key: 'long', label: '中长线配置（30–120日）', desc: '重 L3 PIT 财报质量与 ROE/估值安全边际，严控高负债' },
  { key: 'event', label: '事件与预期差（3–10日）', desc: '重 L4 业绩增速二阶提速与 L6 正面公告催化反应' },
]

const boardOptions: Array<{ key: QuantBoard; label: string }> = [
  { key: 'MAIN', label: '沪深主板' },
  { key: 'CHINEXT', label: '创业板' },
  { key: 'STAR', label: '科创板' },
  { key: 'BSE', label: '北交所' },
]

const poolTierOptions: Array<{ key: PoolTier; label: string; mvMin: string; mvMax: string }> = [
  { key: 'mainstream', label: '主流精选池（≥50亿·默认）', mvMin: '50', mvMax: '' },
  { key: 'large_core', label: '核心大中盘（≥150亿）', mvMin: '150', mvMax: '' },
  { key: 'growth_mid', label: '专精成长（50–200亿）', mvMin: '50', mvMax: '200' },
  { key: 'small_micro', label: '小微盘观察（<50亿）', mvMin: '', mvMax: '50' },
  { key: 'all', label: '全市场不限', mvMin: '', mvMax: '' },
]

const layerMeta: Array<{
  key: keyof LayeredSelectionItem['layers']
  short: string
  label: string
  colorClass: string
}> = [
  { key: 'l2_industry', short: 'L2 行业', label: 'L2 行业景气顺风', colorClass: 'seg-l2' },
  { key: 'l3_fundamental', short: 'L3 质地', label: 'L3 财务质量与估值', colorClass: 'seg-l3' },
  { key: 'l4_expectation', short: 'L4 预期差', label: 'L4 预期差与加速度', colorClass: 'seg-l4' },
  { key: 'l5_timing', short: 'L5 时机', label: 'L5 模型排名与时机', colorClass: 'seg-l5' },
  { key: 'l6_event', short: 'L6 事件', label: 'L6 公告催化与窗口', colorClass: 'seg-l6' },
]

const regime = computed<MarketRegimeL1 | null>(() => response.value?.regime || null)
const items = computed<LayeredSelectionItem[]>(() => response.value?.items || [])
const summary = computed(() => response.value?.summary || { total: 0, action: 0, watch: 0, pass: 0 })
const currentWeights = computed(() => response.value?.weights || {})

const stanceTone = computed(() => {
  const s = regime.value?.stance
  if (s === 'attack') return 'is-attack'
  if (s === 'defense') return 'is-defense'
  return 'is-neutral'
})

function fmtNum(val: number | null | undefined, digits = 2): string {
  if (val == null || Number.isNaN(val)) return '—'
  return Number(val).toFixed(digits)
}

function fmtPctRatio(val: number | null | undefined, digits = 1): string {
  if (val == null || Number.isNaN(val)) return '—'
  return `${(Number(val) * 100).toFixed(digits)}%`
}

function fmtDate(compact: string | null | undefined): string {
  if (!compact) return '暂无截面'
  const s = String(compact).replace(/-/g, '')
  if (s.length !== 8) return compact
  return `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}`
}

function fmtTrend(trend: string | null | undefined): string {
  if (trend === 'bull') return '多头顺风'
  if (trend === 'bear') return '空头弱势'
  if (trend === 'neutral') return '震荡走平'
  return trend || '—'
}

function selectPoolTier(tier: PoolTier) {
  poolTier.value = tier
  const found = poolTierOptions.find((t) => t.key === tier)
  if (found) {
    totalMvMin.value = found.mvMin
    totalMvMax.value = found.mvMax
  }
  void runQuery()
}

function returnClass(pct: number | null | undefined): string {
  if (pct == null || pct === 0) return ''
  return pct > 0 ? 'pnl-up' : 'pnl-down'
}

function toggleBoard(board: QuantBoard) {
  const idx = selectedBoards.value.indexOf(board)
  if (idx >= 0) {
    if (selectedBoards.value.length > 1) selectedBoards.value.splice(idx, 1)
  } else {
    selectedBoards.value.push(board)
  }
}

function selectVerdict(v: QuantVerdict | '') {
  verdictFilter.value = v
  void runQuery()
}

function selectPlan(p: QuantPlanType) {
  planType.value = p
  void runQuery()
}

async function runQuery() {
  loading.value = true
  error.value = ''
  try {
    const pMax = priceMax.value.trim() ? Number(priceMax.value) : null
    const peLim = peMax.value.trim() ? Number(peMax.value) : null
    const mvMin = totalMvMin.value.trim() ? Number(totalMvMin.value) : null
    const mvMax = totalMvMax.value.trim() ? Number(totalMvMax.value) : null
    response.value = await api.quantSelectionQuery({
      plan_type: planType.value,
      verdict_filter: verdictFilter.value || null,
      industry: industryFilter.value.trim() || null,
      keyword: keyword.value.trim() || null,
      boards: selectedBoards.value.length ? selectedBoards.value : null,
      price_max: pMax && pMax > 0 ? pMax : null,
      pe_max: peLim && peLim > 0 ? peLim : null,
      total_mv_min: mvMin != null && mvMin >= 0 ? mvMin : null,
      total_mv_max: mvMax && mvMax > 0 ? mvMax : null,
      capital: capital.value,
      account_id: selectedAccountId.value,
      limit: 60,
    })
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载分层选股数据失败'
  } finally {
    loading.value = false
  }
}

async function rebuildSnapshots(withAuxSync = false) {
  rebuilding.value = true
  error.value = ''
  notice.value = ''
  try {
    const res = await api.quantSelectionRebuild({
      sync_indices: withAuxSync,
      sync_events: withAuxSync,
      sync_financial_pit: false,
    })
    const count = Number(res.rows_written || 0)
    notice.value = `已完成 ${fmtDate(String(res.trade_date || ''))} 分层截面重算（共写入 ${count} 条策略快照）`
    await runQuery()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '重算分层截面失败'
  } finally {
    rebuilding.value = false
  }
}

async function openStockDrawer(item: LayeredSelectionItem) {
  drawerOpen.value = true
  drawerLoading.value = true
  aiThesis.value = null
  detail.value = null
  await nextTick()
  drawerRef.value?.focus()
  try {
    detail.value = await api.quantSelectionStockDetail(item.symbol, {
      tradeDate: item.trade_date,
      capital: capital.value,
      accountId: selectedAccountId.value,
    })
  } catch (err) {
    error.value = err instanceof Error ? err.message : '读取个股 L1–L7 体检失败'
    drawerOpen.value = false
  } finally {
    drawerLoading.value = false
  }
}

function closeDrawer() {
  drawerOpen.value = false
}

async function runAiExpectationCheck() {
  if (!detail.value || thesisLoading.value) return
  thesisLoading.value = true
  try {
    const res = await api.quantSelectionExpectationGap(detail.value.symbol, detail.value.trade_date)
    aiThesis.value = res
    if (detail.value.plans[planType.value]) {
      detail.value.plans[planType.value].expectation_thesis = res.formatted_thesis
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : '生成预期差三段论失败'
  } finally {
    thesisLoading.value = false
  }
}

async function addWatch(item: LayeredSelectionItem) {
  if (!canWrite.value || watchMap.value[item.symbol] === 'adding') return
  watchMap.value[item.symbol] = 'adding'
  try {
    const created = await api.addWatchlist({
      symbol: item.symbol,
      group_name: '量化选股',
      reason: `[${item.plan_label}·${item.verdict_label} ${fmtNum(item.total_score, 1)}分] ${item.expectation_thesis || ''}`.slice(0, 180),
    })
    watchMap.value[item.symbol] = 'added'
    emit('watchlistAdded', created)
  } catch {
    watchMap.value[item.symbol] = 'error'
  }
}

function watchBtnText(symbol: string): string {
  const st = watchMap.value[symbol]
  if (st === 'adding') return '加入中…'
  if (st === 'added') return '已在自选'
  if (st === 'error') return '重试自选'
  return '+ 自选'
}

function resetFilters() {
  poolTier.value = 'mainstream'
  industryFilter.value = ''
  keyword.value = ''
  priceMax.value = ''
  peMax.value = ''
  totalMvMin.value = '50'
  totalMvMax.value = ''
  verdictFilter.value = 'action'
  void runQuery()
}

function switchTab(tab: ViewTab) {
  activeTab.value = tab
}

function handleTabKeydown(e: KeyboardEvent) {
  const order: ViewTab[] = ['pool', 'method']
  const idx = order.indexOf(activeTab.value)
  if (e.key === 'ArrowRight') {
    e.preventDefault()
    switchTab(order[(idx + 1) % order.length])
  } else if (e.key === 'ArrowLeft') {
    e.preventDefault()
    switchTab(order[(idx + order.length - 1) % order.length])
  }
}

watch(
  () => props.initialIndustry,
  (val) => {
    if (val && val !== industryFilter.value) {
      industryFilter.value = val
      verdictFilter.value = ''
      void runQuery()
    }
  }
)

onMounted(() => {
  void runQuery()
})
</script>

<template>
  <section class="qs-page">
    <!-- L1 市场环境温度计顶栏（独立定仓位上限，绝不掺入个股打分） -->
    <header class="qs-regime-banner" :class="stanceTone">
      <div class="qs-regime-main">
        <div class="qs-stance-badge">
          <span class="qs-stance-tag">L1 市场环境</span>
          <strong>{{ regime?.stance_label || '中性' }} · 温度 {{ fmtNum(regime?.regime_score, 0) }}</strong>
        </div>
        <div class="qs-cap-callout">
          <span>建议总仓位上限</span>
          <strong>{{ regime ? `${Math.round(regime.max_position_ratio * 100)}%` : '60%' }}</strong>
        </div>
        <div class="qs-cap-callout">
          <span>当前风格象限</span>
          <strong>{{ regime?.style_label || '均衡震荡轮动' }}</strong>
        </div>
      </div>

      <div class="qs-regime-metrics">
        <div class="qs-metric-cell">
          <span>站上 20 日线宽度</span>
          <strong>{{ fmtPctRatio(regime?.breadth_ma20) }}</strong>
        </div>
        <div class="qs-metric-cell">
          <span>站上 60 日线宽度</span>
          <strong>{{ fmtPctRatio(regime?.breadth_ma60) }}</strong>
        </div>
        <div class="qs-metric-cell">
          <span>两市成交 / 120日分位</span>
          <strong>
            {{ regime?.total_amount_yi != null ? `${fmtNum(regime.total_amount_yi, 0)}亿` : '—' }}
            / {{ fmtPctRatio(regime?.amount_pct_120, 0) }}
          </strong>
        </div>
        <div class="qs-metric-cell">
          <span>沪深300 / 中证1000</span>
          <strong>{{ fmtTrend(regime?.hs300_trend) }} / {{ fmtTrend(regime?.zz1000_trend) }}</strong>
        </div>
        <div class="qs-metric-actions">
          <span class="qs-date-label">截面 {{ fmtDate(response?.trade_date) }}</span>
          <button
            v-if="canRebuild"
            class="ui-btn ui-btn--ghost ui-btn--sm"
            type="button"
            :disabled="rebuilding"
            @click="rebuildSnapshots(false)"
          >
            {{ rebuilding ? '计算中…' : '刷新截面' }}
          </button>
        </div>
      </div>
    </header>

    <div v-if="error" class="qs-alert qs-alert--danger">{{ error }}</div>
    <div v-if="notice" class="qs-alert qs-alert--ok">{{ notice }}</div>

    <!-- 顶层协同视图切换 -->
    <div class="qs-nav-row">
      <div class="ui-tabs" role="tablist" aria-label="量化选股工作台视图" @keydown="handleTabKeydown">
        <button
          type="button"
          role="tab"
          class="ui-tab"
          :class="{ 'is-active': activeTab === 'pool' }"
          :aria-selected="activeTab === 'pool'"
          @click="switchTab('pool')"
        >
          分层打分（未验证）
        </button>
        <button
          type="button"
          role="tab"
          class="ui-tab"
          :class="{ 'is-active': activeTab === 'method' }"
          :aria-selected="activeTab === 'method'"
          @click="switchTab('method')"
        >
          打分方法
        </button>
      </div>

      <div class="qs-cross-links">
        <button v-if="!embedded" class="ui-btn ui-btn--ghost ui-btn--sm" type="button" @click="emit('openBaseline')">
          看模型排名前 30（验证过）
        </button>
        <button class="ui-btn ui-btn--ghost ui-btn--sm" type="button" @click="emit('openCustomScreener')">
          传统指标条件器
        </button>
      </div>
    </div>

    <!-- Tab 1: L1-L7 分层选股主工作台 -->
    <div v-if="activeTab === 'pool'" role="tabpanel" class="qs-workbench">
      <!-- 策略档位（切权重与专属否决） + 三档结论卡片 -->
      <div class="qs-top-grid">
        <div class="qs-plan-cards">
          <button
            v-for="p in planOptions"
            :key="p.key"
            type="button"
            class="qs-plan-card"
            :class="{ 'is-selected': planType === p.key }"
            @click="selectPlan(p.key)"
          >
            <div class="qs-plan-head">
              <strong>{{ p.label }}</strong>
              <span v-if="planType === p.key" class="qs-active-pill">当前视角</span>
            </div>
            <p>{{ p.desc }}</p>
          </button>
        </div>

        <div class="qs-verdict-cards">
          <button
            type="button"
            class="qs-verdict-card is-action"
            :class="{ 'is-selected': verdictFilter === 'action' }"
            @click="selectVerdict('action')"
          >
            <span>高分（≥70·未排除）</span>
            <strong>{{ summary.action }}</strong>
          </button>
          <button
            type="button"
            class="qs-verdict-card is-watch"
            :class="{ 'is-selected': verdictFilter === 'watch' }"
            @click="selectVerdict('watch')"
          >
            <span>中等（55–69 或数据不全）</span>
            <strong>{{ summary.watch }}</strong>
          </button>
          <button
            type="button"
            class="qs-verdict-card is-pass"
            :class="{ 'is-selected': verdictFilter === 'pass' }"
            @click="selectVerdict('pass')"
          >
            <span>低分或排除（&lt;55 或排雷、否决）</span>
            <strong>{{ summary.pass }}</strong>
          </button>
          <button
            type="button"
            class="qs-verdict-card"
            :class="{ 'is-selected': verdictFilter === '' }"
            @click="selectVerdict('')"
          >
            <span>全池标的</span>
            <strong>{{ summary.total }}</strong>
          </button>
        </div>
      </div>

      <!-- 股票池生态位一键切换 + 账户感知过滤栏 -->
      <form class="qs-filter-bar" @submit.prevent="runQuery">
        <div class="qs-pool-tier-row">
          <div class="qs-board-toggles">
            <span class="qs-board-label">选股生态位：</span>
            <button
              v-for="t in poolTierOptions"
              :key="t.key"
              type="button"
              class="qs-board-chip"
              :class="{ 'is-on': poolTier === t.key }"
              @click="selectPoolTier(t.key)"
            >
              {{ t.label }}
            </button>
          </div>
          <span v-if="poolTier === 'small_micro'" class="qs-micro-warn">
            提示：小微盘流动性折价波动大，仅适合分散篮子观察，不宜单票重仓
          </span>
        </div>

        <div class="qs-filter-group">
          <label>
            <span>账户预算（元）</span>
            <input v-model.number="capital" type="number" min="10000" step="10000" />
          </label>
          <label v-if="accounts && accounts.length">
            <span>关联真实账户</span>
            <select v-model="selectedAccountId">
              <option :value="null">仅用上方预算测算</option>
              <option v-for="acc in accounts" :key="acc.id" :value="acc.id">
                {{ acc.account_name || acc.broker_name }}
              </option>
            </select>
          </label>
          <label>
            <span>股价上限（元）</span>
            <input v-model="priceMax" type="number" min="1" placeholder="如 50" />
          </label>
          <label>
            <span>PE(TTM)上限</span>
            <input v-model="peMax" type="number" min="1" placeholder="如 45" />
          </label>
          <label>
            <span>行业筛选</span>
            <input v-model="industryFilter" type="text" placeholder="如 半导体" />
          </label>
          <label>
            <span>代码 / 名称</span>
            <input v-model="keyword" type="text" placeholder="搜索标的" />
          </label>
        </div>

        <div class="qs-filter-bottom">
          <div class="qs-board-toggles">
            <span class="qs-board-label">可买板块权限：</span>
            <button
              v-for="b in boardOptions"
              :key="b.key"
              type="button"
              class="qs-board-chip"
              :class="{ 'is-on': selectedBoards.includes(b.key) }"
              @click="toggleBoard(b.key)"
            >
              {{ b.label }}
            </button>
          </div>

          <div class="qs-weight-legend">
            <span v-for="lm in layerMeta" :key="lm.key" class="qs-legend-item">
              <i :class="lm.colorClass"></i>
              {{ lm.short }} {{ currentWeights[lm.key] ? `${Math.round(currentWeights[lm.key] * 100)}%` : '' }}
            </span>
          </div>

          <div class="qs-filter-btns">
            <button class="ui-btn ui-btn--ghost ui-btn--sm" type="button" @click="resetFilters">重置</button>
            <button class="ui-btn ui-btn--primary ui-btn--sm" type="submit" :disabled="loading">
              {{ loading ? '筛选中…' : '执行分层筛选' }}
            </button>
          </div>
        </div>
      </form>

      <!-- 结果表格 -->
      <div class="qs-table-card">
        <div v-if="loading" class="qs-empty">正在读取预计算分层截面…</div>
        <div v-else-if="!items.length" class="qs-empty">
          <p>当前过滤条件下暂无匹配标的（可能处于严格门禁保护或尚未同步本地日线）。</p>
          <div class="qs-empty-actions">
            <button class="ui-btn ui-btn--ghost ui-btn--sm" type="button" @click="selectVerdict('')">
              切换到「全部」看中等和被排除的原因
            </button>
            <button v-if="canRebuild" class="ui-btn ui-btn--primary ui-btn--sm" type="button" @click="rebuildSnapshots(true)">
              同步指数/公告并重算截面
            </button>
          </div>
        </div>

        <div v-else class="qs-table-wrap">
          <table class="qs-table">
            <thead>
              <tr>
                <th>标的 / 行业 / 市值</th>
                <th>结论档位</th>
                <th>总分 / 覆盖率</th>
                <th>L2–L6 分层证据条（点击行看详情）</th>
                <th>现价 / 涨跌</th>
                <th>挂买价 / 止损下限</th>
                <th>新开仓参考股数</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="item in items"
                :key="item.symbol"
                class="qs-row"
                @click="openStockDrawer(item)"
              >
                <td>
                  <div class="qs-sym-cell">
                    <strong>{{ item.name }}</strong>
                    <span>
                      {{ item.symbol }} · {{ item.industry || '未分类' }} ·
                      {{ item.total_mv_yi != null ? `${fmtNum(item.total_mv_yi, 0)}亿` : '—' }}
                    </span>
                  </div>
                </td>
                <td>
                  <span class="qs-verdict-pill" :class="`is-${item.verdict}`">
                    {{ item.verdict_label }}
                  </span>
                  <div v-if="item.vetoed" class="qs-veto-hint">
                    {{ item.veto_labels[0] || '被排除' }}
                  </div>
                </td>
                <td class="qs-num">
                  <strong class="qs-score-num">{{ fmtNum(item.total_score, 1) }}</strong>
                  <span class="qs-sub-num">覆盖 {{ fmtPctRatio(item.coverage_ratio, 0) }}</span>
                </td>
                <td>
                  <div class="qs-stack-bar" :title="item.expectation_thesis || ''">
                    <span
                      v-for="lm in layerMeta"
                      :key="lm.key"
                      class="qs-stack-seg"
                      :class="[lm.colorClass, { 'is-missing': item.layers[lm.key] == null }]"
                      :style="{ width: `${(currentWeights[lm.key] || 0.2) * 100}%`, opacity: item.layers[lm.key] == null ? 0.22 : Math.max(0.35, (item.layers[lm.key] || 50) / 100) }"
                    ></span>
                  </div>
                  <div class="qs-layer-mini">
                    <span v-for="lm in layerMeta" :key="lm.key">
                      {{ lm.short.slice(0, 2) }}:{{ item.layers[lm.key] != null ? Math.round(item.layers[lm.key]!) : '缺' }}
                    </span>
                  </div>
                </td>
                <td class="qs-num">
                  <div>{{ fmtNum(item.close_price) }}</div>
                  <span class="qs-sub-num" :class="returnClass(item.pct_chg)">
                    {{ item.pct_chg != null ? `${item.pct_chg > 0 ? '+' : ''}${fmtNum(item.pct_chg)}%` : '—' }}
                  </span>
                </td>
                <td class="qs-num">
                  <div v-if="item.suggested_buy_price != null">挂买 {{ fmtNum(item.suggested_buy_price) }}</div>
                  <div v-else class="qs-muted-reason" :title="item.evidence.buy_basis || ''">模型说不挂</div>
                  <span class="qs-sub-num">止损不高于 {{ fmtNum(item.stop_ref_price) }}</span>
                </td>
                <td class="qs-num">
                  <template v-if="item.l7_risk.eligible">
                    <strong>{{ item.l7_risk.advised_shares }} 股</strong>
                    <span class="qs-sub-num">
                      仓位 {{ fmtPctRatio(item.l7_risk.position_pct, 1) }} · 跌 2 倍 ATR 约亏 ¥{{ fmtNum(item.l7_risk.max_loss_amount, 0) }}
                    </span>
                  </template>
                  <span v-else class="qs-sub-num qs-muted-reason">{{ item.l7_risk.reason }}</span>
                </td>
                <td @click.stop>
                  <div class="qs-row-actions">
                    <button
                      v-if="canWrite"
                      class="ui-btn ui-btn--ghost ui-btn--sm"
                      type="button"
                      :disabled="watchMap[item.symbol] === 'adding' || watchMap[item.symbol] === 'added'"
                      @click="addWatch(item)"
                    >
                      {{ watchBtnText(item.symbol) }}
                    </button>
                    <button
                      v-if="canAsk"
                      class="ui-btn ui-btn--ghost ui-btn--sm"
                      type="button"
                      @click="emit('askStock', item, regime)"
                    >
                      问股
                    </button>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- 打分方法 -->
    <div v-else role="tabpanel" class="qs-method-panel">
      <article class="qs-method-card">
        <h3>一、这个打分是什么，验证到哪一步</h3>
        <p>
          分层打分把行业、基本面、预期差、时机、事件五层的分数按人定的权重合成总分，L1 大盘环境单独给仓位参考。
          其中只有时机层用到的模型排名经过了历史回测（2019 年以后年化超额约 15 个百分点）；五层权重、70 分的高分线、破位和阴跌否决、大盘仓位上限都没有回测。
          所以这里的“高分”只说明按这套规则分数高，不是买入信号。波段档的总分已经作为挑战者“分层打分前 30”接进自动选股，和模型排名前 30 用同一套规则记成绩，好不好看那边的历史战绩。
        </p>
      </article>
      <article class="qs-method-card">
        <h3>二、各层做什么，哪些验证过</h3>
        <ul class="qs-rule-list">
          <li><strong>L1 市场环境独立控仓</strong>：依据全 A 站上 20/60 日均线占比、成交分位与宽基趋势输出 90%/60%/30% 总仓位上限，绝不掺入个股分数。</li>
          <li><strong>三大横截面去偏（防微盘虹吸与单一行业霸榜）</strong>：① L5 将量价分在四档市值同温层（&lt;50亿 / 50–100亿 / 100–300亿 / ≥300亿）内独立中性化，并融合 45% 自身均线多头缩量回踩结构分；② L3 估值与盈利质地采用 65% 同行业内排名 + 35% 全市场排名，防止银行低 PE/PB 跨行业通吃；③ 同一行业前 2 只零衰减、第 3 只起阶梯扣分，确保主流精选池呈现跨行业真龙头。</li>
          <li><strong>排除优先于总分</strong>：立案调查、退市风险警示、财务造假非标意见、ST 直接排除；波段档还排除深跌破位和阴跌的股票（没有回测，而且和模型排名偏爱的方向相反）；日均成交不足 4500 万元最高只到「中等」。</li>
          <li><strong>贝叶斯覆盖率收缩</strong>：缺失财务指标不计入权重分母，但覆盖率不足 85% 时向 50 分中性收缩，覆盖率低于 65% 最高只到「中等」。</li>
          <li><strong>哪些验证过</strong>：模型排名、买入模型的挂买价、止损不能比 3 倍 ATR 更近，这三样经过历史回测；L2–L6 的分层权重、「高分」门槛、破位和阴跌否决、L1 仓位上限都还没有回测，只作参考。</li>
          <li><strong>和操作计划同一套代码</strong>：挂买价只来自买入模型，模型说不挂就不给价；止损只给下限（不能比 3 倍 ATR 更近，也可以不设）；新开仓股数按持仓股数的同一公式估算，已有持仓以操作计划为准。系统不下单。</li>
        </ul>
      </article>
    </div>

    <!-- 单票 L1–L7 深度体检抽屉 -->
    <Teleport to="body">
      <div
        v-if="drawerOpen"
        class="qs-drawer-backdrop"
        @click.self="closeDrawer"
        @keydown.esc.stop.prevent="closeDrawer"
      >
        <aside
          ref="drawerRef"
          class="qs-drawer"
          role="dialog"
          aria-modal="true"
          aria-labelledby="qs-drawer-title"
          tabindex="-1"
        >
          <header class="qs-drawer-head">
            <div>
              <h2 id="qs-drawer-title">
                {{ detail?.name || '加载中…' }}
                <span>{{ detail?.symbol }}</span>
              </h2>
              <p v-if="detail">
                {{ detail.industry || '未分类行业' }} · {{ detail.board }} 板块 · 截面 {{ fmtDate(detail.trade_date) }}
              </p>
            </div>
            <button class="ui-btn ui-btn--ghost ui-btn--sm" type="button" @click="closeDrawer">关闭</button>
          </header>

          <div v-if="drawerLoading || !detail" class="qs-empty">正在读取单票 L1–L7 全栈证据…</div>

          <div v-else class="qs-drawer-body">
            <!-- 三档策略横向对比 -->
            <section class="qs-drawer-section">
              <h3>三档持有周期视角对比</h3>
              <div class="qs-plan-compare">
                <div
                  v-for="p in planOptions"
                  :key="p.key"
                  class="qs-plan-mini"
                  :class="{ 'is-current': planType === p.key }"
                >
                  <span>{{ p.label }}</span>
                  <template v-if="detail.plans[p.key]">
                    <strong>{{ fmtNum(detail.plans[p.key].total_score, 1) }} 分</strong>
                    <em class="qs-verdict-pill" :class="`is-${detail.plans[p.key].verdict}`">
                      {{ detail.plans[p.key].verdict_label }}
                    </em>
                  </template>
                </div>
              </div>
            </section>

            <!-- 当前档位被排除的原因 -->
            <section
              v-if="detail.plans[planType]?.vetoed"
              class="qs-drawer-section qs-veto-box"
            >
              <h3>被排除的原因</h3>
              <ul>
                <li v-for="reason in detail.plans[planType].veto_labels" :key="reason">{{ reason }}</li>
              </ul>
            </section>

            <!-- L2-L6 逐层拆解 -->
            <section v-if="detail.plans[planType]" class="qs-drawer-section">
              <h3>L2–L6 分层证据拆解（{{ detail.plans[planType].plan_label }}）</h3>
              <div class="qs-layer-bars">
                <div v-for="lm in layerMeta" :key="lm.key" class="qs-layer-row">
                  <div class="qs-layer-row-top">
                    <span>{{ lm.label }}</span>
                    <strong>
                      {{ detail.plans[planType].layers[lm.key] != null ? `${fmtNum(detail.plans[planType].layers[lm.key], 1)} 分` : '数据暂缺（中性处理）' }}
                    </strong>
                  </div>
                  <div class="qs-progress-track">
                    <div
                      class="qs-progress-fill"
                      :class="lm.colorClass"
                      :style="{ width: `${detail.plans[planType].layers[lm.key] ?? 0}%` }"
                    ></div>
                  </div>
                </div>
              </div>
            </section>

            <!-- L4 预期差三段论与 AI 核验 -->
            <section v-if="detail.plans[planType]" class="qs-drawer-section">
              <div class="qs-section-title-row">
                <h3>L4 预期差与催化核验</h3>
                <button
                  v-if="canAsk"
                  class="ui-btn ui-btn--ghost ui-btn--sm"
                  type="button"
                  :disabled="thesisLoading"
                  @click="runAiExpectationCheck"
                >
                  {{ thesisLoading ? '核验中…' : 'AI 生成预期差三段论' }}
                </button>
              </div>
              <div v-if="aiThesis" class="qs-thesis-box">
                <p><strong>市场以为：</strong>{{ aiThesis.market_consensus }}</p>
                <p><strong>我认为：</strong>{{ aiThesis.our_thesis }}</p>
                <p><strong>客观依据：</strong>{{ aiThesis.hard_evidence }}</p>
                <span class="qs-sufficiency-tag" :class="{ 'is-weak': !aiThesis.evidence_sufficient }">
                  {{ aiThesis.evidence_sufficient ? '客观证据充分' : '客观催化不足（仅建议观察）' }}
                </span>
              </div>
              <p v-else class="qs-plain-thesis">
                {{ detail.plans[planType].expectation_thesis }}
              </p>
            </section>

            <!-- 执行层限价与 L7 风险股数 -->
            <section class="qs-drawer-section">
              <h3>挂买价、止损下限和参考股数</h3>
              <dl class="qs-l7-grid">
                <div>
                  <dt>最新收盘价</dt>
                  <dd>{{ fmtNum(detail.close_price) }} 元</dd>
                </div>
                <div>
                  <dt>买入模型挂买价</dt>
                  <dd>{{ detail.suggested_buy_price != null ? `${fmtNum(detail.suggested_buy_price)} 元` : '不挂' }}</dd>
                </div>
                <div>
                  <dt>止损下限（可以不设）</dt>
                  <dd>{{ fmtNum(detail.stop_ref_price) }} 元</dd>
                </div>
                <div>
                  <dt>新开仓参考股数</dt>
                  <dd>
                    {{ detail.l7_risk.eligible ? `${detail.l7_risk.advised_shares} 股（约 ¥${fmtNum(detail.l7_risk.advised_amount, 0)}）` : '0 股' }}
                  </dd>
                </div>
              </dl>
              <p class="qs-l7-note">
                {{ detail.plans[planType]?.evidence.buy_basis || '' }} · {{ detail.l7_risk.reason }}
              </p>
            </section>

            <footer class="qs-drawer-foot">
              <button
                v-if="canAsk && detail.plans[planType]"
                class="ui-btn ui-btn--primary"
                type="button"
                @click="emit('askStock', detail.plans[planType], detail.regime); closeDrawer()"
              >
                带 L1–L7 完整证据去问股
              </button>
              <button
                v-if="canWrite && detail.plans[planType]"
                class="ui-btn ui-btn--ghost"
                type="button"
                @click="addWatch(detail.plans[planType])"
              >
                {{ watchBtnText(detail.symbol) }}
              </button>
            </footer>
          </div>
        </aside>
      </div>
    </Teleport>
  </section>
</template>

<style scoped>
.qs-page {
  display: grid;
  gap: 12px;
  color: var(--text);
}

.qs-regime-banner {
  display: grid;
  gap: 12px;
  padding: 14px 16px;
  border: 1px solid var(--line-strong);
  border-radius: 8px;
  background: var(--surface);
}
.qs-regime-banner.is-attack {
  border-color: var(--ok);
}
.qs-regime-banner.is-defense {
  border-color: var(--danger);
}

.qs-regime-main {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 20px;
}
.qs-stance-badge {
  display: flex;
  align-items: center;
  gap: 10px;
}
.qs-stance-tag {
  padding: 2px 8px;
  border-radius: 999px;
  background: var(--surface-3);
  color: var(--gold);
  font-size: 12px;
}
.qs-stance-badge strong {
  font-size: 20px;
  font-variant-numeric: tabular-nums;
}
.qs-cap-callout {
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.qs-cap-callout span {
  color: var(--muted);
  font-size: 12px;
}
.qs-cap-callout strong {
  color: var(--gold-2);
  font-size: 16px;
  font-variant-numeric: tabular-nums;
}

.qs-regime-metrics {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding-top: 10px;
  border-top: 1px solid var(--line);
}
.qs-metric-cell {
  display: grid;
  gap: 2px;
}
.qs-metric-cell span {
  color: var(--muted);
  font-size: 12px;
}
.qs-metric-cell strong {
  font-size: 13px;
  font-variant-numeric: tabular-nums;
}
.qs-metric-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}
.qs-date-label {
  color: var(--muted);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}

.qs-alert {
  padding: 10px 14px;
  border-radius: 8px;
  font-size: 13px;
}
.qs-alert--danger {
  border: 1px solid var(--danger);
  background: var(--surface-2);
  color: var(--danger);
}
.qs-alert--ok {
  border: 1px solid var(--ok);
  background: var(--surface-2);
  color: var(--ok);
}

.qs-nav-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.qs-cross-links {
  display: flex;
  gap: 8px;
}

.qs-workbench {
  display: grid;
  gap: 12px;
}

.qs-top-grid {
  display: grid;
  grid-template-columns: 3fr 2fr;
  gap: 12px;
}
.qs-plan-cards {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
}
.qs-plan-card {
  display: grid;
  gap: 6px;
  padding: 12px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface);
  color: var(--text);
  text-align: left;
}
.qs-plan-card.is-selected {
  border-color: var(--gold);
  background: var(--surface-3);
}
.qs-plan-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
}
.qs-plan-head strong {
  font-size: 14px;
}
.qs-active-pill {
  padding: 2px 6px;
  border-radius: 999px;
  background: var(--gold-dim);
  color: var(--text);
  font-size: 12px;
}
.qs-plan-card p {
  margin: 0;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.45;
}

.qs-verdict-cards {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
}
.qs-verdict-card {
  display: grid;
  align-content: center;
  gap: 4px;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface);
  color: var(--text);
  text-align: left;
}
.qs-verdict-card span {
  color: var(--muted);
  font-size: 12px;
}
.qs-verdict-card strong {
  font-size: 20px;
  font-variant-numeric: tabular-nums;
}
.qs-verdict-card.is-selected {
  border-color: var(--gold);
  background: var(--surface-2);
}
.qs-verdict-card.is-action strong {
  color: var(--ok);
}
.qs-verdict-card.is-watch strong {
  color: var(--warning);
}
.qs-verdict-card.is-pass strong {
  color: var(--muted);
}

.qs-filter-bar {
  display: grid;
  gap: 10px;
  padding: 12px 14px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface);
}
.qs-pool-tier-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--line);
}
.qs-micro-warn {
  color: var(--warning);
  font-size: 12px;
}
.qs-filter-group {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(135px, 1fr));
  gap: 10px;
}
.qs-filter-group label {
  display: grid;
  gap: 4px;
}
.qs-filter-group label span {
  color: var(--muted);
  font-size: 12px;
}
.qs-filter-group input,
.qs-filter-group select {
  min-height: 32px;
  padding: 4px 8px;
  border: 1px solid var(--line);
  border-radius: 4px;
  background: var(--bg-2);
  color: var(--text);
  font-size: 13px;
  font-variant-numeric: tabular-nums;
}

.qs-filter-bottom {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding-top: 8px;
  border-top: 1px solid var(--line);
}
.qs-board-toggles {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}
.qs-board-label {
  color: var(--muted);
  font-size: 12px;
}
.qs-board-chip {
  padding: 3px 10px;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: var(--bg-2);
  color: var(--muted);
  font-size: 12px;
}
.qs-board-chip.is-on {
  border-color: var(--gold-dim);
  background: var(--surface-3);
  color: var(--gold-2);
}

.qs-weight-legend {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  color: var(--muted);
  font-size: 12px;
}
.qs-legend-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.qs-legend-item i {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 999px;
}
.seg-l2 { background: var(--cyan); }
.seg-l3 { background: var(--ok); }
.seg-l4 { background: var(--gold); }
.seg-l5 { background: var(--pink); }
.seg-l6 { background: var(--warning); }

.qs-filter-btns {
  display: flex;
  gap: 8px;
}

.qs-table-card {
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface);
  overflow: hidden;
}
.qs-table-wrap {
  overflow-x: auto;
}
.qs-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.qs-table th {
  padding: 10px 12px;
  border-bottom: 1px solid var(--line);
  background: var(--surface-2);
  color: var(--muted);
  font-size: 12px;
  font-weight: 500;
  text-align: left;
  white-space: nowrap;
}
.qs-table td {
  padding: 10px 12px;
  border-bottom: 1px solid var(--line);
  vertical-align: middle;
}
.qs-row {
  cursor: pointer;
}
.qs-row:hover {
  background: var(--surface-2);
}

.qs-sym-cell {
  display: grid;
  gap: 2px;
}
.qs-sym-cell strong {
  font-size: 14px;
}
.qs-sym-cell span {
  color: var(--muted);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}

.qs-verdict-pill {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 500;
}
.qs-verdict-pill.is-action {
  background: rgba(39, 197, 138, 0.16);
  color: var(--ok);
}
.qs-verdict-pill.is-watch {
  background: rgba(213, 166, 66, 0.16);
  color: var(--warning);
}
.qs-verdict-pill.is-pass {
  background: var(--surface-2);
  color: var(--muted);
}
.qs-veto-hint {
  margin-top: 3px;
  color: var(--danger);
  font-size: 12px;
}

.qs-num {
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.qs-score-num {
  display: block;
  color: var(--gold-2);
  font-size: 16px;
}
.qs-sub-num {
  display: block;
  color: var(--muted);
  font-size: 12px;
}
.qs-muted-reason {
  max-width: 190px;
  white-space: normal;
}

.qs-stack-bar {
  display: flex;
  height: 8px;
  min-width: 160px;
  border-radius: 999px;
  background: var(--bg-2);
  overflow: hidden;
  gap: 1px;
}
.qs-stack-seg {
  height: 100%;
}
.qs-layer-mini {
  display: flex;
  gap: 6px;
  margin-top: 4px;
  color: var(--muted);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}

.qs-row-actions {
  display: flex;
  gap: 6px;
}

.qs-empty {
  padding: 32px 16px;
  color: var(--muted);
  text-align: center;
  font-size: 13px;
}
.qs-empty-actions {
  display: flex;
  justify-content: center;
  gap: 8px;
  margin-top: 12px;
}

.qs-method-panel {
  display: grid;
  gap: 12px;
}
.qs-method-card {
  padding: 16px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface);
}
.qs-method-card h3 {
  margin: 0 0 6px;
  font-size: 16px;
}
.qs-method-card p {
  margin: 0 0 12px;
  color: var(--muted);
  font-size: 13px;
  line-height: 1.6;
}
.qs-rule-list {
  margin: 0;
  padding-left: 18px;
  color: var(--text);
  font-size: 13px;
  line-height: 1.75;
}

.pnl-up { color: var(--up, var(--danger)); }
.pnl-down { color: var(--down, var(--ok)); }

.qs-drawer-backdrop {
  position: fixed;
  inset: 0;
  z-index: 90;
  display: flex;
  justify-content: flex-end;
  background: rgba(0, 0, 0, 0.65);
}
.qs-drawer {
  width: min(560px, 100vw);
  height: 100%;
  overflow-y: auto;
  padding: 20px;
  border-left: 1px solid var(--line-strong);
  background: var(--bg-2);
  color: var(--text);
}
.qs-drawer-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--line);
}
.qs-drawer-head h2 {
  margin: 0;
  font-size: 20px;
}
.qs-drawer-head h2 span {
  margin-left: 8px;
  color: var(--muted);
  font-size: 14px;
  font-variant-numeric: tabular-nums;
}
.qs-drawer-head p {
  margin: 4px 0 0;
  color: var(--muted);
  font-size: 12px;
}
.qs-drawer-body {
  display: grid;
  gap: 14px;
  padding-top: 14px;
}
.qs-drawer-section {
  padding: 12px 14px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface);
}
.qs-drawer-section h3 {
  margin: 0 0 10px;
  font-size: 14px;
}
.qs-veto-box {
  border-color: var(--danger);
}
.qs-veto-box h3 {
  color: var(--danger);
}
.qs-veto-box ul {
  margin: 0;
  padding-left: 18px;
  font-size: 13px;
}

.qs-plan-compare {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
}
.qs-plan-mini {
  display: grid;
  gap: 4px;
  padding: 8px 10px;
  border: 1px solid var(--line);
  border-radius: 4px;
  background: var(--bg-2);
}
.qs-plan-mini.is-current {
  border-color: var(--gold);
}
.qs-plan-mini span {
  color: var(--muted);
  font-size: 12px;
}
.qs-plan-mini strong {
  font-size: 16px;
  font-variant-numeric: tabular-nums;
}
.qs-plan-mini em {
  font-style: normal;
  justify-self: start;
}

.qs-layer-bars {
  display: grid;
  gap: 8px;
}
.qs-layer-row-top {
  display: flex;
  justify-content: space-between;
  margin-bottom: 4px;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}
.qs-progress-track {
  height: 8px;
  border-radius: 999px;
  background: var(--bg-2);
  overflow: hidden;
}
.qs-progress-fill {
  height: 100%;
  border-radius: 999px;
}

.qs-section-title-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 8px;
}
.qs-section-title-row h3 {
  margin: 0;
}
.qs-thesis-box {
  display: grid;
  gap: 6px;
  font-size: 13px;
  line-height: 1.55;
}
.qs-thesis-box p {
  margin: 0;
}
.qs-sufficiency-tag {
  justify-self: start;
  padding: 2px 8px;
  border-radius: 999px;
  background: rgba(39, 197, 138, 0.16);
  color: var(--ok);
  font-size: 12px;
}
.qs-sufficiency-tag.is-weak {
  background: rgba(213, 166, 66, 0.16);
  color: var(--warning);
}
.qs-plain-thesis {
  margin: 0;
  color: var(--muted);
  font-size: 13px;
  line-height: 1.6;
}

.qs-l7-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
  margin: 0 0 8px;
}
.qs-l7-grid dt {
  color: var(--muted);
  font-size: 12px;
}
.qs-l7-grid dd {
  margin: 2px 0 0;
  font-size: 14px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.qs-l7-note {
  margin: 0;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.5;
}

.qs-drawer-foot {
  display: flex;
  gap: 10px;
}

@media (max-width: 960px) {
  .qs-top-grid {
    grid-template-columns: 1fr;
  }
  .qs-plan-cards {
    grid-template-columns: 1fr;
  }
  .qs-verdict-cards {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>

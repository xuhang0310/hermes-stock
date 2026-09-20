<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api, ApiError } from '../api'
import type { IndustryMember, IndustryMembers, IndustryOverview, IndustryStat, UserPayload, WatchlistItem } from '../types'
import { industryTiles, mergeSmallIndustries, OTHER_INDUSTRY } from '../utils/industry'

const props = defineProps<{ user: UserPayload }>()
const emit = defineEmits<{
  askStock: [item: IndustryMember]
  screenIndustry: [industry: string]
  openScreener: []
  watchlistAdded: [item: WatchlistItem]
}>()
const data = ref<IndustryOverview | null>(null)
const members = ref<IndustryMembers | null>(null)
const loading = ref(false), memberLoading = ref(false)
const error = ref(''), memberError = ref(''), actionMessage = ref('')
const search = ref(''), selected = ref('')
const view = ref<'heatmap' | 'ranking'>('heatmap')
const metric = ref<'count' | 'amount'>('count')
const rankBy = ref<'avg_pct' | 'amount_yi' | 'up_ratio'>('avg_pct')
const sortBy = ref('pct_chg'), order = ref('desc'), page = ref(0)
const adding = ref(new Set<string>()), added = ref(new Set<string>())
let overviewController: AbortController | undefined
let membersController: AbortController | undefined
let overviewRequest = 0, memberRequest = 0
let disposed = false
const canWrite = computed(() => props.user.permissions.some(p => p === 'all' || p === 'data:write'))
const canAsk = computed(() => props.user.permissions.some(p => p === 'all' || p === 'agent:use'))
const all = computed(() => data.value?.industries ?? [])
const visible = computed(() => all.value.filter(row => row.industry.includes(search.value.trim())).slice().sort((a, b) => {
  const av = a[rankBy.value], bv = b[rankBy.value]
  return Number(av == null) - Number(bv == null) || (bv ?? 0) - (av ?? 0) || a.industry.localeCompare(b.industry)
}))
const current = computed(() => all.value.find(row => row.industry === selected.value))
const heatmapRows = computed(() => mergeSmallIndustries(visible.value, metric.value))
const tiles = computed(() => industryTiles(heatmapRows.value, metric.value))
const hoverStat = ref<IndustryStat | null>(null)
const previewStat = computed(() => hoverStat.value ?? current.value ?? null)
const stale = computed(() => Boolean(
  data.value?.expected_date && data.value?.as_of && data.value.as_of < data.value.expected_date
))
const leading = computed(() => all.value.find(row => row.avg_pct != null))
const active = computed(() => all.value.filter(row => row.amount_yi != null).slice().sort((a, b) => b.amount_yi! - a.amount_yi!)[0])
const up = computed(() => all.value.filter(row => (row.avg_pct ?? 0) > 0).length)
const down = computed(() => all.value.filter(row => (row.avg_pct ?? 0) < 0).length)
const pct = (value: number | null | undefined) => value == null ? '—' : `${value > 0 ? '+' : ''}${value.toFixed(2)}%`
const number = (value: number | null | undefined, digits = 2) => value == null ? '—' : value.toLocaleString('zh-CN', { maximumFractionDigits: digits, minimumFractionDigits: digits })
const direction = (value: number | null | undefined) => value == null || value === 0 ? 'neutral' : value > 0 ? 'rise' : 'fall'
const statusLabel = { valid: '', missing: '缺少当日日线', no_trade: '当日无成交', invalid: '行情字段不完整' }
function tileColor(row: IndustryStat) {
  if (row.avg_pct == null) return '#25272a'
  const strength = Math.min(Math.abs(row.avg_pct) / 6, 1)
  const rgb = row.avg_pct > 0 ? [105 + 75 * strength, 39 + 10 * strength, 43 + 14 * strength] : row.avg_pct < 0 ? [19, 66 + 62 * strength, 57 + 34 * strength] : [52, 52, 52]
  return `rgb(${rgb.map(Math.round).join(',')})`
}
function choose(industry: string) {
  if (industry === OTHER_INDUSTRY) {
    view.value = 'ranking'
    search.value = ''
    return
  }
  selected.value = industry; page.value = 0; actionMessage.value = ''
}

async function refresh() {
  const id = ++overviewRequest
  overviewController?.abort(); membersController?.abort(); ++memberRequest
  overviewController = new AbortController()
  loading.value = true; error.value = ''; memberError.value = ''; members.value = null
  // Existing content stays visible (dimmed) while the new section loads;
  // only a failure clears it, so users never lose context on transient errors.
  try {
    const result = await api.industryOverview(overviewController.signal)
    if (id !== overviewRequest) return
    data.value = result
    if (!result.industries.some(item => item.industry === selected.value)) selected.value = result.industries[0]?.industry ?? ''
    page.value = 0
  } catch (err) {
    if (id === overviewRequest && !(err instanceof DOMException && err.name === 'AbortError')) {
      data.value = null
      error.value = err instanceof Error ? err.message : '行业数据加载失败'
    }
  } finally { if (id === overviewRequest) loading.value = false }
}

async function loadMembers() {
  const id = ++memberRequest
  membersController?.abort()
  membersController = new AbortController()
  members.value = null; memberError.value = ''
  if (!selected.value || !data.value?.as_of) { memberLoading.value = false; return }
  memberLoading.value = true
  try {
    const result = await api.industryMembers({ industry: selected.value, snapshot_id: data.value.snapshot_id, sort_by: sortBy.value, order: order.value, offset: page.value * 30, limit: 30 }, membersController.signal)
    if (id === memberRequest) members.value = result
  } catch (err) {
    if (id !== memberRequest || (err instanceof DOMException && err.name === 'AbortError')) return
    memberError.value = err instanceof ApiError && err.status === 409 ? '市场数据已更新，请点击顶部“刷新数据”，使概览与成员保持一致。' : err instanceof Error ? err.message : '成分股加载失败'
  } finally { if (id === memberRequest) memberLoading.value = false }
}

async function addWatchlist(item: IndustryMember) {
  if (!canWrite.value || adding.value.has(item.symbol) || added.value.has(item.symbol)) return
  adding.value.add(item.symbol); actionMessage.value = ''
  try {
    const saved = await api.addWatchlist({ symbol: item.symbol, group_name: '行业观察', reason: `${item.industry}行业观察，日线截面 ${item.trade_date}` })
    if (disposed) return
    added.value.add(item.symbol); emit('watchlistAdded', saved)
    actionMessage.value = `${item.name} 已加入“行业观察”自选组`
  } catch (err) { actionMessage.value = err instanceof Error ? err.message : '加入自选失败' }
  finally { adding.value.delete(item.symbol) }
}
watch([selected, page, sortBy, order, () => data.value?.snapshot_id], () => void loadMembers())
onMounted(() => {
  if (typeof window !== 'undefined' && window.innerWidth < 980) view.value = 'ranking'
  void refresh()
})
onBeforeUnmount(() => { disposed = true; ++overviewRequest; ++memberRequest; overviewController?.abort(); membersController?.abort() })
</script>

<template>
  <section class="ia-page" aria-label="行业分析" data-testid="industry-analysis">
    <header class="ia-heading">
      <p class="ia-heading-note">行业强弱、成交活跃与成分股，基于最近同步日线快照。</p>
      <div class="ia-heading-actions"><span class="ia-date" :class="{ stale }">日线 · {{ data?.as_of || '尚无数据' }}</span><span v-if="stale" class="ia-stale-badge">早于最近收盘日 {{ data?.expected_date }}</span><button type="button" :disabled="loading" @click="refresh">{{ loading ? '读取中…' : '↻ 刷新数据' }}</button></div>
    </header>
    <div v-if="error" class="ia-message ia-error" role="alert">{{ error }} <button type="button" @click="refresh">重试</button></div>
    <div v-if="loading && !data" class="ia-loading" role="status"><span class="ia-spinner" /> 正在读取本地行业行情…</div>
    <template v-else-if="data">
      <div v-if="loading" class="ia-refreshing" role="status">正在刷新最新截面…</div>
      <div class="ia-content" :class="{ 'ia-dim': loading }" :aria-busy="loading">
      <div class="ia-meta"><span>{{ data.classification }}</span><span>有效统计 {{ data.coverage.valid_count.toLocaleString() }} 只</span><button type="button" @click="emit('openScreener')">去同步数据</button></div>
      <div v-if="!data.as_of || !all.length" class="ia-empty"><div class="ia-empty-mark">▦</div><h3>{{ !data.as_of ? '先准备一份市场日线' : '还没有可用的行业分类' }}</h3><p>到 AI 选股页同步全市场行情和股票目录，再回来查看行业分布。</p><button type="button" class="ui-btn ui-btn--primary" @click="emit('openScreener')">去同步数据</button></div>
      <template v-else>
        <div class="ia-metrics">
          <article><span>行业分布</span><strong>{{ all.length }}<small> 个行业</small></strong><p><b class="rise">{{ up }} 上涨</b><i> / </i><b class="fall">{{ down }} 下跌</b></p></article>
          <article><span>等权涨幅领先</span><strong>{{ leading?.industry || '—' }}</strong><p :class="direction(leading?.avg_pct)">{{ pct(leading?.avg_pct) }}<small> 成分股均值</small></p></article>
          <article><span>成交最活跃</span><strong>{{ active?.industry || '—' }}</strong><p>{{ number(active?.amount_yi) }}<small> 亿元 · 非资金净流入</small></p></article>
          <article><span>数据覆盖</span><strong>{{ (data.coverage.bar_ratio * 100).toFixed(1) }}<small>%</small></strong><p>覆盖 {{ data.coverage.bar_count.toLocaleString() }} / {{ data.coverage.stock_count.toLocaleString() }} 只<i> · </i>缺 {{ data.coverage.missing_count }} · 无成交 {{ data.coverage.no_trade_count }}</p></article>
        </div>
        <div class="ia-workspace">
          <section class="ia-panel ia-overview" aria-label="行业分布">
            <div class="ia-panel-head"><div><h3>行业分布</h3></div><div class="ia-segment" aria-label="行业展示方式"><button type="button" :aria-pressed="view === 'heatmap'" @click="view = 'heatmap'">热力图</button><button type="button" :aria-pressed="view === 'ranking'" @click="view = 'ranking'">强弱榜</button></div></div>
            <div class="ia-toolbar"><input v-model="search" type="search" placeholder="搜索行业" aria-label="搜索行业" /><select v-if="view === 'heatmap'" v-model="metric" aria-label="热力图面积"><option value="count">面积 · 成分股数</option><option value="amount">面积 · 成交额</option></select><select v-else v-model="rankBy" aria-label="行业排序"><option value="avg_pct">等权涨幅</option><option value="amount_yi">成交额</option><option value="up_ratio">上涨占比</option></select></div>
            <div v-if="!visible.length" class="ia-inline-empty">没有匹配的行业 <button type="button" @click="search = ''">清除搜索</button></div>
            <template v-else-if="view === 'heatmap'">
              <svg v-if="tiles.length" class="ia-treemap" viewBox="0 0 1000 660" role="group" aria-label="行业热力图，点击行业查看成分股">
                <g v-for="tile in tiles" :key="tile.industry.industry" role="button" tabindex="0" :aria-pressed="selected === tile.industry.industry" :aria-label="`${tile.industry.industry}，等权涨跌幅 ${pct(tile.industry.avg_pct)}，${tile.industry.stock_count} 只股票`" @click="choose(tile.industry.industry)" @keydown.enter="choose(tile.industry.industry)" @keydown.space.prevent="choose(tile.industry.industry)" @mouseenter="hoverStat = tile.industry" @mouseleave="hoverStat = null" @focus="hoverStat = tile.industry" @blur="hoverStat = null">
                  <title>{{ tile.industry.industry }} · {{ pct(tile.industry.avg_pct) }} · 有效 {{ tile.industry.valid_count }}/{{ tile.industry.stock_count }} 只{{ tile.industry.industry === OTHER_INDUSTRY ? '（点击查看明细榜单）' : '' }}</title>
                  <rect :x="tile.x + 2" :y="tile.y + 2" :width="Math.max(0, tile.width - 4)" :height="Math.max(0, tile.height - 4)" :fill="tileColor(tile.industry)" :class="{ selected: selected === tile.industry.industry }" rx="4" />
                  <template v-if="tile.width > 85 && tile.height > 64"><text :x="tile.x + tile.width / 2" :y="tile.y + tile.height / 2 - 5" :font-size="Math.min(25, (tile.width - 12) / Math.max(3, tile.industry.industry.length))">{{ tile.industry.industry }}</text><text class="ia-tile-pct" :style="{ fontSize: `${Math.min(23, (tile.width - 14) / 4.8)}px` }" :x="tile.x + tile.width / 2" :y="tile.y + tile.height / 2 + 23">{{ pct(tile.industry.avg_pct) }}</text></template>
                  <text v-else-if="tile.width > 34 && tile.height > 22" :x="tile.x + tile.width / 2" :y="tile.y + tile.height / 2 + 4" class="ia-tile-dot">{{ tile.industry.industry === OTHER_INDUSTRY ? '其他' : '·' }}</text>
                </g>
              </svg>
              <p v-else class="ia-inline-empty">当前行业暂无有效成交额，请切换为成分股数。</p>
              <div class="ia-preview" aria-live="polite" data-testid="industry-hover-preview">
                <template v-if="previewStat">
                  <strong>{{ previewStat.industry }}</strong>
                  <b :class="direction(previewStat.avg_pct)">{{ pct(previewStat.avg_pct) }}</b>
                  <span>上涨 {{ previewStat.up_count }} · 下跌 {{ previewStat.down_count }} · 平盘 {{ previewStat.flat_count }}</span>
                  <span v-if="previewStat.amount_yi != null">成交 {{ number(previewStat.amount_yi, 1) }} 亿</span>
                  <span v-if="previewStat.leader">龙头 {{ previewStat.leader.name }} {{ pct(previewStat.leader.pct_chg) }}</span>
                </template>
                <span v-else class="ia-preview-hint">悬停行业块快速预览，点击固定选择</span>
              </div>
              <div class="ia-legend"><span class="fall">下跌</span><i /><span class="rise">上涨</span><small>颜色：成分股等权涨幅</small></div>
              <p class="ia-note">{{ metric === 'count' ? '面积按成分股数量分配。' : '面积按已覆盖成分股成交额分配，缺成交额行业不绘制。' }}灰色表示缺少有效涨幅；尾部小行业已聚合为“{{ OTHER_INDUSTRY }}”，点击可展开榜单。涨跌幅为成分股等权均值，并非官方行业指数。</p>
            </template>
            <div v-else class="ia-rank-list">
              <div class="ia-rank-label"><span>行业 / 有效样本</span><span>等权涨幅</span><span>上涨占比</span><span>成交额 / 亿</span></div>
              <button v-for="row in visible" :key="row.industry" type="button" :class="{ 'is-selected': selected === row.industry }" :aria-pressed="selected === row.industry" @click="choose(row.industry)"><span><b>{{ row.industry }}</b><small>{{ row.valid_count }}/{{ row.stock_count }} 只</small></span><b :class="direction(row.avg_pct)">{{ pct(row.avg_pct) }}</b><span>{{ row.up_ratio == null ? '—' : `${(row.up_ratio * 100).toFixed(0)}%` }}</span><span>{{ number(row.amount_yi, 1) }}</span></button>
            </div>
            <details class="ia-method"><summary>统计口径与数据说明</summary><p>按 {{ data.as_of }} 的 TuShare 日线与同日每日指标计算。涨跌幅为成分股等权均值，并非官方行业指数。无成交、无行情和字段不完整的股票不计入涨跌统计。</p><p>分类为当前股票目录；只接受单位明确的 TuShare 日线。日线覆盖达到 90% 时优先选用该日期，不代表数据绝对完整。没有行业分类 {{ data.coverage.unclassified_count }} 只，行情字段不完整 {{ data.coverage.invalid_count }} 只。</p></details>
          </section>
          <section class="ia-panel ia-detail" aria-label="行业成分股" :aria-busy="memberLoading">
            <div class="ia-panel-head"><div><h3>{{ current?.industry || '选择一个行业' }}</h3><span class="ia-count">{{ current?.stock_count ?? 0 }} 只</span></div><button v-if="current" type="button" class="ui-btn ui-btn--primary" @click="emit('screenIndustry', current.industry)">去选股</button></div>
            <template v-if="current">
              <div class="ia-detail-summary"><div><small>等权涨跌幅</small><strong :class="direction(current.avg_pct)">{{ pct(current.avg_pct) }}</strong></div><div><small>中位涨跌幅</small><strong :class="direction(current.median_pct)">{{ pct(current.median_pct) }}</strong></div><div><small>上涨 / 下跌 / 平盘</small><strong><span class="rise">{{ current.up_count }}</span> / <span class="fall">{{ current.down_count }}</span> / {{ current.flat_count }}</strong></div></div>
              <div class="ia-breadth" aria-hidden="true" :title="`上涨 ${current.up_count} · 平盘 ${current.flat_count} · 下跌 ${current.down_count}`"><span class="ia-up-bar" :style="{ width: `${current.valid_count ? current.up_count / current.valid_count * 100 : 0}%` }" /><span class="ia-flat-bar" :style="{ width: `${current.valid_count ? current.flat_count / current.valid_count * 100 : 0}%` }" /><span class="ia-down-bar" :style="{ width: `${current.valid_count ? current.down_count / current.valid_count * 100 : 0}%` }" /></div>
              <p class="ia-note">有效 {{ current.valid_count }}/{{ current.stock_count }} · 缺日线 {{ current.missing_count }} · 无成交 {{ current.no_trade_count }} · 成交额覆盖 {{ current.amount_count }} 只</p>
              <div class="ia-toolbar ia-member-toolbar"><h4>成分股排名</h4><select v-model="sortBy" aria-label="成分股排序" @change="page = 0"><option value="pct_chg">涨跌幅</option><option value="amount_yi">成交额</option><option value="turnover_rate">换手率</option><option value="volume_ratio">量比</option><option value="pe_ttm">PE（TTM）</option><option value="total_mv_yi">市值</option></select><button type="button" :aria-label="order === 'desc' ? '切换为升序' : '切换为降序'" @click="order = order === 'desc' ? 'asc' : 'desc'; page = 0">{{ order === 'desc' ? '↓ 降序' : '↑ 升序' }}</button></div>
              <p v-if="actionMessage" class="ia-action-message" role="status">{{ actionMessage }}</p>
              <p v-if="memberError" class="ia-message ia-error" role="alert">{{ memberError }} <button type="button" @click="refresh">刷新数据</button></p>
              <div v-else-if="memberLoading" class="ia-inline-empty" role="status">正在读取 {{ current.industry }} 成分股…</div>
              <div v-else-if="members" class="ia-table-wrap">
                <table><thead><tr><th>股票</th><th>涨跌幅</th><th>成交额<small>亿元</small></th><th>换手率</th><th>量比</th><th>PE<small>TTM</small></th><th>总市值<small>亿元</small></th><th>研究</th></tr></thead><tbody><tr v-for="item in members.items" :key="item.symbol"><td><strong>{{ item.name }}</strong><small>{{ item.symbol }}</small><em v-if="item.status !== 'valid'">{{ statusLabel[item.status] }}</em></td><td :class="direction(item.pct_chg)">{{ pct(item.pct_chg) }}</td><td>{{ number(item.amount_yi) }}</td><td>{{ item.turnover_rate == null ? '—' : `${number(item.turnover_rate)}%` }}</td><td>{{ number(item.volume_ratio) }}</td><td>{{ number(item.pe_ttm, 1) }}</td><td>{{ number(item.total_mv_yi, 1) }}</td><td><div class="ia-row-actions"><button v-if="canAsk" type="button" :aria-label="`问股 ${item.name}`" @click="emit('askStock', item)">问股</button><button v-if="canWrite" type="button" :disabled="adding.has(item.symbol) || added.has(item.symbol)" :aria-label="`加入自选 ${item.name}`" @click="addWatchlist(item)">{{ adding.has(item.symbol) ? '添加中' : added.has(item.symbol) ? '已加入' : '+ 自选' }}</button></div></td></tr></tbody></table>
                <p v-if="!members.items.length" class="ia-inline-empty">当前行业暂无成分股。</p>
              </div>
              <div v-if="members && members.total > 30" class="ia-pagination"><span>第 {{ page + 1 }} / {{ Math.ceil(members.total / 30) }} 页 · {{ members.total }} 只</span><button type="button" :disabled="page === 0" @click="page--">上一页</button><button type="button" :disabled="(page + 1) * 30 >= members.total" @click="page++">下一页</button></div>
            </template>
          </section>
        </div>
        <footer class="ia-footer">{{ data.source }}<span>日线观察 · 个股排名用于研究比较，不代表未来收益</span></footer>
      </template>
      </div>
    </template>
  </section>
</template>

<style scoped>
.ia-page { color: var(--text); padding: 8px 0 24px; max-width: 1800px; margin: auto; font-variant-numeric: tabular-nums; }
.ia-page button,.ia-page input,.ia-page select { border: 1px solid var(--line); border-radius: 4px; color: var(--text); background: var(--surface-2); padding: 8px 11px; font-size: 12px; }
.ia-page button:hover:not(:disabled) { border-color: var(--gold); color: var(--gold-2); }
.ia-page button:disabled { cursor: default; opacity: .45; }
.ia-page :is(button,input,select,summary):focus-visible { outline: 2px solid var(--gold); outline-offset: 3px; }
.ia-heading { display: flex; align-items: center; justify-content: space-between; gap: 20px; margin-bottom: 18px; }
.ia-heading-note { color: var(--muted); font-size: 12px; margin: 0; }
.ia-heading-actions { display: flex; align-items: center; gap: 12px; flex-shrink: 0; }
.ia-date { font-size: 12px; color: var(--gold-2); }
.ia-date.stale { color: var(--warning); }
.ia-stale-badge { border: 1px solid var(--warning); border-radius: 4px; padding: 2px 7px; color: var(--warning); font-size: 12px; white-space: nowrap; }
.ia-content { transition: opacity .15s ease; }
.ia-content.ia-dim { opacity: .45; pointer-events: none; }
.ia-refreshing { border-left: 2px solid var(--gold); background: #d5b46a0d; padding: 6px 14px; color: var(--gold-2); font-size: 12px; margin-bottom: 8px; }
.ia-preview { display: flex; align-items: center; flex-wrap: wrap; gap: 6px 16px; min-height: 34px; padding: 7px 18px; border-top: 1px solid var(--line); color: var(--muted); font-size: 12px; }
.ia-preview strong { color: var(--text); font-weight: 500; }
.ia-preview b { font-weight: 600; font-variant-numeric: tabular-nums; }
.ia-preview-hint { color: var(--muted-2); font-size: 12px; }
.ia-tile-dot { text-anchor: middle; fill: #d8d2c2; font-size: 13px; pointer-events: none; }
.ia-meta { display: flex; align-items: center; flex-wrap: wrap; gap: 10px 22px; border-block: 1px solid var(--line); padding: 10px 0; color: var(--muted); font-size: 12px; }
.ia-meta button { margin-left: auto; border: none; background: none; padding: 0; color: var(--gold); }
.ia-message { border-left: 2px solid var(--gold); background: #d5b46a09; padding: 9px 14px; font-size: 12px; color: #cbb988; margin: 14px 0; line-height: 1.8; }
.ia-message p { margin: 0; }.ia-error { color: var(--danger); border-color: var(--danger); }.ia-message button { margin-left: 10px; }
.ia-metrics { display: grid; grid-template-columns: repeat(4,minmax(0,1fr)); padding: 24px 0; }
.ia-metrics article { padding: 0 22px; border-left: 1px solid var(--line); }.ia-metrics article:first-child { padding-left: 0; border: 0; }
.ia-metrics article > span { color: var(--muted); font-size: 12px; }.ia-metrics strong { display: block; font-size: 24px; margin: 10px 0 8px; font-weight: 500; }
.ia-metrics small { font-size: 12px; color: var(--muted); font-weight: 400; }.ia-metrics p { font-size: 12px; margin: 0; }.ia-metrics b { font-weight: 400; }.ia-metrics i { font-style: normal; color: var(--muted-2); }
.ia-workspace { display: grid; grid-template-columns: minmax(330px, .9fr) minmax(450px, 1.25fr); gap: 18px; align-items: start; }
.ia-panel { background: var(--surface); border: 1px solid var(--line); border-radius: 8px; overflow: hidden; min-width: 0; }
.ia-panel-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; min-height: 64px; padding: 15px 18px; border-bottom: 1px solid var(--line); }
.ia-panel-head > div { display: flex; align-items: center; gap: 10px; min-width: 0; }.ia-panel-head h3 { font-size: 16px; margin: 0; font-weight: 500; }.ia-count { font-size: 12px; color: var(--muted); white-space: nowrap; }

.ia-segment { border: 1px solid var(--line); border-radius: 4px; padding: 2px; gap: 0 !important; }.ia-segment button { border: none; background: none; padding: 5px 8px; }.ia-segment button[aria-pressed=true] { color: var(--gold-2); background: #d5b46a1a; }
.ia-toolbar { display: flex; gap: 8px; padding: 16px; }.ia-toolbar input { width: 0; min-width: 90px; flex: 1; background: var(--bg-2); }.ia-toolbar select { max-width: 160px; }
.ia-treemap { display: block; width: calc(100% - 28px); margin: 0 14px; }.ia-treemap g { cursor: pointer; outline: none; }.ia-treemap g:hover rect,.ia-treemap g:focus-visible rect,.ia-treemap rect.selected { stroke: var(--gold-2); stroke-width: 3; }.ia-treemap text { text-anchor: middle; fill: #f6f1e6; pointer-events: none; }.ia-tile-pct { font-size: 24px; }
.ia-legend { display: flex; gap: 8px; align-items: center; padding: 12px 18px 0; font-size: 12px; }.ia-legend i { width: 95px; height: 5px; background: linear-gradient(90deg,#138066,#303032,#ba3d46); border-radius: 4px; }.ia-legend small { margin-left: auto; color: var(--muted); font-size: 12px; }
.ia-note { color: var(--muted); margin: 10px 18px 16px; line-height: 1.8; font-size: 12px; }
.ia-rank-list { max-height: 610px; overflow: auto; padding: 0 10px 14px; }.ia-rank-list button,.ia-rank-label { display: grid; width: 100%; grid-template-columns: 1.4fr 1fr .85fr 1fr; gap: 8px; align-items: center; text-align: right; }.ia-rank-label { font-size: 12px; color: var(--muted); padding: 4px 10px 12px; }.ia-rank-list button { border: 0; border-top: 1px solid var(--line); border-radius: 0; padding: 13px 10px; background: none; }.ia-rank-list button.is-selected { background: #d5b46a12; box-shadow: inset 2px 0 var(--gold); }.ia-rank-list :is(button,.ia-rank-label) > :first-child { text-align: left; }.ia-rank-list b { font-weight: 500; }.ia-rank-list small { display: block; font-size: 12px; margin-top: 4px; color: var(--muted); }
.ia-method { padding: 14px 18px; border-top: 1px solid var(--line); color: var(--muted); font-size: 12px; line-height: 1.8; }.ia-method summary { cursor: pointer; }.ia-method p { margin: 10px 0 0; }
.ia-detail-summary { display: grid; grid-template-columns: 1fr 1fr 1.3fr; gap: 12px; padding: 22px 18px 14px; }.ia-detail-summary small { display: block; color: var(--muted); font-size: 12px; margin-bottom: 10px; }.ia-detail-summary strong { font-size: 20px; font-weight: 500; white-space: nowrap; }
.ia-breadth { display: flex; height: 6px; margin: 0 18px; background: var(--surface-3); }.ia-up-bar { background: #e96467; }.ia-down-bar { background: #37ad86; }.ia-flat-bar { background: #666; }
.ia-member-toolbar { border-top: 1px solid var(--line); align-items: center; padding-block: 12px; }.ia-member-toolbar h4 { margin: 0 auto 0 0; font-size: 12px; font-weight: 500; }.ia-member-toolbar select { width: 95px; }
.ia-table-wrap { overflow: auto; max-height: 660px; }.ia-table-wrap table { width: 100%; border-collapse: collapse; font-size: 12px; white-space: nowrap; }.ia-table-wrap th { text-align: right; font-size: 12px; font-weight: 400; color: var(--muted); padding: 10px 9px; background: var(--bg-2); position: sticky; top: 0; }.ia-table-wrap :is(th,td):first-child { text-align: left; padding-left: 18px; }.ia-table-wrap td { padding: 13px 9px; text-align: right; border-top: 1px solid var(--line); }.ia-table-wrap td strong { font-weight: 500; }.ia-table-wrap td small,.ia-table-wrap th small { display: block; font-size: 12px; color: var(--muted-2); margin-top: 4px; }.ia-table-wrap td em { display: block; font-style: normal; font-size: 12px; color: var(--warning); margin-top: 5px; }.ia-table-wrap tr:hover td { background: #d5b46a06; }
.ia-row-actions { display: flex; flex-direction: column; gap: 5px; }.ia-row-actions button { border: 0; padding: 0 4px; color: var(--gold); background: none; font-size: 12px; }
.ia-action-message { padding: 0 18px; color: var(--gold-2); font-size: 12px; }.ia-pagination { display: flex; align-items: center; gap: 8px; padding: 12px 18px; border-top: 1px solid var(--line); }.ia-pagination span { color: var(--muted); margin-right: auto; font-size: 12px; }
.ia-footer { display: flex; justify-content: space-between; gap: 12px; color: var(--muted-2); font-size: 12px; margin-top: 16px; }.rise { color: #f0787c; }.fall { color: #4ac69a; }.neutral { color: var(--muted); }
.ia-loading,.ia-empty { padding: 90px 20px; text-align: center; color: var(--muted); }.ia-empty h3 { color: var(--text); font-weight: 500; }.ia-empty p { font-size: 12px; margin-bottom: 22px; }.ia-empty-mark { font-size: 32px; color: var(--gold-dim); }.ia-inline-empty { padding: 45px 16px; text-align: center; color: var(--muted); font-size: 12px; }.ia-spinner { display: inline-block; width: 14px; height: 14px; border: 2px solid var(--line); border-top-color: var(--gold); border-radius: 50%; vertical-align: middle; margin-right: 8px; animation: ia-spin 1s linear infinite; }@keyframes ia-spin { to { transform: rotate(360deg); } }
@media (max-width: 1200px) { .ia-workspace { grid-template-columns: 1fr; }.ia-treemap { max-height: 540px; }.ia-metrics article { padding-inline: 14px; }.ia-metrics strong { font-size: 24px; } }
@media (max-width: 680px) { .ia-heading { align-items: start; flex-direction: column; gap: 16px; }.ia-metrics { grid-template-columns: repeat(2,minmax(0,1fr)); gap: 20px 0; }.ia-metrics article:nth-child(3) { border: 0; padding-left: 0; }.ia-meta { gap: 10px; }.ia-meta button { margin-left: 0; }.ia-panel-head { padding-inline: 12px; }.ia-detail-summary { gap: 7px; padding-inline: 12px; }.ia-detail-summary strong { font-size: 16px; }.ia-footer { flex-direction: column; }.ia-rank-list { max-height: 440px; } }
@media (prefers-reduced-motion: reduce) { .ia-spinner { animation: none; } }
</style>

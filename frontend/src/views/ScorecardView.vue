<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { api } from '../api'
import type { ScorecardGroup, ScorecardItem, ScorecardStats, ScorecardSummary, UserPayload } from '../types'

const props = defineProps<{ user: UserPayload }>()

const summary = ref<ScorecardSummary | null>(null)
const horizon = ref(10)
const accountId = ref<number | null>(null)
const loading = ref(false)
const refreshing = ref(false)
const error = ref('')
const notice = ref('')
let sequence = 0

const canRefresh = computed(
  () => props.user.permissions.includes('all') || props.user.permissions.includes('schedule:manage')
)
const horizons = computed(() => summary.value?.horizons ?? [5, 10, 20])
const accounts = computed(() => summary.value?.accounts ?? [])
const showAccounts = computed(() => accounts.value.length > 1)
const overall = computed(() => summary.value?.overall ?? null)
const groups = computed(() => {
  const data = summary.value
  if (!data) return []
  return [
    ...(accountId.value === null && data.by_account.length > 1
      ? [{ title: '按账户', hint: '名下每个账户各自的成绩，账户之间不合并', rows: data.by_account }]
      : []),
    { title: '按建议类型', hint: '买、卖、继续拿着，哪一类更准', rows: data.by_action },
    { title: '按哪份计划', hint: '早盘、午盘、晚间计划各自的成绩', rows: data.by_source },
    { title: '按模型看法', hint: '数字人的建议和量化模型一致或相反时，谁对得多', rows: data.by_model },
    { title: '按有没有照做', hint: '照做的和没照做的建议，结果有没有差别', rows: data.by_followed },
    { title: '按股数', hint: '数字人改了代码建议的股数时，建议本身准不准', rows: data.by_size },
  ].filter((group) => group.rows.length)
})

const headline = computed(() => {
  const data = summary.value
  const stats = overall.value
  if (!data || !stats) return ''
  if (!stats.total) {
    return data.pending
      ? `已经记下 ${data.pending} 条建议，都还没到 ${data.horizon} 个交易日，到期后这里会出结果。`
      : '还没有记到建议。出过操作计划之后，每天收盘会自动登记。'
  }
  const rate = stats.right_rate === null ? '' : `，分出对错的里面对了 ${percent(stats.right_rate, 0)}`
  const worth = stats.avg_edge === null ? '' : `，照做平均比全市场${stats.avg_edge >= 0 ? '多赚' : '少赚'} ${percent(Math.abs(stats.avg_edge), 1)}`
  const caveat = stats.enough ? '' : `。样本不到 ${data.min_samples} 条，现在的数字还不能下结论`
  return `${data.horizon} 个交易日后有结果的建议 ${stats.total} 条：对 ${stats.right}、错 ${stats.wrong}、差不多 ${stats.flat}${rate}${worth}${caveat}。`
})

function percent(value: number | null, digits = 1): string {
  return value === null ? '—' : `${(value * 100).toFixed(digits)}%`
}

function signed(value: number | null): string {
  if (value === null) return '—'
  return `${value > 0 ? '+' : ''}${(value * 100).toFixed(1)}%`
}

function tone(value: number | null): string {
  if (value === null || value === 0) return ''
  return value > 0 ? 'pnl-up' : 'pnl-down'
}

function day(value: string | null): string {
  return value && value.length === 8 ? `${value.slice(4, 6)}-${value.slice(6)}` : '—'
}

function verdictLabel(item: ScorecardItem): { text: string; kind: string } {
  if (item.verdict === 'right') return { text: '对', kind: 'ok' }
  if (item.verdict === 'wrong') return { text: '错', kind: 'danger' }
  if (item.verdict === 'flat') return { text: '差不多', kind: 'muted' }
  if (item.verdict === 'no_data') return { text: '没有行情', kind: 'muted' }
  return { text: '没到期', kind: 'warning' }
}

function modelLabel(view: ScorecardItem['model_view']): string {
  return { agree: '一致', disagree: '相反', neutral: '中性', unknown: '—' }[view]
}

function groupNote(row: ScorecardStats | ScorecardGroup): string {
  return row.enough ? '' : '样本少'
}

async function load() {
  const current = ++sequence
  loading.value = true
  error.value = ''
  try {
    const data = await api.scorecard(horizon.value, accountId.value)
    if (current === sequence) summary.value = data
  } catch (exc) {
    if (current === sequence) error.value = exc instanceof Error ? exc.message : '成绩单加载失败'
  } finally {
    if (current === sequence) loading.value = false
  }
}

function selectAccount(event: Event) {
  const value = (event.target as HTMLSelectElement).value
  accountId.value = value === '' ? null : Number(value)
  void load()
}

function selectHorizon(value: number) {
  if (horizon.value === value) return
  horizon.value = value
  void load()
}

async function onHorizonKeydown(event: KeyboardEvent) {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
  event.preventDefault()
  const keys = horizons.value
  const index = keys.indexOf(horizon.value)
  const next = event.key === 'Home'
    ? 0
    : event.key === 'End'
      ? keys.length - 1
      : (index + (event.key === 'ArrowRight' ? 1 : -1) + keys.length) % keys.length
  selectHorizon(keys[next])
  await nextTick()
  document.getElementById(`scorecard-tab-${keys[next]}`)?.focus()
}

async function refresh() {
  refreshing.value = true
  error.value = ''
  notice.value = ''
  try {
    notice.value = (await api.scorecardRefresh()).message
    await load()
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : '更新失败'
  } finally {
    refreshing.value = false
  }
}

onMounted(load)
// 换了登录账号：上一位用户的成绩单和账户筛选都不能留在页面上
watch(
  () => props.user.id,
  () => {
    summary.value = null
    accountId.value = null
    notice.value = ''
    void load()
  }
)
</script>

<template>
  <div class="score-page">
    <div class="score-command">
      <div
        class="ui-tabs"
        role="tablist"
        aria-label="过多少个交易日看结果"
        @keydown="onHorizonKeydown"
      >
        <button
          v-for="value in horizons"
          :id="`scorecard-tab-${value}`"
          :key="value"
          role="tab"
          type="button"
          :aria-selected="horizon === value"
          :tabindex="horizon === value ? 0 : -1"
          @click="selectHorizon(value)"
        >
          {{ value }} 个交易日后
        </button>
      </div>
      <label v-if="showAccounts" class="score-account">
        <span>账户</span>
        <select :value="accountId === null ? '' : String(accountId)" @change="selectAccount">
          <option value="">全部账户</option>
          <option v-for="account in accounts" :key="account.account_id" :value="String(account.account_id)">
            {{ account.label }}
          </option>
        </select>
      </label>
      <button
        v-if="canRefresh"
        class="ui-btn ui-btn--ghost ui-btn--sm"
        type="button"
        :disabled="refreshing"
        @click="refresh"
      >
        {{ refreshing ? '更新中…' : '现在更新' }}
      </button>
    </div>

    <p v-if="error" class="score-notice score-notice--danger" role="alert">{{ error }}</p>
    <p v-else-if="notice" class="score-notice score-notice--ok" role="status">{{ notice }}</p>

    <section v-if="summary && overall" class="ui-panel score-conclusion">
      <p class="score-headline">{{ headline }}</p>
      <div v-if="overall.total" class="ui-kpi">
        <div>
          <span>有结果的建议</span>
          <strong>{{ overall.total }}</strong>
          <small>还没到期 {{ summary.pending }} 条</small>
        </div>
        <div>
          <span>对的比例</span>
          <strong>{{ percent(overall.right_rate, 0) }}</strong>
          <small>对 {{ overall.right }} / 错 {{ overall.wrong }}</small>
        </div>
        <div>
          <span>照做平均多赚</span>
          <strong :class="tone(overall.avg_edge)">{{ signed(overall.avg_edge) }}</strong>
          <small>相对全市场平均</small>
        </div>
        <div>
          <span>差不多</span>
          <strong>{{ overall.flat }}</strong>
          <small>差距不到 {{ percent(summary.dead_band, 0) }}</small>
        </div>
      </div>
      <p class="score-rule">
        怎么算：从建议生效那天收盘起，过 {{ summary.horizon }} 个交易日，看这只股票比全市场平均多涨还是少涨。建议买或继续拿着的，跑赢算对；建议卖的，跑输算对。差距不到
        {{ percent(summary.dead_band, 0) }} 算差不多。同一个账户同一天同一只股票只算一次，账户之间不合并。每一类不到 {{ summary.min_samples }} 条先不下结论。
      </p>
    </section>

    <div v-if="summary && overall?.total" class="score-groups">
      <section v-for="group in groups" :key="group.title" class="ui-panel">
        <header class="ui-panel-head">
          <div>
            <h3>{{ group.title }}</h3>
            <p>{{ group.hint }}</p>
          </div>
        </header>
        <div class="ui-table-wrap">
          <table class="ui-table">
            <thead>
              <tr>
                <th>分类</th>
                <th class="num">条数</th>
                <th class="num">对</th>
                <th class="num">错</th>
                <th class="num">对的比例</th>
                <th class="num">平均多赚</th>
                <th />
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in group.rows" :key="row.key">
                <td>{{ row.label }}</td>
                <td class="num">{{ row.total }}</td>
                <td class="num">{{ row.right }}</td>
                <td class="num">{{ row.wrong }}</td>
                <td class="num">{{ percent(row.right_rate, 0) }}</td>
                <td class="num" :class="tone(row.avg_edge)">{{ signed(row.avg_edge) }}</td>
                <td>
                  <span v-if="groupNote(row)" class="ui-pill ui-pill--muted">{{ groupNote(row) }}</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </div>

    <section v-if="summary?.recent.length" class="ui-panel">
      <header class="ui-panel-head">
        <div>
          <h3>最近的建议</h3>
          <p>最新的 {{ summary.recent.length }} 条，没到期的也在里面</p>
        </div>
      </header>
      <div class="ui-table-wrap">
        <table class="ui-table">
          <thead>
            <tr>
              <th>建议日</th>
              <th v-if="showAccounts">账户</th>
              <th>股票</th>
              <th>建议</th>
              <th>来自</th>
              <th>模型</th>
              <th>照做</th>
              <th>到期日</th>
              <th class="num">这只股票</th>
              <th class="num">全市场</th>
              <th class="num">差距</th>
              <th>结果</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in summary.recent" :key="`${item.advice_date}-${item.account_id}-${item.symbol}-${item.action}`">
              <td class="num-left">{{ day(item.advice_date) }}</td>
              <td v-if="showAccounts">{{ item.account_label }}</td>
              <td>{{ item.name || item.symbol }}<small class="score-code">{{ item.symbol.slice(0, 6) }}</small></td>
              <td>{{ item.action_label }}</td>
              <td>{{ item.plan_source }}</td>
              <td>{{ modelLabel(item.model_view) }}</td>
              <td>{{ item.followed ? '是' : '否' }}</td>
              <td class="num-left">{{ day(item.due_date) }}</td>
              <td class="num" :class="tone(item.stock_return)">{{ signed(item.stock_return) }}</td>
              <td class="num" :class="tone(item.market_return)">{{ signed(item.market_return) }}</td>
              <td class="num" :class="tone(item.excess_return)">{{ signed(item.excess_return) }}</td>
              <td><span class="ui-pill" :class="`ui-pill--${verdictLabel(item).kind}`">{{ verdictLabel(item).text }}</span></td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <div v-if="loading && !summary" class="ui-panel ui-empty">
      <strong>正在加载成绩单</strong>
    </div>
    <div v-else-if="summary && !summary.recent.length" class="ui-panel ui-empty">
      <strong>还没有可以记的建议</strong>
      <p>早盘、午盘、晚间计划出过之后，每天收盘会自动登记，5 个交易日后开始出结果。</p>
    </div>

    <p class="score-disclaimer">成绩单只统计过去的建议对不对，不代表以后也这样，不构成投资建议。</p>
  </div>
</template>

<style scoped>
.score-page { display: grid; gap: 12px; color: var(--text); }
.score-command { display: flex; flex-wrap: wrap; align-items: flex-end; justify-content: space-between; gap: 12px; }
.score-command .ui-tabs { flex: 1; }
.score-command .ui-tabs [role="tab"] { min-height: 40px; font-size: 14px; }

.score-account { display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: 12px; }
.score-account select {
  min-height: 34px;
  border: 1px solid var(--line);
  border-radius: var(--r-ctrl);
  padding: 4px 8px;
  background: var(--bg-2);
  color: var(--text);
  font-size: 13px;
}

.score-notice { margin: 0; border: 1px solid; border-radius: var(--r-ctrl); padding: 8px 12px; font-size: 13px; }
.score-notice--danger { border-color: rgba(255, 93, 93, .4); background: rgba(255, 93, 93, .07); color: #ff9b9b; }
.score-notice--ok { border-color: rgba(39, 197, 138, .35); background: rgba(39, 197, 138, .07); color: #78dbb8; }

.score-conclusion { display: grid; gap: 16px; padding: 20px; }
.score-headline { margin: 0; color: var(--gold-2); font-size: 16px; line-height: 1.7; }
.score-rule { margin: 0; color: var(--muted); font-size: 12px; line-height: 1.7; }
.ui-kpi strong.pnl-up { color: var(--up); }
.ui-kpi strong.pnl-down { color: var(--down); }

.score-groups { display: grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); gap: 12px; }
.ui-table th.num, .ui-table td.num { text-align: right; font-variant-numeric: tabular-nums; }
.ui-table td.num-left { font-variant-numeric: tabular-nums; }
.score-code { margin-left: 6px; color: var(--muted-2); font-size: 12px; font-variant-numeric: tabular-nums; }
.score-disclaimer { margin: 0; color: var(--muted); font-size: 12px; line-height: 1.6; }

@media (max-width: 980px) {
  .score-groups { grid-template-columns: 1fr; }
  .ui-kpi { grid-auto-flow: row; }
}
</style>

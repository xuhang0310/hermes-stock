import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const viewSource = await readFile(
  new URL('../src/views/ScreenerView.vue', import.meta.url),
  'utf8'
)

test('ranking table shows price and pct change with A-share tones next to the symbol', () => {
  assert.match(viewSource, /<th class="sc-num">最新价<\/th><th class="sc-num">涨跌幅<\/th>/)
  assert.match(viewSource, /<td class="sc-num">\{\{ formatNum\(item\.price\) \}\}<\/td>/)
  assert.match(viewSource, /<td class="sc-num" :class="pctTone\(item\.pct_chg\)">\{\{ formatPct\(item\.pct_chg\) \}\}<\/td>/)
  assert.match(viewSource, /\.sc-ranking-table td\.pnl-up \{ color: #f0787c/)
  assert.match(viewSource, /\.sc-ranking-table td\.pnl-down \{ color: #4ac69a/)
})

test('rank cell uses the page-local row index instead of a per-row findIndex scan', () => {
  assert.match(viewSource, /v-for="\(item, rowIndex\) in sortedResultItems"/)
  assert.match(viewSource, /\{\{ \(resultPage - 1\) \* resultPageSize \+ rowIndex \+ 1 \}\}/)
  assert.doesNotMatch(viewSource, /allSortedResultItems\.findIndex\(\(row\) => row\.symbol === item\.symbol\)/)
})

test('result rows are keyboard selectable and expose selection state', () => {
  assert.match(viewSource, /tabindex="0"/)
  assert.match(viewSource, /:aria-selected="selectedResultItem\?\.symbol === item\.symbol"/)
  assert.match(viewSource, /@keydown\.enter="selectResult\(item\)"/)
  assert.match(viewSource, /@keydown\.space\.prevent="selectResult\(item\)"/)
  assert.match(viewSource, /\.sc-ranking-table tbody tr:focus-visible/)
})

test('result header honestly describes the daily snapshot instead of realtime results', () => {
  assert.match(viewSource, /最近同步截面 \{\{ fmtDate\(result\.as_of\) \}\}/)
  assert.doesNotMatch(viewSource, /实时结果/)
})

test('result header reports the real hit count and the server-side ranking', () => {
  assert.match(viewSource, /命中 \{\{ result\.total\.toLocaleString\('zh-CN'\) \}\} 只/)
  assert.match(viewSource, /按\{\{ SORT_LABELS\[resultSort\] \}\}展示前 \{\{ result\.items\.length \}\} 只/)
  assert.doesNotMatch(viewSource, /function relativeScore/)
  assert.match(viewSource, /watch\(\[resultSort, scoreValueWeight, scoreMomentumWeight, scoreLiquidityWeight\], scheduleRerank\)/)
})

test('sync status marks stale market data instead of staying silently green', () => {
  assert.match(viewSource, /const dataStale = computed/)
  assert.match(viewSource, /:class="\{ 'is-stale': dataStale \}"/)
  assert.match(viewSource, /数据可能滞后/)
  assert.match(viewSource, /\.sc-data-status\.is-stale \.sc-status-dot/)
})

test('ranking table header stays visible while scrolling and symbol cell no longer depends on column position', () => {
  assert.match(viewSource, /\.sc-ranking-table th \{ position: sticky; top: 0/)
  assert.match(viewSource, /class="sc-symbol-cell"/)
  assert.doesNotMatch(viewSource, /td:nth-child\(3\)/)
})

test('factor cards and comparison cards carry price context', () => {
  const factorPriceLines = viewSource.match(/sc-factor-price/g) || []
  assert.ok(factorPriceLines.length >= 3, 'factor and compare cards should both show the price line plus styles')
  assert.match(viewSource, /最新 \{\{ formatNum\(item\.price\) \}\} · <em :class="pctTone\(item\.pct_chg\)">\{\{ formatPct\(item\.pct_chg\) \}\}<\/em>/)
})

test('page chrome stays plain: no eyebrow labels and one unified font with tabular numerals', () => {
  assert.doesNotMatch(viewSource, /sc-eyebrow/)
  assert.doesNotMatch(viewSource, /class="eyebrow"/)
  assert.doesNotMatch(viewSource, /font-family: ui-monospace/)
  assert.match(viewSource, /font-variant-numeric: tabular-nums/)
})

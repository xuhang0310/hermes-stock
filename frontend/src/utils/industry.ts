import type { IndustryStat } from '../types'

export type IndustryTile = { industry: IndustryStat; x: number; y: number; width: number; height: number }

export const OTHER_INDUSTRY = '其他行业'

/**
 * Merge tail industries below `minShare` of the total metric into one bucket.
 * Tail rows stay individually visible in the ranking view; aggregation only
 * makes the heatmap legible without inventing a fake industry server-side.
 */
export function mergeSmallIndustries(
  rows: IndustryStat[],
  metric: 'count' | 'amount',
  minShare = 0.004
): IndustryStat[] {
  const weightOf = (row: IndustryStat) => (metric === 'count' ? row.stock_count : row.amount_yi ?? 0)
  const total = rows.reduce((sum, row) => sum + weightOf(row), 0)
  if (total <= 0) return rows
  const main: IndustryStat[] = []
  const tail: IndustryStat[] = []
  for (const row of rows) {
    (weightOf(row) / total >= minShare ? main : tail).push(row)
  }
  if (tail.length < 2) return rows
  const sumOf = (key: 'stock_count' | 'valid_count' | 'up_count' | 'down_count' | 'flat_count' | 'amount_count' | 'missing_count' | 'no_trade_count' | 'invalid_count') =>
    tail.reduce((sum, row) => sum + row[key], 0)
  const avgSources = tail.filter(row => row.avg_pct != null && row.valid_count > 0)
  const avgWeight = avgSources.reduce((sum, row) => sum + row.valid_count, 0)
  const stockCount = sumOf('stock_count')
  const up = sumOf('up_count'), down = sumOf('down_count'), flat = sumOf('flat_count')
  const amountRows = tail.filter(row => row.amount_yi != null)
  return [...main, {
    industry: OTHER_INDUSTRY,
    stock_count: stockCount,
    valid_count: sumOf('valid_count'),
    coverage: stockCount ? sumOf('valid_count') / stockCount : 0,
    avg_pct: avgWeight
      ? avgSources.reduce((sum, row) => sum + (row.avg_pct as number) * row.valid_count, 0) / avgWeight
      : null,
    median_pct: null,
    up_count: up,
    down_count: down,
    flat_count: flat,
    up_ratio: up + down + flat > 0 ? up / (up + down + flat) : null,
    amount_yi: amountRows.length ? amountRows.reduce((sum, row) => sum + (row.amount_yi as number), 0) : null,
    amount_count: sumOf('amount_count'),
    missing_count: sumOf('missing_count'),
    no_trade_count: sumOf('no_trade_count'),
    invalid_count: sumOf('invalid_count'),
    leader: null,
    leaders: [],
  }]
}

/** Binary treemap: non-overlapping area proportional to the chosen metric. */
export function industryTiles(rows: IndustryStat[], metric: 'count' | 'amount'): IndustryTile[] {
  const weighted = rows.map(industry => ({ industry, weight: metric === 'count' ? industry.stock_count : industry.amount_yi ?? 0 }))
    .filter(item => Number.isFinite(item.weight) && item.weight > 0)
    .sort((a, b) => b.weight - a.weight || a.industry.industry.localeCompare(b.industry.industry))
  const output: IndustryTile[] = []
  function split(items: typeof weighted, x: number, y: number, width: number, height: number) {
    if (!items.length) return
    if (items.length === 1) { output.push({ industry: items[0].industry, x, y, width, height }); return }
    const total = items.reduce((sum, item) => sum + item.weight, 0)
    let index = 1, first = items[0].weight
    while (index < items.length - 1 && Math.abs(first + items[index].weight - total / 2) < Math.abs(first - total / 2)) {
      first += items[index++].weight
    }
    const ratio = first / total
    if (width >= height) {
      split(items.slice(0, index), x, y, width * ratio, height)
      split(items.slice(index), x + width * ratio, y, width * (1 - ratio), height)
    } else {
      split(items.slice(0, index), x, y, width, height * ratio)
      split(items.slice(index), x, y + height * ratio, width, height * (1 - ratio))
    }
  }
  split(weighted, 0, 0, 1000, 660)
  return output
}

export function industryQuestion(item: import('../types').IndustryMember): string {
  const facts = [
    `行业：${item.industry || '未分类'}；统计日期：${item.trade_date || '未知'}（日线快照，非实时）。`,
    item.pct_chg != null ? `当日涨跌幅 ${item.pct_chg.toFixed(2)}%。` : '',
    item.amount_yi != null ? `成交额 ${item.amount_yi.toFixed(2)} 亿元。` : '',
    item.turnover_rate != null ? `换手率 ${item.turnover_rate.toFixed(2)}%。` : '',
  ].filter(Boolean).join('\n')
  return `请研究 ${item.name}（${item.symbol}）在所属行业中的表现。\n${facts}\n请先核对最新行情，再结合行业整体表现、公司基本面、催化和风险进行分析。区分事实与推断，不将涨幅排名当作未来收益预测；证据不足请明确说明。`
}

export type LeaderFactorKey = 'pct_chg' | 'turnover_rate' | 'amount_yi' | 'volume_ratio' | 'total_mv_yi'
export type LeaderFactor = { key: LeaderFactorKey; label: string; value: number | null; score: number }
export type LeaderScore = { member: import('../types').IndustryMember; composite: number; factors: LeaderFactor[] }

const LEADER_FACTORS: Array<{ key: LeaderFactorKey; label: string }> = [
  { key: 'pct_chg', label: '涨幅' },
  { key: 'turnover_rate', label: '换手' },
  { key: 'amount_yi', label: '成交额' },
  { key: 'volume_ratio', label: '量比' },
  { key: 'total_mv_yi', label: '市值' },
]

/**
 * Equal-weight min-max normalized leader scores within the given member set.
 * Values are relative to the compared members only — a strength comparison,
 * not a return prediction. Missing factors score 0 and are disclosed as null.
 */
export function leaderScores(members: import('../types').IndustryMember[], top = 5): LeaderScore[] {
  const valid = members
    .filter(item => item.status === 'valid' && item.pct_chg != null)
    .sort((a, b) => (b.pct_chg ?? 0) - (a.pct_chg ?? 0) || a.symbol.localeCompare(b.symbol))
    .slice(0, top)
  if (!valid.length) return []
  const ranges = LEADER_FACTORS.map(({ key }) => {
    const values = valid
      .map(item => item[key])
      .filter((value): value is number => typeof value === 'number' && Number.isFinite(value))
    return { min: values.length ? Math.min(...values) : 0, max: values.length ? Math.max(...values) : 0 }
  })
  return valid.map(member => {
    const factors = LEADER_FACTORS.map(({ key, label }, index) => {
      const value = member[key]
      const { min, max } = ranges[index]
      const usable = typeof value === 'number' && Number.isFinite(value)
      const score = usable ? (max === min ? 60 : Math.round(((value as number) - min) / (max - min) * 100)) : 0
      return { key, label, value: usable ? (value as number) : null, score }
    })
    const composite = Math.round(factors.reduce((sum, factor) => sum + factor.score, 0) / factors.length)
    return { member, composite, factors }
  })
}

/** Min-max normalize avg_pct across industries into a relative 0-100 strength score. */
export function industryStrength(rows: IndustryStat[]): Map<string, number> {
  const values = rows
    .map(row => row.avg_pct)
    .filter((value): value is number => typeof value === 'number' && Number.isFinite(value))
  const map = new Map<string, number>()
  if (!values.length) return map
  const min = Math.min(...values)
  const max = Math.max(...values)
  for (const row of rows) {
    if (row.avg_pct == null) continue
    map.set(row.industry, max === min ? 60 : Math.round((row.avg_pct - min) / (max - min) * 100))
  }
  return map
}

import type { IndustryStat } from '../types'

export type IndustryTile = { industry: IndustryStat; x: number; y: number; width: number; height: number }

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

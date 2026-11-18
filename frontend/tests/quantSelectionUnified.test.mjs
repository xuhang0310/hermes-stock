import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const viewPath = fileURLToPath(new URL('../src/views/QuantSelectionView.vue', import.meta.url))
const shellPath = fileURLToPath(new URL('../src/components/AppShell.vue', import.meta.url))
const appPath = fileURLToPath(new URL('../src/App.vue', import.meta.url))
const apiPath = fileURLToPath(new URL('../src/api.ts', import.meta.url))
const typesPath = fileURLToPath(new URL('../src/types.ts', import.meta.url))

test('unified quant selection view is wired into navigation, router, api and types', async () => {
  const [view, shell, app, api, types] = await Promise.all([
    readFile(viewPath, 'utf8'),
    readFile(shellPath, 'utf8'),
    readFile(appPath, 'utf8'),
    readFile(apiPath, 'utf8'),
    readFile(typesPath, 'utf8'),
  ])

  assert.match(shell, /\{ key: 'quantSelection', label: '量化选股（L1-L7）' \}/)
  assert.match(app, /import QuantSelectionView from '\.\/views\/QuantSelectionView\.vue'/)
  assert.match(app, /v-else-if="activePage === 'quantSelection'"/)
  assert.match(app, /window\.history\.pushState\(\{\}, '', '\/quant-selection'\)/)

  assert.match(api, /quantMarketRegime:/)
  assert.match(api, /quantSelectionQuery:/)
  assert.match(api, /quantSelectionStockDetail:/)
  assert.match(api, /quantSelectionExpectationGap:/)
  assert.match(api, /quantSelectionShadowNav:/)

  assert.match(types, /export type MarketRegimeL1 = \{/)
  assert.match(types, /export type LayeredSelectionItem = \{/)
  assert.match(types, /export type L7RiskSizing = \{/)

  // L1 独立温度计、三档周期切换、三档结论卡片、L2-L6 叠色条、单票 L1-L7 抽屉与前瞻净值对账齐全
  assert.match(view, /L1 市场环境/)
  assert.match(view, /建议总仓位上限/)
  assert.match(view, /波段交易（5–15日）/)
  assert.match(view, /中长线配置（30–120日）/)
  assert.match(view, /事件与预期差（3–10日）/)
  assert.match(view, /动手（≥70·无否决）/)
  assert.match(view, /前瞻净值对账（PK 标尺）/)
  assert.match(view, /role="dialog"[\s\S]*aria-modal="true"/)
})

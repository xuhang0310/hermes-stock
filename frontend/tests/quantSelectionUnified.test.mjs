import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const viewPath = fileURLToPath(new URL('../src/views/QuantSelectionView.vue', import.meta.url))
const shellPath = fileURLToPath(new URL('../src/components/AppShell.vue', import.meta.url))
const appPath = fileURLToPath(new URL('../src/App.vue', import.meta.url))
const apiPath = fileURLToPath(new URL('../src/api.ts', import.meta.url))
const typesPath = fileURLToPath(new URL('../src/types.ts', import.meta.url))
const selectionPath = fileURLToPath(new URL('../src/views/AutomaticSelectionView.vue', import.meta.url))
const linksPath = fileURLToPath(new URL('../src/utils/notifications.ts', import.meta.url))

test('layered scoring lives inside the selection page, not as a second menu', async () => {
  const [view, shell, app, api, types, selection, links] = await Promise.all([
    readFile(viewPath, 'utf8'),
    readFile(shellPath, 'utf8'),
    readFile(appPath, 'utf8'),
    readFile(apiPath, 'utf8'),
    readFile(typesPath, 'utf8'),
    readFile(selectionPath, 'utf8'),
    readFile(linksPath, 'utf8'),
  ])

  // 菜单只有一个“选股”，量化选股是里面的“分层打分”标签
  assert.match(shell, /\{ key: 'automaticSelection', label: '选股' \}/)
  assert.doesNotMatch(shell, /quantSelection/)
  assert.doesNotMatch(app, /import QuantSelectionView/)
  assert.match(selection, /import QuantSelectionView from '\.\/QuantSelectionView\.vue'/)
  assert.match(selection, /\{ key: 'layered', label: '分层打分（未验证）' \}/)
  assert.match(selection, /<QuantSelectionView\s+embedded/)
  // 旧链接 /quant-selection 打开选股页的分层打分标签
  assert.match(app, /\^\\\/quant-selection\\\/\?\$\/i\.test\(pathname\)\) \{[\s\S]*?selectionInitialTab\.value = 'layered'/)
  assert.match(links, /'\/quant-selection': 'automaticSelection'/)

  assert.match(api, /quantMarketRegime:/)
  assert.match(api, /quantSelectionQuery:/)
  assert.match(api, /quantSelectionStockDetail:/)
  assert.match(api, /quantSelectionExpectationGap:/)
  assert.match(types, /export type MarketRegimeL1 = \{/)
  assert.match(types, /export type LayeredSelectionItem = \{/)
  assert.match(types, /export type L7RiskSizing = \{/)

  // 偏乐观的“前瞻净值对比”去掉了，成绩看自动选股的前瞻评估
  assert.doesNotMatch(api, /quantSelectionShadowNav|shadow-nav/)
  assert.doesNotMatch(view, /前瞻净值|PK 标尺/)

  // 没有回测的打分只说分数高低，不说“动手”；风险比例不让用户自己选
  assert.match(view, /L1 市场环境/)
  assert.match(view, /波段交易（5–15日）/)
  assert.match(view, /高分（≥70·未排除）/)
  assert.doesNotMatch(view, /动手|单笔风险上限|证伪/)
  assert.match(view, /v-if="canRebuild"/)
  assert.match(view, /role="dialog"[\s\S]*aria-modal="true"/)
})

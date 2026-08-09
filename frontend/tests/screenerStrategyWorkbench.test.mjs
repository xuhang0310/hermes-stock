import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const viewPath = fileURLToPath(new URL('../src/views/ScreenerView.vue', import.meta.url))
const appPath = fileURLToPath(new URL('../src/App.vue', import.meta.url))

test('strategy builder exposes the three-column decision workspace', async () => {
  const source = await readFile(viewPath, 'utf8')

  assert.match(source, /class="sc-strategy-sidebar sc-panel"/)
  assert.match(source, /class="sc-results-center sc-panel"/)
  assert.match(source, /class="sc-explain-panel sc-panel"/)
  assert.match(source, /const scoredResultItems = computed/)
  assert.match(source, /候选集相对评分/)
})

test('quick backtest uses the real deterministic backtest API', async () => {
  const [view, app] = await Promise.all([readFile(viewPath, 'utf8'), readFile(appPath, 'utf8')])

  assert.match(view, /async function runQuickBacktest/)
  assert.match(view, /await api\.runStrategyBacktest/)
  assert.match(view, /strategy_code: 'momentum'/)
  assert.match(view, /emit\('openBacktest', out\.run_id\)/)
  assert.match(app, /@open-backtest="openScreenerBacktest"/)
})

test('strategy workbench actions remain connected to real destinations', async () => {
  const [view, app] = await Promise.all([readFile(viewPath, 'utf8'), readFile(appPath, 'utf8')])

  assert.match(view, /emit\('createAgentTask', allSortedResultItems\.slice\(0, 20\)\)/)
  assert.match(app, /function createScreenerAgentTask/)
  assert.match(app, /@create-agent-task="createScreenerAgentTask"/)
})

import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const viewPath = new URL('../src/views/BacktestReportView.vue', import.meta.url)

test('backtest report describes excess return against the actual benchmark', async () => {
  const view = await readFile(viewPath, 'utf8')

  assert.match(view, /detail: `相对\$\{run\.value\?\.benchmark_label \|\| '基准'\}`/)
  assert.match(view, /相对\{\{ run\.benchmark_label \|\| '基准' \}\}/)
  assert.doesNotMatch(view, /detail: '相对沪深300'/)
})

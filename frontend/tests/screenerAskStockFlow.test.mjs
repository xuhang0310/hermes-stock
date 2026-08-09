import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const currentDir = path.dirname(fileURLToPath(import.meta.url))
const appSource = fs.readFileSync(path.resolve(currentDir, '../src/App.vue'), 'utf8')
const screenerSource = fs.readFileSync(path.resolve(currentDir, '../src/views/ScreenerView.vue'), 'utf8')
const digitalHumanSource = fs.readFileSync(path.resolve(currentDir, '../src/views/DigitalHumanCenter.vue'), 'utf8')

test('AI screener results expose a stock-specific digital-human action', () => {
  assert.match(screenerSource, /askStock: \[item: ScreenerItem\]/)
  assert.match(screenerSource, /@click\.stop="emit\('askStock', item\)"/)
  assert.match(screenerSource, /function askCheckedStock/)
  assert.match(appSource, /@ask-stock="openScreenerStockAnalysis"/)
})

test('selected screener stock becomes a detailed initial analysis question', () => {
  assert.match(appSource, /function openScreenerStockAnalysis\(item: ScreenerItem\)/)
  assert.match(appSource, /`请对 \$\{item\.name\}（\$\{item\.symbol\}）进行专项解读。`/)
  assert.match(appSource, /digitalHumanInitialQuestion\.value =/)
  assert.match(appSource, /:initial-question="digitalHumanInitialQuestion"/)
})

test('digital human center starts the passed stock question automatically', () => {
  assert.match(digitalHumanSource, /initialQuestion\?: string/)
  assert.match(digitalHumanSource, /else if \(props\.initialQuestion\.trim\(\)\)/)
  assert.match(digitalHumanSource, /question\.value = props\.initialQuestion\.trim\(\)/)
  assert.match(digitalHumanSource, /await send\(\)/)
})

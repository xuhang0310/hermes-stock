import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const viewPath = fileURLToPath(new URL('../src/views/ScreenerView.vue', import.meta.url))
const apiPath = fileURLToPath(new URL('../src/api.ts', import.meta.url))

test('saved strategies are a real reusable workspace', async () => {
  const [view, api] = await Promise.all([readFile(viewPath, 'utf8'), readFile(apiPath, 'utf8')])

  assert.match(view, /type ScreenerWorkspace = 'quick' \| 'builder' \| 'saved'/)
  assert.match(view, /@click="openSavedStrategies">我的策略/)
  assert.doesNotMatch(view, /disabled[^>]*>我的策略/)
  assert.match(view, /async function saveCurrentStrategy/)
  assert.match(view, /function useSavedStrategy/)
  assert.match(view, /async function deleteSavedStrategy/)
  assert.match(api, /screenerStrategySave:/)
  assert.match(api, /screenerStrategyDelete:/)
})

test('running a saved strategy records its latest result count', async () => {
  const view = await readFile(viewPath, 'utf8')

  assert.match(view, /activeSavedStrategyId\.value != null/)
  assert.match(view, /api\.screenerStrategyResult\(activeSavedStrategyId\.value, result\.value\.total\)/)
})

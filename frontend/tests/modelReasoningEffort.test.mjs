import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

// 生效的模型配置页是 SettingsView 内嵌的 ModelSettingsView（ModelConfigView 已无引用）
const viewPath = fileURLToPath(new URL('../src/views/ModelSettingsView.vue', import.meta.url))
const stylesPath = fileURLToPath(new URL('../src/styles.css', import.meta.url))
const typesPath = fileURLToPath(new URL('../src/types.ts', import.meta.url))

test('model settings dialog exposes a segmented per-model reasoning-effort control', async () => {
  const source = await readFile(viewPath, 'utf8')

  assert.match(source, /reasoning_effort: 'auto'/)
  assert.match(source, /class="effort-tabs"/)
  assert.match(source, /v-for="opt in effortOptions"/)
  assert.match(source, /configForm\.reasoning_effort = opt\.value/)
  for (const [value, label] of [
    ['auto', '自动'],
    ['off', '关闭'],
    ['low', '低'],
    ['medium', '中'],
    ['high', '高'],
    ['xhigh', '超高'],
    ['max', '最大'],
  ]) {
    assert.match(source, new RegExp(`\\{ value: '${value}', label: '${label}' \\}`))
  }
  assert.match(source, /reasoning_effort: row\.reasoning_effort \|\| 'auto'/)
  assert.match(source, /reasoning_effort: configForm\.reasoning_effort/)
})

test('max tool steps input allows up to 60', async () => {
  const source = await readFile(viewPath, 'utf8')
  assert.match(source, /v-model\.number="configForm\.max_steps" type="number" min="1" max="60"/)
})

test('segmented control styles exist', async () => {
  const source = await readFile(stylesPath, 'utf8')
  assert.match(source, /\.effort-tabs \{/)
  assert.match(source, /\.effort-tabs button\.active \{/)
})

test('ModelConfig type carries reasoning_effort', async () => {
  const source = await readFile(typesPath, 'utf8')
  assert.match(source, /reasoning_effort: string/)
})

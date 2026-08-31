import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

// 生效的模型配置页是 SettingsView 内嵌的 ModelSettingsView（ModelConfigView 已无引用）
const viewPath = fileURLToPath(new URL('../src/views/ModelSettingsView.vue', import.meta.url))
const typesPath = fileURLToPath(new URL('../src/types.ts', import.meta.url))

test('model settings dialog exposes a per-model thinking-mode control', async () => {
  const source = await readFile(viewPath, 'utf8')

  assert.match(source, /thinking_mode: 'auto'/)
  assert.match(source, /v-model="configForm\.thinking_mode"/)
  assert.match(source, /<option value="on">开启深度思考<\/option>/)
  assert.match(source, /<option value="off">关闭深度思考<\/option>/)
  assert.match(source, /thinking_mode: row\.thinking_mode \|\| 'auto'/)
  assert.match(source, /thinking_mode: configForm\.thinking_mode/)
})

test('ModelConfig type carries thinking_mode', async () => {
  const source = await readFile(typesPath, 'utf8')
  assert.match(source, /thinking_mode: string/)
})

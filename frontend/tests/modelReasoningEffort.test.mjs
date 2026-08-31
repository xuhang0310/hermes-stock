import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

// 生效的模型配置页是 SettingsView 内嵌的 ModelSettingsView（ModelConfigView 已无引用）
const viewPath = fileURLToPath(new URL('../src/views/ModelSettingsView.vue', import.meta.url))
const typesPath = fileURLToPath(new URL('../src/types.ts', import.meta.url))

test('model settings dialog exposes a graded per-model reasoning-effort control', async () => {
  const source = await readFile(viewPath, 'utf8')

  assert.match(source, /reasoning_effort: 'auto'/)
  assert.match(source, /v-model="configForm\.reasoning_effort"/)
  assert.match(source, /<option value="auto">跟随模型默认<\/option>/)
  assert.match(source, /<option value="minimal">最弱<\/option>/)
  assert.match(source, /<option value="low">低<\/option>/)
  assert.match(source, /<option value="medium">中<\/option>/)
  assert.match(source, /<option value="high">高<\/option>/)
  assert.match(source, /<option value="xhigh">极限<\/option>/)
  assert.match(source, /reasoning_effort: row\.reasoning_effort \|\| 'auto'/)
  assert.match(source, /reasoning_effort: configForm\.reasoning_effort/)
})

test('ModelConfig type carries reasoning_effort', async () => {
  const source = await readFile(typesPath, 'utf8')
  assert.match(source, /reasoning_effort: string/)
})

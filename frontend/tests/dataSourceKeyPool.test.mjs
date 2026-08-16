import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const source = readFileSync(new URL('../src/views/DataSourceSettingsView.vue', import.meta.url), 'utf8')

test('Tavily settings append backup keys without replacing the existing pool', () => {
  assert.match(source, /新增备用 API Key/)
  assert.match(source, /form\.additional_api_key/)
  assert.match(source, /不会覆盖现有配置，额度不足时会自动切换/)
  assert.match(source, /item\.key_count/)
})

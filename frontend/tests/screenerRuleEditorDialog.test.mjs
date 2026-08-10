import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const viewPath = fileURLToPath(new URL('../src/views/ScreenerView.vue', import.meta.url))

test('strategy condition editor is rendered as an accessible modal', async () => {
  const source = await readFile(viewPath, 'utf8')

  assert.match(source, /<Teleport to="body">/)
  assert.match(source, /class="sc-rule-dialog-backdrop" @mousedown\.self="closeRuleEditor"/)
  assert.match(source, /class="sc-rule-config-dialog" role="dialog" aria-modal="true"/)
  assert.match(source, /aria-labelledby="sc-rule-dialog-title"/)
  assert.match(source, /aria-label="关闭条件编辑" @click="closeRuleEditor"/)
  assert.doesNotMatch(source, /class="sc-panel sc-rule-config-panel"/)
})

test('condition modal supports focus, escape, and document scroll restoration', async () => {
  const source = await readFile(viewPath, 'utf8')

  assert.match(source, /function openRuleEditor\(event\?: Event\)/)
  assert.match(source, /function closeRuleEditor\(\)/)
  assert.match(source, /event\.key === 'Escape' && ruleEditorOpen\.value/)
  assert.match(source, /event\.key !== 'Tab'/)
  assert.match(source, /document\.body\.classList\.toggle\('sc-rule-dialog-open', open\)/)
  assert.match(source, /document\.body\.classList\.remove\('sc-rule-dialog-open'\)/)
  assert.match(source, /:global\(body\.sc-rule-dialog-open\) \{ overflow: hidden; \}/)
})

test('condition modal becomes a full-height editor on mobile', async () => {
  const source = await readFile(viewPath, 'utf8')

  assert.match(source, /\.sc-rule-dialog-backdrop \{ align-items: end; padding: 0; \}/)
  assert.match(source, /\.sc-rule-config-dialog \{ width: 100%; height: 100dvh; max-height: 100dvh;/)
  assert.match(source, /\.sc-rule-dialog-footer > div \{ display: grid; grid-template-columns: 1fr 1fr; \}/)
})

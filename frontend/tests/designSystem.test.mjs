import assert from 'node:assert/strict'
import { readdir, readFile } from 'node:fs/promises'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

const viewsDir = fileURLToPath(new URL('../src/views/', import.meta.url))
const viewFiles = (await readdir(viewsDir)).filter((name) => name.endsWith('.vue'))

const FORBIDDEN_HEX = [
  '#0d1117', '#161b22', '#21262d', '#30363d', '#484f58', '#8b949e', '#e6edf3',
  '#d7b76f', '#eabf63', '#ead08f', '#785f2d', '#75602f', '#75602e', '#8a713b', '#745e2d', '#b38a34',
]
const MONOSPACE_WHITELIST = new Map([
  ['AutomaticSelectionView.vue', 1],
  ['BacktestReportView.vue', 2],
  ['FactorAnalysisView.vue', 1],
  ['WorkspaceDocsView.vue', 1],
])
// 设置中心（model-hub 家族）沿用其自身设计族，eyebrow 清理排入 W5 波次；
// 量化参数模块与新建页面一律禁止再引入。
const EYEBROW_EXEMPT = new Set([
  'DataSourceSettingsView.vue',
  'DigitalHumanAdminView.vue',
  'ModelSettingsView.vue',
  'NotifyChannelSettingsView.vue',
  'RhythmView.vue',
])
const MIGRATED_FILES = [
  'ScreenerView.vue',
  'AutomaticSelectionView.vue',
  'IndustryAnalysisView.vue',
  'BacktestHistoryView.vue',
  'BacktestReportView.vue',
  'FactorAnalysisView.vue',
  'AgentTaskBoardView.vue',
]
const FONT_SCALE = new Set(['12px', '13px', '14px', '16px', '20px', '24px', '32px'])
const RADIUS_SCALE = new Set(['0px', '4px', '8px', '999px'])

async function readView(name) {
  return readFile(join(viewsDir, name), 'utf8')
}

function styleBlock(source) {
  const open = source.indexOf('<style')
  if (open === -1) return ''
  const start = source.indexOf('>', open) + 1
  const end = source.indexOf('</style>')
  return source.slice(start, end)
}

test('no foreign color palette or ad-hoc gold values anywhere in views', async () => {
  for (const name of viewFiles) {
    const source = await readView(name)
    for (const hex of FORBIDDEN_HEX) {
      assert.ok(
        !source.toLowerCase().includes(hex),
        `${name} must not use ${hex}; map it to a design token (docs/design-system.md §3.1)`
      )
    }
  }
})

test('no template eyebrow chrome or decorative section numbers', async () => {
  for (const name of viewFiles) {
    if (EYEBROW_EXEMPT.has(name)) continue
    const source = await readView(name)
    assert.doesNotMatch(source, /class="eyebrow"/, `${name} must not use eyebrow labels`)
    assert.doesNotMatch(source, /\.sc-eyebrow|\.ia-eyebrow|ia-section-number/, `${name} must not keep eyebrow styles`)
  }
})

test('monospace stays confined to genuine code contexts', async () => {
  for (const name of viewFiles) {
    const source = await readView(name)
    const occurrences = source.split('\n').filter((line) => /ui-monospace|Consolas|monospace/.test(line))
    const allowed = MONOSPACE_WHITELIST.get(name) || 0
    assert.ok(
      occurrences.length <= allowed,
      `${name} uses monospace ${occurrences.length}x (allowed ${allowed}); use tabular-nums instead`
    )
  }
})

test('migrated quant pages keep the font scale inside style blocks', async () => {
  for (const name of MIGRATED_FILES) {
    const css = styleBlock(await readView(name))
    const declarations = css.matchAll(/font(?:-size)?:\s*(\d+(?:\.\d+)?)px/g)
    for (const match of declarations) {
      assert.ok(
        FONT_SCALE.has(match[1] + 'px'),
        `${name} uses font-size ${match[1]}px; allowed scale: ${[...FONT_SCALE].join(' ')}`
      )
    }
  }
})

test('migrated quant pages keep the radius scale inside style blocks', async () => {
  for (const name of MIGRATED_FILES) {
    const css = styleBlock(await readView(name))
    const declarations = css.matchAll(/border-radius:\s*(\d+(?:\.\d+)?)px/g)
    for (const match of declarations) {
      assert.ok(
        RADIUS_SCALE.has(match[1] + 'px'),
        `${name} uses border-radius ${match[1]}px; allowed scale: 4px controls, 8px panels, 999px pills`
      )
    }
  }
})

test('quant module buttons use the shared ui-btn system', async () => {
  for (const name of MIGRATED_FILES) {
    const source = await readView(name)
    assert.doesNotMatch(source, /sc-btn|auto-button|ia-primary|sc-text-btn/, `${name} must use ui-btn classes`)
  }
})

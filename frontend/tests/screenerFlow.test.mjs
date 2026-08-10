import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const viewPath = fileURLToPath(new URL('../src/views/ScreenerView.vue', import.meta.url))

async function readScreenerView() {
  return readFile(viewPath, 'utf8')
}

test('strategy builder tab is an enabled workspace entry', async () => {
  const source = await readScreenerView()
  const tab = source.match(/<button[\s\S]*?data-testid="screener-strategy-tab"[\s\S]*?<\/button>/)?.[0]

  assert.ok(tab, 'strategy builder tab should exist')
  assert.match(tab, /@click="openBuilder"/)
  assert.doesNotMatch(tab, /\sdisabled(?:\s|=|>)/)
  assert.match(source, /data-testid="screener-builder"/)
  assert.match(source, /data-testid="screener-run"/)
})

test('generated and template prompts enter the executable builder flow', async () => {
  const source = await readScreenerView()
  const usePrompt = source.match(/async function usePrompt[\s\S]*?\n}/)?.[0]
  const parseNL = source.match(/async function parseNL[\s\S]*?\n}/)?.[0]

  assert.ok(usePrompt, 'template prompt handler should exist')
  assert.ok(parseNL, 'natural language parser should exist')
  assert.match(usePrompt, /knownQuery\?: ScreenerQuery/)
  assert.match(usePrompt, /applyQuery\(query\)/)
  assert.match(usePrompt, /api\.screenerPromptRecord/)
  assert.match(usePrompt, /await parseNL\(\)/)
  assert.ok(
    usePrompt.indexOf('return') < usePrompt.indexOf('await parseNL()'),
    'known structured queries should return before the LLM parser fallback'
  )
  assert.match(parseNL, /result\.value = null/)
  assert.match(parseNL, /ran\.value = false/)
  assert.match(parseNL, /activeWorkspace\.value = 'builder'/)
  assert.match(parseNL, /api\.screenerParse/)
  assert.match(source, /usePrompt\(templatePrompt\(tpl\), tpl\.query/)
  assert.match(source, /usePrompt\(item\.prompt, item\.query\)/)
  assert.match(source, /ref="resultSection"/)
  assert.match(source, /resultSection\.value\?\.scrollIntoView/)
})

test('history prompts are direct strategy builder shortcuts', async () => {
  const source = await readScreenerView()
  const usePrompt = source.match(/async function usePrompt[\s\S]*?\n}/)?.[0]

  assert.ok(usePrompt, 'history prompt handler should exist')
  assert.match(usePrompt, /activeWorkspace\.value = 'builder'/)
  assert.ok(
    usePrompt.indexOf("activeWorkspace.value = 'builder'") < usePrompt.indexOf('await parseNL()'),
    'the builder should open before the prompt parser finishes'
  )
  assert.match(source, /data-testid="screener-history-entry"/)
  assert.match(source, /data-testid="screener-history-open-builder"/)
  assert.match(source, /usePrompt\(item\.text, item\.parsed_query, item\.parse_summary, item\.id\)/)
  assert.match(source, /进入策略构建/)
})

test('holding recommendations expose bounded progression strategies', async () => {
  const source = await readScreenerView()

  assert.match(source, /持仓推进策略/)
  assert.match(source, /每套策略按实时行情收紧至 20–120 只/)
  assert.match(source, /recommendation\.model !== '持仓画像规则引擎'/)
  assert.match(source, /void refreshRecommendation\(\)/)
  assert.match(source, /templateConditionChips\(tpl\)/)
  assert.match(source, /tpl\.stage \|\| '基于持仓'/)
  assert.match(source, /进入策略构建/)
})

import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const viewPath = fileURLToPath(new URL('../src/views/FactorAnalysisView.vue', import.meta.url))
const apiPath = fileURLToPath(new URL('../src/api.ts', import.meta.url))
const typesPath = fileURLToPath(new URL('../src/types.ts', import.meta.url))

test('factor analysis view hosts the multi-factor composite scoring panel', async () => {
  const source = await readFile(viewPath, 'utf8')

  assert.match(source, /多因子综合打分（横截面 z-score 加权）/)
  assert.match(source, /const compositeWeights = ref\(''\)/)
  assert.match(source, /const compositeSymbolsInput = ref\(''\)/)
  assert.match(source, /const compositeLimit = ref\(50\)/)
  assert.match(source, /const compositeResult = ref<FactorCompositeResult \| null>\(null\)/)
  assert.match(source, /data-testid="factor-composite-run"/)
  assert.match(source, /:disabled="compositeRunning \|\| !selectedCount \|\| selectedCount > 10"/)
})

test('runComposite sends aligned weights and optional symbol pool to the envelope API', async () => {
  const source = await readFile(viewPath, 'utf8')

  assert.match(source, /async function runComposite\(\)/)
  assert.match(source, /api\.factorCompositeScore\(\{/)
  assert.match(source, /weights: Object\.keys\(weights\)\.length \? weights : null/)
  assert.match(source, /symbols: symbols\.length \? symbols : null/)
  assert.match(source, /含本地因子必填/)
})

test('composite result table shows ranking, per-factor z columns and A-share color convention', async () => {
  const source = await readFile(viewPath, 'utf8')

  assert.match(source, /<th>排名<\/th>/)
  assert.match(source, /<th class="num">综合得分<\/th>/)
  assert.match(source, /<th class="num">百分位<\/th>/)
  assert.match(source, /z\(\{\{ shortFactorId\(f\.id\) \}\}\)/)
  assert.match(source, /:class="pctTone\(row\.pct_chg\)"/)
  assert.match(source, /function pctTone\(value: number \| null \| undefined\): string/)
  assert.match(source, /return 'pnl-up'/)
  assert.match(source, /\{\{ compositeResult\.interpretation \}\}/)
})

test('api client exposes factorCompositeScore against the envelope endpoint', async () => {
  const source = await readFile(apiPath, 'utf8')

  assert.match(source, /factorCompositeScore: \(payload: \{/)
  assert.match(source, /'\/api\/factors\/composite-score'/)
  assert.match(source, /request<ApiEnvelope<FactorCompositeResult>>/)
})

test('types describe the composite scoring payload', async () => {
  const source = await readFile(typesPath, 'utf8')

  assert.match(source, /export type FactorCompositeRow = \{/)
  assert.match(source, /composite_score: number/)
  assert.match(source, /factor_scores: Record<string, \{ raw: number; z: number \}>/)
  assert.match(source, /factors_used: Array<\{ id: string; zoo: string; as_of_date: string; weight: number \}>/)
  assert.match(source, /interpretation: string/)
})

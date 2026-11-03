import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'

const source = await readFile(new URL('../src/components/PositionSyncWizard.vue', import.meta.url), 'utf8')
const setup = source.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1]
const output = ts.transpileModule(`${setup}\nglobalThis.harness = { step, accountId, recognized, rows, preview, previewLoading, canApply, error, selectAccount, loadPreview, backToUpload, confirm };`, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
}).outputText.split(/\r?\n/).filter((line) => !line.startsWith('import ') && !/^export\s*\{\s*\};?$/.test(line)).join('\n')

function makePreview(accountId) {
  return { account_id: accountId, items: [], funds_after: { available_cash: 1000 }, apply_funds_default: true }
}

function deferred() {
  let resolve, reject
  const promise = new Promise((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}

function createHarness(fetchPreview) {
  const writes = []
  const context = {
    defineProps: () => ({ defaultAccountId: 3, accounts: [{ id: 3, broker_name: '账户 A' }, { id: 4, broker_name: '账户 B' }] }),
    defineEmits: () => () => {},
    ref: (value) => ({ value }),
    reactive: (value) => value,
    computed: (fn) => ({ get value() { return fn() } }),
    onMounted: () => {},
    onBeforeUnmount: () => {},
    window: { localStorage: { getItem: () => null, setItem: () => {} } },
    api: {
      positionSyncPreview: fetchPreview,
      positionSyncApply: async (payload) => { writes.push(payload); return { batch_id: 1, account: { id: payload.account_id } } },
    },
  }
  vm.runInNewContext(output, context)
  const h = context.harness
  h.step.value = 'review'
  h.recognized.value = { funds: { available_cash: 1000 } }
  return { ...h, writes }
}

const settle = () => new Promise((resolve) => setImmediate(resolve))

test('failed account switch clears the previous account preview and cannot write it to the new account', async () => {
  const pending = deferred()
  const h = createHarness(() => pending.promise)
  h.preview.value = makePreview(3)
  assert.equal(h.canApply.value, true)
  h.selectAccount(4)
  assert.equal(h.preview.value, null)
  assert.equal(h.canApply.value, false)
  pending.reject(new Error('offline'))
  await settle()
  assert.equal(h.previewLoading.value, false)
  assert.match(h.error.value, /offline/)
  await h.confirm()
  assert.equal(h.writes.length, 0)
})

test('a slower account preview cannot replace the selected account or enable the wrong write', async () => {
  const first = deferred()
  const second = deferred()
  const h = createHarness(({ account_id }) => account_id === 3 ? first.promise : second.promise)
  const oldRequest = h.loadPreview()
  h.selectAccount(4)
  second.resolve(makePreview(4))
  await settle()
  first.resolve(makePreview(3))
  await oldRequest
  assert.equal(h.preview.value.account_id, 4)
  assert.equal(h.previewLoading.value, false)
  assert.equal(h.canApply.value, true)
  await h.confirm()
  assert.equal(h.writes.length, 1)
  assert.equal(h.writes[0].account_id, 4)
})

test('returning to upload ignores an in-flight preview and keeps confirmation unavailable', async () => {
  const pending = deferred()
  const h = createHarness(() => pending.promise)
  const request = h.loadPreview()
  h.backToUpload()
  pending.resolve(makePreview(3))
  await request
  assert.equal(h.step.value, 'upload')
  assert.equal(h.preview.value, null)
  assert.equal(h.canApply.value, false)
})

test('confirmation requires a preview for the currently selected account', async () => {
  const h = createHarness(() => Promise.resolve(makePreview(4)))
  h.preview.value = makePreview(4)
  assert.equal(h.accountId.value, 3)
  assert.equal(h.canApply.value, false)
  await h.confirm()
  assert.equal(h.writes.length, 0)
})

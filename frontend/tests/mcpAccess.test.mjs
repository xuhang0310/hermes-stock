import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'

function compileSetup(source, exports) {
  const setup = source.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1]
  return ts.transpileModule(`${setup}\nglobalThis.harness = { ${exports} };`, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  }).outputText.split(/\r?\n/)
    .filter((line) => !line.startsWith('import ') && !/^export\s*\{\s*\};?$/.test(line)).join('\n')
}

const source = await readFile(new URL('../src/views/McpAccessView.vue', import.meta.url), 'utf8')
const output = compileSetup(source, [
  'credentials', 'availableScopes', 'endpoint', 'loading', 'loaded', 'saving', 'error', 'notice',
  'dialogError', 'dialog', 'revokeDialog', 'secretInput', 'createdSecret', 'createdName', 'secretVisible',
  'copied', 'revokeTarget', 'revoking', 'form', 'options', 'activeCount', 'canCreate', 'credentialStatus',
  'connectionConfig', 'load', 'openCreate', 'closeCreate', 'createKey', 'copyText', 'requestRevoke', 'revokeKey',
].join(', '))

const clone = (value) => JSON.parse(JSON.stringify(value))
const credential = (changes = {}) => ({
  id: 4, name: '研究助手', token_prefix: 'hs_mcp_test', scopes: ['market:read'],
  created_at: '2026-10-30T01:00:00+00:00', expires_at: '2099-10-30T01:00:00+00:00',
  revoked_at: null, is_expired: false, ...changes,
})
const created = (changes = {}) => ({
  token: 'hs_mcp_one_time_secret', credential: credential(), endpoint: '/api/mcp/', ...changes,
})

function createHarness({ scopes = ['market:read', 'portfolio:read', 'portfolio:write'], tokens = [], list, create, revoke, clipboard } = {}) {
  const requests = { list: [], create: [], revoke: [], clipboard: [] }
  const unmounted = []
  const context = {
    Error, URL, Date,
    defineProps: () => ({ user: { id: 1, display_name: 'Owner' } }),
    ref: (value) => ({ value }), reactive: (value) => value,
    computed: (fn) => ({ get value() { return fn() } }),
    onMounted: () => {}, onUnmounted: (fn) => unmounted.push(fn), nextTick: async () => {},
    window: { location: { origin: 'https://stock.example' } },
    navigator: { clipboard: { writeText: async (value) => {
      requests.clipboard.push(value)
      if (clipboard) return clipboard(value)
    } } },
    api: {
      mcpTokens: async () => {
        requests.list.push({})
        return list ? list() : { tokens: clone(tokens), available_scopes: scopes, endpoint: '/api/mcp/' }
      },
      createMcpToken: async (params) => {
        requests.create.push(clone(params))
        return create ? create(params) : created()
      },
      revokeMcpToken: async (id) => {
        requests.revoke.push(id)
        return revoke ? revoke(id) : { revoked: true, id }
      },
    },
  }
  vm.runInNewContext(output, context)
  const h = context.harness
  h.dialog.value = { showModal() {}, close() {} }
  h.revokeDialog.value = { showModal() {}, close() {} }
  return { ...h, requests, unmounted }
}

test('only granted scopes appear and a new key defaults to available read-only scopes', async () => {
  const h = createHarness()
  assert.equal(h.canCreate.value, false)
  await h.load()
  await h.openCreate()
  assert.deepEqual(Array.from(h.options.value, (item) => item.key), ['market:read', 'portfolio:read', 'portfolio:write'])
  assert.deepEqual(Array.from(h.form.scopes), ['market:read', 'portfolio:read'])
  assert.equal(h.form.days, 90)
  assert.equal(h.canCreate.value, true)
  assert.equal(h.endpoint.value, 'https://stock.example/api/mcp/')
  const noAccess = createHarness({ scopes: [] })
  await noAccess.load()
  assert.equal(noAccess.canCreate.value, false)
})

test('double submit sends one request, preserves the one-time secret during refresh, and clears it on close', async () => {
  let resolveCreate
  const h = createHarness({ create: () => new Promise((resolve) => { resolveCreate = resolve }) })
  await h.load()
  await h.openCreate()
  h.form.name = '  我的助手  '
  const pending = h.createKey()
  await h.createKey()
  assert.equal(h.requests.create.length, 1)
  assert.equal(h.saving.value, true)
  assert.equal(h.requests.create[0].name, '我的助手')
  resolveCreate(created())
  await pending
  assert.equal(h.saving.value, false)
  assert.equal(h.createdSecret.value, 'hs_mcp_one_time_secret')
  assert.equal(h.credentials.value[0].id, 4)
  await h.createKey()
  assert.equal(h.requests.create.length, 1)
  await h.load()
  assert.equal(h.createdSecret.value, 'hs_mcp_one_time_secret')
  h.closeCreate()
  assert.equal(h.createdSecret.value, '')
  assert.equal(h.createdName.value, '')
  assert.equal(h.secretVisible.value, false)
})

test('invalid scopes and expiry are rejected without an API mutation', async () => {
  const h = createHarness({ scopes: ['market:read'] })
  await h.load()
  h.form.name = 'test'
  for (const scopes of [[], ['portfolio:write'], ['market:read', 'portfolio:write']]) {
    h.form.scopes = scopes
    await h.createKey()
    assert.equal(h.requests.create.length, 0, `unexpected mutation for ${scopes}`)
    assert.ok(h.dialogError.value)
  }
  h.form.scopes = ['market:read']
  for (const days of [0, 366, 1.5]) {
    h.form.days = days
    await h.createKey()
    assert.equal(h.requests.create.length, 0)
  }
})

test('connection config copies the exact endpoint and token while template contains no created secret', async () => {
  const h = createHarness()
  await h.load()
  const config = h.connectionConfig('special-token')
  const parsed = JSON.parse(config)
  assert.deepEqual(parsed, { mcpServers: { 'sixrabbits-stock': {
    url: 'https://stock.example/api/mcp/', headers: { Authorization: 'Bearer special-token' },
  } } })
  await h.copyText(config, '连接配置', true)
  assert.equal(h.requests.clipboard[0], config)
  assert.equal(h.copied.value, '连接配置已复制')
  assert.match(h.connectionConfig(), /YOUR_MCP_TOKEN/)
  assert.doesNotMatch(h.connectionConfig(), /special-token/)
})

test('clipboard failure offers manual copying rather than reporting success', async () => {
  let selected = false
  const h = createHarness({ clipboard: async () => { throw new Error('denied') } })
  h.secretInput.value = { select: () => { selected = true } }
  await h.copyText('secret', '密钥', true)
  assert.match(h.copied.value, /手动复制/)
  assert.equal(h.secretVisible.value, true)
  assert.equal(selected, true)
  await h.copyText(h.endpoint.value, '地址')
  assert.match(h.notice.value, /手动复制/)
})

test('revocation updates only the selected credential and failure leaves it usable', async () => {
  const h = createHarness({ tokens: [credential(), credential({ id: 5, name: '另一助手' })] })
  await h.load()
  h.requestRevoke(h.credentials.value[0])
  await h.revokeKey()
  assert.deepEqual(h.requests.revoke, [4])
  assert.equal(h.credentialStatus(h.credentials.value[0]), '已撤销')
  assert.equal(h.credentialStatus(h.credentials.value[1]), '可用')
  assert.equal(h.revokeTarget.value, null)
  assert.equal(h.activeCount.value, 1)

  const failed = createHarness({ tokens: [credential()], revoke: async () => { throw new Error('network unavailable') } })
  await failed.load()
  failed.requestRevoke(failed.credentials.value[0])
  await failed.revokeKey()
  assert.equal(failed.credentialStatus(failed.credentials.value[0]), '可用')
  assert.match(failed.dialogError.value, /network unavailable/)
  assert.equal(failed.revoking.value, false)
})

test('a pending list response cannot restore a successfully revoked credential', async () => {
  let resolveList
  let first = true
  const h = createHarness({ list: () => {
    if (first) {
      first = false
      return { tokens: [credential()], available_scopes: ['market:read'], endpoint: '/api/mcp/' }
    }
    return new Promise((resolve) => { resolveList = resolve })
  } })
  await h.load()
  const stale = h.load()
  h.requestRevoke(h.credentials.value[0])
  await h.revokeKey()
  resolveList({ tokens: [credential()], available_scopes: ['market:read'], endpoint: '/api/mcp/' })
  await stale
  assert.equal(h.credentialStatus(h.credentials.value[0]), '已撤销')
})

test('unmount clears a displayed secret and ignores a late create response', async () => {
  const h = createHarness()
  await h.load()
  await h.openCreate()
  h.form.name = 'first'
  await h.createKey()
  h.unmounted[0]()
  assert.equal(h.createdSecret.value, '')

  let resolveCreate
  const pendingView = createHarness({ create: () => new Promise((resolve) => { resolveCreate = resolve }) })
  await pendingView.load()
  await pendingView.openCreate()
  pendingView.form.name = 'second'
  const pending = pendingView.createKey()
  pendingView.unmounted[0]()
  resolveCreate(created())
  await pending
  assert.equal(pendingView.createdSecret.value, '')
  assert.equal(pendingView.createdName.value, '')
  assert.equal(pendingView.credentials.value.length, 0)
})

test('MCP management API methods use authenticated correct routes and payloads', async () => {
  const apiSource = await readFile(new URL('../src/api.ts', import.meta.url), 'utf8')
  const compiled = ts.transpileModule(apiSource, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText
  const requests = []
  const context = { exports: {}, require: () => ({}), URLSearchParams, FormData,
    fetch: async (url, init) => {
      requests.push({ url, ...clone(init) })
      return { ok: true, status: 200, json: async () => ({}) }
    },
  }
  vm.runInNewContext(compiled, context)
  context.exports.setAuthToken('web-jwt')
  await context.exports.api.mcpTokens()
  await context.exports.api.createMcpToken({ name: 'test', scopes: ['market:read'], expires_in_days: 30 })
  await context.exports.api.revokeMcpToken(12)
  assert.deepEqual(requests.map((item) => item.url), ['/api/mcp-tokens', '/api/mcp-tokens', '/api/mcp-tokens/12'])
  assert.equal(requests[0].cache, 'no-store')
  assert.equal(requests[1].method, 'POST')
  assert.deepEqual(JSON.parse(requests[1].body), { name: 'test', scopes: ['market:read'], expires_in_days: 30 })
  assert.equal(requests[2].method, 'DELETE')
  assert.ok(requests.every((item) => item.headers.Authorization === 'Bearer web-jwt'))
})

test('ordinary users can reach MCP settings without gaining admin settings tabs', async () => {
  const settingsSource = await readFile(new URL('../src/views/SettingsView.vue', import.meta.url), 'utf8')
  const settingsOutput = compileSetup(settingsSource, 'tabs, activeTab, syncTabFromLocation, selectTab')
  function settingsHarness(user, location) {
    const events = new Map()
    const win = { location: new URL(location), addEventListener: (name, fn) => events.set(name, fn), removeEventListener() {} }
    win.history = { pushState: (_state, _title, href) => { win.location = new URL(href, win.location) } }
    const context = { URL, URLSearchParams, window: win, defineProps: () => ({ user }),
      ref: (value) => ({ value }), computed: (fn) => ({ get value() { return fn() } }),
      watch: (_value, fn, options) => { if (options?.immediate) fn() },
      onMounted: (fn) => fn(), onBeforeUnmount() {},
    }
    vm.runInNewContext(settingsOutput, context)
    return { ...context.harness, win, events }
  }
  const viewer = settingsHarness({ role: 'viewer', permissions: ['data:read'] }, 'https://stock.example/settings?tab=users')
  assert.deepEqual(Array.from(viewer.tabs.value, (item) => item.key), ['mcp'])
  assert.equal(viewer.activeTab.value, 'mcp')
  viewer.selectTab('users')
  assert.equal(viewer.activeTab.value, 'mcp')
  const admin = settingsHarness({ role: 'admin', permissions: ['all'] }, 'https://stock.example/settings?tab=mcp')
  assert.equal(admin.activeTab.value, 'mcp')
  admin.selectTab('models')
  assert.equal(admin.win.location.search, '?tab=models')
  admin.win.location = new URL('https://stock.example/settings?tab=mcp')
  admin.events.get('popstate')()
  assert.equal(admin.activeTab.value, 'mcp')
})

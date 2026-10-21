import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'

const bellSource = await readFile(new URL('../src/components/NotificationBell.vue', import.meta.url), 'utf8')
const shellSource = await readFile(new URL('../src/components/AppShell.vue', import.meta.url), 'utf8')
const apiSource = await readFile(new URL('../src/api.ts', import.meta.url), 'utf8')
const linkSource = await readFile(new URL('../src/utils/notifications.ts', import.meta.url), 'utf8')

const setupMatch = bellSource.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)
assert.ok(setupMatch, 'NotificationBell script setup should be present')
const harnessExposure = `
;(globalThis as Record<string, unknown>).__bellHarness = {
  refreshCount, loadList, toggle, close, markRead, toggleItem, openLink, markAll, remove,
  isLong, isExpanded, open, items, unreadCount, unreadOnly, error, badge, triggerLabel,
}
`
const transpiled = ts.transpileModule(`${setupMatch[1]}\n${harnessExposure}`, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  reportDiagnostics: true,
})
assert.equal(transpiled.diagnostics?.length || 0, 0)
const executable = transpiled.outputText
  .split(/\r?\n/)
  .filter((line) => !line.startsWith('import ') && !/^export\s*\{\s*\};?$/.test(line))
  .join('\n')

function notice(id, overrides = {}) {
  return {
    id,
    level: 'success',
    category: 'automatic_selection',
    title: `每日选股（10 月 ${id} 日）：宝新能源等 30 只`,
    content: '**10 月 21 日收盘后选出 30 只。**\n1. **宝新能源（000690.SZ）**',
    link: '/automatic-selection',
    is_read: false,
    created_at: '2026-10-21T09:52:36+00:00',
    read_at: null,
    ...overrides,
  }
}

function createHarness(apiOverrides = {}) {
  const ref = (value) => ({ value })
  const computed = (getter) => Object.defineProperty({}, 'value', { get: getter })
  const mounted = []
  const emitted = []
  const calls = { list: [], read: [], readAll: 0, deleted: [] }
  const api = {
    notifications: async (unreadOnly, limit) => {
      calls.list.push([unreadOnly, limit])
      return { items: [notice(21), notice(20, { is_read: true, level: 'warning' })], unread_count: 3 }
    },
    markNotificationRead: async (id) => {
      calls.read.push(id)
      return { success: true }
    },
    markAllNotificationsRead: async () => {
      calls.readAll += 1
      return { marked: 3 }
    },
    deleteNotification: async (id) => {
      calls.deleted.push(id)
      return { success: true }
    },
    ...apiOverrides,
  }
  const context = {
    api,
    ref,
    computed,
    nextTick: (callback) => Promise.resolve().then(callback),
    onMounted: (callback) => mounted.push(callback),
    onBeforeUnmount: () => {},
    defineEmits: () => (...args) => emitted.push(args),
    beijingFromDb: (value) => value,
    renderMarkdown: (value) => value,
    window: { setInterval: () => 1, clearInterval: () => {} },
    document: { visibilityState: 'visible', addEventListener: () => {}, removeEventListener: () => {} },
  }
  vm.runInNewContext(executable, context)
  return { harness: context.__bellHarness, mounted, emitted, calls }
}

test('the bell is mounted in the top bar and talks to the inbox endpoints', () => {
  assert.match(shellSource, /<NotificationBell @open-link="openNotificationLink" \/>/)
  assert.match(apiSource, /\/api\/notifications\?unread_only=\$\{unreadOnly\}&limit=\$\{limit\}/)
  assert.match(apiSource, /\/api\/notifications\/\$\{id\}\/read`, \{ method: 'POST' \}/)
  assert.match(apiSource, /'\/api\/notifications\/read-all', \{ method: 'POST' \}/)
  assert.match(apiSource, /`\/api\/notifications\/\$\{id\}`, \{ method: 'DELETE' \}/)
})

test('message links map to app pages and unknown links do not navigate', () => {
  const output = ts.transpileModule(linkSource, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText
  const sandbox = { exports: {} }
  vm.runInNewContext(output, sandbox)
  const { pageForNotificationLink } = sandbox.exports
  assert.equal(pageForNotificationLink('/automatic-selection'), 'automaticSelection')
  assert.equal(pageForNotificationLink('/Automatic-Selection/'), 'automaticSelection')
  assert.equal(pageForNotificationLink('/industries'), 'industryAnalysis')
  assert.equal(pageForNotificationLink('https://evil.example/automatic-selection'), null)
  assert.equal(pageForNotificationLink(null), null)
})

test('mounting fetches the unread count for the badge', async () => {
  const { harness, mounted, calls } = createHarness({
    notifications: async (unreadOnly, limit) => {
      calls.list.push([unreadOnly, limit])
      return { items: [], unread_count: 120 }
    },
  })
  for (const callback of mounted) callback()
  await new Promise((resolve) => setImmediate(resolve))
  assert.deepEqual([...calls.list[0]], [true, 1])
  assert.equal(harness.unreadCount.value, 120)
  assert.equal(harness.badge.value, '99+')
  assert.equal(harness.triggerLabel.value, '站内消息，120 条未读')
})

test('opening the panel loads messages and reading one updates the badge', async () => {
  const { harness, calls } = createHarness()
  await harness.toggle()
  assert.equal(harness.open.value, true)
  assert.deepEqual([...calls.list[0]], [false, 50])
  assert.equal(harness.items.value.length, 2)
  assert.equal(harness.unreadCount.value, 3)

  const [unread, read] = harness.items.value
  await harness.markRead(read)
  assert.deepEqual(calls.read, [])
  await harness.toggleItem(unread)
  assert.equal(harness.isExpanded(unread.id), true)
  assert.equal(unread.is_read, true)
  assert.deepEqual(calls.read, [21])
  assert.equal(harness.unreadCount.value, 2)
})

test('a failed read is rolled back and explained', async () => {
  const { harness } = createHarness({
    markNotificationRead: async () => {
      throw new Error('网络不通')
    },
  })
  await harness.loadList()
  const item = harness.items.value[0]
  await harness.markRead(item)
  assert.equal(item.is_read, false)
  assert.equal(harness.unreadCount.value, 3)
  assert.equal(harness.error.value, '网络不通')
})

test('following a link marks it read, emits the link and closes the panel', async () => {
  const { harness, emitted, calls } = createHarness()
  await harness.toggle()
  await harness.openLink(harness.items.value[0])
  assert.deepEqual(calls.read, [21])
  assert.deepEqual(emitted, [['openLink', '/automatic-selection']])
  assert.equal(harness.open.value, false)
})

test('mark all read and delete keep the badge in sync', async () => {
  const { harness, calls } = createHarness()
  await harness.loadList()
  await harness.remove(harness.items.value[0])
  assert.deepEqual(calls.deleted, [21])
  assert.equal(harness.items.value.length, 1)
  assert.equal(harness.unreadCount.value, 2)

  harness.unreadOnly.value = true
  await harness.markAll()
  assert.equal(calls.readAll, 1)
  assert.equal(harness.unreadCount.value, 0)
  assert.equal(harness.items.value.length, 0)
})

test('long digests are collapsed until expanded', () => {
  const { harness } = createHarness()
  assert.equal(harness.isLong(notice(1, { content: '短消息' })), false)
  assert.equal(harness.isLong(notice(1, { content: 'a\nb\nc\nd' })), true)
  assert.equal(harness.isLong(notice(1, { content: '字'.repeat(121) })), true)
})

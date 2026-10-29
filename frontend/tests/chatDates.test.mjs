import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

const server = await createServer({
  root: fileURLToPath(new URL('../', import.meta.url)),
  appType: 'custom',
  optimizeDeps: { noDiscovery: true },
  server: { middlewareMode: true, hmr: false }
})
const { chatDateStamp, groupSessionsByDate, messageDateTimeline } = await server.ssrLoadModule('/src/utils/chatDates.ts')
await server.close()

const now = Date.parse('2026-10-29T09:00:00+08:00')

test('chat dates normalize legacy UTC and offset timestamps to the same Beijing date and time', () => {
  const expected = chatDateStamp('2026-10-29T00:01:02+08:00', now)
  assert.deepEqual(chatDateStamp('2026-10-28 16:01:02', now), expected)
  assert.deepEqual(chatDateStamp('2026-10-28T16:01:02Z', now), expected)
  assert.equal(expected.key, '2026-10-29')
  assert.equal(expected.time, '00:01')
  assert.equal(expected.label, '今天 · 10月29日 周四')
  assert.equal(expected.fullLabel, '2026-10-29 00:01:02 周四（北京时间）')
})

test('labels change at Beijing midnight and include years across a calendar year', () => {
  const date = '2026-10-28T23:59:59+08:00'
  assert.equal(chatDateStamp(date, Date.parse(date)).label, '今天 · 10月28日 周三')
  assert.equal(chatDateStamp(date, now).label, '昨天 · 10月28日 周三')
  assert.equal(chatDateStamp('2026-10-27T15:00:00+08:00', now).label, '10月27日 周二')
  assert.equal(chatDateStamp('2025-12-31T23:00:00+08:00', Date.parse('2026-01-01T00:00:00+08:00')).label, '昨天 · 2025年12月31日 周三')
})

test('display does not follow the browser or process timezone', () => {
  const original = process.env.TZ
  try {
    process.env.TZ = 'America/Los_Angeles'
    assert.equal(chatDateStamp('2026-10-28T16:15:00Z', now).time, '00:15')
    assert.equal(chatDateStamp('2026-10-28T16:15:00Z', now).label, '今天 · 10月29日 周四')
  } finally {
    if (original === undefined) delete process.env.TZ
    else process.env.TZ = original
  }
})

test('missing and malformed dates stay unknown rather than appearing as today', () => {
  for (const value of [undefined, null, '', ' ', 'not-a-date']) {
    assert.deepEqual(chatDateStamp(value, now), {
      key: 'unknown', iso: undefined, timestamp: null,
      time: '时间未知', label: '日期未知', fullLabel: '未记录时间'
    })
  }
})

test('session groups use latest activity, stable tie breaks and unknown dates last without mutating input', () => {
  const items = [
    { id: 1, updated_at: '2026-10-28T23:59:59+08:00' },
    { id: 2, updated_at: null },
    { id: 3, updated_at: '2026-10-29T01:01:01+08:00' },
    { id: 4, updated_at: '2026-10-29T01:01:02+08:00' },
    { id: 5, updated_at: '2026-10-29T01:01:02+08:00' },
    { id: 6, created_at: '2026-10-27T10:00:00+08:00', updated_at: 'invalid' }
  ]
  const groups = groupSessionsByDate(items, now)
  assert.deepEqual(groups.map((group) => group.key), ['2026-10-29', '2026-10-28', '2026-10-27', 'unknown'])
  assert.deepEqual(groups[0].entries.map((entry) => entry.item.id), [5, 4, 3])
  assert.equal(groups[2].entries[0].stamp.time, '10:00')
  assert.deepEqual(items.map((item) => item.id), [1, 2, 3, 4, 5, 6])
})

test('message dates insert one divider per adjacent date and retain conversation order', () => {
  const messages = [
    { id: 10, created_at: '2026-10-28T15:59:59Z' },
    { id: 11, created_at: '2026-10-28T16:00:01Z' },
    { id: 12, created_at: '2026-10-29T00:02:00+08:00' },
    { id: 13, created_at: null },
    { id: 14, created_at: null },
    { id: 15, created_at: '2026-10-29T00:03:00+08:00' }
  ]
  const timeline = messageDateTimeline(messages, now)
  assert.deepEqual(timeline.map((row) => row.item.id), [10, 11, 12, 13, 14, 15])
  assert.deepEqual(timeline.map((row) => row.showDate), [true, true, false, true, false, true])
  assert.equal(timeline[0].stamp.label, '昨天 · 10月28日 周三')
  assert.equal(timeline[1].stamp.time, '00:00')
  assert.deepEqual(messageDateTimeline([], now), [])
})

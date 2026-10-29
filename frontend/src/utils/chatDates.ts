import { timestampFromDb } from './datetime'

const dayMilliseconds = 86_400_000
const beijingOffset = 8 * 60 * 60 * 1000
const weekdays = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']
const calendarFormat = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'Asia/Shanghai',
  year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23'
})

export type ChatDateStamp = {
  key: string
  iso: string | undefined
  timestamp: number | null
  time: string
  label: string
  fullLabel: string
}

/** 日期分界始终以北京时间零点为准，不受浏览器所在时区影响。 */
export function chatDateStamp(value: string | null | undefined, now = Date.now()): ChatDateStamp {
  const timestamp = timestampFromDb(value)
  if (timestamp === null) {
    return { key: 'unknown', iso: undefined, timestamp: null, time: '时间未知', label: '日期未知', fullLabel: '未记录时间' }
  }
  const date = new Date(timestamp)
  const parts = calendarFormat.formatToParts(date)
  const part = (name: string) => parts.find((item) => item.type === name)?.value || ''
  const key = `${part('year')}-${part('month')}-${part('day')}`
  const day = Math.floor((timestamp + beijingOffset) / dayMilliseconds)
  const today = Math.floor((now + beijingOffset) / dayMilliseconds)
  const weekday = weekdays[new Date(`${key}T00:00:00Z`).getUTCDay()]
  const currentYear = calendarFormat.formatToParts(new Date(now)).find((item) => item.type === 'year')?.value
  const year = part('year') === currentYear ? '' : `${part('year')}年`
  const dateLabel = `${year}${Number(part('month'))}月${Number(part('day'))}日 ${weekday}`
  const relative = day === today ? '今天 · ' : day === today - 1 ? '昨天 · ' : ''
  const time = `${part('hour')}:${part('minute')}`
  return {
    key, iso: date.toISOString(), timestamp, time,
    label: relative + dateLabel,
    fullLabel: `${key} ${time}:${part('second')} ${weekday}（北京时间）`
  }
}

type SessionDates = { id: number; updated_at?: string | null; created_at?: string | null }

/** 按最后活动时间倒序排列；未知时间置底，不改动接口返回的数组。 */
export function groupSessionsByDate<T extends SessionDates>(items: T[], now = Date.now()) {
  const entries = items.map((item) => {
    const stamp = chatDateStamp(item.updated_at, now)
    return { item, stamp: stamp.timestamp === null ? chatDateStamp(item.created_at, now) : stamp }
  }).sort((a, b) => {
    if (a.stamp.timestamp === null) return b.stamp.timestamp === null ? b.item.id - a.item.id : 1
    if (b.stamp.timestamp === null) return -1
    return b.stamp.timestamp - a.stamp.timestamp || b.item.id - a.item.id
  })
  const groups: Array<{ key: string; label: string; entries: typeof entries }> = []
  for (const entry of entries) {
    let group = groups[groups.length - 1]
    if (!group || group.key !== entry.stamp.key) {
      group = { key: entry.stamp.key, label: entry.stamp.label, entries: [] }
      groups.push(group)
    }
    group.entries.push(entry)
  }
  return groups
}

/** 消息沿用服务器/流式接收顺序，仅在首条和日期变化时添加分隔。 */
export function messageDateTimeline<T extends { created_at?: string | null }>(items: T[], now = Date.now()) {
  let previousDay: string | null = null
  return items.map((item) => {
    const stamp = chatDateStamp(item.created_at, now)
    const showDate = stamp.key !== previousDay
    previousDay = stamp.key
    return { item, stamp, showDate }
  })
}

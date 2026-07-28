/**
 * 时间统一按东八区（北京时间 Asia/Shanghai）显示，不随浏览器时区漂移。
 *
 * 接口时间契约：
 * - 新接口必须返回 ISO 8601，并显式携带 Z 或时区偏移。
 * - 历史 DB 裸时间串按 UTC 兼容，避免被浏览器当成本地时间。
 * - 北京墙钟时间由后端序列化为 +08:00，前端不再按字段名猜测时区。
 */

const _fmt = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false
})

function _render(d: Date): string {
  if (isNaN(d.getTime())) return '-'
  const parts = _fmt.formatToParts(d)
  const g = (t: string) => parts.find((p) => p.type === t)?.value || ''
  return `${g('year')}-${g('month')}-${g('day')} ${g('hour')}:${g('minute')}`
}

/** epoch 秒 → 北京时间字符串（YYYY-MM-DD HH:mm）。 */
export function beijingFromEpoch(sec: number): string {
  return _render(new Date(sec * 1000))
}

const DB_TIMESTAMP_RE =
  /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?$/

function _parseDbTimestamp(value: string): Date {
  const hasTz = /[zZ]$|[+-]\d\d:?\d\d$/.test(value)
  if (hasTz) {
    const iso = value.replace(/[ ]+/, 'T').replace(/([+-]\d\d)(\d\d)$/, '$1:$2')
    return new Date(iso)
  }

  const match = value.match(DB_TIMESTAMP_RE)
  if (!match) return new Date(Number.NaN)
  const milliseconds = Number((match[7] || '').padEnd(3, '0').slice(0, 3))
  return new Date(Date.UTC(
    Number(match[1]),
    Number(match[2]) - 1,
    Number(match[3]),
    Number(match[4]),
    Number(match[5]),
    Number(match[6] || 0),
    milliseconds
  ))
}

/** ISO 时间 → 北京时间；历史无时区 DB 字符串按 UTC 兼容。 */
export function beijingFromDb(value: string | null | undefined): string {
  if (!value) return '-'
  const s = String(value).trim()
  return _render(_parseDbTimestamp(s))
}

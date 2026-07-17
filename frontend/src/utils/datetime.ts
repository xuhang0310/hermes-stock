/**
 * 时间统一按东八区（北京时间 Asia/Shanghai）显示，不随浏览器时区漂移。
 *
 * 两类来源分开处理：
 * - epoch 秒（绝对时间，如文件 st_mtime）：直接按北京时区格式化。
 * - 后端 DB 裸时间串（无时区后缀）：生产 MySQL 跑在 UTC，存的是 UTC 墙钟时间，
 *   补 'Z' 当 UTC 解析后再转北京时区，避免被当作浏览器本地时间而慢 8 小时。
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

/** 后端 DB 裸时间串（当作 UTC）→ 北京时间字符串。已带时区后缀的原样解析。 */
export function beijingFromDb(value: string | null | undefined): string {
  if (!value) return '-'
  const s = String(value).trim()
  const hasTz = /[zZ]$|[+-]\d\d:?\d\d$/.test(s)
  const iso = hasTz ? s : s.replace(' ', 'T') + 'Z'
  return _render(new Date(iso))
}

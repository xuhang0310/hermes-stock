export function elapsedStreamMilliseconds(startedAt: number | undefined, finishedAt = Date.now()) {
  if (typeof startedAt !== 'number' || !Number.isFinite(startedAt)) return undefined
  return Math.max(0, Math.round(finishedAt - startedAt))
}

export function formatStreamDuration(durationMs: number) {
  const totalSeconds = Math.max(0, durationMs) / 1000
  if (totalSeconds < 10) return `${Math.max(0.1, totalSeconds).toFixed(1)} 秒`
  if (totalSeconds < 60) return `${Math.round(totalSeconds)} 秒`

  const roundedSeconds = Math.round(totalSeconds)
  const hours = Math.floor(roundedSeconds / 3600)
  const minutes = Math.floor((roundedSeconds % 3600) / 60)
  const seconds = roundedSeconds % 60
  if (hours) return `${hours} 小时 ${minutes} 分`
  return `${minutes} 分 ${seconds} 秒`
}

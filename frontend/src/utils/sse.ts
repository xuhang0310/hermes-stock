type StreamEvent = {
  type?: string
}

function parseEvent<T>(raw: string): T | null {
  const dataLines = raw
    .split(/\r?\n/)
    .filter((line) => line.startsWith('data:'))
    .map((line) => line.slice(5).trimStart())
  if (!dataLines.length) return null
  return JSON.parse(dataLines.join('\n')) as T
}

export async function readSseStream<T extends StreamEvent>(
  body: ReadableStream<Uint8Array>,
  onEvent: (event: T) => void
) {
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let terminalEventReceived = false

  const consumeBufferedEvents = () => {
    const frames = buffer.split(/\r?\n\r?\n/)
    buffer = frames.pop() || ''
    for (const raw of frames) {
      const event = parseEvent<T>(raw)
      if (!event) continue
      onEvent(event)
      if (event.type === 'done') {
        terminalEventReceived = true
        return true
      }
    }
    return false
  }

  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) {
        buffer += decoder.decode()
        if (buffer.trim()) {
          const event = parseEvent<T>(buffer)
          if (event) onEvent(event)
        }
        return
      }
      buffer += decoder.decode(value, { stream: true })
      if (consumeBufferedEvents()) return
    }
  } finally {
    if (terminalEventReceived) {
      // Some browsers wait for the server connection to close before resolving
      // cancel(). A terminal SSE event is authoritative, so cleanup must not
      // keep the caller's busy state locked.
      void reader.cancel().catch(() => undefined)
    }
    reader.releaseLock()
  }
}

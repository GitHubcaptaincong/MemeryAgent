const retryDelays = [1000, 2000, 4000, 8000, 15000]

export const runEventTypes = [
  'run.created', 'run.state_changed', 'source.loaded', 'memory.retrieved',
  'skills.selected', 'agent.plan_created', 'agent.decision', 'tool.started',
  'tool.completed', 'tool.failed', 'draft.created', 'run.failed',
  'memory.candidate_created', 'checkpoint.created', 'run.retryable_error',
  'run.retry_scheduled', 'run.recovery_started', 'run.expired_lease_recovered',
  'review.cards_created',
]

// A transport failure must never cancel a persisted server task.
export function subscribeRunEvents({
  url, afterSeq = 0, onEvent, onPulse, onComplete, onConnectionChange,
  EventSourceImpl = globalThis.EventSource,
  setTimer = globalThis.setTimeout, clearTimer = globalThis.clearTimeout,
}) {
  let source = null
  let timer = null
  let cursor = afterSeq
  let failures = 0
  let stopped = false

  function disconnect() {
    source?.close()
    source = null
  }

  function retry() {
    if (stopped) return
    disconnect()
    if (failures >= retryDelays.length) {
      onConnectionChange('paused')
      return
    }
    const delay = retryDelays[failures++]
    onConnectionChange('reconnecting')
    timer = setTimer(connect, delay)
  }

  function connect() {
    if (stopped) return
    timer = null
    onConnectionChange(failures ? 'reconnecting' : 'connecting')
    let current
    try {
      const separator = url.includes('?') ? '&' : '?'
      current = new EventSourceImpl(`${url}${separator}after_seq=${cursor}`)
      source = current
    } catch {
      retry()
      return
    }
    const active = () => !stopped && source === current
    const healthy = () => {
      failures = 0
      onConnectionChange('connected')
    }
    runEventTypes.forEach((type) => {
      current.addEventListener(type, (message) => {
        if (!active()) return
        let item
        try { item = JSON.parse(message.data) } catch { retry(); return }
        if (!Number.isInteger(item?.seq) || item.seq <= 0 || item.event_type !== type) {
          retry()
          return
        }
        if (item.seq <= cursor) return
        onEvent(item)
        cursor = item.seq
        healthy()
      })
    })
    current.addEventListener('progress.pulse', (message) => {
      if (!active()) return
      let pulse
      try { pulse = JSON.parse(message.data) } catch { retry(); return }
      if (!pulse || typeof pulse !== 'object') { retry(); return }
      onPulse(pulse)
      healthy()
    })
    current.addEventListener('stream.closed', async () => {
      if (!active()) return
      disconnect()
      try {
        await onComplete()
        if (!stopped) {
          stopped = true
          onConnectionChange('idle')
        }
      } catch {
        retry()
      }
    })
    current.onerror = () => { if (active()) retry() }
  }

  connect()
  return {
    close() {
      stopped = true
      if (timer !== null) clearTimer(timer)
      disconnect()
    },
  }
}

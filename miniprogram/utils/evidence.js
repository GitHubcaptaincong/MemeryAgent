function openEvidence(evidence, sourceId) {
  if (!Array.isArray(evidence) || !evidence.length) return
  wx.navigateTo({
    url: '/pages/evidence/evidence',
    success: ({ eventChannel }) => eventChannel.emit('evidence', {
      evidence: evidence.map((item) => ({ ...item, source_id: item.source_id || sourceId || null })),
    }),
    fail: () => wx.showToast({ title: '无法打开原文，请重试', icon: 'none' }),
  })
}

// Backend offsets count Unicode code points, not JavaScript UTF-16 code units.
function locateEvidence(content, evidence) {
  const chars = Array.from(content || '')
  const { start_char: start, end_char: end, quote } = evidence
  if (!Number.isInteger(start) || !Number.isInteger(end) || start < 0
    || end <= start || end > chars.length || !quote
    || chars.slice(start, end).join('') !== quote) {
    return { located: false, before: content || '', quote: '', after: '' }
  }
  return {
    located: true,
    before: chars.slice(0, start).join(''),
    quote: chars.slice(start, end).join(''),
    after: chars.slice(end).join(''),
  }
}

module.exports = { openEvidence, locateEvidence }

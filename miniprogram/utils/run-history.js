const { eventLabel, formatClock } = require('./format')

function processHistory(run, events) {
  const has = (type) => events.some((event) => event.event_type === type)
  const validations = events.filter((event) => event.event_type === 'tool.completed'
    && event.payload && event.payload.tool === 'schema_validate')
  const lastValidation = validations[validations.length - 1]
  const valid = lastValidation && lastValidation.payload.result_summary
    && lastValidation.payload.result_summary.valid === true
  const confirmed = ['confirmed', 'curating_memory', 'completed'].includes(run.state)
  return {
    stages: [
      { title: '读取材料', status: has('source.loaded') ? '已记录' : '暂无记录' },
      { title: '生成知识点', status: has('draft.created') ? '已生成' : '暂无生成记录' },
      { title: '校验证据', status: lastValidation ? (valid ? '校验通过' : '未通过校验') : '暂无校验记录' },
      { title: '等待确认', status: confirmed ? '已确认' : run.state === 'awaiting_user' ? '待你确认' : '尚未到达' },
    ],
    events: events.map((event) => {
      const payload = event.payload || {}
      const result = payload.result_summary || {}
      const details = []
      if (payload.tool) details.push(`工具：${payload.tool}`)
      if (payload.tool === 'source_locate_quotes' && event.event_type === 'tool.completed') {
        details.push(`请求 ${result.requested_count || 0} 条，定位 ${result.resolved_count || 0} 条，未定位 ${result.unresolved_count || 0} 条`)
      }
      if (payload.tool === 'schema_validate' && event.event_type === 'tool.completed') {
        details.push(`知识点 ${result.unit_count || 0} 个，校验问题 ${result.error_count || 0} 个`)
      }
      return { ...event, label: eventLabel(event), clock: formatClock(event.created_at), details }
    }),
  }
}
module.exports = { processHistory }

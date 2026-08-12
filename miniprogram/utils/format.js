const stateLabels = {
  queued: '等待开始',
  ingesting: '正在读取材料',
  retrieving_memory: '正在检索记忆',
  routing_skills: '正在选择技能',
  planning: '正在制定计划',
  executing: '正在调用工具',
  drafting: '正在生成知识卡',
  reviewing: '正在校验结果',
  retry_wait: '模型繁忙，等待重试',
  awaiting_user: '等待你的确认',
  confirmed: '已确认',
  completed: '已完成',
  failed: '处理失败',
  budget_exhausted: '达到运行预算',
  cancelled: '已取消',
}

function stateLabel(state) {
  return stateLabels[state] || state || '准备中'
}

function eventLabel(event) {
  const payload = event.payload || {}
  if (payload.message) return payload.message
  if (payload.summary) return payload.summary
  const labels = {
    'run.created': '任务已创建',
    'source.loaded': '已读取原始材料',
    'memory.retrieved': '已完成相关记忆检索',
    'skills.selected': '已选择处理技能',
    'agent.plan_created': '已制定处理计划',
    'tool.started': '正在调用处理工具',
    'tool.completed': '工具调用完成',
    'draft.created': '知识草稿已经生成',
    'checkpoint.created': '已保存可恢复进度',
    'run.retryable_error': '模型服务暂时异常',
    'run.retry_scheduled': '已安排自动重试',
    'review.cards_created': '已创建复习卡',
    'memory.candidate_created': '已生成待审批的长期记忆候选',
  }
  return labels[event.event_type] || event.event_type
}

function formatClock(value) {
  if (!value) return ''
  const date = new Date(value)
  const hours = String(date.getHours()).padStart(2, '0')
  const minutes = String(date.getMinutes()).padStart(2, '0')
  return `${hours}:${minutes}`
}

module.exports = { stateLabel, eventLabel, formatClock }

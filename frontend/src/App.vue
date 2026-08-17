<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { publicDemo } from './demoData.js'

const apiBase = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000'
const isPublicDemo = import.meta.env.VITE_PUBLIC_DEMO === 'true'
const demoReadOnlyMessage = '公开演示模式不会连接后端、写入数据或调用模型。'
const sourceMaxChars = 50000
const sourceMode = ref('text')
const form = ref({
  title: '',
  learning_goal: '',
  content: '',
  url: '',
  content_type: 'markdown',
  web_access_allowed: false,
})
const busy = ref(false)
const error = ref('')
const run = ref(null)
const events = ref([])
const draft = ref(null)
const memoryCandidates = ref([])
const savedSource = ref(null)
const livePulse = ref(null)
const startedAt = ref(null)
const elapsedSeconds = ref(0)
const lastSignalAt = ref(null)
const activeView = ref('organize')
const reviewQueue = ref([])
const activeReviewCard = ref(null)
const reviewAnswer = ref('')
const reviewAnswerResult = ref(null)
const reviewBusy = ref(false)
const reviewError = ref('')
const reviewedThisSession = ref(0)
const reviewOverview = ref({ due_count: 0, total_active: 0, next_due_at: null })
const reviewHistory = ref([])
const reviewAnswerKey = ref(null)
const reviewSubmittedAnswer = ref('')
const reviewRatingKey = ref(null)
const reviewRatingValue = ref(null)
const reviewRatingAcknowledged = ref(false)
const reviewQueueMode = ref('today')
const dailyPlan = ref(null)
const insights = ref(null)
const evaluationPollCount = ref(0)
const reminder = ref({
  enabled: true,
  preferred_time: '20:00',
  daily_limit: 10,
  overdue_enabled: true,
  ai_evaluation_enabled: true,
  timezone: 'Asia/Shanghai',
})
const subscriptionStatus = ref(null)
const reminderBusy = ref(false)
const reminderSaved = ref(false)
let eventSource = null
let clock = null
let evaluationTimer = null

const stateLabels = {
  queued: '排队中',
  ingesting: '读取材料',
  retrieving_memory: '检索记忆',
  routing_skills: '选择技能',
  planning: '制定计划',
  executing: '调用工具',
  drafting: '生成草稿',
  reviewing: '校验草稿',
  retry_wait: '等待重试',
  awaiting_user: '等待确认',
  confirmed: '已确认',
  completed: '已完成',
  failed: '执行失败',
  budget_exhausted: '达到预算',
  cancelled: '已取消',
}

const charCount = computed(() => form.value.content.length)
const sourceReady = computed(() => sourceMode.value === 'url'
  ? Boolean(form.value.url.trim())
  : Boolean(form.value.content.trim()) && charCount.value <= sourceMaxChars)
const progress = computed(() => {
  const states = ['queued', 'ingesting', 'retrieving_memory', 'routing_skills', 'planning', 'executing', 'drafting', 'reviewing', 'awaiting_user']
  const index = states.indexOf(run.value?.state)
  if (!run.value && busy.value) return 4
  return index < 0 ? (run.value?.state === 'completed' ? 100 : 0) : Math.round(((index + 1) / states.length) * 100)
})
const processingMode = computed(() => {
  const plan = events.value.find((item) => item.event_type === 'agent.plan_created')
  if (!plan) return '正在判断处理通道'
  return plan?.payload?.processing_mode === 'quick' ? '短内容快速通道' : '完整 Agent 通道'
})
const currentActivity = computed(() => {
  if (livePulse.value?.message) return livePulse.value.message
  const latest = events.value[events.value.length - 1]
  return latest ? eventTitle(latest) : (busy.value ? '请求已收到，正在创建任务' : '等待提交材料')
})
const signalAge = computed(() => {
  if (!lastSignalAt.value) return 0
  return Math.max(0, Math.floor((Date.now() - lastSignalAt.value) / 1000))
})
const dueCount = computed(() => reviewOverview.value.due_count ?? reviewQueue.value.length)
const plannedCardIds = computed(() => new Set((dailyPlan.value?.planned_cards || []).map((card) => card.id)))
const visibleReviewQueue = computed(() => {
  if (reviewQueueMode.value === 'all' || !dailyPlan.value) return reviewQueue.value
  return reviewQueue.value.filter((card) => plannedCardIds.value.has(card.id))
})
const todayCount = computed(() => dailyPlan.value?.planned_count ?? visibleReviewQueue.value.length)
const answerEvaluation = computed(() => reviewAnswerResult.value?.evaluation || null)
const evaluationStatus = computed(() => reviewAnswerResult.value?.evaluation_status || 'disabled')
const masteryPercent = computed(() => Math.round((insights.value?.summary?.self_rated_mastery_rate || 0) * 100))
const trendPoints = computed(() => (insights.value?.trend?.daily || []).slice(-7))
const maxTrendValue = computed(() => Math.max(1, ...trendPoints.value.map((point) => point.completed_count || 0)))

async function api(path, options = {}) {
  if (isPublicDemo) throw new Error(demoReadOnlyMessage)
  const response = await fetch(`${apiBase}${path}`, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
  })
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}))
    const detail = payload.detail
    throw new Error(
      typeof detail === 'string'
        ? detail
        : (detail?.message || `请求失败（${response.status}）`),
    )
  }
  return response.json()
}

async function startRun() {
  error.value = ''
  if (isPublicDemo) {
    error.value = demoReadOnlyMessage
    return
  }
  if (!sourceReady.value) {
    error.value = sourceMode.value === 'url'
      ? '请先填写一个公开的 HTTP 或 HTTPS 链接。'
      : `请输入材料正文，且不要超过 ${sourceMaxChars.toLocaleString()} 字。`
    return
  }
  busy.value = true
  run.value = null
  events.value = []
  draft.value = null
  memoryCandidates.value = []
  savedSource.value = null
  startedAt.value = Date.now()
  elapsedSeconds.value = 0
  lastSignalAt.value = Date.now()
  livePulse.value = { message: '请求已收到，正在保存材料并创建任务' }
  closeStream()
  startClock()
  try {
    const saveStarted = performance.now()
    const sourcePayload = sourceMode.value === 'url'
      ? {
          url: form.value.url.trim(),
          title: form.value.title.trim() || null,
          learning_goal: form.value.learning_goal.trim() || '准确整理并记住这个公开链接的内容',
          web_access_allowed: form.value.web_access_allowed,
        }
      : {
          title: form.value.title.trim() || '快速记录',
          learning_goal: form.value.learning_goal.trim() || '准确整理并记住这段内容',
          content: form.value.content,
          content_type: form.value.content_type,
          web_access_allowed: form.value.web_access_allowed,
        }
    const source = await api(sourceMode.value === 'url' ? '/api/v1/sources/from-url' : '/api/v1/sources', {
      method: 'POST',
      body: JSON.stringify(sourcePayload),
    })
    savedSource.value = {
      ...source,
      save_ms: Math.max(1, Math.round(performance.now() - saveStarted)),
    }
    livePulse.value = { message: source.origin_type === 'url' ? '网页正文已解析并保存，AI 正在后台整理' : '原文已经保存，AI 正在后台整理' }
    lastSignalAt.value = Date.now()
    run.value = await api('/api/v1/runs', {
      method: 'POST',
      body: JSON.stringify({
        source_id: source.id,
        idempotency_key: `web-${source.id}`,
      }),
    })
    livePulse.value = { message: '任务已创建，正在连接实时运行轨迹' }
    lastSignalAt.value = Date.now()
    openStream(run.value.id)
  } catch (reason) {
    error.value = reason.message
    busy.value = false
    stopClock()
  }
}

function openStream(runId) {
  eventSource = new EventSource(`${apiBase}/api/v1/runs/${runId}/events/stream`)
  const eventTypes = [
    'run.created', 'run.state_changed', 'source.loaded', 'memory.retrieved',
    'skills.selected', 'agent.plan_created', 'agent.decision', 'tool.started',
    'tool.completed', 'tool.failed', 'draft.created', 'run.failed',
    'memory.candidate_created', 'checkpoint.created', 'run.retryable_error',
    'run.retry_scheduled', 'run.recovery_started', 'run.expired_lease_recovered',
    'review.cards_created',
  ]
  eventTypes.forEach((eventType) => {
    eventSource.addEventListener(eventType, (message) => consumeEvent(message))
  })
  eventSource.addEventListener('progress.pulse', (message) => consumePulse(message))
  eventSource.addEventListener('stream.closed', async () => {
    await refreshRun()
    if (run.value?.state === 'awaiting_user') await loadDraft()
    busy.value = false
    stopClock()
    closeStream()
  })
  eventSource.onerror = async () => {
    await refreshRun().catch(() => {})
    if (run.value?.state === 'awaiting_user') await loadDraft().catch(() => {})
    if (['awaiting_user', 'completed', 'failed', 'cancelled', 'budget_exhausted'].includes(run.value?.state)) {
      busy.value = false
      stopClock()
      closeStream()
    }
  }
}

function consumeEvent(message) {
  const item = JSON.parse(message.data)
  if (!events.value.some((existing) => existing.seq === item.seq)) events.value.push(item)
  livePulse.value = { message: eventTitle(item) }
  lastSignalAt.value = Date.now()
  if (item.event_type === 'run.state_changed') {
    run.value = { ...run.value, state: item.payload.state }
  }
  if (item.event_type === 'draft.created') loadDraft().catch(() => {})
}

function consumePulse(message) {
  livePulse.value = JSON.parse(message.data)
  lastSignalAt.value = Date.now()
}

function eventTitle(item) {
  const payload = item.payload || {}
  if (payload.message) return payload.message
  if (payload.summary) return payload.summary
  const labels = {
    'run.created': '任务已持久化，准备进入 Agent 流程',
    'source.loaded': `已读取 ${payload.char_count || 0} 字材料`,
    'memory.retrieved': `已检索 ${payload.count || 0} 条已批准记忆`,
    'skills.selected': `已选择 ${payload.skills?.length || 0} 个处理技能`,
    'agent.plan_created': payload.processing_mode === 'quick' ? '已选择短内容快速通道' : '已制定完整 Agent 计划',
    'tool.started': `正在调用 ${toolLabel(payload.tool)}`,
    'tool.failed': `${toolLabel(payload.tool)}执行失败：${payload.error || '未知错误'}`,
    'draft.created': `已生成 ${payload.unit_count || 0} 个知识单元，等待审阅`,
    'checkpoint.created': '已保存可恢复的运行摘要',
    'run.retryable_error': '模型服务暂时异常，准备自动重试',
    'run.retry_scheduled': `已安排第 ${payload.attempt || '?'} 次尝试`,
    'run.recovery_started': '正在从持久化业务状态安全恢复',
    'run.expired_lease_recovered': '原 Worker 租约已过期，新 Worker 已接管',
    'review.cards_created': `已创建 ${payload.card_count ?? payload.count ?? 0} 张复习卡，可以开始练习`,
    'memory.candidate_created': '已生成待你审批的长期记忆候选',
  }
  if (item.event_type === 'tool.completed') return toolResultTitle(payload)
  return labels[item.event_type] || item.event_type
}

function toolLabel(name) {
  return ({
    source_read: '材料读取工具',
    source_locate_quotes: '原文证据定位工具',
    schema_validate: '草稿校验工具',
  })[name] || name || '工具'
}

function toolResultTitle(payload) {
  const result = payload.result_summary || {}
  if (payload.tool === 'source_read') return `材料读取完成，共 ${result.char_count || 0} 字`
  if (payload.tool === 'source_locate_quotes') return `证据定位完成：${result.resolved_count || 0} 条成功，${result.unresolved_count || 0} 条待修正`
  if (payload.tool === 'schema_validate') return result.valid ? '草稿结构与证据校验通过' : `草稿校验发现 ${result.error_count || 0} 个问题，模型将修正`
  return `${toolLabel(payload.tool)}执行完成`
}

function startClock() {
  stopClock()
  clock = window.setInterval(() => {
    if (startedAt.value) elapsedSeconds.value = Math.floor((Date.now() - startedAt.value) / 1000)
  }, 500)
}

function stopClock() {
  if (clock) window.clearInterval(clock)
  clock = null
  if (startedAt.value) elapsedSeconds.value = Math.floor((Date.now() - startedAt.value) / 1000)
}

function formatElapsed(seconds) {
  if (seconds < 60) return `${seconds} 秒`
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`
}

function closeStream() {
  eventSource?.close()
  eventSource = null
}

async function refreshRun() {
  if (!run.value?.id) return
  run.value = await api(`/api/v1/runs/${run.value.id}`)
}

async function refreshVisibleEvents() {
  if (!run.value?.id) return
  const afterSeq = events.value.reduce((latest, item) => Math.max(latest, item.seq || 0), 0)
  const newEvents = await api(`/api/v1/runs/${run.value.id}/events?after_seq=${afterSeq}`)
  if (newEvents.length) {
    events.value.push(...newEvents.filter((item) => !events.value.some((existing) => existing.seq === item.seq)))
    const latest = events.value[events.value.length - 1]
    livePulse.value = { message: eventTitle(latest) }
    lastSignalAt.value = Date.now()
  } else if (run.value) {
    livePulse.value = { message: stateLabels[run.value.state] || run.value.state }
    lastSignalAt.value = Date.now()
  }
}

async function loadDraft() {
  if (run.value?.id) draft.value = await api(`/api/v1/runs/${run.value.id}/draft`)
}

async function confirmDraft() {
  if (!draft.value) return
  if (isPublicDemo) {
    error.value = demoReadOnlyMessage
    return
  }
  error.value = ''
  busy.value = true
  try {
    draft.value = await api(`/api/v1/drafts/${draft.value.id}/confirm`, { method: 'POST' })
    await Promise.all([refreshRun(), refreshVisibleEvents()])
    memoryCandidates.value = await api('/api/v1/memory-candidates?status=pending')
    await loadReviewData()
  } catch (reason) {
    error.value = reason.message
  } finally {
    busy.value = false
  }
}

async function showView(view) {
  activeView.value = view
  window.scrollTo({ top: 0, behavior: 'auto' })
  if (!isPublicDemo && view === 'review') await loadReviewData().catch((reason) => { reviewError.value = reason.message })
  if (!isPublicDemo && view === 'reminders') {
    await Promise.all([loadReminderSettings(), loadSubscriptionStatus()]).catch((reason) => { error.value = reason.message })
  }
}

async function loadReviewQueue() {
  reviewQueue.value = await api('/api/v1/review/queue?limit=50')
  if (!activeReviewCard.value || !reviewQueue.value.some((item) => item.id === activeReviewCard.value.id)) {
    selectReviewCard(reviewQueue.value[0] || null)
  }
}

async function loadReviewOverview() {
  reviewOverview.value = await api('/api/v1/review/overview')
}

async function loadReviewHistory() {
  reviewHistory.value = await api('/api/v1/review/history?limit=10')
}

async function loadDailyPlan() {
  dailyPlan.value = await api('/api/v1/review/daily-plan?include_overflow=true')
}

async function loadInsights() {
  insights.value = await api('/api/v1/review/insights?trend_days=30&forecast_days=14&weak_limit=5')
}

async function loadReviewData() {
  await Promise.all([loadReviewQueue(), loadReviewOverview(), loadReviewHistory(), loadDailyPlan(), loadInsights()])
  if (!activeReviewCard.value || !visibleReviewQueue.value.some((item) => item.id === activeReviewCard.value.id)) {
    selectReviewCard(visibleReviewQueue.value[0] || null)
  }
}

function selectReviewCard(card) {
  stopEvaluationPolling()
  activeReviewCard.value = card
  reviewAnswer.value = ''
  reviewAnswerResult.value = null
  reviewError.value = ''
  resetReviewAttempt()
}

async function submitReviewAnswer() {
  if (!activeReviewCard.value || !reviewAnswer.value.trim()) return
  if (isPublicDemo) {
    reviewAnswerResult.value = {
      attempt_id: `demo-attempt-${activeReviewCard.value.id}`,
      answer: reviewAnswer.value.trim(),
      answer_key: activeReviewCard.value.answer_key,
      evidence: activeReviewCard.value.evidence,
      evaluation_status: 'completed',
      evaluation: publicDemo.answerEvaluation,
    }
    return
  }
  const answer = reviewAnswer.value.trim()
  if (!reviewAnswerKey.value || reviewSubmittedAnswer.value !== answer) {
    reviewAnswerKey.value = eventKey('answer')
    reviewSubmittedAnswer.value = answer
  }
  reviewBusy.value = true
  reviewError.value = ''
  try {
    reviewAnswerResult.value = await api(`/api/v1/review/cards/${activeReviewCard.value.id}/answers`, {
      method: 'POST',
      body: JSON.stringify({
        answer,
        idempotency_key: reviewAnswerKey.value,
      }),
    })
    if (reviewAnswerResult.value.evaluation_status === 'pending') startEvaluationPolling()
  } catch (reason) {
    reviewError.value = reason.message
  } finally {
    reviewBusy.value = false
  }
}

function startEvaluationPolling() {
  stopEvaluationPolling()
  evaluationPollCount.value = 0
  const poll = async () => {
    if (!activeReviewCard.value || !reviewAnswerResult.value?.attempt_id) return
    try {
      const result = await api(`/api/v1/review/cards/${activeReviewCard.value.id}/attempts/${reviewAnswerResult.value.attempt_id}/evaluation`)
      reviewAnswerResult.value = {
        ...reviewAnswerResult.value,
        evaluation_status: result.status,
        evaluation: result.evaluation,
      }
      if (result.status !== 'pending') return
    } catch (reason) {
      if (evaluationPollCount.value >= 8) {
        reviewAnswerResult.value = { ...reviewAnswerResult.value, evaluation_status: 'failed' }
        return
      }
    }
    evaluationPollCount.value += 1
    if (evaluationPollCount.value < 10) evaluationTimer = window.setTimeout(poll, 1500)
  }
  evaluationTimer = window.setTimeout(poll, 800)
}

function stopEvaluationPolling() {
  if (evaluationTimer) window.clearTimeout(evaluationTimer)
  evaluationTimer = null
}

function switchReviewQueue(mode) {
  reviewQueueMode.value = mode
  if (!activeReviewCard.value || !visibleReviewQueue.value.some((item) => item.id === activeReviewCard.value.id)) {
    selectReviewCard(visibleReviewQueue.value[0] || null)
  }
}

async function rateReview(rating) {
  if (!activeReviewCard.value || !reviewAnswerResult.value) return
  if (isPublicDemo) {
    const reviewedCard = activeReviewCard.value
    const selectedOption = reviewedCard.rating_options.find((item) => item.rating === rating)
    reviewHistory.value = [{
      id: `demo-history-${Date.now()}`,
      card_id: reviewedCard.id,
      title: reviewedCard.title,
      source_title: reviewedCard.source_title,
      rating,
      next_due_at: selectedOption?.due_at,
    }, ...reviewHistory.value].slice(0, 10)
    reviewQueue.value = reviewQueue.value.filter((item) => item.id !== reviewedCard.id)
    if (dailyPlan.value) {
      dailyPlan.value = {
        ...dailyPlan.value,
        completed_today: dailyPlan.value.completed_today + 1,
        planned_count: Math.max(0, dailyPlan.value.planned_count - 1),
        planned_cards: dailyPlan.value.planned_cards.filter((item) => item.id !== reviewedCard.id),
      }
    }
    reviewedThisSession.value += 1
    reviewOverview.value = { ...reviewOverview.value, due_count: reviewQueue.value.length, total_active: 2 }
    selectReviewCard(visibleReviewQueue.value[0] || null)
    return
  }
  if (reviewRatingKey.value && reviewRatingValue.value !== rating) {
    reviewError.value = '上次评分的响应未确认，请用原评分重试。'
    return
  }
  if (!reviewRatingKey.value) {
    reviewRatingKey.value = eventKey('rating')
    reviewRatingValue.value = rating
  }
  reviewBusy.value = true
  reviewError.value = ''
  try {
    await api(`/api/v1/review/cards/${activeReviewCard.value.id}/ratings`, {
      method: 'POST',
      body: JSON.stringify({
        attempt_id: reviewAnswerResult.value.attempt_id,
        rating,
        idempotency_key: reviewRatingKey.value,
      }),
    })
    if (!reviewRatingAcknowledged.value) reviewedThisSession.value += 1
    reviewRatingAcknowledged.value = true
    await loadReviewData()
  } catch (reason) {
    reviewError.value = reason.message
  } finally {
    reviewBusy.value = false
  }
}

function resetReviewAttempt() {
  reviewAnswerKey.value = null
  reviewSubmittedAnswer.value = ''
  reviewRatingKey.value = null
  reviewRatingValue.value = null
  reviewRatingAcknowledged.value = false
}

async function loadReminderSettings() {
  reminder.value = await api('/api/v1/reminders/preferences')
}

async function loadSubscriptionStatus() {
  subscriptionStatus.value = await api('/api/v1/reminders/status')
}

async function saveReminderSettings() {
  if (isPublicDemo) {
    error.value = demoReadOnlyMessage
    return
  }
  reminderBusy.value = true
  reminderSaved.value = false
  error.value = ''
  try {
    reminder.value = await api('/api/v1/reminders/preferences', {
      method: 'PUT',
      body: JSON.stringify(reminder.value),
    })
    reminderSaved.value = true
  } catch (reason) {
    error.value = reason.message
  } finally {
    reminderBusy.value = false
  }
}

function eventKey(prefix) {
  const suffix = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`
  return `${prefix}-${suffix}`
}

function ratingLabel(rating) {
  return ({ 1: '忘记了', 2: '有点难', 3: '掌握了', 4: '很轻松' })[rating]
}

function ratingHint(rating) {
  const option = activeReviewCard.value?.rating_options?.find((item) => item.rating === rating)
  return option ? formatInterval(option.interval_days, option.due_at) : '计算中'
}

function formatInterval(intervalDays, dueAt) {
  const minutes = Math.max(0, Math.round(Number(intervalDays || 0) * 24 * 60))
  if (minutes < 60) return `${Math.max(1, minutes)} 分钟后`
  if (minutes < 48 * 60) return `${Math.round(minutes / 60)} 小时后`
  if (Number.isFinite(Number(intervalDays)) && Number(intervalDays) > 0) return `${Math.round(Number(intervalDays))} 天后`
  return dueAt ? formatNextDue(dueAt) : '稍后安排'
}

function formatNextDue(value) {
  if (!value) return '暂未安排'
  const due = new Date(value)
  if (Number.isNaN(due.getTime())) return '暂未安排'
  const now = new Date()
  const sameDay = due.toDateString() === now.toDateString()
  const time = new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit' }).format(due)
  return sameDay ? `今天 ${time}` : new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(due)
}

function formatHistoryRating(rating) {
  return ({ 1: '忘记了', 2: '有点难', 3: '掌握了', 4: '很轻松' })[rating] || '已评分'
}

async function decideMemory(candidate, decision) {
  error.value = ''
  if (isPublicDemo) {
    error.value = demoReadOnlyMessage
    return
  }
  try {
    await api(`/api/v1/memory-candidates/${candidate.id}/decision`, {
      method: 'POST',
      body: JSON.stringify({ decision }),
    })
    memoryCandidates.value = memoryCandidates.value.filter((item) => item.id !== candidate.id)
  } catch (reason) {
    error.value = reason.message
  }
}

function formatTime(value) {
  return new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(new Date(value))
}

function loadPublicDemo() {
  form.value = {
    ...form.value,
    title: publicDemo.source.title,
    learning_goal: publicDemo.source.learning_goal,
    content: publicDemo.source.content,
  }
  savedSource.value = publicDemo.source
  run.value = publicDemo.run
  events.value = [...publicDemo.events]
  draft.value = publicDemo.draft
  reviewQueue.value = [...publicDemo.reviewQueue]
  reviewOverview.value = { due_count: publicDemo.reviewQueue.length, total_active: publicDemo.reviewQueue.length, next_due_at: null }
  reviewHistory.value = [...publicDemo.reviewHistory]
  dailyPlan.value = structuredClone(publicDemo.dailyPlan)
  insights.value = structuredClone(publicDemo.insights)
  subscriptionStatus.value = { ...publicDemo.subscriptionStatus }
  selectReviewCard(visibleReviewQueue.value[0])
}

onBeforeUnmount(() => {
  closeStream()
  stopClock()
  stopEvaluationPolling()
})

onMounted(() => {
  if (isPublicDemo) {
    loadPublicDemo()
    return
  }
  loadReviewData().catch(() => {})
  loadReminderSettings().catch(() => {})
})
</script>

<template>
  <div class="shell mini-shell">
    <header class="mini-appbar">
      <span>Memory Agent</span>
      <b>{{ isPublicDemo ? '只读演示' : '移动客户端' }}</b>
    </header>

    <main>
      <nav class="view-tabs" aria-label="主要功能">
        <button :class="{ active: activeView === 'organize' }" @click="showView('organize')">
          <span>01</span><strong>整理</strong><small>材料转知识</small>
        </button>
        <button :class="{ active: activeView === 'review' }" @click="showView('review')">
          <span>02</span><strong>复习</strong><small>计划与统计</small><b v-if="dueCount">{{ dueCount }}</b>
        </button>
        <button :class="{ active: activeView === 'reminders' }" @click="showView('reminders')">
          <span>03</span><strong>提醒</strong><small>时间与数量</small>
        </button>
      </nav>

      <aside v-if="isPublicDemo" class="demo-banner" role="status">
        <div><b>公开只读演示</b><span>当前展示的是脱敏示例数据；交互只发生在浏览器内，不连接数据库，也不会调用模型。</span></div>
        <small>DEMO · NO PERSISTENCE</small>
      </aside>

      <section v-if="activeView === 'organize'" class="hero">
        <p class="eyebrow">QUICK CAPTURE</p>
        <h1>今天想记住什么？</h1>
        <p class="hero-copy">写下来就可以离开。原文会先保存，Agent 在后台把它整理成可练习的知识。</p>
      </section>

      <section v-if="activeView === 'organize'" class="workspace">
        <form class="source-panel" @submit.prevent="startRun">
          <div class="section-heading">
            <span>01</span>
            <div><h2>投递学习材料</h2><p>支持最多 50,000 字的长文本，或解析一个公开网页链接。</p></div>
          </div>

          <div class="source-mode-tabs" role="tablist" aria-label="材料类型">
            <button type="button" :class="{ active: sourceMode === 'text' }" :disabled="isPublicDemo || busy" @click="sourceMode = 'text'">粘贴长文本</button>
            <button type="button" :class="{ active: sourceMode === 'url' }" :disabled="isPublicDemo || busy" @click="sourceMode = 'url'">公开链接</button>
          </div>
          <label>标题 <small class="optional">可选</small><input v-model="form.title" :disabled="isPublicDemo" maxlength="300" placeholder="留空时自动生成" /></label>
          <label>这次想学会什么？ <small class="optional">可选</small><input v-model="form.learning_goal" :disabled="isPublicDemo" maxlength="500" placeholder="留空时自动整理并记住内容" /></label>
          <label v-if="sourceMode === 'text'" class="content-field">
            <span>材料正文 <small :class="{ danger: charCount > sourceMaxChars }">{{ charCount.toLocaleString() }} / {{ sourceMaxChars.toLocaleString() }}</small></span>
            <textarea v-model="form.content" :disabled="isPublicDemo" :maxlength="sourceMaxChars" placeholder="粘贴长文章、课程笔记或自己的思考……"></textarea>
          </label>
          <label v-else class="content-field url-field">
            <span>公开网页地址 <small>仅读取你提交的这个链接</small></span>
            <input v-model="form.url" :disabled="isPublicDemo" maxlength="2048" inputmode="url" placeholder="https://example.com/article" />
            <small class="field-help">不支持登录页、内网地址、文件下载和依赖登录态的内容；不会自动搜索其他网页。</small>
          </label>
          <label class="switch-row">
            <input v-model="form.web_access_allowed" :disabled="isPublicDemo" type="checkbox" />
            <span class="switch"></span>
            <span><strong>允许外部检索</strong><small>默认关闭；MVP 尚未接入真实搜索工具</small></span>
          </label>
          <p v-if="error" class="error">{{ error }}</p>
          <button class="primary" :disabled="isPublicDemo || busy || !sourceReady">
            <span>{{ isPublicDemo ? '公开演示不提交数据' : (busy && savedSource ? '材料已保存 · AI 后台整理中' : (busy ? (sourceMode === 'url' ? '正在解析链接' : '正在保存') : (sourceMode === 'url' ? '解析链接并整理' : (charCount <= 600 ? '快速记录并整理' : '开始生成知识草稿')))) }}</span><b>→</b>
          </button>
          <div v-if="savedSource" class="capture-receipt">
            <b>✓ {{ savedSource.origin_type === 'url' ? '网页正文已记录' : '原文已记录' }}</b>
            <span>{{ savedSource.char_count }} 字 · {{ savedSource.save_ms }} ms</span>
            <p>AI 整理在后台继续；记录不会因为模型等待或重试而丢失。</p>
          </div>
        </form>

        <aside class="run-panel">
          <div class="section-heading compact">
            <span>02</span>
            <div><h2>Agent 在做什么</h2><p>展示可审计的计划、动作、工具和校验结果；不展示隐式思维链。</p></div>
          </div>
          <div v-if="busy || run" class="activity-card">
            <div class="activity-orb" :class="{ active: busy }"><i></i></div>
            <div>
              <small>CURRENT ACTION · {{ processingMode }}</small>
              <strong>{{ currentActivity }}</strong>
              <p>已用时 {{ formatElapsed(elapsedSeconds) }} · 最近反馈 {{ signalAge }} 秒前</p>
            </div>
          </div>
          <div v-if="!run && !busy" class="empty-run">
            <div class="orbit"><span></span></div>
            <p>提交材料后，运行事件会实时出现在这里。</p>
          </div>
          <template v-else>
            <div class="run-state">
              <div><small>RUN STATUS</small><strong>{{ run ? (stateLabels[run.state] || run.state) : '正在提交' }}</strong></div>
              <b>{{ progress }}%</b>
            </div>
            <div class="progress"><i :style="{ width: `${progress}%` }"></i></div>
            <ol v-if="events.length" class="event-list">
              <li v-for="item in events" :key="item.seq">
                <span>{{ String(item.seq).padStart(2, '0') }}</span>
                <div><strong>{{ eventTitle(item) }}</strong><small>{{ formatTime(item.created_at) }} · {{ item.event_type }}</small></div>
              </li>
            </ol>
            <div v-else class="waiting-events"><i></i><span>正在建立实时事件连接，界面会持续报告状态</span></div>
          </template>
        </aside>
      </section>

      <section v-if="activeView === 'organize' && draft" class="draft-section">
        <div class="section-heading">
          <span>03</span>
          <div><h2>知识草稿</h2><p>{{ draft.agent_summary.overview }} 请先审阅，再确认。</p></div>
        </div>
        <div class="unit-grid">
          <article v-for="unit in draft.units" :key="unit.id" class="unit-card">
            <header><span>UNIT {{ String(unit.position).padStart(2, '0') }}</span><b>{{ Math.round(unit.confidence * 100) }}% confidence</b></header>
            <h3>{{ unit.title }}</h3>
            <p class="objective">{{ unit.learning_objective }}</p>
            <p>{{ unit.explanation }}</p>
            <div class="question"><small>OPEN QUESTION</small><strong>{{ unit.question }}</strong></div>
            <details><summary>查看答案要点与证据</summary><ul><li v-for="point in unit.answer_key" :key="point">{{ point }}</li></ul><blockquote v-if="unit.evidence[0]">“{{ unit.evidence[0].quote }}”</blockquote></details>
          </article>
        </div>
        <button v-if="draft.status !== 'confirmed'" class="confirm" :disabled="busy" @click="confirmDraft">我已审阅，确认这份草稿 <span>✓</span></button>
        <p v-else class="confirmed">草稿已确认。学习目标已成为待审批的记忆候选。</p>
      </section>

      <section v-if="activeView === 'organize' && memoryCandidates.length" class="memory-section">
        <div class="section-heading compact"><span>04</span><div><h2>记忆审批</h2><p>草稿确认不等于长期记忆写入。</p></div></div>
        <article v-for="candidate in memoryCandidates" :key="candidate.id" class="memory-card">
          <div><small>{{ candidate.kind }} · PENDING</small><strong>{{ candidate.content }}</strong><p>{{ candidate.rationale }}</p></div>
          <div><button @click="decideMemory(candidate, 'reject')">不保存</button><button class="approve" @click="decideMemory(candidate, 'approve')">批准写入</button></div>
        </article>
      </section>

      <section v-if="activeView === 'review'" class="review-page">
        <header class="page-intro">
          <div>
            <p class="eyebrow">ACTIVE RECALL</p>
            <h1>不是再看一遍，<em>而是先讲出来。</em></h1>
            <p>先独立回答，再对照答案要点与原文证据。你的自评是本阶段调度的最终依据。</p>
          </div>
          <div class="review-stats">
            <article><small>DUE NOW</small><strong>{{ dueCount }}</strong><span>当前待复习</span></article>
            <article><small>TODAY PLAN</small><strong>{{ todayCount }}</strong><span>建议今日完成</span></article>
          </div>
        </header>

        <section v-if="dailyPlan" class="daily-plan-card">
          <div>
            <small>DAILY LOAD</small>
            <strong>今日已完成 {{ dailyPlan.completed_today }} / {{ dailyPlan.daily_limit }}</strong>
            <p v-if="dailyPlan.overflow_count">还有 {{ dailyPlan.overflow_count }} 张超出今日软上限，可在“全部到期”中查看。</p>
            <p v-else>当前负载在你设置的每日上限内。</p>
          </div>
          <span>{{ dailyPlan.balance_status === 'overloaded' ? '已平衡' : '负载合适' }}</span>
        </section>

        <div class="queue-mode" role="tablist" aria-label="复习队列范围">
          <button :class="{ active: reviewQueueMode === 'today' }" @click="switchReviewQueue('today')">今日计划 <b>{{ todayCount }}</b></button>
          <button :class="{ active: reviewQueueMode === 'all' }" @click="switchReviewQueue('all')">全部到期 <b>{{ dueCount }}</b></button>
        </div>

        <div v-if="reviewError" class="error">{{ reviewError }}</div>
        <div v-if="!visibleReviewQueue.length" class="review-empty">
          <span>ALL CLEAR</span>
          <h2>{{ reviewQueueMode === 'today' ? '今日计划已完成。' : '全部到期队列已清空。' }}</h2>
          <p>确认新的知识草稿后，系统会立即生成首轮复习；完成自评后会按掌握程度安排下次时间。</p>
          <p v-if="reviewOverview.next_due_at" class="next-due">下一次复习：{{ formatNextDue(reviewOverview.next_due_at) }}</p>
          <button @click="showView('organize')">去整理新材料 <b>→</b></button>
        </div>

        <div v-else class="review-workspace">
          <aside class="review-queue">
            <div class="queue-heading"><span>今日队列</span><b>{{ dueCount }}</b></div>
            <button
              v-for="(card, index) in visibleReviewQueue"
              :key="card.id"
              :class="{ active: activeReviewCard?.id === card.id }"
              @click="selectReviewCard(card)"
            >
              <span>{{ String(index + 1).padStart(2, '0') }}</span>
              <div><strong>{{ card.title }}</strong><small>{{ card.source_title }} · 第 {{ card.review_count + 1 }} 次</small></div>
            </button>
          </aside>

          <article v-if="activeReviewCard" class="answer-panel">
            <div class="answer-meta">
              <span>OPEN QUESTION</span>
              <small>{{ activeReviewCard.source_title }}</small>
            </div>
            <h2>{{ activeReviewCard.question }}</h2>
            <p class="objective">学习目标：{{ activeReviewCard.learning_objective }}</p>

            <template v-if="!reviewAnswerResult">
              <label class="answer-field">
                先用自己的话回答
                <textarea v-model="reviewAnswer" placeholder="不要急着看答案。把你记得的机制、原因或步骤讲出来……"></textarea>
              </label>
              <button class="primary answer-submit" :disabled="reviewBusy || !reviewAnswer.trim()" @click="submitReviewAnswer">
                <span>{{ reviewBusy ? '正在保存回答' : '提交回答并对照要点' }}</span><b>→</b>
              </button>
            </template>

            <template v-else>
              <section class="answer-compare">
                <div><small>YOUR ANSWER</small><p>{{ reviewAnswerResult.answer }}</p></div>
                <div class="reference-answer"><small>ANSWER KEY</small><ul><li v-for="point in reviewAnswerResult.answer_key" :key="point">{{ point }}</li></ul></div>
                <blockquote v-if="reviewAnswerResult.evidence?.[0]">“{{ reviewAnswerResult.evidence[0].quote }}”<cite>原文证据</cite></blockquote>
              </section>
              <section v-if="evaluationStatus !== 'disabled'" class="ai-evaluation" :class="`is-${evaluationStatus}`">
                <header>
                  <div><small>AI ADVISORY</small><h3>AI 建议，不代替你评分</h3></div>
                  <span v-if="evaluationStatus === 'completed'">建议 {{ answerEvaluation?.suggested_rating }} 级</span>
                  <span v-else-if="evaluationStatus === 'pending'">后台评估中</span>
                  <span v-else>本次未完成</span>
                </header>
                <div v-if="evaluationStatus === 'pending'" class="evaluation-pending"><i></i><p>回答已保存，你可以立即自评；AI 结果会自动刷新。</p></div>
                <template v-else-if="evaluationStatus === 'completed' && answerEvaluation">
                  <p class="evaluation-summary">{{ answerEvaluation.summary }}</p>
                  <div class="evaluation-columns">
                    <div><b>已覆盖</b><p v-for="item in answerEvaluation.covered_points" :key="`covered-${item.point_index}`">{{ item.point }}</p><small v-if="!answerEvaluation.covered_points?.length">暂无明确覆盖点</small></div>
                    <div><b>建议补充</b><p v-for="item in answerEvaluation.missing_points" :key="`missing-${item.point_index}`">{{ item.suggestion }}</p><small v-if="!answerEvaluation.missing_points?.length">要点已基本覆盖</small></div>
                  </div>
                </template>
                <p v-else class="evaluation-summary">评估失败不会阻塞复习，请仍根据对照结果自行评分。</p>
              </section>
              <section class="self-rating">
                <div><span>最后一步</span><h3>对照之后，你掌握得怎么样？</h3><p>当前 MVP 不让 AI 替你做最终判断；你的选择会直接决定下次复习时间。</p></div>
                <div class="rating-grid">
                  <button v-for="rating in [1, 2, 3, 4]" :key="rating" :disabled="reviewBusy" @click="rateReview(rating)">
                    <b>{{ rating }}</b><strong>{{ ratingLabel(rating) }}</strong><small>{{ ratingHint(rating) }}</small>
                  </button>
                </div>
              </section>
            </template>
          </article>
        </div>

        <section v-if="insights" class="insights-section">
          <div class="insights-heading"><div><small>LEARNING INSIGHTS</small><h2>复习趋势与薄弱知识</h2></div><span>自评掌握率 {{ masteryPercent }}%</span></div>
          <div class="insight-grid">
            <article class="trend-card">
              <header><strong>近 7 天复习</strong><small>已完成 {{ insights.trend.summary.completed_count }} 次 · 连续 {{ insights.trend.summary.current_streak }} 天</small></header>
              <div class="trend-bars">
                <div v-for="point in trendPoints" :key="point.date"><i :style="{ height: `${Math.max(6, (point.completed_count / maxTrendValue) * 68)}px` }"></i><b>{{ point.completed_count }}</b><small>{{ point.date.slice(5) }}</small></div>
              </div>
              <p>统计来自你的最终自评，不将 AI 建议当作正确率。</p>
            </article>
            <article class="weak-card">
              <header><strong>优先加强</strong><small>仅排名已有复习样本的知识</small></header>
              <div v-for="card in insights.weak_cards.slice(0, 3)" :key="card.card_id">
                <span>{{ Math.round(card.weakness_score) }}</span><p><b>{{ card.title }}</b><small>{{ card.source_title }} · {{ card.confidence === 'high' ? '高' : (card.confidence === 'medium' ? '中' : '低') }}置信度</small></p>
              </div>
              <p v-if="!insights.weak_cards.length" class="no-weakness">样本还不足，完成几次复习后再生成薄弱点。</p>
            </article>
          </div>
          <article class="workload-card">
            <div><strong>未来 7 天负载建议</strong><small>只调整展示计划，不改动 FSRS 到期时间</small></div>
            <div class="workload-days"><span v-for="day in insights.workload.daily.slice(0, 7)" :key="day.date"><b>{{ day.recommended_count }}</b><small>{{ day.date.slice(5) }}</small></span></div>
          </article>
        </section>

        <section v-if="reviewHistory.length" class="review-history">
          <div class="history-heading"><div><span>RECENT REVIEWS</span><h2>最近复习</h2></div><small>保留最近 {{ reviewHistory.length }} 条</small></div>
          <article v-for="item in reviewHistory" :key="item.id" class="history-item">
            <div><strong>{{ item.title }}</strong><small>{{ item.source_title }} · {{ formatHistoryRating(item.rating) }}</small></div>
            <span>下次 {{ formatNextDue(item.next_due_at) }}</span>
          </article>
        </section>
      </section>

      <section v-if="activeView === 'reminders'" class="reminder-page">
        <header class="page-intro reminder-intro">
          <div>
            <p class="eyebrow">REVIEW RHYTHM</p>
            <h1>让复习出现得<em>刚刚好。</em></h1>
            <p>设置每日复习节奏与 AI 建议。微信提醒需要你在小程序内主动授权，H5 只展示状态和能力边界。</p>
          </div>
        </header>

        <div class="reminder-layout">
          <form class="reminder-form" @submit.prevent="saveReminderSettings">
            <label class="setting-row switch-row">
              <input v-model="reminder.enabled" :disabled="isPublicDemo" type="checkbox" />
              <span class="switch"></span>
              <span><strong>启用复习提醒</strong><small>保留每日提醒时间与待复习队列设置</small></span>
            </label>
            <label class="setting-row">
              <span><strong>提醒时间</strong><small>每天优先在这个时间开始复习</small></span>
              <input v-model="reminder.preferred_time" type="time" :disabled="isPublicDemo || !reminder.enabled" />
            </label>
            <label class="setting-row">
              <span><strong>每日上限</strong><small>避免一次出现太多任务</small></span>
              <input v-model.number="reminder.daily_limit" type="number" min="1" max="100" :disabled="isPublicDemo || !reminder.enabled" />
            </label>
            <label class="setting-row switch-row">
              <input v-model="reminder.overdue_enabled" type="checkbox" :disabled="isPublicDemo || !reminder.enabled" />
              <span class="switch"></span>
              <span><strong>包含逾期内容</strong><small>优先补回已经错过的复习</small></span>
            </label>
            <label class="setting-row switch-row">
              <input v-model="reminder.ai_evaluation_enabled" type="checkbox" :disabled="isPublicDemo" />
              <span class="switch"></span>
              <span><strong>AI 回答建议</strong><small>后台评估缺失点与建议等级；你始终做最终评分</small></span>
            </label>
            <div class="timezone-row"><span>时区</span><strong>{{ reminder.timezone }}</strong></div>
            <p v-if="error" class="error">{{ error }}</p>
            <p v-if="reminderSaved" class="saved-message">✓ 提醒偏好已保存</p>
            <button class="primary" :disabled="isPublicDemo || reminderBusy"><span>{{ isPublicDemo ? '公开演示不保存设置' : (reminderBusy ? '正在保存' : '保存提醒设置') }}</span><b>→</b></button>
          </form>

          <aside class="delivery-status">
            <p class="eyebrow">DELIVERY STATUS</p>
            <h2>微信一次性订阅</h2>
            <ol>
              <li class="done"><span>01</span><div><strong>到期队列与定时调度</strong><p>服务端按偏好时间生成发送任务，不依赖 H5 持续打开。</p></div></li>
              <li class="done"><span>02</span><div><strong>小程序内用户授权</strong><p>必须由用户点击触发；每次接受一次，对应一次可用发送机会。</p></div></li>
            </ol>
            <div class="subscription-card">
              <span>可用授权次数</span><strong>{{ subscriptionStatus?.available_grants ?? 0 }}</strong>
              <button disabled>请在微信小程序内授权</button>
            </div>
            <div class="scope-note"><b>H5 能力边界</b><p>H5 不调用 wx.requestSubscribeMessage，也不模拟授权结果。请在微信小程序的明确按钮点击中完成授权。</p></div>
          </aside>
        </div>
      </section>
    </main>
  </div>
</template>

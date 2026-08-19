<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { publicDemo } from './demoData.js'

const apiBase = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000'
const isPublicDemo = import.meta.env.VITE_PUBLIC_DEMO === 'true'
const demoReadOnlyMessage = '公开演示模式不会连接后端、写入数据或调用模型。'
const sourceMaxChars = 50000
const form = ref({
  title: '',
  content: '',
  content_type: 'markdown',
})
const busy = ref(false)
const error = ref('')
const run = ref(null)
const events = ref([])
const draft = ref(null)
const memoryCandidates = ref([])
const savedSource = ref(null)
const tasksExpanded = ref(true)
const conversations = ref([])
const activeConversation = ref(null)
const conversationTurns = ref([])
const historyOpen = ref(false)
const historyError = ref('')
const composerPlaceholder = ref('发消息、粘贴文章或链接…')
const composerTextarea = ref(null)
const attachmentSheetOpen = ref(false)
const expandedTurnIds = ref(new Set())
const expandedResultId = ref('')
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
const canceling = ref(false)
let eventSource = null
let clock = null
let evaluationTimer = null
let submitController = null
let stoppingAfterServerError = false
let pendingTurn = null

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
const sourceReady = computed(() => Boolean(form.value.content.trim()) && charCount.value <= sourceMaxChars)
const inputLooksLikeUrl = computed(() => /^https?:\/\/\S+$/i.test(form.value.content.trim()))
const showCharacterCount = computed(() => charCount.value >= sourceMaxChars * 0.8)
const groupedConversations = computed(() => {
  const now = new Date()
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  const yesterday = today - 24 * 60 * 60 * 1000
  const groups = new Map()
  conversations.value.forEach((conversation) => {
    const time = new Date(conversation.updated_at).getTime()
    const label = time >= today ? '今天' : time >= yesterday ? '昨天' : '更早'
    if (!groups.has(label)) groups.set(label, [])
    groups.get(label).push(conversation)
  })
  return [...groups].map(([label, items]) => ({ label, items }))
})
const runFinished = computed(() => ['awaiting_user', 'completed', 'failed', 'cancelled', 'budget_exhausted'].includes(run.value?.state))
const validSchemaEvent = computed(() => {
  for (let index = events.value.length - 1; index >= 0; index -= 1) {
    const item = events.value[index]
    if (item.event_type === 'tool.completed' && item.payload?.tool === 'schema_validate' && item.payload?.result_summary?.valid === true) return item
  }
  return null
})
const latestDraftEvent = computed(() => {
  for (let index = events.value.length - 1; index >= 0; index -= 1) {
    if (events.value[index].event_type === 'draft.created') return events.value[index]
  }
  return null
})
const finalizing = computed(() => Boolean(
  validSchemaEvent.value
  && (!latestDraftEvent.value || latestDraftEvent.value.seq < validSchemaEvent.value.seq),
))
const finalizingElapsedSeconds = computed(() => {
  if (!finalizing.value) return 0
  const began = Date.parse(validSchemaEvent.value?.created_at || '')
  if (!Number.isFinite(began)) return 0
  const now = startedAt.value ? startedAt.value + (elapsedSeconds.value * 1000) : Date.now()
  return Math.max(0, Math.floor((now - began) / 1000))
})
const finalizingMessage = computed(() => {
  if (finalizingElapsedSeconds.value < 8) return '正在完成整理…'
  if (finalizingElapsedSeconds.value < 20) return '正在整理知识结构和引用…'
  return '内容较多，仍在处理中…'
})
const processingFacts = computed(() => {
  const facts = []
  const source = [...events.value].reverse().find((item) => item.event_type === 'source.loaded')
  const locate = [...events.value].reverse().find((item) => item.event_type === 'tool.completed' && item.payload?.tool === 'source_locate_quotes')
  if (source?.payload?.char_count) facts.push(`已读取 ${source.payload.char_count} 字材料`)
  if (locate) facts.push(`已找到 ${locate.payload?.result_summary?.resolved_count || 0} 条原文依据`)
  if (validSchemaEvent.value) facts.push(`已校验 ${validSchemaEvent.value.payload?.result_summary?.unit_count || 0} 个知识单元`)
  return facts
})
const processingMode = computed(() => {
  const plan = events.value.find((item) => item.event_type === 'agent.plan_created')
  if (!plan) return '正在判断处理通道'
  return plan?.payload?.processing_mode === 'quick' ? '短内容快速通道' : '完整 Agent 通道'
})
const currentActivity = computed(() => {
  if (finalizing.value) return finalizingMessage.value
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

function friendlyError(reason, fallback = '操作失败，请重试') {
  const message = String(reason?.message || '')
  if (/not found/i.test(message)) return '当前云端版本尚未包含对话接口，请部署最新后端后重试。'
  return message || fallback
}

function parsedTurnUrl(content) {
  const value = String(content || '').trim()
  if (!/^https?:\/\/\S+$/i.test(value)) return null
  try { return new URL(value) } catch { return null }
}

function turnIsUrl(turn) { return Boolean(parsedTurnUrl(turn.user_content)) }
function turnHostname(turn) { return parsedTurnUrl(turn.user_content)?.hostname.replace(/^www\./, '') || '' }
function turnShortUrl(turn) {
  const url = parsedTurnUrl(turn.user_content)
  return url ? `${url.origin}${url.pathname === '/' ? '' : url.pathname}`.slice(0, 96) : ''
}
function turnIsLongText(turn) { return !turnIsUrl(turn) && String(turn.user_content || '').length > 420 }
function turnPreview(turn) { return String(turn.user_content || '').slice(0, 140) }
function turnExpanded(turn) { return expandedTurnIds.value.has(turn.id) }

function toggleTurn(turn) {
  const next = new Set(expandedTurnIds.value)
  if (next.has(turn.id)) next.delete(turn.id)
  else next.add(turn.id)
  expandedTurnIds.value = next
}

function chooseQuickAction(placeholder) {
  composerPlaceholder.value = placeholder
  nextTick(() => composerTextarea.value?.focus())
}

function resizeComposer(event) {
  const textarea = event.currentTarget
  textarea.style.height = 'auto'
  textarea.style.height = `${Math.min(textarea.scrollHeight, 180)}px`
  textarea.style.overflowY = textarea.scrollHeight > 180 ? 'auto' : 'hidden'
}

function addLinkFromSheet() {
  attachmentSheetOpen.value = false
  composerPlaceholder.value = '粘贴一个 https:// 开头的公开链接…'
  nextTick(() => composerTextarea.value?.focus())
}

async function pasteFromClipboard() {
  attachmentSheetOpen.value = false
  try {
    const content = await navigator.clipboard.readText()
    form.value.content = content.slice(0, sourceMaxChars)
    nextTick(() => composerTextarea.value?.focus())
  } catch {
    error.value = '浏览器未允许读取剪贴板，请直接在输入框中粘贴。'
  }
}

async function loadConversations() {
  try {
    conversations.value = await api('/api/v1/conversations?limit=50')
    historyError.value = ''
  } catch (reason) {
    historyError.value = friendlyError(reason, '历史对话加载失败，请重试。')
  }
}

async function openConversation(conversationId) {
  if (busy.value) return
  try {
    const detail = await api(`/api/v1/conversations/${conversationId}`)
    activeConversation.value = detail.conversation
    conversationTurns.value = detail.turns
    const latest = detail.turns[detail.turns.length - 1]
    run.value = latest?.run_id ? { id: latest.run_id, state: latest.run_state } : null
    draft.value = latest?.draft || null
    events.value = []
    livePulse.value = null
    error.value = ''
    if (window.innerWidth <= 900) historyOpen.value = false
    await loadConversations()
  } catch (reason) {
    error.value = friendlyError(reason, '对话加载失败，请重试。')
  }
}

function resetConversationState() {
  pendingTurn = null
  activeConversation.value = null
  conversationTurns.value = []
  run.value = null
  draft.value = null
  events.value = []
  livePulse.value = null
  form.value.content = ''
  composerPlaceholder.value = '发消息、粘贴文章或链接…'
  expandedResultId.value = ''
  error.value = ''
  if (window.innerWidth <= 900) historyOpen.value = false
}

async function stopActiveRun({ notify = true } = {}) {
  if (canceling.value) return false
  canceling.value = true
  const controller = submitController
  submitController = null
  controller?.abort()
  closeStream()
  stopClock()
  try {
    if (run.value?.id && !runFinished.value) {
      const cancelled = await api(`/api/v1/runs/${run.value.id}/cancel`, {
        method: 'POST',
        body: '{}',
      })
      run.value = { ...cancelled, state: 'cancelled' }
    }
    busy.value = false
    tasksExpanded.value = false
    if (notify) error.value = '本次请求已停止，不会自动重试。'
    return true
  } catch (reason) {
    busy.value = false
    tasksExpanded.value = false
    error.value = `客户端已停止等待，但服务器取消失败：${friendlyError(reason)}`
    return false
  } finally {
    canceling.value = false
  }
}

async function terminateAfterServerError(message = '') {
  if (stoppingAfterServerError) return
  stoppingAfterServerError = true
  await stopActiveRun({ notify: false })
  error.value = message
    ? `服务器处理失败，本次请求已终止：${message}`
    : '服务器连接失败，本次请求已终止，不会自动重试。'
  stoppingAfterServerError = false
}

async function newConversation() {
  if (canceling.value) return
  if (busy.value) {
    const confirmed = window.confirm('当前任务仍在运行。停止任务并新建对话吗？')
    if (!confirmed) return
    if (!await stopActiveRun({ notify: false })) return
  }
  resetConversationState()
}

async function refreshActiveConversation() {
  if (!activeConversation.value?.id) return
  const detail = await api(`/api/v1/conversations/${activeConversation.value.id}`)
  activeConversation.value = detail.conversation
  conversationTurns.value = detail.turns
  await loadConversations()
}

async function startRun() {
  if (busy.value) return
  error.value = ''
  if (isPublicDemo) {
    error.value = demoReadOnlyMessage
    return
  }
  if (!sourceReady.value) {
    error.value = `请输入长文本或公开链接，且不要超过 ${sourceMaxChars.toLocaleString()} 字。`
    return
  }
  busy.value = true
  run.value = null
  events.value = []
  draft.value = null
  memoryCandidates.value = []
  savedSource.value = null
  tasksExpanded.value = true
  startedAt.value = Date.now()
  elapsedSeconds.value = 0
  lastSignalAt.value = Date.now()
  livePulse.value = { message: '请求已收到，正在保存材料并创建任务' }
  closeStream()
  startClock()
  const requestController = new AbortController()
  submitController = requestController
  try {
    let conversation = activeConversation.value
    if (!conversation) {
      conversation = await api('/api/v1/conversations', {
        method: 'POST',
        body: '{}',
        signal: requestController.signal,
      })
      activeConversation.value = conversation
    }
    const submittedInput = form.value.content.trim()
    if (
      !pendingTurn
      || pendingTurn.conversationId !== conversation.id
      || pendingTurn.input !== submittedInput
    ) {
      pendingTurn = {
        conversationId: conversation.id,
        input: submittedInput,
        key: `web-turn-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`,
      }
    }
    const started = await api(`/api/v1/conversations/${conversation.id}/turns`, {
      method: 'POST',
      body: JSON.stringify({
        input: submittedInput,
        title: null,
        content_type: form.value.content_type,
        web_access_allowed: false,
        idempotency_key: pendingTurn.key,
      }),
      signal: requestController.signal,
    })
    pendingTurn = null
    if (submitController === requestController) submitController = null
    activeConversation.value = started.conversation
    conversationTurns.value = [...conversationTurns.value, started.turn]
    form.value.content = ''
    livePulse.value = { message: '材料已保存，Agent 正在生成可复习的知识草稿' }
    lastSignalAt.value = Date.now()
    run.value = started.run
    livePulse.value = { message: '任务已创建，正在连接实时运行轨迹' }
    lastSignalAt.value = Date.now()
    openStream(run.value.id)
  } catch (reason) {
    if (submitController === requestController) submitController = null
    if (reason?.name !== 'AbortError') error.value = friendlyError(reason)
    busy.value = false
    stopClock()
  }
}

function openStream(runId) {
  const source = new EventSource(`${apiBase}/api/v1/runs/${runId}/events/stream`)
  eventSource = source
  const eventTypes = [
    'run.created', 'run.state_changed', 'source.loaded', 'memory.retrieved',
    'skills.selected', 'agent.plan_created', 'agent.decision', 'tool.started',
    'tool.completed', 'tool.failed', 'draft.created', 'run.failed',
    'memory.candidate_created', 'checkpoint.created', 'run.retryable_error',
    'run.retry_scheduled', 'run.recovery_started', 'run.expired_lease_recovered',
    'review.cards_created',
  ]
  eventTypes.forEach((eventType) => {
    source.addEventListener(eventType, (message) => consumeEvent(message))
  })
  source.addEventListener('progress.pulse', (message) => consumePulse(message))
  source.addEventListener('stream.closed', async () => {
    if (eventSource !== source) return
    await refreshRun()
    if (run.value?.state === 'awaiting_user') await loadDraft()
    await refreshActiveConversation().catch(() => {})
    if (runFinished.value) tasksExpanded.value = false
    busy.value = false
    stopClock()
    closeStream()
  })
  source.onerror = () => {
    if (eventSource !== source) return
    void terminateAfterServerError()
  }
}

function consumeEvent(message) {
  const item = JSON.parse(message.data)
  if (!events.value.some((existing) => existing.seq === item.seq)) events.value.push(item)
  livePulse.value = { message: eventTitle(item) }
  lastSignalAt.value = Date.now()
  if (item.event_type === 'run.retryable_error') {
    void terminateAfterServerError(item.payload?.message || '')
    return
  }
  if (item.event_type === 'run.state_changed') {
    run.value = { ...run.value, state: item.payload.state }
    if (['awaiting_user', 'completed', 'failed', 'cancelled', 'budget_exhausted'].includes(item.payload.state)) {
      tasksExpanded.value = false
    }
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
  if (!run.value?.id) return
  const loaded = await api(`/api/v1/runs/${run.value.id}/draft`)
  draft.value = loaded
  conversationTurns.value = conversationTurns.value.map((turn) => (
    turn.run_id === run.value.id
      ? {
          ...turn,
          assistant_summary: loaded.agent_summary?.overview || turn.assistant_summary,
          draft: loaded,
        }
      : turn
  ))
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
    await refreshActiveConversation()
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
    content: publicDemo.source.content,
  }
  savedSource.value = publicDemo.source
  run.value = publicDemo.run
  events.value = [...publicDemo.events]
  draft.value = publicDemo.draft
  const demoConversation = {
    id: 'demo-conversation',
    title: publicDemo.source.title,
    title_status: 'generated',
    turn_count: 1,
    created_at: publicDemo.run.created_at,
    updated_at: publicDemo.run.finished_at || publicDemo.run.created_at,
  }
  activeConversation.value = demoConversation
  conversations.value = [demoConversation]
  conversationTurns.value = [{
    id: 'demo-turn',
    position: 1,
    user_content: publicDemo.source.content,
    source_id: publicDemo.source.id,
    run_id: publicDemo.run.id,
    run_state: publicDemo.run.state,
    assistant_summary: publicDemo.draft.agent_summary.overview,
    draft: publicDemo.draft,
    created_at: publicDemo.run.created_at,
  }]
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
  loadConversations().catch(() => {})
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

      <section v-if="activeView === 'organize'" class="agent-chat-layout">
        <button v-if="historyOpen" class="history-backdrop" aria-label="关闭历史记录" @click="historyOpen = false"></button>
        <aside class="chat-history" :class="{ open: historyOpen }">
          <header><strong>历史对话</strong><button type="button" aria-label="关闭历史记录" @click="historyOpen = false">×</button></header>
          <button type="button" class="new-chat" :disabled="canceling" @click="newConversation"><span>✎</span> 新对话</button>
          <div class="history-list">
            <template v-for="group in groupedConversations" :key="group.label">
              <p class="history-group-label">{{ group.label }}</p>
              <button
                v-for="item in group.items"
                :key="item.id"
                type="button"
                :class="{ active: activeConversation?.id === item.id }"
                :disabled="busy"
                @click="openConversation(item.id)"
              ><strong>{{ item.title }}</strong><small>{{ item.turn_count }} 次整理</small></button>
            </template>
            <div v-if="historyError" class="history-error"><span>{{ historyError }}</span><button type="button" @click="loadConversations">重试</button></div>
            <p v-else-if="!conversations.length" class="history-empty">发送第一条消息后，对话会保存在这里。</p>
          </div>
        </aside>

        <section class="chat-main">
          <header class="chat-header">
            <button type="button" class="history-trigger" aria-label="打开历史对话" @click="historyOpen = true">☰</button>
            <strong>{{ activeConversation?.title || '整理' }}</strong>
            <button type="button" class="compose-trigger" aria-label="新建对话" :disabled="canceling" @click="newConversation">✎</button>
          </header>

          <div class="chat-thread">
            <div v-if="error" class="organize-inline-error"><span>{{ error }}</span><button type="button" @click="error = ''">关闭</button></div>

            <div v-if="!conversationTurns.length && !busy" class="chat-welcome">
              <span>M</span><h1>今天想记住什么？</h1><p>粘贴文章、链接，或者直接问我</p>
              <div class="quick-action-chips">
                <button type="button" @click="chooseQuickAction('粘贴要总结的文章或链接…')">总结文章</button>
                <button type="button" @click="chooseQuickAction('写下你想记住的知识…')">记录知识</button>
                <button type="button" @click="chooseQuickAction('写下需要梳理的想法…')">整理想法</button>
                <button type="button" @click="chooseQuickAction('写下你想理解的问题…')">问一个问题</button>
              </div>
            </div>

            <template v-for="turn in conversationTurns" :key="turn.id">
              <article class="user-turn">
                <div v-if="turnIsUrl(turn)" class="source-preview-card"><span>🌐</span><div><small>网页</small><strong>{{ turnHostname(turn) }}</strong><p>{{ turnShortUrl(turn) }}</p></div></div>
                <div v-else-if="turnIsLongText(turn)" class="source-preview-card document-card"><span>📄</span><div><small>长文本 · {{ turn.user_content.length.toLocaleString() }} 字</small><p :class="{ expanded: turnExpanded(turn) }">{{ turnExpanded(turn) ? turn.user_content : turnPreview(turn) }}</p><button type="button" @click="toggleTurn(turn)">{{ turnExpanded(turn) ? '收起原文' : '展开原文' }}</button></div></div>
                <p v-else>{{ turn.user_content }}</p>
              </article>

              <article v-if="turn.assistant_summary || turn.draft" class="assistant-turn">
                <span class="assistant-avatar">M</span>
                <div><small class="intent-badge">知识整理</small><p v-if="turn.assistant_summary" class="assistant-copy">{{ turn.assistant_summary }}</p>
                  <section v-if="turn.draft" class="memory-result-card">
                    <header><span>🧠</span><div><strong>已整理 {{ turn.draft.units.length }} 条记忆</strong><small>{{ turn.draft.units.length }} 个知识点 · {{ turn.draft.units.length }} 个复习问题</small></div></header>
                    <ul><li v-for="unit in turn.draft.units.slice(0, 3)" :key="unit.id">{{ unit.title }}</li></ul>
                    <button type="button" class="result-toggle" @click="expandedResultId = expandedResultId === turn.draft.id ? '' : turn.draft.id"><span>{{ expandedResultId === turn.draft.id ? '收起' : '查看全部' }}</span><b>{{ expandedResultId === turn.draft.id ? '⌃' : '›' }}</b></button>
                    <div v-if="expandedResultId === turn.draft.id" class="result-details">
                      <article v-for="unit in turn.draft.units" :key="unit.id"><strong>{{ unit.position }}. {{ unit.title }}</strong><p>{{ unit.explanation }}</p><div><small>复习问题</small><b>{{ unit.question }}</b></div><blockquote v-if="unit.evidence[0]">“{{ unit.evidence[0].quote }}”</blockquote></article>
                      <button v-if="turn.draft.id === draft?.id && turn.draft.status !== 'confirmed'" class="confirm" :disabled="busy" @click="confirmDraft">确认并加入复习</button>
                      <p v-else-if="turn.draft.status === 'confirmed'" class="confirmed">✓ 已加入复习队列</p>
                    </div>
                  </section>
                </div>
              </article>
            </template>

            <article v-if="busy" class="assistant-turn processing-turn"><span class="assistant-avatar">M</span><div>
              <button type="button" class="inline-processing" @click="tasksExpanded = !tasksExpanded"><i></i><span>{{ currentActivity || '正在整理…' }}</span><b>{{ tasksExpanded ? '⌃' : '⌄' }}</b></button>
              <div v-show="tasksExpanded" class="processing-details">
                <p v-if="finalizing" class="processing-status-detail">{{ finalizingMessage }} 已处理 {{ formatElapsed(finalizingElapsedSeconds) }}</p>
                <ul v-if="processingFacts.length" class="processing-facts"><li v-for="fact in processingFacts" :key="fact">✓ {{ fact }}</li></ul>
                <ol v-if="events.length"><li v-for="item in events" :key="item.seq"><span>{{ eventTitle(item) }}</span><small>{{ formatTime(item.created_at) }}</small></li></ol>
                <button type="button" class="stop-run" :disabled="canceling" @click="stopActiveRun">{{ canceling ? '正在停止…' : '停止本次任务' }}</button>
              </div>
            </div></article>

            <article v-for="candidate in memoryCandidates" :key="candidate.id" class="chat-memory-approval">
              <small>长期记忆建议</small><strong>{{ candidate.content }}</strong><p>{{ candidate.rationale }}</p>
              <footer><button type="button" @click="decideMemory(candidate, 'reject')">忽略</button><button type="button" @click="decideMemory(candidate, 'approve')">保存</button></footer>
            </article>
          </div>

          <form class="chat-composer" @submit.prevent="startRun">
            <textarea ref="composerTextarea" v-model="form.content" rows="1" :disabled="isPublicDemo || busy" :maxlength="sourceMaxChars" :placeholder="composerPlaceholder" @input="resizeComposer"></textarea>
            <footer class="composer-toolbar">
              <button type="button" class="attachment-trigger" aria-label="添加内容" :disabled="isPublicDemo || busy" @click="attachmentSheetOpen = true">＋</button>
              <span class="composer-spacer"></span><small v-if="showCharacterCount">{{ charCount.toLocaleString() }} / {{ sourceMaxChars.toLocaleString() }}</small>
              <button class="send-message" :disabled="isPublicDemo || busy || !sourceReady" aria-label="发送给 Agent">↑</button>
            </footer>
          </form>
        </section>

        <div v-if="attachmentSheetOpen" class="composer-sheet-backdrop" @click="attachmentSheetOpen = false"></div>
        <section class="composer-sheet" :class="{ open: attachmentSheetOpen }"><i></i><header><strong>添加内容</strong><button type="button" @click="attachmentSheetOpen = false">完成</button></header><button type="button" @click="addLinkFromSheet">添加链接</button><button type="button" @click="pasteFromClipboard">粘贴剪贴板内容</button></section>
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
            <label class="setting-row limit-range-setting">
              <span><strong>每日上限</strong><small>避免一次出现太多任务</small></span>
              <span class="range-control">
                <strong>{{ reminder.daily_limit }} 条</strong>
                <input v-model.number="reminder.daily_limit" type="range" min="1" max="100" step="1" :disabled="isPublicDemo || !reminder.enabled" />
                <small><i>1</i><i>100</i></small>
              </span>
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

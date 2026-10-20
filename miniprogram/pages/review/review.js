const api = require('../../utils/api')
const { idempotencyKey } = require('../../utils/ids')
const { openEvidence } = require('../../utils/evidence')

function nextDueLabel(value) {
  if (!value) return '暂未安排'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '暂未安排'
  const now = new Date()
  const sameDay = date.getFullYear() === now.getFullYear() && date.getMonth() === now.getMonth() && date.getDate() === now.getDate()
  const time = `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`
  if (sameDay) return `今天 ${time}`
  return `${date.getMonth() + 1} 月 ${date.getDate()} 日 ${time}`
}

function intervalLabel(intervalDays, dueAt) {
  const minutes = Math.max(0, Math.round(Number(intervalDays || 0) * 24 * 60))
  if (minutes < 60) return `${Math.max(1, minutes)} 分钟后`
  if (minutes < 48 * 60) return `${Math.round(minutes / 60)} 小时后`
  if (Number.isFinite(Number(intervalDays)) && Number(intervalDays) > 0) return `${Math.round(Number(intervalDays))} 天后`
  return nextDueLabel(dueAt)
}

function ratingsFor(card, suggestedRating) {
  const options = card && Array.isArray(card.rating_options) ? card.rating_options : []
  const labels = {
    1: ['忘记了', 'again'],
    2: ['有点难', 'hard'],
    3: ['掌握了', 'good'],
    4: ['很轻松', 'easy'],
  }
  return [1, 2, 3, 4].map((value) => {
    const option = options.find((item) => item.rating === value)
    return {
      value,
      label: labels[value][0],
      tone: labels[value][1],
      hint: option ? intervalLabel(option.interval_days, option.due_at) : '计算中',
      recommended: Number(suggestedRating) === value,
      recommendedClass: Number(suggestedRating) === value ? 'rating-button--recommended' : '',
    }
  })
}

function historyFor(items) {
  return (items || []).map((item) => ({ ...item, next_due_label: nextDueLabel(item.next_due_at) }))
}

function dateLabel(value) {
  if (!value) return '暂无'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '暂无'
  return `${date.getFullYear()} 年 ${date.getMonth() + 1} 月 ${date.getDate()} 日`
}

function decorateKnowledgeSet(item) {
  return {
    ...item,
    created_label: dateLabel(item.created_at),
    last_reviewed_label: item.last_reviewed_at ? dateLabel(item.last_reviewed_at) : '尚未复习',
    source_label: item.source && item.source.context_type === 'url'
      ? item.source.title
      : (item.source && item.source.context_type === 'conversation' ? '对话整理' : '直接输入'),
  }
}

function decorateDailyPlan(plan) {
  const value = plan || {}
  const completed = Number(value.completed_today || 0)
  const planned = Number(value.planned_count || 0)
  const goal = completed + planned
  return {
    daily_limit: Number(value.daily_limit || 10),
    completed_today: completed,
    planned_count: planned,
    due_now_count: Number(value.due_now_count || 0),
    overflow_count: Number(value.overflow_count || 0),
    must_do_count: Number(value.must_do_count || 0),
    planned_cards: value.planned_cards || [],
    progress_percent: goal > 0 ? Math.min(100, Math.round((completed / goal) * 100)) : 100,
    balance_status: value.balance_status || 'balanced',
  }
}

function plannedQueueFor(allQueue, dailyPlan) {
  const byId = new Map((allQueue || []).map((card) => [card.id, card]))
  return (dailyPlan.planned_cards || []).map((item) => byId.get(item.id)).filter(Boolean)
}

const reminderOptions = [
  { label: '1分钟后', seconds: 60 },
  { label: '10分钟后', seconds: 600 },
  { label: '1小时后', seconds: 3600 },
  { label: '明天', seconds: 86400 },
]

Page({
  data: {
    loading: true,
    busy: false,
    syncing: false,
    queue: [],
    allQueue: [],
    showAllDue: false,
    activeIndex: 0,
    activeCard: null,
    answer: '',
    answerResult: null,
    hintsVisible: false,
    sessionCount: 0,
    lastScheduleText: '',
    error: '',
    overview: { due_count: 0, total_active: 0, next_due_at: null, next_due_label: '' },
    history: [],
    dailyPlan: decorateDailyPlan(null),
    planAvailable: true,
    evaluationWaitingLong: false,
    evaluationExpanded: false,
    ratings: ratingsFor(null),
    section: 'practice',
    activeKnowledgeSetId: '',
    knowledgeSets: [],
    knowledgeDetail: null,
    expandedUnitId: '',
    completedCard: null,
    cardReminder: null,
    reminderBusy: false,
  },

  onShow() {
    this._hidden = false
    const tabBar = typeof this.getTabBar === 'function' && this.getTabBar()
    if (tabBar) tabBar.setData({ selected: 1 })
    if (this.data.busy) return
    const practiceSetId = wx.getStorageSync('memoryAgentPracticeSetId')
    if (practiceSetId) {
      wx.removeStorageSync('memoryAgentPracticeSetId')
      this.setData({ section: 'practice', activeKnowledgeSetId: practiceSetId, showAllDue: true })
      this.loadReviewData()
      return
    }
    const knowledgeSetId = wx.getStorageSync('memoryAgentKnowledgeSetId')
    if (knowledgeSetId) {
      wx.removeStorageSync('memoryAgentKnowledgeSetId')
      this.openKnowledgeSetById(knowledgeSetId)
    } else if (this.data.section === 'practice') {
      this.loadReviewData({ silent: Boolean(this.data.queue.length || this.data.activeCard) })
    }
  },

  onPullDownRefresh() {
    this.loadReviewData().finally(() => wx.stopPullDownRefresh())
  },

  onHide() {
    this._hidden = true
    this.saveCardState()
    this.stopEvaluationPolling()
  },

  onUnload() {
    this._unloaded = true
    this._loadId = (this._loadId || 0) + 1
    this.stopEvaluationPolling()
  },

  async loadReviewData({ silent = false } = {}) {
    if (this.data.busy) return
    this.saveCardState()
    const loadId = this._loadId = (this._loadId || 0) + 1
    this._loadingData = true
    this.setData({ loading: !silent, syncing: true, error: '' })
    try {
      const setQuery = this.data.activeKnowledgeSetId
        ? `&knowledge_set_id=${encodeURIComponent(this.data.activeKnowledgeSetId)}`
        : ''
      const [allQueue, overview, history, planResult] = await Promise.all([
        api.get(`/review/queue?limit=100${setQuery}`),
        api.get('/review/overview'),
        api.get('/review/history?limit=10'),
        api.get('/review/daily-plan')
          .then((value) => ({ value }))
          .catch((error) => ({ error })),
      ])
      if (loadId !== this._loadId || this._unloaded) return
      const planAvailable = !planResult.error
      const dailyPlan = decorateDailyPlan(planAvailable ? planResult.value : null)
      const queue = this.data.activeKnowledgeSetId || this.data.showAllDue || !planAvailable
        ? allQueue
        : plannedQueueFor(allQueue, dailyPlan)
      const previousCardId = this.data.activeCard && this.data.activeCard.id
      const previousIndex = queue.findIndex((card) => card.id === previousCardId)
      const activeIndex = previousIndex >= 0
        ? previousIndex
        : Math.min(this.data.activeIndex, Math.max(0, queue.length - 1))
      const activeCard = queue[activeIndex] || null
      // A silent refresh may finish after the user has continued typing.
      this.saveCardState()
      this.stopEvaluationPolling()
      this.setData({
        queue,
        allQueue,
        activeIndex,
        activeCard,
        ...this.restoreCardState(activeCard),
        loading: false,
        syncing: false,
        busy: false,
        overview: { ...overview, next_due_label: nextDueLabel(overview.next_due_at) },
        history: historyFor(history),
        dailyPlan,
        planAvailable,
      })
      this.resumeEvaluation()
      this.updateTabBadge(allQueue.length)
      wx.removeStorageSync('memoryAgentReviewQueueDirty')
    } catch (error) {
      if (loadId === this._loadId && !this._unloaded) this.setData({ loading: false, syncing: false, busy: false, error: error.message })
    } finally { if (loadId === this._loadId) this._loadingData = false }
  },

  saveCardState() {
    const card = this.data.activeCard
    if (!card) return
    if (!this._cardStates) this._cardStates = {}
    this._cardStates[card.id] = {
      answer: this.data.answer, answerResult: this.data.answerResult,
      hintsVisible: this.data.hintsVisible, evaluationExpanded: this.data.evaluationExpanded,
      keys: [this._answerIdempotencyKey, this._answerSubmittedValue, this._ratingIdempotencyKey,
        this._ratingValue, this._ratingAcknowledged],
    }
  },

  restoreCardState(card) {
    const saved = card && this._cardStates && this._cardStates[card.id]
    this.resetAttemptKeys()
    if (saved) {
      [this._answerIdempotencyKey, this._answerSubmittedValue, this._ratingIdempotencyKey,
        this._ratingValue, this._ratingAcknowledged] = saved.keys
    }
    const answerResult = saved ? saved.answerResult : null
    return {
      answer: saved ? saved.answer : '', answerResult,
      hintsVisible: saved ? saved.hintsVisible : false,
      evaluationExpanded: saved ? saved.evaluationExpanded : false,
      evaluationWaitingLong: false,
      ratings: ratingsFor(card, answerResult && answerResult.evaluation && answerResult.evaluation.suggested_rating),
    }
  },

  resumeEvaluation() {
    const result = this.data.answerResult
    if (!this._hidden && result && result.evaluation_status === 'pending') this.startEvaluationPolling(result)
  },

  toggleEvaluation() { this.setData({ evaluationExpanded: !this.data.evaluationExpanded }) },

  viewEvidence(event) {
    const id = event.currentTarget.dataset.id
    const unit = id && this.data.knowledgeDetail && this.data.knowledgeDetail.units.find((item) => item.id === id)
    if (unit) openEvidence(unit.evidence, this.data.knowledgeDetail.source.id)
    else if (this.data.answerResult) openEvidence(this.data.answerResult.evidence)
  },

  updateTabBadge(count) {
    const tabBar = typeof this.getTabBar === 'function' && this.getTabBar()
    if (tabBar) tabBar.setData({ selected: 1, badge: count })
  },

  selectCard(event) {
    if (this.data.busy || this.data.loading || this._loadingData) return
    const activeIndex = Number(event.currentTarget.dataset.index)
    const activeCard = this.data.queue[activeIndex]
    if (!activeCard || (this.data.activeCard && this.data.activeCard.id === activeCard.id)) return
    this.saveCardState()
    this.stopEvaluationPolling()
    this.setData({
      activeIndex,
      activeCard,
      ...this.restoreCardState(activeCard),
      error: '',
    })
    this.resumeEvaluation()
  },

  toggleHints() {
    this.setData({ hintsVisible: !this.data.hintsVisible })
  },

  onAnswerInput(event) {
    if (this.data.busy || this.data.answerResult) return
    if (this._answerSubmittedValue && this._answerSubmittedValue !== event.detail.value) {
      this._answerIdempotencyKey = null
      this._answerSubmittedValue = null
    }
    this.setData({ answer: event.detail.value })
  },

  async submitAnswer() {
    const card = this.data.activeCard
    const answer = this.data.answer.trim()
    if (!card || !answer || this.data.busy || this.data.answerResult || this._loadingData) return
    const requestId = this._answerRequestId = (this._answerRequestId || 0) + 1
    const isCurrent = () => !this._unloaded && requestId === this._answerRequestId
      && this.data.activeCard && this.data.activeCard.id === card.id
    if (!this._answerIdempotencyKey) this._answerIdempotencyKey = idempotencyKey('mini-answer')
    this._answerSubmittedValue = answer
    this.setData({ busy: true, error: '' })
    try {
      const answerResult = await api.post(`/review/cards/${card.id}/answers`, {
        answer,
        idempotency_key: this._answerIdempotencyKey,
      })
      if (!isCurrent()) return
      if (answerResult.card_id !== card.id) throw new Error('回答与当前题目不匹配，请重试')
      this.setData({ answerResult, busy: false, evaluationWaitingLong: false })
      this.resumeEvaluation()
      if (answerResult.evaluation_status === 'completed' && answerResult.evaluation) {
        this.setData({ ratings: ratingsFor(card, answerResult.evaluation.suggested_rating) })
      }
    } catch (error) {
      if (isCurrent()) this.setData({ busy: false, error: error.message })
    }
  },

  async rateAnswer(event) {
    const rating = Number(event.currentTarget.dataset.rating)
    const card = this.data.activeCard
    const answerResult = this.data.answerResult
    if (!card || !answerResult || this.data.busy || this._loadingData || answerResult.card_id !== card.id) return
    if (this._ratingIdempotencyKey && this._ratingValue !== rating) {
      this.setData({ error: '上次评分的响应未确认，请用原评分重试。' })
      return
    }
    if (!this._ratingIdempotencyKey) {
      this._ratingIdempotencyKey = idempotencyKey('mini-rating')
      this._ratingValue = rating
    }
    this.setData({ busy: true, error: '' })
    try {
      const result = await api.post(`/review/cards/${card.id}/ratings`, {
        attempt_id: answerResult.attempt_id,
        rating,
        idempotency_key: this._ratingIdempotencyKey,
      })
      if (this._unloaded || !this.data.activeCard || this.data.activeCard.id !== card.id
        || !this.data.answerResult || this.data.answerResult.attempt_id !== answerResult.attempt_id) return
      this.stopEvaluationPolling()
      const nextSessionCount = this._ratingAcknowledged
        ? this.data.sessionCount
        : this.data.sessionCount + 1
      this._ratingAcknowledged = true
      const queue = this.data.queue.filter((item) => item.id !== card.id)
      const allQueue = this.data.allQueue.filter((item) => item.id !== card.id)
      const nextCard = queue[Math.min(this.data.activeIndex, Math.max(0, queue.length - 1))] || null
      const historyItem = {
        id: result.id || result.event_id || `review-${result.attempt_id || Date.now()}`,
        card_id: card.id,
        title: card.title,
        source_title: card.source_title,
        rating,
        reviewed_at: result.reviewed_at,
        next_due_at: result.next_due_at,
        interval_days: result.interval_days,
        user_rating_is_final: true,
      }
      const dailyPlan = {
        ...this.data.dailyPlan,
        completed_today: Number(this.data.dailyPlan.completed_today || 0) + 1,
        planned_count: Math.max(0, Number(this.data.dailyPlan.planned_count || 0) - 1),
        due_now_count: Math.max(0, Number(this.data.dailyPlan.due_now_count || 0) - 1),
      }
      if (this._cardStates) delete this._cardStates[card.id]
      const goal = dailyPlan.completed_today + dailyPlan.planned_count
      dailyPlan.progress_percent = goal ? Math.round(dailyPlan.completed_today / goal * 100) : 100
      this.setData({
        sessionCount: nextSessionCount,
        lastScheduleText: `已安排到 ${nextDueLabel(result.next_due_at)}`,
        completedCard: { id: card.id, title: card.title },
        cardReminder: null,
        queue,
        allQueue,
        activeIndex: Math.min(this.data.activeIndex, Math.max(0, queue.length - 1)),
        activeCard: nextCard,
        ...this.restoreCardState(nextCard),
        dailyPlan,
        overview: { ...this.data.overview, due_count: Math.max(0, Number(this.data.overview.due_count || 0) - 1) },
        history: historyFor([historyItem, ...this.data.history]).slice(0, 10),
        busy: false,
      })
      this.resumeEvaluation()
      this.updateTabBadge(allQueue.length)
      wx.showToast({ title: '已完成一题', icon: 'success' })
    } catch (error) {
      if (!this._unloaded && this.data.activeCard && this.data.activeCard.id === card.id) this.setData({ busy: false, error: error.message })
    }
  },

  showCardReminderOptions() {
    if (!this.data.completedCard || this.data.reminderBusy) return
    wx.showActionSheet({
      itemList: reminderOptions.map((item) => item.label),
      success: ({ tapIndex }) => this.createCardReminder(reminderOptions[tapIndex]),
    })
  },

  async createCardReminder(option) {
    const card = this.data.completedCard
    if (!card || !option || this.data.reminderBusy) return
    this.setData({ reminderBusy: true, error: '' })
    try {
      const reminder = await api.post(`/review/cards/${card.id}/reminders`, {
        delay_seconds: option.seconds,
      })
      this.setData({
        reminderBusy: false,
        cardReminder: { ...reminder, label: option.label },
      })
      wx.showToast({ title: '已设置提醒', icon: 'success' })
    } catch (error) {
      this.setData({ reminderBusy: false, error: '提醒设置失败，请稍后重试' })
    }
  },

  async cancelCardReminder() {
    const card = this.data.completedCard
    const reminder = this.data.cardReminder
    if (!card || !reminder || this.data.reminderBusy) return
    this.setData({ reminderBusy: true, error: '' })
    try {
      await api.delete(`/review/cards/${card.id}/reminders/${reminder.reminder_id}`)
      this.setData({ reminderBusy: false, cardReminder: null })
      wx.showToast({ title: '提醒已取消', icon: 'success' })
    } catch (error) {
      this.setData({ reminderBusy: false, error: error.message || '提醒取消失败，请稍后重试' })
    }
  },

  goToCapture() {
    wx.switchTab({ url: '/pages/capture/capture' })
  },

  goToInsights() {
    wx.navigateTo({ url: '/pages/insights/insights' })
  },

  async showPractice() {
    if (this.data.busy || this._loadingData) return
    this.setData({ section: 'practice', activeKnowledgeSetId: '', showAllDue: false })
    await this.loadReviewData()
  },

  async showKnowledgeSets() {
    if (this.data.busy || this._loadingData) return
    this.saveCardState()
    this.stopEvaluationPolling()
    this.setData({ section: 'knowledge', activeKnowledgeSetId: '', knowledgeDetail: null, loading: true, error: '' })
    try {
      const items = await api.get('/knowledge-sets')
      this.setData({ knowledgeSets: items.map(decorateKnowledgeSet), loading: false })
    } catch (error) {
      this.setData({ loading: false, error: error.message })
    }
  },

  openKnowledgeSet(event) {
    this.openKnowledgeSetById(event.currentTarget.dataset.id)
  },

  async openKnowledgeSetById(id) {
    if (this.data.busy || this._loadingData) return
    this.saveCardState()
    this.stopEvaluationPolling()
    this.setData({ section: 'knowledge', loading: true, error: '' })
    try {
      const item = await api.get(`/knowledge-sets/${id}`)
      this.setData({
        knowledgeDetail: {
          ...decorateKnowledgeSet(item),
          units: item.units.map((unit) => ({ ...unit, last_reviewed_label: unit.last_reviewed_at ? dateLabel(unit.last_reviewed_at) : '尚未复习' })),
        },
        expandedUnitId: '',
        loading: false,
      })
    } catch (error) {
      this.setData({ loading: false, error: error.message })
    }
  },

  backToKnowledgeSets() {
    this.showKnowledgeSets()
  },

  toggleKnowledgeUnit(event) {
    const id = event.currentTarget.dataset.id
    this.setData({ expandedUnitId: this.data.expandedUnitId === id ? '' : id })
  },

  promptValue(title, content) {
    return new Promise((resolve) => {
      wx.showModal({ title, content, editable: true, placeholderText: title, success: (result) => resolve(result.confirm ? result.content : null), fail: () => resolve(null) })
    })
  },

  async renameKnowledgeSet() {
    const current = this.data.knowledgeDetail
    if (!current) return
    const title = await this.promptValue('编辑知识集标题', current.title)
    if (!title || !title.trim()) return
    try {
      await api.patch(`/knowledge-sets/${current.id}`, { title: title.trim() })
      await this.openKnowledgeSetById(current.id)
    } catch (error) { this.setData({ error: error.message }) }
  },

  editKnowledgeUnit(event) {
    if (this.data.busy || !this.data.knowledgeDetail) return
    const id = event.currentTarget.dataset.id
    const unit = this.data.knowledgeDetail.units.find((item) => item.id === id)
    if (!unit) return
    wx.navigateTo({
      url: `/pages/knowledge-edit/knowledge-edit?setId=${encodeURIComponent(this.data.knowledgeDetail.id)}&unitId=${encodeURIComponent(id)}`,
      events: { saved: ({ detail, unitId }) => this.onKnowledgeEdited(detail, unitId) },
      fail: () => this.setData({ error: '编辑页打开失败，请重试。' }),
    })
  },

  onKnowledgeEdited(detail, unitId) {
    if (this._unloaded) return
    const affected = this.data.allQueue.filter((card) => card.draft_unit_id === unitId)
    affected.forEach((card) => { if (this._cardStates) delete this._cardStates[card.id] })
    if (this.data.activeCard && this.data.activeCard.draft_unit_id === unitId) {
      this.stopEvaluationPolling()
      this.resetAttemptKeys()
      this.setData({ answer: '', answerResult: null, hintsVisible: false, evaluationExpanded: false })
    }
    if (this.data.knowledgeDetail && this.data.knowledgeDetail.id === detail.id) {
      this.setData({
        knowledgeDetail: {
          ...decorateKnowledgeSet(detail),
          units: detail.units.map((unit) => ({ ...unit, last_reviewed_label: unit.last_reviewed_at ? dateLabel(unit.last_reviewed_at) : '尚未复习' })),
        },
        expandedUnitId: unitId,
        error: '',
      })
    }
  },

  goToSettings() {
    wx.switchTab({ url: '/pages/reminders/reminders' })
  },

  deleteKnowledgeUnit(event) {
    const id = event.currentTarget.dataset.id
    const unit = this.data.knowledgeDetail.units.find((item) => item.id === id)
    if (!unit) return
    wx.showModal({
      title: '删除这个知识点？',
      content: '删除后，该知识点将不再出现在后续复习中。',
      confirmColor: '#d70015',
      success: async (result) => {
        if (!result.confirm) return
        try {
          await api.delete(`/knowledge-units/${id}`)
          if (this.data.knowledgeDetail.unit_count <= 1) await this.showKnowledgeSets()
          else await this.openKnowledgeSetById(this.data.knowledgeDetail.id)
        } catch (error) { this.setData({ error: error.message }) }
      },
    })
  },

  deleteKnowledgeSet() {
    const current = this.data.knowledgeDetail
    if (!current) return
    wx.showModal({
      title: '删除知识集？',
      content: `删除后，其中 ${current.unit_count} 个知识点将不再参与复习。`,
      confirmColor: '#d70015',
      success: async (result) => {
        if (!result.confirm) return
        try { await api.delete(`/knowledge-sets/${current.id}`); await this.showKnowledgeSets() } catch (error) { this.setData({ error: error.message }) }
      },
    })
  },

  async startKnowledgeSetReview() {
    if (this.data.busy || this._loadingData) return
    const current = this.data.knowledgeDetail
    if (!current || !current.due_count) return
    this.setData({ section: 'practice', activeKnowledgeSetId: current.id, showAllDue: true })
    await this.loadReviewData()
  },

  copySourceUrl() {
    const url = this.data.knowledgeDetail && this.data.knowledgeDetail.source.origin_url
    if (url) wx.setClipboardData({ data: url, success: () => wx.showToast({ title: '链接已复制', icon: 'none' }) })
  },

  showAllDueCards() {
    if (this.data.showAllDue || this.data.busy || this._loadingData) return
    this.saveCardState()
    const queue = this.data.allQueue
    const activeCard = queue[0] || null
    this.stopEvaluationPolling()
    this.setData({
      showAllDue: true,
      queue,
      activeIndex: 0,
      activeCard,
      ...this.restoreCardState(activeCard),
    })
    this.resumeEvaluation()
  },

  showTodayPlan() {
    if (this.data.busy || this._loadingData) return
    this.saveCardState()
    if (this.data.activeKnowledgeSetId) {
      this.setData({ activeKnowledgeSetId: '', showAllDue: false })
      this.loadReviewData()
      return
    }
    if (!this.data.showAllDue) return
    const queue = plannedQueueFor(this.data.allQueue, this.data.dailyPlan)
    const activeCard = queue[0] || null
    this.stopEvaluationPolling()
    this.setData({
      showAllDue: false,
      queue,
      activeIndex: 0,
      activeCard,
      ...this.restoreCardState(activeCard),
    })
    this.resumeEvaluation()
  },

  startEvaluationPolling(answerResult) {
    this.stopEvaluationPolling()
    const generation = this._evaluationGeneration
    const isCurrent = () => !this._hidden && !this._unloaded && generation === this._evaluationGeneration
      && this.data.activeCard && this.data.activeCard.id === answerResult.card_id
      && this.data.answerResult && this.data.answerResult.attempt_id === answerResult.attempt_id
    this._evaluationPollCount = 0
    const poll = async () => {
      if (!isCurrent()) return
      this._evaluationPollCount += 1
      try {
        const result = await api.get(`/review/cards/${answerResult.card_id}/attempts/${answerResult.attempt_id}/evaluation`)
        if (!isCurrent()) return
        if (result.status === 'completed') {
          const nextAnswerResult = {
            ...this.data.answerResult,
            evaluation_status: 'completed',
            evaluation: result.evaluation,
          }
          this.setData({
            answerResult: nextAnswerResult,
            evaluationWaitingLong: false,
            ratings: ratingsFor(this.data.activeCard, result.evaluation && result.evaluation.suggested_rating),
          })
          return
        }
        if (result.status === 'failed' || result.status === 'disabled') {
          this.setData({
            'answerResult.evaluation_status': result.status,
            evaluationWaitingLong: false,
          })
          return
        }
      } catch (error) {
        if (!isCurrent()) return
        if (this._evaluationPollCount >= 3) this.setData({ evaluationWaitingLong: true })
      }
      if (!isCurrent()) return
      if (this._evaluationPollCount >= 4) this.setData({ evaluationWaitingLong: true })
      if (this._evaluationPollCount < 30) {
        this._evaluationTimer = setTimeout(poll, 1200)
      }
    }
    this._evaluationTimer = setTimeout(poll, 600)
  },

  stopEvaluationPolling() {
    this._evaluationGeneration = (this._evaluationGeneration || 0) + 1
    if (this._evaluationTimer) clearTimeout(this._evaluationTimer)
    this._evaluationTimer = null
    this._evaluationPollCount = 0
  },

  resetAttemptKeys() {
    this._answerIdempotencyKey = null
    this._answerSubmittedValue = null
    this._ratingIdempotencyKey = null
    this._ratingValue = null
    this._ratingAcknowledged = false
  },
})

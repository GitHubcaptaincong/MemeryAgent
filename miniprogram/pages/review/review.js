const api = require('../../utils/api')
const { idempotencyKey } = require('../../utils/ids')

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

Page({
  data: {
    loading: true,
    busy: false,
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
    ratings: ratingsFor(null),
    section: 'practice',
    activeKnowledgeSetId: '',
    knowledgeSets: [],
    knowledgeDetail: null,
    expandedUnitId: '',
  },

  onShow() {
    const tabBar = typeof this.getTabBar === 'function' && this.getTabBar()
    if (tabBar) tabBar.setData({ selected: 1 })
    const knowledgeSetId = wx.getStorageSync('memoryAgentKnowledgeSetId')
    if (knowledgeSetId) {
      wx.removeStorageSync('memoryAgentKnowledgeSetId')
      this.openKnowledgeSetById(knowledgeSetId)
    } else {
      this.loadReviewData()
    }
  },

  onPullDownRefresh() {
    this.loadReviewData().finally(() => wx.stopPullDownRefresh())
  },

  onHide() {
    this.stopEvaluationPolling()
  },

  onUnload() {
    this.stopEvaluationPolling()
  },

  async loadReviewData() {
    this.setData({ loading: true, error: '' })
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
      this.stopEvaluationPolling()
      this.setData({
        queue,
        allQueue,
        activeIndex,
        activeCard,
        answer: '',
        answerResult: null,
        hintsVisible: false,
        loading: false,
        busy: false,
        overview: { ...overview, next_due_label: nextDueLabel(overview.next_due_at) },
        history: historyFor(history),
        dailyPlan,
        planAvailable,
        evaluationWaitingLong: false,
        ratings: ratingsFor(activeCard),
      })
      if (previousCardId !== (activeCard && activeCard.id)) this.resetAttemptKeys()
      this.updateTabBadge(allQueue.length)
      wx.removeStorageSync('memoryAgentReviewQueueDirty')
    } catch (error) {
      this.setData({ loading: false, busy: false, error: error.message })
    }
  },

  updateTabBadge(count) {
    const tabBar = typeof this.getTabBar === 'function' && this.getTabBar()
    if (tabBar) tabBar.setData({ selected: 1, badge: count })
  },

  selectCard(event) {
    const activeIndex = Number(event.currentTarget.dataset.index)
    const activeCard = this.data.queue[activeIndex]
    this.stopEvaluationPolling()
    this.setData({
      activeIndex,
      activeCard,
      answer: '',
      answerResult: null,
      hintsVisible: false,
      error: '',
      evaluationWaitingLong: false,
      ratings: ratingsFor(activeCard),
    })
    this.resetAttemptKeys()
  },

  toggleHints() {
    this.setData({ hintsVisible: !this.data.hintsVisible })
  },

  onAnswerInput(event) {
    if (this._answerSubmittedValue && this._answerSubmittedValue !== event.detail.value) {
      this._answerIdempotencyKey = null
      this._answerSubmittedValue = null
    }
    this.setData({ answer: event.detail.value })
  },

  async submitAnswer() {
    const card = this.data.activeCard
    const answer = this.data.answer.trim()
    if (!card || !answer || this.data.busy) return
    if (!this._answerIdempotencyKey) this._answerIdempotencyKey = idempotencyKey('mini-answer')
    this._answerSubmittedValue = answer
    this.setData({ busy: true, error: '' })
    try {
      const answerResult = await api.post(`/review/cards/${card.id}/answers`, {
        answer,
        idempotency_key: this._answerIdempotencyKey,
      })
      this.setData({ answerResult, busy: false, evaluationWaitingLong: false })
      if (answerResult.evaluation_status === 'pending') this.startEvaluationPolling(answerResult)
      if (answerResult.evaluation_status === 'completed' && answerResult.evaluation) {
        this.setData({ ratings: ratingsFor(card, answerResult.evaluation.suggested_rating) })
      }
    } catch (error) {
      this.setData({ busy: false, error: error.message })
    }
  },

  async rateAnswer(event) {
    const rating = Number(event.currentTarget.dataset.rating)
    const card = this.data.activeCard
    const answerResult = this.data.answerResult
    if (!card || !answerResult || this.data.busy) return
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
      this.stopEvaluationPolling()
      const nextSessionCount = this._ratingAcknowledged
        ? this.data.sessionCount
        : this.data.sessionCount + 1
      this._ratingAcknowledged = true
      this.setData({
        sessionCount: nextSessionCount,
        lastScheduleText: `已安排到 ${nextDueLabel(result.next_due_at)}`,
      })
      await this.loadReviewData()
      wx.showToast({ title: '已完成一题', icon: 'success' })
    } catch (error) {
      this.setData({ busy: false, error: error.message })
    }
  },

  goToCapture() {
    wx.switchTab({ url: '/pages/capture/capture' })
  },

  goToInsights() {
    wx.navigateTo({ url: '/pages/insights/insights' })
  },

  async showPractice() {
    this.setData({ section: 'practice', activeKnowledgeSetId: '', showAllDue: false })
    await this.loadReviewData()
  },

  async showKnowledgeSets() {
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

  async editKnowledgeUnit(event) {
    const id = event.currentTarget.dataset.id
    const unit = this.data.knowledgeDetail.units.find((item) => item.id === id)
    if (!unit) return
    const question = await this.promptValue('编辑复习问题', unit.question)
    if (!question || !question.trim()) return
    const answer = await this.promptValue('编辑答案要点', unit.answer)
    if (!answer || !answer.trim()) return
    try {
      await api.patch(`/knowledge-units/${id}`, { question: question.trim(), answer: answer.trim() })
      await this.openKnowledgeSetById(this.data.knowledgeDetail.id)
    } catch (error) { this.setData({ error: error.message }) }
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
    if (this.data.showAllDue) return
    const queue = this.data.allQueue
    const activeCard = queue[0] || null
    this.stopEvaluationPolling()
    this.setData({
      showAllDue: true,
      queue,
      activeIndex: 0,
      activeCard,
      answer: '',
      answerResult: null,
      hintsVisible: false,
      ratings: ratingsFor(activeCard),
    })
    this.resetAttemptKeys()
  },

  showTodayPlan() {
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
      answer: '',
      answerResult: null,
      hintsVisible: false,
      ratings: ratingsFor(activeCard),
    })
    this.resetAttemptKeys()
  },

  startEvaluationPolling(answerResult) {
    this.stopEvaluationPolling()
    this._evaluationPollCount = 0
    const poll = async () => {
      if (!this.data.answerResult || this.data.answerResult.attempt_id !== answerResult.attempt_id) return
      this._evaluationPollCount += 1
      try {
        const result = await api.get(`/review/cards/${answerResult.card_id}/attempts/${answerResult.attempt_id}/evaluation`)
        if (!this.data.answerResult || this.data.answerResult.attempt_id !== answerResult.attempt_id) return
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
        if (this._evaluationPollCount >= 3) this.setData({ evaluationWaitingLong: true })
      }
      if (this._evaluationPollCount >= 4) this.setData({ evaluationWaitingLong: true })
      if (this._evaluationPollCount < 30) {
        this._evaluationTimer = setTimeout(poll, 1200)
      }
    }
    this._evaluationTimer = setTimeout(poll, 600)
  },

  stopEvaluationPolling() {
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

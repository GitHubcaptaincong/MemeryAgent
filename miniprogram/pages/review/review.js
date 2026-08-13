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
  },

  onShow() {
    const tabBar = typeof this.getTabBar === 'function' && this.getTabBar()
    if (tabBar) tabBar.setData({ selected: 1 })
    this.loadReviewData()
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
      const [allQueue, overview, history, planResult] = await Promise.all([
        api.get('/review/queue?limit=100'),
        api.get('/review/overview'),
        api.get('/review/history?limit=10'),
        api.get('/review/daily-plan')
          .then((value) => ({ value }))
          .catch((error) => ({ error })),
      ])
      const planAvailable = !planResult.error
      const dailyPlan = decorateDailyPlan(planAvailable ? planResult.value : null)
      const queue = this.data.showAllDue || !planAvailable
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

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

function ratingsFor(card) {
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
    }
  })
}

function historyFor(items) {
  return (items || []).map((item) => ({ ...item, next_due_label: nextDueLabel(item.next_due_at) }))
}

Page({
  data: {
    loading: true,
    busy: false,
    queue: [],
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

  async loadReviewData() {
    this.setData({ loading: true, error: '' })
    try {
      const [queue, overview, history] = await Promise.all([
        api.get('/review/queue?limit=50'),
        api.get('/review/overview'),
        api.get('/review/history?limit=10'),
      ])
      const previousCardId = this.data.activeCard && this.data.activeCard.id
      const activeIndex = Math.min(this.data.activeIndex, Math.max(0, queue.length - 1))
      const activeCard = queue[activeIndex] || null
      this.setData({
        queue,
        activeIndex,
        activeCard,
        answer: '',
        answerResult: null,
        hintsVisible: false,
        loading: false,
        busy: false,
        overview: { ...overview, next_due_label: nextDueLabel(overview.next_due_at) },
        history: historyFor(history),
        ratings: ratingsFor(activeCard),
      })
      if (previousCardId !== (activeCard && activeCard.id)) this.resetAttemptKeys()
      this.updateTabBadge(queue.length)
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
    this.setData({
      activeIndex,
      activeCard,
      answer: '',
      answerResult: null,
      hintsVisible: false,
      error: '',
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
      this.setData({ answerResult, busy: false })
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

  resetAttemptKeys() {
    this._answerIdempotencyKey = null
    this._answerSubmittedValue = null
    this._ratingIdempotencyKey = null
    this._ratingValue = null
    this._ratingAcknowledged = false
  },
})

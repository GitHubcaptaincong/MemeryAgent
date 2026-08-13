const api = require('../../utils/api')

function shortDate(value) {
  const parts = String(value || '').split('-')
  return parts.length === 3 ? `${Number(parts[1])}/${Number(parts[2])}` : value
}

function confidenceLabel(value) {
  return { low: '低样本', medium: '中等样本', high: '高样本' }[value] || value
}

function decorate(payload) {
  const trendDaily = (payload.trend && payload.trend.daily) || []
  const trendMax = Math.max(1, ...trendDaily.map((item) => Number(item.completed_count || 0)))
  const workloadDaily = (payload.workload && payload.workload.daily) || []
  const workloadLimit = Number((payload.workload && payload.workload.daily_limit) || 1)
  const workloadMax = Math.max(
    1,
    workloadLimit,
    ...workloadDaily.map((item) => Math.max(Number(item.canonical_due_count || 0), Number(item.recommended_count || 0))),
  )
  return {
    ...payload,
    mastery_percent: Math.round(Number(payload.summary.self_rated_mastery_rate || 0) * 100),
    trend: {
      ...payload.trend,
      daily: trendDaily.map((item) => ({
      ...item,
      date_label: shortDate(item.date),
      bar_height: 12 + Math.round((Number(item.completed_count || 0) / trendMax) * 104),
      bar_class: item.struggle_count ? 'trend-bar--struggle' : '',
      })),
    },
    weak_cards: (payload.weak_cards || []).map((item) => ({
      ...item,
      confidence_label: confidenceLabel(item.confidence),
      top_reasons: (item.reasons || []).slice(0, 2),
    })),
    weak_tags: (payload.weak_tags || []).map((item) => ({
      ...item,
      confidence_label: confidenceLabel(item.confidence),
    })),
    workload: {
      ...payload.workload,
      daily: workloadDaily.map((item) => ({
        ...item,
        date_label: shortDate(item.date),
        canonical_height: 8 + Math.round((Number(item.canonical_due_count || 0) / workloadMax) * 104),
        recommended_height: 8 + Math.round((Number(item.recommended_count || 0) / workloadMax) * 104),
        limit_height: 8 + Math.round((workloadLimit / workloadMax) * 104),
      })),
    },
  }
}

function currentRequestToken(page) {
  page._requestToken = (page._requestToken || 0) + 1
  return page._requestToken
}

Page({
  data: {
    loading: true,
    error: '',
    trendDays: 30,
    insights: null,
    range7Class: '',
    range30Class: 'range-switch--active',
  },

  onShow() {
    this.loadInsights()
  },

  onPullDownRefresh() {
    this.loadInsights().finally(() => wx.stopPullDownRefresh())
  },

  async loadInsights() {
    const requestToken = currentRequestToken(this)
    this.setData({ loading: true, error: '' })
    try {
      const payload = await api.get(`/review/insights?trend_days=${this.data.trendDays}&forecast_days=14&weak_limit=5`)
      if (requestToken !== this._requestToken) return
      this.setData({ loading: false, insights: decorate(payload) })
    } catch (error) {
      if (requestToken !== this._requestToken) return
      this.setData({ loading: false, error: error.message })
    }
  },

  changeTrendDays(event) {
    const trendDays = Number(event.currentTarget.dataset.days)
    if (trendDays === this.data.trendDays || this.data.loading) return
    this.setData({
      trendDays,
      range7Class: trendDays === 7 ? 'range-switch--active' : '',
      range30Class: trendDays === 30 ? 'range-switch--active' : '',
    })
    this.loadInsights()
  },
})

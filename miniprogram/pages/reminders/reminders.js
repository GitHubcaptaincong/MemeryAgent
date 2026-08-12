const api = require('../../utils/api')

Page({
  data: {
    loading: true,
    saving: false,
    saved: false,
    error: '',
    form: {
      enabled: true,
      preferred_time: '20:00',
      daily_limit: 10,
      overdue_enabled: true,
      timezone: 'Asia/Shanghai',
    },
  },

  onShow() {
    const tabBar = typeof this.getTabBar === 'function' && this.getTabBar()
    if (tabBar) tabBar.setData({ selected: 2 })
    this.loadPreferences()
  },

  async loadPreferences() {
    this.setData({ loading: true, error: '' })
    try {
      const form = await api.get('/reminders/preferences')
      this.setData({ loading: false, form })
    } catch (error) {
      this.setData({ loading: false, error: error.message })
    }
  },

  onEnabledChange(event) {
    this.setData({ 'form.enabled': event.detail.value, saved: false })
  },

  onTimeChange(event) {
    this.setData({ 'form.preferred_time': event.detail.value, saved: false })
  },

  onOverdueChange(event) {
    this.setData({ 'form.overdue_enabled': event.detail.value, saved: false })
  },

  changeLimit(event) {
    const delta = Number(event.currentTarget.dataset.delta)
    const dailyLimit = Math.max(1, Math.min(100, this.data.form.daily_limit + delta))
    this.setData({ 'form.daily_limit': dailyLimit, saved: false })
  },

  async savePreferences() {
    if (this.data.saving) return
    this.setData({ saving: true, saved: false, error: '' })
    try {
      const form = await api.put('/reminders/preferences', {
        enabled: this.data.form.enabled,
        preferred_time: this.data.form.preferred_time,
        daily_limit: this.data.form.daily_limit,
        overdue_enabled: this.data.form.overdue_enabled,
        timezone: this.data.form.timezone,
      })
      this.setData({ saving: false, saved: true, form })
      wx.showToast({ title: '设置已保存', icon: 'success' })
    } catch (error) {
      this.setData({ saving: false, error: error.message })
    }
  },
})
